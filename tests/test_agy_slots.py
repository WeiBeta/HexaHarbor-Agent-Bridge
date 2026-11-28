from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import subprocess
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_bridge import agy_slot_worker, agy_slots
from agent_bridge.adapters.antigravity import AgyAdapter, agy_projects_dir
from agent_bridge.agy_slot_worker import parse_rfc3339, run_cleanup_request, validate_loopback_address
from agent_bridge.agy_slots import (
    WORKER_COMMAND,
    CleanerExecutionResult,
    CleanerRequest,
    SlotCleanupConflictError,
    SlotConfigError,
    build_tier3_cleaner_command,
    build_tier3_settings,
    cleanup_agy_slot,
    compute_slot_schedule,
    describe_slots,
    execute_tier3_cleanup,
    select_active_slot,
)
from agent_bridge.config import (
    AgentConfig,
    AgySlotConfig,
    AgySlotSpec,
    load_config,
    resolve_rotation_timezone,
)
from agent_bridge.dashboard_server import DashboardRequestHandler
from agent_bridge.models import ProcState, Session, Task, TaskStatus
from agent_bridge.paths import state_path
from agent_bridge.persist import atomic_write_json

SLOT_A = AgySlotSpec(name="slotA", project_id="11111111-1111-4111-8111-111111111111")
SLOT_B = AgySlotSpec(name="slotB", project_id="22222222-2222-4222-8222-222222222222")
SLOT_C = AgySlotSpec(name="slotC", project_id="33333333-3333-4333-8333-333333333333")
EXTERNAL_PROJECT_ID = "99999999-9999-4999-8999-999999999999"


def _three_slot_cfg(**overrides: Any) -> AgySlotConfig:
    params: dict[str, Any] = {
        "enabled": True,
        "slots": [SLOT_A, SLOT_B, SLOT_C],
        "rotation_tz": "+08:00",
        "grace_hours": 24,
        "cleaner_mode": "cli_run_command",
        "cleaner_model": "gemini-3.8-flash-low",
    }
    params.update(overrides)
    return AgySlotConfig(**params)


def _write_slot_overlay(
    home: Path, *, enabled: bool = True, grace_hours: int = 24, cleaner_mode: str = "cli_run_command"
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    toml = f"""
[agents.antigravity.agy_slots]
enabled = {"true" if enabled else "false"}
rotation_tz = "+08:00"
grace_hours = {grace_hours}
cleaner_mode = "{cleaner_mode}"
cleaner_model = "gemini-3.8-flash-low"

[[agents.antigravity.agy_slots.slots]]
name = "{SLOT_A.name}"
project_id = "{SLOT_A.project_id}"

[[agents.antigravity.agy_slots.slots]]
name = "{SLOT_B.name}"
project_id = "{SLOT_B.project_id}"

[[agents.antigravity.agy_slots.slots]]
name = "{SLOT_C.name}"
project_id = "{SLOT_C.project_id}"
"""
    (home / "agents.toml").write_text(toml.strip() + "\n", encoding="utf-8")


def _write_projects(projects_dir: Path, *, make_slot_c_non_empty: bool = False) -> None:
    projects_dir.mkdir(parents=True, exist_ok=True)
    for spec in (SLOT_A, SLOT_B, SLOT_C):
        (projects_dir / f"{spec.project_id}.json").write_text(
            json.dumps({"id": spec.project_id, "name": spec.name, "projectResources": {}}),
            encoding="utf-8",
        )
    if make_slot_c_non_empty:
        (projects_dir / f"{SLOT_C.project_id}.json").write_text(
            json.dumps(
                {
                    "id": SLOT_C.project_id,
                    "name": SLOT_C.name,
                    "projectResources": {"resources": [{"folderUri": "file:///C:/repo"}]},
                }
            ),
            encoding="utf-8",
        )


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    profile = tmp_path / "os-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: profile))
    _write_projects(agy_projects_dir())
    monkeypatch.setattr("agent_bridge.adapters.antigravity.discovered_agy_commands", lambda: [])
    monkeypatch.setattr(agy_slots.psutil, "pids", lambda: [])
    monkeypatch.setattr(
        "agent_bridge.agy_slots.build_worker_env",
        lambda *a, **kw: {
            "USERPROFILE": str(profile),
            "ANTIGRAVITY_LS_ADDRESS": "fixture-address",
            "antigravity_csrf_token": "fixture-marker",
            "KEEP_SYNTHETIC_SETTING": "yes",
        },
    )


def _json_req(server, method, path, *, csrf=None, payload=None, extra_headers=None):
    """Exercise the HTTP handler and JSON contract using an in-memory request/response."""
    handler = object.__new__(DashboardRequestHandler)
    handler.server = server
    handler.path = path
    body = json.dumps(payload).encode() if payload is not None else b""
    handler.rfile = io.BytesIO(body)
    host = f"127.0.0.1:{server.bound_port}"
    handler.headers = {
        "Host": host,
        "Origin": f"http://{host}",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        **(extra_headers or {}),
    }
    if csrf is not None:
        handler.headers["X-Bridge-CSRF"] = csrf
    responses = []
    handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append((int(status), payload))
    getattr(handler, f"do_{method}")()
    assert len(responses) == 1
    return responses[0]


def _synthetic_cli_profile():
    profile = Path.home() / ".gemini" / "antigravity-cli"
    (profile / "conversations").mkdir(parents=True)
    (profile / "brain").mkdir()
    db = sqlite3.connect(profile / "conversation_summaries.db")
    try:
        db.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY)")
        db.commit()
    finally:
        db.close()
    return profile


def _batch_fields(request):
    return {
        "cutoff_utc": request["cutoff_utc"],
        "batch_number": request["batch_number"],
        "self_conversation_id": "fixture-control",
        "http_200_ids": [],
        "has_more": False,
        "worker_elapsed_ms": 3,
    }


# ---------------------------------------------------------------------------
# 1. Config, timezone resolution, and backward compatibility
# ---------------------------------------------------------------------------


def test_default_config_keeps_agy_slots_disabled(tmp_path: Path):
    cfg = load_config(tmp_path)
    agy = cfg.get("antigravity")
    assert agy.agy_slots.enabled is False
    assert agy.agy_slots.slots == []
    assert agy.agy_slots.grace_hours == 24
    assert select_active_slot(agy.agy_slots) is None


def test_overlay_toml_loads_agy_slots_and_preserves_other_agent_fields(tmp_path: Path):
    _write_slot_overlay(tmp_path, enabled=True, grace_hours=36)
    cfg = load_config(tmp_path)
    agy = cfg.get("antigravity")
    assert agy.protocol == "agy"
    assert agy.agy_slots.enabled is True
    assert agy.agy_slots.grace_hours == 36
    assert [s.name for s in agy.agy_slots.slots] == ["slotA", "slotB", "slotC"]


def test_agy_slot_config_validation_rejects_bad_configs():
    # Enabled with < 2 slots
    with pytest.raises(ValueError, match="at least 2"):
        AgySlotConfig(enabled=True, slots=[SLOT_A])
    # Duplicate slot names
    with pytest.raises(ValueError, match="duplicate agy slot name"):
        AgySlotConfig(
            enabled=True,
            slots=[SLOT_A, AgySlotSpec(name=SLOT_A.name, project_id=SLOT_B.project_id)],
        )
    # Duplicate project_ids
    with pytest.raises(ValueError, match="duplicate agy slot project_id"):
        AgySlotConfig(
            enabled=True,
            slots=[SLOT_A, AgySlotSpec(name="slotX", project_id=SLOT_A.project_id)],
        )
    # Invalid slot name
    with pytest.raises(ValueError, match="invalid agy slot name"):
        AgySlotSpec(name="../evil", project_id=SLOT_A.project_id)
    # Invalid cleaner mode
    with pytest.raises(ValueError, match="cleaner_mode"):
        AgySlotConfig(enabled=False, cleaner_mode="desktop_uia")
    # Invalid timezone
    with pytest.raises(ValueError, match="rotation_tz"):
        AgySlotConfig(enabled=False, rotation_tz="Mars/Olympus_Mons")


def test_resolve_rotation_timezone_handles_windows_without_tzdata():
    assert resolve_rotation_timezone("UTC") == UTC
    assert resolve_rotation_timezone("+08:00") == timezone(timedelta(hours=8))
    assert resolve_rotation_timezone("UTC-05:30") == timezone(timedelta(hours=-5, minutes=-30))
    shanghai = resolve_rotation_timezone("Asia/Shanghai")
    sample = datetime(2026, 11, 26, 0, 0, tzinfo=UTC).astimezone(shanghai)
    assert sample.hour == 8
    assert resolve_rotation_timezone("local") is not None


def test_legacy_session_deserializes_without_agy_slot_fields():
    legacy = {
        "session_id": "sess_legacy",
        "agent": "antigravity",
        "cwd": "C:/repo",
        "native_session_id": "conv-old",
        "proc_state": "idle_unloaded",
        "turns": 2,
    }
    session = Session.model_validate(legacy)
    assert session.agy_slot is None
    assert session.agy_project_id is None


# ---------------------------------------------------------------------------
# 2. Natural-day rotation & strict routing boundary
# ---------------------------------------------------------------------------


def test_midnight_rotation_3_slots_and_cooling_windows():
    cfg = _three_slot_cfg(rotation_tz="+08:00", grace_hours=24)
    tz = timezone(timedelta(hours=8))

    before_midnight = datetime(2026, 11, 26, 23, 59, 59, tzinfo=tz)
    after_midnight = datetime(2026, 11, 27, 0, 0, 0, tzinfo=tz)
    midday_next = datetime(2026, 11, 27, 14, 0, 0, tzinfo=tz)

    slot_day1 = select_active_slot(cfg, now=before_midnight)
    slot_day2 = select_active_slot(cfg, now=after_midnight)
    assert slot_day1 is not None and slot_day2 is not None
    assert slot_day1.name != slot_day2.name

    _local_now, timings = compute_slot_schedule(cfg, now=midday_next)
    by_days = {t.days_since_active: t for t in timings}
    # Day 0: active (today)
    assert by_days[0].is_active is True
    assert by_days[0].spec.name == slot_day2.name
    # Day 1: yesterday -> ended 14h ago -> still inside 24h grace window
    assert by_days[1].is_active is False
    assert by_days[1].spec.name == slot_day1.name
    assert by_days[1].cooling_elapsed_hours == pytest.approx(14.0, abs=0.01)
    assert by_days[1].grace_remaining_hours == pytest.approx(10.0, abs=0.01)
    # Day 2: 2 days ago -> ended 38h ago -> grace elapsed (0h remaining)
    assert by_days[2].is_active is False
    assert by_days[2].cooling_elapsed_hours == pytest.approx(38.0, abs=0.01)
    assert by_days[2].grace_remaining_hours == 0.0


def test_midnight_rotation_2_slots_alternates_daily():
    cfg = AgySlotConfig(enabled=True, slots=[SLOT_A, SLOT_B], rotation_tz="UTC", grace_hours=12)
    d1 = select_active_slot(cfg, now=datetime(2026, 11, 26, 12, 0, tzinfo=UTC))
    d2 = select_active_slot(cfg, now=datetime(2026, 11, 27, 12, 0, tzinfo=UTC))
    d3 = select_active_slot(cfg, now=datetime(2026, 11, 28, 12, 0, tzinfo=UTC))
    assert d1 is not None and d2 is not None and d3 is not None
    assert d1.name != d2.name
    assert d1.name == d3.name


def test_agy_adapter_disabled_matches_v014_behavior(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agent_bridge.adapters.antigravity.resolve_command", lambda cmd, fb=None: ["agy"])
    monkeypatch.setattr("agent_bridge.adapters.antigravity.agy_projects_dir", lambda: tmp_path / "empty_projects")
    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )
    session = Session(session_id="s1", agent="antigravity", cwd=str(tmp_path))
    task = Task(task_id="t1", session_id="s1", agent="antigravity", message="hi", cwd=str(tmp_path))
    cmd = adapter._build_cmd(session, task)
    assert "--new-project" in cmd
    assert "--project" not in cmd
    assert "--add-dir" in cmd
    assert session.agy_slot is None
    assert session.agy_project_id is None


def test_agy_adapter_routes_new_session_to_active_slot_and_pins_followup(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agent_bridge.adapters.antigravity.resolve_command", lambda cmd, fb=None: ["agy"])
    # Even if an old folder-bound project exists, slot mode routes new sessions to the active empty slot.
    projects_dir = tmp_path / "projects"
    _write_projects(projects_dir)
    (projects_dir / "legacy.json").write_text(
        json.dumps(
            {
                "id": "legacy-folder-project",
                "projectResources": {"resources": [{"folderUri": "file://" + tmp_path.as_posix()}]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("agent_bridge.adapters.antigravity.agy_projects_dir", lambda: projects_dir)

    cfg = _three_slot_cfg()
    day1_slot = SLOT_A
    day2_slot = SLOT_B
    chosen = [day1_slot]
    monkeypatch.setattr("agent_bridge.adapters.antigravity.select_active_slot", lambda _cfg: chosen[0])

    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"], agy_slots=cfg),
        tmp_path,
    )
    session = Session(session_id="s_slot", agent="antigravity", cwd=str(tmp_path))
    task1 = Task(task_id="t1", session_id=session.session_id, agent="antigravity", message="first", cwd=str(tmp_path))

    # prepare_turn persists metadata before spawn and pins slot across midnight before _build_cmd.
    adapter.prepare_turn(session)
    assert session.agy_slot == day1_slot.name
    assert session.agy_project_id == day1_slot.project_id

    chosen[0] = day2_slot  # simulate midnight tick between prepare_turn and _build_cmd
    cmd1 = adapter._build_cmd(session, task1)
    assert cmd1[cmd1.index("--add-dir") + 1] == str(tmp_path)
    assert cmd1[cmd1.index("--project") + 1] == day1_slot.project_id
    assert "--new-project" not in cmd1
    assert "--conversation" not in cmd1

    # Turn 1 completes and gets native_session_id; Turn 2 next day MUST stay on --conversation, never re-slot.
    session.native_session_id = "conv-day1-xyz"
    task2 = Task(
        task_id="t2", session_id=session.session_id, agent="antigravity", message="follow up", cwd=str(tmp_path)
    )
    adapter.prepare_turn(session)
    cmd2 = adapter._build_cmd(session, task2)
    assert cmd2[cmd2.index("--conversation") + 1] == "conv-day1-xyz"
    assert "--project" not in cmd2
    assert "--new-project" not in cmd2
    assert session.agy_slot == day1_slot.name
    assert session.agy_project_id == day1_slot.project_id


# ---------------------------------------------------------------------------
# 3. Three-lock Drain Guard & Bridge session blocking
# ---------------------------------------------------------------------------


def _find_roles_for_now(cfg: AgySlotConfig, now: datetime) -> dict[str, AgySlotSpec]:
    _, timings = compute_slot_schedule(cfg, now=now)
    roles: dict[str, AgySlotSpec] = {}
    for t in timings:
        if t.days_since_active == 0:
            roles["active"] = t.spec
        elif t.days_since_active == 1:
            roles["cooling"] = t.spec
        elif t.days_since_active == 2:
            roles["releasable"] = t.spec
    return roles


def test_drain_guard_three_locks_and_session_lifecycle(tmp_path: Path):
    _write_slot_overlay(tmp_path, enabled=True, grace_hours=24)
    projects_dir = tmp_path / "agy_projects"
    _write_projects(projects_dir)

    tz = timezone(timedelta(hours=8))
    now = datetime(2026, 11, 27, 14, 0, 0, tzinfo=tz)
    cfg = load_config(tmp_path).get("antigravity").agy_slots
    roles = _find_roles_for_now(cfg, now)
    rel_slot = roles["releasable"]

    # Bind one unclosed (idle_unloaded) Bridge session to the otherwise releasable slot.
    atomic_write_json(
        state_path(tmp_path),
        {
            "sessions": [
                {
                    "session_id": "sess_unclosed",
                    "agent": "antigravity",
                    "cwd": str(tmp_path),
                    "native_session_id": "conv-1",
                    "proc_state": ProcState.idle_unloaded.value,
                    "agy_slot": rel_slot.name,
                    "agy_project_id": rel_slot.project_id,
                },
                {
                    "session_id": "sess_dead",
                    "agent": "antigravity",
                    "cwd": str(tmp_path),
                    "native_session_id": "conv-dead",
                    "proc_state": ProcState.dead.value,
                    "agy_slot": rel_slot.name,
                    "agy_project_id": rel_slot.project_id,
                },
            ],
            "tasks": [],
            "requests": [],
        },
    )

    desc = describe_slots(tmp_path, now=now, projects_dir=projects_dir, cleaner_available=True)
    by_name = {row["name"]: row for row in desc["slots"]}
    assert by_name[roles["active"].name]["status"] == "active"
    assert by_name[roles["cooling"].name]["status"] == "cooling"
    assert by_name[rel_slot.name]["status"] == "blocked"
    assert [b["session_id"] for b in by_name[rel_slot.name]["blocked_sessions"]] == ["sess_unclosed"]
    assert by_name[SLOT_C.name]["project_is_empty"] is True

    called = False

    def fake_runner(req: CleanerRequest, **_kw: Any) -> CleanerExecutionResult:
        nonlocal called
        called = True
        assert req.slot == rel_slot.name
        return CleanerExecutionResult(ok=True, checked=1, skipped_recent=1)

    # Lock 1: active & cooling refused for live cleanup without invoking cleaner
    for lock_role in ("active", "cooling"):
        slot = roles[lock_role]
        with pytest.raises(SlotCleanupConflictError) as exc_info:
            cleanup_agy_slot(
                tmp_path,
                slot.name,
                dry_run=False,
                confirm=slot.name,
                now=now,
                projects_dir=projects_dir,
                cleaner_runner=fake_runner,
            )
        assert exc_info.value.payload["ok"] is False
        assert called is False

    # Lock 2: blocked slot refused for live cleanup and reports blocked_sessions
    with pytest.raises(SlotCleanupConflictError) as blocked_exc:
        cleanup_agy_slot(
            tmp_path,
            rel_slot.name,
            dry_run=False,
            confirm=rel_slot.name,
            now=now,
            projects_dir=projects_dir,
            cleaner_runner=fake_runner,
        )
    assert [b["session_id"] for b in blocked_exc.value.payload["blocked_sessions"]] == ["sess_unclosed"]
    assert called is False

    # dry_run=True is still allowed to inspect a blocked slot without deleting anything
    preview = cleanup_agy_slot(
        tmp_path,
        rel_slot.name,
        dry_run=True,
        now=now,
        projects_dir=projects_dir,
        cleaner_runner=fake_runner,
    )
    assert preview["ok"] is True
    assert preview["dry_run"] is True
    assert preview["deleted"] == 0
    assert [b["session_id"] for b in preview["blocked_sessions"]] == ["sess_unclosed"]
    assert called is True

    # Closing the Bridge session clears its drain guard for the confirmed live operation.
    atomic_write_json(
        state_path(tmp_path),
        {
            "agy_slot_epoch": json.loads(state_path(tmp_path).read_text())["agy_slot_epoch"],
            "sessions": [
                {
                    "session_id": "sess_unclosed",
                    "agent": "antigravity",
                    "cwd": str(tmp_path),
                    "native_session_id": "conv-1",
                    "proc_state": ProcState.dead.value,
                    "agy_slot": rel_slot.name,
                    "agy_project_id": rel_slot.project_id,
                }
            ],
            "tasks": [],
            "requests": [],
        },
    )
    desc_after = describe_slots(tmp_path, now=now, projects_dir=projects_dir, cleaner_available=True)
    by_name_after = {row["name"]: row for row in desc_after["slots"]}
    assert by_name_after[rel_slot.name]["status"] == "releasable"

    called = False
    live = cleanup_agy_slot(
        tmp_path,
        rel_slot.name,
        dry_run=False,
        confirm=rel_slot.name,
        now=now,
        projects_dir=projects_dir,
        cleaner_runner=fake_runner,
    )
    assert live["outcome"] == "completed" and called


def test_whitelist_and_confirm_guards_reject_external_targets(tmp_path: Path):
    _write_slot_overlay(tmp_path, enabled=True, grace_hours=24)
    now = datetime(2026, 11, 27, 14, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    cfg = load_config(tmp_path).get("antigravity").agy_slots
    rel_slot = _find_roles_for_now(cfg, now)["releasable"]

    with pytest.raises(SlotConfigError, match="unknown AGY slot"):
        cleanup_agy_slot(tmp_path, "external-user-project", dry_run=True, now=now)

    with pytest.raises(SlotConfigError, match="project_id override is forbidden"):
        cleanup_agy_slot(
            tmp_path,
            rel_slot.name,
            dry_run=True,
            requested_project_id=EXTERNAL_PROJECT_ID,
            now=now,
        )

    with pytest.raises(SlotConfigError, match="confirmation required"):
        cleanup_agy_slot(tmp_path, rel_slot.name, dry_run=False, confirm="WRONG", now=now)


# ---------------------------------------------------------------------------
# 4. Official ConversationService worker protocol & whitelist tests (offline mock HTTP)
# ---------------------------------------------------------------------------


def test_worker_timestamp_and_loopback_validation():
    dt = parse_rfc3339("2026-11-26T06:01:17.748011700Z")
    assert dt == datetime(2026, 11, 26, 6, 1, 17, 748011, tzinfo=UTC)
    assert parse_rfc3339("not-a-timestamp") is None
    assert validate_loopback_address("127.0.0.1:5387") == "127.0.0.1:5387"
    with pytest.raises(ValueError, match="loopback"):
        validate_loopback_address("evil.example:5387")


def test_worker_protocol_filters_whitelist_self_running_recent_and_redacts_token(monkeypatch):
    secret_token = "synthetic-transport-marker"
    self_conv_id = "conv-cleaner-self"
    cutoff = datetime(2026, 11, 26, 6, 0, 0, tzinfo=UTC)

    pages = [
        [
            # Eligible old IDLE conversation (9-digit nanos)
            {
                "id": "conv-old-idle-1",
                "metadata": {"projectId": SLOT_B.project_id},
                "state": {"runState": "RUN_STATE_IDLE", "updateTime": "2026-11-25T05:59:59.123456789Z"},
            },
            # Scheme A own control conversation (MUST skip even if state were IDLE)
            {
                "id": self_conv_id,
                "metadata": {"projectId": SLOT_B.project_id},
                "state": {"runState": "RUN_STATE_IDLE", "updateTime": "2026-11-24T00:00:00Z"},
            },
            # Active running conversation
            {
                "id": "conv-running",
                "metadata": {"projectId": SLOT_B.project_id},
                "state": {"runState": "RUN_STATE_RUNNING", "updateTime": "2026-11-24T00:00:00Z"},
            },
            # Recent IDLE conversation (inside grace window)
            {
                "id": "conv-recent",
                "metadata": {"projectId": SLOT_B.project_id},
                "state": {"runState": "RUN_STATE_IDLE", "updateTime": "2026-11-26T06:00:00Z"},
            },
        ],
        [
            # Second page eligible old IDLE conversation (will simulate delete failure to test redaction)
            {
                "id": "conv-old-fail",
                "metadata": {"projectId": SLOT_B.project_id},
                "state": {"runState": "RUN_STATE_IDLE", "updateTime": "2026-11-24T12:00:00Z"},
            },
            # Out-of-whitelist rogue item returned by server (defense-in-depth must skip)
            {
                "id": "conv-rogue-external",
                "metadata": {"projectId": EXTERNAL_PROJECT_ID},
                "state": {"runState": "RUN_STATE_IDLE", "updateTime": "2026-11-20T00:00:00Z"},
            },
        ],
    ]

    list_calls = []
    delete_calls = []

    def list_page(address, marker, method, body, **kw):
        if method == "DeleteConversation":
            delete_calls.append(body["conversationId"])
            return (
                {"ok": False, "rejected": True}
                if body["conversationId"] == "conv-old-fail"
                else {"ok": True, "http_status": 200}
            )
        assert method == "ListConversations"
        list_calls.append(body)
        index = int(body.get("pageToken", 0))
        return {"ok": True, "body": {"conversations": pages[index], "nextPageToken": "1" if index == 0 else ""}}

    monkeypatch.setattr("agent_bridge.agy_slot_worker._call_rpc", list_page)
    env = {
        "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:12000",
        "ANTIGRAVITY_CSRF_TOKEN": secret_token,
        "ANTIGRAVITY_CONVERSATION_ID": self_conv_id,
    }
    allowed = [SLOT_B.project_id]

    # 1. External project_id refused with ZERO RPC calls
    rejected = run_cleanup_request(
        {
            "operation_id": str(uuid.uuid4()),
            "slot": "evil",
            "project_id": EXTERNAL_PROJECT_ID,
            "allowed_project_ids": allowed,
            "cutoff_utc": cutoff.isoformat(),
            "dry_run": False,
        },
        env=env,
    )
    assert rejected["ok"] is False
    assert list_calls == []

    # 2. dry_run=True lists both pages, skips non-eligible, executes ZERO deletes
    dry = run_cleanup_request(
        {
            "operation_id": str(uuid.uuid4()),
            "slot": SLOT_B.name,
            "project_id": SLOT_B.project_id,
            "allowed_project_ids": allowed,
            "cutoff_utc": cutoff.isoformat(),
            "dry_run": True,
        },
        env=env,
    )
    assert len(list_calls) == 2
    assert list_calls[0]["projectId"] == SLOT_B.project_id
    assert list_calls[0]["includeArchived"] is True
    assert dry["checked"] == 6
    assert dry["deleted"] == 0
    assert dry["would_delete"] == 2 and dry["would_delete_ids"] == dry["eligible_ids"]
    assert not delete_calls
    assert dry["eligible_ids"] == ["conv-old-idle-1", "conv-old-fail"]
    assert dry["skipped_running"] == 2
    assert dry["skipped_recent"] == 1
    assert len(dry["errors"]) == 1  # rogue external projectId flagged

    # Only same-project, old, idle non-self IDs are submitted; count confirmed successes only.
    live = run_cleanup_request(
        {
            "operation_id": str(uuid.uuid4()),
            "slot": SLOT_B.name,
            "project_id": SLOT_B.project_id,
            "allowed_project_ids": allowed,
            "cutoff_utc": cutoff.isoformat(),
            "dry_run": False,
        },
        env=env,
    )
    assert live["checked"] == 6 and len(list_calls) == 4
    assert live["errors"] == ["unexpected_project", "delete_rejected"]
    assert delete_calls == ["conv-old-idle-1", "conv-old-fail"]
    assert live["deleted"] == 1 and live["deleted_ids"] == ["conv-old-idle-1"]
    assert live["rejected_delete_ids"] == ["conv-old-fail"] and not live["unconfirmed_delete_ids"]
    assert live["ok"] is False
    assert secret_token not in json.dumps(live)


# ---------------------------------------------------------------------------
# 5. Tier 3 cleaner command builder & isolated subprocess execution
# ---------------------------------------------------------------------------


def test_tier3_cleaner_uses_scheme_a_and_minimal_allowlist(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agent_bridge.agy_slots.resolve_command", lambda cmd, fb=None: ["agy"])
    captured: dict[str, Any] = {}

    def fake_process(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]
        runner_dir = Path(kwargs["cwd"])
        captured["settings"] = json.loads((runner_dir / "minimal-settings.json").read_text(encoding="utf-8"))
        captured["request"] = json.loads((runner_dir / "request.json").read_text(encoding="utf-8"))
        captured["worker_exists"] = (runner_dir / "cleanup_worker.py").is_file()
        (runner_dir / "result.json").write_text(
            json.dumps(
                {
                    **{k: captured["request"][k] for k in ("operation_id", "slot", "project_id", "dry_run")},
                    **_batch_fields(captured["request"]),
                    "skipped_unverified": 0,
                    "rejected_delete_ids": [],
                    "unconfirmed_delete_ids": [],
                    "eligible_ids": ["c1", "c2"],
                    "ok": True,
                    "checked": 3,
                    "deleted": 0,
                    "would_delete": 2,
                    "deleted_ids": [],
                    "would_delete_ids": ["c1", "c2"],
                    "skipped_running": 1,
                    "skipped_recent": 0,
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(communicate=lambda **kw: (None, b""), returncode=0)

    monkeypatch.setattr("agent_bridge.agy_slots.subprocess.Popen", fake_process)
    agent = AgentConfig(name="antigravity", protocol="agy", command=["agy"], agy_slots=_three_slot_cfg())
    res = execute_tier3_cleanup(
        CleanerRequest(
            slot=SLOT_B.name,
            project_id=SLOT_B.project_id,
            allowed_project_ids=(SLOT_A.project_id, SLOT_B.project_id, SLOT_C.project_id),
            cutoff_utc=datetime(2026, 11, 26, 0, 0, tzinfo=UTC),
            dry_run=True,
        ),
        agent_cfg=agent,
        home=tmp_path,
    )
    assert res.ok is True
    assert res.checked == 3
    assert res.deleted == 0
    assert res.eligible_ids == ("c1", "c2")
    assert res.skipped_running == 1
    assert captured["worker_exists"] is True
    argv = captured["argv"]
    # Scheme A: cleaner runs inside the target slot project, never default-cli-project
    assert argv[argv.index("--project") + 1] == SLOT_B.project_id
    assert "--disable-slash-commands" in argv
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-low"
    assert captured["settings"] == build_tier3_settings(WORKER_COMMAND)
    assert captured["settings"]["allowNonWorkspaceAccess"] is False
    assert captured["settings"]["permissions"]["allow"] == [f"command({WORKER_COMMAND})"]
    assert captured["env"]["KEEP_SYNTHETIC_SETTING"] == "yes"
    assert not any(k.upper().startswith("ANTIGRAVITY_") for k in captured["env"])

    # Direct helper check
    cmd = build_tier3_cleaner_command(
        agy_cmd=["agy"],
        slot_cfg=_three_slot_cfg(),
        target_project_id=SLOT_B.project_id,
        runner_dir=tmp_path,
        settings_path=tmp_path / "s.json",
    )
    assert cmd[cmd.index("--project") + 1] == SLOT_B.project_id


# ---------------------------------------------------------------------------
# 6. Dashboard HTTP API: GET /api/agy/slots & POST /api/agy/slots/cleanup
# ---------------------------------------------------------------------------


def test_dashboard_agy_slots_api_auth_dry_run_confirm_and_live_cleanup(tmp_path: Path):
    _write_slot_overlay(tmp_path, enabled=True, grace_hours=24)
    projects_dir = tmp_path / "agy_projects"
    _write_projects(projects_dir)

    tz = timezone(timedelta(hours=8))
    fixed_now = datetime(2026, 11, 27, 14, 0, 0, tzinfo=tz)
    cfg = load_config(tmp_path).get("antigravity").agy_slots
    roles = _find_roles_for_now(cfg, fixed_now)
    rel_slot = roles["releasable"]
    act_slot = roles["active"]

    calls: list[CleanerRequest] = []

    def fake_cleaner(req: CleanerRequest, **_kw: Any) -> CleanerExecutionResult:
        calls.append(req)
        ids = ("c1", "c2")
        if not req.dry_run:
            return CleanerExecutionResult(ok=True, checked=2, eligible_ids=ids, deleted_ids=ids, deleted=2)
        return CleanerExecutionResult(
            ok=True,
            checked=4,
            eligible_ids=ids,
            would_delete=2,
            would_delete_ids=ids,
            skipped_running=1,
            skipped_recent=1,
        )

    atomic_write_json(state_path(tmp_path), {"sessions": [], "tasks": []})
    server = SimpleNamespace(
        home=tmp_path,
        host="127.0.0.1",
        csrf_token="unit-marker",
        bound_port=5000,
        agy_cleaner_runner=fake_cleaner,
        agy_projects_dir_override=projects_dir,
        agy_now_fn=lambda: fixed_now,
    )
    # 1. Host header guard on GET
    st, _ = _json_req(server, "GET", "/api/agy/slots", extra_headers={"Host": "evil.example"})
    assert st == 403

    # 2. GET /api/agy/slots returns full structured slot overview
    st, overview = _json_req(server, "GET", "/api/agy/slots")
    assert st == 200 and overview["ok"] is True
    assert overview["enabled"] is True
    assert overview["active_slot"] == act_slot.name
    assert overview["cleaner"]["available"] is True
    assert overview["last_cleanup"] is None
    by_name = {row["name"]: row for row in overview["slots"]}
    assert by_name[act_slot.name]["status"] == "active"
    assert by_name[rel_slot.name]["status"] == "releasable"
    assert by_name[SLOT_A.name]["project_file_exists"] is True
    assert by_name[SLOT_A.name]["project_is_empty"] is True

    # 3. POST security guards: missing CSRF & cross-origin Origin rejected
    st, _ = _json_req(server, "POST", "/api/agy/slots/cleanup", payload={"slot": rel_slot.name, "dry_run": True})
    assert st == 403
    st, _ = _json_req(
        server,
        "POST",
        "/api/agy/slots/cleanup",
        csrf=server.csrf_token,
        payload={"slot": rel_slot.name, "dry_run": True},
        extra_headers={"Origin": "http://evil.example"},
    )
    assert st == 403

    # 4. Input validation: non-boolean dry_run, missing/wrong confirm, unknown slot
    st, _ = _json_req(
        server,
        "POST",
        "/api/agy/slots/cleanup",
        csrf=server.csrf_token,
        payload={"slot": rel_slot.name, "dry_run": "false"},
    )
    assert st == 400

    st, _ = _json_req(
        server,
        "POST",
        "/api/agy/slots/cleanup",
        csrf=server.csrf_token,
        payload={"slot": rel_slot.name, "dry_run": False, "confirm": "wrong"},
    )
    assert st == 400

    st, _ = _json_req(
        server,
        "POST",
        "/api/agy/slots/cleanup",
        csrf=server.csrf_token,
        payload={"slot": "nonexistent", "dry_run": True},
    )
    assert st == 400

    # 5. Live cleanup on active slot blocked with 409 Conflict
    st, conflict = _json_req(
        server,
        "POST",
        "/api/agy/slots/cleanup",
        csrf=server.csrf_token,
        payload={"slot": act_slot.name, "dry_run": False, "confirm": act_slot.name},
    )
    assert st == 409
    assert conflict["ok"] is False
    assert conflict["slot"] == act_slot.name
    assert conflict["errors"]

    # 6. dry_run=True preview returns structured counts without deleting
    st, dry_resp = _json_req(
        server,
        "POST",
        "/api/agy/slots/cleanup",
        csrf=server.csrf_token,
        payload={"slot": rel_slot.name, "dry_run": True},
    )
    assert st == 200
    for key in (
        "ok",
        "slot",
        "project_id",
        "dry_run",
        "checked",
        "deleted",
        "skipped_running",
        "skipped_recent",
        "blocked_sessions",
        "errors",
    ):
        assert key in dry_resp
    assert dry_resp["ok"] is True
    assert dry_resp["dry_run"] is True
    assert dry_resp["checked"] == 4
    assert dry_resp["deleted"] == 0
    assert dry_resp["would_delete"] == 2 and dry_resp["would_delete_ids"] == dry_resp["eligible_ids"] == ["c1", "c2"]

    # 7. Confirmed live cleanup returns the runner's confirmed counts.
    st, live_resp = _json_req(
        server,
        "POST",
        "/api/agy/slots/cleanup",
        csrf=server.csrf_token,
        payload={"slot": rel_slot.name, "dry_run": False, "confirm": rel_slot.name},
    )
    assert st == 200 and live_resp["outcome"] == "completed"
    assert live_resp["deleted"] == 2 and live_resp["deleted_ids"] == ["c1", "c2"]
    assert len(calls) == 2

    # 8. Subsequent GET /api/agy/slots surfaces the latest cleanup receipt
    st, after_overview = _json_req(server, "GET", "/api/agy/slots")
    assert st == 200
    assert after_overview["last_cleanup"]["slot"] == rel_slot.name
    assert after_overview["last_cleanup"] == live_resp
    after_by_name = {row["name"]: row for row in after_overview["slots"]}
    assert after_by_name[rel_slot.name]["last_cleanup"] == live_resp


def test_dashboard_agy_slots_api_default_disabled(tmp_path: Path):
    atomic_write_json(state_path(tmp_path), {"sessions": [], "tasks": []})
    server = SimpleNamespace(
        home=tmp_path,
        agy_now_fn=None,
        agy_cleaner_runner=None,
        host="127.0.0.1",
        csrf_token="unit-marker",
        bound_port=5000,
        agy_projects_dir_override=tmp_path / "missing_projects",
    )
    st, payload = _json_req(server, "GET", "/api/agy/slots")
    assert st == 200
    assert payload["ok"] is True
    assert payload["enabled"] is False
    assert payload["active_slot"] is None
    assert payload["slots"] == []
    assert payload["cleaner"]["available"] is False
    assert payload["cleaner"]["live_cleanup_available"] is False

    st, err = _json_req(
        server,
        "POST",
        "/api/agy/slots/cleanup",
        csrf=server.csrf_token,
        payload={"slot": "slotA", "dry_run": True},
    )
    assert st == 400
    assert err["ok"] is False


def test_queued_task_blocks_slot_even_when_session_state_is_dead(tmp_path: Path):
    _write_slot_overlay(tmp_path, enabled=True, grace_hours=24)
    now = datetime(2026, 11, 27, 14, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    cfg = load_config(tmp_path).get("antigravity").agy_slots
    rel_slot = _find_roles_for_now(cfg, now)["releasable"]

    atomic_write_json(
        state_path(tmp_path),
        {
            "sessions": [
                {
                    "session_id": "sess_queued",
                    "agent": "antigravity",
                    "cwd": str(tmp_path),
                    "proc_state": ProcState.dead.value,
                    "agy_slot": rel_slot.name,
                    "agy_project_id": rel_slot.project_id,
                }
            ],
            "tasks": [
                {
                    "task_id": "task_queued_1",
                    "session_id": "sess_queued",
                    "agent": "antigravity",
                    "message": "pending",
                    "cwd": str(tmp_path),
                    "status": TaskStatus.queued.value,
                }
            ],
            "requests": [],
        },
    )
    desc = describe_slots(tmp_path, now=now, projects_dir=tmp_path / "missing", cleaner_available=True)
    by_name = {row["name"]: row for row in desc["slots"]}
    assert by_name[rel_slot.name]["status"] == "unknown"
    assert by_name[rel_slot.name]["blocked_sessions"][0]["reason"] == "active_task"


def test_tier3_cleaner_handles_timeout_and_missing_result_with_redaction(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agent_bridge.agy_slots.resolve_command", lambda cmd, fb=None: ["agy"])
    agent = AgentConfig(name="antigravity", protocol="agy", command=["agy"], agy_slots=_three_slot_cfg())
    req = CleanerRequest(
        slot=SLOT_B.name,
        project_id=SLOT_B.project_id,
        allowed_project_ids=(SLOT_A.project_id, SLOT_B.project_id, SLOT_C.project_id),
        cutoff_utc=datetime(2026, 11, 26, 0, 0, tzinfo=UTC),
        dry_run=True,
    )

    actions = []

    class TimedOutProcess:
        def communicate(self, **kwargs):
            actions.append("communicate")
            raise subprocess.TimeoutExpired("fixture", timeout=10)

        def poll(self):
            return None

        def kill(self):
            actions.append("kill")

        def wait(self, timeout):
            actions.append(("wait", timeout))
            raise subprocess.TimeoutExpired("fixture", timeout=timeout)

    monkeypatch.setattr("agent_bridge.agy_slots.subprocess.Popen", lambda *a, **kw: TimedOutProcess())
    timeout_res = execute_tier3_cleanup(req, agent_cfg=agent, home=tmp_path)
    assert timeout_res.outcome_unknown
    assert actions == ["communicate", "kill", ("wait", 1)]
    assert any("timed out" in e for e in timeout_res.errors)

    marker = b"synthetic-stderr-marker"
    raw_stderr = b"token=" + b"a" * 510 + marker + b" " + marker
    monkeypatch.setattr(
        "agent_bridge.agy_slots.subprocess.Popen",
        lambda *a, **kw: SimpleNamespace(communicate=lambda **kw: (None, raw_stderr), returncode=1),
    )
    duplicate = execute_tier3_cleanup(req, agent_cfg=agent, home=tmp_path)
    assert duplicate.outcome_unknown and duplicate.errors == ("cleanup operation already has a workspace",)
    other = replace(req, operation_id=str(uuid.uuid4()))
    missing_res = execute_tier3_cleanup(other, agent_cfg=agent, home=tmp_path)
    assert missing_res.outcome_unknown
    assert any("no valid result.json" in e for e in missing_res.errors)
    assert marker.decode() not in str(missing_res)


async def test_registry_persists_agy_slot_metadata_before_turn_completes(tmp_path: Path, monkeypatch):
    from agent_bridge.models import TurnResult
    from agent_bridge.registry import Registry

    _write_slot_overlay(tmp_path, enabled=True, grace_hours=24)
    monkeypatch.setattr("agent_bridge.adapters.antigravity.select_active_slot", lambda _cfg: SLOT_A)
    observed_mid_turn: list[tuple[str | None, str | None]] = []

    async def fake_run_turn(self: AgyAdapter, session: Session, task: Task) -> TurnResult:
        observed_mid_turn.append((session.agy_slot, session.agy_project_id))
        return TurnResult(text="ok", native_session_id="conv-mid-1")

    monkeypatch.setattr(AgyAdapter, "run_turn", fake_run_turn)

    reg = Registry(tmp_path, load_config(tmp_path), runtime_context="coordinator")
    await reg.start()
    try:
        dispatched = await reg.dispatch_task(
            agent="antigravity",
            message="hello slot",
            cwd=str(tmp_path),
            user_requested=True,
        )
        await reg.wait_task(dispatched["task_id"], timeout_sec=10)
        await reg.flush_state()
        assert observed_mid_turn == [(SLOT_A.name, SLOT_A.project_id)]
        disk = json.loads(state_path(tmp_path).read_text(encoding="utf-8"))
        sess_row = next(row for row in disk["sessions"] if row["session_id"] == dispatched["session_id"])
        assert sess_row["agy_slot"] == SLOT_A.name
        assert sess_row["agy_project_id"] == SLOT_A.project_id
        assert sess_row["native_session_id"] == "conv-mid-1"
    finally:
        await reg.stop()


FIXED_NOW = datetime(2026, 11, 27, 14, tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def enabled_home(tmp_path):
    _write_slot_overlay(tmp_path)
    atomic_write_json(state_path(tmp_path), {"sessions": [], "tasks": [], "requests": []})
    return tmp_path


def overview(home, now=FIXED_NOW):
    return describe_slots(home, now=now, projects_dir=agy_projects_dir(), cleaner_available=True)


def releasable_slot(home):
    cfg = load_config(home).get("antigravity").agy_slots
    return _find_roles_for_now(cfg, FIXED_NOW)["releasable"]


def run_slot(home, spec, runner, *, dry_run=True):
    return cleanup_agy_slot(
        home,
        spec.name,
        dry_run=dry_run,
        confirm=spec.name,
        now=FIXED_NOW,
        cleaner_runner=runner,
        projects_dir=agy_projects_dir(),
    )


def unexpected_runner(*args, **kwargs):
    pytest.fail("cleaner must not be invoked")


@pytest.mark.parametrize("fault", ("missing", "denied", "json", "root", "session", "task", "orphan", "duplicate"))
def test_unknown_bridge_state_is_never_treated_as_drained(enabled_home, fault, monkeypatch):
    home = enabled_home
    rel = releasable_slot(home)
    path = state_path(home)
    if fault == "missing":
        path.unlink()
    elif fault == "denied":
        read = agy_slots.read_json_strict

        def denied(p, default):
            if p == path:
                raise PermissionError
            return read(p, default)

        monkeypatch.setattr(agy_slots, "read_json_strict", denied)
    elif fault == "json":
        path.write_text("{")
    elif fault == "root":
        atomic_write_json(path, [])
    elif fault == "session":
        atomic_write_json(path, {"sessions": [{"proc_state": "unrecognized"}], "tasks": []})
    elif fault == "task":
        atomic_write_json(path, {"sessions": [], "tasks": [{"status": None}]})
    elif fault == "duplicate":
        row = Session(session_id="s", agent="antigravity", cwd=str(home)).model_dump(mode="json")
        atomic_write_json(path, {"sessions": [row, row], "tasks": []})
    else:
        task = Task(task_id="fixture", session_id="unattributed", agent="antigravity", cwd=str(home), message="")
        atomic_write_json(path, {"sessions": [], "tasks": [task.model_dump(mode="json")]})
    desc = overview(home)
    assert desc["state_error"] and all(
        r["status"] == "unknown" and r["bound_sessions_count"] is None for r in desc["slots"]
    )
    with pytest.raises(SlotCleanupConflictError) as conflict:
        run_slot(home, rel, unexpected_runner, dry_run=False)
    assert conflict.value.payload["outcome"] == "not_started"


@pytest.mark.parametrize(
    "resources,body_id,expected",
    [
        ({}, None, True),
        ({"resources": []}, None, True),
        (None, None, False),
        ("omit", None, False),
        ({"resources": [{"folderUri": "fixture"}]}, None, False),
        ({"resources": [], "unknownResources": [1]}, None, False),
        ({}, EXTERNAL_PROJECT_ID, False),
    ],
)
def test_project_evidence_required_for_dispatch_preview_and_live(
    enabled_home, monkeypatch, resources, body_id, expected
):
    home = enabled_home
    spec = releasable_slot(home)
    path = agy_projects_dir() / f"{spec.project_id}.json"
    doc = {"id": body_id or spec.project_id}
    if resources != "omit":
        doc["projectResources"] = resources
    atomic_write_json(path, doc)
    monkeypatch.setattr("agent_bridge.adapters.antigravity.select_active_slot", lambda cfg: spec)
    adapter = AgyAdapter(load_config(home).get("antigravity"), home)
    session = Session(session_id="s", agent="antigravity", cwd=str(home))
    row = next(r for r in overview(home)["slots"] if r["name"] == spec.name)
    if expected:
        adapter.prepare_turn(session)
        assert row["project_is_empty"] is True and session.agy_project_id == spec.project_id
        assert run_slot(home, spec, lambda *a, **kw: CleanerExecutionResult(True))["ok"]
    else:
        assert row["guard_error"] and row["status"] == "unknown"
        with pytest.raises(agy_slots.SlotGuardError):
            adapter.prepare_turn(session)
        for mode in (True, False):
            with pytest.raises(SlotCleanupConflictError):
                run_slot(home, spec, unexpected_runner, dry_run=mode)


def test_duplicate_normalized_project_and_registration_path_rejected():
    upper_id = "ABCDEF12-1111-4111-8111-111111111111"
    with pytest.raises(ValueError, match="duplicate agy slot project_id"):
        AgySlotConfig(
            enabled=True,
            slots=[AgySlotSpec(name="a", project_id=upper_id), AgySlotSpec(name="b", project_id=upper_id.lower())],
        )
    placeholder = AgySlotSpec(name="a", project_id="not-a-registration")
    assert AgySlotConfig(enabled=False, slots=[placeholder]).slots == [placeholder]
    with pytest.raises(ValueError, match="expected a UUID"):
        AgySlotConfig(enabled=True, slots=[placeholder, SLOT_A])


def test_worker_only_connects_to_selected_numeric_loopback(monkeypatch):
    calls = []
    replies = iter((307, 200))

    class MemoryConnection:
        def __init__(self, host, port, *, timeout):
            calls.append((host, port))

        def request(self, method, path, body, headers):
            assert method == "POST" and path == "/product.v1.ConversationService/ListConversations"
            assert headers["x-antigravity-csrf-token"] == "fixture-marker"

        def getresponse(self):
            return SimpleNamespace(status=next(replies), read=lambda: b"{}")

        def close(self):
            pass

    monkeypatch.setenv("HTTP_PROXY", "http://unused.invalid:1234")
    monkeypatch.setattr(agy_slot_worker.http.client, "HTTPConnection", MemoryConnection)
    first = agy_slot_worker._call_rpc("localhost:6001", "fixture-marker", "ListConversations", {})
    second = agy_slot_worker._call_rpc("[::1]:6002", "fixture-marker", "ListConversations", {})
    assert first == {"ok": False} and second["ok"]
    assert calls == [("127.0.0.1", 6001), ("::1", 6002)]


@pytest.mark.parametrize(
    "mode,own_id,error",
    [
        (False, "", "missing_identity"),
        ("false", "own", "invalid_mode"),
        (None, "own", "invalid_mode"),
        (0, "own", "invalid_mode"),
    ],
)
def test_invalid_worker_mode_or_identity_stops_before_rpc(monkeypatch, mode, own_id, error):
    monkeypatch.setattr(agy_slot_worker, "_call_rpc", unexpected_runner)
    result = run_cleanup_request(
        {
            "operation_id": str(uuid.uuid4()),
            "slot": SLOT_A.name,
            "project_id": SLOT_A.project_id,
            "allowed_project_ids": [SLOT_A.project_id],
            "dry_run": mode,
            "cutoff_utc": FIXED_NOW.isoformat(),
        },
        env={
            "ANTIGRAVITY_CONVERSATION_ID": own_id,
            "ANTIGRAVITY_CSRF_TOKEN": "marker",
            "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:12000",
        },
    )
    assert result["errors"] == [error] and result["deleted"] == 0


def test_grace_admission_uses_exact_boundary(enabled_home):
    home = enabled_home
    _write_slot_overlay(home, grace_hours=12)
    exact = FIXED_NOW.replace(hour=12)
    cfg = load_config(home).get("antigravity").agy_slots
    yesterday = _find_roles_for_now(cfg, exact)["cooling"]
    before = next(r for r in overview(home, exact - timedelta(seconds=1))["slots"] if r["name"] == yesterday.name)
    after = next(r for r in overview(home, exact)["slots"] if r["name"] == yesterday.name)
    assert before["status"] == "cooling" and after["status"] == "releasable"


def test_slot_becomes_active_before_admission(enabled_home, monkeypatch):
    from contextlib import contextmanager

    home = enabled_home
    spec = releasable_slot(home)
    clock = [FIXED_NOW.replace(hour=23, minute=59, second=59)]
    monkeypatch.setattr(agy_slots, "_ensure_aware", lambda now: now or clock[0])
    admit = agy_slots.slot_admission

    @contextmanager
    def crosses_midnight(*args, **kw):
        with admit(*args, **kw) as owner:
            clock[0] += timedelta(seconds=1)
            yield owner

    monkeypatch.setattr(agy_slots, "slot_admission", crosses_midnight)
    with pytest.raises(SlotCleanupConflictError) as conflict:
        cleanup_agy_slot(
            home,
            spec.name,
            dry_run=False,
            confirm=spec.name,
            projects_dir=agy_projects_dir(),
            cleaner_runner=unexpected_runner,
        )
    assert conflict.value.payload["slot_status"] == "active"


@pytest.mark.parametrize(
    "scenario", ["cli", "list_failure", "name_denied", "desktop", "none", "preview", "partial", "failed", "unknown"]
)
def test_production_cleanup_http_process_guard_and_service_receipts(enabled_home, monkeypatch, scenario):
    home = enabled_home
    spec = releasable_slot(home)
    cli_names = {501: r"C:\fixtures\AGY.EXE", 503: "agy.exe"}
    names = {502: "Antigravity.exe", 504: "agy.exe.helper", 505: "not-agy.exe", 506: None, 508: ""}
    if scenario in {"cli", "preview", "name_denied"}:
        names.update(cli_names)
    if scenario == "name_denied":
        names[507] = "denied"
    if scenario == "none":
        names.clear()
    calls, scans, launches = [], [], []

    def pids():
        scans.append(True)
        # The preflight runs inside the same project lock used by Bridge dispatch.
        with (
            pytest.raises(agy_slots.FileLockTimeout),
            agy_slots.file_lock(agy_slots._owner_path(spec).with_suffix(".lock"), timeout_sec=0),
        ):
            pass
        if scenario == "list_failure":
            raise OSError("synthetic process list failure")
        return list(reversed(names))

    def process(pid):
        def name():
            if names[pid] is None:
                raise agy_slots.psutil.NoSuchProcess(pid)
            if names[pid] == "denied":
                raise agy_slots.psutil.AccessDenied(pid)
            return names[pid]

        return SimpleNamespace(name=name)

    ps = agy_slots.psutil
    monkeypatch.setattr(
        agy_slots,
        "psutil",
        SimpleNamespace(
            pids=pids,
            Process=process,
            Error=ps.Error,
            NoSuchProcess=ps.NoSuchProcess,
            AccessDenied=ps.AccessDenied,
        ),
    )
    monkeypatch.setattr(agy_slots, "resolve_command", lambda *args: ["fixture-agy"])
    ids = [str(uuid.uuid4()) for _ in range(3)]
    _synthetic_cli_profile()

    def service_response(method, payload):
        if method == "ListConversations":
            assert payload["projectId"] == spec.project_id
            items = [
                {
                    "id": cid,
                    "metadata": {"projectId": spec.project_id},
                    "state": {"runState": "RUN_STATE_IDLE", "updateTime": "2000-01-01T00:00:00Z"},
                }
                for cid in [*ids, "fixture-control", ids[0], "changed"]
            ]
            items.append({**items[-1], "state": {"runState": "RUN_STATE_RUNNING"}})
            return 200, {"conversations": items}
        assert method == "DeleteConversation" and set(payload) == {"conversationId"}
        cid = payload["conversationId"]
        calls.append(cid)
        if scenario == "failed":
            return 403, {}
        if cid == ids[1] and scenario in {"partial", "unknown"}:
            if scenario == "unknown":
                raise TimeoutError("fixture acknowledgement unavailable")
            return 404, {}
        return 200, {}

    class MemoryService:
        def __init__(self, host, port, *, timeout):
            assert (host, port) == ("127.0.0.1", 43111)

        def request(self, method, path, body, headers):
            assert method == "POST" and path.startswith("/product.v1.ConversationService/")
            self.method = path.rsplit("/", 1)[1]
            self.payload = json.loads(body)

        def getresponse(self):
            status, body = service_response(self.method, self.payload)
            return SimpleNamespace(status=status, read=lambda: json.dumps(body).encode())

        def close(self):
            pass

    monkeypatch.setattr(agy_slot_worker.http.client, "HTTPConnection", MemoryService)

    def launch(argv, *, cwd, **kwargs):
        launches.append(argv)
        assert argv[argv.index("--project") + 1] == spec.project_id
        request = json.loads((Path(cwd) / "request.json").read_text())
        assert request["allowed_project_ids"] == [spec.project_id]
        output = run_cleanup_request(
            request,
            env={
                "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:43111",
                "ANTIGRAVITY_CSRF_TOKEN": "fixture-marker",
                "ANTIGRAVITY_CONVERSATION_ID": "fixture-control",
            },
        )
        atomic_write_json(Path(cwd) / "result.json", output)
        return SimpleNamespace(communicate=lambda **kw: (None, b""), returncode=0 if output["ok"] else 1)

    monkeypatch.setattr(agy_slots.subprocess, "Popen", launch)
    server = SimpleNamespace(
        home=home,
        bound_port=5001,
        csrf_token="fixture-csrf",
        agy_now_fn=lambda: FIXED_NOW,
        agy_projects_dir_override=agy_projects_dir(),
        agy_cleaner_runner=None,
    )
    state = _json_req(server, "GET", "/api/agy/slots")[1]
    assert state["cleaner"]["live_cleanup_available"] and state["cleaner"]["conditional_delete_available"] is False
    assert state["cleaner"]["live_block_reason"] is None and not scans
    dry_run = scenario == "preview"
    status, result = _json_req(
        server,
        "POST",
        "/api/agy/slots/cleanup",
        csrf=server.csrf_token,
        payload={"slot": spec.name, "dry_run": dry_run, "confirm": spec.name},
    )
    assert len(scans) == int(not dry_run) + int(bool(result.get("http_200_ids")) and agy_slots.sys.platform == "win32")
    if scenario in {"cli", "list_failure", "name_denied"}:
        assert status == 409 and result["outcome"] == "not_started" and not launches and not calls
        assert not result["receipt_saved"] and result["deleted"] == 0
        error = {
            "cli": "agy_cli_running",
            "list_failure": "agy_cli_process_list_failed",
            "name_denied": "agy_cli_process_name_unavailable",
        }[scenario]
        assert result["errors"] == [error]
        if scenario != "list_failure":
            assert result["blocking_processes"] == [{"pid": 501, "name": "AGY.EXE"}, {"pid": 503, "name": "agy.exe"}]
        assert _json_req(server, "GET", "/api/agy/slots")[1]["last_cleanup"] is None
        return
    assert len(launches) == 1 and result["eligible_ids"] == ids and result["skipped_running"] == 2
    assert result["counts_confirmed"] and result["receipt_saved"]
    assert result["would_delete_ids"] == (ids if dry_run else [])
    assert result["would_delete"] == (len(ids) if dry_run else 0)
    assert calls == ([] if dry_run else ids[:2] if scenario == "unknown" else ids)
    if scenario == "unknown":
        assert status == 502 and result["outcome"] == "unknown" and result["unconfirmed_delete_ids"] == ids[1:2]
        assert result["deleted_ids"] == ids[:1] and result["errors"] == ["delete_unconfirmed"]
        with pytest.raises(SlotCleanupConflictError):
            run_slot(home, spec, unexpected_runner)
    elif scenario == "partial":
        assert status == 200 and result["outcome"] == "partial" and result["rejected_delete_ids"] == ids[1:2]
        assert result["deleted_ids"] == [ids[0], ids[2]] and result["errors"] == ["delete_rejected"]
    elif scenario == "failed":
        assert status == 502 and result["outcome"] == "failed" and result["rejected_delete_ids"] == ids
        assert result["deleted_ids"] == [] and result["errors"] == ["delete_rejected"]
    else:
        assert status == 200 and result["outcome"] == "completed" and result["deleted_ids"] == ([] if dry_run else ids)
    assert result["deleted"] == len(result["deleted_ids"])
    assert _json_req(server, "GET", "/api/agy/slots")[1]["last_cleanup"] == result


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "different"),
        ("slot", "other"),
        ("project_id", SLOT_C.project_id),
        ("dry_run", 1),
        ("ok", "true"),
        ("checked", True),
        ("checked", -1),
        ("skipped_recent", 1),
        ("eligible_ids", ["same", "same"]),
        ("errors", ["unstructured-fixture-marker"]),
        ("would_delete", 0),
        ("deleted_ids", ["unexpected"]),
        ("batch_number", 2),
        ("cutoff_utc", "1999-01-01T00:00:00+00:00"),
        ("http_200_ids", ["unexpected"]),
        ("self_conversation_id", "fixture-id"),
        ("has_more", True),
        ("worker_elapsed_ms", -1),
    ],
)
def test_worker_receipt_must_match_operation_and_account_for_counts(field, value):
    request = CleanerRequest(SLOT_A.name, SLOT_A.project_id, (SLOT_A.project_id,), FIXED_NOW, True)
    raw = {
        **_batch_fields(request.identity),
        "operation_id": request.operation_id,
        "slot": SLOT_A.name,
        "project_id": SLOT_A.project_id,
        "dry_run": True,
        "ok": True,
        "checked": 1,
        "deleted": 0,
        "deleted_ids": [],
        "would_delete": 1,
        "would_delete_ids": ["fixture-id"],
        "rejected_delete_ids": [],
        "unconfirmed_delete_ids": [],
        "skipped_running": 0,
        "skipped_recent": 0,
        "skipped_unverified": 0,
        "eligible_ids": ["fixture-id"],
        "errors": [],
    }
    assert agy_slots.parse_worker_receipt(raw, request.identity).eligible_ids == ("fixture-id",)
    bad = agy_slots.parse_worker_receipt({**raw, field: value}, request.identity)
    assert not bad.ok and bad.outcome_unknown and "unstructured-fixture-marker" not in str(bad)


def test_two_slots_finishing_concurrently_preserve_both_receipts(enabled_home):
    from concurrent.futures import ThreadPoolExecutor

    home = enabled_home
    gate = threading.Barrier(2)
    finish_first = threading.Event()

    def runner(req, **kw):
        gate.wait(4)
        if req.slot == SLOT_B.name:
            assert finish_first.wait(4)
        return CleanerExecutionResult(True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run_slot, home, SLOT_A, runner)
        b = pool.submit(run_slot, home, SLOT_B, runner)
        a_result = a.result(6)
        finish_first.set()
        b_result = b.result(6)
    desc = overview(home)
    by_slot = {r["name"]: r["last_cleanup"] for r in desc["slots"]}
    assert by_slot[SLOT_A.name] == a_result and by_slot[SLOT_B.name] == b_result
    assert desc["last_cleanup"] == b_result


def test_unknown_execution_is_durable_and_not_retried_at_reconnect(enabled_home):
    home = enabled_home
    spec = releasable_slot(home)
    starts = []

    def unknown(req, **kw):
        starts.append(req.operation_id)
        return CleanerExecutionResult(False, errors=("cleaner timed out; execution unconfirmed",), outcome_unknown=True)

    server = SimpleNamespace(
        home=home,
        bound_port=5001,
        csrf_token="http-marker",
        agy_now_fn=lambda: FIXED_NOW,
        agy_projects_dir_override=agy_projects_dir(),
        agy_cleaner_runner=unknown,
    )
    path = "/api/agy/slots/cleanup"
    code, response = _json_req(server, "POST", path, csrf=server.csrf_token, payload={"slot": spec.name})
    assert (
        code == 502 and response["outcome"] == "unknown" and response["deleted"] is None and response["receipt_saved"]
    )
    code, desc = _json_req(server, "GET", "/api/agy/slots")
    row = next(r for r in desc["slots"] if r["name"] == spec.name)
    assert row["unconfirmed_operation_id"] == starts[0] and row["last_cleanup"] == response
    assert _json_req(server, "POST", path, csrf=server.csrf_token, payload={"slot": spec.name})[0] == 409
    assert len(starts) == 1


def test_corrupt_history_and_terminal_write_failure_are_visible(enabled_home, monkeypatch):
    home = enabled_home
    spec = releasable_slot(home)
    path = agy_slots.agy_slot_receipts_path(home)
    path.write_text("{")
    assert overview(home)["receipt_error"] and path.read_text() == "{"
    with pytest.raises(SlotCleanupConflictError):
        run_slot(home, spec, unexpected_runner)
    path.unlink()
    save = agy_slots._save_cleanup_receipt

    def persist(home, slot, receipt):
        if receipt["outcome"] != "unconfirmed":
            raise OSError("fixture storage error")
        save(home, slot, receipt)

    monkeypatch.setattr(agy_slots, "_save_cleanup_receipt", persist)
    result = run_slot(home, spec, lambda *a, **kw: CleanerExecutionResult(True, checked=1, skipped_running=1))
    assert result["outcome"] == "completed" and result["checked"] == 1 and not result["receipt_saved"]
    assert result["receipt_error"] and overview(home)["last_cleanup"]["outcome"] == "unconfirmed"
    with pytest.raises(SlotCleanupConflictError):
        run_slot(home, spec, unexpected_runner)


def test_other_home_and_lost_state_generation_cannot_reuse_slot(enabled_home):
    home = enabled_home
    spec = releasable_slot(home)
    result = run_slot(home, spec, lambda *a, **kw: CleanerExecutionResult(True))
    assert result["ok"]
    other = home / "other-home"
    _write_slot_overlay(other)
    atomic_write_json(state_path(other), {"sessions": [], "tasks": []})
    with pytest.raises(SlotCleanupConflictError, match="another Bridge home"):
        run_slot(other, spec, unexpected_runner)
    atomic_write_json(state_path(home), {"sessions": [], "tasks": []})
    with pytest.raises(SlotCleanupConflictError, match="generation"):
        run_slot(home, spec, unexpected_runner)


def test_exact_origin_only_for_slot_endpoint(enabled_home):
    home = enabled_home
    server = SimpleNamespace(
        home=home,
        bound_port=5001,
        csrf_token="marker",
        agy_now_fn=lambda: FIXED_NOW,
        agy_projects_dir_override=agy_projects_dir(),
        agy_cleaner_runner=unexpected_runner,
    )
    for origin in ("", "http://localhost:5001", "http://127.0.0.1:5002", "https://127.0.0.1:5001"):
        code, _ = _json_req(
            server,
            "POST",
            "/api/agy/slots/cleanup",
            csrf="marker",
            extra_headers={"Origin": origin},
            payload={"slot": SLOT_A.name},
        )
        assert code == 403
    # Generic mutation security still accepts clients without a browser Origin.
    code, _ = _json_req(server, "POST", "/not-an-endpoint", csrf="marker", extra_headers={"Origin": ""}, payload={})
    assert code == 404


async def test_queue_and_resume_admission_exclude_cleanup_in_both_orders(enabled_home, monkeypatch):
    from agent_bridge.models import TurnResult
    from agent_bridge.registry import Registry

    home = enabled_home
    spec = releasable_slot(home)
    monkeypatch.setattr("agent_bridge.adapters.antigravity.select_active_slot", lambda cfg: spec)
    # Separate Registry owners, with process liveness supplied entirely by the fixture.
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, stamp: pid in (11, 22))
    registries = [
        Registry(home, load_config(home), owner_pid=pid, owner_create_time=1, runtime_context="coordinator")
        for pid in (11, 22)
    ]
    a, b = registries
    for reg in registries:
        await reg.start()
    entered_queue, release_queue = threading.Event(), threading.Event()
    entered_turn, release_turn = asyncio.Event(), asyncio.Event()
    entered_clean, release_clean = threading.Event(), threading.Event()

    def synthetic_clean(req, **kw):
        entered_clean.set()
        assert release_clean.wait(5)
        return CleanerExecutionResult(True)

    write = a._write_state

    def pause_queue(own):
        if any(t["status"] == "queued" for t in own["tasks"]):
            entered_queue.set()
            assert release_queue.wait(5)
        write(own)

    monkeypatch.setattr(a, "_write_state", pause_queue)

    async def turn(self, session, task):
        entered_turn.set()
        await release_turn.wait()
        return TurnResult(text="fixture", native_session_id="fixture-native")

    monkeypatch.setattr(AgyAdapter, "run_turn", turn)

    async def dispatch(reg, session_id=None):
        return await reg.dispatch_task("antigravity", "fixture", str(home), session_id=session_id, user_requested=True)

    try:
        dispatching = asyncio.create_task(dispatch(a))
        assert await asyncio.to_thread(entered_queue.wait, 4)
        # The queued binding is not yet on disk. Its project lock still excludes cleanup.
        with pytest.raises(SlotCleanupConflictError):
            await asyncio.to_thread(run_slot, home, spec, synthetic_clean, dry_run=False)
        assert not entered_clean.is_set()
        release_queue.set()
        accepted = await dispatching
        await asyncio.wait_for(entered_turn.wait(), timeout=4)
        with pytest.raises(SlotCleanupConflictError) as busy:
            await asyncio.to_thread(run_slot, home, spec, synthetic_clean, dry_run=False)
        assert busy.value.payload["blocked_sessions"][0]["reason"] == "active_task"
        assert not entered_clean.is_set()
        release_turn.set()
        await a.wait_task(accepted["task_id"], timeout_sec=5)
        await a.end_session(accepted["session_id"])
        assert await a.flush_state()
        # Now the cleaner wins. A continuation in the first owner and a new
        # admission in a different Registry cannot start until it releases.
        cleaning = asyncio.create_task(asyncio.to_thread(run_slot, home, spec, synthetic_clean))
        assert await asyncio.to_thread(entered_clean.wait, 4)
        for reg, sid in ((a, accepted["session_id"]), (b, None)):
            with pytest.raises(RuntimeError, match="slot guard"):
                await dispatch(reg, sid)
        assert not b.sessions and len(a.tasks) == 1
        release_clean.set()
        assert (await cleaning)["outcome"] == "completed"
        resumed = await dispatch(a, accepted["session_id"])
        await a.wait_task(resumed["task_id"], timeout_sec=5)
        assert a.sessions[accepted["session_id"]].native_session_id == "fixture-native"
    finally:
        release_queue.set()
        release_turn.set()
        release_clean.set()
        for reg in registries:
            await reg.stop()


async def test_registry_never_runs_unpersisted_slot_turn(enabled_home, monkeypatch):
    from agent_bridge.registry import Registry

    home = enabled_home
    reg = Registry(home, load_config(home), runtime_context="coordinator")
    await reg.start()
    run = []

    async def never(self, session, task):
        run.append(task.task_id)
        pytest.fail("turn started with an uncommitted slot binding")

    monkeypatch.setattr(AgyAdapter, "run_turn", never)
    write = reg._write_state

    def reject_queued(own):
        if any(t["status"] == "queued" for t in own["tasks"]):
            raise OSError("fixture storage unavailable")
        write(own)

    monkeypatch.setattr(reg, "_write_state", reject_queued)
    try:
        ticket = await reg.dispatch_task("antigravity", "fixture", str(home), user_requested=True)
        assert ticket["persisted"] is False
        task = reg.tasks[ticket["task_id"]]
        assert task.status == TaskStatus.failed and "not persisted" in task.error
        assert not run
    finally:
        await reg.stop()


async def test_pre_spawn_failure_retries_next_day_without_stale_slot(enabled_home, monkeypatch):
    from agent_bridge.models import TurnResult
    from agent_bridge.registry import Registry

    home = enabled_home
    chosen = [SLOT_A]
    monkeypatch.setattr("agent_bridge.adapters.antigravity.select_active_slot", lambda cfg: chosen[0])

    def command_missing(*a):
        raise FileNotFoundError("fixture CLI missing")

    monkeypatch.setattr("agent_bridge.adapters.antigravity.resolve_command", command_missing)
    reg = Registry(home, load_config(home), runtime_context="coordinator")
    await reg.start()
    try:
        first = await reg.dispatch_task("antigravity", "fixture", str(home), user_requested=True)
        await reg.wait_task(first["task_id"], timeout_sec=5)
        session = reg.sessions[first["session_id"]]
        assert reg.tasks[first["task_id"]].status == TaskStatus.failed
        assert session.native_session_id is None and session.agy_project_id == SLOT_A.project_id
        chosen[0] = SLOT_B
        monkeypatch.setattr("agent_bridge.adapters.antigravity.resolve_command", lambda *a: ["fixture"])
        commands = []

        async def command_only(self, session, task):
            commands.append(self._build_cmd(session, task))
            return TurnResult(text="fixture", native_session_id="fixture-native")

        monkeypatch.setattr(AgyAdapter, "run_turn", command_only)
        second = await reg.dispatch_task(
            "antigravity", "fixture", str(home), session_id=session.session_id, user_requested=True
        )
        await reg.wait_task(second["task_id"], timeout_sec=5)
        (cmd,) = commands
        assert cmd[cmd.index("--project") + 1] == SLOT_B.project_id == session.agy_project_id
    finally:
        await reg.stop()


async def test_old_idle_slot_binding_survives_restart_until_end(enabled_home):
    from agent_bridge.registry import Registry

    home = enabled_home
    spec = releasable_slot(home)
    row = Session(
        session_id="old-slot",
        agent="antigravity",
        cwd=str(home),
        agy_slot=spec.name,
        agy_project_id=spec.project_id,
        proc_state=ProcState.idle_unloaded,
        last_active_at="2000-01-01T00:00:00+00:00",
    ).model_dump(mode="json")
    atomic_write_json(state_path(home), {"sessions": [row], "tasks": []})
    for _ in range(2):
        reg = Registry(home, load_config(home), runtime_context="coordinator")
        await reg.start()
        assert row["session_id"] in reg.sessions
        assert next(r for r in overview(home)["slots"] if r["name"] == spec.name)["status"] == "blocked"
        await reg.stop()
    await reg.end_session(row["session_id"])
    assert await reg.flush_state()
    assert next(r for r in overview(home)["slots"] if r["name"] == spec.name)["status"] == "releasable"


@pytest.fixture
async def bound_native_turn(enabled_home, monkeypatch):
    from agent_bridge.models import TurnResult
    from agent_bridge.registry import Registry

    home = enabled_home
    commands = []
    monkeypatch.setattr("agent_bridge.adapters.antigravity.select_active_slot", lambda cfg: SLOT_A)
    monkeypatch.setattr("agent_bridge.adapters.antigravity.resolve_command", lambda *args: ["fixture-agy"])

    async def run_turn(self, session, task):
        commands.append(self._build_cmd(session, task))
        return TurnResult(text="fixture", native_session_id="fixture-original-conversation")

    monkeypatch.setattr(AgyAdapter, "run_turn", run_turn)
    reg = Registry(home, load_config(home), runtime_context="coordinator")
    await reg.start()
    try:
        first = await reg.dispatch_task("antigravity", "first", str(home), user_requested=True)
        await reg.wait_task(first["task_id"], timeout_sec=5)
        assert reg.tasks[first["task_id"]].status == TaskStatus.completed
        (argv,) = commands
        assert argv[argv.index("--project") + 1] == SLOT_A.project_id
    finally:
        await reg.stop()
    return home, first, commands


@pytest.mark.parametrize("remove_section", [False, True])
async def test_bound_native_turn_resumes_after_opt_out_and_registration_loss(bound_native_turn, remove_section):
    from agent_bridge.registry import Registry

    home, first, commands = bound_native_turn
    assert run_slot(home, SLOT_A, lambda *a, **kw: CleanerExecutionResult(True))["ok"]
    receipt_path, owner_path = agy_slots.agy_slot_receipts_path(home), agy_slots._owner_path(SLOT_A)
    history, owner = receipt_path.read_bytes(), owner_path.read_bytes()
    if remove_section:
        (home / "agents.toml").write_text("", encoding="utf-8")
    else:
        _write_slot_overlay(home, enabled=False)
    registration = agy_projects_dir() / f"{SLOT_A.project_id}.json"
    registration.unlink()
    reg = Registry(home, load_config(home), runtime_context="coordinator")
    await reg.start()
    try:
        resumed = await reg.dispatch_task(
            "antigravity", "follow-up", str(home), session_id=first["session_id"], user_requested=True
        )
        await reg.wait_task(resumed["task_id"], timeout_sec=5)
        assert resumed["persisted"] and reg.tasks[resumed["task_id"]].status == TaskStatus.completed
        assert reg.tasks[first["task_id"]].status == TaskStatus.completed
        _, argv = commands
        assert argv[argv.index("--conversation") + 1] == "fixture-original-conversation"
        assert "--project" not in argv and "--new-project" not in argv
        assert await reg.flush_state()
        (saved,) = json.loads(state_path(home).read_text(encoding="utf-8"))["sessions"]
        assert saved["session_id"] == first["session_id"]
        assert saved["native_session_id"] == "fixture-original-conversation"
        assert (saved["agy_slot"], saved["agy_project_id"]) == (SLOT_A.name, SLOT_A.project_id)
        assert receipt_path.read_bytes() == history and owner_path.read_bytes() == owner
        assert not registration.exists()
    finally:
        await reg.stop()


@pytest.mark.parametrize("conflict", ["in_progress", "unknown", "identity_mismatch"])
async def test_opted_out_native_turn_preserves_conflict_guards(bound_native_turn, conflict):
    from agent_bridge.registry import Registry

    home, first, commands = bound_native_turn
    entered, release = threading.Event(), threading.Event()

    def runner(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return CleanerExecutionResult(False, outcome_unknown=True)

    cleaning = None
    if conflict != "identity_mismatch":
        cleaning = asyncio.create_task(asyncio.to_thread(run_slot, home, SLOT_A, runner))
        assert await asyncio.to_thread(entered.wait, 4)
        if conflict == "unknown":
            release.set()
            assert (await cleaning)["outcome"] == "unknown"
    _write_slot_overlay(home, enabled=False)
    registration = agy_projects_dir() / f"{SLOT_A.project_id}.json"
    if conflict == "identity_mismatch":
        atomic_write_json(registration, {"id": SLOT_B.project_id, "projectResources": {}})
        error = "identity differs"
    else:
        registration.unlink()
        error = "slot guard is held" if conflict == "in_progress" else "no confirmed end"
    owner = agy_slots._owner_path(SLOT_A)
    before = owner.read_bytes()
    reg = Registry(home, load_config(home), runtime_context="coordinator")
    await reg.start()
    try:
        with pytest.raises(agy_slots.SlotGuardError, match=error):
            await reg.dispatch_task(
                "antigravity", "follow-up", str(home), session_id=first["session_id"], user_requested=True
            )
        assert list(reg.tasks) == [first["task_id"]] and len(commands) == 1
        assert owner.read_bytes() == before
    finally:
        release.set()
        if cleaning is not None:
            await cleaning
        await reg.stop()


@pytest.mark.parametrize("old_unconfirmed", [False, True])
def test_reassigned_slot_only_shows_its_own_latest_receipt(enabled_home, old_unconfirmed):
    home = enabled_home
    original = SLOT_A
    replacement = AgySlotSpec(name=original.name, project_id=EXTERNAL_PROJECT_ID)
    atomic_write_json(
        agy_projects_dir() / f"{replacement.project_id}.json", {"id": replacement.project_id, "projectResources": {}}
    )
    server = SimpleNamespace(
        home=home,
        bound_port=5001,
        agy_now_fn=lambda: FIXED_NOW,
        agy_projects_dir_override=agy_projects_dir(),
        agy_cleaner_runner=lambda *a, **kw: CleanerExecutionResult(True),
    )

    def get_receipts():
        status, body = _json_req(server, "GET", "/api/agy/slots")
        assert status == 200 and body["receipt_error"] is None
        row = next(row for row in body["slots"] if row["name"] == original.name)
        return row["project_id"], row["last_cleanup"], body["last_cleanup"]

    def inventory(request, **kwargs):
        candidate = "old-candidate" if request.project_id == original.project_id else "new-candidate"
        return CleanerExecutionResult(
            True, checked=1, would_delete=1, would_delete_ids=(candidate,), eligible_ids=(candidate,)
        )

    def unconfirmed(*args, **kwargs):
        return CleanerExecutionResult(False, outcome_unknown=True)

    old = run_slot(home, original, unconfirmed if old_unconfirmed else inventory)
    path = agy_slots.agy_slot_receipts_path(home)
    history = path.read_bytes()
    assert get_receipts() == (original.project_id, old, old)
    config = home / "agents.toml"
    original_config = config.read_text(encoding="utf-8")
    config.write_text(original_config.replace(original.project_id, replacement.project_id), encoding="utf-8")
    assert get_receipts() == (replacement.project_id, None, old)
    assert path.read_bytes() == history
    new = run_slot(home, replacement, inventory)
    assert get_receipts() == (replacement.project_id, new, new)
    config.write_text(original_config, encoding="utf-8")
    assert get_receipts() == (original.project_id, old, new)
    if old_unconfirmed:
        stored = agy_slots._load_cleanup_receipts(home)
        assert agy_slots._unconfirmed_operation(stored, original.project_id) == old["operation_id"]
        with pytest.raises(SlotCleanupConflictError, match="no confirmed end"):
            run_slot(home, original, unexpected_runner)
        assert get_receipts() == (original.project_id, old, new)
        return
    latest = run_slot(home, original, inventory)
    assert latest["operation_id"] != old["operation_id"]
    assert get_receipts() == (original.project_id, latest, latest)
    config.write_text(original_config.replace(original.project_id, replacement.project_id), encoding="utf-8")
    assert get_receipts() == (replacement.project_id, new, latest)


def test_interrupted_dashboard_keeps_unconfirmed_operation_across_restart(enabled_home):
    home = enabled_home
    spec = releasable_slot(home)

    class SimulatedStop(BaseException):
        pass

    def interrupted(*a, **kw):
        raise SimulatedStop()

    with pytest.raises(SimulatedStop):
        run_slot(home, spec, interrupted)
    row = next(r for r in overview(home)["slots"] if r["name"] == spec.name)
    assert row["unconfirmed_operation_id"] == row["last_cleanup"]["operation_id"]
    assert row["last_cleanup"]["outcome"] == "unconfirmed" and row["last_cleanup"]["deleted"] is None
    with pytest.raises(SlotCleanupConflictError):
        run_slot(home, spec, unexpected_runner)


def test_no_raw_cli_output_in_http_history_or_logs(enabled_home, monkeypatch, caplog):
    home = enabled_home
    spec = releasable_slot(home)
    marker = "synthetic-unlabelled-value"
    stderr = ("token=" + "a" * 510 + marker + "\n" + marker).encode()
    monkeypatch.setattr(agy_slots, "resolve_command", lambda *a: ["fixture"])
    monkeypatch.setattr(
        agy_slots.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(communicate=lambda **kw: (None, stderr), returncode=7),
    )
    server = SimpleNamespace(
        home=home,
        bound_port=5001,
        csrf_token="unit",
        agy_now_fn=lambda: FIXED_NOW,
        agy_projects_dir_override=agy_projects_dir(),
        agy_cleaner_runner=None,
    )
    code, response = _json_req(server, "POST", "/api/agy/slots/cleanup", csrf="unit", payload={"slot": spec.name})
    assert code == 502 and response["outcome"] == "unknown"
    assert "agy exit 7" in response["errors"][0] and "stderr present=True" in response["errors"][0]
    assert marker not in json.dumps(response)
    assert marker not in agy_slots.agy_slot_receipts_path(home).read_text() and marker not in caplog.text


def test_verified_partial_inventory_preserves_ids_and_safe_error(monkeypatch):
    replies = iter(
        [
            {
                "ok": True,
                "body": {
                    "conversations": [
                        {
                            "id": "fixture-eligible",
                            "metadata": {"projectId": SLOT_A.project_id},
                            "state": {"runState": "RUN_STATE_IDLE", "updateTime": "2000-01-01T00:00:00Z"},
                        }
                    ],
                    "nextPageToken": "second",
                },
            },
            {"ok": False},
        ]
    )
    monkeypatch.setattr(agy_slot_worker, "_call_rpc", lambda *a, **kw: next(replies))
    request = CleanerRequest(SLOT_A.name, SLOT_A.project_id, (SLOT_A.project_id,), FIXED_NOW, True)
    raw = run_cleanup_request(
        {
            "operation_id": request.operation_id,
            "slot": SLOT_A.name,
            "project_id": SLOT_A.project_id,
            "dry_run": True,
            "allowed_project_ids": [SLOT_A.project_id],
            "cutoff_utc": FIXED_NOW.isoformat(),
        },
        env={
            "ANTIGRAVITY_CSRF_TOKEN": "unit-marker",
            "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:10000",
            "ANTIGRAVITY_CONVERSATION_ID": "unit-own",
        },
    )
    partial = agy_slots.parse_worker_receipt(raw, request.identity)
    assert not partial.ok and not partial.outcome_unknown and partial.checked == 1
    assert partial.eligible_ids == ("fixture-eligible",) and partial.errors == ("list_failed",)


def test_valid_worker_counts_survive_unconfirmed_process_timeout(enabled_home, monkeypatch):
    home = enabled_home
    spec = releasable_slot(home)
    monkeypatch.setattr(agy_slots, "resolve_command", lambda *a: ["fixture"])

    def fake_process(argv, *, cwd, **kwargs):
        request = json.loads((Path(cwd) / "request.json").read_text())

        def timeout(**kwargs):
            assert request["allowed_project_ids"] == [spec.project_id]
            raw = {k: request[k] for k in ("operation_id", "slot", "project_id", "dry_run")}
            atomic_write_json(
                Path(cwd) / "result.json",
                {
                    **raw,
                    **_batch_fields(request),
                    "ok": True,
                    "checked": 1,
                    "deleted": 0,
                    "deleted_ids": [],
                    "would_delete": 1,
                    "would_delete_ids": ["verified-id"],
                    "rejected_delete_ids": [],
                    "unconfirmed_delete_ids": [],
                    "skipped_recent": 0,
                    "skipped_running": 0,
                    "skipped_unverified": 0,
                    "eligible_ids": ["verified-id"],
                    "errors": [],
                },
            )
            raise subprocess.TimeoutExpired("fixture", timeout=1)

        return SimpleNamespace(communicate=timeout, poll=lambda: 0)

    monkeypatch.setattr(agy_slots.subprocess, "Popen", fake_process)
    result = run_slot(home, spec, None)
    assert result["outcome"] == "unknown" and result["counts_confirmed"] is True
    assert result["checked"] == 1 and result["eligible_ids"] == ["verified-id"]
    with pytest.raises(SlotCleanupConflictError):
        run_slot(home, spec, unexpected_runner)


@pytest.mark.parametrize("prior", ["null", "artifacts", "quarantine"])
async def test_registry_cannot_reinitialize_lost_state_as_empty(tmp_path, prior):
    from agent_bridge.registry import Registry

    home = tmp_path
    _write_slot_overlay(home)
    path = state_path(home)
    if prior == "null":
        path.write_text("null")
    elif prior == "artifacts":
        (home / "receipts" / "previous.json").write_text("{}")
    else:
        (home / "state.json.corrupt-fixture").write_text("{")
    reg = Registry(home, load_config(home), runtime_context="coordinator")
    with pytest.raises(agy_slots.SlotGuardError):
        await reg.start()
    assert not path.exists() or path.read_text() == "null"


def test_legacy_receipt_safe_projection_preserves_original(enabled_home):
    home = enabled_home
    marker = "legacy-fixture-output"
    row = {
        "slot": SLOT_A.name,
        "project_id": SLOT_A.project_id,
        "ok": False,
        "dry_run": True,
        "errors": [marker],
        "checked": 0,
        "deleted": 0,
    }
    path = agy_slots.agy_slot_receipts_path(home)
    atomic_write_json(path, {"last_cleanup": row, "by_slot": {SLOT_A.name: row}})
    before = path.read_bytes()
    desc = overview(home)
    assert desc["last_cleanup"]["outcome"] == "historical_unverified" and marker not in json.dumps(desc)
    assert path.read_bytes() == before


def test_worker_repeated_command_does_not_replace_first_receipt(tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    atomic_write_json(request, {"dry_run": False})
    calls = []

    def fake_worker(obj):
        calls.append(obj)
        return {"ok": True}

    monkeypatch.setattr(agy_slot_worker, "run_cleanup_request", fake_worker)
    args = [str(request), str(result)]
    assert agy_slot_worker.main(args) == 0
    before = result.read_bytes()
    request.write_text("{")
    assert agy_slot_worker.main(args) == 1
    assert result.read_bytes() == before and len(calls) == 1


def test_missing_origin_and_default_port_origin_semantics(enabled_home):
    home = enabled_home
    server = SimpleNamespace(
        home=home,
        bound_port=80,
        csrf_token="unit",
        agy_now_fn=lambda: FIXED_NOW,
        agy_projects_dir_override=agy_projects_dir(),
        agy_cleaner_runner=unexpected_runner,
    )
    code, _ = _json_req(
        server,
        "POST",
        "/api/agy/slots/cleanup",
        csrf="unit",
        payload={"slot": "unknown"},
        extra_headers={"Host": "localhost:80", "Origin": "http://localhost"},
    )
    assert code == 400  # Reached slot-name validation after accepting the effective origin.


async def test_independent_registries_can_queue_concurrent_new_turns_into_active_slot(enabled_home, monkeypatch):
    from agent_bridge.models import TurnResult
    from agent_bridge.registry import Registry

    home = enabled_home
    monkeypatch.setattr("agent_bridge.adapters.antigravity.select_active_slot", lambda cfg: SLOT_A)
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, stamp: pid in (11, 22))
    a, b = (
        Registry(home, load_config(home), owner_pid=pid, owner_create_time=1, runtime_context="coordinator")
        for pid in (11, 22)
    )

    async def turn(self, session, task):
        return TurnResult(text="fixture", native_session_id=f"native-{task.task_id}")

    monkeypatch.setattr(AgyAdapter, "run_turn", turn)
    await a.start()
    await b.start()
    try:
        work = [reg.dispatch_task("antigravity", "fixture", str(home), user_requested=True) for reg in (a, b)]
        tickets = await asyncio.gather(*work)
        for reg, ticket in zip((a, b), tickets, strict=True):
            assert ticket["persisted"]
            assert (await reg.wait_task(ticket["task_id"], timeout_sec=5))["status"] == "completed"
        state = json.loads(state_path(home).read_text())
        assert len(state["sessions"]) == 2 and {s["agy_project_id"] for s in state["sessions"]} == {SLOT_A.project_id}
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.parametrize("old_preview", [False, True])
def test_late_matching_receipt_updates_visible_counts_without_releasing_slot(enabled_home, old_preview):
    home = enabled_home
    spec = releasable_slot(home)
    result = run_slot(
        home,
        spec,
        lambda *a, **kw: CleanerExecutionResult(
            False, outcome_unknown=True, errors=("cleaner completion unconfirmed",)
        ),
    )
    assert not result["counts_confirmed"]
    path = agy_slots.agy_slots_dir(home) / "runs" / result["operation_id"] / "result.json"
    identity = {k: result[k] for k in ("operation_id", "slot", "project_id", "dry_run")}
    raw = {
        **identity,
        "ok": True,
        "checked": 1,
        "deleted": 0,
        "would_delete": 1,
        "deleted_ids": [],
        "would_delete_ids": ["late-id"],
        "rejected_delete_ids": [],
        "unconfirmed_delete_ids": [],
        "eligible_ids": ["late-id"],
        "skipped_running": 0,
        "skipped_recent": 0,
        "skipped_unverified": 0,
        "errors": [],
    }
    if old_preview:
        # R2 pending inventory and its still-running worker predate live receipts.
        path_store = agy_slots.agy_slot_receipts_path(home)
        store = json.loads(path_store.read_text(encoding="utf-8"))
        for row in (raw, store["last_cleanup"], store["by_slot"][spec.name]):
            row.pop("rejected_delete_ids")
            row.pop("unconfirmed_delete_ids")
        raw.update(would_delete=0, would_delete_ids=[], skipped_unverified=1)
        atomic_write_json(path_store, store)
    atomic_write_json(path, {**raw, "operation_id": str(uuid.uuid4())})
    assert not overview(home)["last_cleanup"]["counts_confirmed"]
    atomic_write_json(path, raw)
    desc = overview(home)
    row = next(r for r in desc["slots"] if r["name"] == spec.name)
    assert desc["last_cleanup"]["late_worker_receipt"] and desc["last_cleanup"]["checked"] == 1
    assert row["status"] == "unknown" and row["unconfirmed_operation_id"] == identity["operation_id"]
    with pytest.raises(SlotCleanupConflictError):
        run_slot(home, spec, unexpected_runner)


@pytest.mark.parametrize(
    "field,value", [("outcome", ["invalid-fixture-value"]), ("deleted_ids", ["invalid-fixture-value"])]
)
def test_malformed_persisted_operation_is_visible_without_echoing_details(enabled_home, field, value):
    home = enabled_home
    spec = releasable_slot(home)
    row = run_slot(home, spec, lambda *a, **kw: CleanerExecutionResult(True))
    row[field] = value
    atomic_write_json(agy_slots.agy_slot_receipts_path(home), {"by_slot": {spec.name: row}, "last_cleanup": row})
    desc = overview(home)
    assert desc["receipt_error"] and "invalid-fixture-value" not in json.dumps(desc)
    with pytest.raises(SlotCleanupConflictError):
        run_slot(home, spec, unexpected_runner)


@pytest.mark.parametrize(
    "scenario,count,expected_calls,expected_batches,stop",
    [
        ("windows", 53, 53, 2, "completed"),
        ("cap", 501, 500, 10, "batch_limit"),
        ("reject", 87, 50, 1, "no_progress"),
        ("mixed", 53, 53, 2, "worker_failed"),
        ("changed", 53, 50, 2, "no_progress"),
        ("busy", 53, 50, 1, "admission_changed"),
        ("midnight", 53, 50, 1, "admission_changed"),
        ("unknown", 53, 51, 2, "unknown"),
        ("late", 53, 50, 2, "unknown"),
        ("preview", 84, 0, 1, "preview"),
    ],
)
def test_bounded_cleanup_through_production_runner(
    enabled_home, monkeypatch, scenario, count, expected_calls, expected_batches, stop
):
    home = enabled_home
    spec = releasable_slot(home)
    windows = scenario in {"windows", "preview"}
    monkeypatch.setattr(agy_slots, "_WINDOWS_RESIDUALS", windows)
    profile = _synthetic_cli_profile()
    ids = [str(uuid.UUID(int=i + 1)) for i in range(count)]
    # ids[0] represents an old, independently eligible control conversation.
    artifact = profile / "conversations" / f"{ids[0]}.db"
    artifact.write_bytes(b"fixture old conversation")
    new_controls, calls, requests, workspaces = [], [], [], []
    monkeypatch.setattr(agy_slots, "resolve_command", lambda *args: ["fixture-agy"])

    def processes():
        if scenario == "busy" and calls:
            return [{"pid": 17, "name": "agy.exe"}], True
        return [], True

    monkeypatch.setattr(agy_slots, "_running_agy_cli_processes", processes)
    if scenario == "midnight":
        schedule = agy_slots.compute_slot_schedule
        cfg = load_config(home).get("antigravity").agy_slots
        active_day = next(
            t for i in range(1, 4) if select_active_slot(cfg, now=(t := FIXED_NOW + timedelta(days=i))) == spec
        )
        monkeypatch.setattr(
            agy_slots, "compute_slot_schedule", lambda cfg, now=None: schedule(cfg, now=active_day if calls else now)
        )

    def rpc(address, token, method, payload, **kwargs):
        if method == "ListConversations":
            # The service can repeat previously acknowledged items and all controls on every listing.
            # The first inventory freezes membership; each batch still rechecks age and activity.
            return {
                "ok": True,
                "body": {
                    "conversations": [
                        {
                            "id": cid,
                            "metadata": {"projectId": spec.project_id},
                            "state": {
                                "runState": "RUN_STATE_RUNNING"
                                if scenario == "changed" and len(requests) == 2
                                else "RUN_STATE_IDLE",
                                "updateTime": "2000-01-01T00:00:00Z",
                            },
                        }
                        for cid in [*ids, *new_controls]
                    ]
                },
            }
        assert method == "DeleteConversation"
        cid = payload["conversationId"]
        assert cid in ids and cid not in calls and cid not in new_controls
        calls.append(cid)
        if scenario == "reject" or (scenario == "mixed" and len(calls) == 2):
            return {"ok": False, "rejected": True}
        if scenario == "unknown" and len(requests) == 2:
            return {"ok": False}
        return {"ok": True, "http_status": 200}

    monkeypatch.setattr(agy_slot_worker, "_call_rpc", rpc)

    def launch(argv, *, cwd, **kwargs):
        directory = Path(cwd)
        req = json.loads((directory / "request.json").read_text())
        requests.append(req)
        workspaces.append(directory)
        number = len(requests)
        assert req["batch_number"] == number and req["project_id"] == spec.project_id
        assert req["operation_id"] == requests[0]["operation_id"] and req["cutoff_utc"] == requests[0]["cutoff_utc"]
        assert req["target_ids"] == (None if number == 1 else ids[(number - 1) * 50 :])
        if scenario == "windows" and number > 1:
            assert not artifact.exists()
        new_controls.append(str(uuid.uuid4()))
        if scenario != "late" or number == 1:
            result = run_cleanup_request(
                req,
                env={
                    "ANTIGRAVITY_CSRF_TOKEN": "synth-marker",
                    "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:41001",
                    "ANTIGRAVITY_CONVERSATION_ID": new_controls[-1],
                },
            )
            atomic_write_json(directory / "result.json", result)
        return SimpleNamespace(communicate=lambda **kw: (None, b""), returncode=0)

    monkeypatch.setattr(agy_slots.subprocess, "Popen", launch)
    result = run_slot(home, spec, None, dry_run=scenario == "preview")
    assert result["receipt_saved"] and result["counts_confirmed"]
    assert result["stop_reason"] == stop and len(result["batches"]) == len(set(workspaces)) == expected_batches
    assert len(calls) == expected_calls and all(
        len(b["http_200_ids"] + b["unconfirmed_delete_ids"] + b["rejected_delete_ids"]) <= 50 for b in result["batches"]
    )
    assert result["http_200_ids"] == result["deleted_ids"]
    assert result["worker_elapsed_ms"] == sum(b["worker_elapsed_ms"] or 0 for b in result["batches"])
    assert not set(new_controls).intersection(result["eligible_ids"])
    assert result["eligible_ids"] == ids and result["checked"] == count + 1
    assert overview(home)["last_cleanup"] == result
    if scenario == "preview":
        assert result["would_delete_ids"] == ids and result["deleted"] == result["windows_residual_reclaimed"] == 0
        assert artifact.read_bytes() == b"fixture old conversation"
        assert not (Path.home() / ".agent-bridge" / "agy_slot_quarantine").exists()
    elif scenario == "windows":
        assert result["ok"] and result["has_more"] is False and result["remaining_ids"] == []
        assert (
            result["native_removed"] == 52
            and result["windows_residual_reclaimed"] == 1
            and result["residual_locked"] == 0
        )
        assert result["batches"][0]["has_more"] and not result["batches"][1]["has_more"]
    else:
        assert artifact.read_bytes() == b"fixture old conversation"  # The non-Windows path never opens the profile.
        assert all(not b["residuals"] for b in result["batches"]) and not result["ok"]
        if scenario == "cap":
            assert result["outcome"] == "partial" and result["remaining_ids"] == ids[500:]
        if scenario == "late":
            assert result["outcome"] == "unknown" and result["deleted_ids"] == ids[:50]
            assert result["has_more"] is None and result["pending_batch_number"] == 2
            raw = run_cleanup_request(
                requests[1],
                env={
                    "ANTIGRAVITY_CSRF_TOKEN": "synth-marker",
                    "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:41001",
                    "ANTIGRAVITY_CONVERSATION_ID": new_controls[-1],
                },
            )
            path = workspaces[1] / "result.json"
            atomic_write_json(path, {**raw, "batch_number": 1})
            assert overview(home)["last_cleanup"]["deleted_ids"] == ids[:50]
            atomic_write_json(path, raw)
            late = overview(home)["last_cleanup"]
            assert late["late_worker_receipt"] and late["outcome"] == "unknown" and late["deleted_ids"] == ids
            assert not late["batches"][1]["process_exited"] and len(requests) == 2
            with pytest.raises(SlotCleanupConflictError):
                run_slot(home, spec, unexpected_runner)


def _verified_http_batch():
    cid = str(uuid.uuid4())
    req = CleanerRequest(SLOT_A.name, SLOT_A.project_id, (SLOT_A.project_id,), FIXED_NOW, False)
    raw = {
        **req.identity,
        **_batch_fields(req.identity),
        "ok": True,
        "checked": 1,
        "eligible_ids": [cid],
        "deleted_ids": [cid],
        "deleted": 1,
        "http_200_ids": [cid],
        "would_delete": 0,
        "would_delete_ids": [],
        "rejected_delete_ids": [],
        "unconfirmed_delete_ids": [],
        "skipped_recent": 0,
        "skipped_running": 0,
        "skipped_unverified": 0,
        "errors": [],
    }
    res = agy_slots.parse_worker_receipt(raw, req.identity)
    assert res.worker_receipt_valid
    return req, replace(res, process_exited=True, profile_matches=True), cid


@pytest.mark.parametrize(
    "gate, reason",
    [
        ("preview", None),
        ("other_os", None),
        ("no_http", None),
        ("no_receipt", "receipt_unverified"),
        ("wrong_project", "receipt_unverified"),
        ("wrong_batch", "receipt_unverified"),
        ("no_self", "receipt_unverified"),
        ("self", "receipt_unverified"),
        ("not_eligible", "receipt_unverified"),
        ("opaque_id", "receipt_unverified"),
        ("process_still_running", "process_exit_unconfirmed"),
        ("profile_mismatch", "cli_profile_unverified"),
        ("cli", "agy_cli_running"),
        ("scan", "agy_cli_scan_unavailable"),
        ("summary_present", "summary_present"),
        ("summary_missing", "summary_unavailable"),
        ("summary_locked", "summary_check_failed"),
        ("existing_quarantine", "quarantine_unavailable"),
    ],
)
def test_windows_residual_requires_each_gate(monkeypatch, gate, reason):
    profile = _synthetic_cli_profile()
    req, res, cid = _verified_http_batch()
    monkeypatch.setattr(agy_slots, "_WINDOWS_RESIDUALS", gate != "other_os")
    db = sqlite3.connect(profile / "conversation_summaries.db")
    source = profile / "conversations" / f"{cid}.db"
    source.write_bytes(b"fixture payload")
    root = Path.home() / ".agent-bridge" / "agy_slot_quarantine" / req.operation_id
    try:
        if gate == "preview":
            req = replace(req, dry_run=True)
        elif gate == "no_http":
            res = replace(res, http_200_ids=())
        elif gate == "no_receipt":
            res = replace(res, worker_receipt_valid=False)
        elif gate == "wrong_project":
            req = replace(req, project_id=SLOT_B.project_id)
        elif gate == "wrong_batch":
            req = replace(req, batch_number=2)
        elif gate == "no_self":
            res = replace(res, self_conversation_id=None)
        elif gate == "self":
            res = replace(res, self_conversation_id=cid.upper())
        elif gate == "not_eligible":
            res = replace(res, eligible_ids=())
        elif gate == "opaque_id":
            res = replace(res, eligible_ids=("not-a-uuid",), http_200_ids=("not-a-uuid",))
        elif gate == "process_still_running":
            res = replace(res, process_exited=False)
        elif gate == "profile_mismatch":
            res = replace(res, profile_matches=False)
        elif gate in ("cli", "scan"):
            monkeypatch.setattr(
                agy_slots,
                "_running_agy_cli_processes",
                lambda: ([{"pid": 123, "name": "AGY.EXE"}], True) if gate == "cli" else ([], False),
            )
        elif gate == "summary_present":
            db.execute("INSERT INTO conversation_summaries VALUES (?)", (cid,))
            db.commit()
        elif gate == "summary_missing":
            db.close()
            (profile / "conversation_summaries.db").unlink()
        elif gate == "summary_locked":
            db.execute("BEGIN EXCLUSIVE")
        elif gate == "existing_quarantine":
            occupied = root / "batch-01" / cid
            agy_slots._extended_path(occupied).mkdir(parents=True)
            agy_slots._extended_path(occupied / source.name).write_bytes(b"prior fixture")
        rows = agy_slots._windows_residuals(req, res)
        if reason:
            (item,) = rows
            assert item["status"] == "residual_locked" and item["reason"] == reason and not item["moved"]
        else:
            assert rows == []
        assert source.read_bytes() == b"fixture payload"
        if gate == "existing_quarantine":
            assert agy_slots._extended_path(occupied / source.name).read_bytes() == b"prior fixture"
        else:
            assert not root.exists()
    finally:
        db.close()


@pytest.mark.parametrize("interrupt_move", [False, True])
def test_windows_quarantine_moves_only_owned_items_and_reports_partial(monkeypatch, interrupt_move):
    monkeypatch.setattr(agy_slots, "_WINDOWS_RESIDUALS", True)
    profile = _synthetic_cli_profile()
    req, res, cid = _verified_http_batch()
    data = profile / "conversations"
    for suffix in (".db", ".db-wal", ".db-shm"):
        (data / (cid + suffix)).write_bytes(suffix.encode())
    unrelated = data / f"{uuid.uuid4()}.db"
    unrelated.write_bytes(b"keep")
    deep = Path(*(["nested-synthetic-data-12345"] * 10), "fixture.txt")
    original = profile / "brain" / cid / deep
    agy_slots._extended_path(original.parent).mkdir(parents=True)
    agy_slots._extended_path(original).write_text("deep fixture", encoding="utf-8")
    destination = Path.home() / ".agent-bridge" / "agy_slot_quarantine" / req.operation_id / "batch-01" / cid
    renames = []
    rename = agy_slots._move_residual

    def rename_fixture(src, dst, before, *, directory):
        renames.append(Path(src).name)
        if interrupt_move and Path(src).name.endswith("-wal"):
            raise PermissionError("fixture sharing violation")
        return rename(src, dst, before, directory=directory)

    monkeypatch.setattr(agy_slots, "_move_residual", rename_fixture)
    (item,) = agy_slots._windows_residuals(req, res)
    assert item["id"] == cid and unrelated.read_bytes() == b"keep"
    assert agy_slots._extended_path(destination / (cid + ".db")).read_bytes() == b".db"
    assert not (data / (cid + ".db")).exists()
    if interrupt_move:
        assert item["status"] == "residual_locked" and item["moved"] == ["db"]
        assert item["reason"] == "move_failed" and item["remaining"] == ["wal", "shm", "brain"]
        assert renames == [cid + ".db", cid + ".db-wal"]
        assert agy_slots._extended_path(original).read_text(encoding="utf-8") == "deep fixture"
    else:
        assert item["status"] == "windows_residual_reclaimed" and item["remaining"] == []
        assert renames == [cid + ".db", cid + ".db-wal", cid + ".db-shm", cid]
        assert agy_slots._extended_path(destination / cid / deep).read_text(encoding="utf-8") == "deep fixture"
    # Reusing an occupied CID quarantine cannot overwrite it or claim a native removal.
    (again,) = agy_slots._windows_residuals(req, res)
    assert again["status"] == "residual_locked" and not again["moved"]


@pytest.mark.parametrize("where", ["source_parent", "brain", "quarantine"])
def test_quarantine_rejects_indirect_roots_and_entries(monkeypatch, tmp_path, where):
    monkeypatch.setattr(agy_slots, "_WINDOWS_RESIDUALS", True)
    profile = _synthetic_cli_profile()
    req, res, cid = _verified_http_batch()
    target = tmp_path / "separate-fixture"
    target.mkdir()
    (target / (cid + ".db")).write_bytes(b"other fixture")
    source_db = profile / "conversations" / (cid + ".db")
    if where == "source_parent":
        link = source_db.parent
        link.rmdir()
    else:
        source_db.write_bytes(b"local fixture")
        link = profile / "brain" / cid if where == "brain" else Path.home() / ".agent-bridge" / "agy_slot_quarantine"
        link.parent.mkdir(exist_ok=True)
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        (item,) = agy_slots._windows_residuals(req, res)
        assert item["status"] == "residual_locked" and item["moved"] == []
        assert (target / (cid + ".db")).read_bytes() == b"other fixture"
        assert list(target.iterdir()) == [target / (cid + ".db")]
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_native_removal_uses_readonly_summary_even_if_artifact_roots_are_absent(monkeypatch):
    monkeypatch.setattr(agy_slots, "_WINDOWS_RESIDUALS", True)
    profile = _synthetic_cli_profile()
    req, res, cid = _verified_http_batch()
    (profile / "conversations").rmdir()
    (profile / "brain").rmdir()
    connect = sqlite3.connect
    queries = []

    def connect_readonly(database, **kwargs):
        assert kwargs["uri"] is True and database == (profile / "conversation_summaries.db").as_uri() + "?mode=ro"
        db = connect(database, **kwargs)

        def execute(sql, params):
            queries.append((sql, params))
            return db.execute(sql, params)

        return SimpleNamespace(execute=execute, close=db.close)

    monkeypatch.setattr(agy_slots.sqlite3, "connect", connect_readonly)
    (row,) = agy_slots._windows_residuals(req, res)
    assert queries == [("SELECT COUNT(*) FROM conversation_summaries WHERE conversation_id = ?", (cid,))]
    assert row == {"id": cid, "status": "native_removed", "moved": [], "remaining": [], "reason": None}
    assert not (profile / "brain").exists() and not (profile / "conversations").exists()
    assert not (Path.home() / ".agent-bridge" / "agy_slot_quarantine").exists()


def test_residual_stays_in_place_when_destination_volume_differs(monkeypatch):
    monkeypatch.setattr(agy_slots, "_WINDOWS_RESIDUALS", True)
    profile = _synthetic_cli_profile()
    req, res, cid = _verified_http_batch()
    source = profile / "conversations" / (cid + ".db")
    source.write_bytes(b"original fixture")
    destination = Path.home() / ".agent-bridge" / "agy_slot_quarantine" / req.operation_id / "batch-01" / cid
    inspect = agy_slots._checked_entry

    def fake_volume(path, **kwargs):
        info = inspect(path, **kwargs)
        if info is not None and path == destination:
            return SimpleNamespace(st_dev=info.st_dev + 1, st_ino=info.st_ino)
        return info

    monkeypatch.setattr(agy_slots, "_checked_entry", fake_volume)
    monkeypatch.setattr(agy_slots, "_move_residual", unexpected_runner)
    (row,) = agy_slots._windows_residuals(req, res)
    assert row["status"] == "residual_locked" and row["moved"] == [] and row["remaining"] == ["db"]
    assert (
        source.read_bytes() == b"original fixture" and not agy_slots._extended_path(destination / source.name).exists()
    )
