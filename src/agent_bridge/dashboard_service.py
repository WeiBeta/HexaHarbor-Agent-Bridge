"""Background daemon lifecycle, Windows autostart, system tray, and on-demand update.

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
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any, Protocol

import psutil
from pydantic import BaseModel, Field, field_validator

from agent_bridge.cli import GIT_SOURCE, PACKAGE, checkout_root, detect_install
from agent_bridge.paths import (
    billing_dir,
    bridge_home,
    bundled_tray_script,
    dashboard_log_path,
    dashboard_pid_path,
    service_config_path,
)
from agent_bridge.persist import atomic_write_json, atomic_write_text, file_lock, read_json
from agent_bridge.processes import count_sibling_servers, process_create_time

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
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
            raise ValueError(f"host must be a loopback address ({', '.join(sorted(LOOPBACK_HOSTS))}); got {v!r}")
        return cleaned


class DashboardPidRecord(BaseModel):
    pid: int
    create_time: float | None = None
    host: str = DEFAULT_SERVICE_HOST
    port: int = DEFAULT_SERVICE_PORT
    started_at: str = ""
    executable: str = ""
    install_kind: str = "unknown"


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


def clear_pid_record(home: Path | None = None, *, expected_pid: int | None = None) -> None:
    resolved = home or bridge_home()
    path = dashboard_pid_path(resolved)
    if not path.is_file():
        return
    if expected_pid is not None:
        existing = read_pid_record(resolved)
        if existing is not None and existing.pid != expected_pid:
            return
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def is_pid_record_alive(record: DashboardPidRecord | None) -> bool:
    if record is None or record.pid <= 0:
        return False
    try:
        proc = psutil.Process(record.pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if record.create_time is not None:
            actual_ct = proc.create_time()
            if abs(actual_ct - record.create_time) > 2.0:
                return False
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
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
        **_detached_kwargs(),
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
    return {
        "supported": True,
        "enabled": bool(existing),
        "command": existing,
        "expected_command": expected_cmd,
        "matches_current": bool(existing and existing.strip().lower() == expected_cmd.strip().lower()),
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
    if enabled:
        atomic_write_text(vbs_path, vbs_content)
        backend.write_value(AUTOSTART_REG_PATH, AUTOSTART_VALUE_NAME, reg_cmd)
    else:
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
) -> dict[str, Any]:
    resolved = home or bridge_home()
    cfg = load_service_config(resolved)
    eff_host = (host or cfg.host).strip().lower()
    if eff_host not in LOOPBACK_HOSTS:
        raise ValueError(f"host must be loopback; got {eff_host!r}")
    eff_port = cfg.port if port is None else int(port)

    # Already alive? Reuse cleanly.
    existing = read_pid_record(resolved)
    if is_pid_record_alive(existing) and existing is not None:
        health = probe_health(existing.host, existing.port)
        if health is not None:
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

    exe, prefix = resolve_bridge_command()
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
    log_file = dashboard_log_path(resolved)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as sink:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            env=env,
            **_detached_kwargs(),
        )

    url = f"http://{eff_host}:{eff_port}"
    deadline = time.monotonic() + 6.0
    healthy = False
    while time.monotonic() < deadline:
        if probe_health(eff_host, eff_port, timeout_sec=0.5) is not None:
            healthy = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.15)

    want_tray = cfg.tray_enabled if with_tray is None else with_tray
    if healthy and want_tray and os.name == "nt":
        start_tray(resolved, host=eff_host, port=eff_port)

    if healthy and open_browser:
        import webbrowser

        with contextlib.suppress(Exception):
            webbrowser.open(url)

    return {
        "started": healthy,
        "running": healthy,
        "pid": proc.pid if healthy else None,
        "url": url,
        "message": (
            f"看板服务已启动 ({url})"
            if healthy
            else f"看板服务未能启动，请查看日志 {log_file}"  # noqa: RUF001
        ),
    }


def stop_background_service(home: Path | None = None) -> dict[str, Any]:
    resolved = home or bridge_home()
    rec = read_pid_record(resolved)
    if rec is None or not is_pid_record_alive(rec):
        clear_pid_record(resolved)
        return {"stopped": False, "running": False, "message": "看板服务当前未运行"}
    try:
        proc = psutil.Process(rec.pid)
        proc.terminate()
        try:
            proc.wait(timeout=4.0)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        pass
    finally:
        clear_pid_record(resolved, expected_pid=rec.pid)
    return {"stopped": True, "running": False, "pid": rec.pid, "message": "看板服务已停止"}


def restart_background_service(
    home: Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    resolved = home or bridge_home()
    stop_background_service(resolved)
    return start_background_service(resolved, host=host, port=port, open_browser=False)


def _spawn_wait_then_run_helper(wait_pid: int, commands: list[list[str]], *, cwd: str | None, env: dict[str, str], log_path: Path) -> None:
    """Spawn a tiny detached Python helper that waits for ``wait_pid`` to exit before running fixed commands."""
    helper_code = (
        "import json, pathlib, subprocess, sys, time, psutil\n"
        "wait_pid = int(sys.argv[1])\n"
        "log_file = pathlib.Path(sys.argv[2])\n"
        "cwd = sys.argv[3] or None\n"
        "cmds = json.loads(sys.argv[4])\n"
        "deadline = time.monotonic() + 15.0\n"
        "while time.monotonic() < deadline:\n"
        "    if not psutil.pid_exists(wait_pid):\n"
        "        break\n"
        "    time.sleep(0.15)\n"
        "time.sleep(0.2)\n"
        "log_file.parent.mkdir(parents=True, exist_ok=True)\n"
        "with log_file.open('ab') as sink:\n"
        "    for idx, cmd in enumerate(cmds):\n"
        "        is_last = idx == len(cmds) - 1\n"
        "        sink.write(f'\\n=== run {cmd!r} ===\\n'.encode('utf-8'))\n"
        "        sink.flush()\n"
        "        if is_last:\n"
        "            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, cwd=cwd)\n"
        "        else:\n"
        "            rc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, cwd=cwd, check=False).returncode\n"
        "            if rc != 0:\n"
        "                sink.write(f'step failed rc={rc}\\n'.encode('utf-8'))\n"
        "                break\n"
    )
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            helper_code,
            str(wait_pid),
            str(log_path),
            cwd or "",
            json.dumps(commands),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
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
    exe, prefix = resolve_bridge_command()
    relaunch = [exe, *prefix, "dashboard", "serve", "--host", host, "--port", str(port), "--no-open"]
    env = os.environ.copy()
    env["AGENT_BRIDGE_HOME"] = str(home)
    _spawn_wait_then_run_helper(
        os.getpid(),
        [relaunch],
        cwd=None,
        env=env,
        log_path=dashboard_log_path(home),
    )
    schedule_server_stop(server, delay_sec=delay_sec)


def _installed_direct_url() -> tuple[str | None, str | None]:
    """Read ``(repo_url, commit_id)`` from ``direct_url.json`` for ``uv tool`` installs."""
    try:
        dist = distribution(PACKAGE)
        raw = dist.read_text("direct_url.json")
    except (PackageNotFoundError, OSError):
        return None, None
    if not raw:
        return None, None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(obj, dict):
        return None, None
    url = obj.get("url") if isinstance(obj.get("url"), str) else None
    raw_vcs = obj.get("vcs_info")
    vcs: dict[str, Any] = raw_vcs if isinstance(raw_vcs, dict) else {}
    commit_id = vcs.get("commit_id") if isinstance(vcs.get("commit_id"), str) else None
    return url, commit_id


def _parse_ls_remote_sha(stdout: str) -> str | None:
    for line in stdout.splitlines():
        parts = line.strip().split()
        if parts and len(parts[0]) >= 7:
            return parts[0]
    return None


def check_update_available(
    *,
    kind: str | None = None,
    run: Callable[..., Any] | None = None,
    siblings: int | None = None,
) -> dict[str, Any]:
    """On-demand check whether the remote repository has a newer commit.

    Never called automatically on a timer — runs only when the user clicks
    "重启并更新" or runs ``agent-bridge dashboard update``.
    """
    detected = kind or detect_install()
    runner = run or subprocess.run
    git = shutil.which("git")
    if not git:
        return {
            "status": "error",
            "up_to_date": False,
            "install_kind": detected,
            "message": "本机未找到 git，无法检查远端更新",  # noqa: RUF001
        }

    if detected == "checkout":
        root = checkout_root()
        if root is None:
            return {
                "status": "error",
                "up_to_date": False,
                "install_kind": detected,
                "message": "未定位到源码 Git 根目录",
            }
        local_res = runner(
            [git, "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        local_sha = (getattr(local_res, "stdout", "") or "").strip()
        if getattr(local_res, "returncode", 1) != 0 or not local_sha:
            return {
                "status": "error",
                "up_to_date": False,
                "install_kind": detected,
                "message": "无法读取当前 Git 提交哈希",
            }
        # Resolve tracking remote/branch if configured; else fall back to origin HEAD.
        up_res = runner(
            [git, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        remote_name = "origin"
        ref_spec = "HEAD"
        if getattr(up_res, "returncode", 1) == 0:
            upstream_ref = (getattr(up_res, "stdout", "") or "").strip()
            if "/" in upstream_ref:
                remote_name, branch_name = upstream_ref.split("/", 1)
                ref_spec = f"refs/heads/{branch_name}"
        ls_res = runner(
            [git, "ls-remote", remote_name, ref_spec],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        remote_sha = _parse_ls_remote_sha(getattr(ls_res, "stdout", "") or "")
        if getattr(ls_res, "returncode", 1) != 0 or not remote_sha:
            return {
                "status": "error",
                "up_to_date": False,
                "install_kind": detected,
                "current_commit": local_sha[:8],
                "message": "检查远端更新失败（请确认网络或 Git 远端可达）",  # noqa: RUF001
            }
        if remote_sha == local_sha:
            return {
                "status": "up_to_date",
                "up_to_date": True,
                "install_kind": detected,
                "current_commit": local_sha[:8],
                "remote_commit": remote_sha[:8],
                "message": f"最新，别搞（当前已对齐远端 {local_sha[:8]}）",  # noqa: RUF001
            }
        dirty_res = runner(
            [git, "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if (getattr(dirty_res, "stdout", "") or "").strip():
            return {
                "status": "blocked",
                "up_to_date": False,
                "install_kind": detected,
                "current_commit": local_sha[:8],
                "remote_commit": remote_sha[:8],
                "message": "远端有新提交，但当前源码目录存在未提交修改，已阻止自动覆盖",  # noqa: RUF001
            }
    elif detected == "uv-tool":
        url, local_sha = _installed_direct_url()
        repo_url = url or GIT_SOURCE.removeprefix("git+")
        if not local_sha:
            return {
                "status": "error",
                "up_to_date": False,
                "install_kind": detected,
                "message": "未能读取当前安装提交号（direct_url.json 缺失），请手动运行 agent-bridge upgrade",  # noqa: RUF001
            }
        ls_res = runner(
            [git, "ls-remote", repo_url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        remote_sha = _parse_ls_remote_sha(getattr(ls_res, "stdout", "") or "")
        if getattr(ls_res, "returncode", 1) != 0 or not remote_sha:
            return {
                "status": "error",
                "up_to_date": False,
                "install_kind": detected,
                "current_commit": local_sha[:8],
                "message": "检查远端更新失败（请确认网络可达）",  # noqa: RUF001
            }
        if remote_sha == local_sha:
            return {
                "status": "up_to_date",
                "up_to_date": True,
                "install_kind": detected,
                "current_commit": local_sha[:8],
                "remote_commit": remote_sha[:8],
                "message": f"最新，别搞（当前已对齐远端 {local_sha[:8]}）",  # noqa: RUF001
            }
    else:
        return {
            "status": "unsupported",
            "up_to_date": False,
            "install_kind": detected,
            "message": "当前运行方式既非 uv tool 也非 git checkout，不支持自动更新",  # noqa: RUF001
        }

    # Check if other live Agent Bridge MCP instances would lock agent-bridge.exe on Windows.
    live_siblings = count_sibling_servers() if siblings is None else siblings
    if live_siblings > 0:
        return {
            "status": "blocked",
            "up_to_date": False,
            "install_kind": detected,
            "current_commit": local_sha[:8],
            "remote_commit": remote_sha[:8],
            "message": f"发现新版本 ({local_sha[:8]} → {remote_sha[:8]})，但仍有 {live_siblings} 个主控 Bridge 实例运行中，请先关闭主控再更新",  # noqa: RUF001
        }

    return {
        "status": "update_available",
        "up_to_date": False,
        "install_kind": detected,
        "current_commit": local_sha[:8],
        "remote_commit": remote_sha[:8],
        "message": f"发现新版本 ({local_sha[:8]} → {remote_sha[:8]})，准备更新并重启",  # noqa: RUF001
    }


def _upgrade_commands(detected: str) -> tuple[list[list[str]], str | None]:
    uv = shutil.which("uv")
    git = shutil.which("git")
    if detected == "uv-tool":
        if not uv:
            raise RuntimeError("uv 未在 PATH 中")
        return [[uv, "tool", "upgrade", PACKAGE]], None
    if detected == "checkout":
        root = checkout_root()
        if root is None or not git or not uv:
            raise RuntimeError("源码目录更新需要 git 和 uv")
        return [[git, "pull", "--ff-only"], [uv, "sync", "--extra", "dev"]], str(root)
    raise RuntimeError("不支持的安装方式")


def schedule_update_and_restart(
    home: Path,
    server: Any,
    *,
    host: str,
    port: int,
    delay_sec: float = 0.25,
) -> dict[str, Any]:
    check = check_update_available()
    if check["status"] != "update_available":
        return check

    detected = str(check["install_kind"])
    cmds, cwd = _upgrade_commands(detected)
    exe, prefix = resolve_bridge_command()
    relaunch = [exe, *prefix, "dashboard", "serve", "--host", host, "--port", str(port), "--no-open"]
    env = os.environ.copy()
    env["AGENT_BRIDGE_HOME"] = str(home)
    _spawn_wait_then_run_helper(
        os.getpid(),
        [*cmds, relaunch],
        cwd=cwd,
        env=env,
        log_path=billing_dir(home) / "update.log",
    )
    schedule_server_stop(server, delay_sec=delay_sec)
    return {
        **check,
        "status": "updating",
        "url": f"http://{host}:{port}",
        "message": f"正在更新 ({check['current_commit']} → {check['remote_commit']}) 并重启服务…",
    }


def cli_update_and_restart(home: Path | None = None) -> dict[str, Any]:
    """Run on-demand update check from CLI / tray; if update available, stop, upgrade, and restart."""
    resolved = home or bridge_home()
    rec = read_pid_record(resolved)
    was_running = is_pid_record_alive(rec)
    # Exclude the background dashboard PID itself when checking if other MCP coordinators block upgrade.
    siblings = count_sibling_servers()
    if was_running and siblings > 0:
        siblings = max(0, siblings - 1)
    check = check_update_available(siblings=siblings)
    if check["status"] != "update_available":
        return check
    if was_running:
        stop_background_service(resolved)
    cmds, cwd = _upgrade_commands(str(check["install_kind"]))
    for cmd in cmds:
        completed = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            if was_running:
                start_background_service(resolved)
            return {
                **check,
                "status": "error",
                "message": f"更新命令失败：{' '.join(cmd)} (exit {completed.returncode})",  # noqa: RUF001
            }
    if was_running:
        start_background_service(resolved)
    return {
        **check,
        "status": "updated",
        "message": f"已更新到 {check['remote_commit']} 并重启服务",
    }


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
    pid_rec = read_pid_record(resolved)
    alive_rec = pid_rec if is_pid_record_alive(pid_rec) else None

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
        running = False
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
