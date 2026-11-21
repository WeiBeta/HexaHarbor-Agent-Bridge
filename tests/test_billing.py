from __future__ import annotations

import pytest

from agent_bridge.billing import (
    build_ledger_record,
    compute_record_cost,
    compute_summary,
    filter_records,
    normalize_usage,
)
from agent_bridge.billing_pricing import (
    DEFAULT_MODEL_BUCKET,
    HarnessDiscountRule,
    ModelRate,
    PricingConfig,
    default_pricing_config,
    resolve_harness_discount,
    resolve_model_rate,
)


def test_antigravity_usage_treats_cache_read_as_exclusive():
    raw = {
        "input_tokens": 554149,
        "output_tokens": 206811,
        "thinking_tokens": 168650,
        "cache_read_tokens": 14005449,
        "total_tokens": 760960,
        "scope": "turn",
    }
    scope, tokens, cum = normalize_usage("antigravity", raw)
    assert scope == "turn"
    assert tokens.measured is True
    assert tokens.uncached_input_tokens == 554149
    assert tokens.cache_read_tokens == 14005449
    assert tokens.input_tokens == 554149 + 14005449
    assert tokens.output_tokens == 206811
    assert tokens.reasoning_tokens == 168650
    assert tokens.total_tokens == 554149 + 14005449 + 206811
    assert tokens.cache_reported is True
    assert tokens.delta_from_conversation is False
    assert cum is not None


def test_antigravity_conversation_scope_subtracts_previous_cumulative():
    turn1_raw = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "thinking_tokens": 50,
        "cache_read_tokens": 4000,
        "total_tokens": 1200,
        "scope": "turn",
    }
    turn2_raw = {
        "input_tokens": 1600,
        "output_tokens": 500,
        "thinking_tokens": 150,
        "cache_read_tokens": 9000,
        "total_tokens": 2100,
        "scope": "conversation",
    }
    _, t1, cum1 = normalize_usage("antigravity", turn1_raw)
    scope2, t2, cum2 = normalize_usage("antigravity", turn2_raw, previous_cumulative=cum1)
    assert t1.uncached_input_tokens == 1000
    assert scope2 == "conversation"
    assert t2.delta_from_conversation is True
    assert t2.delta_reset_warning is False
    assert t2.uncached_input_tokens == 600
    assert t2.cache_read_tokens == 5000
    assert t2.input_tokens == 5600
    assert t2.output_tokens == 300
    assert t2.reasoning_tokens == 100
    assert t2.total_tokens == 5900
    assert cum2 is not None and cum2.uncached_input_tokens == 1600

    # Counter rollback should warn and fall back cleanly without negative counts.
    turn3_reset = {
        "input_tokens": 300,
        "output_tokens": 100,
        "cache_read_tokens": 500,
        "scope": "conversation",
    }
    _, t3, _ = normalize_usage("antigravity", turn3_reset, previous_cumulative=cum2)
    assert t3.delta_from_conversation is True
    assert t3.delta_reset_warning is True
    assert t3.uncached_input_tokens == 300
    assert t3.cache_read_tokens == 500


def test_codex_usage_subtracts_cached_from_inclusive_input():
    raw = {
        "input_tokens": 316796,
        "cached_input_tokens": 255744,
        "cache_write_input_tokens": 0,
        "output_tokens": 12952,
        "reasoning_output_tokens": 7364,
    }
    _, tokens, _ = normalize_usage("codex", raw)
    assert tokens.measured is True
    assert tokens.input_tokens == 316796
    assert tokens.cache_read_tokens == 255744
    assert tokens.uncached_input_tokens == 316796 - 255744
    assert tokens.cache_write_tokens == 0
    assert tokens.output_tokens == 12952
    assert tokens.reasoning_tokens == 7364
    assert tokens.total_tokens == 316796 + 12952
    assert tokens.cache_reported is True


def test_acp_camelcase_and_missing_cache_keep_cache_as_none():
    _, tokens, _ = normalize_usage("cursor", {"inputTokens": 4000, "outputTokens": 1000})
    assert tokens.measured is True
    assert tokens.input_tokens == 4000
    assert tokens.uncached_input_tokens == 4000
    assert tokens.output_tokens == 1000
    assert tokens.cache_read_tokens is None
    assert tokens.cache_reported is False

    _, empty, _ = normalize_usage("grok", {})
    assert empty.measured is False
    assert empty.total_tokens is None


def test_build_ledger_record_never_retains_prompt_or_result_body():
    payload = {
        "task_id": "task_1",
        "session_id": "sess_1",
        "agent": "antigravity",
        "cwd": "C:/Dev/Proj",
        "title": "Review diff",
        "message": "TOP SECRET PROMPT BODY",
        "result_text": "TOP SECRET OUTPUT BODY",
        "result_preview": "SECRET PREVIEW",
        "result_file": "/tmp/.agent-bridge/results/task_1.txt",
        "status": "completed",
        "model": None,
        "observed_model": None,
        "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 200, "scope": "turn"},
        "created_at": "2026-11-20T10:00:00+00:00",
        "started_at": "2026-11-20T10:00:00+00:00",
        "finished_at": "2026-11-20T10:00:10+00:00",
    }
    rec, _ = build_ledger_record(payload)
    dumped = rec.model_dump_json()
    assert "TOP SECRET" not in dumped
    assert "SECRET PREVIEW" not in dumped
    assert "result_file" not in dumped
    assert rec.effective_model == DEFAULT_MODEL_BUCKET
    assert rec.duration_ms == 10000


def test_pricing_resolution_and_zcode_150_discount():
    cfg = default_pricing_config()

    # Verified preset resolves exact and derived effort variants.
    m_sol = resolve_model_rate("codex", "gpt-6.2-sol", cfg)
    assert m_sol is not None
    assert m_sol.source == "preset"
    assert m_sol.rate.input == 2.0

    m_argon = resolve_model_rate("antigravity", "gemini-4-argon-high", cfg)
    assert m_argon is not None
    assert m_argon.source == "preset_derived"
    assert m_argon.matched_key == "gemini-4-argon"

    # Unverified preview model must remain honestly unpriced until user configures it.
    assert resolve_model_rate("antigravity", "gemini-4-hydrogen-xhigh", cfg) is None

    # ZCode GLM 150% preset quota boost applies to zhipu\GLM-5.3 (1 / 1.5 = 0.666667).
    rec, _ = build_ledger_record(
        {
            "task_id": "task_z",
            "session_id": "sess_z",
            "agent": "zcode",
            "model": r"zhipu\GLM-5.3",
            "observed_model": r"zhipu\GLM-5.3",
            "status": "completed",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
            "created_at": "2026-11-20T12:00:00+00:00",
        }
    )
    cost = compute_record_cost(rec, cfg)
    assert cost.priced is True
    assert cost.matched_price_key == "glm-5.3"
    assert cost.list_cost_usd == pytest.approx(5.8, rel=1e-6)  # 1.4 + 4.4
    assert cost.discount_quota_boost_pct == 150.0
    assert cost.discount_multiplier == pytest.approx(100.0 / 150.0, rel=1e-5)
    assert cost.effective_cost_usd == pytest.approx(5.8 * (100.0 / 150.0), rel=1e-5)

    # Non-GLM model on zcode should NOT match glm* discount rule.
    assert resolve_harness_discount("zcode", "claude-sonnet-5-5", cfg) is None

    # Agent-scoped user override beats global user override and preset.
    custom = PricingConfig(
        models={
            "gpt-6.2-sol": ModelRate(input=9.0, output=19.0),
            "codex::gpt-6.2-sol": ModelRate(input=3.0, output=15.0, cache_read=0.3),
        },
        harness_discounts=[
            HarnessDiscountRule(
                id="codex-half",
                agent="codex",
                model_pattern="*",
                mode="cost_multiplier",
                cost_multiplier=0.5,
                label="Half price test",
            )
        ],
    )
    match = resolve_model_rate("codex", "gpt-6.2-sol", custom)
    assert match is not None and match.matched_key == "codex::gpt-6.2-sol" and match.source == "user"
    disc = resolve_harness_discount("codex", "gpt-6.2-sol", custom)
    assert disc is not None and disc.effective_multiplier() == 0.5 and disc.quota_boost_pct == 200.0


def test_summary_excludes_unpriced_and_computes_cache_rate_over_observed_only():
    cfg = default_pricing_config()
    rec_agy_unpriced, _ = build_ledger_record(
        {
            "task_id": "t1",
            "session_id": "s1",
            "agent": "antigravity",
            "model": "gemini-4-hydrogen-xhigh",
            "status": "completed",
            "usage": {"input_tokens": 1000, "cache_read_tokens": 9000, "output_tokens": 500, "scope": "turn"},
            "created_at": "2026-11-20T08:00:00+00:00",
            "started_at": "2026-11-20T08:00:00+00:00",
            "finished_at": "2026-11-20T08:00:10+00:00",
        }
    )
    rec_cursor_nocache, _ = build_ledger_record(
        {
            "task_id": "t2",
            "session_id": "s2",
            "agent": "cursor",
            "model": "auto",
            "status": "completed",
            "usage": {"inputTokens": 10000, "outputTokens": 1000},
            "created_at": "2026-11-20T09:00:00+00:00",
            "started_at": "2026-11-20T09:00:00+00:00",
            "finished_at": "2026-11-20T09:00:20+00:00",
        }
    )
    summary = compute_summary([rec_agy_unpriced, rec_cursor_nocache], cfg)
    ov = summary["overview"]
    assert ov["tasks"] == 2
    assert ov["measured_tasks"] == 2
    assert ov["priced_tasks"] == 1
    assert ov["unpriced_tasks"] == 1
    # Cache hit rate is computed only across tasks that reported cache (9000 / 10000 = 0.9),
    # NOT diluted by Cursor's unreported 10000 input tokens.
    assert ov["cache_hit_rate"] == pytest.approx(0.9, rel=1e-6)
    assert ov["effective_cost_usd"] == pytest.approx((10000 * 1.25 + 1000 * 6.0) / 1e6, rel=1e-6)

    # Filtering by agent and pair model key works accurately.
    only_agy = filter_records([rec_agy_unpriced, rec_cursor_nocache], agents={"antigravity"})
    assert [r.task_id for r in only_agy] == ["t1"]
    only_pair = filter_records(
        [rec_agy_unpriced, rec_cursor_nocache],
        models={"cursor::auto"},
    )
    assert [r.task_id for r in only_pair] == ["t2"]
