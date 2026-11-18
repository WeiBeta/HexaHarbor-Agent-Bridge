from __future__ import annotations

import json
import sys
import types
import uuid

import pytest

from agent_bridge.config import AgentConfig, EnvConfig
from agent_bridge.models import AGY_EFFORTS, EFFORTS, TurnResult, normalize_effort
from agent_bridge.paths import receipt_path
from agent_bridge.probes import probe_agent
from agent_bridge.registry import Registry
from agent_bridge.server import dispatch_task, dispatch_ticket

OTHER_HARNESSES = (
    "grok",
    "kimi",
    "cursor",
    "dsh",
    "claude",
    "opencode",
    "devin",
    "zcode",
    "minimax",
    "codex",
    "fake",
)


def test_normalize_effort_preserves_legacy_tiers_aliases_none_and_gates_xhigh():
    assert normalize_effort(None) is None
    assert normalize_effort(None, allow_xhigh=True) is None

    for tier in EFFORTS:
        assert normalize_effort(tier) == tier
        assert normalize_effort(f"  {tier.upper()}  ") == tier
        assert normalize_effort(tier, allow_xhigh=True) == tier

    for raw, expected in (("l", "low"), ("M", "medium"), (" med ", "medium"), ("H", "high")):
        assert normalize_effort(raw) == expected
        assert normalize_effort(raw, allow_xhigh=True) == expected

    # 默认(非 agy)仍严格拒绝 xhigh
    with pytest.raises(ValueError, match="xhigh"):
        normalize_effort("xhigh")
    with pytest.raises(ValueError, match="xhigh"):
        normalize_effort("  XHIGH ", agent="grok", allow_xhigh=False)

    # agy 显式启用时, xhigh 与 max 各自独立透传
    assert normalize_effort("xhigh", allow_xhigh=True) == "xhigh"
    assert normalize_effort("  XHIGH  ", allow_xhigh=True) == "xhigh"
    assert normalize_effort("max", allow_xhigh=True) == "max"
    assert AGY_EFFORTS == ("off", "low", "medium", "high", "xhigh", "max")

    with pytest.raises(ValueError, match="effort"):
        normalize_effort("turbo", allow_xhigh=True)


@pytest.mark.asyncio
async def test_agy_dispatch_xhigh_end_to_end_and_followup_lifecycle(bridge_home, tmp_path, monkeypatch):
    """验证 antigravity 传 xhigh 从 dispatch 校验直达 CLI --effort xhigh, 并与 max / follow-up / 重启保持一致."""
    work = tmp_path / "work"
    work.mkdir()
    captured_log = tmp_path / "agy_invocations.jsonl"
    stub = tmp_path / "stub_agy.py"
    stub.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "sys.stdin.readline()\n"
        f"log = Path({str(captured_log)!r})\n"
        "with log.open('a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'event': 'init', 'conversation_id': 'conv-h2', 'init': {'permission_mode': 'always-proceed'}}))\n"
        "print(json.dumps({'event': 'result', 'result': {'conversation_id': 'conv-h2', 'status': 'SUCCESS', 'response': 'done'}}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: [sys.executable, str(stub)],
    )
    monkeypatch.setattr("agent_bridge.adapters.antigravity.agy_projects_dir", lambda: tmp_path / "projects")

    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        # 1. 首轮显式传入 " XHIGH "(规范化为 "xhigh")
        first = await registry.dispatch_task(
            "antigravity",
            "hydrogen xhigh check",
            cwd=str(work.resolve()),
            model="gemini-4-hydrogen",
            effort=" XHIGH ",
        )
        assert first["effort"] == "xhigh"
        assert registry.sessions[first["session_id"]].effort == "xhigh"
        assert registry.tasks[first["task_id"]].effort == "xhigh"
        waited_first = await registry.wait_task(first["task_id"], timeout_sec=10)
        assert waited_first["status"] == "completed"
        assert waited_first["effort"] == "xhigh"
        result_first = await registry.get_result(first["task_id"])
        assert result_first["effort"] == "xhigh"

        # 2. 同一 session follow-up 省略 effort, 沿用 xhigh
        followup_same = await registry.dispatch_task(
            "antigravity",
            "continue",
            cwd=str(work.resolve()),
            session_id=first["session_id"],
        )
        assert followup_same["effort"] == "xhigh"
        assert registry.tasks[followup_same["task_id"]].effort == "xhigh"
        waited_same = await registry.wait_task(followup_same["task_id"], timeout_sec=10)
        assert waited_same["status"] == "completed"

        # 3. 同一 session follow-up 切为 max, 必须独立于 xhigh
        followup_max = await registry.dispatch_task(
            "antigravity",
            "switch to max",
            cwd=str(work.resolve()),
            session_id=first["session_id"],
            effort="max",
        )
        assert followup_max["effort"] == "max"
        assert registry.sessions[first["session_id"]].effort == "max"
        assert registry.tasks[followup_max["task_id"]].effort == "max"
        waited_max = await registry.wait_task(followup_max["task_id"], timeout_sec=10)
        assert waited_max["status"] == "completed"

        # 4. 同一 session 再切回 xhigh
        followup_back = await registry.dispatch_task(
            "antigravity",
            "back to xhigh",
            cwd=str(work.resolve()),
            session_id=first["session_id"],
            effort="xhigh",
        )
        assert followup_back["effort"] == "xhigh"
        waited_back = await registry.wait_task(followup_back["task_id"], timeout_sec=10)
        assert waited_back["status"] == "completed"
    finally:
        await registry.stop()

    # 5. 重启恢复后不传 effort 的 follow-up 仍保留 xhigh
    registry2 = Registry.create(bridge_home)
    await registry2.start()
    try:
        assert registry2.sessions[first["session_id"]].effort == "xhigh"
        resumed = await registry2.dispatch_task(
            "antigravity",
            "after restart",
            cwd=str(work.resolve()),
            session_id=first["session_id"],
        )
        assert resumed["effort"] == "xhigh"
        waited_resumed = await registry2.wait_task(resumed["task_id"], timeout_sec=10)
        assert waited_resumed["status"] == "completed"
    finally:
        await registry2.stop()

    invocations = [json.loads(line) for line in captured_log.read_text(encoding="utf-8").splitlines()]
    assert len(invocations) == 5
    efforts_sent = [argv[argv.index("--effort") + 1] for argv in invocations]
    assert efforts_sent == ["xhigh", "xhigh", "max", "xhigh", "xhigh"]
    assert all(argv.index("--effort") < argv.index("-p") for argv in invocations)


def _stub_agy_run_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """用测试替身替换 AgyAdapter.run_turn, 保持 protocol=agy 而不触发真实 harness."""

    async def fake_run_turn(self, session, task):
        return TurnResult(
            text=f"[stub-agy:{self.agent.name}] {task.message}",
            stop_reason="end_turn",
            native_session_id=session.native_session_id or f"agy-{session.session_id}",
        )

    monkeypatch.setattr("agent_bridge.adapters.antigravity.AgyAdapter.run_turn", fake_run_turn)


@pytest.mark.asyncio
async def test_agy_dispatch_ticket_records_xhigh_in_receipt(bridge_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    _stub_agy_run_turn(monkeypatch)

    registry = Registry.create(bridge_home)
    await registry.start()
    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context=registry))
    try:
        assert registry.config.get("antigravity").protocol == "agy"
        res = await dispatch_ticket(
            ctx=ctx,
            agent="antigravity",
            ticket_id="T-XHIGH",
            message="ping",
            cwd=str(work.resolve()),
            effort="xhigh",
            timeout_sec=5.0,
        )
        assert res["ok"] is True
        assert res["effort"] == "xhigh"
        rcpt = receipt_path(res["task_id"], bridge_home)
        assert json.loads(rcpt.read_text(encoding="utf-8"))["effort"] == "xhigh"
    finally:
        await registry.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_effort", "expected"),
    [
        (None, None),
        ("off", "off"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "xhigh"),
        ("max", "max"),
        ("l", "low"),
        ("m", "medium"),
        ("med", "medium"),
        ("h", "high"),
    ],
)
async def test_agy_stubbed_adapter_supports_none_legacy_tiers_aliases_and_xhigh(
    bridge_home, tmp_path, monkeypatch, raw_effort, expected
):
    work = tmp_path / "work"
    work.mkdir()
    _stub_agy_run_turn(monkeypatch)

    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        assert registry.config.get("antigravity").protocol == "agy"
        dispatched = await registry.dispatch_task(
            "antigravity",
            "check",
            cwd=str(work.resolve()),
            effort=raw_effort,
        )
        assert dispatched["effort"] == expected
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert waited["effort"] == expected
        assert registry.sessions[dispatched["session_id"]].effort == expected
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_request_id_dedup_binds_xhigh_distinct_from_max_and_none(bridge_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    _stub_agy_run_turn(monkeypatch)

    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        assert registry.config.get("antigravity").protocol == "agy"
        rid = str(uuid.uuid4())
        base = dict(agent="antigravity", message="hello", cwd=str(work.resolve()), request_id=rid)
        first = await registry.dispatch_task(**base, effort="xhigh")
        assert first["effort"] == "xhigh"
        assert first["reused"] is False

        # 仅大小写差异规范化后仍视为同一请求并复用
        replayed = await registry.dispatch_task(**base, effort="XHIGH")
        assert replayed == {**first, "reused": True}

        # max / high / None 与 xhigh 严格区分, 必须拒绝且不创建新任务
        for other_effort in ("max", "high", None):
            with pytest.raises(ValueError, match="different dispatch request"):
                await registry.dispatch_task(**base, effort=other_effort)
        assert len(registry.tasks) == 1

        await registry.wait_task(first["task_id"], timeout_sec=5)

        # 反向绑定: 先绑定 max 或 None, 再以 xhigh 重放同样被拒
        rid_max = str(uuid.uuid4())
        await registry.dispatch_task(
            agent="antigravity", message="m", cwd=str(work.resolve()), effort="max", request_id=rid_max
        )
        with pytest.raises(ValueError, match="different dispatch request"):
            await registry.dispatch_task(
                agent="antigravity", message="m", cwd=str(work.resolve()), effort="xhigh", request_id=rid_max
            )

        rid_none = str(uuid.uuid4())
        await registry.dispatch_task(
            agent="antigravity", message="n", cwd=str(work.resolve()), effort=None, request_id=rid_none
        )
        with pytest.raises(ValueError, match="different dispatch request"):
            await registry.dispatch_task(
                agent="antigravity", message="n", cwd=str(work.resolve()), effort="xhigh", request_id=rid_none
            )
    finally:
        await registry.stop()


async def _assert_xhigh_rejected_without_task(registry: Registry, work, agent_name: str) -> None:
    """断言指定 agent 传 xhigh 时在校验阶段被拒绝且不产生任何任务或会话."""
    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context=registry))
    with pytest.raises(ValueError, match="xhigh"):
        await registry.dispatch_task(agent_name, "hi", cwd=str(work.resolve()), effort="xhigh")
    with pytest.raises(ValueError, match="xhigh"):
        await registry.dispatch_task(agent_name, "hi", cwd=str(work.resolve()), effort=" XHIGH ")

    res_task = await dispatch_task(ctx=ctx, agent=agent_name, message="hi", cwd=str(work.resolve()), effort="xhigh")
    assert res_task["ok"] is False
    assert res_task["error_type"] == "ValueError"
    assert "xhigh" in res_task["error"]

    res_ticket = await dispatch_ticket(
        ctx=ctx,
        agent=agent_name,
        message="hi",
        cwd=str(work.resolve()),
        effort="xhigh",
        timeout_sec=1.0,
    )
    assert res_ticket["ok"] is False
    assert res_ticket["error_type"] == "ValueError"
    assert "xhigh" in res_ticket["error"]

    assert registry.tasks == {}
    assert registry.sessions == {}
    assert registry._bg == {}
    assert registry._requests == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", OTHER_HARNESSES)
async def test_other_harnesses_reject_xhigh_without_starting_task(bridge_home, tmp_path, monkeypatch, agent_name):
    """非 agy harness 传 xhigh 必须在 dispatch 校验阶段明确拒绝, 不能降级, 静默丢弃或启动任务."""
    work = tmp_path / "work"
    work.mkdir()

    def _forbid(*args, **kwargs):
        raise AssertionError("adapter or subprocess must not start when xhigh is rejected")

    monkeypatch.setattr("agent_bridge.registry.build_adapter", _forbid)

    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        await _assert_xhigh_rejected_without_task(registry, work, agent_name)
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_antigravity_with_fake_protocol_rejects_xhigh_without_creating_task(bridge_home, tmp_path, monkeypatch):
    """即使 agent 名字叫 antigravity, 若 protocol=fake 也必须明确拒绝 xhigh 且不创建任务."""
    work = tmp_path / "work"
    work.mkdir()
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text('[agents.antigravity]\nprotocol = "fake"\n', encoding="utf-8")

    def _forbid(*args, **kwargs):
        raise AssertionError("adapter or subprocess must not start when xhigh is rejected")

    monkeypatch.setattr("agent_bridge.registry.build_adapter", _forbid)

    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        assert registry.config.get("antigravity").protocol == "fake"
        await _assert_xhigh_rejected_without_task(registry, work, "antigravity")
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_non_agy_protocol_override_and_custom_agy_protocol_follow_protocol_rule(
    bridge_home, tmp_path, monkeypatch
):
    work = tmp_path / "work"
    work.mkdir()
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text(
        '[agents.antigravity]\nprotocol = "acp"\n\n[agents.agy_custom]\nprotocol = "agy"\ncommand = ["agy"]\n',
        encoding="utf-8",
    )

    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        # 即使名字叫 antigravity, 若协议被改为 acp 也必须拒绝 xhigh
        with pytest.raises(ValueError, match="xhigh"):
            await registry.dispatch_task("antigravity", "hi", cwd=str(work.resolve()), effort="xhigh")

        # 自定义名称但 protocol="agy" 可透传 xhigh
        captured: list[str] = []

        async def fake_run(self, session, task):
            captured.extend(self._build_cmd(session, task))
            return TurnResult(text="ok")

        monkeypatch.setattr("agent_bridge.adapters.antigravity.AgyAdapter.run_turn", fake_run)
        monkeypatch.setattr("agent_bridge.adapters.antigravity.resolve_command", lambda cmd, fallbacks=None: ["agy"])
        monkeypatch.setattr("agent_bridge.adapters.antigravity.agy_projects_dir", lambda: tmp_path / "projects")
        dispatched = await registry.dispatch_task("agy_custom", "hi", cwd=str(work.resolve()), effort="xhigh")
        await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert captured[captured.index("--effort") + 1] == "xhigh"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_non_agy_followup_rejects_xhigh_without_mutating_session(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text('[agents.grok]\nprotocol = "fake"\n', encoding="utf-8")

    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        first = await registry.dispatch_task("grok", "one", cwd=str(work.resolve()), effort="high")
        await registry.wait_task(first["task_id"], timeout_sec=5)
        with pytest.raises(ValueError, match="xhigh"):
            await registry.dispatch_task(
                "grok",
                "two",
                cwd=str(work.resolve()),
                session_id=first["session_id"],
                effort="xhigh",
            )
        assert registry.sessions[first["session_id"]].effort == "high"
        assert len(registry.tasks) == 1
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_probe_agent_reports_xhigh_for_agy(monkeypatch):
    async def fake_version(_exe):
        return "agy 1.0.0"

    monkeypatch.setattr("agent_bridge.probes.resolve_command", lambda command, fallbacks=None: ["agy"])
    monkeypatch.setattr("agent_bridge.probes.build_worker_env", lambda *args, **kwargs: {})
    monkeypatch.setattr("agent_bridge.probes._version_string", fake_version)

    row = await probe_agent(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        EnvConfig(discover_proxy=False, inherit=[]),
    )
    assert row["available"] is True
    assert "effort=low|medium|high|xhigh|max" in row["detail"]

    custom = await probe_agent(
        AgentConfig(name="agy-hydrogen", protocol="agy", command=["agy"]),
        EnvConfig(discover_proxy=False, inherit=[]),
    )
    assert custom["available"] is True
    assert "effort=low|medium|high|xhigh|max" in custom["detail"]
