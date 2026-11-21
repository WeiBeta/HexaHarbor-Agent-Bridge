"""Independent billing ledger, pricing store, and disk retention.

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
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agent_bridge.billing import (
    LedgerRecord,
    NormalizedTokens,
    _extract_raw_snapshot,
    _parse_iso,
    build_ledger_record,
    normalize_usage,
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
EARLIEST_TIMESTAMP = datetime.min.replace(tzinfo=UTC)

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


def _turn_order(record: LedgerRecord) -> tuple[datetime, str]:
    start = _parse_iso(record.created_at) or _parse_iso(record.started_at) or _parse_iso(record.finished_at)
    return start or EARLIEST_TIMESTAMP, record.task_id


def _reconcile_cumulative_usage(existing: list[LedgerRecord], additions: list[LedgerRecord]) -> dict[str, LedgerRecord]:
    """Apply measured turns in session order and update the known deltas they change."""
    new_ids = {record.task_id for record in additions}
    affected = {
        record.session_id
        for record in additions
        if record.agent.strip().lower() == "antigravity" and record.session_id and record.tokens.measured
    }
    sessions: dict[str, list[LedgerRecord]] = defaultdict(list)
    for record in chain(existing, additions):
        if record.session_id in affected and record.agent.strip().lower() == "antigravity":
            sessions[record.session_id].append(record)

    corrections: dict[str, LedgerRecord] = {}
    for records in sessions.values():
        previous: NormalizedTokens | None = None
        previous_on_disk: NormalizedTokens | None = None
        previous_is_new = False
        for record in sorted(records, key=_turn_order):
            snapshot = _extract_raw_snapshot(record.agent, record.usage_raw)
            if not snapshot.measured:
                continue
            is_new = record.task_id in new_ids
            if is_new or previous_is_new:
                _, updated, _ = normalize_usage(record.agent, record.usage_raw, previous_cumulative=previous)
                if is_new:
                    record.tokens = updated
                elif record.tokens != updated:
                    _, old, _ = normalize_usage(record.agent, record.usage_raw, previous_cumulative=previous_on_disk)
                    # Retention may have removed the baseline for an already measured row.
                    # Preserve that delta when the surviving rows cannot account for it.
                    if record.tokens == old:
                        corrections[record.task_id] = record.model_copy(update={"tokens": updated})
            previous = snapshot
            previous_is_new = is_new
            if not is_new:
                previous_on_disk = snapshot
    return corrections


def _encode_record(record: LedgerRecord) -> bytes:
    line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return (line + "\n").encode("utf-8")


def _append_records_locked(path: Path, existing: list[LedgerRecord], additions: list[LedgerRecord]) -> None:
    corrections = _reconcile_cumulative_usage(existing, additions)
    appended = b"".join(_encode_record(record) for record in additions)
    path.parent.mkdir(parents=True, exist_ok=True)
    if corrections:
        lines = _read_raw_lines(path)
        for index, line in enumerate(lines):
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            task_id = payload.get("task_id") if isinstance(payload, dict) else None
            if isinstance(task_id, str) and task_id in corrections:
                lines[index] = _encode_record(corrections[task_id])
        # Publish the late turn and its changed successor deltas together.
        _atomic_write_bytes(path, b"".join(lines) + appended)
        return

    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell():
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                # Readers already ignore an interrupted append's unfinished line.
                # Drop that suffix before it can consume the next committed record.
                handle.seek(0)
                handle.truncate(handle.read().rfind(b"\n") + 1)
        handle.write(appended)
        handle.flush()


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

    cutoff = current_time - timedelta(days=policy.max_age_days) if policy.max_age_days is not None else None
    kept: list[tuple[bytes, datetime | None]] = []
    for raw_line in lines:
        try:
            ts = record_timestamp(LedgerRecord.model_validate(json.loads(raw_line.decode("utf-8"))))
        except ValueError:
            ts = None
        if cutoff is None or ts is None or ts >= cutoff:
            kept.append((raw_line, ts))

    removed_by_age = len(lines) - len(kept)
    dropped_for_size: set[int] = set()
    remaining_bytes = sum(len(line) for line, _ in kept)
    if policy.max_bytes is not None and remaining_bytes > policy.max_bytes and len(kept) > 1:
        target = max(1, int(policy.max_bytes * RETENTION_TARGET_RATIO))
        # Backfill can append an old turn after newer rows. Evict by record
        # time and keep file order for the surviving rows and equal times.
        by_age = sorted(range(len(kept)), key=lambda index: kept[index][1] or EARLIEST_TIMESTAMP)
        for index in by_age[:-1]:
            if remaining_bytes <= target:
                break
            dropped_for_size.add(index)
            remaining_bytes -= len(kept[index][0])

    removed_by_size = len(dropped_for_size)
    changed = bool(removed_by_age or removed_by_size)
    if changed:
        _atomic_write_bytes(
            path, b"".join(line for index, (line, _) in enumerate(kept) if index not in dropped_for_size)
        )

    return {
        "changed": changed,
        "removed_by_age": removed_by_age,
        "removed_by_size": removed_by_size,
        "removed_bytes": max(0, original_bytes - remaining_bytes),
        "remaining_records": len(kept) - removed_by_size,
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


def _retention_due(path: Path, policy: StoragePolicy, records: Iterable[LedgerRecord]) -> bool:
    if not policy.auto_rotate:
        return False
    if policy.max_bytes is not None and path.stat().st_size > policy.max_bytes:
        return True
    if policy.max_age_days is None:
        return False
    cutoff = datetime.now(UTC) - timedelta(days=policy.max_age_days)
    return any((ts := record_timestamp(record)) is not None and ts < cutoff for record in records)


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
        record, _ = build_ledger_record(payload)
        _append_records_locked(path, existing, [record])
        if enforce_policy:
            policy = load_storage_policy(home)
            if _retention_due(path, policy, chain(existing, [record])):
                _enforce_retention_locked(home, policy)
    return record


def record_receipt_to_ledger(home: Path, payload: dict[str, Any]) -> None:
    """Fail-open wrapper used by the Registry's background ledger writer."""
    try:
        append_receipt_record(home, payload, enforce_policy=True)
    except Exception:
        log.warning(
            "could not append billing ledger entry for task %s",
            payload.get("task_id"),
            exc_info=True,
        )


def _collect_bridge_candidates(home: Path) -> list[dict[str, Any]]:
    """Select terminal task metadata while parsing receipt and state JSON."""
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

        additions: list[LedgerRecord] = []
        for item in candidates:
            tid = str(item.get("task_id") or "")
            if not tid or tid in known_ids:
                continue
            record, _ = build_ledger_record(item)
            additions.append(record)
            known_ids.add(tid)

        if additions:
            _append_records_locked(path, existing, additions)

        policy = load_storage_policy(resolved)
        retention = _enforce_retention_locked(resolved, policy) if (additions and policy.auto_rotate) else None

    return {
        "scanned": len(candidates),
        "imported": len(additions),
        "skipped_existing": len(candidates) - len(additions),
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
