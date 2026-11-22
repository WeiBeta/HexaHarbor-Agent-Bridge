from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_bridge import dashboard_server, dashboard_service
from agent_bridge.dashboard_i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    dashboard_locales_dir,
    dashboard_preferences_path,
    load_dashboard_locale,
    load_dashboard_preferences,
    load_locale_catalog,
    normalize_locale,
    save_dashboard_preferences,
)
from agent_bridge.dashboard_server import create_dashboard_server
from agent_bridge.dashboard_service import (
    ServiceConfig,
    has_service_config,
    initialize_first_use_preferences,
    load_service_config,
    save_service_config,
)
from agent_bridge.paths import bundled_tray_script, dashboard_assets_dir

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def _flatten_catalog(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, val in data.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(val, dict):
            flat.update(_flatten_catalog(val, full))
        else:
            assert isinstance(val, str), f"catalog leaf {full} must be string, got {type(val).__name__}"
            flat[full] = val
    return flat


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, hdrs, data
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
    status, _resp_headers, raw = _request(port, method, path, headers=hdrs, body=body)
    return status, json.loads(raw.decode("utf-8"))


class _FakeRegistry:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def read_value(self, key_path: str, name: str) -> str | None:
        return self.values.get((key_path, name))

    def write_value(self, key_path: str, name: str, value: str) -> None:
        self.values[(key_path, name)] = value

    def delete_value(self, key_path: str, name: str) -> None:
        self.values.pop((key_path, name), None)


def test_locale_catalogs_have_identical_keys_and_placeholders():
    zh = _flatten_catalog(load_locale_catalog("zh-CN"))
    en = _flatten_catalog(load_locale_catalog("en"))

    assert set(zh.keys()) == set(en.keys())
    assert zh["dialogs.updateMessage"] == "最新，别搞"  # noqa: RUF001
    assert zh["tray.update_message"] == "最新，别搞"  # noqa: RUF001

    for key in sorted(zh):
        assert zh[key].strip(), f"empty zh-CN value for {key}"
        assert en[key].strip(), f"empty en value for {key}"
        zh_ph = sorted(_PLACEHOLDER_RE.findall(zh[key]))
        en_ph = sorted(_PLACEHOLDER_RE.findall(en[key]))
        assert zh_ph == en_ph, f"placeholder mismatch for {key}: zh={zh_ph} vs en={en_ph}"

    # WinForms NotifyIcon.Text hard limit is 63 characters on .NET Framework (PowerShell 5.1).
    for loc, flat in (("zh-CN", zh), ("en", en)):
        for tooltip_key in ("tray.tooltip_init", "tray.tooltip_online", "tray.tooltip_offline"):
            rendered = flat[tooltip_key].replace("{port}", "65535")
            assert len(rendered) <= 63, f"{loc} {tooltip_key} exceeds 63 chars: {rendered!r} ({len(rendered)})"


def test_all_ui_i18n_keys_exist_in_both_catalogs():
    zh = _flatten_catalog(load_locale_catalog("zh-CN"))
    en = _flatten_catalog(load_locale_catalog("en"))
    assets = dashboard_assets_dir()
    html = (assets / "index.html").read_text(encoding="utf-8")
    app_js = (assets / "app.js").read_text(encoding="utf-8")
    i18n_js = (assets / "i18n.js").read_text(encoding="utf-8")

    html_keys = set(re.findall(r'data-i18n(?:-[a-z-]+)?="([a-zA-Z0-9_.]+)"', html))
    js_keys = set(re.findall(r'\bt(?:Html)?\(\s*"([a-zA-Z0-9_.]+)"', app_js))
    js_keys.update(re.findall(r'\bt\(\s*"([a-zA-Z0-9_.]+)"', i18n_js))
    js_keys.update(re.findall(r':\s*"(backend\.[a-zA-Z0-9_]+)"', i18n_js))
    js_keys.update(re.findall(r'(?:titleKey|messageKey|detailKey):\s*"([a-zA-Z0-9_.]+)"', app_js))

    referenced = html_keys | js_keys
    assert referenced, "expected i18n keys in index.html / app.js"
    missing_zh = sorted(k for k in referenced if k not in zh)
    missing_en = sorted(k for k in referenced if k not in en)
    assert missing_zh == [], f"missing keys in zh-CN.json: {missing_zh}"
    assert missing_en == [], f"missing keys in en.json: {missing_en}"


def test_preferences_storage_edge_cases_and_independence_from_service_json(tmp_path: Path):
    # 1. Missing file defaults to zh-CN and creates nothing on disk.
    assert load_dashboard_locale(tmp_path) == DEFAULT_LOCALE
    assert not (tmp_path / "billing" / "preferences.json").exists()
    assert has_service_config(tmp_path) is False

    # 2. Saving locale creates preferences.json only, NEVER service.json.
    saved = save_dashboard_preferences(home=tmp_path, locale="en")
    assert saved.locale == "en"
    assert load_dashboard_locale(tmp_path) == "en"
    assert has_service_config(tmp_path) is False

    # 3. Windows first-use defaults still trigger independently after language switch!
    fake_reg = _FakeRegistry()
    svc_cfg = initialize_first_use_preferences(tmp_path, platform_is_windows=True, registry=fake_reg)
    assert svc_cfg.auto_start is True and svc_cfg.tray_enabled is True
    assert load_dashboard_locale(tmp_path) == "en"

    # 4. Saving service.json preserves preferences.json, and vice versa.
    save_service_config(ServiceConfig(port=9123, auto_start=False, tray_enabled=True), tmp_path)
    assert load_dashboard_locale(tmp_path) == "en"
    save_dashboard_preferences(home=tmp_path, locale="zh-CN")
    reloaded_svc = load_service_config(tmp_path)
    assert reloaded_svc.port == 9123 and reloaded_svc.auto_start is False and reloaded_svc.tray_enabled is True

    # 5. Partial fields, extra unknown fields, invalid locales, and corrupt JSON.
    prefs_path = dashboard_preferences_path(tmp_path)

    prefs_path.write_text("{}", encoding="utf-8")
    assert load_dashboard_preferences(tmp_path).locale == "zh-CN"

    prefs_path.write_text(json.dumps({"schema_version": 1, "custom_flag": True}), encoding="utf-8")
    assert load_dashboard_preferences(tmp_path).locale == "zh-CN"
    save_dashboard_preferences(home=tmp_path, locale="en")
    raw_after = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert raw_after["locale"] == "en" and raw_after["custom_flag"] is True

    for invalid_payload in (
        {"locale": "fr-FR"},
        {"locale": ""},
        {"locale": None},
        {"locale": 123},
        {"locale": True},
    ):
        prefs_path.write_text(json.dumps(invalid_payload), encoding="utf-8")
        assert load_dashboard_locale(tmp_path) == "zh-CN"

    prefs_path.write_text("{corrupt-json", encoding="utf-8")
    assert load_dashboard_locale(tmp_path) == "zh-CN"

    # 6. Strict vs lenient normalization.
    assert normalize_locale("en-US", strict=False) == "en"
    assert normalize_locale("zh_CN", strict=False) == "zh-CN"
    assert normalize_locale("unknown", strict=False) == "zh-CN"
    assert normalize_locale("EN", strict=True) == "en"
    assert normalize_locale("zh-cn", strict=True) == "zh-CN"
    with pytest.raises(ValueError, match="unsupported locale"):
        normalize_locale("fr", strict=True)
    with pytest.raises(ValueError, match="unsupported locale"):
        normalize_locale(None, strict=True)


def test_preferences_http_endpoints_security_and_roundtrip(tmp_path: Path, monkeypatch):
    def _fail_side_effect(*_a: Any, **_kw: Any) -> Any:
        pytest.fail("language switch must not touch autostart, tray, or service lifecycle")

    monkeypatch.setattr(dashboard_server, "set_autostart", _fail_side_effect)
    monkeypatch.setattr(dashboard_server, "start_tray", _fail_side_effect)
    monkeypatch.setattr(dashboard_server, "stop_tray", _fail_side_effect)
    monkeypatch.setattr(dashboard_server, "schedule_server_restart", _fail_side_effect)
    monkeypatch.setattr(dashboard_server, "schedule_server_stop", _fail_side_effect)

    server = create_dashboard_server(home=tmp_path, host="127.0.0.1", port=0, backfill=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.bound_port
    try:
        # 1. Default HTML + static allowlist
        st, _hdrs, raw_html = _request(port, "GET", "/")
        assert st == 200
        html = raw_html.decode("utf-8")
        assert '<html lang="zh-CN"' in html
        assert '<meta name="ab-locale" content="zh-CN"' in html
        m = re.search(r'<meta name="ab-csrf" content="([^"]+)"', html)
        assert m is not None
        csrf = m.group(1)

        assert _request(port, "GET", "/assets/i18n.js")[0] == 200
        assert _request(port, "GET", "/assets/locales/zh-CN.json")[0] == 200
        assert _request(port, "GET", "/assets/locales/en.json")[0] == 200
        assert _request(port, "GET", "/assets/locales/fr.json")[0] == 404
        assert _request(port, "GET", "/assets/locales/../../cli.py")[0] == 404

        # 2. Read-only GET /api/preferences and /api/meta do not create service.json or preferences.json
        st, prefs = _json_req(port, "GET", "/api/preferences")
        assert st == 200 and prefs["preferences"]["locale"] == "zh-CN"
        assert prefs["supported_locales"] == list(SUPPORTED_LOCALES)
        st, meta = _json_req(port, "GET", "/api/meta")
        assert st == 200 and meta["locale"] == "zh-CN"
        st, ver = _json_req(port, "GET", "/api/version")
        assert st == 200 and ver["locale"] == "zh-CN"
        assert has_service_config(tmp_path) is False

        # 3. CSRF / Origin / Host guards enforced on PUT /api/preferences
        assert _json_req(port, "PUT", "/api/preferences", payload={"locale": "en"})[0] == 403
        assert (
            _json_req(
                port,
                "PUT",
                "/api/preferences",
                csrf=csrf,
                payload={"locale": "en"},
                extra_headers={"Origin": "http://evil.example"},
            )[0]
            == 403
        )
        assert (
            _request(port, "GET", "/api/preferences", headers={"Host": "evil.example"})[0]
            == 403
        )

        # 4. Invalid locale returns 400
        st, err = _json_req(port, "PUT", "/api/preferences", csrf=csrf, payload={"locale": "ja"})
        assert st == 400 and err["ok"] is False

        # 5. CN -> EN -> CN round-trip persists and injects into HTML without creating service.json
        st, res_en = _json_req(port, "PUT", "/api/preferences", csrf=csrf, payload={"locale": "en"})
        assert st == 200 and res_en["preferences"]["locale"] == "en"
        assert has_service_config(tmp_path) is False
        _, _, raw_en_html = _request(port, "GET", "/")
        en_html = raw_en_html.decode("utf-8")
        assert '<html lang="en"' in en_html
        assert '<meta name="ab-locale" content="en"' in en_html
        assert _json_req(port, "GET", "/api/version")[1]["locale"] == "en"

        st, res_zh = _json_req(port, "PUT", "/api/preferences", csrf=csrf, payload={"locale": "zh-CN"})
        assert st == 200 and res_zh["preferences"]["locale"] == "zh-CN"
        _, _, raw_zh_html = _request(port, "GET", "/")
        assert '<meta name="ab-locale" content="zh-CN"' in raw_zh_html.decode("utf-8")
        assert has_service_config(tmp_path) is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell tray tests run on Windows only")
def test_tray_script_powershell_51_syntax_and_live_online_offline_i18n(tmp_path: Path):
    ps51 = shutil.which("powershell.exe")
    if not ps51:
        pytest.skip("Windows PowerShell 5.1 (powershell.exe) not found")

    tray_script = bundled_tray_script()
    raw_tray = tray_script.read_bytes()
    assert raw_tray.startswith(b"\xef\xbb\xbf") and not raw_tray.startswith(b"\xef\xbb\xbf\xef\xbb\xbf")
    locales_dir = dashboard_locales_dir()

    # Also verify clean syntax under PowerShell 7 (pwsh) when installed.
    pwsh = shutil.which("pwsh")
    if pwsh:
        escaped = str(tray_script).replace("'", "''")
        pwsh_check = subprocess.run(
            [
                pwsh,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('{escaped}',[ref]$null,[ref]$e);if($e.Count -gt 0){{throw ($e.Message -join '; ')}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert pwsh_check.returncode == 0, pwsh_check.stderr

    # 1. Start a real isolated local dashboard server so the live WinForms tray probes it online, then stops it offline.
    server = create_dashboard_server(home=tmp_path, host="127.0.0.1", port=0, backfill=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.bound_port

    driver_ps1 = tmp_path / "tray_i18n_driver.ps1"
    driver_ps1.write_text(
        r"""
param(
  [string]$TrayScript,
  [string]$LocaleDir,
  [string]$TestHome,
  [int]$TestPort,
  [string]$StageSignalFile,
  [string]$ReadyOfflineFile,
  [string]$OutJson
)
$ErrorActionPreference = "Stop"

# Verify clean PowerShell 5.1 AST parsing of dashboard-tray.ps1 first.
$tokens = $null
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($TrayScript, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors -and $parseErrors.Count -gt 0) {
  throw ("Parse errors in tray script: " + (($parseErrors | ForEach-Object { $_.Message }) -join "; "))
}

# Dot-source the production tray script and create a real WinForms tray controller.
. $TrayScript

function Capture-Snapshot($Ctrl) {
  $notice = Get-TrayUpdateNotice $Ctrl
  return @{
    locale         = $Ctrl.Locale
    online_state   = $Ctrl.OnlineState
    tooltip        = $Ctrl.Notify.Text
    menu_open      = $Ctrl.ItemOpen.Text
    menu_start     = $Ctrl.ItemStart.Text
    menu_stop      = $Ctrl.ItemStop.Text
    menu_restart   = $Ctrl.ItemRestart.Text
    menu_update    = $Ctrl.ItemUpdate.Text
    status_text    = $Ctrl.ItemStatus.Text
    menu_folder    = $Ctrl.ItemFolder.Text
    menu_quit      = $Ctrl.ItemQuit.Text
    start_enabled  = [bool]$Ctrl.ItemStart.Enabled
    stop_enabled   = [bool]$Ctrl.ItemStop.Enabled
    update_title   = $notice.Title
    update_message = $notice.Message
  }
}

$results = @{}
$ctrl = New-TrayController -Exe "python.exe" -PrefixArgs @() -HomeDir $TestHome -DefaultHost "127.0.0.1" -DefaultPort $TestPort -LocaleDir $LocaleDir -ShowNotifyIcon $false
try {
  # Stage 1: Initial checking state (default zh-CN)
  $results["checking_zh"] = Capture-Snapshot $ctrl

  # Stage 2: Online probe in zh-CN
  Refresh-TrayStatus $ctrl
  $results["online_zh"] = Capture-Snapshot $ctrl

  # Stage 3: Switch preferences.json to EN while online -> trigger real ContextMenuStrip.Opening handler
  $prefsPath = Join-Path (Join-Path $TestHome "billing") "preferences.json"
  [System.IO.File]::WriteAllText($prefsPath, '{"schema_version":1,"locale":"en"}', (New-Object System.Text.UTF8Encoding($false)))
  $openingMethod = $ctrl.Menu.GetType().GetMethod("OnOpening", [System.Reflection.BindingFlags]"Instance,NonPublic")
  $cancelArgs = New-Object System.ComponentModel.CancelEventArgs
  $openingMethod.Invoke($ctrl.Menu, [object[]]@($cancelArgs.PSObject.BaseObject))
  $results["online_en_on_open"] = Capture-Snapshot $ctrl

  # Stage 4: Real WinForms Timer tick inside Application::Run() message loop refreshes status & tooltip in EN
  $loopTimer = New-Object System.Windows.Forms.Timer
  $loopTimer.Interval = 50
  $loopTimer.Tag = $ctrl
  $loopTimer.Add_Tick({
    param($sender, $eventArgs)
    $null = $eventArgs
    $sender.Stop()
    Refresh-TrayStatus $sender.Tag
    [System.Windows.Forms.Application]::ExitThread()
  })
  $loopTimer.Start()
  [System.Windows.Forms.Application]::Run()
  $loopTimer.Dispose()
  $results["online_en_refresh"] = Capture-Snapshot $ctrl

  # Stage 5: Signal Python to shut down the local server, then wait until server is stopped (offline).
  [System.IO.File]::WriteAllText($StageSignalFile, "shutdown-server", (New-Object System.Text.UTF8Encoding($false)))
  $deadline = [DateTime]::UtcNow.AddSeconds(10)
  while (-not [System.IO.File]::Exists($ReadyOfflineFile) -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 50
  }
  if (-not [System.IO.File]::Exists($ReadyOfflineFile)) {
    throw "Timed out waiting for dashboard server shutdown"
  }

  # Stage 6: Server is now OFFLINE; tray preserves last locale (EN)
  Refresh-TrayStatus $ctrl
  $results["offline_en"] = Capture-Snapshot $ctrl

  # Stage 7: Switch back to zh-CN while server is OFFLINE; menu Opening syncs immediately without HTTP
  [System.IO.File]::WriteAllText($prefsPath, '{"schema_version":1,"locale":"zh-CN"}', (New-Object System.Text.UTF8Encoding($false)))
  $cancelArgs2 = New-Object System.ComponentModel.CancelEventArgs
  $openingMethod.Invoke($ctrl.Menu, [object[]]@($cancelArgs2.PSObject.BaseObject))
  $results["offline_zh_on_open"] = Capture-Snapshot $ctrl

  # Stage 8: Invalid / partial / corrupt preferences.json falls back cleanly to zh-CN
  [System.IO.File]::WriteAllText($prefsPath, '{"locale":"en"}', (New-Object System.Text.UTF8Encoding($false)))
  Apply-TrayLocale $ctrl
  $results["partial_en"] = Capture-Snapshot $ctrl

  [System.IO.File]::WriteAllText($prefsPath, '{"locale":"bogus"}', (New-Object System.Text.UTF8Encoding($false)))
  Apply-TrayLocale $ctrl
  $results["invalid_fallback_zh"] = Capture-Snapshot $ctrl

  [System.IO.File]::WriteAllText($prefsPath, '{corrupt', (New-Object System.Text.UTF8Encoding($false)))
  Apply-TrayLocale $ctrl
  $results["corrupt_fallback_zh"] = Capture-Snapshot $ctrl

  # Stage 9: Fresh controller with persisted EN starts directly in EN checking state
  [System.IO.File]::WriteAllText($prefsPath, '{"schema_version":1,"locale":"en"}', (New-Object System.Text.UTF8Encoding($false)))
  $ctrl2 = New-TrayController -Exe "python.exe" -PrefixArgs @() -HomeDir $TestHome -DefaultHost "127.0.0.1" -DefaultPort $TestPort -LocaleDir $LocaleDir -ShowNotifyIcon $false
  try {
    $results["checking_en"] = Capture-Snapshot $ctrl2
  } finally {
    Dispose-TrayController $ctrl2
  }
} finally {
  Dispose-TrayController $ctrl
}

$json = $results | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($OutJson, $json, (New-Object System.Text.UTF8Encoding($false)))
""",
        encoding="utf-8-sig",
    )

    signal_file = tmp_path / "signal_shutdown.txt"
    ready_offline = tmp_path / "server_stopped.txt"
    out_json = tmp_path / "tray_results.json"

    proc = subprocess.Popen(
        [
            ps51,
            "-NoProfile",
            "-NonInteractive",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(driver_ps1),
            "-TrayScript",
            str(tray_script),
            "-LocaleDir",
            str(locales_dir),
            "-TestHome",
            str(tmp_path),
            "-TestPort",
            str(port),
            "-StageSignalFile",
            str(signal_file),
            "-ReadyOfflineFile",
            str(ready_offline),
            "-OutJson",
            str(out_json),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    server_closed = False
    try:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if not server_closed and signal_file.is_file():
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                server_closed = True
                ready_offline.write_text("stopped", encoding="utf-8")
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 0, f"PS 5.1 tray driver failed:\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    finally:
        if not server_closed:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    res = json.loads(out_json.read_text(encoding="utf-8"))

    assert res["checking_zh"]["status_text"] == "服务: 检测中..."
    assert res["checking_zh"]["tooltip"] == "六库仙贼/HexaHarbor 看板"
    assert res["checking_zh"]["menu_open"] == "打开面板"

    assert res["checking_en"]["status_text"] == "Service: Checking..."
    assert res["checking_en"]["tooltip"] == "HexaHarbor Dashboard"
    assert res["checking_en"]["menu_open"] == "Open Dashboard"

    assert res["online_zh"]["online_state"] == "online"
    assert res["online_zh"]["tooltip"] == f"HexaHarbor 看板: 在线 ({port})"
    assert res["online_zh"]["status_text"] == f"服务: 在线 (端口 {port})"
    assert res["online_zh"]["update_title"] == "六库仙贼/HexaHarbor 看板"
    assert res["online_zh"]["update_message"] == "最新，别搞"  # noqa: RUF001
    assert res["online_zh"]["start_enabled"] is False and res["online_zh"]["stop_enabled"] is True

    for stage in ("online_en_on_open", "online_en_refresh"):
        snap = res[stage]
        assert snap["locale"] == "en"
        assert snap["tooltip"] == f"HexaHarbor Dashboard: Online ({port})"
        assert snap["status_text"] == f"Service: Online (Port {port})"
        assert snap["menu_open"] == "Open Dashboard"
        assert snap["menu_start"] == "Start Service"
        assert snap["menu_stop"] == "Stop Service"
        assert snap["menu_restart"] == "Restart Service"
        assert snap["menu_update"] == "Update Notice"
        assert snap["menu_folder"] == "Open Logs Folder"
        assert snap["menu_quit"] == "Quit Tray"
        assert snap["update_title"] == "HexaHarbor Dashboard"
        assert snap["update_message"] == "Already up to date — don't mess with it"

    assert res["offline_en"]["online_state"] == "offline"
    assert res["offline_en"]["locale"] == "en"
    assert res["offline_en"]["tooltip"] == "HexaHarbor Dashboard: Stopped"
    assert res["offline_en"]["status_text"] == "Service: Stopped"
    assert res["offline_en"]["start_enabled"] is True and res["offline_en"]["stop_enabled"] is False

    assert res["offline_zh_on_open"]["online_state"] == "offline"
    assert res["offline_zh_on_open"]["locale"] == "zh-CN"
    assert res["offline_zh_on_open"]["tooltip"] == "HexaHarbor 看板: 已停止"
    assert res["offline_zh_on_open"]["status_text"] == "服务: 已停止"
    assert res["offline_zh_on_open"]["menu_open"] == "打开面板"
    assert res["offline_zh_on_open"]["update_message"] == "最新，别搞"  # noqa: RUF001

    assert res["partial_en"]["locale"] == "en"
    assert res["invalid_fallback_zh"]["locale"] == "zh-CN"
    assert res["corrupt_fallback_zh"]["locale"] == "zh-CN"


@pytest.mark.skipif(os.name != "nt", reason="Windows tray process test runs on Windows only")
def test_tray_running_process_starts_survives_locale_change_and_stops_cleanly(tmp_path: Path):
    ps51 = shutil.which("powershell.exe")
    if not ps51:
        pytest.skip("Windows PowerShell 5.1 (powershell.exe) not found")

    # Start real background tray process on isolated home + ephemeral port, switch locale, stop cleanly.
    res_start = dashboard_service.start_tray(tmp_path, host="127.0.0.1", port=18765)
    try:
        assert res_start["ok"] is True
        assert dashboard_service.is_tray_running(tmp_path) is True
        save_dashboard_preferences(home=tmp_path, locale="en")
        time.sleep(0.3)
        assert dashboard_service.is_tray_running(tmp_path) is True
        save_dashboard_preferences(home=tmp_path, locale="zh-CN")
        time.sleep(0.3)
        assert dashboard_service.is_tray_running(tmp_path) is True
    finally:
        res_stop = dashboard_service.stop_tray(tmp_path)
    assert res_stop["ok"] is True
    assert res_stop["running"] is False
    assert dashboard_service.is_tray_running(tmp_path) is False
    assert not (tmp_path / "billing" / "tray.pid").exists()
    assert sys.platform == "win32"


def test_i18n_js_backend_message_translation_preserves_raw_diagnostics():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute i18n.js helper test")
    assets = dashboard_assets_dir()
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const assetsDir = process.argv[1];
const catalogs = {
  "zh-CN": JSON.parse(fs.readFileSync(path.join(assetsDir, "locales", "zh-CN.json"), "utf8")),
  "en": JSON.parse(fs.readFileSync(path.join(assetsDir, "locales", "en.json"), "utf8")),
};

const sandbox = {
  window: {},
  document: {
    documentElement: { lang: "zh-CN", setAttribute() {} },
    title: "",
    querySelectorAll: () => [],
  },
  fetch: async (url) => {
    const loc = url.includes("en.json") ? "en" : "zh-CN";
    return { ok: true, status: 200, json: async () => catalogs[loc] };
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(assetsDir, "i18n.js"), "utf8"), sandbox);

(async () => {
  const i18n = sandbox.window.AbI18n;
  await i18n.init("en");

  // Project-produced backend messages translate cleanly in EN
  assert.strictEqual(i18n.translateBackendText("已开启开机自启 · 系统托盘已启动"), "Autostart enabled · System tray started");
  assert.strictEqual(
    i18n.translateBackendText("端口已保存为 9001\uff0c点击「重启服务」后生效"),
    'Port saved as 9001; click "Restart Service" to apply'
  );
  assert.strictEqual(
    i18n.translateBackendText("开机自启已被另一个工作目录占用"),
    "Autostart is already in use by another Bridge home"
  );
  assert.strictEqual(i18n.translateBackendText("unknown task task_xyz"), "Unknown task task_xyz");
  assert.strictEqual(i18n.translateDiscountLabel("ZCode GLM 150% 额度加成", "preset-zcode-glm-150"), "ZCode GLM 150% Quota Boost");

  // User labels, model names, paths, and raw diagnostics must NOT be mistranslated
  assert.strictEqual(i18n.translateDiscountLabel("我的自定义折扣", "user-1"), "我的自定义折扣");
  const rawDiag = "OSError: [WinError 5] Access is denied: 'C:\\\\Users\\\\demo\\\\billing\\\\pricing.json'";
  assert.strictEqual(i18n.translateBackendText(rawDiag), rawDiag);
  assert.strictEqual(i18n.translateBackendText("recorded_in_receipt"), "recorded_in_receipt");

  // Round-trip back to zh-CN
  await i18n.setLocale("zh-CN");
  assert.strictEqual(i18n.translateBackendText("missing or invalid X-Bridge-CSRF token"), "缺少或无效的 CSRF 令牌\uff08请刷新页面后重试\uff09");
  assert.strictEqual(i18n.translateDiscountLabel("ZCode GLM 150% 额度加成", "preset-zcode-glm-150"), "ZCode GLM 150% 额度加成");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [node, "-e", script, str(assets)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
