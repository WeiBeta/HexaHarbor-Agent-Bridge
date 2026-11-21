"""Token normalization, USD cost calculation, and billing aggregation.

All ledger records intentionally store task metadata and token counters only —
never prompt messages, worker output text, or conversation transcripts.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_bridge.billing_pricing import (
    DEFAULT_MODEL_BUCKET,
    HarnessDiscountRule,
    PricingConfig,
    resolve_harness_discount,
    resolve_model_rate,
)

UsageScope = Literal["turn", "conversation", "unknown"]

_MAX_ERROR_CHARS = 500
_MAX_WARNING_CHARS = 300
_MAX_WARNINGS = 10


def _nonneg_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _pick_int(raw: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in raw:
            parsed = _nonneg_int(raw.get(key))
            if parsed is not None:
                return parsed
    return None


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def compute_duration_ms(started_at: str | None, finished_at: str | None) -> int | None:
    start = _parse_iso(started_at)
    end = _parse_iso(finished_at)
    if start is None or end is None:
        return None
    delta = int((end - start).total_seconds() * 1000)
    return max(0, delta)


class NormalizedTokens(BaseModel):
    """Per-task normalized token breakdown (never coerces missing data to 0)."""

    measured: bool = False
    input_tokens: int | None = None  # Total prompt tokens (uncached + cache_read + cache_write)
    uncached_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None  # Includes reasoning/thinking tokens when nested
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cache_reported: bool = False
    delta_from_conversation: bool = False
    delta_reset_warning: bool = False


class LedgerRecord(BaseModel):
    """One append-only billing record for a terminal Bridge task (metadata only)."""

    schema_version: int = 1
    task_id: str
    session_id: str
    native_session_id: str | None = None
    agent: str
    cwd: str = ""
    title: str | None = None
    model: str | None = None
    observed_model: str | None = None
    effective_model: str = DEFAULT_MODEL_BUCKET
    effort: str | None = None
    observed_effort: str | None = None
    observed_permission_mode: str | None = None
    status: str = "completed"
    stop_reason: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    files_changed_total: int = 0
    files_outside_cwd_total: int = 0
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    usage_scope: UsageScope = "unknown"
    usage_raw: dict[str, Any] = Field(default_factory=dict)
    tokens: NormalizedTokens = Field(default_factory=NormalizedTokens)


class CostBreakdown(BaseModel):
    """Computed USD cost breakdown for one ledger record."""

    currency: Literal["USD"] = "USD"
    priced: bool = False
    list_cost_usd: float | None = None
    effective_cost_usd: float | None = None
    input_cost_usd: float | None = None
    cache_read_cost_usd: float | None = None
    cache_write_cost_usd: float | None = None
    output_cost_usd: float | None = None
    matched_price_key: str | None = None
    price_source: str | None = None
    rate_input: float | None = None
    rate_output: float | None = None
    rate_cache_read: float | None = None
    rate_cache_write: float | None = None
    discount_rule_id: str | None = None
    discount_label: str | None = None
    discount_multiplier: float = 1.0
    discount_quota_boost_pct: float = 100.0
    unavailable_reason: str | None = None


def _extract_raw_snapshot(agent: str, usage_raw: dict[str, Any]) -> NormalizedTokens:
    """Normalize a single usage dict into total-prompt + uncached + cache + output counts.

    Adapter conventions observed in Agent-Bridge:
    - Antigravity (`agy`): `input_tokens` is *uncached* input only
      (`input_tokens + output_tokens == total_tokens` while `cache_read_tokens` is separate).
      `output_tokens` already includes `thinking_tokens`.
    - Codex / OpenAI: `input_tokens` *includes* `cached_input_tokens`; `output_tokens`
      already includes `reasoning_output_tokens`.
    - Anthropic / ACP: `input_tokens` (or `inputTokens`) excludes `cache_read_input_tokens`
      and `cache_creation_input_tokens` when those Anthropic keys are present; plain ACP
      `UsageUpdate` may report `inputTokens` / `outputTokens` / `totalTokens` without cache.
    """
    if not isinstance(usage_raw, dict) or not usage_raw:
        return NormalizedTokens(measured=False)

    agent_l = agent.strip().lower()
    raw_input = _pick_int(usage_raw, "input_tokens", "inputTokens", "prompt_tokens", "promptTokens")
    raw_output = _pick_int(usage_raw, "output_tokens", "outputTokens", "completion_tokens", "completionTokens")
    raw_total = _pick_int(usage_raw, "total_tokens", "totalTokens")
    reasoning = _pick_int(
        usage_raw,
        "thinking_tokens",
        "thinkingTokens",
        "reasoning_output_tokens",
        "reasoningOutputTokens",
        "reasoning_tokens",
        "reasoningTokens",
    )

    # Cache keys by schema family
    agy_cache_read = _pick_int(usage_raw, "cache_read_tokens", "cacheReadTokens")
    openai_cache_read = _pick_int(usage_raw, "cached_input_tokens", "cachedInputTokens", "cached_tokens")
    anth_cache_read = _pick_int(usage_raw, "cache_read_input_tokens", "cacheReadInputTokens")

    cache_write = _pick_int(
        usage_raw,
        "cache_write_input_tokens",
        "cacheWriteInputTokens",
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
        "cache_write_tokens",
        "cacheWriteTokens",
    )

    if raw_input is None and raw_output is None and raw_total is None and agy_cache_read is None and openai_cache_read is None and anth_cache_read is None:
        return NormalizedTokens(measured=False)

    uncached_input: int | None = None
    total_input: int | None = None
    cache_read: int | None = None
    cache_reported = False

    if agent_l == "antigravity" or (
        agy_cache_read is not None and openai_cache_read is None and anth_cache_read is None
    ):
        # Antigravity reports uncached input in `input_tokens` and separate `cache_read_tokens`.
        cache_read = agy_cache_read
        cache_reported = cache_read is not None
        if raw_input is not None:
            uncached_input = raw_input
            total_input = raw_input + (cache_read or 0) + (cache_write or 0)
        elif cache_read is not None:
            uncached_input = 0
            total_input = cache_read + (cache_write or 0)
    elif openai_cache_read is not None or agent_l == "codex":
        # OpenAI / Codex: `input_tokens` includes `cached_input_tokens`.
        cache_read = openai_cache_read
        cache_reported = cache_read is not None
        if raw_input is not None:
            total_input = raw_input
            if cache_read is not None:
                # Guard against malformed payloads where cache exceeds total input.
                if cache_read <= raw_input:
                    uncached_input = raw_input - cache_read
                else:
                    uncached_input = raw_input
                    total_input = raw_input + cache_read
            else:
                uncached_input = raw_input
    elif anth_cache_read is not None or (
        "cache_creation_input_tokens" in usage_raw or "cacheCreationInputTokens" in usage_raw
    ):
        # Anthropic: `input_tokens` is uncached input; cache read & creation are additive.
        cache_read = anth_cache_read
        cache_reported = cache_read is not None or cache_write is not None
        if raw_input is not None:
            uncached_input = raw_input
            total_input = raw_input + (cache_read or 0) + (cache_write or 0)
        elif cache_read is not None or cache_write is not None:
            uncached_input = 0
            total_input = (cache_read or 0) + (cache_write or 0)
    else:
        # Generic ACP/CLI usage with no cache fields reported.
        if raw_input is not None:
            uncached_input = raw_input
            total_input = raw_input
        cache_read = None
        cache_reported = False

    output = raw_output
    computed_total: int | None = (
        (total_input or 0) + (output or 0)
        if total_input is not None or output is not None
        else raw_total
    )

    return NormalizedTokens(
        measured=True,
        input_tokens=total_input,
        uncached_input_tokens=uncached_input,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output,
        reasoning_tokens=reasoning,
        total_tokens=computed_total,
        cache_reported=cache_reported,
    )


def _subtract_optional(curr: int | None, prev: int | None) -> tuple[int | None, bool]:
    if curr is None:
        return None, False
    if prev is None:
        return curr, False
    if curr < prev:
        return curr, True
    return curr - prev, False


def normalize_usage(
    agent: str,
    usage_raw: dict[str, Any],
    *,
    previous_cumulative: NormalizedTokens | None = None,
) -> tuple[UsageScope, NormalizedTokens, NormalizedTokens | None]:
    """Normalize a task's raw usage and compute turn delta if conversation-scoped.

    Returns ``(scope, per_task_tokens, cumulative_snapshot_for_next_turn)``.
    """
    raw_scope = str(usage_raw.get("scope") or "").strip().lower() if isinstance(usage_raw, dict) else ""
    scope: UsageScope = "conversation" if raw_scope == "conversation" else ("turn" if raw_scope == "turn" else "unknown")

    snapshot = _extract_raw_snapshot(agent, usage_raw)
    if not snapshot.measured:
        return scope, snapshot, previous_cumulative

    if scope != "conversation" or previous_cumulative is None or not previous_cumulative.measured:
        # First turn of a session (scope == "turn") is also the baseline cumulative counter
        # for subsequent resumed turns on Antigravity.
        next_cum = snapshot if agent.strip().lower() == "antigravity" else None
        return scope, snapshot, next_cum

    # Conversation-scoped resumed turn: subtract the prior cumulative snapshot.
    uncached, r1 = _subtract_optional(snapshot.uncached_input_tokens, previous_cumulative.uncached_input_tokens)
    cache_read, r2 = _subtract_optional(snapshot.cache_read_tokens, previous_cumulative.cache_read_tokens)
    cache_write, r3 = _subtract_optional(snapshot.cache_write_tokens, previous_cumulative.cache_write_tokens)
    output, r4 = _subtract_optional(snapshot.output_tokens, previous_cumulative.output_tokens)
    reasoning, r5 = _subtract_optional(snapshot.reasoning_tokens, previous_cumulative.reasoning_tokens)

    reset_warn = r1 or r2 or r3 or r4 or r5
    if reset_warn:
        # Counter reset or rolled back; keep raw snapshot as fallback and flag warning.
        delta = snapshot.model_copy(update={"delta_from_conversation": True, "delta_reset_warning": True})
        return scope, delta, snapshot

    total_input: int | None = None
    if uncached is not None or cache_read is not None or cache_write is not None:
        total_input = (uncached or 0) + (cache_read or 0) + (cache_write or 0)
    total_tokens: int | None = None
    if total_input is not None or output is not None:
        total_tokens = (total_input or 0) + (output or 0)
    elif snapshot.total_tokens is not None:
        total_tokens, _ = _subtract_optional(snapshot.total_tokens, previous_cumulative.total_tokens)

    delta = NormalizedTokens(
        measured=True,
        input_tokens=total_input,
        uncached_input_tokens=uncached,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output,
        reasoning_tokens=reasoning,
        total_tokens=total_tokens,
        cache_reported=snapshot.cache_reported,
        delta_from_conversation=True,
        delta_reset_warning=False,
    )
    return scope, delta, snapshot


def build_ledger_record(
    payload: dict[str, Any],
    *,
    previous_cumulative: NormalizedTokens | None = None,
) -> tuple[LedgerRecord, NormalizedTokens | None]:
    """Construct a metadata-only ``LedgerRecord`` from a receipt or state task dict."""
    agent = str(payload.get("agent") or "unknown").strip()
    model = str(payload["model"]).strip() if isinstance(payload.get("model"), str) and payload["model"].strip() else None
    observed_model = (
        str(payload["observed_model"]).strip()
        if isinstance(payload.get("observed_model"), str) and payload["observed_model"].strip()
        else None
    )
    effective_model = observed_model or model or DEFAULT_MODEL_BUCKET

    effort = str(payload["effort"]).strip() if isinstance(payload.get("effort"), str) and payload["effort"].strip() else None
    observed_effort = (
        str(payload["observed_effort"]).strip()
        if isinstance(payload.get("observed_effort"), str) and payload["observed_effort"].strip()
        else None
    )
    observed_pm = (
        str(payload["observed_permission_mode"]).strip()
        if isinstance(payload.get("observed_permission_mode"), str) and payload["observed_permission_mode"].strip()
        else None
    )

    raw_usage = payload.get("usage")
    usage_dict: dict[str, Any] = dict(raw_usage) if isinstance(raw_usage, dict) else {}
    scope, tokens, next_cum = normalize_usage(agent, usage_dict, previous_cumulative=previous_cumulative)

    raw_error = payload.get("error")
    error_text = str(raw_error)[:_MAX_ERROR_CHARS] if isinstance(raw_error, str) and raw_error.strip() else None

    raw_warnings = payload.get("warnings")
    warnings: list[str] = []
    if isinstance(raw_warnings, list):
        for item in raw_warnings[:_MAX_WARNINGS]:
            if isinstance(item, str) and item.strip():
                warnings.append(item[:_MAX_WARNING_CHARS])

    started_at = payload.get("started_at") if isinstance(payload.get("started_at"), str) else None
    finished_at = payload.get("finished_at") if isinstance(payload.get("finished_at"), str) else None
    raw_created = payload.get("created_at")
    created_at: str = (
        raw_created
        if isinstance(raw_created, str) and raw_created
        else (started_at or finished_at or datetime.now(UTC).isoformat())
    )

    files_changed_total = _nonneg_int(payload.get("files_changed_total"))
    if files_changed_total is None:
        fc = payload.get("files_changed")
        files_changed_total = len(fc) if isinstance(fc, list) else 0
    files_outside_total = _nonneg_int(payload.get("files_outside_cwd_total"))
    if files_outside_total is None:
        fo = payload.get("files_outside_cwd")
        files_outside_total = len(fo) if isinstance(fo, list) else 0

    record = LedgerRecord(
        task_id=str(payload.get("task_id") or ""),
        session_id=str(payload.get("session_id") or ""),
        native_session_id=(
            str(payload["native_session_id"])
            if isinstance(payload.get("native_session_id"), str) and payload["native_session_id"]
            else None
        ),
        agent=agent,
        cwd=str(payload.get("cwd") or ""),
        title=str(payload["title"]) if isinstance(payload.get("title"), str) and payload["title"] else None,
        model=model,
        observed_model=observed_model,
        effective_model=effective_model,
        effort=effort,
        observed_effort=observed_effort,
        observed_permission_mode=observed_pm,
        status=str(payload.get("status") or "completed"),
        stop_reason=str(payload["stop_reason"]) if isinstance(payload.get("stop_reason"), str) else None,
        error=error_text,
        warnings=warnings,
        files_changed_total=files_changed_total,
        files_outside_cwd_total=files_outside_total,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=compute_duration_ms(started_at, finished_at),
        usage_scope=scope,
        usage_raw=usage_dict,
        tokens=tokens,
    )
    return record, next_cum


def compute_record_cost(record: LedgerRecord, pricing: PricingConfig) -> CostBreakdown:
    """Compute USD list cost and harness-discounted effective cost for one record."""
    tokens = record.tokens
    if not tokens.measured:
        return CostBreakdown(priced=False, unavailable_reason="usage_missing")
    if tokens.uncached_input_tokens is None and tokens.output_tokens is None and tokens.cache_read_tokens is None:
        # Only total_tokens was reported; cannot split input vs output prices honestly.
        return CostBreakdown(priced=False, unavailable_reason="usage_breakdown_missing")

    match = resolve_model_rate(record.agent, record.effective_model, pricing)
    if match is None:
        return CostBreakdown(priced=False, unavailable_reason="price_unmatched")

    rate = match.rate
    in_cost = (tokens.uncached_input_tokens or 0) * rate.input / 1_000_000.0
    cr_cost = (tokens.cache_read_tokens or 0) * rate.cache_read / 1_000_000.0
    cw_cost = (tokens.cache_write_tokens or 0) * rate.cache_write / 1_000_000.0
    out_cost = (tokens.output_tokens or 0) * rate.output / 1_000_000.0
    list_cost = in_cost + cr_cost + cw_cost + out_cost

    discount: HarnessDiscountRule | None = resolve_harness_discount(record.agent, record.effective_model, pricing)
    multiplier = discount.effective_multiplier() if discount is not None else 1.0
    effective_cost = list_cost * multiplier

    return CostBreakdown(
        priced=True,
        list_cost_usd=list_cost,
        effective_cost_usd=effective_cost,
        input_cost_usd=in_cost,
        cache_read_cost_usd=cr_cost,
        cache_write_cost_usd=cw_cost,
        output_cost_usd=out_cost,
        matched_price_key=match.matched_key,
        price_source=match.source,
        rate_input=rate.input,
        rate_output=rate.output,
        rate_cache_read=rate.cache_read,
        rate_cache_write=rate.cache_write,
        discount_rule_id=discount.id if discount is not None else None,
        discount_label=(discount.label or f"{discount.agent}:{discount.model_pattern}") if discount is not None else None,
        discount_multiplier=multiplier,
        discount_quota_boost_pct=discount.quota_boost_pct if discount is not None else 100.0,
    )


def record_timestamp(record: LedgerRecord) -> datetime | None:
    return _parse_iso(record.started_at or record.created_at or record.finished_at)


def record_tok_per_sec(record: LedgerRecord) -> float | None:
    if record.tokens.output_tokens is None or record.tokens.output_tokens <= 0:
        return None
    if record.duration_ms is None or record.duration_ms <= 0:
        return None
    return record.tokens.output_tokens / (record.duration_ms / 1000.0)


def record_cache_hit_rate(record: LedgerRecord) -> float | None:
    tokens = record.tokens
    if not tokens.cache_reported or tokens.cache_read_tokens is None:
        return None
    denom = tokens.input_tokens
    if denom is None or denom <= 0:
        return None
    return tokens.cache_read_tokens / denom


def resolve_time_window(
    range_name: str = "all",
    *,
    start_iso: str | None = None,
    end_iso: str | None = None,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    current = now or datetime.now().astimezone()
    key = (range_name or "all").strip().lower()
    if key == "all":
        return None, None
    if key == "today":
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, None
    if key == "15m":
        return current - timedelta(minutes=15), None
    if key == "1h":
        return current - timedelta(hours=1), None
    if key == "24h":
        return current - timedelta(hours=24), None
    if key == "7d":
        start = (current - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, None
    if key == "30d":
        start = (current - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, None
    if key == "custom":
        start_dt = _parse_iso(start_iso)
        end_dt = _parse_iso(end_iso)
        return start_dt, end_dt
    return None, None


def filter_records(
    records: list[LedgerRecord],
    *,
    agents: set[str] | None = None,
    models: set[str] | None = None,
    status: str | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
    speed: str | None = None,
    query: str | None = None,
    range_name: str = "all",
    start_iso: str | None = None,
    end_iso: str | None = None,
    day_filter: str | None = None,
    now: datetime | None = None,
) -> list[LedgerRecord]:
    start_dt, end_dt = resolve_time_window(range_name, start_iso=start_iso, end_iso=end_iso, now=now)
    local_tz = (now or datetime.now().astimezone()).tzinfo
    q = (query or "").strip().lower()
    cwd_norm = (cwd or "").strip().lower()
    sess_norm = (session_id or "").strip().lower()
    status_norm = (status or "").strip().lower()
    speed_norm = (speed or "").strip().lower()
    day_norm = (day_filter or "").strip()

    agents_l = {a.strip().lower() for a in agents} if agents is not None else None
    models_l = {m.strip().lower() for m in models} if models is not None else None

    out: list[LedgerRecord] = []
    for rec in records:
        if agents_l is not None and rec.agent.lower() not in agents_l:
            continue
        if models_l is not None:
            pair_key = f"{rec.agent.lower()}::{rec.effective_model.lower()}"
            if rec.effective_model.lower() not in models_l and pair_key not in models_l:
                continue
        if status_norm and status_norm != "all":
            if status_norm == "errors":
                if rec.status == "completed":
                    continue
            elif rec.status.lower() != status_norm:
                continue
        if cwd_norm and rec.cwd.lower() != cwd_norm:
            continue
        if sess_norm and sess_norm not in rec.session_id.lower() and (
            not rec.native_session_id or sess_norm not in rec.native_session_id.lower()
        ):
            continue
        ts = record_timestamp(rec)
        if start_dt is not None and (ts is None or ts < start_dt):
            continue
        if end_dt is not None and (ts is None or ts > end_dt):
            continue
        if day_norm:
            if ts is None:
                continue
            local_day = ts.astimezone(local_tz).strftime("%Y-%m-%d")
            if local_day != day_norm:
                continue
        if speed_norm and speed_norm != "all":
            tps = record_tok_per_sec(rec)
            if tps is None:
                continue
            if speed_norm == "slow" and not (tps < 15.0):
                continue
            if speed_norm == "medium" and not (15.0 <= tps < 50.0):
                continue
            if speed_norm == "fast" and not (tps >= 50.0):
                continue
        if q:
            haystack = " ".join(
                part
                for part in (
                    rec.task_id,
                    rec.session_id,
                    rec.agent,
                    rec.effective_model,
                    rec.model or "",
                    rec.observed_model or "",
                    rec.title or "",
                    rec.cwd,
                    rec.error or "",
                )
                if part
            ).lower()
            if q not in haystack:
                continue
        out.append(rec)
    return out


def enrich_record_summary(record: LedgerRecord, pricing: PricingConfig) -> dict[str, Any]:
    """Row representation for the Request/Task Detail list and drawer (no prompt/result text)."""
    cost = compute_record_cost(record, pricing)
    tps = record_tok_per_sec(record)
    hit_rate = record_cache_hit_rate(record)
    return {
        "task_id": record.task_id,
        "session_id": record.session_id,
        "native_session_id": record.native_session_id,
        "agent": record.agent,
        "cwd": record.cwd,
        "title": record.title,
        "model": record.model,
        "observed_model": record.observed_model,
        "effective_model": record.effective_model,
        "effort": record.effort,
        "observed_effort": record.observed_effort,
        "observed_permission_mode": record.observed_permission_mode,
        "status": record.status,
        "stop_reason": record.stop_reason,
        "error": record.error,
        "warnings": record.warnings,
        "files_changed_total": record.files_changed_total,
        "files_outside_cwd_total": record.files_outside_cwd_total,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "duration_ms": record.duration_ms,
        "tok_per_sec": round(tps, 2) if tps is not None else None,
        "cache_hit_rate": round(hit_rate, 6) if hit_rate is not None else None,
        "usage_scope": record.usage_scope,
        "tokens": record.tokens.model_dump(mode="json"),
        "cost": cost.model_dump(mode="json"),
        "usage_raw": record.usage_raw,
    }


def compute_summary(
    records: list[LedgerRecord],
    pricing: PricingConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute OpenCodex-inspired summary cards, daily bar series, model table, and agent table."""
    local_tz = (now or datetime.now().astimezone()).tzinfo

    total_tasks = len(records)
    measured_tasks = 0
    priced_tasks = 0
    unpriced_tasks = 0
    unmeasured_tasks = 0

    sum_uncached_input = 0
    sum_total_input = 0
    sum_cache_read = 0
    sum_cache_write = 0
    sum_output = 0
    sum_reasoning = 0
    sum_total_tokens = 0
    cache_obs_input = 0
    cache_obs_hits = 0
    cache_obs_tasks = 0
    sum_list_cost = 0.0
    sum_effective_cost = 0.0
    tps_output_sum = 0
    tps_duration_ms_sum = 0
    tps_samples = 0

    daily: dict[str, dict[str, Any]] = {}
    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    by_agent: dict[str, dict[str, Any]] = {}

    for rec in records:
        cost = compute_record_cost(rec, pricing)
        t = rec.tokens
        ts = record_timestamp(rec)
        day_key = ts.astimezone(local_tz).strftime("%Y-%m-%d") if ts is not None else "unknown"

        if day_key not in daily:
            daily[day_key] = {
                "date": day_key,
                "tasks": 0,
                "measured_tasks": 0,
                "uncached_input_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cache_observed_input_tokens": 0,
                "cache_observed_hits": 0,
                "list_cost_usd": 0.0,
                "effective_cost_usd": 0.0,
                "priced_tasks": 0,
                "unpriced_tasks": 0,
            }
        d_bucket = daily[day_key]
        d_bucket["tasks"] += 1

        mk = (rec.agent, rec.effective_model)
        if mk not in by_model:
            by_model[mk] = {
                "agent": rec.agent,
                "model": rec.effective_model,
                "tasks": 0,
                "measured_tasks": 0,
                "priced_tasks": 0,
                "unpriced_tasks": 0,
                "uncached_input_tokens": 0,
                "input_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "cache_observed_input_tokens": 0,
                "cache_observed_hits": 0,
                "cache_observed_tasks": 0,
                "list_cost_usd": 0.0,
                "effective_cost_usd": 0.0,
                "tps_output_sum": 0,
                "tps_duration_ms_sum": 0,
                "tps_samples": 0,
                "discount_multiplier": cost.discount_multiplier,
                "discount_quota_boost_pct": cost.discount_quota_boost_pct,
                "discount_label": cost.discount_label,
                "price_source": cost.price_source,
                "matched_price_key": cost.matched_price_key,
            }
        m_row = by_model[mk]
        m_row["tasks"] += 1
        if cost.matched_price_key and not m_row["matched_price_key"]:
            m_row["matched_price_key"] = cost.matched_price_key
            m_row["price_source"] = cost.price_source
        if cost.discount_label and not m_row["discount_label"]:
            m_row["discount_label"] = cost.discount_label
            m_row["discount_multiplier"] = cost.discount_multiplier
            m_row["discount_quota_boost_pct"] = cost.discount_quota_boost_pct

        if rec.agent not in by_agent:
            by_agent[rec.agent] = {
                "agent": rec.agent,
                "tasks": 0,
                "measured_tasks": 0,
                "priced_tasks": 0,
                "unpriced_tasks": 0,
                "uncached_input_tokens": 0,
                "input_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cache_observed_input_tokens": 0,
                "cache_observed_hits": 0,
                "list_cost_usd": 0.0,
                "effective_cost_usd": 0.0,
            }
        a_row = by_agent[rec.agent]
        a_row["tasks"] += 1

        if t.measured:
            measured_tasks += 1
            d_bucket["measured_tasks"] += 1
            m_row["measured_tasks"] += 1
            a_row["measured_tasks"] += 1

            u_in = t.uncached_input_tokens or 0
            tot_in = t.input_tokens or 0
            c_read = t.cache_read_tokens or 0
            c_write = t.cache_write_tokens or 0
            out = t.output_tokens or 0
            reas = t.reasoning_tokens or 0
            tot = t.total_tokens or 0

            sum_uncached_input += u_in
            sum_total_input += tot_in
            sum_cache_read += c_read
            sum_cache_write += c_write
            sum_output += out
            sum_reasoning += reas
            sum_total_tokens += tot

            d_bucket["uncached_input_tokens"] += u_in
            d_bucket["cache_read_tokens"] += c_read
            d_bucket["cache_write_tokens"] += c_write
            d_bucket["output_tokens"] += out
            d_bucket["total_tokens"] += tot

            m_row["uncached_input_tokens"] += u_in
            m_row["input_tokens"] += tot_in
            m_row["cache_read_tokens"] += c_read
            m_row["cache_write_tokens"] += c_write
            m_row["output_tokens"] += out
            m_row["reasoning_tokens"] += reas
            m_row["total_tokens"] += tot

            a_row["uncached_input_tokens"] += u_in
            a_row["input_tokens"] += tot_in
            a_row["cache_read_tokens"] += c_read
            a_row["cache_write_tokens"] += c_write
            a_row["output_tokens"] += out
            a_row["total_tokens"] += tot

            if t.cache_reported and t.cache_read_tokens is not None and t.input_tokens is not None:
                cache_obs_tasks += 1
                cache_obs_input += t.input_tokens
                cache_obs_hits += t.cache_read_tokens
                d_bucket["cache_observed_input_tokens"] += t.input_tokens
                d_bucket["cache_observed_hits"] += t.cache_read_tokens
                m_row["cache_observed_tasks"] += 1
                m_row["cache_observed_input_tokens"] += t.input_tokens
                m_row["cache_observed_hits"] += t.cache_read_tokens
                a_row["cache_observed_input_tokens"] += t.input_tokens
                a_row["cache_observed_hits"] += t.cache_read_tokens

            if t.output_tokens is not None and t.output_tokens > 0 and rec.duration_ms is not None and rec.duration_ms > 0:
                tps_output_sum += t.output_tokens
                tps_duration_ms_sum += rec.duration_ms
                tps_samples += 1
                m_row["tps_output_sum"] += t.output_tokens
                m_row["tps_duration_ms_sum"] += rec.duration_ms
                m_row["tps_samples"] += 1
        else:
            unmeasured_tasks += 1

        if cost.priced and cost.list_cost_usd is not None and cost.effective_cost_usd is not None:
            priced_tasks += 1
            sum_list_cost += cost.list_cost_usd
            sum_effective_cost += cost.effective_cost_usd
            d_bucket["priced_tasks"] += 1
            d_bucket["list_cost_usd"] += cost.list_cost_usd
            d_bucket["effective_cost_usd"] += cost.effective_cost_usd
            m_row["priced_tasks"] += 1
            m_row["list_cost_usd"] += cost.list_cost_usd
            m_row["effective_cost_usd"] += cost.effective_cost_usd
            a_row["priced_tasks"] += 1
            a_row["list_cost_usd"] += cost.list_cost_usd
            a_row["effective_cost_usd"] += cost.effective_cost_usd
        else:
            unpriced_tasks += 1
            d_bucket["unpriced_tasks"] += 1
            m_row["unpriced_tasks"] += 1
            a_row["unpriced_tasks"] += 1

    model_rows: list[dict[str, Any]] = []
    for row in by_model.values():
        share = (row["total_tokens"] / sum_total_tokens) if sum_total_tokens > 0 else 0.0
        obs_in = row["cache_observed_input_tokens"]
        hit_rate = (row["cache_observed_hits"] / obs_in) if obs_in > 0 else None
        tok_per_sec = (
            row["tps_output_sum"] / (row["tps_duration_ms_sum"] / 1000.0)
            if row["tps_duration_ms_sum"] > 0
            else None
        )
        # Also resolve static pricing metadata when all tasks in this row were unmeasured.
        if row["matched_price_key"] is None:
            match = resolve_model_rate(row["agent"], row["model"], pricing)
            if match is not None:
                row["matched_price_key"] = match.matched_key
                row["price_source"] = match.source
        if row["discount_label"] is None:
            disc = resolve_harness_discount(row["agent"], row["model"], pricing)
            if disc is not None:
                row["discount_label"] = disc.label or f"{disc.agent}:{disc.model_pattern}"
                row["discount_multiplier"] = disc.effective_multiplier()
                row["discount_quota_boost_pct"] = disc.quota_boost_pct

        model_rows.append(
            {
                "agent": row["agent"],
                "model": row["model"],
                "tasks": row["tasks"],
                "measured_tasks": row["measured_tasks"],
                "priced_tasks": row["priced_tasks"],
                "unpriced_tasks": row["unpriced_tasks"],
                "uncached_input_tokens": row["uncached_input_tokens"],
                "input_tokens": row["input_tokens"],
                "cache_read_tokens": row["cache_read_tokens"] if row["cache_observed_tasks"] > 0 else None,
                "cache_write_tokens": row["cache_write_tokens"],
                "output_tokens": row["output_tokens"],
                "reasoning_tokens": row["reasoning_tokens"],
                "total_tokens": row["total_tokens"],
                "share_ratio": round(share, 6),
                "cache_hit_rate": round(hit_rate, 6) if hit_rate is not None else None,
                "cache_observed_input_tokens": obs_in,
                "tok_per_sec": round(tok_per_sec, 2) if tok_per_sec is not None else None,
                "tps_samples": row["tps_samples"],
                "list_cost_usd": round(row["list_cost_usd"], 6) if row["priced_tasks"] > 0 else None,
                "effective_cost_usd": round(row["effective_cost_usd"], 6) if row["priced_tasks"] > 0 else None,
                "matched_price_key": row["matched_price_key"],
                "price_source": row["price_source"],
                "discount_multiplier": row["discount_multiplier"],
                "discount_quota_boost_pct": row["discount_quota_boost_pct"],
                "discount_label": row["discount_label"],
            }
        )
    model_rows.sort(key=lambda item: (item["total_tokens"], item["tasks"]), reverse=True)

    agent_rows: list[dict[str, Any]] = []
    for a in by_agent.values():
        a_share = (a["total_tokens"] / sum_total_tokens) if sum_total_tokens > 0 else 0.0
        a_obs = a["cache_observed_input_tokens"]
        a_hit = (a["cache_observed_hits"] / a_obs) if a_obs > 0 else None
        agent_rows.append(
            {
                "agent": a["agent"],
                "tasks": a["tasks"],
                "measured_tasks": a["measured_tasks"],
                "priced_tasks": a["priced_tasks"],
                "unpriced_tasks": a["unpriced_tasks"],
                "uncached_input_tokens": a["uncached_input_tokens"],
                "input_tokens": a["input_tokens"],
                "cache_read_tokens": a["cache_read_tokens"],
                "cache_write_tokens": a["cache_write_tokens"],
                "output_tokens": a["output_tokens"],
                "total_tokens": a["total_tokens"],
                "share_ratio": round(a_share, 6),
                "cache_hit_rate": round(a_hit, 6) if a_hit is not None else None,
                "list_cost_usd": round(a["list_cost_usd"], 6) if a["priced_tasks"] > 0 else None,
                "effective_cost_usd": round(a["effective_cost_usd"], 6) if a["priced_tasks"] > 0 else None,
            }
        )
    agent_rows.sort(key=lambda item: (item["total_tokens"], item["tasks"]), reverse=True)

    daily_series = [daily[k] for k in sorted(daily) if k != "unknown"]
    for point in daily_series:
        point["list_cost_usd"] = round(point["list_cost_usd"], 6)
        point["effective_cost_usd"] = round(point["effective_cost_usd"], 6)
        obs_in = point.pop("cache_observed_input_tokens")
        obs_hits = point.pop("cache_observed_hits")
        point["cache_hit_rate"] = round(obs_hits / obs_in, 6) if obs_in > 0 else None

    overall_hit_rate = (cache_obs_hits / cache_obs_input) if cache_obs_input > 0 else None
    overall_tps = (
        tps_output_sum / (tps_duration_ms_sum / 1000.0) if tps_duration_ms_sum > 0 else None
    )

    return {
        "currency": "USD",
        "overview": {
            "tasks": total_tasks,
            "measured_tasks": measured_tasks,
            "unmeasured_tasks": unmeasured_tasks,
            "priced_tasks": priced_tasks,
            "unpriced_tasks": unpriced_tasks,
            "coverage_ratio": round(measured_tasks / total_tasks, 6) if total_tasks > 0 else None,
            "active_days": len(daily_series),
            "uncached_input_tokens": sum_uncached_input,
            "input_tokens": sum_total_input,
            "cache_read_tokens": sum_cache_read if cache_obs_tasks > 0 else None,
            "cache_write_tokens": sum_cache_write,
            "output_tokens": sum_output,
            "reasoning_tokens": sum_reasoning,
            "total_tokens": sum_total_tokens,
            "cache_hit_rate": round(overall_hit_rate, 6) if overall_hit_rate is not None else None,
            "cache_observed_input_tokens": cache_obs_input,
            "cache_observed_tasks": cache_obs_tasks,
            "tok_per_sec": round(overall_tps, 2) if overall_tps is not None else None,
            "tps_samples": tps_samples,
            "list_cost_usd": round(sum_list_cost, 6) if priced_tasks > 0 else None,
            "effective_cost_usd": round(sum_effective_cost, 6) if priced_tasks > 0 else None,
        },
        "daily": daily_series,
        "models": model_rows,
        "agents": agent_rows,
    }


def collect_agent_models(
    records: list[LedgerRecord],
    pricing: PricingConfig,
    configured_agents: list[str],
) -> dict[str, list[str]]:
    """Build per-agent model lists from ledger history union pricing overrides."""
    buckets: dict[str, set[str]] = defaultdict(set)
    for agent in configured_agents:
        buckets[agent]  # ensure key exists
    for rec in records:
        buckets[rec.agent].add(rec.effective_model)
    for key in pricing.models:
        if "::" in key:
            agent_part, model_part = key.split("::", 1)
            a = agent_part.strip()
            m = model_part.strip()
            if a and m:
                buckets[a].add(m)
    return {agent: sorted(models) for agent, models in buckets.items()}
