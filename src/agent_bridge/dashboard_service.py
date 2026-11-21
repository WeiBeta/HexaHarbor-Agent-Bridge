"""Background daemon lifecycle, Windows autostart, and system tray.

Zero extra third-party dependencies:
- Background process management uses stdlib ``subprocess`` + existing ``psutil``.
- Windows autostart uses user-scoped ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
  (no admin/UAC required, disabled by default).
- Windows system tray delegates to a bundled native WinForms PowerShell script
  (``share/dashboard-tray.ps1``), inspired by ``@bitkyc08/opencodex`` (MIT).
"""

from __future__ import annotations

import contextlib
import http.client
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import psutil
from pydantic import BaseModel, Field, field_validator

from agent_bridge.cli import detect_install
from agent_bridge.paths import (
    billing_dir,
    bridge_home,
    bundled_tray_script,
    dashboard_log_path,
    dashboard_pid_path,
    service_config_path,
)
from agent_bridge.persist import atomic_write_json, atomic_write_text, file_lock, read_json
from agent_bridge.processes import process_create_time

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
DASHBOARD_LAUNCH_ID_ENV = "AGENT_BRIDGE_DASHBOARD_LAUNCH_ID"
DEFAULT_SERVICE_HOST = "127.0.0.1"
DEFAULT_SERVICE_PORT = 8765
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "HexaHarborAgentBridgeDashboard"
LOCK_TIMEOUT_SEC = 5.0


class ServiceConfig(BaseModel):
    """Persistent service preferences stored at ``~/.agent-bridge/billing/service.json``."""

    schema_version: int = 1
    host: str = DEFAULT_SERVICE_HOST
    port: int = Field(default=DEFAULT_SERVICE_PORT, ge=1024, le=65535)
    tray_enabled: bool = False
    auto_start: bool = False

    @field_validator("host")
    @classmethod
    def _validate_loopback(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if cleaned not in LOOPBACK_HOSTS:
            raise ValueError(f"dashboard requires IPv4 loopback (127.0.0.1 or localhost); got {v!r}")
        return cleaned


class DashboardPidRecord(BaseModel):
    pid: int
    create_time: float | None = None
    host: str = DEFAULT_SERVICE_HOST
    port: int = DEFAULT_SERVICE_PORT
    started_at: str = ""
    executable: str = ""
    install_kind: str = "unknown"
    instance_id: str | None = None


class RegistryBackend(Protocol):
    def read_value(self, key_path: str, name: str) -> str | None: ...

    def write_value(self, key_path: str, name: str, value: str) -> None: ...

    def delete_value(self, key_path: str, name: str) -> None: ...


class _WinRegBackend:
    """Real HKCU registry backend loaded lazily on Windows only."""

    def read_value(self, key_path: str, name: str) -> str | None:
        winreg = importlib.import_module("winreg")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                value, _kind = winreg.QueryValueEx(key, name)
                return str(value)
        except FileNotFoundError:
            return None

    def write_value(self, key_path: str, name: str, value: str) -> None:
        winreg = importlib.import_module("winreg")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete_value(self, key_path: str, name: str) -> None:
        winreg = importlib.import_module("winreg")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return


def _service_lock(home: Path) -> Path:
    return service_config_path(home).with_name("service.json.lock")


def load_service_config(home: Path | None = None) -> ServiceConfig:
    resolved = home or bridge_home()
    path = service_config_path(resolved)
    if not path.is_file():
        return ServiceConfig()
    raw = read_json(path, None)
    if not isinstance(raw, dict):
        return ServiceConfig()
    try:
        return ServiceConfig.model_validate(raw)
    except Exception:
        log.warning("invalid service.json at %s; using defaults", path, exc_info=True)
        return ServiceConfig()


def has_service_config(home: Path | None = None) -> bool:
    """Return whether this home has a saved preference file, including explicit false values."""
    return service_config_path(home or bridge_home()).is_file()


def initialize_first_use_preferences(
    home: Path,
    *,
    platform_is_windows: bool | None = None,
    registry: RegistryBackend | None = None,
) -> ServiceConfig:
    """Apply the Windows first-use defaults once, then preserve every saved choice."""
    is_win = (os.name == "nt") if platform_is_windows is None else platform_is_windows
    if not is_win or has_service_config(home):
        return load_service_config(home)
    cfg = ServiceConfig(tray_enabled=True, auto_start=True)
    try:
        set_autostart(True, home, platform_is_windows=True, registry=registry)
    except ValueError as exc:
        # Another Bridge home owns the single Run entry; leave it untouched.
        cfg = ServiceConfig(tray_enabled=True, auto_start=False)
        log.warning("first-use autostart not enabled: %s", exc)
    save_service_config(cfg, home)
    return cfg


def save_service_config(config: ServiceConfig, home: Path | None = None) -> ServiceConfig:
    resolved = home or bridge_home()
    validated = ServiceConfig.model_validate(config.model_dump(mode="json"))
    path = service_config_path(resolved)
    with file_lock(_service_lock(resolved), timeout_sec=LOCK_TIMEOUT_SEC):
        atomic_write_json(path, validated.model_dump(mode="json"))
    return validated


def resolve_bridge_command() -> tuple[str, list[str]]:
    """Return ``(executable, prefix_args)`` for spawning ``agent-bridge`` subcommands."""
    exe_dir = Path(sys.executable).resolve().parent
    names = ("agent-bridge.exe", "agent-bridge") if os.name == "nt" else ("agent-bridge",)
    for name in names:
        candidate = exe_dir / name
        if candidate.is_file():
            return str(candidate), []
    return str(Path(sys.executable).resolve()), ["-m", "agent_bridge"]


def write_pid_record(
    home: Path,
    *,
    host: str,
    port: int,
    pid: int | None = None,
    instance_id: str | None = None,
) -> DashboardPidRecord:
    actual_pid = os.getpid() if pid is None else pid
    exe, _prefix = resolve_bridge_command()
    record = DashboardPidRecord(
        pid=actual_pid,
        create_time=process_create_time(actual_pid),
        host=host,
        port=port,
        started_at=datetime.now(UTC).isoformat(),
        executable=exe,
        install_kind=detect_install(),
        instance_id=instance_id,
    )
    atomic_write_json(dashboard_pid_path(home), record.model_dump(mode="json"))
    return record


def read_pid_record(home: Path | None = None) -> DashboardPidRecord | None:
    resolved = home or bridge_home()
    path = dashboard_pid_path(resolved)
    if not path.is_file():
        return None
    raw = read_json(path, None)
    if not isinstance(raw, dict):
        return None
    try:
        return DashboardPidRecord.model_validate(raw)
    except Exception:
        return None


def clear_pid_record(
    home: Path | None = None,
    *,
    expected_pid: int | None = None,
    expected_record: DashboardPidRecord | None = None,
) -> None:
    resolved = home or bridge_home()
    path = dashboard_pid_path(resolved)
    if not path.is_file():
        return
    if expected_record is not None:
        if read_pid_record(resolved) != expected_record:
            return
    elif expected_pid is not None:
        existing = read_pid_record(resolved)
        if existing is None or existing.pid != expected_pid:
            return
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _tracked_record(home: Path) -> DashboardPidRecord | None:
    record = read_pid_record(home)
    if record is None and dashboard_pid_path(home).exists():
        raise ValueError("看板 PID 记录无法读取。请检查服务状态。")
    return record


def _recorded_process(record: DashboardPidRecord | None) -> psutil.Process | None:
    if record is None:
        return None
    if record.pid <= 0:
        raise ValueError("看板 PID 记录无效")
    try:
        proc = psutil.Process(record.pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return None
        if record.create_time is None:
            raise ValueError("看板 PID 记录缺少创建时间")
        return proc if proc.create_time() == record.create_time else None
    except psutil.NoSuchProcess:
        return None


def is_pid_record_alive(record: DashboardPidRecord | None) -> bool:
    try:
        return _recorded_process(record) is not None
    except (psutil.Error, OSError, ValueError):
        return False


def probe_health(host: str, port: int, *, timeout_sec: float = 1.0) -> dict[str, Any] | None:
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout_sec)
        try:
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
        finally:
            conn.close()
    except Exception:
        return None


def _health_matches(record: DashboardPidRecord, *, timeout_sec: float = 1.0) -> bool:
    if record.host not in LOOPBACK_HOSTS:
        return False
    health = probe_health(record.host, record.port, timeout_sec=timeout_sec)
    return bool(
        health
        and health.get("ok") is True
        and health.get("pid") == record.pid
        and health.get("host") == record.host
        and health.get("port") == record.port
        and (record.instance_id is None or health.get("instance_id") == record.instance_id)
    )


def _tray_pid_path(home: Path) -> Path:
    return billing_dir(home) / "tray.pid"


def is_tray_running(home: Path | None = None) -> bool:
    if os.name != "nt":
        return False
    resolved = home or bridge_home()
    path = _tray_pid_path(resolved)
    if not path.is_file():
        return False
    raw = read_json(path, None)
    if not isinstance(raw, dict):
        return False
    pid = raw.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        name = proc.name().lower()
        return "powershell" in name or "pwsh" in name
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


def _powershell_executable() -> str | None:
    if os.name != "nt":
        return None
    # Windows PowerShell 5.1 always bundles System.Windows.Forms & System.Drawing.
    for candidate in ("powershell.exe", "pwsh.exe", "pwsh"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _detached_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        return {"creationflags": flags, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def start_tray(
    home: Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": False, "supported": False, "running": False, "message": "系统托盘仅支持 Windows"}
    resolved = home or bridge_home()
    if is_tray_running(resolved):
        return {"ok": True, "supported": True, "running": True, "message": "系统托盘已在运行"}
    ps = _powershell_executable()
    script = bundled_tray_script()
    if not ps or not script.is_file():
        return {
            "ok": False,
            "supported": True,
            "running": False,
            "message": "未找到 PowerShell 或托盘脚本",
        }
    cfg = load_service_config(resolved)
    eff_host = host or cfg.host
    eff_port = port or cfg.port
    exe, prefix = resolve_bridge_command()
    cmd = [
        ps,
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-STA",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-BridgeExe",
        exe,
        "-BridgeHome",
        str(resolved),
        "-HostAddr",
        eff_host,
        "-Port",
        str(eff_port),
        "-Mode",
        "Run",
    ]
    if prefix:
        cmd.extend(["-BridgePrefixJson", json.dumps(prefix)])
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        close_fds=True,
    )
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if is_tray_running(resolved):
            return {"ok": True, "supported": True, "running": True, "message": "系统托盘已启动"}
        time.sleep(0.15)
    return {"ok": True, "supported": True, "running": is_tray_running(resolved), "message": "已拉起系统托盘"}


def stop_tray(home: Path | None = None) -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": True, "supported": False, "running": False, "message": "非 Windows 平台无托盘"}
    resolved = home or bridge_home()
    ps = _powershell_executable()
    script = bundled_tray_script()
    if ps and script.is_file():
        exe, _prefix = resolve_bridge_command()
        cmd = [
            ps,
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-BridgeExe",
            exe,
            "-BridgeHome",
            str(resolved),
            "-Mode",
            "Stop",
        ]
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline and is_tray_running(resolved):
        time.sleep(0.15)
    if not is_tray_running(resolved):
        with contextlib.suppress(OSError):
            _tray_pid_path(resolved).unlink(missing_ok=True)
    return {"ok": True, "supported": True, "running": is_tray_running(resolved), "message": "系统托盘已关闭"}


def _autostart_vbs_path(home: Path) -> Path:
    return billing_dir(home) / "dashboard-autostart.vbs"


def _vb_quote(text: str) -> str:
    return text.replace('"', '""')


def build_autostart_command(home: Path) -> tuple[Path, str, str]:
    """Return ``(vbs_path, vbs_content, registry_command)`` for Windows HKCU Run."""
    vbs_path = _autostart_vbs_path(home)
    exe, prefix = resolve_bridge_command()
    parts = [exe, *prefix, "dashboard", "start"]
    cmd_line = " ".join(f'"{p}"' if (" " in p or "\t" in p) else p for p in parts)
    vbs = (
        "' HexaHarbor Agent Bridge dashboard autostart launcher (hidden window).\r\n"
        'Set shell = CreateObject("WScript.Shell")\r\n'
        f'shell.Environment("Process")("AGENT_BRIDGE_HOME") = "{_vb_quote(str(home))}"\r\n'
        f'shell.Run "{_vb_quote(cmd_line)}", 0, False\r\n'
    )
    reg_cmd = f'wscript.exe "{vbs_path}"'
    return vbs_path, vbs, reg_cmd


def get_autostart_status(
    home: Path | None = None,
    *,
    platform_is_windows: bool | None = None,
    registry: RegistryBackend | None = None,
) -> dict[str, Any]:
    is_win = (os.name == "nt") if platform_is_windows is None else platform_is_windows
    if not is_win:
        return {"supported": False, "enabled": False, "command": None, "matches_current": False}
    resolved = home or bridge_home()
    backend = registry or _WinRegBackend()
    existing = backend.read_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME)
    _vbs_path, _vbs, expected_cmd = build_autostart_command(resolved)
    matches = bool(existing and existing.strip().casefold() == expected_cmd.casefold())
    return {
        "supported": True,
        "enabled": matches,
        "command": existing if matches else None,
        "expected_command": expected_cmd,
        "matches_current": matches,
        "occupied": bool(existing and not matches),
    }


def set_autostart(
    enabled: bool,
    home: Path | None = None,
    *,
    platform_is_windows: bool | None = None,
    registry: RegistryBackend | None = None,
) -> dict[str, Any]:
    is_win = (os.name == "nt") if platform_is_windows is None else platform_is_windows
    if not is_win:
        return {"supported": False, "enabled": False, "command": None, "matches_current": False}
    resolved = home or bridge_home()
    backend = registry or _WinRegBackend()
    vbs_path, vbs_content, reg_cmd = build_autostart_command(resolved)
    current = get_autostart_status(resolved, platform_is_windows=is_win, registry=backend)
    if enabled:
        if current["occupied"]:
            raise ValueError("开机自启已被另一个工作目录占用")
        atomic_write_text(vbs_path, vbs_content)
        backend.write_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME, reg_cmd)
    elif current["matches_current"]:
        backend.delete_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME)
        with contextlib.suppress(OSError):
            vbs_path.unlink(missing_ok=True)
    return get_autostart_status(resolved, platform_is_windows=is_win, registry=backend)


def start_background_service(
    home: Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool = False,
    with_tray: bool | None = None,
    command: tuple[str, list[str]] | None = None,
) -> dict[str, Any]:
    resolved = home or bridge_home()
    cfg = load_service_config(resolved)
    eff_host = (host or cfg.host).strip().lower()
    if eff_host not in LOOPBACK_HOSTS:
        raise ValueError(f"dashboard requires IPv4 loopback (127.0.0.1 or localhost); got {eff_host!r}")
    eff_port = cfg.port if port is None else int(port)

    try:
        existing = _tracked_record(resolved)
        if existing is not None and _recorded_process(existing) is not None:
            if not _health_matches(existing):
                return {"started": False, "running": None, "message": "无法确认已记录看板的健康响应。已保留进程记录。"}
            want_tray = cfg.tray_enabled if with_tray is None else with_tray
            if want_tray and os.name == "nt":
                start_tray(resolved, host=existing.host, port=existing.port)
            return {
                "started": False,
                "running": True,
                "pid": existing.pid,
                "url": f"http://{existing.host}:{existing.port}",
                "message": "看板服务已在运行",
            }
    except (psutil.Error, OSError, ValueError) as exc:
        return {"started": False, "running": None, "message": f"无法核实已记录看板进程 ({type(exc).__name__})"}

    exe, prefix = command or resolve_bridge_command()
    cmd = [
        exe,
        *prefix,
        "dashboard",
        "serve",
        "--host",
        eff_host,
        "--port",
        str(eff_port),
        "--no-open",
    ]
    env = os.environ.copy()
    env["AGENT_BRIDGE_HOME"] = str(resolved)
    launch_id = uuid4().hex
    env[DASHBOARD_LAUNCH_ID_ENV] = launch_id
    log_file = dashboard_log_path(resolved)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_file.open("ab") as sink:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=sink,
                env=env,
                **_detached_kwargs(),
            )
    except OSError as exc:
        return {"started": False, "running": False, "message": f"无法启动看板命令 ({type(exc).__name__})"}

    url = f"http://{eff_host}:{eff_port}"
    deadline = time.monotonic() + 6.0
    started: DashboardPidRecord | None = None
    while time.monotonic() < deadline:
        record = read_pid_record(resolved)
        if (
            record is not None
            and record.instance_id == launch_id
            and eff_port in (0, record.port)
            and is_pid_record_alive(record)
            and _health_matches(record, timeout_sec=0.5)
        ):
            started = record
            break
        if proc.poll() not in (None, 0):
            break
        time.sleep(0.15)

    want_tray = cfg.tray_enabled if with_tray is None else with_tray
    if started is not None:
        url = f"http://{started.host}:{started.port}"
        if want_tray and os.name == "nt":
            start_tray(resolved, host=started.host, port=started.port)

    if started is not None and open_browser:
        import webbrowser

        with contextlib.suppress(Exception):
            webbrowser.open(url)

    return {
        "started": started is not None,
        "running": started is not None,
        "pid": started.pid if started is not None else None,
        "url": url,
        "message": (
            f"看板服务已启动 ({url})" if started is not None else f"看板服务未能启动，请查看日志 {log_file}"  # noqa: RUF001
        ),
    }


def stop_background_service(home: Path | None = None) -> dict[str, Any]:
    resolved = home or bridge_home()
    try:
        rec = _tracked_record(resolved)
        proc = _recorded_process(rec)
        if rec is None or proc is None:
            if rec is not None:
                clear_pid_record(resolved, expected_record=rec)
            return {"stopped": False, "running": False, "message": "看板服务当前未运行"}
    except (psutil.Error, OSError, ValueError) as exc:
        return {
            "stopped": False,
            "running": None,
            "message": f"无法核实看板进程 ({type(exc).__name__})。已保留进程记录。",
        }
    try:
        proc.terminate()
        try:
            proc.wait(timeout=4.0)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
    except psutil.NoSuchProcess:
        pass
    except (psutil.Error, OSError) as exc:
        return {
            "stopped": False,
            "running": None,
            "message": f"无法确认看板已停止 ({type(exc).__name__})。已保留进程记录。",
        }
    clear_pid_record(resolved, expected_record=rec)
    return {"stopped": True, "running": False, "pid": rec.pid, "message": "看板服务已停止"}


def restart_background_service(
    home: Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    resolved = home or bridge_home()
    stopped = stop_background_service(resolved)
    if stopped["running"] is not False:
        return stopped
    return start_background_service(resolved, host=host, port=port, open_browser=False)


def _relaunch_dashboard(home: Path, address: tuple[str, int], command: tuple[str, list[str]]) -> str | None:
    host, port = address
    try:
        started = start_background_service(home, host=host, port=port, with_tray=False, command=command)
    except (OSError, ValueError) as exc:
        return f"看板启动失败 ({type(exc).__name__})"
    return None if started.get("running") is True else str(started.get("message") or "无法确认看板已启动")


def _run_detached_dashboard_restart(spec: dict[str, Any]) -> None:
    home = Path(spec["home"])
    with Path(spec["log_path"]).open("a", encoding="utf-8") as sink:
        try:
            process = _recorded_process(DashboardPidRecord(pid=spec["pid"], create_time=spec["create_time"]))
            if process is not None:
                process.wait(timeout=15)
        except psutil.NoSuchProcess:
            pass
        except (psutil.Error, OSError, ValueError) as exc:
            sink.write(f"无法确认原看板已停止 ({type(exc).__name__})\n")
            return
        restart_error = _relaunch_dashboard(home, spec["target_address"], spec["command"])
        if restart_error:
            sink.write(f"看板未恢复运行：{restart_error}\n")  # noqa: RUF001
        else:
            sink.write("看板已启动。\n")


def _spawn_restart_helper(
    home: Path,
    target_address: tuple[str, int],
    *,
    log_path: Path,
) -> None:
    create_time = process_create_time(os.getpid())
    if create_time is None:
        raise OSError("看板进程创建时间无法核实")
    spec = {
        "home": str(home),
        "pid": os.getpid(),
        "create_time": create_time,
        "log_path": str(log_path),
        "target_address": target_address,
        "command": resolve_bridge_command(),
    }
    helper = (
        "import json, sys; from agent_bridge.dashboard_service import _run_detached_dashboard_restart; "
        "_run_detached_dashboard_restart(json.loads(sys.argv[1]))"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as sink:
        subprocess.Popen(
            [sys.executable, "-c", helper, json.dumps(spec)],
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            **_detached_kwargs(),
        )


def schedule_server_stop(server: Any, *, delay_sec: float = 0.25) -> None:
    def _do() -> None:
        time.sleep(delay_sec)
        with contextlib.suppress(Exception):
            server.shutdown()

    threading.Thread(target=_do, daemon=True, name="dashboard-self-stop").start()


def schedule_server_restart(
    home: Path,
    server: Any,
    *,
    host: str,
    port: int,
    delay_sec: float = 0.25,
) -> None:
    _spawn_restart_helper(home, (host, port), log_path=dashboard_log_path(home))
    schedule_server_stop(server, delay_sec=delay_sec)


def dashboard_update_response() -> dict[str, str]:
    # Dashboard self-update is disabled. Its previous implementation is in 5c2596c.
    return {"status": "disabled", "message": "最新，别搞"}  # noqa: RUF001


def open_billing_folder(home: Path | None = None) -> dict[str, Any]:
    target = billing_dir(home)
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", str(target)], **_detached_kwargs())
        return {"ok": True, "path": str(target)}
    opener = shutil.which("open") if sys.platform == "darwin" else shutil.which("xdg-open")
    if opener:
        subprocess.Popen([opener, str(target)], **_detached_kwargs())
        return {"ok": True, "path": str(target)}
    return {"ok": False, "path": str(target), "message": "未找到系统文件管理器命令"}


def service_status(
    home: Path | None = None,
    *,
    current_pid: int | None = None,
    current_host: str | None = None,
    current_port: int | None = None,
    started_monotonic: float | None = None,
    registry: RegistryBackend | None = None,
) -> dict[str, Any]:
    resolved = home or bridge_home()
    cfg = load_service_config(resolved)
    alive_rec = None
    identity_unknown = False
    if current_pid is None:
        try:
            pid_rec = _tracked_record(resolved)
            alive_rec = pid_rec if _recorded_process(pid_rec) is not None else None
        except (psutil.Error, OSError, ValueError):
            identity_unknown = True

    running: bool | None
    if current_pid is not None and current_host is not None and current_port is not None:
        running = True
        pid = current_pid
        host = current_host
        port = current_port
        uptime_sec = int(max(0.0, time.monotonic() - started_monotonic)) if started_monotonic is not None else 0
    elif alive_rec is not None:
        running = True
        pid = alive_rec.pid
        host = alive_rec.host
        port = alive_rec.port
        uptime_sec = 0
        if alive_rec.started_at:
            with contextlib.suppress(ValueError):
                dt = datetime.fromisoformat(alive_rec.started_at)
                uptime_sec = max(0, int((datetime.now(UTC) - dt).total_seconds()))
    else:
        running = None if identity_unknown else False
        pid = None
        host = cfg.host
        port = cfg.port
        uptime_sec = 0

    exe, prefix = resolve_bridge_command()
    install_kind = detect_install()
    autostart = get_autostart_status(resolved, registry=registry)
    return {
        "running": running,
        "pid": pid,
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}",
        "uptime_sec": uptime_sec,
        "executable": " ".join([exe, *prefix]),
        "install_kind": install_kind,
        "is_checkout": install_kind == "checkout",
        "platform": sys.platform,
        "windows_supported": os.name == "nt",
        "autostart": autostart,
        "tray_running": is_tray_running(resolved),
        "billing_dir": str(billing_dir(resolved)),
        "log_path": str(dashboard_log_path(resolved)),
        "config": cfg.model_dump(mode="json"),
    }
