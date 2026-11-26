"""Antigravity daily empty-project slot rotation and safe ConversationService cleanup.

Design summary (Gate 0 verified):
- Bridge routes *new* AGY sessions (``session.native_session_id is None``) to one of
  2..N pre-created 0-folder empty AGY projects (``projectResources: {}``), rotating
  atomically at local midnight in ``rotation_tz``.
- Existing sessions with ``native_session_id`` stay pinned to ``--conversation <id>``
  and never migrate across slots.
- A slot is only ``releasable`` when ALL three Drain Guard locks pass:
    1. Not the currently active slot, and past ``grace_hours`` since its active day ended.
    2. Zero active/unclosed Bridge sessions (or queued/running tasks) bound to the slot.
    3. Official ``ConversationService`` deletes only ``RUN_STATE_IDLE`` conversations
       strictly older than ``grace_hours`` inside a hard-whitelisted slot ``project_id``.
- Cleanup runs via Tier 3 (headless ``agy -p`` ``run_command`` with a single-command
  ``--settings`` allowlist and Scheme A ``--project <target_slot_project_id>``) so
  credentials come strictly from AGY's official CLI Language Server without touching
  Desktop UIA, raw storage files, or ``default-cli-project``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
from agent_bridge.diagnostics import redact_diagnostic
from agent_bridge.models import ProcState, Session, Task, TaskStatus, iso
from agent_bridge.paths import agy_slot_receipts_path, agy_slots_dir, state_path
from agent_bridge.persist import atomic_write_json, file_lock, read_json_strict
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


@dataclass(frozen=True)
class CleanerRequest:
    slot: str
    project_id: str
    allowed_project_ids: tuple[str, ...]
    cutoff_utc: datetime
    dry_run: bool


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
    tz = resolve_rotation_timezone(slot_cfg.rotation_tz)
    local_now = _ensure_aware(now).astimezone(tz)
    idx = local_now.date().toordinal() % len(slot_cfg.slots)
    return slot_cfg.slots[idx]


def compute_slot_schedule(
    slot_cfg: AgySlotConfig,
    *,
    now: datetime | None = None,
) -> tuple[datetime, list[SlotTiming]]:
    """Compute local timestamp and per-slot rotation/cooling windows."""
    tz = resolve_rotation_timezone(slot_cfg.rotation_tz)
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
        elapsed_hours = max(
            0.0,
            (local_now.astimezone(UTC) - last_active_end.astimezone(UTC)).total_seconds() / 3600.0,
        )
        remaining_hours = max(0.0, grace_hours - elapsed_hours)
        timings.append(
            SlotTiming(
                spec=spec,
                is_active=False,
                days_since_active=days_ago,
                last_active_end=last_active_end,
                cooling_elapsed_hours=round(elapsed_hours, 3),
                grace_remaining_hours=round(remaining_hours, 3),
            )
        )
    return local_now, timings


def load_bridge_sessions_and_tasks(home: Path) -> tuple[list[Session], list[Task]]:
    """Read Bridge ``state.json`` safely without mutating it or failing on missing state."""
    path = state_path(home)
    if not path.is_file():
        return [], []
    try:
        raw = read_json_strict(path, {})
    except (OSError, ValueError):
        log.warning("could not parse %s while inspecting AGY slots", path, exc_info=True)
        return [], []
    if not isinstance(raw, dict):
        return [], []

    sessions: list[Session] = []
    for row in raw.get("sessions") or []:
        if not isinstance(row, dict):
            continue
        try:
            sessions.append(Session.model_validate(row))
        except Exception:
            continue

    tasks: list[Task] = []
    for row in raw.get("tasks") or []:
        if not isinstance(row, dict):
            continue
        try:
            tasks.append(Task.model_validate(row))
        except Exception:
            continue
    return sessions, tasks


def find_blocking_sessions(
    slot: AgySlotSpec,
    sessions: Sequence[Session],
    tasks: Sequence[Task],
) -> list[dict[str, Any]]:
    """Return active or unclosed Bridge sessions bound to ``slot``."""
    pending_session_ids = {task.session_id for task in tasks if task.status in {TaskStatus.queued, TaskStatus.running}}
    blocked: list[dict[str, Any]] = []
    for session in sessions:
        bound = session.agy_slot == slot.name or session.agy_project_id == slot.project_id
        if not bound:
            continue
        has_pending_task = session.session_id in pending_session_ids
        if has_pending_task:
            reason = "active_task"
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


def inspect_slot_project_file(project_id: str, projects_dir: Path | None) -> tuple[bool, bool | None]:
    """Read-only check whether ``<project_id>.json`` exists and has empty ``projectResources``."""
    if projects_dir is None or not projects_dir.is_dir():
        return False, None
    path = projects_dir / f"{project_id}.json"
    if not path.is_file():
        return False, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, None
    if not isinstance(data, dict):
        return True, None
    resources = data.get("projectResources")
    if resources is None or resources == {}:
        return True, True
    if isinstance(resources, dict):
        entries = resources.get("resources")
        if entries == [] or (not resources):
            return True, True
        return True, False
    return True, False


def _load_cleanup_receipts(home: Path) -> dict[str, Any]:
    path = agy_slot_receipts_path(home)
    if not path.is_file():
        return {"last_cleanup": None, "by_slot": {}}
    try:
        data = read_json_strict(path, {})
    except (OSError, ValueError):
        return {"last_cleanup": None, "by_slot": {}}
    if not isinstance(data, dict):
        return {"last_cleanup": None, "by_slot": {}}
    by_slot = data.get("by_slot")
    return {
        "last_cleanup": data.get("last_cleanup") if isinstance(data.get("last_cleanup"), dict) else None,
        "by_slot": dict(by_slot) if isinstance(by_slot, dict) else {},
    }


def _save_cleanup_receipt(home: Path, slot_name: str, receipt: dict[str, Any]) -> None:
    store = _load_cleanup_receipts(home)
    store["last_cleanup"] = receipt
    store["by_slot"][slot_name] = receipt
    atomic_write_json(agy_slot_receipts_path(home), store)


def _resolve_agy_agent(home: Path, agent_cfg: AgentConfig | None = None) -> tuple[AgentConfig | None, EnvConfig]:
    if agent_cfg is not None:
        return agent_cfg, EnvConfig()
    cfg = load_config(home)
    return cfg.agents.get("antigravity"), cfg.env


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
    resolved_agent, _env_cfg = _resolve_agy_agent(home, agent_cfg)
    slot_cfg = resolved_agent.agy_slots if resolved_agent is not None else AgySlotConfig()
    local_now, timings = compute_slot_schedule(slot_cfg, now=now)
    sessions, tasks = load_bridge_sessions_and_tasks(home)
    receipts = _load_cleanup_receipts(home)

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
        blocked = find_blocking_sessions(spec, sessions, tasks)
        if timing.is_active:
            status = "active"
            active_slot_name = spec.name
            active_project_id = spec.project_id
        elif timing.grace_remaining_hours > 0.0:
            status = "cooling"
        elif blocked:
            status = "blocked"
        else:
            status = "releasable"

        exists, is_empty = inspect_slot_project_file(spec.project_id, projects_dir)
        slot_rows.append(
            {
                "name": spec.name,
                "project_id": spec.project_id,
                "status": status,
                "is_active": timing.is_active,
                "days_since_active": timing.days_since_active,
                "last_active_end": timing.last_active_end.isoformat() if timing.last_active_end else None,
                "cooling_elapsed_hours": timing.cooling_elapsed_hours,
                "grace_remaining_hours": timing.grace_remaining_hours,
                "bound_sessions_count": len(blocked),
                "blocked_sessions": blocked,
                "project_file_exists": exists,
                "project_is_empty": is_empty,
                "last_cleanup": receipts["by_slot"].get(spec.name),
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
            "mode": slot_cfg.cleaner_mode,
            "model": slot_cfg.cleaner_model,
            "available": avail,
            "reason": reason,
        },
        "slots": slot_rows,
        "last_cleanup": receipts["last_cleanup"],
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


def _last_stream_tool_diagnostic(raw_stdout: bytes) -> str:
    """Extract the last redacted tool output/error from agy stream-json when result.json is absent."""
    last = ""
    for line in raw_stdout.splitlines():
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        step = obj.get("step_update")
        if isinstance(step, dict) and step.get("step_type") == "tool":
            info = step.get("tool_info")
            if isinstance(info, dict):
                text = str(info.get("error") or info.get("output") or "").strip()
                if text:
                    last = redact_diagnostic(text)[-300:]
    return last


def execute_tier3_cleanup(
    request: CleanerRequest,
    *,
    agent_cfg: AgentConfig,
    home: Path,
    env_config: EnvConfig | None = None,
) -> CleanerExecutionResult:
    """Run Tier 3 headless ``agy -p`` cleanup using Scheme A and minimal command allowlist."""
    if request.project_id not in request.allowed_project_ids:
        return CleanerExecutionResult(
            ok=False,
            errors=(f"whitelist rejected project_id {request.project_id!r}",),
        )

    from agent_bridge.adapters.antigravity import discovered_agy_commands, stream_json_prompt

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
    runs_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clean-", dir=str(runs_parent)) as tmp:
        runner_dir = Path(tmp)
        (runner_dir / "cleanup_worker.py").write_text(worker_source, encoding="utf-8")
        request_payload = {
            "slot": request.slot,
            "project_id": request.project_id,
            "allowed_project_ids": list(request.allowed_project_ids),
            "cutoff_utc": request.cutoff_utc.astimezone(UTC).isoformat(),
            "dry_run": request.dry_run,
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
            proc = subprocess.run(
                argv,
                input=stdin_bytes,
                capture_output=True,
                cwd=str(runner_dir),
                env=env,
                timeout=outer_timeout,
                check=False,
                **kwargs,
            )
        except subprocess.TimeoutExpired:
            return CleanerExecutionResult(
                ok=False,
                errors=(f"agy slot cleaner timed out after {outer_timeout}s",),
            )
        except OSError as exc:
            return CleanerExecutionResult(
                ok=False,
                errors=(f"failed to launch agy slot cleaner: {redact_diagnostic(str(exc))}",),
            )

        result_path = runner_dir / "result.json"
        if not result_path.is_file():
            stderr_tail = redact_diagnostic(proc.stderr.decode("utf-8", errors="replace").strip()[-500:])
            tool_tail = _last_stream_tool_diagnostic(proc.stdout)
            parts = [p for p in (tool_tail, stderr_tail) if p]
            detail = f": {' | '.join(parts)}" if parts else ""
            return CleanerExecutionResult(
                ok=False,
                errors=(f"cleaner worker produced no result.json (agy exit {proc.returncode}){detail}",),
            )

        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return CleanerExecutionResult(
                ok=False,
                errors=(f"cleaner worker wrote unreadable result.json: {exc}",),
            )

        errors = tuple(
            redact_diagnostic(str(item)) for item in (raw.get("errors") if isinstance(raw.get("errors"), list) else [])
        )
        return CleanerExecutionResult(
            ok=bool(raw.get("ok")) and len(errors) == 0,
            checked=int(raw.get("checked") or 0),
            deleted=int(raw.get("deleted") or 0),
            would_delete=int(raw.get("would_delete") or 0),
            deleted_ids=tuple(str(x) for x in (raw.get("deleted_ids") or [])),
            would_delete_ids=tuple(str(x) for x in (raw.get("would_delete_ids") or [])),
            skipped_running=int(raw.get("skipped_running") or 0),
            skipped_recent=int(raw.get("skipped_recent") or 0),
            errors=errors,
        )


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

    resolved_agent, resolved_env = _resolve_agy_agent(home, agent_cfg)
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

    if not dry_run and confirm != target_spec.name:
        raise SlotConfigError(f'confirmation required for live cleanup: pass {{"confirm": "{target_spec.name}"}}')

    if slot_cfg.cleaner_mode == "disabled":
        raise SlotConfigError("AGY slot cleanup is disabled (cleaner_mode='disabled')")

    current_utc = _ensure_aware(now).astimezone(UTC)
    status_snapshot = describe_slots(
        home,
        agent_cfg=resolved_agent,
        now=current_utc,
        projects_dir=projects_dir,
        cleaner_available=True if cleaner_runner is not None else None,
    )
    slot_info = next(row for row in status_snapshot["slots"] if row["name"] == target_spec.name)
    slot_status = str(slot_info["status"])
    blocked_sessions = list(slot_info["blocked_sessions"])

    def _build_payload(
        *,
        ok: bool,
        checked: int = 0,
        deleted: int = 0,
        would_delete: int = 0,
        deleted_ids: Sequence[str] = (),
        would_delete_ids: Sequence[str] = (),
        skipped_running: int = 0,
        skipped_recent: int = 0,
        errors: Sequence[str] = (),
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "slot": target_spec.name,
            "project_id": target_spec.project_id,
            "dry_run": dry_run,
            "slot_status": slot_status,
            "checked": checked,
            "deleted": deleted,
            "would_delete": would_delete,
            "deleted_ids": list(deleted_ids),
            "would_delete_ids": list(would_delete_ids),
            "skipped_running": skipped_running,
            "skipped_recent": skipped_recent,
            "blocked_sessions": blocked_sessions,
            "errors": list(errors),
            "executed_at": iso(current_utc),
        }

    # Three-lock Drain Guard (Locks 1 & 2) enforced hard before spawning any live cleaner.
    if not dry_run:
        if slot_status == "active":
            msg = f"slot {target_spec.name!r} is currently active and cannot be cleaned"
            raise SlotCleanupConflictError(msg, _build_payload(ok=False, errors=[msg]))
        if slot_status == "cooling":
            msg = (
                f"slot {target_spec.name!r} is still cooling within the {slot_cfg.grace_hours}h grace window "
                f"({slot_info['grace_remaining_hours']}h remaining)"
            )
            raise SlotCleanupConflictError(msg, _build_payload(ok=False, errors=[msg]))
        if slot_status == "blocked" or blocked_sessions:
            ids = ", ".join(item["session_id"] for item in blocked_sessions)
            msg = (
                f"slot {target_spec.name!r} is blocked by {len(blocked_sessions)} active/unclosed "
                f"Bridge session(s): {ids}"
            )
            raise SlotCleanupConflictError(msg, _build_payload(ok=False, errors=[msg]))

    cutoff_utc = current_utc - timedelta(hours=slot_cfg.grace_hours)
    runner = cleaner_runner or execute_tier3_cleanup
    lock_path = agy_slots_dir(home) / "cleanup.lock"
    try:
        with file_lock(lock_path, timeout_sec=CLEANUP_LOCK_TIMEOUT_SEC):
            # Re-verify Bridge sessions under lock for live cleanup to narrow TOCTOU races.
            if not dry_run:
                sessions_now, tasks_now = load_bridge_sessions_and_tasks(home)
                rechecked_blocked = find_blocking_sessions(target_spec, sessions_now, tasks_now)
                if rechecked_blocked:
                    blocked_sessions = rechecked_blocked
                    slot_status = "blocked"
                    ids = ", ".join(item["session_id"] for item in rechecked_blocked)
                    msg = (
                        f"slot {target_spec.name!r} became blocked by {len(rechecked_blocked)} "
                        f"Bridge session(s) before cleanup started: {ids}"
                    )
                    raise SlotCleanupConflictError(msg, _build_payload(ok=False, errors=[msg]))

            exec_result = runner(
                CleanerRequest(
                    slot=target_spec.name,
                    project_id=target_spec.project_id,
                    allowed_project_ids=allowed_project_ids,
                    cutoff_utc=cutoff_utc,
                    dry_run=dry_run,
                ),
                agent_cfg=resolved_agent,
                home=home,
                env_config=env_config or resolved_env,
            )
    except TimeoutError as exc:
        msg = f"another AGY slot cleanup is already running ({exc})"
        raise SlotCleanupConflictError(msg, _build_payload(ok=False, errors=[msg])) from exc

    payload = _build_payload(
        ok=exec_result.ok,
        checked=exec_result.checked,
        deleted=exec_result.deleted,
        would_delete=exec_result.would_delete,
        deleted_ids=exec_result.deleted_ids,
        would_delete_ids=exec_result.would_delete_ids,
        skipped_running=exec_result.skipped_running,
        skipped_recent=exec_result.skipped_recent,
        errors=exec_result.errors,
    )
    _save_cleanup_receipt(home, target_spec.name, payload)
    return payload
