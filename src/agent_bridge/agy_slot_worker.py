"""Standalone AGY slot worker. The service deletes by ID after a separate inventory.
This module is copied into the isolated CLI workspace and uses only the standard library.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_LIST_PAGES = 50
MAX_DELETES_PER_BATCH = 50
MAX_CLEANUP_BATCHES = 10
LIST_PAGE_SIZE = 100
RPC_TIMEOUT_SEC = 15

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_FRACTIONAL_RE = re.compile(r"(\.\d+)(?=(?:Z|[+-]\d{2}:\d{2})$)")
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
WORKER_ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_mode",
        "invalid_cutoff",
        "invalid_whitelist",
        "missing_identity",
        "missing_credentials",
        "invalid_address",
        "list_failed",
        "invalid_list",
        "pagination_limit",
        "unexpected_project",
        "delete_rejected",
        "delete_unconfirmed",
    }
)


def is_canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


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
        return dt.astimezone(UTC) if dt.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


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
    return f"{host.lower()}:{port}"


def _call_rpc(
    address: str,
    csrf_token: str,
    method: str,
    payload: dict[str, Any],
    *,
    timeout_sec: int = RPC_TIMEOUT_SEC,
) -> dict[str, Any]:
    endpoint = validate_loopback_address(address)
    host, _, port = endpoint.rpartition(":")
    # Direct numeric loopback connection: no proxy, DNS alias or redirect handling.
    host = "127.0.0.1" if host == "localhost" else host.strip("[]")
    if method not in {"ListConversations", "DeleteConversation"}:
        raise ValueError("unsupported ConversationService operation")
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout_sec)
    try:
        conn.request(
            "POST",
            f"/product.v1.ConversationService/{method}",
            json.dumps(payload).encode(),
            {
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "x-antigravity-csrf-token": csrf_token,
            },
        )
        resp = conn.getresponse()
        if resp.status != 200:
            # An explicit client-error rejection differs from losing an
            # acknowledgement after the server may have performed the delete.
            if method == "DeleteConversation" and resp.status in {400, 401, 403, 404, 409, 412, 422, 429}:
                return {"ok": False, "rejected": True}
            return {"ok": False}
        raw = resp.read()
        body = json.loads(raw) if raw else {}
        return {
            "ok": isinstance(body, dict) and "error" not in body and "code" not in body,
            "http_status": resp.status,
            "body": body,
        }
    except (OSError, ValueError, http.client.HTTPException):
        return {"ok": False}
    finally:
        conn.close()


def run_cleanup_request(
    request: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    timeout_sec: int = RPC_TIMEOUT_SEC,
) -> dict[str, Any]:
    """List the target, then perform at most one requested deletion per eligible ID."""
    source_env = os.environ if env is None else env
    started = time.monotonic_ns()
    slot = request.get("slot")
    project_id = request.get("project_id")
    allowed = request.get("allowed_project_ids")
    dry_run = request.get("dry_run")
    operation_id = request.get("operation_id")
    batch_number = request.get("batch_number", 1)
    target_ids = request.get("target_ids")
    cutoff = parse_rfc3339(request.get("cutoff_utc"))
    result: dict[str, Any] = {
        "ok": False,
        "operation_id": operation_id if is_canonical_uuid(operation_id) else None,
        "slot": slot if isinstance(slot, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", slot) else None,
        "project_id": project_id if is_canonical_uuid(project_id) else None,
        "dry_run": dry_run if type(dry_run) is bool else None,
        "batch_number": batch_number if type(batch_number) is int else None,
        "cutoff_utc": cutoff.astimezone(UTC).isoformat() if cutoff is not None else None,
        "self_conversation_id": None,
        "http_200_ids": [],
        "has_more": False,
        "checked": 0,
        "deleted": 0,
        "would_delete": 0,
        "deleted_ids": [],
        "would_delete_ids": [],
        "eligible_ids": [],
        "rejected_delete_ids": [],
        "unconfirmed_delete_ids": [],
        "skipped_running": 0,
        "skipped_recent": 0,
        "skipped_unverified": 0,
        "errors": [],
    }

    # Keep insertion order for the receipt; a repeated observation may disqualify an ID.
    eligible: dict[str, None] = {}

    def finish() -> dict[str, Any]:
        result["eligible_ids"] = list(eligible)
        result["worker_elapsed_ms"] = (time.monotonic_ns() - started) // 1_000_000
        attempted = sum(len(result[k]) for k in ("http_200_ids", "rejected_delete_ids", "unconfirmed_delete_ids"))
        result["has_more"] = dry_run is False and len(eligible) > attempted
        if dry_run is True:
            result["would_delete_ids"] = list(eligible)
            result["would_delete"] = len(eligible)
        return result

    def fail(code: str) -> dict[str, Any]:
        result["errors"].append(code)
        return finish()

    if type(dry_run) is not bool:
        return fail("invalid_mode")
    if not result["operation_id"] or not result["slot"]:
        return fail("invalid_request")
    if (
        type(batch_number) is not int
        or not 1 <= batch_number <= MAX_CLEANUP_BATCHES
        or (batch_number == 1) != (target_ids is None)
    ):
        return fail("invalid_request")
    if target_ids is not None and (
        not isinstance(target_ids, list)
        or not all(isinstance(cid, str) and _ID_RE.fullmatch(cid) for cid in target_ids)
        or len(set(target_ids)) != len(target_ids)
    ):
        return fail("invalid_request")
    targets = set(target_ids) if target_ids is not None else None
    if not result["project_id"] or not isinstance(allowed, list) or allowed != [project_id]:
        return fail("invalid_whitelist")
    if cutoff is None:
        return fail("invalid_cutoff")
    token = source_env.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    own_id = source_env.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
    if not token:
        return fail("missing_credentials")
    if not _ID_RE.fullmatch(own_id) or token in own_id:
        return fail("missing_identity")
    result["self_conversation_id"] = own_id
    try:
        address = validate_loopback_address(source_env.get("ANTIGRAVITY_LS_ADDRESS", ""))
    except ValueError:
        return fail("invalid_address")

    seen_tokens: set[str] = set()
    seen_ids: set[str] = set()
    page_token = ""
    for _page in range(MAX_LIST_PAGES):
        payload: dict[str, Any] = {"projectId": project_id, "includeArchived": True, "pageSize": LIST_PAGE_SIZE}
        if page_token:
            payload["pageToken"] = page_token
        response = _call_rpc(address, token, "ListConversations", payload, timeout_sec=timeout_sec)
        if response.get("ok") is not True:
            return fail("list_failed")
        body = response["body"]
        items = body.get("conversations", [])
        if not isinstance(items, list):
            return fail("invalid_list")
        for item in items:
            if not isinstance(item, dict):
                return fail("invalid_list")
            cid = item.get("id")
            if not isinstance(cid, str) or not _ID_RE.fullmatch(cid) or token in cid:
                return fail("invalid_list")
            if targets is not None and cid not in targets:
                continue
            if cid in seen_ids and cid not in eligible:
                continue
            if cid not in seen_ids:
                seen_ids.add(cid)
                result["checked"] += 1
            meta = item.get("metadata")
            state = item.get("state")
            if not isinstance(meta, dict) or meta.get("projectId") != project_id:
                result["skipped_unverified"] += 1
                if "unexpected_project" not in result["errors"]:
                    result["errors"].append("unexpected_project")
            elif (
                cid.casefold() == own_id.casefold()
                or not isinstance(state, dict)
                or state.get("runState") != "RUN_STATE_IDLE"
            ):
                result["skipped_running"] += 1
            elif (updated := parse_rfc3339(state.get("updateTime"))) is None or updated >= cutoff:
                result["skipped_recent"] += 1
            else:
                eligible[cid] = None
                continue
            eligible.pop(cid, None)
        page_token = body.get("nextPageToken", "")
        if not isinstance(page_token, str) or page_token in seen_tokens:
            return fail("invalid_list")
        if not page_token:
            break
        seen_tokens.add(page_token)
    else:
        return fail("pagination_limit")

    # Finish paging before mutating the collection used by the page tokens.
    if not dry_run:
        for cid in list(eligible)[:MAX_DELETES_PER_BATCH]:
            reply = _call_rpc(address, token, "DeleteConversation", {"conversationId": cid}, timeout_sec=timeout_sec)
            if reply.get("ok") is True and reply.get("http_status") == 200:
                result["http_200_ids"].append(cid)
                result["deleted_ids"].append(cid)
                result["deleted"] += 1
            elif reply.get("rejected") is True:
                result["rejected_delete_ids"].append(cid)
                if "delete_rejected" not in result["errors"]:
                    result["errors"].append("delete_rejected")
            else:
                result["unconfirmed_delete_ids"].append(cid)
                return fail("delete_unconfirmed")
    result["ok"] = not result["errors"]
    return finish()


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
    # A repeated command cannot run the same operation twice or replace its receipt.
    try:
        with out_path.with_suffix(".started").open("x", encoding="utf-8"):
            pass
    except FileExistsError:
        sys.stdout.write("AGY_SLOT_CLEANUP_ERR\n")
        return 1

    try:
        request = json.loads(req_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("invalid request")
    except (OSError, ValueError):
        _atomic_write_json(out_path, {"ok": False, "errors": ["invalid_request"]})
        sys.stdout.write("AGY_SLOT_CLEANUP_ERR\n")
        return 1

    result = run_cleanup_request(request)
    _atomic_write_json(out_path, result)
    sys.stdout.write("AGY_SLOT_CLEANUP_OK\n" if result["ok"] else "AGY_SLOT_CLEANUP_ERR\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
