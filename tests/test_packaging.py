from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from agent_bridge.paths import bundled_agents_toml

ROOT = Path(__file__).resolve().parents[1]


def test_bundled_agents_toml_matches_root_template():
    bundled = bundled_agents_toml().read_text(encoding="utf-8")
    root = (ROOT / "agents.toml").read_text(encoding="utf-8")
    assert bundled == root
    assert "[coordinator]" in bundled
    assert 'mode = "auto"' in bundled
    assert "instructions" in bundled


def test_wheel_ships_coordinator_defaults(tmp_path: Path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build the wheel")
    completed = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    wheels = sorted(tmp_path.glob("agent_bridge-*.whl"))
    assert wheels
    with zipfile.ZipFile(wheels[-1]) as archive:
        names = [name for name in archive.namelist() if name.endswith("agent_bridge/agents.toml")]
        assert names == ["agent_bridge/agents.toml"]
        text = archive.read(names[0]).decode("utf-8")
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
        assert "License-Expression: AGPL-3.0-only" in metadata
        assert "https://github.com/WeiBeta/HexaHarbor-Agent-Bridge" in metadata
        license_root = metadata_name.removesuffix("METADATA") + "licenses/"
        assert "GNU AFFERO GENERAL PUBLIC LICENSE" in archive.read(license_root + "LICENSE").decode("utf-8")
        mit = archive.read(license_root + "LICENSES/Agent-Bridge-MIT.txt").decode("utf-8")
        assert "Copyright (c) 2026 FeiZhuLulu" in mit
        assert "Permission is hereby granted, free of charge" in mit
        assert "5560e065f5aa7d7d5155cd17a28baa231208b7ed" in archive.read(license_root + "NOTICE.md").decode("utf-8")
    assert "[coordinator]" in text
    assert 'mode = "auto"' in text
    assert "instructions" in text


def test_wheel_ships_dashboard_assets_and_opencodex_license(tmp_path: Path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build the wheel")
    completed = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    wheels = sorted(tmp_path.glob("agent_bridge-*.whl"))
    assert wheels
    with zipfile.ZipFile(wheels[-1]) as archive:
        names = set(archive.namelist())
        assert "agent_bridge/dashboard_assets/index.html" in names
        assert "agent_bridge/dashboard_assets/app.css" in names
        assert "agent_bridge/dashboard_assets/app.js" in names
        assert "agent_bridge/share/dashboard-tray.ps1" in names
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        license_root = metadata_name.removesuffix("METADATA") + "licenses/"
        ocx_mit = archive.read(license_root + "LICENSES/opencodex-MIT.txt").decode("utf-8")
        assert "Copyright (c) 2026 opencodex contributors" in ocx_mit
        notice = archive.read(license_root + "NOTICE.md").decode("utf-8")
        assert "@bitkyc08/opencodex" in notice
