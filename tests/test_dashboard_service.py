from __future__ import annotations

import http.client
import json
import os
import re
import threading
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_bridge.billing_store import load_ledger
from agent_bridge.dashboard_server import create_dashboard_server
from agent_bridge.dashboard_service import (
    AUTOSTART_REG_PATH,
    AUTOSTART_VALUE_NAME,
    ServiceConfig,
    check_update_available,
    clear_pid_record,
    get_autostart_status,
    is_pid_record_alive,
    load_service_config,
    read_pid_record,
    save_service_config,
    set_autostart,
    write_pid_record,
)


class _FakeRegistry:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def read_value(self, key_path: str, name: str) -> str | None:
        return self.values.get((key_path, name))

    def write_value(self, key_path: str, name: str, value: str) -> None:
        self.values[(key_path, name)] = value

    def delete_value(self, key_path: str, name: str) -> None:
        self.values.pop((key_path, name), None)


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

    saved = save_service_config(ServiceConfig(host="localhost", port=9100, auto_start=False, tray_enabled=True), tmp_path)
    assert saved.host == "localhost"
    reloaded = load_service_config(tmp_path)
    assert reloaded.port == 9100 and reloaded.tray_enabled is True

    with pytest.raises(ValidationError):
        ServiceConfig(host="0.0.0.0")
    with pytest.raises(ValidationError):
        ServiceConfig(port=80)


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
    assert st_drift["enabled"] is True
    assert st_drift["matches_current"] is False

    # Disable removes registry entry and VBS file cleanly.
    st2 = set_autostart(False, tmp_path, platform_is_windows=True, registry=fake_reg)
    assert st2["enabled"] is False
    assert fake_reg.read_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME) is None
    assert not vbs_file.exists()

    # Non-Windows reports unsupported without touching registry.
    st_linux = set_autostart(True, tmp_path, platform_is_windows=False, registry=fake_reg)
    assert st_linux["supported"] is False and st_linux["enabled"] is False


def test_check_update_available_scenarios(monkeypatch):
    sha_a = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    sha_b = "11223344556677889900aabbccddeeff00112233"

    def make_runner(remote_sha: str, *, dirty: bool = False):
        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            if "rev-parse" in cmd and "HEAD" in cmd:
                return types.SimpleNamespace(returncode=0, stdout=sha_a + "\n")
            if "rev-parse" in cmd and "@{u}" in cmd:
                return types.SimpleNamespace(returncode=0, stdout="origin/main\n")
            if "ls-remote" in cmd:
                return types.SimpleNamespace(returncode=0, stdout=f"{remote_sha}\trefs/heads/main\n")
            if "status" in cmd and "--porcelain" in cmd:
                return types.SimpleNamespace(returncode=0, stdout=" M foo.py\n" if dirty else "")
            return types.SimpleNamespace(returncode=1, stdout="")

        return fake_run

    # 1. Up-to-date returns exact concise up-to-date prompt.
    res_same = check_update_available(kind="checkout", run=make_runner(sha_a), siblings=0)
    assert res_same["status"] == "up_to_date"
    assert res_same["up_to_date"] is True
    assert "最新" in res_same["message"] and "别搞" in res_same["message"]

    # 2. Newer commit + dirty working tree -> blocked.
    res_dirty = check_update_available(kind="checkout", run=make_runner(sha_b, dirty=True), siblings=0)
    assert res_dirty["status"] == "blocked"

    # 3. Newer commit + live sibling MCP Bridge instances -> blocked.
    res_sib = check_update_available(kind="checkout", run=make_runner(sha_b, dirty=False), siblings=2)
    assert res_sib["status"] == "blocked"
    assert "2" in res_sib["message"]

    # 4. Newer commit + clean tree + no sibling MCP Bridge -> update_available.
    res_avail = check_update_available(kind="checkout", run=make_runner(sha_b, dirty=False), siblings=0)
    assert res_avail["status"] == "update_available"
    assert res_avail["current_commit"] == sha_a[:8]
    assert res_avail["remote_commit"] == sha_b[:8]

    # 5. uv-tool up-to-date check via direct_url.json.
    monkeypatch.setattr("agent_bridge.dashboard_service._installed_direct_url", lambda: ("https://example/repo.git", sha_a))
    res_uv = check_update_available(kind="uv-tool", run=make_runner(sha_a), siblings=0)
    assert res_uv["status"] == "up_to_date"
    assert "最新" in res_uv["message"] and "别搞" in res_uv["message"]

    # 6. Unknown install kind -> unsupported.
    res_unk = check_update_available(kind="unknown", run=make_runner(sha_a), siblings=0)
    assert res_unk["status"] == "unsupported"


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
                    "usage": {"input_tokens": 2000, "cache_read_tokens": 8000, "output_tokens": 400, "scope": "turn"},
                    "created_at": "2026-11-21T10:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        st, v2 = _json_req(port, "GET", "/api/version")
        assert st == 200 and v2["imported_receipts"] == 1
        assert v2["revision"] != v1["revision"]
        records, _ = load_ledger(tmp_path)
        assert [r.task_id for r in records] == ["task_live_r1"]

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

        # 5. POST /api/service/update when up-to-date returns cleanly without shutting down server
        monkeypatch.setattr(
            "agent_bridge.dashboard_service.check_update_available",
            lambda **_kw: {
                "status": "up_to_date",
                "up_to_date": True,
                "install_kind": "checkout",
                "current_commit": "abcdef12",
                "remote_commit": "abcdef12",
                "message": "最新，别搞（当前已对齐远端 abcdef12）",  # noqa: RUF001
            },
        )
        st, upd = _json_req(port, "POST", "/api/service/update", csrf=csrf, payload={})
        assert st == 200 and upd["status"] == "up_to_date"
        assert "最新" in upd["message"] and "别搞" in upd["message"]
        # Server is still healthy after up-to-date response.
        st_h, _ = _json_req(port, "GET", "/api/health")
        assert st_h == 200
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

    monkeypatch.setattr(
        "agent_bridge.dashboard_service.cli_update_and_restart",
        lambda: {"status": "up_to_date", "up_to_date": True, "message": "最新，别搞"},  # noqa: RUF001
    )
    main(["dashboard", "update", "--json"])
    upd = json.loads(capsys.readouterr().out)
    assert upd["status"] == "up_to_date"

    with pytest.raises(SystemExit):
        main(["dashboard", "bogus-action"])


def test_bundled_tray_script_has_utf8_bom_for_powershell_51():
    from agent_bridge.paths import bundled_tray_script

    raw = bundled_tray_script().read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "dashboard-tray.ps1 must keep UTF-8 BOM for Windows PowerShell 5.1"
    text = raw.decode("utf-8-sig")
    assert "打开面板" in text and "停止服务" in text and "重启并更新" in text
