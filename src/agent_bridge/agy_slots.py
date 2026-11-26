"""Daily AGY empty-project rotation, Bridge admission guards and cleanup receipts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AsyncExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from agent_bridge import agy_slot_worker
from agent_bridge.config import (
    AgentConfig,
    AgySlotConfig,
    AgySlotSpec,
    EnvConfig,
    load_config,
    resolve_rotation_timezone,
)
from agent_bridge.models import ProcState, Session, Task, TaskStatus, iso
from agent_bridge.paths import agy_slot_receipts_path, agy_slots_dir, state_path
from agent_bridge.persist import FileLockTimeout, atomic_write_json, file_lock, read_json_strict
from agent_bridge.processes import resolve_command
from agent_bridge.worker_env import build_worker_env, enforce_env_deny

log = logging.getLogger(__name__)

CLEANUP_LOCK_TIMEOUT_SEC = 5.0
WORKER_COMMAND = "python cleanup_worker.py request.json result.json"


class SlotConfigError(ValueError):
    """Raised when AGY slot configuration or request parameters are invalid (HTTP 400)."""


class SlotCleanupConflictError(RuntimeError):
    """Raised when Drain Guard or concurrency lock blocks a live cleanup (HTTP 409)."""

    def __init__(self, message: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.payload = payload


@dataclass(frozen=True)
class SlotTiming:
    spec: AgySlotSpec
    is_active: bool
    days_since_active: int
    last_active_end: datetime | None
    cooling_elapsed_hours: float
    grace_remaining_hours: float
    grace_until: datetime | None = None


@dataclass(frozen=True)
class CleanerRequest:
    slot: str
    project_id: str
    allowed_project_ids: tuple[str, ...]
    cutoff_utc: datetime
    dry_run: bool
    operation_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "slot": self.slot,
            "project_id": self.project_id,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class CleanerExecutionResult:
    ok: bool
    checked: int = 0
    deleted: int = 0
    would_delete: int = 0
    deleted_ids: tuple[str, ...] = ()
    would_delete_ids: tuple[str, ...] = ()
    skipped_running: int = 0
    skipped_recent: int = 0
    errors: tuple[str, ...] = ()
    eligible_ids: tuple[str, ...] = ()
    skipped_unverified: int = 0
    outcome_unknown: bool = False
    worker_receipt_valid: bool = False


class CleanerRunner(Protocol):
    def __call__(
        self,
        request: CleanerRequest,
        *,
        agent_cfg: AgentConfig,
        home: Path,
        env_config: EnvConfig | None = None,
    ) -> CleanerExecutionResult: ...


def _ensure_aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def select_active_slot(slot_cfg: AgySlotConfig, *, now: datetime | None = None) -> AgySlotSpec | None:
    """Return today's active AGY slot according to ``rotation_tz``, or None when disabled."""
    if not slot_cfg.enabled or not slot_cfg.slots:
        return None
    tz = None if slot_cfg.rotation_tz.lower() == "local" else resolve_rotation_timezone(slot_cfg.rotation_tz)
    local_now = _ensure_aware(now).astimezone(tz)
    idx = local_now.date().toordinal() % len(slot_cfg.slots)
    return slot_cfg.slots[idx]


def compute_slot_schedule(
    slot_cfg: AgySlotConfig,
    *,
    now: datetime | None = None,
) -> tuple[datetime, list[SlotTiming]]:
    """Compute local timestamp and per-slot rotation/cooling windows."""
    tz = None if slot_cfg.rotation_tz.lower() == "local" else resolve_rotation_timezone(slot_cfg.rotation_tz)
    local_now = _ensure_aware(now).astimezone(tz)
    if not slot_cfg.enabled or not slot_cfg.slots:
        return local_now, []

    slot_count = len(slot_cfg.slots)
    local_day = local_now.date()
    active_idx = local_day.toordinal() % slot_count
    grace_hours = float(slot_cfg.grace_hours)
    timings: list[SlotTiming] = []
    for idx, spec in enumerate(slot_cfg.slots):
        if idx == active_idx:
            timings.append(
                SlotTiming(
                    spec=spec,
                    is_active=True,
                    days_since_active=0,
                    last_active_end=None,
                    cooling_elapsed_hours=0.0,
                    grace_remaining_hours=grace_hours,
                )
            )
            continue
        days_ago = (active_idx - idx) % slot_count
        last_active_day = local_day - timedelta(days=days_ago)
        end_day = last_active_day + timedelta(days=1)
        last_active_end = datetime(end_day.year, end_day.month, end_day.day, 0, 0, 0, tzinfo=tz)
        if tz is None:
            # Resolve the OS offset for that midnight, including historical DST.
            last_active_end = last_active_end.astimezone()
        grace_until = last_active_end.astimezone(UTC) + timedelta(hours=slot_cfg.grace_hours)
        elapsed_hours = max(
            0.0,
            (local_now.astimezone(UTC) - last_active_end.astimezone(UTC)).total_seconds() / 3600.0,
        )
        remaining_hours = max(0.0, (grace_until - local_now.astimezone(UTC)).total_seconds() / 3600)
        timings.append(
            SlotTiming(
                spec=spec,
                is_active=False,
                days_since_active=days_ago,
                last_active_end=last_active_end,
                cooling_elapsed_hours=round(elapsed_hours, 3),
                grace_remaining_hours=remaining_hours,
                grace_until=grace_until,
            )
        )
    return local_now, timings


class SlotGuardError(RuntimeError):
    """A prerequisite for admission or cleanup could not be established."""


def validate_slot_state(raw: Any) -> tuple[list[Session], list[Task]]:
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), list) for k in ("sessions", "tasks")):
        raise SlotGuardError("Bridge state is missing or incomplete")
    try:
        sessions = [Session.model_validate(row) for row in raw["sessions"]]
        tasks = [Task.model_validate(row) for row in raw["tasks"]]
    except ValueError:
        raise SlotGuardError("Bridge session or task record is invalid") from None
    if "agy_slot_epoch" in raw and not agy_slot_worker.is_canonical_uuid(raw["agy_slot_epoch"]):
        raise SlotGuardError("Bridge state generation is invalid")
    by_id = {s.session_id: s for s in sessions}
    if len(by_id) != len(sessions) or len({t.task_id for t in tasks}) != len(tasks):
        raise SlotGuardError("Bridge state contains duplicate identities")
    for session in sessions:
        has_binding = session.agy_slot is not None or session.agy_project_id is not None
        if "proc_state" not in session.model_fields_set or (
            has_binding
            and (
                not agy_slot_worker.is_canonical_uuid(session.agy_project_id)
                or not session.agy_slot
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", session.agy_slot)
            )
        ):
            raise SlotGuardError("Bridge session state or slot binding is incomplete")
    for task in tasks:
        if "status" not in task.model_fields_set:
            raise SlotGuardError("Bridge task status is missing")
        if task.status in {TaskStatus.queued, TaskStatus.running}:
            task_session = by_id.get(task.session_id)
            if task_session is None or task.agent != task_session.agent:
                raise SlotGuardError("pending Bridge task cannot be attributed to a session")
    return sessions, tasks


def verify_unused_slot_home(home: Path) -> None:
    """Before a Registry initializes missing state, look for evidence of previous use."""
    try:
        if any(path.name.startswith("state.json.corrupt-") for path in home.iterdir()):
            raise SlotGuardError("Bridge state was previously quarantined")
        for name in ("results", "receipts", "transcripts", "agy_slots"):
            try:
                if next((home / name).iterdir(), None) is not None:
                    raise SlotGuardError("Bridge state is missing from a previously used home")
            except FileNotFoundError:
                continue
    except OSError:
        raise SlotGuardError("cannot establish whether this Bridge home is unused") from None


def bound_slot(session: Session, config: AgySlotConfig) -> AgySlotSpec | None:
    if session.agy_slot is None and session.agy_project_id is None:
        return None
    try:
        spec = AgySlotSpec(name=session.agy_slot or "", project_id=session.agy_project_id or "").with_canonical_id()
    except ValueError:
        raise SlotGuardError("AGY session has an invalid slot binding") from None
    if config.enabled and spec not in config.slots:
        raise SlotGuardError("AGY session binding is outside the configured slots")
    return spec


def _read_slot_state(home: Path) -> Any:
    try:
        raw = read_json_strict(state_path(home), None)
    except (OSError, ValueError):
        raise SlotGuardError("Bridge state is unreadable") from None
    return raw


def find_blocking_sessions(
    slot: AgySlotSpec,
    sessions: Sequence[Session],
    tasks: Sequence[Task],
    agy_agents: frozenset[str] = frozenset({"antigravity"}),
) -> list[dict[str, Any]]:
    """Return active or unclosed Bridge sessions bound to ``slot``."""
    pending_session_ids = {task.session_id for task in tasks if task.status in {TaskStatus.queued, TaskStatus.running}}
    blocked: list[dict[str, Any]] = []
    for session in sessions:
        bound = session.agy_slot == slot.name or session.agy_project_id == slot.project_id
        has_pending_task = session.session_id in pending_session_ids
        if not bound and not (has_pending_task and session.agent in agy_agents and not session.agy_project_id):
            continue
        if has_pending_task:
            reason = "active_task" if bound else "unattributed_active_task"
        elif session.proc_state in {ProcState.spawning, ProcState.ready, ProcState.busy}:
            reason = "active_session"
        elif session.proc_state == ProcState.idle_unloaded:
            reason = "unclosed_session"
        else:
            continue
        blocked.append(
            {
                "session_id": session.session_id,
                "native_session_id": session.native_session_id,
                "proc_state": session.proc_state.value,
                "last_active_at": session.last_active_at,
                "reason": reason,
            }
        )
    blocked.sort(key=lambda item: str(item["session_id"]))
    return blocked


def inspect_slot_project_file(
    project_id: str, projects_dir: Path | None
) -> tuple[bool | None, bool | None, str | None]:
    if projects_dir is None:
        return False, None, "project registration directory is unknown"
    if not agy_slot_worker.is_canonical_uuid(project_id):
        return False, None, "invalid project UUID"
    path = projects_dir / f"{project_id}.json"
    try:
        if path.resolve(strict=True) != projects_dir.resolve(strict=True) / path.name or path.stat().st_nlink != 1:
            return True, None, "project registration is not a direct, unique file"
    except FileNotFoundError:
        return False, None, "project registration is missing"
    except (OSError, RuntimeError):
        return None, None, "project registration location is unreadable"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, None, "project registration is unreadable"
    if not isinstance(data, dict) or data.get("id") != project_id:
        return True, None, "project registration identity differs from configured UUID"
    resources = data.get("projectResources")
    if resources == {} or resources == {"resources": []}:
        return True, True, None
    if isinstance(resources, dict) and isinstance(resources.get("resources"), list) and resources["resources"]:
        return True, False, "project contains resources"
    return True, None, "project has no recognized explicit empty resource declaration"


def require_empty_project(spec: AgySlotSpec, projects_dir: Path) -> None:
    _, _, reason = inspect_slot_project_file(spec.project_id, projects_dir)
    if reason:
        raise SlotGuardError(reason)


def _owner_path(spec: AgySlotSpec) -> Path:
    # AGY registrations are per OS account. Custom Bridge homes must agree on their owner.
    if not agy_slot_worker.is_canonical_uuid(spec.project_id):
        raise SlotGuardError("invalid project UUID")
    return Path.home() / ".agent-bridge" / "agy_slot_owners" / f"{spec.project_id}.json"


def _read_owner(spec: AgySlotSpec, home: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    try:
        absent = object()
        owner = read_json_strict(_owner_path(spec), absent)
    except (OSError, ValueError):
        raise SlotGuardError("slot ownership record is unreadable") from None
    if owner is absent:
        return None
    if (
        not isinstance(owner, dict)
        or (set(owner) - {"home", "epoch", "operation_id"})
        or owner.get("home") != os.path.normcase(str(home.resolve()))
    ):
        raise SlotGuardError("slot ownership is invalid or belongs to another Bridge home")
    epoch = state.get("agy_slot_epoch")
    if not isinstance(epoch, str) or owner.get("epoch") != epoch:
        raise SlotGuardError("Bridge state generation differs from the slot ownership record")
    if "operation_id" in owner and not agy_slot_worker.is_canonical_uuid(owner["operation_id"]):
        raise SlotGuardError("slot operation identity is invalid")
    return owner


def _display_receipt(home: Path, row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if "operation_id" in row:
        fields = (
            "operation_id",
            "slot",
            "project_id",
            "slot_status",
            "dry_run",
            "ok",
            "outcome",
            "executed_at",
            "counts_confirmed",
            "receipt_saved",
            "eligible_ids",
            "deleted_ids",
            "would_delete_ids",
            "errors",
            *_COUNT_FIELDS,
        )
        public = {key: row[key] for key in fields}
        public["blocked_sessions"] = [
            {k: item.get(k) for k in ("session_id", "native_session_id", "proc_state", "last_active_at", "reason")}
            for item in row["blocked_sessions"]
        ]
        if row["outcome"] in {"unconfirmed", "unknown"} and not row["counts_confirmed"]:
            # A worker may finish after its caller timed out or the dashboard stopped.
            # Only display its validated evidence. The CLI process may still be active.
            path = agy_slots_dir(home) / "runs" / row["operation_id"] / "result.json"
            result = _read_worker_receipt(path, row, "no terminal worker receipt")
            if result.worker_receipt_valid:
                return {
                    **public,
                    "checked": result.checked,
                    "deleted": result.deleted,
                    "would_delete": result.would_delete,
                    "deleted_ids": list(result.deleted_ids),
                    "would_delete_ids": list(result.would_delete_ids),
                    "eligible_ids": list(result.eligible_ids),
                    "skipped_unverified": result.skipped_unverified,
                    "skipped_running": result.skipped_running,
                    "skipped_recent": result.skipped_recent,
                    "errors": list(dict.fromkeys([*row["errors"], *result.errors])),
                    "counts_confirmed": True,
                    "late_worker_receipt": True,
                }
        return public
    # The original unbound format could contain CLI output in errors. Preserve the
    # on-disk record and expose only its target, mode and provenance after upgrading.
    return {
        "slot": row["slot"],
        "project_id": row["project_id"],
        "dry_run": row["dry_run"],
        "outcome": "historical_unverified",
        "ok": False,
        "checked": None,
        "deleted": None,
        "would_delete": None,
        "errors": ["historical_receipt_unverified"],
    }


_COUNT_FIELDS = ("checked", "deleted", "would_delete", "skipped_running", "skipped_recent", "skipped_unverified")


def _safe_runner_error(value: str) -> str:
    if (
        value in agy_slot_worker.WORKER_ERRORS
        or value
        in {
            "invalid cleanup target or mode",
            "failed to launch agy slot cleaner",
            "could not prepare cleanup workspace",
            "cleanup operation already has a workspace",
            "agy executable not found on PATH or standard install locations",
            "invalid or mismatched cleaner receipt",
            "cleaner timed out; execution unconfirmed",
            "cleaner completion unconfirmed",
            "cleaner_failed",
            "project registration is missing",
            "project registration is unreadable",
            "project registration location is unreadable",
            "project registration is not a direct, unique file",
            "project registration identity differs from configured UUID",
            "project contains resources",
            "project has no recognized explicit empty resource declaration",
        }
        or re.fullmatch(r"no valid result\.json \(agy exit -?\d+, stderr present=(?:True|False)\)", value)
    ):
        return value
    return "cleaner_failed"


def _valid_stored_receipt(name: str, row: Any) -> bool:
    if (
        not isinstance(row, dict)
        or row.get("slot") != name
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name)
        or type(row.get("dry_run")) is not bool
        or type(row.get("ok")) is not bool
        or not agy_slot_worker.is_canonical_uuid(row.get("project_id"))
    ):
        return False
    if "operation_id" not in row:
        return "outcome" not in row and "counts_confirmed" not in row  # legacy unbound format
    if (
        not agy_slot_worker.is_canonical_uuid(row["operation_id"])
        or type(row.get("counts_confirmed")) is not bool
        or row.get("outcome") not in ("unconfirmed", "unknown", "failed", "partial", "completed")
        or row.get("slot_status") not in ("active", "cooling", "blocked", "unknown", "releasable")
        or agy_slot_worker.parse_rfc3339(row.get("executed_at")) is None
        or not isinstance(row.get("blocked_sessions"), list)
        or not all(isinstance(item, dict) for item in row["blocked_sessions"])
        or (row["outcome"] == "completed") != row["ok"]
        or row.get("receipt_saved") is not True
        or not isinstance(row.get("errors"), list)
        or not all(isinstance(e, str) and _safe_runner_error(e) == e for e in row["errors"])
    ):
        return False
    for key in ("eligible_ids", "deleted_ids", "would_delete_ids"):
        if not isinstance(row.get(key), list) or not all(
            isinstance(cid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cid) for cid in row[key]
        ):
            return False
    if row["counts_confirmed"]:
        return all(type(row.get(k)) is int and row[k] >= 0 for k in _COUNT_FIELDS)
    return row["outcome"] in {"unconfirmed", "unknown"} and all(k in row and row[k] is None for k in _COUNT_FIELDS)


def _load_cleanup_receipts(home: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {"last_cleanup": None, "by_slot": {}}
    try:
        data = read_json_strict(agy_slot_receipts_path(home), empty)
    except (OSError, ValueError):
        raise SlotGuardError("cleanup receipt store is unreadable") from None
    if (
        not isinstance(data, dict)
        or "last_cleanup" not in data
        or not isinstance(data.get("by_slot"), dict)
        or not all(_valid_stored_receipt(k, v) for k, v in data["by_slot"].items())
        or (data["by_slot"] and data["last_cleanup"] not in data["by_slot"].values())
        or (not data["by_slot"] and data["last_cleanup"] is not None)
    ):
        raise SlotGuardError("cleanup receipt store is invalid")
    return data


def _save_cleanup_receipt(home: Path, slot_name: str, receipt: dict[str, Any]) -> None:
    path = agy_slot_receipts_path(home)
    with file_lock(path.with_suffix(".lock"), timeout_sec=CLEANUP_LOCK_TIMEOUT_SEC):
        store = _load_cleanup_receipts(home)
        store["last_cleanup"] = receipt
        store["by_slot"][slot_name] = receipt
        atomic_write_json(path, store)


def _unconfirmed_operation(receipts: dict[str, Any], project_id: str) -> str | None:
    for row in receipts["by_slot"].values():
        if row["project_id"] == project_id and row.get("outcome") in {"unconfirmed", "unknown"}:
            return row["operation_id"]
    return None


async def admit_slot_turn(admissions: AsyncExitStack, home: Path, spec: AgySlotSpec, projects_dir: Path) -> None:
    # Queuing concurrent turns is legitimate. Retry briefly on a busy admission lock
    # without blocking this Registry's state flusher. An admitted cleanup wins.
    deadline = asyncio.get_running_loop().time() + CLEANUP_LOCK_TIMEOUT_SEC
    while True:
        try:
            admissions.enter_context(slot_admission(home, spec, projects_dir))
            return
        except FileLockTimeout:
            if _unconfirmed_operation(_load_cleanup_receipts(home), spec.project_id):
                raise SlotGuardError("AGY slot guard is held by a cleanup") from None
            if asyncio.get_running_loop().time() >= deadline:
                raise SlotGuardError("AGY slot guard admission timed out") from None
            await asyncio.sleep(0.05)


@contextmanager
def slot_admission(
    home: Path, spec: AgySlotSpec, projects_dir: Path, *, claim_on_entry: bool = True
) -> Iterator[dict[str, Any]]:
    """Serialize dispatch admission and cleanup for this registration, across Bridge homes."""
    path = _owner_path(spec)
    with file_lock(path.with_suffix(".lock"), timeout_sec=0):
        require_empty_project(spec, projects_dir)
        receipts = _load_cleanup_receipts(home)
        if _unconfirmed_operation(receipts, spec.project_id):
            raise SlotGuardError("previous cleanup has no confirmed end; inspect its receipt before reuse")
        state_file = state_path(home)
        with file_lock(
            state_file.with_name("state.json.lock"), timeout_sec=0 if claim_on_entry else CLEANUP_LOCK_TIMEOUT_SEC
        ):
            state = _read_slot_state(home)
            validate_slot_state(state)
            owner = _read_owner(spec, home, state)
            if owner is None:
                if "agy_slot_epoch" not in state:
                    state["agy_slot_epoch"] = str(uuid.uuid4())
                    atomic_write_json(state_file, state)
                owner = {"home": os.path.normcase(str(home.resolve())), "epoch": state["agy_slot_epoch"]}
                if claim_on_entry:
                    atomic_write_json(path, owner)
        if owner.get("operation_id"):
            raise SlotGuardError("previous cleanup has no confirmed end; inspect its receipt before reuse")
        yield owner


def _resolve_agy_agent(
    home: Path, agent_cfg: AgentConfig | None = None
) -> tuple[AgentConfig | None, EnvConfig, frozenset[str]]:
    if agent_cfg is not None:
        return agent_cfg, EnvConfig(), frozenset({agent_cfg.name, "antigravity"})
    cfg = load_config(home)
    return cfg.agents.get("antigravity"), cfg.env, frozenset(k for k, a in cfg.agents.items() if a.protocol == "agy")


def _probe_cleaner_availability(
    agent_cfg: AgentConfig | None,
    *,
    discovered_commands_fn: Callable[[], list[list[str]]] | None = None,
) -> tuple[bool, str | None]:
    if agent_cfg is None:
        return False, "antigravity agent is not configured"
    slot_cfg = agent_cfg.agy_slots
    if slot_cfg.cleaner_mode == "disabled":
        return False, "cleaner_mode is disabled"
    if not slot_cfg.enabled:
        return False, "agy_slots is disabled"
    try:
        fallbacks = list(agent_cfg.fallback_commands)
        if discovered_commands_fn is not None:
            fallbacks.extend(discovered_commands_fn())
        else:
            from agent_bridge.adapters.antigravity import discovered_agy_commands

            fallbacks.extend(discovered_agy_commands())
        resolve_command(agent_cfg.command, fallbacks)
        return True, None
    except FileNotFoundError:
        return False, "agy executable not found on PATH or standard install locations"


def describe_slots(
    home: Path,
    *,
    agent_cfg: AgentConfig | None = None,
    now: datetime | None = None,
    projects_dir: Path | None = None,
    cleaner_available: bool | None = None,
    cleaner_reason: str | None = None,
) -> dict[str, Any]:
    """Build the ``GET /api/agy/slots`` status payload."""
    resolved_agent, _env_cfg, agy_agents = _resolve_agy_agent(home, agent_cfg)
    slot_cfg = resolved_agent.agy_slots if resolved_agent is not None else AgySlotConfig()
    local_now, timings = compute_slot_schedule(slot_cfg, now=now)
    sessions: list[Session] = []
    tasks: list[Task] = []
    state: dict[str, Any] = {}
    state_error = receipt_error = None
    receipts: dict[str, Any] = {"last_cleanup": None, "by_slot": {}}
    if slot_cfg.enabled:
        try:
            state = _read_slot_state(home)
            sessions, tasks = validate_slot_state(state)
        except SlotGuardError as exc:
            state_error = str(exc)
        try:
            receipts = _load_cleanup_receipts(home)
        except SlotGuardError as exc:
            receipt_error = str(exc)

    avail: bool
    reason: str | None
    if not slot_cfg.enabled:
        avail, reason = False, "agy_slots is disabled"
    elif slot_cfg.cleaner_mode == "disabled":
        avail, reason = False, "cleaner_mode is disabled"
    elif cleaner_available is None:
        avail, reason = _probe_cleaner_availability(resolved_agent)
    else:
        avail, reason = cleaner_available, cleaner_reason

    active_slot_name: str | None = None
    active_project_id: str | None = None
    slot_rows: list[dict[str, Any]] = []
    for timing in timings:
        spec = timing.spec
        blocked = find_blocking_sessions(spec, sessions, tasks, agy_agents)
        if timing.is_active:
            status = "active"
            active_slot_name = spec.name
            active_project_id = spec.project_id
        elif timing.grace_until is not None and local_now.astimezone(UTC) < timing.grace_until:
            status = "cooling"
        elif blocked:
            status = "blocked"
        else:
            status = "releasable"

        exists, is_empty, project_error = inspect_slot_project_file(spec.project_id, projects_dir)
        guard_error = project_error or state_error or receipt_error
        operation_id = None
        if not guard_error:
            try:
                owner = _read_owner(spec, home, state)
                operation_id = owner.get("operation_id") if owner else None
                if not operation_id:
                    operation_id = _unconfirmed_operation(receipts, spec.project_id)
                if operation_id:
                    guard_error = "cleanup completion is unconfirmed"
            except SlotGuardError as exc:
                guard_error = str(exc)
        if guard_error:
            status = "unknown"
        slot_rows.append(
            {
                "name": spec.name,
                "project_id": spec.project_id,
                "status": status,
                "is_active": timing.is_active,
                "days_since_active": timing.days_since_active,
                "last_active_end": timing.last_active_end.isoformat() if timing.last_active_end else None,
                "cooling_elapsed_hours": timing.cooling_elapsed_hours,
                "grace_remaining_hours": round(timing.grace_remaining_hours, 3),
                "bound_sessions_count": None if state_error else len(blocked),
                "guard_error": guard_error,
                "unconfirmed_operation_id": operation_id,
                "blocked_sessions": blocked,
                "project_file_exists": exists,
                "project_is_empty": is_empty,
                "last_cleanup": _display_receipt(home, receipts["by_slot"].get(spec.name)),
            }
        )

    return {
        "ok": True,
        "enabled": slot_cfg.enabled,
        "rotation_tz": slot_cfg.rotation_tz,
        "grace_hours": slot_cfg.grace_hours,
        "now": local_now.isoformat(),
        "active_slot": active_slot_name,
        "active_project_id": active_project_id,
        "cleaner": {
            "conditional_delete_available": False,
            "live_block_reason": "conditional_delete_unavailable",
            "mode": slot_cfg.cleaner_mode,
            "model": slot_cfg.cleaner_model,
            "available": avail,
            "reason": reason,
        },
        "state_error": state_error,
        "receipt_error": receipt_error,
        "slots": slot_rows,
        "last_cleanup": _display_receipt(home, receipts["last_cleanup"]),
    }


def build_tier3_settings(command_line: str = WORKER_COMMAND) -> dict[str, Any]:
    """Minimal one-command ``--settings`` allowlist for Tier 3 headless cleanup."""
    return {
        "enableTerminalSandbox": False,
        "allowNonWorkspaceAccess": False,
        "permissions": {
            "allow": [f"command({command_line})"],
        },
    }


def build_tier3_cleaner_prompt(
    *,
    command_line: str = WORKER_COMMAND,
    max_retries: int = 2,
) -> str:
    """Deterministic prompt instructing headless ``agy -p`` to run only the allowlisted worker."""
    retry_clause = (
        f"If the command exits with a non-zero exit code and no output due to shell cold start, "
        f"retry the exact same command up to {max_retries} time(s). "
        if max_retries > 0
        else ""
    )
    return (
        "Run the exact command below verbatim using run_command in the default working directory. "
        f"{retry_clause}"
        "Do not modify the command, do not run any other command, and do not read or edit any files. "
        "Once it outputs AGY_SLOT_CLEANUP_OK or AGY_SLOT_CLEANUP_ERR, reply DONE.\n\n"
        f"{command_line}"
    )


def build_tier3_cleaner_command(
    *,
    agy_cmd: Sequence[str],
    slot_cfg: AgySlotConfig,
    target_project_id: str,
    runner_dir: Path,
    settings_path: Path,
) -> list[str]:
    """Build the headless ``agy -p`` invocation under Scheme A (``--project <target_slot>``)."""
    return [
        *agy_cmd,
        "--model",
        slot_cfg.cleaner_model,
        "--disable-slash-commands",
        "--settings",
        str(settings_path),
        "--add-dir",
        str(runner_dir),
        "--project",
        target_project_id,
        "-p",
        "",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--print-timeout",
        f"{slot_cfg.cleaner_timeout_sec}s",
    ]


def parse_worker_receipt(raw: Any, expected: dict[str, Any]) -> CleanerExecutionResult:
    invalid = CleanerExecutionResult(False, errors=("invalid or mismatched cleaner receipt",), outcome_unknown=True)
    if (
        not isinstance(raw, dict)
        or any(raw.get(k) != expected[k] for k in ("operation_id", "slot", "project_id"))
        or raw.get("dry_run") is not expected["dry_run"]
        or type(raw.get("ok")) is not bool
    ):
        return invalid
    counts = _COUNT_FIELDS
    if not all(type(raw.get(k)) is int and raw[k] >= 0 for k in counts):
        return invalid
    # No conditional delete is available on this worker's wire protocol.
    for count in ("deleted", "would_delete"):
        if raw[count] != 0 or raw.get(count + "_ids") != []:
            return invalid
    eligible_ids = raw.get("eligible_ids")
    if (
        not isinstance(eligible_ids, list)
        or len(eligible_ids) > raw["skipped_unverified"]
        or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", x) for x in eligible_ids)
        or len(set(eligible_ids)) != len(eligible_ids)
    ):
        return invalid
    errors = raw.get("errors")
    if (
        not isinstance(errors, list)
        or not all(isinstance(e, str) and e in agy_slot_worker.WORKER_ERRORS for e in errors)
        or raw["ok"] == bool(errors)
        or sum(raw[k] for k in counts[1:]) != raw["checked"]
    ):
        return invalid
    return CleanerExecutionResult(
        ok=raw["ok"],
        **{k: raw[k] for k in counts},
        errors=tuple(errors),
        eligible_ids=tuple(eligible_ids),
        worker_receipt_valid=True,
    )


def _read_worker_receipt(path: Path, expected: dict[str, Any], missing_reason: str) -> CleanerExecutionResult:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CleanerExecutionResult(False, errors=(missing_reason,), outcome_unknown=True)
    return parse_worker_receipt(raw, expected)


def execute_tier3_cleanup(
    request: CleanerRequest,
    *,
    agent_cfg: AgentConfig,
    home: Path,
    env_config: EnvConfig | None = None,
) -> CleanerExecutionResult:
    """Run the list-only slot worker under the selected project and its one-command allowlist."""
    from agent_bridge.adapters.antigravity import agy_projects_dir, discovered_agy_commands, stream_json_prompt

    slots = agent_cfg.agy_slots
    spec = next((s for s in slots.slots if (s.name, s.project_id) == (request.slot, request.project_id)), None)
    if (
        not slots.enabled
        or slots.cleaner_mode != "cli_run_command"
        or spec is None
        or type(request.dry_run) is not bool
        or not agy_slot_worker.is_canonical_uuid(request.operation_id)
        or set(request.allowed_project_ids) != {s.project_id for s in slots.slots}
    ):
        return CleanerExecutionResult(False, errors=("invalid cleanup target or mode",))
    if not request.dry_run:
        return CleanerExecutionResult(False, errors=("conditional_delete_unavailable",))
    try:
        require_empty_project(spec, agy_projects_dir())
    except SlotGuardError as exc:
        return CleanerExecutionResult(False, errors=(str(exc),))

    slot_cfg = agent_cfg.agy_slots
    fallbacks = [*agent_cfg.fallback_commands, *discovered_agy_commands()]
    try:
        agy_cmd = resolve_command(agent_cfg.command, fallbacks)
    except FileNotFoundError:
        return CleanerExecutionResult(
            ok=False,
            errors=("agy executable not found on PATH or standard install locations",),
        )

    worker_source = Path(agy_slot_worker.__file__).read_text(encoding="utf-8")
    raw_env = enforce_env_deny(
        build_worker_env(agent_cfg.env, config=env_config or EnvConfig(), worker_context=True, home=home),
        env_config or EnvConfig(),
        set((env_config or EnvConfig()).set) | set(agent_cfg.env),
    )
    # Strip any inherited parent ANTIGRAVITY_* vars (e.g. when Dashboard was
    # launched from an Antigravity Desktop terminal) so the child `agy` CLI
    # injects its own CLI Language Server credentials cleanly.
    env = {k: v for k, v in raw_env.items() if not k.upper().startswith("ANTIGRAVITY_")}

    runs_parent = agy_slots_dir(home) / "runs"
    runner_dir = runs_parent / request.operation_id
    try:
        runner_dir.mkdir(parents=True)
    except FileExistsError:
        return CleanerExecutionResult(
            False, errors=("cleanup operation already has a workspace",), outcome_unknown=True
        )
    except OSError:
        return CleanerExecutionResult(False, errors=("could not prepare cleanup workspace",))
    retain_run = False
    try:
        (runner_dir / "cleanup_worker.py").write_text(worker_source, encoding="utf-8")
        request_payload = {
            **request.identity,
            "allowed_project_ids": [request.project_id],
            "cutoff_utc": request.cutoff_utc.astimezone(UTC).isoformat(),
        }
        (runner_dir / "request.json").write_text(
            json.dumps(request_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        settings_path = runner_dir / "minimal-settings.json"
        settings_path.write_text(
            json.dumps(build_tier3_settings(WORKER_COMMAND), indent=2) + "\n",
            encoding="utf-8",
        )
        argv = build_tier3_cleaner_command(
            agy_cmd=agy_cmd,
            slot_cfg=slot_cfg,
            target_project_id=request.project_id,
            runner_dir=runner_dir,
            settings_path=settings_path,
        )
        prompt = build_tier3_cleaner_prompt(
            command_line=WORKER_COMMAND,
            max_retries=slot_cfg.cleaner_max_retries,
        )
        stdin_bytes = stream_json_prompt(prompt)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        outer_timeout = slot_cfg.cleaner_timeout_sec + 30
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=str(runner_dir),
                env=env,
                **kwargs,
            )
        except OSError:
            return CleanerExecutionResult(False, errors=("failed to launch agy slot cleaner",))
        retain_run = True
        try:
            _, stderr = proc.communicate(input=stdin_bytes, timeout=outer_timeout)
        except subprocess.TimeoutExpired:
            if proc.poll() is None:
                with suppress(OSError):
                    proc.kill()
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1)
            # Descendants can retain the pipe or run independently. Never perform an
            # unbounded second communicate, or treat killing the direct child as proof.
            reason = "cleaner timed out; execution unconfirmed"
            receipt = _read_worker_receipt(runner_dir / "result.json", request.identity, reason)
            if receipt.worker_receipt_valid:
                return replace(receipt, ok=False, errors=(*receipt.errors, reason), outcome_unknown=True)
            return CleanerExecutionResult(False, errors=(reason,), outcome_unknown=True)
        reason = f"no valid result.json (agy exit {proc.returncode}, stderr present={bool(stderr)})"
        result = _read_worker_receipt(runner_dir / "result.json", request.identity, reason)
        return result
    except OSError:
        reason = "cleaner completion unconfirmed" if retain_run else "could not prepare cleanup workspace"
        return CleanerExecutionResult(False, errors=(reason,), outcome_unknown=retain_run)
    finally:
        if not retain_run:
            try:
                shutil.rmtree(runner_dir)
            except OSError:
                log.warning("could not remove completed AGY cleanup workspace")


def cleanup_agy_slot(
    home: Path,
    slot_name: str,
    *,
    dry_run: bool = True,
    confirm: str | None = None,
    requested_project_id: str | None = None,
    agent_cfg: AgentConfig | None = None,
    env_config: EnvConfig | None = None,
    now: datetime | None = None,
    projects_dir: Path | None = None,
    cleaner_runner: CleanerRunner | None = None,
) -> dict[str, Any]:
    """Validate Drain Guard + hard whitelist and run dry-run or live cleanup on one slot."""
    clean_slot_name = (slot_name or "").strip()
    if not clean_slot_name:
        raise SlotConfigError("slot name is required")

    resolved_agent, resolved_env, _agy_agents = _resolve_agy_agent(home, agent_cfg)
    if resolved_agent is None or not resolved_agent.agy_slots.enabled:
        raise SlotConfigError("AGY slot rotation is not enabled")
    slot_cfg = resolved_agent.agy_slots

    slot_map = {spec.name: spec for spec in slot_cfg.slots}
    if clean_slot_name not in slot_map:
        known = ", ".join(sorted(slot_map))
        raise SlotConfigError(f"unknown AGY slot {clean_slot_name!r}; configured slots: {known}")
    target_spec = slot_map[clean_slot_name]
    allowed_project_ids = tuple(spec.project_id for spec in slot_cfg.slots)

    # Hard whitelist: reject any external project_id parameter that does not match the slot.
    if requested_project_id is not None and requested_project_id.strip() != target_spec.project_id:
        raise SlotConfigError("project_id override is forbidden; cleanup only targets configured slot project IDs")
    if target_spec.project_id not in allowed_project_ids:
        raise SlotConfigError(f"slot {clean_slot_name!r} project_id is outside configured whitelist")

    if type(dry_run) is not bool:
        raise SlotConfigError("dry_run must be a boolean")
    if not dry_run and confirm != target_spec.name:
        raise SlotConfigError(f'confirmation required for live cleanup: pass {{"confirm": "{target_spec.name}"}}')
    if slot_cfg.cleaner_mode == "disabled":
        raise SlotConfigError("AGY slot cleanup is disabled (cleaner_mode='disabled')")

    if projects_dir is None:
        from agent_bridge.adapters.antigravity import agy_projects_dir

        projects_dir = agy_projects_dir()

    request_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "ok": False,
        "slot": target_spec.name,
        "project_id": target_spec.project_id,
        "dry_run": dry_run,
        "operation_id": request_id,
        "slot_status": "unknown",
        **dict.fromkeys(_COUNT_FIELDS, 0),
        "deleted_ids": [],
        "would_delete_ids": [],
        "eligible_ids": [],
        "blocked_sessions": [],
        "errors": [],
        "outcome": "not_started",
        "executed_at": iso(_ensure_aware(now)),
        "receipt_saved": False,
        "counts_confirmed": True,
    }
    try:
        with slot_admission(home, target_spec, projects_dir, claim_on_entry=False) as owner:
            # Sample the clock after acquiring the registration. All other Bridge
            # admissions for this project remain excluded through final persistence.
            current_utc = _ensure_aware(now).astimezone(UTC)
            snapshot = describe_slots(
                home, agent_cfg=agent_cfg, now=current_utc, projects_dir=projects_dir, cleaner_available=True
            )
            if (
                [(r["name"], r["project_id"]) for r in snapshot["slots"]]
                != [(s.name, s.project_id) for s in slot_cfg.slots]
                or snapshot["rotation_tz"] != slot_cfg.rotation_tz
                or snapshot["grace_hours"] != slot_cfg.grace_hours
                or snapshot["cleaner"]["mode"] != slot_cfg.cleaner_mode
            ):
                raise SlotGuardError("slot configuration changed during admission")
            info = next(row for row in snapshot["slots"] if row["name"] == target_spec.name)
            status = info["status"]
            payload.update(slot_status=status, blocked_sessions=info["blocked_sessions"], executed_at=iso(current_utc))
            if info["guard_error"] or (not dry_run and status != "releasable"):
                raise SlotGuardError(info["guard_error"] or f"slot is {status}")
            if not dry_run:
                # The supported service request has no atomic idle/version precondition.
                raise SlotGuardError("conditional_delete_unavailable")
            request = CleanerRequest(
                target_spec.name,
                target_spec.project_id,
                allowed_project_ids,
                current_utc - timedelta(hours=slot_cfg.grace_hours),
                dry_run,
                request_id,
            )
            owner_path = _owner_path(target_spec)
            # A killed dashboard leaves an explicit, durable exclusion instead of
            # silently releasing the project when the OS drops its advisory lock.
            atomic_write_json(owner_path, {**owner, "operation_id": request_id})
            payload.update(outcome="unconfirmed", counts_confirmed=False, **dict.fromkeys(_COUNT_FIELDS))
            try:
                _save_cleanup_receipt(home, target_spec.name, {**payload, "receipt_saved": True})
            except (OSError, SlotGuardError):
                atomic_write_json(owner_path, owner)
                payload.update(outcome="not_started", counts_confirmed=True, **dict.fromkeys(_COUNT_FIELDS, 0))
                raise SlotGuardError("could not persist cleanup admission") from None
            try:
                result = (cleaner_runner or execute_tier3_cleanup)(
                    request,
                    agent_cfg=resolved_agent,
                    home=home,
                    env_config=env_config or resolved_env,
                )
            except Exception:
                result = CleanerExecutionResult(False, errors=("cleaner completion unconfirmed",), outcome_unknown=True)
            payload.update(asdict(result))
            unknown = payload.pop("outcome_unknown")
            counts_confirmed = payload.pop("worker_receipt_valid") or not unknown
            payload["counts_confirmed"] = counts_confirmed
            for key in ("deleted_ids", "would_delete_ids", "eligible_ids"):
                payload[key] = list(payload[key])
            payload["errors"] = [_safe_runner_error(e) for e in result.errors]
            if unknown:
                payload["outcome"] = "unknown"
                if not counts_confirmed:
                    payload.update(dict.fromkeys(_COUNT_FIELDS))
            else:
                payload["outcome"] = "completed" if result.ok else "partial" if result.checked else "failed"
            try:
                _save_cleanup_receipt(home, target_spec.name, {**payload, "receipt_saved": True})
                payload["receipt_saved"] = True
            except (OSError, SlotGuardError):
                payload["receipt_error"] = "terminal receipt could not be saved; project remains held"
                return payload
            if not unknown:
                try:
                    atomic_write_json(owner_path, owner)
                except OSError:
                    payload["guard_error"] = "completed operation; project ownership could not be released"
                else:
                    if cleaner_runner is None and result.worker_receipt_valid:
                        # The durable receipt now contains the confirmed result.
                        try:
                            shutil.rmtree(agy_slots_dir(home) / "runs" / request_id)
                        except OSError:
                            log.warning("could not remove completed AGY cleanup workspace")
            return payload
    except (SlotGuardError, OSError) as exc:
        message = str(exc) if isinstance(exc, SlotGuardError) else "slot guard is busy or unavailable"
        payload["errors"] = [message]
        raise SlotCleanupConflictError(message, payload) from exc
