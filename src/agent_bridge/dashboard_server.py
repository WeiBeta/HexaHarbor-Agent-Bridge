"""Localhost HTTP server for the HexaHarbor Agent Bridge billing dashboard.

Uses only the Python standard library (``http.server.ThreadingHTTPServer``) so
no extra runtime dependencies are introduced. Binds strictly to loopback
addresses and enforces Host-header validation + per-instance CSRF tokens on all
mutating endpoints without requiring a login flow.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import ValidationError

from agent_bridge.billing import (
    collect_agent_models,
    compute_record_cost,
    compute_summary,
    enrich_record_summary,
    filter_records,
    record_timestamp,
)
from agent_bridge.billing_pricing import (
    PRESET_MODEL_CATALOG,
    HarnessDiscountRule,
    ModelRate,
    PricingConfig,
    resolve_harness_discount,
    resolve_model_rate,
)
from agent_bridge.billing_store import (
    StoragePolicy,
    backfill_from_bridge_home,
    clear_ledger,
    enforce_retention,
    load_ledger,
    load_pricing,
    load_storage_policy,
    reset_pricing,
    save_pricing,
    save_storage_policy,
    storage_stats,
)
from agent_bridge.config import load_config
from agent_bridge.dashboard_service import (
    DASHBOARD_LAUNCH_ID_ENV,
    LOOPBACK_HOSTS,
    ServiceConfig,
    clear_pid_record,
    dashboard_update_response,
    get_autostart_status,
    initialize_first_use_preferences,
    is_tray_running,
    load_service_config,
    open_billing_folder,
    save_service_config,
    schedule_server_restart,
    schedule_server_stop,
    service_status,
    set_autostart,
    start_tray,
    stop_tray,
    write_pid_record,
)
from agent_bridge.paths import (
    bridge_home,
    dashboard_assets_dir,
    pricing_path,
    service_config_path,
    storage_policy_path,
    usage_ledger_path,
)

log = logging.getLogger(__name__)

DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 8765
MAX_REQUEST_BODY_BYTES = 1024 * 1024  # 1 MiB
_STATIC_ALLOWLIST = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


class DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        home: Path,
        csrf_token: str | None = None,
        auto_backfill: bool = True,
    ) -> None:
        host, _port = server_address
        host = host.strip().lower()
        if host not in LOOPBACK_HOSTS:
            raise ValueError(f"dashboard requires IPv4 loopback (127.0.0.1 or localhost); got {host!r}")
        self.home = home
        self.csrf_token = csrf_token or secrets.token_urlsafe(24)
        self.instance_id = os.environ.get(DASHBOARD_LAUNCH_ID_ENV) or secrets.token_urlsafe(24)
        self.started_monotonic = time.monotonic()
        self.auto_backfill = auto_backfill
        self._receipts_watermark: tuple[int, int] = (-1, -1)
        self._backfill_lock = threading.Lock()
        super().__init__(("127.0.0.1", _port), DashboardRequestHandler)

    @property
    def bound_host(self) -> str:
        return str(self.server_address[0])

    @property
    def bound_port(self) -> int:
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        host = self.bound_host
        display_host = f"[{host}]" if ":" in host else host
        return f"http://{display_host}:{self.bound_port}"

    def sync_receipts_if_changed(self) -> int:
        """Incrementally backfill newly written receipts (e.g. from older Bridge instances)."""
        if not self.auto_backfill:
            return 0
        receipts_dir = self.home / "receipts"
        if not receipts_dir.is_dir():
            return 0
        count = 0
        max_mtime_ns = 0
        try:
            with os.scandir(receipts_dir) as it:
                for entry in it:
                    if not entry.name.endswith(".json") or not entry.is_file():
                        continue
                    count += 1
                    st = entry.stat()
                    if st.st_mtime_ns > max_mtime_ns:
                        max_mtime_ns = st.st_mtime_ns
        except OSError:
            return 0
        watermark = (count, max_mtime_ns)
        if watermark == self._receipts_watermark:
            return 0
        with self._backfill_lock:
            if watermark == self._receipts_watermark:
                return 0
            try:
                result = backfill_from_bridge_home(self.home)
                imported = int(result.get("imported", 0))
            except Exception:
                log.debug("incremental receipts backfill failed", exc_info=True)
                return 0
            self._receipts_watermark = watermark
            return imported

    def data_revision(self) -> dict[str, Any]:
        """Fast stat-only revision fingerprint for frontend auto-refresh."""
        imported = self.sync_receipts_if_changed()
        parts: list[str] = []
        for path in (
            usage_ledger_path(self.home),
            pricing_path(self.home),
            storage_policy_path(self.home),
            service_config_path(self.home),
        ):
            try:
                st = path.stat()
                parts.append(f"{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append("0:0")
        return {
            "revision": "|".join(parts),
            "imported_receipts": imported,
        }


def _parse_csv_param(query: dict[str, list[str]], name: str, filter_flag: str) -> set[str] | None:
    values = query.get(name)
    explicit = (query.get(filter_flag) or ["0"])[0] == "1"
    if values is None:
        return set() if explicit else None
    items = [item.strip() for chunk in values for item in chunk.split(",") if item.strip()]
    if not items:
        return set() if explicit else None
    return set(items)


def _first(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name)
    if not values:
        return default
    return values[0]


def _configured_agents(home: Path) -> list[str]:
    cfg = load_config(home)
    names = [name for name in cfg.agents if name != "fake"]
    ordered: list[str] = []
    if "antigravity" in names:
        ordered.append("antigravity")
    ordered.extend(sorted(name for name in names if name != "antigravity"))
    return ordered


def _pricing_payload(home: Path) -> dict[str, Any]:
    pricing = load_pricing(home)
    records, _ = load_ledger(home)

    observed_counts: Counter[tuple[str, str]] = Counter()
    for rec in records:
        observed_counts[(rec.agent, rec.effective_model)] += 1

    observed_rows: list[dict[str, Any]] = []
    for (agent, model), count in observed_counts.most_common():
        match = resolve_model_rate(agent, model, pricing)
        disc = resolve_harness_discount(agent, model, pricing)
        observed_rows.append(
            {
                "agent": agent,
                "model": model,
                "tasks": count,
                "matched_key": match.matched_key if match else None,
                "source": match.source if match else "unpriced",
                "rate": match.rate.model_dump(mode="json") if match else None,
                "discount_multiplier": disc.effective_multiplier() if disc else 1.0,
                "discount_quota_boost_pct": disc.quota_boost_pct if disc else 100.0,
                "discount_label": (disc.label or f"{disc.agent}:{disc.model_pattern}") if disc else None,
            }
        )

    presets = []
    for entry in PRESET_MODEL_CATALOG:
        disc = resolve_harness_discount(entry.agent_hint, entry.key, pricing)
        presets.append(
            {
                "key": entry.key,
                "agent_hint": entry.agent_hint,
                "rate": entry.rate.model_dump(mode="json"),
                "source": entry.source,
                "discount_multiplier": disc.effective_multiplier() if disc else 1.0,
                "discount_quota_boost_pct": disc.quota_boost_pct if disc else 100.0,
                "discount_label": (disc.label or f"{disc.agent}:{disc.model_pattern}") if disc else None,
            }
        )

    return {
        "currency": "USD",
        "config": pricing.model_dump(mode="json"),
        "observed_models": observed_rows,
        "presets": presets,
    }


def _meta_payload(home: Path) -> dict[str, Any]:
    from agent_bridge.cli import package_version

    records, corrupt = load_ledger(home)
    pricing = load_pricing(home)
    configured = _configured_agents(home)
    # Include any historical agent that appeared in the ledger even if removed from config.
    extra_agents = sorted({rec.agent for rec in records if rec.agent not in configured})
    all_agents = [*configured, *extra_agents]
    counts = Counter(rec.agent for rec in records)
    agent_models = collect_agent_models(records, pricing, all_agents)
    cwds = sorted({rec.cwd for rec in records if rec.cwd})

    return {
        "version": package_version(),
        "currency": "USD",
        "default_agents": ["antigravity"] if "antigravity" in all_agents else (all_agents[:1] if all_agents else []),
        "agents": [
            {
                "name": name,
                "tasks": counts.get(name, 0),
                "models": agent_models.get(name, []),
            }
            for name in all_agents
        ],
        "cwds": cwds,
        "total_records": len(records),
        "corrupt_lines": corrupt,
        "storage": storage_stats(home),
    }


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("dashboard %s - %s", self.address_string(), format % args)

    def _allowed_hosts(self) -> set[str]:
        port = self.server.bound_port
        return {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            "127.0.0.1",
            "localhost",
        }

    def _check_host(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self._allowed_hosts():
            self._send_json(
                {"ok": False, "error": "forbidden Host header (loopback only)"},
                status=HTTPStatus.FORBIDDEN,
            )
            return False
        return True

    def _check_mutation_security(self) -> bool:
        token = (self.headers.get("X-Bridge-CSRF") or "").strip()
        if not token or not hmac.compare_digest(token, self.server.csrf_token):
            self._send_json(
                {"ok": False, "error": "missing or invalid X-Bridge-CSRF token"},
                status=HTTPStatus.FORBIDDEN,
            )
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            parsed = urlparse(origin)
            allowed = self._allowed_hosts()
            if parsed.scheme != "http" or parsed.netloc.lower() not in allowed:
                self._send_json(
                    {"ok": False, "error": "cross-origin mutation rejected"},
                    status=HTTPStatus.FORBIDDEN,
                )
                return False
        return True

    def _read_json_body(self) -> dict[str, Any] | None:
        length_raw = self.headers.get("Content-Length") or "0"
        try:
            length = int(length_raw)
        except ValueError:
            self._send_json({"ok": False, "error": "invalid Content-Length"}, status=HTTPStatus.BAD_REQUEST)
            return None
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            self._send_json({"ok": False, "error": "request body too large"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        if length == 0:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._send_json(
                {"ok": False, "error": "Content-Type must be application/json"},
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
            return None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(parsed, dict):
            self._send_json({"ok": False, "error": "JSON body must be an object"}, status=HTTPStatus.BAD_REQUEST)
            return None
        return parsed

    def _send_common_headers(self, content_type: str, content_length: int, *, cache_control: str = "no-store") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._send_common_headers(content_type, len(body))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status=status)

    def do_GET(self) -> None:
        if not self._check_host():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)

        try:
            if path in {"/", "/index.html"}:
                index_file = dashboard_assets_dir() / "index.html"
                html = index_file.read_text(encoding="utf-8").replace("__AB_CSRF_TOKEN__", self.server.csrf_token)
                self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
                return

            if path.startswith("/assets/"):
                name = path.removeprefix("/assets/")
                content_type = _STATIC_ALLOWLIST.get(name)
                if content_type is None:
                    self._send_json({"ok": False, "error": "asset not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                asset = dashboard_assets_dir() / name
                if not asset.is_file():
                    self._send_json({"ok": False, "error": "asset missing"}, status=HTTPStatus.NOT_FOUND)
                    return
                self._send_bytes(asset.read_bytes(), content_type)
                return

            if path == "/api/health":
                from agent_bridge.cli import package_version

                self._send_json(
                    {
                        "ok": True,
                        "pid": os.getpid(),
                        "instance_id": self.server.instance_id,
                        "host": self.server.bound_host,
                        "port": self.server.bound_port,
                        "uptime_sec": int(max(0.0, time.monotonic() - self.server.started_monotonic)),
                        "version": package_version(),
                    }
                )
                return

            if path == "/api/version":
                self._send_json({"ok": True, **self.server.data_revision()})
                return

            if path == "/api/service":
                self._send_json(
                    {
                        "ok": True,
                        "service": service_status(
                            self.server.home,
                            current_pid=os.getpid(),
                            current_host=self.server.bound_host,
                            current_port=self.server.bound_port,
                            started_monotonic=self.server.started_monotonic,
                        ),
                    }
                )
                return

            if path == "/api/meta":
                self.server.sync_receipts_if_changed()
                self._send_json({"ok": True, **_meta_payload(self.server.home)})
                return

            if path == "/api/summary":
                self._handle_summary(query)
                return

            if path == "/api/records":
                self._handle_records(query)
                return

            if path.startswith("/api/records/"):
                task_id = unquote(path.removeprefix("/api/records/")).strip()
                self._handle_record_detail(task_id)
                return

            if path == "/api/pricing":
                self._send_json({"ok": True, **_pricing_payload(self.server.home)})
                return

            if path == "/api/storage":
                self._send_json({"ok": True, "storage": storage_stats(self.server.home)})
                return

            if path == "/api/storage/export":
                records, _ = load_ledger(self.server.home)
                data = "".join(record.model_dump_json() + "\n" for record in records).encode("utf-8")
                self._send_bytes(
                    data,
                    "application/x-ndjson; charset=utf-8",
                    extra_headers={
                        "Content-Disposition": 'attachment; filename="agent-bridge-usage-ledger.jsonl"'
                    },
                )
                return

            self._send_json({"ok": False, "error": f"not found: {path}"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            log.exception("dashboard GET %s failed", path)
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        if not self._check_host() or not self._check_mutation_security():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()
        if body is None:
            return
        try:
            if path == "/api/pricing":
                self._handle_put_pricing(body)
                return
            if path == "/api/storage":
                self._handle_put_storage(body)
                return
            if path == "/api/service":
                self._handle_put_service(body)
                return
            self._send_json({"ok": False, "error": f"not found: {path}"}, status=HTTPStatus.NOT_FOUND)
        except (ValidationError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            log.exception("dashboard PUT %s failed", path)
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        if not self._check_host() or not self._check_mutation_security():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()
        if body is None:
            return
        try:
            if path == "/api/pricing/reset":
                reset_pricing(self.server.home)
                self._send_json({"ok": True, **_pricing_payload(self.server.home)})
                return
            if path == "/api/storage/prune":
                result = enforce_retention(self.server.home)
                self._send_json({"ok": True, "result": result, "storage": storage_stats(self.server.home)})
                return
            if path == "/api/storage/backfill":
                result = backfill_from_bridge_home(self.server.home)
                self._send_json({"ok": True, "result": result, "storage": storage_stats(self.server.home)})
                return
            if path == "/api/storage/clear":
                if body.get("confirm") != "CLEAR":
                    self._send_json(
                        {"ok": False, "error": 'confirmation required: pass {"confirm": "CLEAR"}'},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                result = clear_ledger(self.server.home)
                self._send_json({"ok": True, "result": result, "storage": storage_stats(self.server.home)})
                return
            if path == "/api/service/restart":
                cfg = load_service_config(self.server.home)
                target_host = cfg.host
                target_port = cfg.port
                schedule_server_restart(self.server.home, self.server, host=target_host, port=target_port)
                self._send_json(
                    {
                        "ok": True,
                        "status": "restarting",
                        "url": f"http://{target_host}:{target_port}",
                        "message": f"正在重启看板服务 (http://{target_host}:{target_port})…",
                    }
                )
                return
            if path == "/api/service/stop":
                schedule_server_stop(self.server)
                self._send_json({"ok": True, "status": "stopping", "message": "看板服务正在停止"})
                return
            if path == "/api/service/update":
                self._send_json({"ok": True, **dashboard_update_response()})
                return
            if path == "/api/service/open-folder":
                result = open_billing_folder(self.server.home)
                self._send_json({"ok": True, **result})
                return
            self._send_json({"ok": False, "error": f"not found: {path}"}, status=HTTPStatus.NOT_FOUND)
        except (ValidationError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            log.exception("dashboard POST %s failed", path)
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_summary(self, query: dict[str, list[str]]) -> None:
        records, corrupt = load_ledger(self.server.home)
        pricing = load_pricing(self.server.home)
        agents = _parse_csv_param(query, "agents", "agents_filter")
        models = _parse_csv_param(query, "models", "models_filter")
        filtered = filter_records(
            records,
            agents=agents,
            models=models,
            cwd=_first(query, "cwd") or None,
            range_name=_first(query, "range", "all"),
            start_iso=_first(query, "start") or None,
            end_iso=_first(query, "end") or None,
            day_filter=_first(query, "day") or None,
        )
        summary = compute_summary(filtered, pricing)
        self._send_json(
            {
                "ok": True,
                "total_ledger_records": len(records),
                "filtered_records": len(filtered),
                "corrupt_lines": corrupt,
                **summary,
            }
        )

    def _handle_records(self, query: dict[str, list[str]]) -> None:
        records, _ = load_ledger(self.server.home)
        pricing = load_pricing(self.server.home)
        agents = _parse_csv_param(query, "agents", "agents_filter")
        models = _parse_csv_param(query, "models", "models_filter")
        filtered = filter_records(
            records,
            agents=agents,
            models=models,
            status=_first(query, "status") or None,
            cwd=_first(query, "cwd") or None,
            session_id=_first(query, "session_id") or None,
            speed=_first(query, "speed") or None,
            query=_first(query, "q") or None,
            range_name=_first(query, "range", "all"),
            start_iso=_first(query, "start") or None,
            end_iso=_first(query, "end") or None,
            day_filter=_first(query, "day") or None,
        )
        filtered.sort(
            key=lambda rec: (
                (ts.timestamp() if (ts := record_timestamp(rec)) is not None else 0.0),
                rec.task_id,
            ),
            reverse=True,
        )
        try:
            offset = max(0, int(_first(query, "offset", "0")))
        except ValueError:
            offset = 0
        try:
            limit = max(1, min(200, int(_first(query, "limit", "50"))))
        except ValueError:
            limit = 50

        # Compute scope totals across all matched records.
        total_tokens = 0
        total_effective_cost = 0.0
        priced = 0
        unpriced = 0
        unmeasured = 0
        for rec in filtered:
            if rec.tokens.measured:
                total_tokens += rec.tokens.total_tokens or 0
            else:
                unmeasured += 1
            cost = compute_record_cost(rec, pricing)
            if cost.priced and cost.effective_cost_usd is not None:
                priced += 1
                total_effective_cost += cost.effective_cost_usd
            else:
                unpriced += 1

        page = [enrich_record_summary(rec, pricing) for rec in filtered[offset : offset + limit]]
        self._send_json(
            {
                "ok": True,
                "total": len(filtered),
                "offset": offset,
                "limit": limit,
                "scope_totals": {
                    "tasks": len(filtered),
                    "total_tokens": total_tokens,
                    "effective_cost_usd": round(total_effective_cost, 6) if priced > 0 else None,
                    "priced_tasks": priced,
                    "unpriced_tasks": unpriced,
                    "unmeasured_tasks": unmeasured,
                },
                "items": page,
            }
        )

    def _handle_record_detail(self, task_id: str) -> None:
        if not task_id:
            self._send_json({"ok": False, "error": "task_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        records, _ = load_ledger(self.server.home)
        pricing = load_pricing(self.server.home)
        for rec in records:
            if rec.task_id == task_id:
                self._send_json({"ok": True, "record": enrich_record_summary(rec, pricing)})
                return
        self._send_json({"ok": False, "error": f"unknown task {task_id}"}, status=HTTPStatus.NOT_FOUND)

    def _handle_put_pricing(self, body: dict[str, Any]) -> None:
        current = load_pricing(self.server.home)
        models = dict(current.models)
        discounts = list(current.harness_discounts)

        if "models" in body:
            raw_models = body["models"]
            if not isinstance(raw_models, dict):
                raise ValueError("models must be an object mapping model_id -> rate")
            models = {}
            for key, val in raw_models.items():
                k = str(key).strip()
                if not k:
                    raise ValueError("model key must be non-empty")
                models[k] = ModelRate.model_validate(val)
        elif "upsert_model" in body:
            entry = body["upsert_model"]
            if not isinstance(entry, dict):
                raise ValueError("upsert_model must be an object")
            k = str(entry.get("key") or "").strip()
            if not k:
                raise ValueError("upsert_model.key is required")
            models[k] = ModelRate.model_validate(entry.get("rate"))
        elif "delete_model" in body:
            k = str(body["delete_model"] or "").strip()
            models.pop(k, None)

        if "harness_discounts" in body:
            raw_discounts = body["harness_discounts"]
            if not isinstance(raw_discounts, list):
                raise ValueError("harness_discounts must be a list")
            discounts = [HarnessDiscountRule.model_validate(item) for item in raw_discounts]

        updated = PricingConfig(
            schema_version=1,
            currency="USD",
            models=models,
            harness_discounts=discounts,
        )
        save_pricing(updated, home=self.server.home)
        self._send_json({"ok": True, **_pricing_payload(self.server.home)})

    def _handle_put_storage(self, body: dict[str, Any]) -> None:
        current = load_storage_policy(self.server.home)
        max_bytes = current.max_bytes
        if "max_bytes" in body:
            raw_mb = body["max_bytes"]
            max_bytes = None if raw_mb is None else int(raw_mb)
        elif "max_mb" in body:
            raw_mb = body["max_mb"]
            max_bytes = None if raw_mb is None or raw_mb == "" else int(float(raw_mb) * 1024 * 1024)

        max_age_days = current.max_age_days
        if "max_age_days" in body:
            raw_days = body["max_age_days"]
            max_age_days = None if raw_days is None or raw_days == "" else int(raw_days)

        auto_rotate = current.auto_rotate
        if "auto_rotate" in body:
            auto_rotate = bool(body["auto_rotate"])

        updated = StoragePolicy(
            schema_version=1,
            max_bytes=max_bytes,
            max_age_days=max_age_days,
            auto_rotate=auto_rotate,
        )
        save_storage_policy(updated, home=self.server.home)
        self._send_json({"ok": True, "storage": storage_stats(self.server.home)})

    def _handle_put_service(self, body: dict[str, Any]) -> None:
        current = load_service_config(self.server.home)
        host = current.host
        if "host" in body and body["host"] is not None:
            host = str(body["host"]).strip().lower()
        port = current.port
        if "port" in body and body["port"] is not None and body["port"] != "":
            port = int(body["port"])
        auto_start = current.auto_start
        if "auto_start" in body:
            auto_start = bool(body["auto_start"])
        tray_enabled = current.tray_enabled
        if "tray_enabled" in body:
            tray_enabled = bool(body["tray_enabled"])

        updated = ServiceConfig(
            schema_version=1,
            host=host,
            port=port,
            auto_start=auto_start,
            tray_enabled=tray_enabled,
        )
        notes: list[str] = []
        if os.name == "nt" and "auto_start" in body:
            autostart = get_autostart_status(self.server.home)
            if updated.auto_start != autostart.get("enabled") or updated.auto_start != current.auto_start:
                set_autostart(updated.auto_start, home=self.server.home)
                notes.append("已开启开机自启" if updated.auto_start else "已关闭开机自启")
        save_service_config(updated, home=self.server.home)
        if os.name == "nt" and "tray_enabled" in body:
            tray_alive = is_tray_running(self.server.home)
            if updated.tray_enabled and not tray_alive:
                tray_res = start_tray(self.server.home, host=updated.host, port=updated.port)
                if tray_res.get("message"):
                    notes.append(str(tray_res["message"]))
            elif not updated.tray_enabled and tray_alive:
                tray_res = stop_tray(self.server.home)
                if tray_res.get("message"):
                    notes.append(str(tray_res["message"]))

        port_changed = updated.port != self.server.bound_port
        if port_changed:
            notes.append(f"端口已保存为 {updated.port}，点击「重启服务」后生效")  # noqa: RUF001

        self._send_json(
            {
                "ok": True,
                "port_restart_required": port_changed,
                "message": " · ".join(notes) if notes else "已保存服务设置",
                "service": service_status(
                    self.server.home,
                    current_pid=os.getpid(),
                    current_host=self.server.bound_host,
                    current_port=self.server.bound_port,
                    started_monotonic=self.server.started_monotonic,
                ),
            }
        )


def create_dashboard_server(
    *,
    home: Path | None = None,
    host: str = DEFAULT_DASHBOARD_HOST,
    port: int = DEFAULT_DASHBOARD_PORT,
    backfill: bool = True,
) -> DashboardHTTPServer:
    resolved = home or bridge_home()
    server = DashboardHTTPServer((host, port), home=resolved, auto_backfill=backfill)
    if backfill:
        try:
            backfill_from_bridge_home(resolved)
        except Exception:
            log.warning("initial billing backfill failed", exc_info=True)
    return server


def run_dashboard(
    *,
    home: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    resolved = home or bridge_home()
    cfg = initialize_first_use_preferences(resolved)
    eff_host = host or cfg.host
    eff_port = cfg.port if port is None else port
    server = create_dashboard_server(home=resolved, host=eff_host, port=eff_port, backfill=True)
    if cfg.tray_enabled and os.name == "nt":
        start_tray(resolved, host=server.bound_host, port=server.bound_port)
    pid_record = write_pid_record(
        resolved, host=server.bound_host, port=server.bound_port, instance_id=server.instance_id
    )
    url = server.url
    print(f"HexaHarbor Agent Bridge billing dashboard listening on {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            log.debug("could not open browser automatically", exc_info=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        clear_pid_record(resolved, expected_record=pid_record)
