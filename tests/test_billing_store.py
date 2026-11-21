from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_bridge.billing_pricing import ModelRate, PricingConfig
from agent_bridge.billing_store import (
    MIN_LEDGER_MAX_BYTES,
    StoragePolicy,
    append_receipt_record,
    backfill_from_bridge_home,
    clear_ledger,
    enforce_retention,
    load_ledger,
    load_pricing,
    load_storage_policy,
    reset_pricing,
    save_pricing,
    save_storage_policy,
    storage_stats,
)
from agent_bridge.paths import usage_ledger_path


def _write_receipt(home: Path, payload: dict) -> None:
    rec_dir = home / "receipts"
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / f"{payload['task_id']}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_backfill_computes_agy_conversation_delta_and_is_idempotent(tmp_path: Path):
    _write_receipt(
        tmp_path,
        {
            "task_id": "task_a",
            "session_id": "sess_agy",
            "agent": "antigravity",
            "cwd": str(tmp_path),
            "title": "Turn 1",
            "status": "completed",
            "model": "gemini-4-argon",
            "result_preview": "DO NOT COPY RESULT TEXT",
            "usage": {"input_tokens": 500, "cache_read_tokens": 1000, "output_tokens": 200, "scope": "turn"},
            "created_at": "2026-11-20T10:00:00+00:00",
            "started_at": "2026-11-20T10:00:00+00:00",
            "finished_at": "2026-11-20T10:00:05+00:00",
        },
    )
    _write_receipt(
        tmp_path,
        {
            "task_id": "task_b",
            "session_id": "sess_agy",
            "agent": "antigravity",
            "cwd": str(tmp_path),
            "title": "Turn 2",
            "status": "completed",
            "model": "gemini-4-argon",
            "usage": {"input_tokens": 900, "cache_read_tokens": 3500, "output_tokens": 500, "scope": "conversation"},
            "created_at": "2026-11-20T10:01:00+00:00",
            "started_at": "2026-11-20T10:01:00+00:00",
            "finished_at": "2026-11-20T10:01:08+00:00",
        },
    )

    first = backfill_from_bridge_home(tmp_path)
    assert first["scanned"] == 2
    assert first["imported"] == 2
    assert first["skipped_existing"] == 0

    records, corrupt = load_ledger(tmp_path)
    assert corrupt == 0
    assert len(records) == 2
    by_id = {r.task_id: r for r in records}
    assert by_id["task_a"].tokens.uncached_input_tokens == 500
    assert by_id["task_b"].tokens.delta_from_conversation is True
    assert by_id["task_b"].tokens.uncached_input_tokens == 400
    assert by_id["task_b"].tokens.cache_read_tokens == 2500
    assert by_id["task_b"].tokens.output_tokens == 300

    # Ensure no body leaked onto disk.
    raw_disk = usage_ledger_path(tmp_path).read_text(encoding="utf-8")
    assert "DO NOT COPY RESULT TEXT" not in raw_disk

    # Second backfill must be a clean no-op.
    second = backfill_from_bridge_home(tmp_path)
    assert second["imported"] == 0
    assert second["skipped_existing"] == 2


def test_late_turn_repairs_only_affected_cumulative_usage(tmp_path):
    now = datetime.now(UTC)
    turns = [
        {
            "task_id": f"turn-{turn}",
            "session_id": "resumed",
            "agent": "antigravity",
            "status": "completed",
            "usage": {
                "scope": "turn" if turn == 1 else "conversation",
                "inputTokens": 100 * turn,
                "cacheReadTokens": 25 * turn,
            },
            "created_at": (now + timedelta(seconds=turn)).isoformat(),
        }
        for turn in range(1, 5)
    ]
    append_receipt_record(tmp_path, {**turns[0], "task_id": "other", "session_id": "other"})
    unrelated = usage_ledger_path(tmp_path).read_bytes()
    append_receipt_record(tmp_path, turns[0])
    append_receipt_record(tmp_path, turns[2])
    _write_receipt(tmp_path, turns[1])
    assert backfill_from_bridge_home(tmp_path)["imported"] == 1
    assert backfill_from_bridge_home(tmp_path)["imported"] == 0
    append_receipt_record(tmp_path, turns[3])
    items, corrupt = load_ledger(tmp_path)
    assert corrupt == 0
    assert usage_ledger_path(tmp_path).read_bytes().startswith(unrelated)
    measured = {record.task_id: record.tokens for record in items if record.session_id == "resumed"}
    assert len(measured) == 4
    assert all(tokens.uncached_input_tokens == 100 and tokens.cache_read_tokens == 25 for tokens in measured.values())


@pytest.mark.parametrize("use_backfill", [False, True])
def test_append_recovers_an_interrupted_jsonl_tail(tmp_path, use_backfill):
    now = datetime.now(UTC).isoformat()
    first = {"task_id": "first", "agent": "codex", "status": "completed", "created_at": now}
    second = {**first, "task_id": "second"}
    append_receipt_record(tmp_path, first)
    path = usage_ledger_path(tmp_path)
    committed = path.read_bytes()
    with path.open("ab") as handle:
        handle.write(b'{"task_id":"interrupted')
    if use_backfill:
        _write_receipt(tmp_path, second)
        assert backfill_from_bridge_home(tmp_path)["imported"] == 1
        assert backfill_from_bridge_home(tmp_path)["imported"] == 0
    else:
        append_receipt_record(tmp_path, second)
        assert append_receipt_record(tmp_path, second) is None
    rows, corrupt = load_ledger(tmp_path)
    assert corrupt == 0
    assert [row.task_id for row in rows] == ["first", "second"]
    assert path.read_bytes().startswith(committed)


def test_size_retention_keeps_newer_usage_when_old_receipt_arrives_last(tmp_path):
    now = datetime.now(UTC)
    save_storage_policy(StoragePolicy(max_bytes=MIN_LEDGER_MAX_BYTES, max_age_days=None), home=tmp_path)
    for task_id, days in [("middle", 2), ("newest", 1), ("old", 3)]:
        row = {
            "task_id": task_id,
            "agent": "codex",
            "status": "completed",
            "title": "a" * 400_000,
            "created_at": (now - timedelta(days=days)).isoformat(),
        }
        if task_id == "old":
            _write_receipt(tmp_path, row)
        else:
            append_receipt_record(tmp_path, row)
    assert backfill_from_bridge_home(tmp_path)["retention"]["removed_by_size"] == 1
    rows, corrupt = load_ledger(tmp_path)
    assert corrupt == 0
    assert [row.task_id for row in rows] == ["middle", "newest"]


@pytest.mark.parametrize("auto_rotate", [False, True])
def test_age_only_policy_applies_on_append_when_auto_rotate_is_enabled(tmp_path, auto_rotate):
    now = datetime.now(UTC)
    save_storage_policy(StoragePolicy(max_bytes=None, max_age_days=10, auto_rotate=auto_rotate), home=tmp_path)
    old = {
        "agent": "codex",
        "status": "completed",
        "task_id": "old",
        "created_at": (now - timedelta(days=20)).isoformat(),
    }
    append_receipt_record(tmp_path, old, enforce_policy=False)
    append_receipt_record(tmp_path, {**old, "task_id": "new", "created_at": now.isoformat()})
    rows, corrupt = load_ledger(tmp_path)
    assert corrupt == 0
    assert [row.task_id for row in rows] == (["new"] if auto_rotate else ["old", "new"])


def test_retention_by_age_and_size_and_clear(tmp_path: Path):
    now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    old_ts = (now - timedelta(days=200)).isoformat()
    recent_ts = (now - timedelta(days=2)).isoformat()

    append_receipt_record(
        tmp_path,
        {
            "task_id": "task_old",
            "session_id": "s_old",
            "agent": "codex",
            "status": "completed",
            "model": "gpt-6.2-sol",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "created_at": old_ts,
        },
        enforce_policy=False,
    )
    append_receipt_record(
        tmp_path,
        {
            "task_id": "task_new",
            "session_id": "s_new",
            "agent": "codex",
            "status": "completed",
            "model": "gpt-6.2-sol",
            "usage": {"input_tokens": 200, "output_tokens": 60},
            "created_at": recent_ts,
        },
        enforce_policy=False,
    )

    res = enforce_retention(tmp_path, StoragePolicy(max_age_days=180, max_bytes=None), now=now)
    assert res["changed"] is True
    assert res["removed_by_age"] == 1
    assert res["remaining_records"] == 1
    recs, _ = load_ledger(tmp_path)
    assert [r.task_id for r in recs] == ["task_new"]

    # Test size rotation above 1 MiB ceiling trims oldest rows cleanly to <= 90% target.
    big_title = "x" * 1800
    lpath = usage_ledger_path(tmp_path)
    with lpath.open("ab") as f:
        for i in range(650):
            row = {
                "schema_version": 1,
                "task_id": f"bulk_{i}",
                "session_id": "s_bulk",
                "agent": "antigravity",
                "cwd": "C:/Dev/Work",
                "title": big_title,
                "effective_model": "gemini-4-argon",
                "status": "completed",
                "created_at": recent_ts,
                "tokens": {"measured": True, "input_tokens": 10, "uncached_input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }
            f.write((json.dumps(row) + "\n").encode("utf-8"))
    assert lpath.stat().st_size > MIN_LEDGER_MAX_BYTES

    size_res = enforce_retention(
        tmp_path,
        StoragePolicy(max_age_days=None, max_bytes=MIN_LEDGER_MAX_BYTES),
        now=now,
    )
    assert size_res["changed"] is True
    assert size_res["removed_by_size"] > 0
    assert size_res["remaining_bytes"] <= int(MIN_LEDGER_MAX_BYTES * 0.9)
    surviving, corrupt = load_ledger(tmp_path)
    assert corrupt == 0
    assert surviving[-1].task_id == "bulk_649"

    # Pricing & storage policy survive clear_ledger.
    save_pricing(
        PricingConfig(models={"custom-model": ModelRate(input=1.2, output=3.4)}),
        home=tmp_path,
    )
    save_storage_policy(StoragePolicy(max_bytes=50 * 1024 * 1024, max_age_days=90), home=tmp_path)
    cleared = clear_ledger(tmp_path)
    assert cleared["cleared_records"] == len(surviving)
    assert load_ledger(tmp_path) == ([], 0)
    assert "custom-model" in load_pricing(tmp_path).models
    assert load_storage_policy(tmp_path).max_age_days == 90

    stats = storage_stats(tmp_path)
    assert stats["record_count"] == 0
    assert stats["policy"]["max_age_days"] == 90

    reset = reset_pricing(tmp_path)
    assert reset.models == {}
    assert any(d.id == "preset-zcode-glm-150" for d in reset.harness_discounts)
