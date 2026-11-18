from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_bridge.adapters.fake import FakeAdapter
from agent_bridge.models import ProcState, Session, Task, TaskStatus, TurnResult
from agent_bridge.registry import Registry
from agent_bridge.transcript import append_event


@dataclass
class _TurnGate:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    in_flight: bool = False
    calls: int = 0


@dataclass
class _FakeRecorder:
    gates: dict[str, _TurnGate] = field(default_factory=dict)
    turn_calls: list[tuple[str, str, str]] = field(default_factory=list)
    cancel_calls: list[str] = field(default_factory=list)

    def gate_for(self, message: str) -> _TurnGate:
        return self.gates.setdefault(message, _TurnGate())


@pytest.fixture
def gated_fake(bridge_home: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeRecorder:
    """Drive offline FakeAdapter turns via deterministic asyncio.Event gates."""
    monkeypatch.delenv("AGENT_BRIDGE_MODE", raising=False)
    monkeypatch.delenv("AGENT_BRIDGE_PARENT_CONTEXT", raising=False)
    recorder = _FakeRecorder()
    original_cancel = FakeAdapter.cancel

    async def gated_run_turn(self: FakeAdapter, session: Session, task: Task) -> TurnResult:
        await self.ensure_session(session)
        append_event(session.session_id, "prompt_sent", {"text": task.message}, self.home)
        gate = recorder.gate_for(task.message)
        gate.calls += 1
        gate.in_flight = True
        recorder.turn_calls.append((task.task_id, session.session_id, task.message))
        gate.started.set()

        cancel_event = self._cancel[session.session_id]
        release_waiter = asyncio.create_task(gate.release.wait())
        cancel_waiter = asyncio.create_task(cancel_event.wait())
        try:
            done, pending = await asyncio.wait(
                {release_waiter, cancel_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for waiter in pending:
                waiter.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if cancel_waiter in done:
                append_event(session.session_id, "turn_end", {"stop_reason": "cancelled"}, self.home)
                return TurnResult(text="", stop_reason="cancelled")

            text = f"[fake:{self.agent.name}] {task.task_id}:{task.message}"
            append_event(session.session_id, "message_chunk", {"text": text}, self.home)
            append_event(session.session_id, "turn_end", {"stop_reason": "end_turn"}, self.home)
            return TurnResult(
                text=text,
                files_changed=[],
                stop_reason="end_turn",
                native_session_id=session.native_session_id,
            )
        finally:
            for waiter in (release_waiter, cancel_waiter):
                if not waiter.done():
                    waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await waiter
            gate.in_flight = False

    async def recorded_cancel(self: FakeAdapter, session: Session) -> None:
        recorder.cancel_calls.append(session.session_id)
        await original_cancel(self, session)

    monkeypatch.setattr(FakeAdapter, "run_turn", gated_run_turn)
    monkeypatch.setattr(FakeAdapter, "cancel", recorded_cancel)
    return recorder


@pytest.mark.asyncio
async def test_short_wait_timeout_neither_cancels_nor_redispatches(
    bridge_home: Path,
    tmp_path: Path,
    gated_fake: _FakeRecorder,
) -> None:
    """A short wait_task timeout neither cancels nor re-dispatches the in-flight turn."""
    work = tmp_path / "work"
    work.mkdir()
    gate = gated_fake.gate_for("keep-running")
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "keep-running", cwd=str(work.resolve()))
        task_id = dispatched["task_id"]
        session_id = dispatched["session_id"]

        await asyncio.wait_for(gate.started.wait(), timeout=5)
        bg_before = registry._bg[task_id]

        timed_out = await registry.wait_task(task_id, timeout_sec=0.01)
        assert timed_out["timed_out"] is True
        assert timed_out["task_id"] == task_id
        assert timed_out["session_id"] == session_id
        assert timed_out["status"] == "running"
        assert timed_out["stop_reason"] is None
        assert timed_out["error"] is None

        # Not cancelled: adapter.cancel never fired and the same turn is still in flight.
        assert gated_fake.cancel_calls == []
        assert task_id not in registry._cancel_requested
        assert gate.in_flight is True
        assert registry._bg.get(task_id) is bg_before
        assert not bg_before.done()
        assert not bg_before.cancelled()

        # Not re-dispatched: no extra task/session rows and run_turn started once.
        assert gate.calls == 1
        assert gated_fake.turn_calls == [(task_id, session_id, "keep-running")]
        assert set(registry.tasks) == {task_id}
        assert set(registry.sessions) == {session_id}
        assert registry.sessions[session_id].proc_state == ProcState.busy

        checked = await registry.check_task(task_id)
        assert checked["status"] == "running"

        gate.release.set()
        finished = await registry.wait_task(task_id, timeout_sec=5)
        assert finished["timed_out"] is False
        assert finished["status"] == "completed"
        assert gate.calls == 1
        assert gated_fake.cancel_calls == []
    finally:
        gate.release.set()
        await registry.stop()


@pytest.mark.asyncio
async def test_repeated_wait_timeouts_still_deliver_original_task_final_result(
    bridge_home: Path,
    tmp_path: Path,
    gated_fake: _FakeRecorder,
) -> None:
    """After more than three consecutive wait_task timeouts, the original task still delivers its final result."""
    work = tmp_path / "work"
    work.mkdir()
    gate = gated_fake.gate_for("multi-timeout-job")
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "multi-timeout-job", cwd=str(work.resolve()))
        task_id = dispatched["task_id"]
        session_id = dispatched["session_id"]

        await asyncio.wait_for(gate.started.wait(), timeout=5)

        for _ in range(4):
            attempt = await registry.wait_task(task_id, timeout_sec=0.01)
            assert attempt["timed_out"] is True
            assert attempt["task_id"] == task_id
            assert attempt["session_id"] == session_id
            assert attempt["status"] == "running"
            assert "result_text" not in attempt

        assert gate.in_flight is True
        assert gate.calls == 1
        assert gated_fake.cancel_calls == []
        assert set(registry.tasks) == {task_id}
        assert not registry._done[task_id].is_set()

        final_waiter = asyncio.create_task(registry.wait_task(task_id, timeout_sec=5))
        try:
            await asyncio.sleep(0)
            assert not final_waiter.done()
            gate.release.set()
            completed = await asyncio.wait_for(final_waiter, timeout=5)
        finally:
            if not final_waiter.done():
                final_waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await final_waiter

        expected_text = f"[fake:fake] {task_id}:multi-timeout-job"
        assert completed["timed_out"] is False
        assert completed["task_id"] == task_id
        assert completed["session_id"] == session_id
        assert completed["status"] == "completed"
        assert completed["stop_reason"] == "end_turn"
        assert completed["persisted"] is True
        assert completed["result_text"] == expected_text

        full = await registry.get_result(task_id)
        assert full["task_id"] == task_id
        assert full["session_id"] == session_id
        assert full["status"] == "completed"
        assert full["result_text"] == expected_text
        assert full["result_complete"] is True
        assert full["result_source"] == "artifact"
        assert gated_fake.turn_calls == [(task_id, session_id, "multi-timeout-job")]
        assert registry.sessions[session_id].turns == 1
    finally:
        gate.release.set()
        await registry.stop()


@pytest.mark.asyncio
async def test_parallel_waits_let_fast_task_finish_and_return_while_slow_task_runs(
    bridge_home: Path,
    tmp_path: Path,
    gated_fake: _FakeRecorder,
) -> None:
    """When two independent tasks are awaited in parallel, the fast task reaches terminal state and returns first."""
    slow_dir = tmp_path / "slow"
    fast_dir = tmp_path / "fast"
    slow_dir.mkdir()
    fast_dir.mkdir()
    slow_gate = gated_fake.gate_for("slow-task")
    fast_gate = gated_fake.gate_for("fast-task")

    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        slow = await registry.dispatch_task("fake", "slow-task", cwd=str(slow_dir.resolve()))
        fast = await registry.dispatch_task("fake", "fast-task", cwd=str(fast_dir.resolve()))
        slow_id = slow["task_id"]
        fast_id = fast["task_id"]
        assert slow_id != fast_id
        assert slow["session_id"] != fast["session_id"]

        await asyncio.wait_for(
            asyncio.gather(slow_gate.started.wait(), fast_gate.started.wait()),
            timeout=5,
        )
        assert slow_gate.in_flight is True
        assert fast_gate.in_flight is True

        slow_wait = asyncio.create_task(registry.wait_task(slow_id, timeout_sec=5))
        fast_wait = asyncio.create_task(registry.wait_task(fast_id, timeout_sec=5))
        try:
            await asyncio.sleep(0)
            assert not slow_wait.done()
            assert not fast_wait.done()

            fast_gate.release.set()
            done, pending = await asyncio.wait(
                {slow_wait, fast_wait},
                timeout=5,
                return_when=asyncio.FIRST_COMPLETED,
            )
            assert done == {fast_wait}
            assert pending == {slow_wait}

            fast_result = await fast_wait
            assert fast_result["timed_out"] is False
            assert fast_result["task_id"] == fast_id
            assert fast_result["status"] == "completed"
            assert fast_result["persisted"] is True
            assert fast_result["result_text"] == f"[fake:fake] {fast_id}:fast-task"

            assert slow_gate.in_flight is True
            assert not slow_wait.done()
            assert registry.tasks[slow_id].status == TaskStatus.running
            slow_check = await registry.check_task(slow_id)
            assert slow_check["status"] == "running"

            slow_gate.release.set()
            slow_result = await asyncio.wait_for(slow_wait, timeout=5)
            assert slow_result["timed_out"] is False
            assert slow_result["task_id"] == slow_id
            assert slow_result["status"] == "completed"
            assert slow_result["persisted"] is True
            assert slow_result["result_text"] == f"[fake:fake] {slow_id}:slow-task"
        finally:
            for waiter in (slow_wait, fast_wait):
                if not waiter.done():
                    waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await waiter
    finally:
        slow_gate.release.set()
        fast_gate.release.set()
        await registry.stop()


@pytest.mark.asyncio
async def test_insert_task_while_wait_is_pending(bridge_home, tmp_path, gated_fake):
    registry = Registry.create(bridge_home)
    await registry.start()
    slow_gate = gated_fake.gate_for("slow")
    fast_gate = gated_fake.gate_for("inserted")
    slow_wait = None
    try:
        slow = await registry.dispatch_task("fake", "slow", cwd=str(tmp_path))
        await asyncio.wait_for(slow_gate.started.wait(), 2)
        slow_wait = asyncio.create_task(registry.wait_task(slow["task_id"], 10))
        await asyncio.sleep(0)
        assert not slow_wait.done()
        # Dispatch occurs AFTER the first wait started, not before two parallel waits.
        fast = await asyncio.wait_for(registry.dispatch_task("fake", "inserted", cwd=str(tmp_path)), 2)
        await asyncio.wait_for(fast_gate.started.wait(), 2)
        fast_gate.release.set()
        result = await asyncio.wait_for(registry.wait_task(fast["task_id"], 2), 3)
        assert result["status"] == "completed"
        assert not slow_wait.done()
        assert slow_gate.in_flight
        assert slow_gate.calls == fast_gate.calls == 1
        slow_gate.release.set()
        assert (await asyncio.wait_for(slow_wait, 2))["status"] == "completed"
    finally:
        slow_gate.release.set()
        fast_gate.release.set()
        if slow_wait is not None and not slow_wait.done():
            slow_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await slow_wait
        await registry.stop()


@pytest.mark.asyncio
async def test_stop_receiving_does_not_cancel_worker_or_lose_result(bridge_home, tmp_path, gated_fake):
    registry = Registry.create(bridge_home)
    await registry.start()
    gate = gated_fake.gate_for("survives-receiver-pause")
    receiver = None
    try:
        task = await registry.dispatch_task("fake", "survives-receiver-pause", cwd=str(tmp_path))
        await asyncio.wait_for(gate.started.wait(), 2)
        receiver = asyncio.create_task(registry.wait_task(task["task_id"], 10))
        await asyncio.sleep(0)
        receiver.cancel()
        with pytest.raises(asyncio.CancelledError):
            await receiver
        assert gate.in_flight
        assert gated_fake.cancel_calls == []
        assert not registry._bg[task["task_id"]].done()
        gate.release.set()
        result = await asyncio.wait_for(registry.wait_task(task["task_id"], 2), 3)
        assert result["status"] == "completed"
        assert result["persisted"] is True
        assert gate.calls == 1
    finally:
        gate.release.set()
        if receiver is not None and not receiver.done():
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver
        await registry.stop()
