from __future__ import annotations

import http.client
import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_bridge.adapters.antigravity import AgyAdapter
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
from agent_bridge.dashboard_server import create_dashboard_server
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
    for spec in (SLOT_A, SLOT_B):
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


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, json.loads(raw.decode("utf-8"))
    finally:
        conn.close()


def _json_req(
    port: int,
    method: str,
    path: str,
    *,
    csrf: str | None = None,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    hdrs: dict[str, str] = {}
    if csrf is not None:
        hdrs["X-Bridge-CSRF"] = csrf
    body: bytes | None = None
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if extra_headers:
        hdrs.update(extra_headers)
    return _request(port, method, path, headers=hdrs, body=body)


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
    projects_dir.mkdir()
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
    adapter._pending_slots.clear()
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
    _write_projects(projects_dir, make_slot_c_non_empty=True)

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
    assert by_name[SLOT_C.name]["project_is_empty"] is False

    called = False

    def fake_runner(req: CleanerRequest, **_kw: Any) -> CleanerExecutionResult:
        nonlocal called
        called = True
        return CleanerExecutionResult(ok=True, checked=1, deleted=0 if req.dry_run else 1)

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

    # Once the Bridge session is closed (proc_state=dead), slot becomes releasable and live cleanup succeeds.
    atomic_write_json(
        state_path(tmp_path),
        {
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

    live = cleanup_agy_slot(
        tmp_path,
        rel_slot.name,
        dry_run=False,
        confirm=rel_slot.name,
        now=now,
        projects_dir=projects_dir,
        cleaner_runner=fake_runner,
    )
    assert live["ok"] is True
    assert live["deleted"] == 1
    assert live["blocked_sessions"] == []


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


class _MockConversationService:
    def __init__(self, csrf_token: str, pages: list[list[dict[str, Any]]]) -> None:
        self.csrf_token = csrf_token
        self.pages = pages
        self.list_calls: list[dict[str, Any]] = []
        self.deleted_ids: list[str] = []
        self.fail_delete_ids: set[str] = set()

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return None

            def do_POST(self) -> None:
                if self.headers.get("Connect-Protocol-Version") != "1":
                    self._reply(
                        HTTPStatus.BAD_REQUEST, {"code": "invalid_argument", "message": "missing Connect header"}
                    )
                    return
                if self.headers.get("x-antigravity-csrf-token") != parent.csrf_token:
                    self._reply(HTTPStatus.UNAUTHORIZED, {"code": "unauthenticated", "message": "bad csrf"})
                    return
                length = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

                if self.path == "/product.v1.ConversationService/ListConversations":
                    parent.list_calls.append(body)
                    token = str(body.get("pageToken") or "")
                    idx = int(token) if token.isdigit() else 0
                    items = parent.pages[idx] if idx < len(parent.pages) else []
                    next_token = str(idx + 1) if idx + 1 < len(parent.pages) else ""
                    self._reply(HTTPStatus.OK, {"conversations": items, "nextPageToken": next_token})
                    return

                if self.path == "/product.v1.ConversationService/DeleteConversation":
                    cid = str(body.get("conversationId") or "")
                    if cid in parent.fail_delete_ids:
                        self._reply(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"code": "internal", "message": f"boom with token={parent.csrf_token}"},
                        )
                        return
                    parent.deleted_ids.append(cid)
                    self._reply(HTTPStatus.OK, {})
                    return

                self._reply(HTTPStatus.NOT_FOUND, {"code": "not_found"})

            def _reply(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def address(self) -> str:
        host, port = self.server.server_address
        return f"{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_worker_timestamp_and_loopback_validation():
    dt = parse_rfc3339("2026-11-26T06:01:17.748011700Z")
    assert dt == datetime(2026, 11, 26, 6, 1, 17, 748011, tzinfo=UTC)
    assert parse_rfc3339("not-a-timestamp") is None
    assert validate_loopback_address("127.0.0.1:5387") == "127.0.0.1:5387"
    with pytest.raises(ValueError, match="loopback"):
        validate_loopback_address("evil.example:5387")


def test_worker_protocol_filters_whitelist_self_running_recent_and_redacts_token():
    secret_token = "SUPER-SECRET-CSRF-TOKEN-NEVER-LEAK-987654"
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
                "state": {"runState": "RUN_STATE_RUNNING", "updateTime": "2026-11-24T00:00:00Z"},
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

    mock_ls = _MockConversationService(secret_token, pages)
    try:
        env = {
            "ANTIGRAVITY_LS_ADDRESS": mock_ls.address,
            "ANTIGRAVITY_CSRF_TOKEN": secret_token,
            "ANTIGRAVITY_CONVERSATION_ID": self_conv_id,
        }
        allowed = [SLOT_A.project_id, SLOT_B.project_id, SLOT_C.project_id]

        # 1. External project_id refused with ZERO RPC calls
        rejected = run_cleanup_request(
            {
                "slot": "evil",
                "project_id": EXTERNAL_PROJECT_ID,
                "allowed_project_ids": allowed,
                "cutoff_utc": cutoff.isoformat(),
                "dry_run": False,
            },
            env=env,
        )
        assert rejected["ok"] is False
        assert mock_ls.list_calls == []
        assert mock_ls.deleted_ids == []

        # 2. dry_run=True lists both pages, skips non-eligible, executes ZERO deletes
        dry = run_cleanup_request(
            {
                "slot": SLOT_B.name,
                "project_id": SLOT_B.project_id,
                "allowed_project_ids": allowed,
                "cutoff_utc": cutoff.isoformat(),
                "dry_run": True,
            },
            env=env,
        )
        assert len(mock_ls.list_calls) == 2
        assert mock_ls.list_calls[0]["projectId"] == SLOT_B.project_id
        assert mock_ls.list_calls[0]["includeArchived"] is True
        assert dry["checked"] == 6
        assert dry["deleted"] == 0
        assert dry["would_delete"] == 2
        assert dry["would_delete_ids"] == ["conv-old-idle-1", "conv-old-fail"]
        assert dry["skipped_running"] == 2
        assert dry["skipped_recent"] == 1
        assert len(dry["errors"]) == 1  # rogue external projectId flagged
        assert mock_ls.deleted_ids == []

        # 3. dry_run=False deletes ONLY eligible old IDLE items; error redacts CSRF token
        mock_ls.fail_delete_ids.add("conv-old-fail")
        live = run_cleanup_request(
            {
                "slot": SLOT_B.name,
                "project_id": SLOT_B.project_id,
                "allowed_project_ids": allowed,
                "cutoff_utc": cutoff.isoformat(),
                "dry_run": False,
            },
            env=env,
        )
        assert mock_ls.deleted_ids == ["conv-old-idle-1"]
        assert live["deleted"] == 1
        assert live["deleted_ids"] == ["conv-old-idle-1"]
        assert live["ok"] is False  # failed delete + rogue item
        assert secret_token not in json.dumps(live)
    finally:
        mock_ls.close()


# ---------------------------------------------------------------------------
# 5. Tier 3 cleaner command builder & isolated subprocess execution
# ---------------------------------------------------------------------------


def test_tier3_cleaner_uses_scheme_a_and_minimal_allowlist(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agent_bridge.agy_slots.resolve_command", lambda cmd, fb=None: ["agy"])
    monkeypatch.setenv("ANTIGRAVITY_LS_ADDRESS", "127.0.0.1:9999")
    monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", "parent-desktop-secret-must-be-stripped")
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
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
                    "ok": True,
                    "checked": 3,
                    "deleted": 2,
                    "would_delete": 0,
                    "deleted_ids": ["c1", "c2"],
                    "would_delete_ids": [],
                    "skipped_running": 1,
                    "skipped_recent": 0,
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("agent_bridge.agy_slots.subprocess.run", fake_run)
    agent = AgentConfig(name="antigravity", protocol="agy", command=["agy"], agy_slots=_three_slot_cfg())
    res = execute_tier3_cleanup(
        CleanerRequest(
            slot=SLOT_B.name,
            project_id=SLOT_B.project_id,
            allowed_project_ids=(SLOT_A.project_id, SLOT_B.project_id, SLOT_C.project_id),
            cutoff_utc=datetime(2026, 11, 26, 0, 0, tzinfo=UTC),
            dry_run=False,
        ),
        agent_cfg=agent,
        home=tmp_path,
    )
    assert res.ok is True
    assert res.checked == 3
    assert res.deleted == 2
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
        if req.dry_run:
            return CleanerExecutionResult(
                ok=True,
                checked=4,
                deleted=0,
                would_delete=2,
                would_delete_ids=("c1", "c2"),
                skipped_running=1,
                skipped_recent=1,
            )
        return CleanerExecutionResult(
            ok=True,
            checked=4,
            deleted=2,
            deleted_ids=("c1", "c2"),
            skipped_running=1,
            skipped_recent=1,
        )

    server = create_dashboard_server(
        home=tmp_path,
        host="127.0.0.1",
        port=0,
        backfill=False,
        agy_cleaner_runner=fake_cleaner,
        agy_projects_dir_override=projects_dir,
        agy_now_fn=lambda: fixed_now,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.bound_port
    csrf = server.csrf_token
    try:
        # 1. Host header guard on GET
        st, _ = _request(port, "GET", "/api/agy/slots", headers={"Host": "evil.example"})
        assert st == 403

        # 2. GET /api/agy/slots returns full structured slot overview
        st, overview = _json_req(port, "GET", "/api/agy/slots")
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
        st, _ = _json_req(port, "POST", "/api/agy/slots/cleanup", payload={"slot": rel_slot.name, "dry_run": True})
        assert st == 403
        st, _ = _json_req(
            port,
            "POST",
            "/api/agy/slots/cleanup",
            csrf=csrf,
            payload={"slot": rel_slot.name, "dry_run": True},
            extra_headers={"Origin": "http://evil.example"},
        )
        assert st == 403

        # 4. Input validation: non-boolean dry_run, missing/wrong confirm, unknown slot
        st, _ = _json_req(
            port,
            "POST",
            "/api/agy/slots/cleanup",
            csrf=csrf,
            payload={"slot": rel_slot.name, "dry_run": "false"},
        )
        assert st == 400

        st, _ = _json_req(
            port,
            "POST",
            "/api/agy/slots/cleanup",
            csrf=csrf,
            payload={"slot": rel_slot.name, "dry_run": False, "confirm": "wrong"},
        )
        assert st == 400

        st, _ = _json_req(
            port,
            "POST",
            "/api/agy/slots/cleanup",
            csrf=csrf,
            payload={"slot": "nonexistent", "dry_run": True},
        )
        assert st == 400

        # 5. Live cleanup on active slot blocked with 409 Conflict
        st, conflict = _json_req(
            port,
            "POST",
            "/api/agy/slots/cleanup",
            csrf=csrf,
            payload={"slot": act_slot.name, "dry_run": False, "confirm": act_slot.name},
        )
        assert st == 409
        assert conflict["ok"] is False
        assert conflict["slot"] == act_slot.name
        assert conflict["errors"]

        # 6. dry_run=True preview returns structured counts without deleting
        st, dry_resp = _json_req(
            port,
            "POST",
            "/api/agy/slots/cleanup",
            csrf=csrf,
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
        assert dry_resp["would_delete"] == 2

        # 7. Live cleanup on releasable slot with exact confirm succeeds and updates receipt
        st, live_resp = _json_req(
            port,
            "POST",
            "/api/agy/slots/cleanup",
            csrf=csrf,
            payload={"slot": rel_slot.name, "dry_run": False, "confirm": rel_slot.name},
        )
        assert st == 200
        assert live_resp["ok"] is True
        assert live_resp["dry_run"] is False
        assert live_resp["deleted"] == 2
        assert live_resp["skipped_running"] == 1
        assert live_resp["skipped_recent"] == 1
        assert live_resp["errors"] == []
        assert len(calls) == 2

        # 8. Subsequent GET /api/agy/slots surfaces the latest cleanup receipt
        st, after_overview = _json_req(port, "GET", "/api/agy/slots")
        assert st == 200
        assert after_overview["last_cleanup"]["slot"] == rel_slot.name
        assert after_overview["last_cleanup"]["deleted"] == 2
        after_by_name = {row["name"]: row for row in after_overview["slots"]}
        assert after_by_name[rel_slot.name]["last_cleanup"]["deleted"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_agy_slots_api_default_disabled(tmp_path: Path):
    server = create_dashboard_server(
        home=tmp_path,
        host="127.0.0.1",
        port=0,
        backfill=False,
        agy_projects_dir_override=tmp_path / "missing_projects",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.bound_port
    try:
        st, payload = _json_req(port, "GET", "/api/agy/slots")
        assert st == 200
        assert payload["ok"] is True
        assert payload["enabled"] is False
        assert payload["active_slot"] is None
        assert payload["slots"] == []
        assert payload["cleaner"]["available"] is False

        st, err = _json_req(
            port,
            "POST",
            "/api/agy/slots/cleanup",
            csrf=server.csrf_token,
            payload={"slot": "slotA", "dry_run": True},
        )
        assert st == 400
        assert err["ok"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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
    assert by_name[rel_slot.name]["status"] == "blocked"
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

    def raise_timeout(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, timeout=10)

    monkeypatch.setattr("agent_bridge.agy_slots.subprocess.run", raise_timeout)
    timeout_res = execute_tier3_cleanup(req, agent_cfg=agent, home=tmp_path)
    assert timeout_res.ok is False
    assert any("timed out" in e for e in timeout_res.errors)

    def no_output_file(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            argv, 1, stdout=b"", stderr=b"boom Authorization: Bearer sk-secret1234567890"
        )

    monkeypatch.setattr("agent_bridge.agy_slots.subprocess.run", no_output_file)
    missing_res = execute_tier3_cleanup(req, agent_cfg=agent, home=tmp_path)
    assert missing_res.ok is False
    assert any("no result.json" in e for e in missing_res.errors)
    assert "sk-secret1234567890" not in " ".join(missing_res.errors)


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
