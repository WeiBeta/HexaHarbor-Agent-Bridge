"""Independent append-only billing ledger, pricing store, and disk retention.

Inspired by ``@bitkyc08/opencodex``'s ``usage.jsonl`` ledger & retention model
(MIT License): rows are stored independently of ``state.json`` / ``receipts/``
pruning, never carry prompt or output bodies, and support configurable size (MB)
and age (days) rolling compaction plus manual clear/backfill.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agent_bridge.billing import (
    LedgerRecord,
    NormalizedTokens,
    _extract_raw_snapshot,
    build_ledger_record,
    record_timestamp,
)
from agent_bridge.billing_pricing import PricingConfig, default_pricing_config
from agent_bridge.paths import (
    billing_dir,
    bridge_home,
    pricing_path,
    state_path,
    storage_policy_path,
    usage_ledger_path,
)
from agent_bridge.persist import atomic_write_json, file_lock, read_json

log = logging.getLogger(__name__)

MIN_LEDGER_MAX_BYTES = 1024 * 1024  # 1 MiB minimum ceiling
DEFAULT_LEDGER_MAX_BYTES = 100 * 1024 * 1024  # 100 MiB default ceiling
DEFAULT_LEDGER_MAX_AGE_DAYS = 180  # 180 days default retention
RETENTION_TARGET_RATIO = 0.9
LOCK_TIMEOUT_SEC = 5.0
TERMINAL_STATUS_VALUES = {"completed", "failed", "cancelled"}


class StoragePolicy(BaseModel):
    """Disk retention settings for ``~/.agent-bridge/billing/usage_ledger.jsonl``."""

    schema_version: int = 1
    max_bytes: int | None = Field(default=DEFAULT_LEDGER_MAX_BYTES)
    max_age_days: int | None = Field(default=DEFAULT_LEDGER_MAX_AGE_DAYS)
    auto_rotate: bool = True

    @field_validator("max_bytes")
    @classmethod
    def _validate_max_bytes(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < MIN_LEDGER_MAX_BYTES:
            raise ValueError(f"max_bytes must be at least {MIN_LEDGER_MAX_BYTES} bytes (1 MiB) or null")
        return int(v)

    @field_validator("max_age_days")
    @classmethod
    def _validate_max_age_days(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 1 or v > 36500:
            raise ValueError("max_age_days must be between 1 and 36500 or null")
        return int(v)


def _ledger_lock(home: Path) -> Path:
    return usage_ledger_path(home).with_name("usage_ledger.jsonl.lock")


def _pricing_lock(home: Path) -> Path:
    return pricing_path(home).with_name("pricing.json.lock")


def _storage_lock(home: Path) -> Path:
    return storage_policy_path(home).with_name("storage.json.lock")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def load_pricing(home: Path | None = None) -> PricingConfig:
    resolved = home or bridge_home()
    path = pricing_path(resolved)
    if not path.is_file():
        return default_pricing_config()
    raw = read_json(path, None)
    if not isinstance(raw, dict):
        return default_pricing_config()
    try:
        return PricingConfig.model_validate(raw)
    except Exception:
        log.warning("invalid pricing.json at %s; falling back to defaults", path, exc_info=True)
        return default_pricing_config()


def save_pricing(config: PricingConfig, home: Path | None = None) -> PricingConfig:
    resolved = home or bridge_home()
    validated = PricingConfig.model_validate(config.model_dump(mode="json"))
    path = pricing_path(resolved)
    with file_lock(_pricing_lock(resolved), timeout_sec=LOCK_TIMEOUT_SEC):
        atomic_write_json(path, validated.model_dump(mode="json"))
    return validated


def reset_pricing(home: Path | None = None) -> PricingConfig:
    return save_pricing(default_pricing_config(), home=home)


def load_storage_policy(home: Path | None = None) -> StoragePolicy:
    resolved = home or bridge_home()
    path = storage_policy_path(resolved)
    if not path.is_file():
        return StoragePolicy()
    raw = read_json(path, None)
    if not isinstance(raw, dict):
        return StoragePolicy()
    try:
        return StoragePolicy.model_validate(raw)
    except Exception:
        log.warning("invalid storage.json at %s; falling back to defaults", path, exc_info=True)
        return StoragePolicy()


def save_storage_policy(policy: StoragePolicy, home: Path | None = None) -> StoragePolicy:
    resolved = home or bridge_home()
    validated = StoragePolicy.model_validate(policy.model_dump(mode="json"))
    path = storage_policy_path(resolved)
    with file_lock(_storage_lock(resolved), timeout_sec=LOCK_TIMEOUT_SEC):
        atomic_write_json(path, validated.model_dump(mode="json"))
    return validated


def _read_raw_lines(path: Path) -> list[bytes]:
    if not path.is_file():
        return []
    raw = path.read_bytes()
    if not raw:
        return []
    lines: list[bytes] = []
    for line in raw.splitlines(keepends=True):
        if not line.strip():
            continue
        if not line.endswith(b"\n"):
            # Uncommitted trailing partial line; skip cleanly.
            continue
        lines.append(line)
    return lines


def load_ledger(home: Path | None = None) -> tuple[list[LedgerRecord], int]:
    """Read all valid ``LedgerRecord`` entries and return ``(records, corrupt_line_count)``."""
    resolved = home or bridge_home()
    path = usage_ledger_path(resolved)
    if not path.is_file():
        return [], 0
    records: list[LedgerRecord] = []
    corrupt = 0
    for raw_line in _read_raw_lines(path):
        try:
            obj = json.loads(raw_line.decode("utf-8"))
            records.append(LedgerRecord.model_validate(obj))
        except Exception:
            corrupt += 1
    return records, corrupt


def _last_cumulative_by_session(records: list[LedgerRecord]) -> dict[str, NormalizedTokens]:
    """Reconstruct the latest raw cumulative snapshot per Antigravity session."""
    latest: dict[str, NormalizedTokens] = {}
    for rec in records:
        if rec.agent.strip().lower() != "antigravity" or not rec.session_id:
            continue
        snap = _extract_raw_snapshot(rec.agent, rec.usage_raw)
        if snap.measured:
            latest[rec.session_id] = snap
    return latest


def _encode_record(record: LedgerRecord) -> bytes:
    line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return (line + "\n").encode("utf-8")


def _enforce_retention_locked(
    home: Path,
    policy: StoragePolicy,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = usage_ledger_path(home)
    if not path.is_file():
        return {
            "changed": False,
            "removed_by_age": 0,
            "removed_by_size": 0,
            "removed_bytes": 0,
            "remaining_records": 0,
            "remaining_bytes": 0,
        }

    lines = _read_raw_lines(path)
    original_bytes = sum(len(line) for line in lines)
    if not lines:
        return {
            "changed": False,
            "removed_by_age": 0,
            "removed_by_size": 0,
            "removed_bytes": 0,
            "remaining_records": 0,
            "remaining_bytes": 0,
        }

    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)

    removed_by_age = 0
    kept_after_age: list[bytes] = []
    if policy.max_age_days is not None:
        cutoff = current_time - timedelta(days=policy.max_age_days)
        for raw_line in lines:
            try:
                rec = LedgerRecord.model_validate(json.loads(raw_line.decode("utf-8")))
                ts = record_timestamp(rec)
            except Exception:
                # Preserve unreadable lines? Drop only clearly expired valid rows; keep unparseable
                # out of age eviction unless size limit removes them.
                kept_after_age.append(raw_line)
                continue
            if ts is not None and ts < cutoff:
                removed_by_age += 1
            else:
                kept_after_age.append(raw_line)
    else:
        kept_after_age = list(lines)

    removed_by_size = 0
    kept = kept_after_age
    size_after_age = sum(len(line) for line in kept)
    if policy.max_bytes is not None and size_after_age > policy.max_bytes and len(kept) > 1:
        target = max(1, int(policy.max_bytes * RETENTION_TARGET_RATIO))
        running = size_after_age
        drop_idx = 0
        # Trim oldest whole rows until remaining size <= target, preserving at least 1 newest row.
        while drop_idx < len(kept) - 1 and running > target:
            running -= len(kept[drop_idx])
            drop_idx += 1
        if drop_idx > 0:
            removed_by_size = drop_idx
            kept = kept[drop_idx:]

    remaining_bytes = sum(len(line) for line in kept)
    changed = (removed_by_age > 0) or (removed_by_size > 0)
    if changed:
        _atomic_write_bytes(path, b"".join(kept))

    return {
        "changed": changed,
        "removed_by_age": removed_by_age,
        "removed_by_size": removed_by_size,
        "removed_bytes": max(0, original_bytes - remaining_bytes),
        "remaining_records": len(kept),
        "remaining_bytes": remaining_bytes,
    }


def enforce_retention(
    home: Path | None = None,
    policy: StoragePolicy | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    resolved = home or bridge_home()
    active_policy = policy or load_storage_policy(resolved)
    with file_lock(_ledger_lock(resolved), timeout_sec=LOCK_TIMEOUT_SEC):
        return _enforce_retention_locked(resolved, active_policy, now=now)


def append_receipt_record(
    home: Path,
    payload: dict[str, Any],
    *,
    enforce_policy: bool = True,
) -> LedgerRecord | None:
    """Append one terminal receipt/task dict to the billing ledger (idempotent by ``task_id``)."""
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        return None
    status = str(payload.get("status") or "").strip().lower()
    if status and status not in TERMINAL_STATUS_VALUES:
        return None

    path = usage_ledger_path(home)
    with file_lock(_ledger_lock(home), timeout_sec=LOCK_TIMEOUT_SEC):
        existing, _ = load_ledger(home)
        if any(rec.task_id == task_id for rec in existing):
            return None
        cum_map = _last_cumulative_by_session(existing)
        session_id = str(payload.get("session_id") or "")
        prev_cum = cum_map.get(session_id)
        record, _ = build_ledger_record(payload, previous_cumulative=prev_cum)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(_encode_record(record))
            handle.flush()
        if enforce_policy:
            policy = load_storage_policy(home)
            if policy.auto_rotate:
                current_size = path.stat().st_size if path.is_file() else 0
                if policy.max_bytes is not None and current_size > policy.max_bytes:
                    _enforce_retention_locked(home, policy)
    return record


def record_receipt_to_ledger(home: Path, payload: dict[str, Any]) -> None:
    """Fail-open wrapper called by ``Registry._persist_receipt``."""
    try:
        append_receipt_record(home, payload, enforce_policy=True)
    except Exception:
        log.warning(
            "could not append billing ledger entry for task %s",
            payload.get("task_id"),
            exc_info=True,
        )


def _collect_bridge_candidates(home: Path) -> list[dict[str, Any]]:
    """Collect terminal task metadata from ``receipts/*.json`` and ``state.json`` without reading bodies."""
    by_id: dict[str, dict[str, Any]] = {}

    # 1. Load terminal rows from state.json as fallback
    st_file = state_path(home)
    if st_file.is_file():
        st = read_json(st_file, {})
        if isinstance(st, dict):
            sessions_title: dict[str, str | None] = {}
            for s in st.get("sessions") or []:
                if isinstance(s, dict) and isinstance(s.get("session_id"), str):
                    sessions_title[s["session_id"]] = s.get("title")
            for row in st.get("tasks") or []:
                if not isinstance(row, dict):
                    continue
                tid = row.get("task_id")
                status = str(row.get("status") or "").lower()
                if not isinstance(tid, str) or not tid or status not in TERMINAL_STATUS_VALUES:
                    continue
                sanitized: dict[str, Any] = {
                    "task_id": tid,
                    "session_id": row.get("session_id"),
                    "agent": row.get("agent"),
                    "cwd": row.get("cwd"),
                    "title": sessions_title.get(str(row.get("session_id") or "")),
                    "status": status,
                    "stop_reason": row.get("stop_reason"),
                    "model": row.get("model"),
                    "effort": row.get("effort"),
                    "observed_model": row.get("observed_model"),
                    "observed_effort": row.get("observed_effort"),
                    "observed_permission_mode": row.get("observed_permission_mode"),
                    "files_changed_total": row.get("files_changed_total"),
                    "files_outside_cwd_total": row.get("files_outside_cwd_total"),
                    "usage": row.get("usage") if isinstance(row.get("usage"), dict) else {},
                    "warnings": row.get("warnings") if isinstance(row.get("warnings"), list) else [],
                    "error": row.get("error"),
                    "created_at": row.get("created_at"),
                    "started_at": row.get("started_at"),
                    "finished_at": row.get("finished_at"),
                }
                by_id[tid] = sanitized

    # 2. Receipts override state.json (they carry native_session_id and finalized title/usage)
    receipts_dir = home / "receipts"
    if receipts_dir.is_dir():
        for file in sorted(receipts_dir.glob("*.json")):
            raw = read_json(file, None)
            if not isinstance(raw, dict):
                continue
            tid = raw.get("task_id")
            status = str(raw.get("status") or "").lower()
            if not isinstance(tid, str) or not tid or status not in TERMINAL_STATUS_VALUES:
                continue
            by_id[tid] = {
                "task_id": tid,
                "session_id": raw.get("session_id"),
                "native_session_id": raw.get("native_session_id"),
                "agent": raw.get("agent"),
                "cwd": raw.get("cwd"),
                "title": raw.get("title"),
                "status": status,
                "stop_reason": raw.get("stop_reason"),
                "model": raw.get("model"),
                "effort": raw.get("effort"),
                "observed_model": raw.get("observed_model"),
                "observed_effort": raw.get("observed_effort"),
                "observed_permission_mode": raw.get("observed_permission_mode"),
                "files_changed_total": raw.get("files_changed_total"),
                "files_outside_cwd_total": raw.get("files_outside_cwd_total"),
                "usage": raw.get("usage") if isinstance(raw.get("usage"), dict) else {},
                "warnings": raw.get("warnings") if isinstance(raw.get("warnings"), list) else [],
                "error": raw.get("error"),
                "created_at": raw.get("created_at"),
                "started_at": raw.get("started_at"),
                "finished_at": raw.get("finished_at"),
            }

    candidates = list(by_id.values())
    candidates.sort(
        key=lambda item: (
            str(item.get("created_at") or item.get("started_at") or item.get("finished_at") or ""),
            str(item.get("task_id") or ""),
        )
    )
    return candidates


def backfill_from_bridge_home(home: Path | None = None) -> dict[str, Any]:
    """Import missing historical terminal tasks from ``receipts/`` and ``state.json``."""
    resolved = home or bridge_home()
    candidates = _collect_bridge_candidates(resolved)
    path = usage_ledger_path(resolved)

    with file_lock(_ledger_lock(resolved), timeout_sec=LOCK_TIMEOUT_SEC):
        existing, _ = load_ledger(resolved)
        known_ids = {rec.task_id for rec in existing}
        cum_map = _last_cumulative_by_session(existing)

        to_write: list[bytes] = []
        imported = 0
        for item in candidates:
            tid = str(item.get("task_id") or "")
            if not tid or tid in known_ids:
                continue
            sid = str(item.get("session_id") or "")
            rec, next_cum = build_ledger_record(item, previous_cumulative=cum_map.get(sid))
            if next_cum is not None and sid:
                cum_map[sid] = next_cum
            to_write.append(_encode_record(rec))
            known_ids.add(tid)
            imported += 1

        if to_write:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as handle:
                handle.write(b"".join(to_write))
                handle.flush()

        policy = load_storage_policy(resolved)
        retention = _enforce_retention_locked(resolved, policy) if (to_write and policy.auto_rotate) else None

    return {
        "scanned": len(candidates),
        "imported": imported,
        "skipped_existing": len(candidates) - imported,
        "retention": retention,
    }


def clear_ledger(home: Path | None = None) -> dict[str, Any]:
    """Truncate ``usage_ledger.jsonl`` cleanly without touching pricing/policy/receipts."""
    resolved = home or bridge_home()
    path = usage_ledger_path(resolved)
    with file_lock(_ledger_lock(resolved), timeout_sec=LOCK_TIMEOUT_SEC):
        before_records, _ = load_ledger(resolved)
        before_bytes = path.stat().st_size if path.is_file() else 0
        _atomic_write_bytes(path, b"")
    return {
        "cleared_records": len(before_records),
        "cleared_bytes": before_bytes,
    }


def storage_stats(home: Path | None = None) -> dict[str, Any]:
    """Return disk footprint, record span, and active retention policy."""
    resolved = home or bridge_home()
    bdir = billing_dir(resolved)
    lpath = usage_ledger_path(resolved)
    ppath = pricing_path(resolved)
    spath = storage_policy_path(resolved)

    records, corrupt = load_ledger(resolved)
    timestamps = [ts for rec in records if (ts := record_timestamp(rec)) is not None]
    earliest = min(timestamps).isoformat() if timestamps else None
    latest = max(timestamps).isoformat() if timestamps else None

    ledger_bytes = lpath.stat().st_size if lpath.is_file() else 0
    pricing_bytes = ppath.stat().st_size if ppath.is_file() else 0
    storage_bytes = spath.stat().st_size if spath.is_file() else 0

    receipts_dir = resolved / "receipts"
    receipts_count = len(list(receipts_dir.glob("*.json"))) if receipts_dir.is_dir() else 0

    policy = load_storage_policy(resolved)
    return {
        "billing_dir": str(bdir),
        "ledger_path": str(lpath),
        "pricing_path": str(ppath),
        "storage_policy_path": str(spath),
        "ledger_bytes": ledger_bytes,
        "pricing_bytes": pricing_bytes,
        "storage_policy_bytes": storage_bytes,
        "total_billing_bytes": ledger_bytes + pricing_bytes + storage_bytes,
        "record_count": len(records),
        "corrupt_lines": corrupt,
        "earliest_ts": earliest,
        "latest_ts": latest,
        "receipts_on_disk": receipts_count,
        "policy": policy.model_dump(mode="json"),
    }
