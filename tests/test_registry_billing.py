from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_bridge import billing_store
from agent_bridge import registry as registry_module
from agent_bridge.adapters.fake import FakeAdapter
from agent_bridge.models import TurnResult
from agent_bridge.paths import receipt_path, usage_ledger_path
from agent_bridge.persist import file_lock, read_json_strict
from agent_bridge.registry import Registry


@pytest.fixture
async def registry(bridge_home, monkeypatch):
    monkeypatch.delenv("AGENT_BRIDGE_MODE", raising=False)
    monkeypatch.setattr(registry_module, "build_adapter", FakeAdapter)
    instance = Registry.create(bridge_home, runtime_context="coordinator")
    await instance.start()
    try:
        yield instance
    finally:
        if not instance._stopping:
            await instance.stop()


async def _wait_for_billing(registry: Registry) -> None:
    with registry._billing_lock:
        work = registry._billing_work
    if work is not None:
        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(work)), timeout=5)
    assert registry._billing_work is None


async def test_receipts_and_waits_finish_behind_a_held_ledger_lock(registry, tmp_path, monkeypatch, caplog):
    home = registry.home
    loop = asyncio.get_running_loop()
    # A ledger write in the default executor would starve the state and receipt writers.
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    ledger_started = asyncio.Event()
    write_order = []

    def record(home, payload):
        assert threading.current_thread().daemon
        write_order.append(payload["task_id"])
        loop.call_soon_threadsafe(ledger_started.set)
        billing_store.record_receipt_to_ledger(home, payload)

    async def run_turn(self, session, task):
        turn = int(task.message)
        return TurnResult(
            text="synthetic worker output",
            usage={
                "scope": "turn" if turn == 1 else "conversation",
                "input_tokens": 100 * turn,
                "output_tokens": 10 * turn,
            },
        )

    monkeypatch.setattr(registry_module, "record_receipt_to_ledger", record)
    monkeypatch.setattr(FakeAdapter, "run_turn", run_turn)
    cwd = str(tmp_path)
    task_ids = []
    session_id = None
    request_id = str(uuid.uuid4())

    with file_lock(billing_store._ledger_lock(home)):
        for turn in range(1, 4):
            sent = await registry.dispatch_task(
                "antigravity", str(turn), cwd, session_id=session_id, request_id=request_id if turn == 1 else None
            )
            task_id = sent["task_id"]
            task_ids.append(task_id)
            session_id = sent["session_id"]
            received = await registry.wait_task(task_id, timeout_sec=2)
            assert received["timed_out"] is False
            assert received["persisted"] is True
            receipt = read_json_strict(receipt_path(task_id, home), None)
            assert receipt["task_id"] == task_id
            assert receipt["status"] == "completed"
            assert receipt["usage"]["input_tokens"] == turn * 100

        await asyncio.wait_for(ledger_started.wait(), timeout=2)
        assert write_order == task_ids[:1]
        assert billing_store.load_ledger(home) == ([], 0)
        reused = await registry.dispatch_task("antigravity", "1", cwd, request_id=request_id)
        assert reused["task_id"] == task_ids[0]
        assert reused["reused"] is True
        assert (await registry.wait_task(task_ids[0], timeout_sec=2))["status"] == "completed"
        await asyncio.wait_for(registry.stop(), timeout=2)
        assert "billing ledger still draining at shutdown" in caplog.text
        assert registry._billing_work is not None
        assert not registry._billing_work.done()

    await _wait_for_billing(registry)
    assert write_order == task_ids
    records, corrupt = billing_store.load_ledger(home)
    assert corrupt == 0
    assert [record.task_id for record in records] == task_ids
    assert [record.tokens.input_tokens for record in records] == [100, 100, 100]
    assert [record.tokens.output_tokens for record in records] == [10, 10, 10]
    assert not any(record.tokens.delta_reset_warning for record in records)

    assert billing_store.backfill_from_bridge_home(home)["imported"] == 0
    ledger = usage_ledger_path(home).read_bytes()
    assert b"synthetic worker output" not in ledger
    receipt_path(task_ids[0], home).unlink()
    assert usage_ledger_path(home).read_bytes() == ledger


async def test_waits_and_followups_respect_in_flight_atomic_receipt(registry, tmp_path, monkeypatch):
    loop = asyncio.get_running_loop()
    writing = asyncio.Event()
    release = threading.Event()
    original_write = registry_module.atomic_write_json

    def write(path: Path, payload):
        if path.parent.name == "receipts":
            loop.call_soon_threadsafe(writing.set)
            assert release.wait(timeout=5)
        original_write(path, payload)

    monkeypatch.setattr(registry_module, "atomic_write_json", write)
    sent = await registry.dispatch_task("fake", "first", str(tmp_path))
    task_id = sent["task_id"]
    try:
        await asyncio.wait_for(writing.wait(), timeout=2)
        assert not receipt_path(task_id, registry.home).exists()
        assert (await registry.wait_task(task_id, timeout_sec=0))["timed_out"] is True
        with pytest.raises(RuntimeError, match=r"session .* is busy"):
            await registry.dispatch_task("fake", "too early", str(tmp_path), session_id=sent["session_id"])
    finally:
        release.set()
    assert (await registry.wait_task(task_id, timeout_sec=2))["timed_out"] is False
    assert receipt_path(task_id, registry.home).is_file()
    await _wait_for_billing(registry)


async def test_cancel_before_turn_starts_writes_one_receipt_and_bills_once(registry, tmp_path):
    sent = await registry.dispatch_task("fake", "cancel now", str(tmp_path))
    task_id = sent["task_id"]
    cancelled = await registry.cancel_task(task_id)
    assert cancelled["status"] == "cancelled"
    receipt = read_json_strict(receipt_path(task_id, registry.home), None)
    assert receipt["status"] == "cancelled"
    assert receipt["started_at"] is None
    assert (await registry.cancel_task(task_id))["status"] == "cancelled"
    assert (await registry.wait_task(task_id, timeout_sec=2))["status"] == "cancelled"

    await _wait_for_billing(registry)
    records, corrupt = billing_store.load_ledger(registry.home)
    assert corrupt == 0
    assert [(record.task_id, record.status) for record in records] == [(task_id, "cancelled")]


async def test_cancelling_cancel_task_call_does_not_interrupt_receipt_writer(registry, tmp_path, monkeypatch):
    loop = asyncio.get_running_loop()
    writing = asyncio.Event()
    release = threading.Event()
    original_write = registry_module.atomic_write_json

    def write(path: Path, payload):
        if path.parent.name == "receipts":
            loop.call_soon_threadsafe(writing.set)
            assert release.wait(timeout=5)
        original_write(path, payload)

    async def dispatch_then_cancel():
        sent = await registry.dispatch_task("fake", "cancel before start", str(tmp_path))
        await registry.cancel_task(sent["task_id"])

    monkeypatch.setattr(registry_module, "atomic_write_json", write)
    caller = asyncio.create_task(dispatch_then_cancel())
    try:
        await asyncio.wait_for(writing.wait(), timeout=2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        (task_id,) = registry.tasks
        assert (await registry.wait_task(task_id, timeout_sec=0))["timed_out"] is True
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)

    received = await registry.wait_task(task_id, timeout_sec=2)
    assert received["timed_out"] is False
    assert received["status"] == "cancelled"
    assert read_json_strict(receipt_path(task_id, registry.home), None)["status"] == "cancelled"
    await _wait_for_billing(registry)
    records, _ = billing_store.load_ledger(registry.home)
    assert [record.task_id for record in records] == [task_id]


async def test_failed_receipt_write_does_not_enqueue_billing(registry, tmp_path, monkeypatch, caplog):
    original_write = registry_module.atomic_write_json

    def write(path: Path, payload):
        if path.parent.name == "receipts":
            raise OSError("synthetic receipt write failure")
        original_write(path, payload)

    monkeypatch.setattr(registry_module, "atomic_write_json", write)
    sent = await registry.dispatch_task("fake", "fail the receipt write", str(tmp_path))
    task_id = sent["task_id"]
    received = await registry.wait_task(task_id, timeout_sec=2)
    assert received["timed_out"] is False
    assert received["status"] == "completed"
    assert "could not persist receipt for task" in caplog.text
    assert not receipt_path(task_id, registry.home).exists()
    await _wait_for_billing(registry)
    assert billing_store.load_ledger(registry.home) == ([], 0)


async def test_billing_failure_leaves_receipt_for_existing_backfill(registry, tmp_path, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise OSError("synthetic ledger write failure")

    with monkeypatch.context() as failure:
        failure.setattr(billing_store, "append_receipt_record", fail)
        sent = await registry.dispatch_task("fake", "finished with receipt", str(tmp_path))
        task_id = sent["task_id"]
        received = await registry.wait_task(task_id, timeout_sec=2)
        assert received["timed_out"] is False
        assert received["status"] == "completed"
        assert receipt_path(task_id, registry.home).is_file()
        await _wait_for_billing(registry)
    assert "could not append billing ledger entry for task" in caplog.text
    assert billing_store.load_ledger(registry.home) == ([], 0)
    assert billing_store.backfill_from_bridge_home(registry.home)["imported"] == 1
    assert billing_store.backfill_from_bridge_home(registry.home)["imported"] == 0
