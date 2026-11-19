import asyncio
import contextlib
import json
import os
import sys
import time
import types
import uuid

import pytest

from agent_bridge.paths import receipt_path
from agent_bridge.processes import reap_subprocess
from agent_bridge.registry import Registry
from agent_bridge.server import INSTRUCTIONS, _error, _registry, dispatch_ticket, list_sessions, mcp


def test_eleven_tools_registered():
    names = sorted(mcp._tool_manager._tools)
    assert names == [
        "cancel_task",
        "check_task",
        "dispatch_task",
        "dispatch_ticket",
        "end_session",
        "get_result",
        "get_transcript",
        "list_agents",
        "list_sessions",
        "set_preferences",
        "wait_task",
    ]


def test_handshake_instructions_carry_hard_rules():
    assert mcp.instructions == INSTRUCTIONS
    for phrase in (
        "dispatch_task.cwd",
        "wait_task",
        "coordinator.mode",
        "dispatch_enabled",
        "runtime_context",
        "cancel_task",
        "end_session",
    ):
        assert phrase in INSTRUCTIONS


def test_error_exposes_exception_type():
    assert _error(ValueError("bad cwd")) == {
        "ok": False,
        "error": "ValueError: bad cwd",
        "error_type": "ValueError",
    }


def test_registry_from_lifespan_context_touches_activity(bridge_home):
    registry = Registry.create(bridge_home)
    registry._last_activity = registry._last_activity - 1
    before = registry._last_activity
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context=registry)
    )
    got = _registry(ctx)
    assert got is registry
    assert registry._last_activity > before


@pytest.mark.asyncio
async def test_list_sessions_does_not_block_event_loop(bridge_home, monkeypatch):
    def slow_count() -> int:
        time.sleep(0.5)
        return 3

    monkeypatch.setattr("agent_bridge.registry.count_sibling_servers", slow_count)
    ticks = 0

    async def counter() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.02)

    registry = Registry.create(bridge_home)
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context=registry)
    )
    task = asyncio.create_task(counter())
    try:
        result = await list_sessions(ctx)
        assert result == {
            "ok": True,
            "scope": "current_instance",
            "other_live_instances": 3,
            "sessions": [],
        }
        assert ticks >= 10
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_stdio_server_exits_on_host_eof(bridge_home):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "from agent_bridge.server import mcp; mcp.run(transport='stdio')",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "AGENT_BRIDGE_HOME": str(bridge_home), "AGENT_BRIDGE_PARENT_CONTEXT": ""},
    )
    try:
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "eof-test", "version": "1"},
            },
        }
        proc.stdin.write((json.dumps(request) + "\n").encode())
        await proc.stdin.drain()
        response = json.loads(await asyncio.wait_for(proc.stdout.readline(), timeout=10))
        assert response["id"] == 1 and "result" in response
        assert proc.returncode is None
        proc.stdin.close()
        assert await asyncio.wait_for(proc.wait(), timeout=10) == 0
    finally:
        await reap_subprocess(proc)


def test_registry_rejects_dict_lifespan(bridge_home):
    registry = Registry.create(bridge_home)
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context={"registry": registry})
    )
    with pytest.raises(RuntimeError, match="not available"):
        _registry(ctx)


@pytest.mark.asyncio
async def test_dispatch_ticket_returns_structured_receipt_and_persists(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context=registry))
    try:
        res = await dispatch_ticket(
            ctx=ctx,
            agent="fake",
            ticket_id="T-001",
            message="ping",
            cwd=str(work.resolve()),
            timeout_sec=5.0,
        )
        assert res["ok"] is True
        assert res["status"] == "completed"
        assert res["ticket_id"] == "T-001"
        assert res["timed_out"] is False
        assert res["persisted"] is True
        rcpt = receipt_path(res["task_id"], bridge_home)
        assert res["receipt_path"] == str(rcpt)
        assert rcpt.is_file()
        data = json.loads(rcpt.read_text(encoding="utf-8"))
        assert data["task_id"] == res["task_id"]
        assert data["session_id"] == res["session_id"]
        assert data["status"] == "completed"
        assert data["title"] == "T-001"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_dispatch_ticket_respects_worker_context_gate(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home, runtime_context="worker")
    await registry.start()
    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context=registry))
    try:
        res = await dispatch_ticket(
            ctx=ctx,
            agent="fake",
            message="ping",
            cwd=str(work.resolve()),
            timeout_sec=5.0,
        )
        assert res["ok"] is False
        assert "nested dispatch is disabled" in res["error"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_ticket_retry_waits_for_receipt_finalization(bridge_home, tmp_path, monkeypatch):
    registry = Registry.create(bridge_home)
    await registry.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    persist = registry._persist_receipt

    async def delayed_receipt(task, session):
        entered.set()
        await release.wait()
        return await persist(task, session)

    monkeypatch.setattr(registry, "_persist_receipt", delayed_receipt)
    args = dict(agent="fake", message="ping", cwd=str(tmp_path.resolve()), request_id=str(uuid.uuid4()))
    try:
        first = await registry.dispatch_task(**args)
        await asyncio.wait_for(entered.wait(), timeout=5)
        pending = await registry.dispatch_ticket(**args, timeout_sec=0.01)
        assert pending["task_id"] == first["task_id"]
        assert pending["timed_out"] is True
        assert pending["receipt_path"] is None
        release.set()
        result = await registry.dispatch_ticket(**args, timeout_sec=5)
        assert result["timed_out"] is False
        assert result["receipt_path"] == str(receipt_path(first["task_id"], bridge_home))
    finally:
        release.set()
        await registry.stop()


@pytest.mark.asyncio
async def test_enabled_agents_filters_list_and_blocks_disabled_dispatch(bridge_home, tmp_path, monkeypatch):
    probed = []

    async def fake_probe(cfg, env_config=None, *, include_models=False):
        probed.append(cfg.name)
        return {"agent": cfg.name, "available": True, "version": "test", "detail": "mock probe"}

    monkeypatch.setattr("agent_bridge.registry.probe_agent", fake_probe)
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text(
        '[server]\nenabled_agents = ["antigravity", "codex"]\n[quota]\nenabled = false\n',
        encoding="utf-8",
    )
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        names = [row["agent"] for row in await registry.list_agents()]
        assert "antigravity" in names
        assert "codex" in names
        assert "grok" not in names
        assert set(probed) == {"antigravity", "codex", "fake"}
        with pytest.raises(KeyError, match=r"disabled by server\.enabled_agents"):
            await registry.dispatch_task("grok", "hi", cwd=str(tmp_path))
    finally:
        await registry.stop()
