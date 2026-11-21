from __future__ import annotations

import http.client
import json
import os
import re
import threading
import types
from pathlib import Path
from typing import Any

import psutil
import pytest
from pydantic import ValidationError

from agent_bridge import dashboard_server, dashboard_service
from agent_bridge.billing_store import load_ledger
from agent_bridge.dashboard_server import create_dashboard_server
from agent_bridge.dashboard_service import (
    AUTOSTART_REG_PATH,
    AUTOSTART_VALUE_NAME,
    ServiceConfig,
    clear_pid_record,
    get_autostart_status,
    has_service_config,
    initialize_first_use_preferences,
    is_pid_record_alive,
    load_service_config,
    read_pid_record,
    save_service_config,
    set_autostart,
    write_pid_record,
)
from agent_bridge.paths import usage_ledger_path
from agent_bridge.persist import FileLockTimeout


class _FakeRegistry:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def read_value(self, key_path: str, name: str) -> str | None:
        return self.values.get((key_path, name))

    def write_value(self, key_path: str, name: str, value: str) -> None:
        self.values[(key_path, name)] = value

    def delete_value(self, key_path: str, name: str) -> None:
        self.values.pop((key_path, name), None)


class _OsNameProxy:
    """Patch the module's platform view without changing the process-wide ``os`` module."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(os, attr)


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _json_req(
    port: int,
    method: str,
    path: str,
    *,
    csrf: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    hdrs: dict[str, str] = {}
    if csrf is not None:
        hdrs["X-Bridge-CSRF"] = csrf
    body: bytes | None = None
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    status, raw = _request(port, method, path, headers=hdrs, body=body)
    return status, json.loads(raw.decode("utf-8"))


def test_service_config_defaults_and_validation(tmp_path: Path):
    cfg = load_service_config(tmp_path)
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8765
    assert cfg.auto_start is False
    assert cfg.tray_enabled is False
    assert has_service_config(tmp_path) is False

    saved = save_service_config(ServiceConfig(host="localhost", port=9100, auto_start=False, tray_enabled=True), tmp_path)
    assert saved.host == "localhost"
    reloaded = load_service_config(tmp_path)
    assert reloaded.port == 9100 and reloaded.tray_enabled is True

    with pytest.raises(ValidationError):
        ServiceConfig(host="0.0.0.0")
    with pytest.raises(ValidationError, match="IPv4 loopback"):
        ServiceConfig(host="::1")
    with pytest.raises(ValidationError):
        ServiceConfig(port=80)


def _read_service_json(home: Path) -> dict[str, Any]:
    return json.loads((home / "billing" / "service.json").read_text(encoding="utf-8"))


def test_first_use_on_windows_saves_both_defaults_and_registers_autostart(tmp_path: Path, monkeypatch):
    fake_reg = _FakeRegistry()
    monkeypatch.setattr(dashboard_service, "os", _OsNameProxy("nt"))
    monkeypatch.setattr(dashboard_service.subprocess, "Popen", _unexpected_popen)
    cfg = initialize_first_use_preferences(tmp_path, registry=fake_reg)
    assert cfg.auto_start is True and cfg.tray_enabled is True
    assert has_service_config(tmp_path) is True
    assert _read_service_json(tmp_path)["auto_start"] is True
    assert _read_service_json(tmp_path)["tray_enabled"] is True
    value = fake_reg.read_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME)
    assert value is not None and value.startswith('wscript.exe "')
    assert (tmp_path / "billing" / "dashboard-autostart.vbs").is_file()


def test_first_use_respects_another_homes_run_entry(tmp_path: Path, monkeypatch):
    fake_reg = _FakeRegistry()
    fake_reg.write_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME, 'wscript.exe "C:/other/path.vbs"')
    monkeypatch.setattr(dashboard_service, "os", _OsNameProxy("nt"))
    cfg = initialize_first_use_preferences(tmp_path, registry=fake_reg)
    assert cfg.tray_enabled is True
    assert cfg.auto_start is False
    assert fake_reg.read_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME) == 'wscript.exe "C:/other/path.vbs"'
    assert not (tmp_path / "billing" / "dashboard-autostart.vbs").exists()


def test_first_use_reload_preserves_defaults_and_explicit_false(tmp_path: Path, monkeypatch):
    fake_reg = _FakeRegistry()
    monkeypatch.setattr(dashboard_service, "os", _OsNameProxy("nt"))
    initialize_first_use_preferences(tmp_path, registry=fake_reg)
    reloaded = load_service_config(tmp_path)
    assert reloaded.auto_start is True and reloaded.tray_enabled is True

    # A later unrelated write through save_service_config must not revive either flag.
    save_service_config(ServiceConfig(auto_start=False, tray_enabled=True), tmp_path)
    again = initialize_first_use_preferences(tmp_path, registry=fake_reg)
    assert again.auto_start is False and again.tray_enabled is True


@pytest.mark.parametrize(
    ("auto_start", "tray_enabled"),
    [(True, True), (True, False), (False, True), (False, False)],
)
def test_saved_switch_combinations_round_trip_without_upgrade(tmp_path: Path, monkeypatch, auto_start, tray_enabled):
    fake_reg = _FakeRegistry()
    monkeypatch.setattr(dashboard_service, "os", _OsNameProxy("nt"))
    save_service_config(ServiceConfig(auto_start=auto_start, tray_enabled=tray_enabled), tmp_path)
    if auto_start:
        set_autostart(True, tmp_path, platform_is_windows=True, registry=fake_reg)
    cfg = initialize_first_use_preferences(tmp_path, registry=fake_reg)
    assert cfg.auto_start is auto_start and cfg.tray_enabled is tray_enabled
    assert load_service_config(tmp_path).auto_start is auto_start
    assert load_service_config(tmp_path).tray_enabled is tray_enabled
    # Reinitialization after another switch write leaves this saved value alone.
    other = not tray_enabled
    save_service_config(ServiceConfig(auto_start=auto_start, tray_enabled=other), tmp_path)
    after = load_service_config(tmp_path)
    assert after.auto_start is auto_start and after.tray_enabled is other
    # Writing the other switch through the real HTTP handler leaves this switch's registry state alone.
    seen = []
    monkeypatch.setattr(dashboard_server, "get_autostart_status", lambda *a, **kw: {"occupied": False})
    monkeypatch.setattr(dashboard_server, "set_autostart", lambda enabled, home=None, **kw: seen.append(enabled))
    monkeypatch.setattr(dashboard_server, "start_tray", lambda *a, **kw: {"message": "tray"})
    monkeypatch.setattr(dashboard_server, "stop_tray", lambda *a, **kw: {"message": "stop"})
    handler = types.SimpleNamespace(
        server=types.SimpleNamespace(home=tmp_path, bound_host="127.0.0.1", bound_port=8765, started_monotonic=1),
        _send_json=lambda value: None,
    )
    dashboard_server.DashboardRequestHandler._handle_put_service(handler, {"tray_enabled": not other})
    assert seen == []
    assert load_service_config(tmp_path).auto_start is auto_start


def _unexpected_popen(*_args, **_kwargs):
    pytest.fail("first-use preference setup must not launch a process")


def _first_use_start_fixture(tmp_path: Path, monkeypatch):
    """Wire a fake Windows first run through start_background_service's real launch path."""
    fake_reg = _FakeRegistry()
    script = tmp_path / "dashboard-tray.ps1"
    script.write_text("# stub", encoding="utf-8")
    tray_state = {"running": False, "calls": 0}
    pid, creation = 6541, 1234.5
    spawned = []
    registry_writes = []
    original_write = fake_reg.write_value

    def write_value(key_path, name, value):
        registry_writes.append((key_path, name, value))
        original_write(key_path, name, value)

    fake_reg.write_value = write_value
    monkeypatch.setattr(dashboard_service, "os", _OsNameProxy("nt"))
    monkeypatch.setattr(dashboard_service, "process_create_time", lambda _pid: creation)
    monkeypatch.setattr(
        dashboard_service.psutil,
        "Process",
        lambda _pid: types.SimpleNamespace(
            is_running=lambda: True, status=lambda: "running", create_time=lambda: creation
        ),
    )
    monkeypatch.setattr(dashboard_service, "resolve_bridge_command", lambda: ("agent-bridge.exe", []))
    monkeypatch.setattr(dashboard_service, "_powershell_executable", lambda: "powershell.exe")
    monkeypatch.setattr(dashboard_service, "bundled_tray_script", lambda: script)

    def is_tray_running(_home):
        return tray_state["running"]

    def start(command, *, env, **kwargs):
        spawned.append((command, kwargs))
        write_pid_record(
            tmp_path,
            host="127.0.0.1",
            port=8765,
            pid=pid,
            instance_id=env[dashboard_service.DASHBOARD_LAUNCH_ID_ENV],
        )
        return types.SimpleNamespace(pid=9870, poll=lambda: 0)

    def health(host, port, **kwargs):
        record = read_pid_record(tmp_path)
        return {"ok": True, "host": host, "port": port, "pid": pid, "instance_id": record.instance_id}

    def tray_popen(command, **kwargs):
        tray_state["calls"] += 1
        tray_state["running"] = True
        spawned.append((command, kwargs))
        return types.SimpleNamespace(pid=4567)

    def dispatch_popen(command, **kwargs):
        return tray_popen(command, **kwargs) if command[0] == "powershell.exe" else start(command, **kwargs)

    monkeypatch.setattr(dashboard_service, "is_tray_running", is_tray_running)
    monkeypatch.setattr(dashboard_service, "probe_health", health)
    monkeypatch.setattr(dashboard_service.subprocess, "Popen", dispatch_popen)
    real_set_autostart = dashboard_service.set_autostart
    monkeypatch.setattr(
        dashboard_service,
        "set_autostart",
        lambda enabled, home=None, **kw: real_set_autostart(enabled, home, platform_is_windows=True, registry=fake_reg),
    )
    real_get_autostart = dashboard_service.get_autostart_status
    monkeypatch.setattr(
        dashboard_service,
        "get_autostart_status",
        lambda home=None, **kw: real_get_autostart(home, platform_is_windows=True, registry=fake_reg),
    )
    return fake_reg, tray_state, spawned, registry_writes


def test_first_use_start_writes_autostart_and_starts_tray_once(tmp_path: Path, monkeypatch):
    fake_reg, tray_state, spawned, registry_writes = _first_use_start_fixture(tmp_path, monkeypatch)
    from agent_bridge.cli import main

    monkeypatch.setenv("AGENT_BRIDGE_HOME", str(tmp_path))
    main(["dashboard", "start"])
    assert tray_state["calls"] == 1 and tray_state["running"] is True
    assert len(registry_writes) == 1
    assert fake_reg.read_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME) is not None
    assert (tmp_path / "billing" / "dashboard-autostart.vbs").is_file()
    tray_calls = [kwargs for command, kwargs in spawned if command[0] == "powershell.exe"]
    assert tray_calls and tray_calls[0]["creationflags"] == 0x08000000
    assert load_service_config(tmp_path).model_dump()["auto_start"] is True
    assert load_service_config(tmp_path).model_dump()["tray_enabled"] is True


def test_read_only_paths_do_not_touch_registry_or_spawn(tmp_path: Path, monkeypatch):
    fake_reg = _FakeRegistry()
    fake_reg.write_value = lambda *_args, **_kwargs: pytest.fail("read-only path wrote the registry")
    monkeypatch.setattr(dashboard_service.subprocess, "Popen", _unexpected_popen)
    monkeypatch.setattr(dashboard_service.subprocess, "run", _unexpected_popen)
    monkeypatch.setattr(dashboard_service, "set_autostart", _unexpected_popen)
    monkeypatch.setattr(dashboard_service, "start_tray", _unexpected_popen)
    monkeypatch.setattr(dashboard_service, "stop_tray", _unexpected_popen)
    monkeypatch.setattr("agent_bridge.dashboard_server.set_autostart", _unexpected_popen)
    monkeypatch.setattr("agent_bridge.dashboard_server.start_tray", _unexpected_popen)
    monkeypatch.setattr("agent_bridge.dashboard_server.stop_tray", _unexpected_popen)
    monkeypatch.setattr(dashboard_service.psutil, "Process", lambda _pid: pytest.fail("read-only path inspected a PID"))
    status = dashboard_service.service_status(tmp_path, registry=fake_reg)
    assert status["config"]["auto_start"] is False and status["config"]["tray_enabled"] is False
    assert not (tmp_path / "billing" / "dashboard-autostart.vbs").exists()


def test_read_only_cli_status_and_version_leave_first_use_uncreated(tmp_path: Path, monkeypatch):
    from agent_bridge.cli import main

    fake_reg = _FakeRegistry()
    fake_reg.write_value = lambda *_args, **_kwargs: pytest.fail("read-only CLI wrote the registry")
    monkeypatch.setenv("AGENT_BRIDGE_HOME", str(tmp_path))
    monkeypatch.setattr(dashboard_service, "get_autostart_status", lambda *a, **kw: {"occupied": False})
    monkeypatch.setattr(dashboard_service.subprocess, "Popen", _unexpected_popen)
    monkeypatch.setattr(dashboard_service.subprocess, "run", _unexpected_popen)
    main(["dashboard", "status"])
    assert has_service_config(tmp_path) is False
    assert not (tmp_path / "billing" / "dashboard-autostart.vbs").exists()


@pytest.mark.asyncio
async def test_mcp_handshake_and_tool_list_leave_first_use_uncreated(tmp_path: Path, monkeypatch):
    from mcp.client import Client

    from agent_bridge.server import mcp

    monkeypatch.setenv("AGENT_BRIDGE_HOME", str(tmp_path))
    monkeypatch.setattr(dashboard_service, "set_autostart", lambda *a, **kw: pytest.fail("handshake wrote autostart"))
    monkeypatch.setattr(dashboard_service.subprocess, "Popen", _unexpected_popen)
    async with Client(mcp) as client:
        tools = await client.list_tools()
    assert tools.tools
    assert has_service_config(tmp_path) is False
    assert not (tmp_path / "billing" / "dashboard-autostart.vbs").exists()


def test_saved_explicit_false_survives_start_and_reload(tmp_path: Path, monkeypatch):
    fake_reg, tray_state, _spawned, _registry_writes = _first_use_start_fixture(tmp_path, monkeypatch)
    save_service_config(ServiceConfig(auto_start=False, tray_enabled=False), tmp_path)
    result = dashboard_service.start_background_service(tmp_path, with_tray=None)
    assert result["started"] is True
    assert tray_state["calls"] == 0
    assert fake_reg.read_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME) is None
    assert load_service_config(tmp_path).auto_start is False
    assert load_service_config(tmp_path).tray_enabled is False
    assert not (tmp_path / "billing" / "dashboard-autostart.vbs").exists()


def test_saved_tray_true_starts_tray_without_rewriting_autostart(tmp_path: Path, monkeypatch):
    _fake_reg, tray_state, _spawned, registry_writes = _first_use_start_fixture(tmp_path, monkeypatch)
    save_service_config(ServiceConfig(auto_start=False, tray_enabled=True), tmp_path)
    result = dashboard_service.start_background_service(tmp_path)
    assert result["started"] is True and tray_state["calls"] == 1
    assert registry_writes == []
    assert not (tmp_path / "billing" / "dashboard-autostart.vbs").exists()


def test_cli_dashboard_start_initializes_first_use_once(tmp_path: Path, monkeypatch):
    from agent_bridge.cli import main

    seen = []
    monkeypatch.setenv("AGENT_BRIDGE_HOME", str(tmp_path))
    monkeypatch.setattr(dashboard_service, "os", _OsNameProxy("nt"))
    monkeypatch.setattr(dashboard_service, "set_autostart", lambda enabled, home=None, **kw: seen.append(enabled))
    monkeypatch.setattr(dashboard_service, "start_background_service", lambda **kw: {"message": "ok"})
    monkeypatch.setattr(dashboard_service, "stop_tray", lambda *a, **kw: {"message": "not called"})
    monkeypatch.setattr(dashboard_service, "start_tray", lambda *a, **kw: {"message": "tray"})
    main(["dashboard", "start"])
    main(["dashboard", "start"])
    assert seen == [True]
    assert load_service_config(tmp_path).model_dump()["auto_start"] is True


def test_put_service_preserves_unsubmitted_switch_after_first_use(tmp_path: Path, monkeypatch):
    seen = []
    monkeypatch.setattr(dashboard_service, "set_autostart", lambda enabled, home=None, **kw: seen.append(enabled))
    monkeypatch.setattr(dashboard_service, "get_autostart_status", lambda *a, **kw: {"occupied": False})
    monkeypatch.setattr(dashboard_server, "get_autostart_status", lambda *a, **kw: {"occupied": False})
    monkeypatch.setattr(dashboard_server, "set_autostart", lambda enabled, home=None, **kw: seen.append(enabled))
    monkeypatch.setattr(dashboard_server, "start_tray", lambda *a, **kw: {"message": "tray"})
    monkeypatch.setattr(dashboard_server, "stop_tray", lambda *a, **kw: {"message": "stop"})
    handler = types.SimpleNamespace(
        server=types.SimpleNamespace(home=tmp_path, bound_host="127.0.0.1", bound_port=8765, started_monotonic=1),
        _send_json=lambda value: None,
    )
    save_service_config(ServiceConfig(auto_start=True, tray_enabled=True), tmp_path)
    dashboard_server.DashboardRequestHandler._handle_put_service(handler, {"tray_enabled": False})
    saved = load_service_config(tmp_path)
    assert saved.auto_start is True and saved.tray_enabled is False
    assert seen == []


def test_pid_record_lifecycle(tmp_path: Path):
    assert read_pid_record(tmp_path) is None
    rec = write_pid_record(tmp_path, host="127.0.0.1", port=8765)
    assert rec.pid == os.getpid()
    loaded = read_pid_record(tmp_path)
    assert loaded is not None and loaded.pid == os.getpid()
    assert is_pid_record_alive(loaded) is True

    # Mismatched expected_pid must not delete another live instance's PID file.
    clear_pid_record(tmp_path, expected_pid=os.getpid() + 999999)
    assert read_pid_record(tmp_path) is not None

    clear_pid_record(tmp_path, expected_pid=os.getpid())
    assert read_pid_record(tmp_path) is None


def test_windows_autostart_uses_fake_registry_and_hidden_vbs(tmp_path: Path):
    fake_reg = _FakeRegistry()
    st0 = get_autostart_status(tmp_path, platform_is_windows=True, registry=fake_reg)
    assert st0["supported"] is True
    assert st0["enabled"] is False

    st1 = set_autostart(True, tmp_path, platform_is_windows=True, registry=fake_reg)
    assert st1["enabled"] is True
    assert st1["matches_current"] is True
    reg_val = fake_reg.read_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME)
    assert reg_val is not None and reg_val.startswith('wscript.exe "')
    vbs_file = tmp_path / "billing" / "dashboard-autostart.vbs"
    assert vbs_file.is_file()
    vbs_text = vbs_file.read_text(encoding="utf-8")
    assert "dashboard start" in vbs_text
    assert str(tmp_path) in vbs_text

    # Simulate path drift -> matches_current turns False.
    fake_reg.write_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME, 'wscript.exe "C:/old/path.vbs"')
    st_drift = get_autostart_status(tmp_path, platform_is_windows=True, registry=fake_reg)
    assert st_drift["enabled"] is False
    assert st_drift["matches_current"] is False
    assert st_drift["occupied"] is True

    # Disable removes registry entry and VBS file cleanly.
    fake_reg.write_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME, reg_val)
    st2 = set_autostart(False, tmp_path, platform_is_windows=True, registry=fake_reg)
    assert st2["enabled"] is False
    assert fake_reg.read_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME) is None
    assert not vbs_file.exists()

    # Non-Windows reports unsupported without touching registry.
    st_linux = set_autostart(True, tmp_path, platform_is_windows=False, registry=fake_reg)
    assert st_linux["supported"] is False and st_linux["enabled"] is False


def test_autostart_setting_cannot_change_another_home(tmp_path, monkeypatch):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    backend = _FakeRegistry()

    def get_status(home):
        return get_autostart_status(home, platform_is_windows=True, registry=backend)

    def set_status(enable, home):
        return set_autostart(enable, home, platform_is_windows=True, registry=backend)

    set_status(True, home_a)
    original = backend.read_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME)
    launcher = home_a / "billing" / "dashboard-autostart.vbs"
    source = launcher.read_bytes()
    handler = types.SimpleNamespace(
        server=types.SimpleNamespace(home=home_b, bound_host="127.0.0.1", bound_port=8765, started_monotonic=1),
        _send_json=lambda value: None,
    )
    monkeypatch.setattr(dashboard_server, "os", types.SimpleNamespace(name="nt", getpid=os.getpid))
    monkeypatch.setattr(dashboard_server, "get_autostart_status", get_status)
    monkeypatch.setattr(dashboard_server, "set_autostart", set_status)
    monkeypatch.setattr(dashboard_server, "service_status", lambda *a, **kw: {})

    dashboard_server.DashboardRequestHandler._handle_put_service(handler, {"port": 9000, "auto_start": False})
    assert load_service_config(home_b).port == 9000
    with pytest.raises(ValueError, match="另一个工作目录占用"):
        dashboard_server.DashboardRequestHandler._handle_put_service(handler, {"port": 9001, "auto_start": True})
    assert load_service_config(home_b).port == 9000
    assert not load_service_config(home_b).auto_start
    assert backend.read_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME) == original
    assert launcher.read_bytes() == source
    assert not (home_b / "billing" / "dashboard-autostart.vbs").exists()
    assert set_status(False, home_a)["enabled"] is False
    assert set_status(True, home_a)["enabled"] is True


def test_start_uses_the_recorded_child_identity_and_reuses_it(tmp_path, monkeypatch):
    pid, creation = 6541, 1234.5
    spawned = []
    monkeypatch.setattr(dashboard_service, "process_create_time", lambda actual: creation)
    monkeypatch.setattr(
        dashboard_service.psutil,
        "Process",
        lambda actual: types.SimpleNamespace(
            is_running=lambda: True, status=lambda: "running", create_time=lambda: creation
        ),
    )

    def start(command, *, env, **kwargs):
        spawned.append(command)
        write_pid_record(
            tmp_path, host="127.0.0.1", port=8765, pid=pid, instance_id=env[dashboard_service.DASHBOARD_LAUNCH_ID_ENV]
        )
        return types.SimpleNamespace(pid=9870, poll=lambda: 0)

    def health(host, port, **kwargs):
        record = read_pid_record(tmp_path)
        return {"ok": True, "host": host, "port": port, "pid": pid, "instance_id": record.instance_id}

    monkeypatch.setattr(dashboard_service.subprocess, "Popen", start)
    monkeypatch.setattr(dashboard_service, "probe_health", health)
    started = dashboard_service.start_background_service(tmp_path)
    assert started["started"] is True and started["pid"] == pid
    reused = dashboard_service.start_background_service(tmp_path)
    assert reused["running"] is True and reused["started"] is False and reused["pid"] == pid
    assert len(spawned) == 1

    monkeypatch.setattr(dashboard_service, "probe_health", lambda *a, **kw: {"ok": True, "pid": 555})
    assert dashboard_service.start_background_service(tmp_path)["running"] is None
    assert len(spawned) == 1


def test_tray_start_uses_only_create_no_window_flags(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    script = tmp_path / "dashboard-tray.ps1"
    script.write_text("# stub", encoding="utf-8")
    calls = []
    tray_state = {"running": False}
    monkeypatch.setattr(dashboard_service, "os", _OsNameProxy("nt"))
    monkeypatch.setattr(dashboard_service, "is_tray_running", lambda _home: tray_state["running"])
    monkeypatch.setattr(dashboard_service, "_powershell_executable", lambda: "powershell.exe")
    monkeypatch.setattr(dashboard_service, "bundled_tray_script", lambda: script)
    monkeypatch.setattr(dashboard_service, "resolve_bridge_command", lambda: ("agent-bridge.exe", []))

    def popen(_cmd, **kwargs):
        calls.append(kwargs)
        tray_state["running"] = True
        return types.SimpleNamespace(pid=4567)

    monkeypatch.setattr(dashboard_service.subprocess, "Popen", popen)
    result = dashboard_service.start_tray(home)
    assert result == {"ok": True, "supported": True, "running": True, "message": "系统托盘已启动"}
    assert len(calls) == 1
    creationflags = calls[0]["creationflags"]
    assert creationflags == 0x08000000
    assert not creationflags & 0x00000008
    assert not creationflags & 0x00000200
    assert calls[0]["close_fds"] is True
    assert "start_new_session" not in calls[0]


def test_tray_start_does_not_spawn_when_already_running(tmp_path: Path, monkeypatch):
    script = tmp_path / "dashboard-tray.ps1"
    script.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(dashboard_service, "os", _OsNameProxy("nt"))
    monkeypatch.setattr(dashboard_service, "is_tray_running", lambda _home: True)
    monkeypatch.setattr(dashboard_service, "_powershell_executable", lambda: "powershell.exe")
    monkeypatch.setattr(dashboard_service, "bundled_tray_script", lambda: script)
    monkeypatch.setattr(dashboard_service.subprocess, "Popen", lambda *a, **kw: pytest.fail("unexpected spawn"))
    assert dashboard_service.start_tray(tmp_path) == {
        "ok": True,
        "supported": True,
        "running": True,
        "message": "系统托盘已在运行",
    }


def test_detached_kwargs_keeps_original_flags_for_non_tray_children(monkeypatch):
    monkeypatch.setattr(dashboard_service, "os", _OsNameProxy("nt"))
    expected = 0x08000000 | 0x00000008 | 0x00000200
    assert dashboard_service._detached_kwargs() == {"creationflags": expected, "close_fds": True}
    monkeypatch.setattr(dashboard_service, "os", _OsNameProxy("posix"))
    assert dashboard_service._detached_kwargs() == {"start_new_session": True, "close_fds": True}


def test_non_tray_launch_paths_still_use_detached_kwargs(tmp_path: Path, monkeypatch):
    sentinel = {"creationflags": 0x12345678, "close_fds": True}
    calls = []
    monkeypatch.setattr(dashboard_service, "_detached_kwargs", lambda: dict(sentinel))
    monkeypatch.setattr(dashboard_service, "os", _OsNameProxy("nt"))
    monkeypatch.setattr(dashboard_service, "process_create_time", lambda _pid: 1.0)
    monkeypatch.setattr(dashboard_service, "resolve_bridge_command", lambda: ("agent-bridge.exe", []))
    monkeypatch.setattr(dashboard_service, "read_pid_record", lambda _home: None)

    def popen(_cmd, **kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(pid=4567, poll=lambda: 1)

    monkeypatch.setattr(dashboard_service.subprocess, "Popen", popen)
    dashboard_service.open_billing_folder(tmp_path)
    dashboard_service._spawn_restart_helper(tmp_path, ("127.0.0.1", 8765), log_path=tmp_path / "dashboard.log")
    dashboard_service.start_background_service(tmp_path, with_tray=False)
    assert len(calls) == 3
    for kwargs in calls:
        assert kwargs["creationflags"] == sentinel["creationflags"]
        assert kwargs["close_fds"] is True


def test_unrecorded_health_cannot_report_launch_success(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_service.subprocess, "Popen", lambda *a, **kw: types.SimpleNamespace(poll=lambda: 1))
    monkeypatch.setattr(dashboard_service, "probe_health", lambda *a, **kw: pytest.fail("unowned endpoint was probed"))
    started = dashboard_service.start_background_service(tmp_path)
    assert started["started"] is False and started["pid"] is None


@pytest.mark.parametrize("at_termination", [False, True])
def test_unconfirmed_stop_preserves_identity_and_blocks_restart(tmp_path, monkeypatch, at_termination):
    pid, creation = 6541, 1234.5
    monkeypatch.setattr(dashboard_service, "process_create_time", lambda actual: creation)
    original = write_pid_record(tmp_path, host="127.0.0.1", port=8765, pid=pid)

    def deny():
        raise psutil.AccessDenied(pid)

    def process(actual):
        if not at_termination:
            deny()
        return types.SimpleNamespace(
            is_running=lambda: True,
            status=lambda: "running",
            create_time=lambda: creation,
            terminate=deny,
        )

    monkeypatch.setattr(dashboard_service.psutil, "Process", process)
    monkeypatch.setattr(
        dashboard_service, "start_background_service", lambda *a, **kw: pytest.fail("unexpected restart")
    )
    stopped = dashboard_service.stop_background_service(tmp_path)
    assert stopped["stopped"] is False and stopped["running"] is None
    assert dashboard_service.restart_background_service(tmp_path)["running"] is None
    assert read_pid_record(tmp_path) == original


def test_detached_restart_waits_for_known_process_then_reuses_known_entry(tmp_path, monkeypatch):
    actions = []
    log_path = tmp_path / "dashboard.log"
    monkeypatch.setattr(
        dashboard_service,
        "_recorded_process",
        lambda record: types.SimpleNamespace(
            wait=lambda timeout: actions.append((record.pid, record.create_time, timeout))
        ),
    )
    command = ["test-python", ["-m", "agent_bridge"]]

    def restart(home, *, host, port, with_tray, command):
        actions.append((home, host, port, with_tray, command))
        return {"running": True}

    monkeypatch.setattr(dashboard_service, "start_background_service", restart)
    dashboard_service._run_detached_dashboard_restart(
        {
            "home": str(tmp_path),
            "log_path": str(log_path),
            "pid": 77,
            "create_time": 15.5,
            "command": command,
            "target_address": ["127.0.0.1", 8765],
        }
    )
    assert actions == [(77, 15.5, 15), (tmp_path, "127.0.0.1", 8765, False, command)]
    assert "看板已启动" in log_path.read_text(encoding="utf-8")


def test_dashboard_version_auto_backfills_new_receipts_and_service_endpoints(tmp_path: Path, monkeypatch):
    # Prevent any accidental real Windows registry / PowerShell calls during HTTP tests.
    fake_reg = _FakeRegistry()
    tray_state = {"running": False}
    monkeypatch.setattr("agent_bridge.dashboard_server.get_autostart_status", lambda h: get_autostart_status(h, platform_is_windows=True, registry=fake_reg))
    monkeypatch.setattr("agent_bridge.dashboard_server.set_autostart", lambda en, home=None: set_autostart(en, home or tmp_path, platform_is_windows=True, registry=fake_reg))
    monkeypatch.setattr("agent_bridge.dashboard_service.get_autostart_status", lambda h=None, **kw: get_autostart_status(h or tmp_path, platform_is_windows=True, registry=fake_reg))
    monkeypatch.setattr("agent_bridge.dashboard_server.is_tray_running", lambda h=None: tray_state["running"])
    monkeypatch.setattr("agent_bridge.dashboard_service.is_tray_running", lambda h=None: tray_state["running"])

    def fake_start_tray(_home: Path | None = None, **_kw: Any) -> dict[str, Any]:
        tray_state["running"] = True
        return {"ok": True, "running": True, "message": "系统托盘已启动"}

    def fake_stop_tray(_home: Path | None = None) -> dict[str, Any]:
        tray_state["running"] = False
        return {"ok": True, "running": False, "message": "系统托盘已关闭"}

    monkeypatch.setattr("agent_bridge.dashboard_server.start_tray", fake_start_tray)
    monkeypatch.setattr("agent_bridge.dashboard_server.stop_tray", fake_stop_tray)

    server = create_dashboard_server(home=tmp_path, host="127.0.0.1", port=0, backfill=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.bound_port
    try:
        _, raw_html = _request(port, "GET", "/")
        m = re.search(r'<meta name="ab-csrf" content="([^"]+)"', raw_html.decode("utf-8"))
        assert m is not None
        csrf = m.group(1)

        # 1. Health endpoint
        st, health = _json_req(port, "GET", "/api/health")
        assert st == 200 and health["ok"] is True and health["port"] == port

        # 2. Version polling detects newly written live receipt and auto-backfills it!
        st, v1 = _json_req(port, "GET", "/api/version")
        assert st == 200 and v1["ok"] is True and v1["imported_receipts"] == 0

        receipts_dir = tmp_path / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        (receipts_dir / "task_live_r1.json").write_text(
            json.dumps(
                {
                    "task_id": "task_live_r1",
                    "session_id": "sess_live",
                    "agent": "antigravity",
                    "cwd": str(tmp_path),
                    "title": "Live r1 run",
                    "status": "completed",
                    "model": "gemini-4-hydrogen-xhigh",
                    "result_preview": "PRIVATE_NEW_PREVIEW",
                    "error": "PRIVATE_NEW_ERROR",
                    "warnings": ["PRIVATE_NEW_WARNING"],
                    "usage": {
                        "input_tokens": 2000,
                        "cache_read_tokens": 8000,
                        "output_tokens": 400,
                        "scope": "turn",
                        "unrecognized": {"details": "PRIVATE_NEW_USAGE"},
                    },
                    "created_at": "2026-11-21T10:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        original_backfill = dashboard_server.backfill_from_bridge_home
        attempts = []

        def backfill(home):
            attempts.append(home)
            if len(attempts) == 1:
                raise FileLockTimeout("synthetic lock held")
            return original_backfill(home)

        monkeypatch.setattr(dashboard_server, "backfill_from_bridge_home", backfill)
        assert _json_req(port, "GET", "/api/version")[1]["imported_receipts"] == 0
        st, v2 = _json_req(port, "GET", "/api/version")
        assert st == 200 and v2["imported_receipts"] == 1
        assert _json_req(port, "GET", "/api/version")[1]["imported_receipts"] == 0
        assert attempts == [tmp_path, tmp_path]
        assert v2["revision"] != v1["revision"]
        records, _ = load_ledger(tmp_path)
        assert [r.task_id for r in records] == ["task_live_r1"]
        assert records[0].tokens.cache_read_tokens == 8000
        path = usage_ledger_path(tmp_path)
        assert b"PRIVATE_NEW" not in path.read_bytes()

        old = records[0].model_dump(mode="json")
        old.update(
            task_id="legacy",
            usage_raw={"input_tokens": 2000, "unknown": {"value": "PRIVATE_LEGACY_USAGE"}},
            warnings=["PRIVATE_LEGACY_WARNING"],
            error="PRIVATE_LEGACY_ERROR",
        )
        with path.open("ab") as sink:
            sink.write((json.dumps(old) + "\n").encode("utf-8"))
        for endpoint in ("/api/records/legacy", "/api/storage/export"):
            status, payload = _request(port, "GET", endpoint)
            assert status == 200 and b"legacy" in payload and b"PRIVATE" not in payload
        assert b"PRIVATE_LEGACY_ERROR" in path.read_bytes()

        # 3. GET /api/service
        st, svc_get = _json_req(port, "GET", "/api/service")
        assert st == 200 and svc_get["service"]["running"] is True
        assert svc_get["service"]["config"]["auto_start"] is False
        assert svc_get["service"]["config"]["tray_enabled"] is False

        # 4. PUT /api/service requires CSRF; with CSRF updates config + mocked hooks
        st, _ = _json_req(port, "PUT", "/api/service", payload={"port": 8899})
        assert st == 403

        st, svc_put = _json_req(
            port,
            "PUT",
            "/api/service",
            csrf=csrf,
            payload={"port": 8899, "auto_start": True, "tray_enabled": True},
        )
        assert st == 200 and svc_put["ok"] is True
        assert svc_put["port_restart_required"] is True
        assert svc_put["service"]["config"]["port"] == 8899
        if os.name == "nt":
            assert tray_state["running"] is True
            assert fake_reg.read_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME) is not None

        # The compatibility update route has no service or package side effects.
        actions = []
        monkeypatch.setattr(
            "agent_bridge.dashboard_server.schedule_server_stop", lambda *a, **kw: actions.append("stop")
        )
        monkeypatch.setattr(
            "agent_bridge.dashboard_server.schedule_server_restart", lambda *a, **kw: actions.append("restart")
        )
        before = (tmp_path / "billing" / "service.json").read_bytes()
        for _ in range(3):
            st, upd = _json_req(port, "POST", "/api/service/update", csrf=csrf, payload={})
            assert st == 200
            assert upd == {"ok": True, "status": "disabled", "message": "最新，别搞"}  # noqa: RUF001
        assert actions == []
        assert (tmp_path / "billing" / "service.json").read_bytes() == before
        assert _json_req(port, "GET", "/api/health")[0] == 200
        st, restarted = _json_req(port, "POST", "/api/service/restart", csrf=csrf, payload={})
        assert st == 200 and restarted["status"] == "restarting"
        assert actions == ["restart"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cli_dashboard_service_subcommands(monkeypatch, capsys):
    from agent_bridge.cli import main

    monkeypatch.setattr(
        "agent_bridge.dashboard_service.service_status",
        lambda: {"running": False, "host": "127.0.0.1", "port": 8765},
    )
    main(["dashboard", "status"])
    out = json.loads(capsys.readouterr().out)
    assert out["port"] == 8765

    def unexpected(*args, **kwargs):
        pytest.fail("compatibility update touched an operational dependency")

    monkeypatch.setattr(dashboard_service.subprocess, "run", unexpected)
    monkeypatch.setattr(dashboard_service.subprocess, "Popen", unexpected)
    monkeypatch.setattr(dashboard_service, "_tracked_record", unexpected)
    for _ in range(2):
        main(["dashboard", "update", "--json"])
        assert json.loads(capsys.readouterr().out) == {"status": "disabled", "message": "最新，别搞"}  # noqa: RUF001

    with pytest.raises(SystemExit):
        main(["dashboard", "bogus-action"])


def test_bundled_tray_script_has_utf8_bom_for_powershell_51():
    from agent_bridge.paths import bundled_tray_script

    raw = bundled_tray_script().read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "dashboard-tray.ps1 must keep UTF-8 BOM for Windows PowerShell 5.1"
    text = raw.decode("utf-8-sig")
    assert "打开面板" in text and "停止服务" in text and "更新提示" in text
