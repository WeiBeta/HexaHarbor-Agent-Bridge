"""Pure-stdlib worker executed inside ``agy -p`` ``run_command`` for slot cleanup.

This module intentionally imports ONLY the Python standard library so it can be
copied verbatim into an ephemeral runner workspace and executed by whatever
``python`` binary ``agy`` resolves on ``PATH`` (without needing Agent Bridge's
virtualenv).

Security invariants:
- Never prints, logs, or persists ``ANTIGRAVITY_CSRF_TOKEN``.
- Refuses any ``project_id`` not present in ``allowed_project_ids``.
- Refuses any ``ANTIGRAVITY_LS_ADDRESS`` that is not a loopback ``host:port``.
- Only invokes official ``product.v1.ConversationService`` Connect RPCs
  (``ListConversations`` and, when ``dry_run`` is False, ``DeleteConversation``).
- Never deletes ``ANTIGRAVITY_CONVERSATION_ID`` (the cleaner's own control
  conversation under Scheme A), never deletes non-``RUN_STATE_IDLE`` sessions,
  and never deletes conversations whose ``updateTime`` is missing, unparseable,
  or at/after ``cutoff_utc``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_LIST_PAGES = 50
LIST_PAGE_SIZE = 100
RPC_TIMEOUT_SEC = 15

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_FRACTIONAL_RE = re.compile(r"(\.\d+)(?=(?:Z|[+-]\d{2}:\d{2})$)")
_SECRET_KV_RE = re.compile(
    r"(?i)(x-antigravity-csrf-token|antigravity_csrf_token|authorization|access_token|token|secret|password)"
    r"(\s*[:=]\s*)\S+"
)


def redact_text(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Scrub secret values and header-like tokens before recording diagnostics."""
    cleaned = _SECRET_KV_RE.sub(r"\1\2<redacted>", text)
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "<redacted>")
    return cleaned[:1000]


def parse_rfc3339(raw: str | None) -> datetime | None:
    """Parse an RFC 3339 timestamp (including 9-digit nanoseconds) into UTC."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # Python 3.11's fromisoformat rejects 9-digit nanoseconds; normalize to 6 digits.
    match = _FRACTIONAL_RE.search(text)
    if match:
        frac = match.group(1)[1:]
        norm_frac = "." + (frac[:6].ljust(6, "0"))
        text = text[: match.start(1)] + norm_frac + text[match.end(1) :]
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)


def validate_loopback_address(address: str) -> str:
    """Verify ``host:port`` points strictly to loopback (SSRF guard)."""
    raw = address.strip()
    if not raw or "://" in raw or "/" in raw or any(ch.isspace() for ch in raw):
        raise ValueError("ANTIGRAVITY_LS_ADDRESS must be a bare loopback host:port")
    host, sep, port_str = raw.rpartition(":")
    if not sep or host.lower() not in _LOOPBACK_HOSTS or not port_str.isdigit():
        raise ValueError("ANTIGRAVITY_LS_ADDRESS must point to a loopback host:port")
    port = int(port_str)
    if port <= 0 or port > 65535:
        raise ValueError("ANTIGRAVITY_LS_ADDRESS port out of range")
    return f"{host}:{port}"


def _call_rpc(
    address: str,
    csrf_token: str,
    method: str,
    payload: dict[str, Any],
    *,
    timeout_sec: int = RPC_TIMEOUT_SEC,
) -> dict[str, Any]:
    url = f"http://{address}/product.v1.ConversationService/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-antigravity-csrf-token": csrf_token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            body = json.loads(raw) if raw.strip() else {}
            if not isinstance(body, dict):
                return {"ok": False, "http_status": resp.status, "error": "non-object JSON response"}
            return {"ok": True, "http_status": resp.status, "body": body}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "http_status": exc.code,
            "error": redact_text(f"HTTP {exc.code}: {err_body}", secrets=(csrf_token,)),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": redact_text(f"{type(exc).__name__}: {exc}", secrets=(csrf_token,)),
        }


def run_cleanup_request(
    request: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    timeout_sec: int = RPC_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Execute one validated slot cleanup against official ConversationService."""
    source_env = os.environ if env is None else env
    slot = str(request.get("slot") or "").strip()
    project_id = str(request.get("project_id") or "").strip()
    raw_allowed = request.get("allowed_project_ids")
    allowed_ids = {
        str(item).strip()
        for item in (raw_allowed if isinstance(raw_allowed, list) else [])
        if isinstance(item, str) and item.strip()
    }
    dry_run = bool(request.get("dry_run", True))
    cutoff_raw = str(request.get("cutoff_utc") or "").strip()
    cutoff_utc = parse_rfc3339(cutoff_raw)

    result: dict[str, Any] = {
        "ok": False,
        "slot": slot,
        "project_id": project_id,
        "dry_run": dry_run,
        "checked": 0,
        "deleted": 0,
        "would_delete": 0,
        "deleted_ids": [],
        "would_delete_ids": [],
        "skipped_running": 0,
        "skipped_recent": 0,
        "errors": [],
    }

    if not project_id or not allowed_ids or project_id not in allowed_ids:
        result["errors"].append(f"whitelist rejected project_id {project_id!r}: not in configured AGY slot project IDs")
        return result
    if cutoff_utc is None:
        result["errors"].append(f"invalid cutoff_utc timestamp: {cutoff_raw!r}")
        return result

    raw_addr = str(source_env.get("ANTIGRAVITY_LS_ADDRESS") or "").strip()
    csrf_token = str(source_env.get("ANTIGRAVITY_CSRF_TOKEN") or "").strip()
    self_conv_id = str(source_env.get("ANTIGRAVITY_CONVERSATION_ID") or "").strip()

    if not raw_addr or not csrf_token:
        result["errors"].append(
            "ConversationService credentials missing in environment "
            f"(ANTIGRAVITY_LS_ADDRESS={bool(raw_addr)}, ANTIGRAVITY_CSRF_TOKEN={bool(csrf_token)})"
        )
        return result

    try:
        address = validate_loopback_address(raw_addr)
    except ValueError as exc:
        result["errors"].append(str(exc))
        return result

    conversations: list[dict[str, Any]] = []
    seen_conv_ids: set[str] = set()
    seen_tokens: set[str] = set()
    page_token = ""
    for _page in range(MAX_LIST_PAGES):
        payload: dict[str, Any] = {
            "projectId": project_id,
            "includeArchived": True,
            "pageSize": LIST_PAGE_SIZE,
        }
        if page_token:
            payload["pageToken"] = page_token
        listed = _call_rpc(address, csrf_token, "ListConversations", payload, timeout_sec=timeout_sec)
        if not listed.get("ok"):
            result["errors"].append(f"ListConversations failed: {listed.get('error', 'unknown error')}")
            return result
        body = listed.get("body") or {}
        raw_items = body.get("conversations")
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("id") or "").strip()
                if not cid or cid in seen_conv_ids:
                    continue
                seen_conv_ids.add(cid)
                conversations.append(item)
        next_token = str(body.get("nextPageToken") or "").strip()
        if not next_token:
            break
        if next_token in seen_tokens:
            result["errors"].append("ListConversations returned a repeating pageToken; aborting pagination")
            return result
        seen_tokens.add(next_token)
        page_token = next_token
    else:
        result["errors"].append(f"ListConversations exceeded {MAX_LIST_PAGES} pages; aborting")
        return result

    for conv in conversations:
        result["checked"] += 1
        cid = str(conv.get("id") or "").strip()
        raw_meta = conv.get("metadata")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        raw_state = conv.get("state")
        state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
        conv_project_id = str(meta.get("projectId") or "").strip()

        # Defense-in-depth: refuse any conversation outside the target whitelisted slot.
        if conv_project_id != project_id or conv_project_id not in allowed_ids:
            result["errors"].append(
                f"skipped conversation {cid}: unexpected projectId {conv_project_id!r} (expected {project_id!r})"
            )
            continue

        run_state = str(state.get("runState") or "").strip()
        # Scheme A: always skip our own control conversation and any non-IDLE conversation.
        if (self_conv_id and cid == self_conv_id) or run_state != "RUN_STATE_IDLE":
            result["skipped_running"] += 1
            continue

        update_dt = parse_rfc3339(state.get("updateTime"))
        if update_dt is None or update_dt >= cutoff_utc:
            result["skipped_recent"] += 1
            continue

        if dry_run:
            result["would_delete"] += 1
            result["would_delete_ids"].append(cid)
            continue

        deleted = _call_rpc(
            address,
            csrf_token,
            "DeleteConversation",
            {"conversationId": cid},
            timeout_sec=timeout_sec,
        )
        if deleted.get("ok"):
            result["deleted"] += 1
            result["deleted_ids"].append(cid)
        else:
            result["errors"].append(f"DeleteConversation({cid}) failed: {deleted.get('error', 'unknown error')}")

    result["ok"] = len(result["errors"]) == 0
    return result


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        sys.stderr.write("usage: cleanup_worker.py <request.json> <result.json>\n")
        return 2
    req_path = Path(args[0])
    out_path = Path(args[1])
    try:
        request = json.loads(req_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request payload must be a JSON object")
    except Exception as exc:
        err = {
            "ok": False,
            "slot": "",
            "project_id": "",
            "dry_run": True,
            "checked": 0,
            "deleted": 0,
            "would_delete": 0,
            "deleted_ids": [],
            "would_delete_ids": [],
            "skipped_running": 0,
            "skipped_recent": 0,
            "errors": [redact_text(f"failed to read request: {exc}")],
        }
        _atomic_write_json(out_path, err)
        sys.stdout.write("AGY_SLOT_CLEANUP_ERR\n")
        return 1

    result = run_cleanup_request(request)
    _atomic_write_json(out_path, result)
    sys.stdout.write("AGY_SLOT_CLEANUP_OK\n" if result["ok"] else "AGY_SLOT_CLEANUP_ERR\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
