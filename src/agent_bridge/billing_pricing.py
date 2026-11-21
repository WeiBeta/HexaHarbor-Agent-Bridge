"""USD API reference pricing catalog and harness discount rules.

Reference model prices (USD per 1M tokens) include public snapshots adapted
from ``@bitkyc08/opencodex`` v2.100.6 (MIT License, ``src/usage/expected-prices.ts``).
Models without a verified public list price intentionally remain unpriced until
the user sets a manual rate in ``~/.agent-bridge/billing/pricing.json`` or via
the localhost dashboard.
"""

from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_RATE_USD_PER_1M = 1_000_000.0
MAX_COST_MULTIPLIER = 10.0
MIN_QUOTA_BOOST_PCT = 1.0
MAX_QUOTA_BOOST_PCT = 10_000.0

DEFAULT_MODEL_BUCKET = "<default>"

_VARIANT_SUFFIX_RE = re.compile(r"(?:\[[^\]]+\]|#[^#]+)$")
_EFFORT_SUFFIX_RE = re.compile(
    r"-(?:extra-low|xhigh|medium|mid|low|high|max|thinking|preview)$",
    re.IGNORECASE,
)


def _validate_finite_rate(value: float, *, field_name: str) -> float:
    num = float(value)
    if not math.isfinite(num) or num < 0.0 or num > MAX_RATE_USD_PER_1M:
        raise ValueError(f"{field_name} must be a finite number between 0 and {MAX_RATE_USD_PER_1M:g}")
    return num


class ModelRate(BaseModel):
    """Per-1M-token API list rates in USD."""

    input: float = Field(ge=0.0, le=MAX_RATE_USD_PER_1M)
    output: float = Field(ge=0.0, le=MAX_RATE_USD_PER_1M)
    cache_read: float = Field(default=0.0, ge=0.0, le=MAX_RATE_USD_PER_1M)
    cache_write: float = Field(default=0.0, ge=0.0, le=MAX_RATE_USD_PER_1M)
    note: str = ""

    @field_validator("input", "output", "cache_read", "cache_write", mode="before")
    @classmethod
    def _check_rate(cls, v: Any, info: Any) -> float:
        if v is None or v == "":
            if info.field_name in {"cache_read", "cache_write"}:
                return 0.0
            raise ValueError(f"{info.field_name} is required")
        return _validate_finite_rate(float(v), field_name=str(info.field_name))


class HarnessDiscountRule(BaseModel):
    """Harness-level or harness+model promotional discount / quota-boost rule.

    Two equivalent input modes are supported:
    - ``quota_boost``: e.g. ``150`` means 150% effective quota per dollar
      (such as ZCode's 150% GLM quota boost), which corresponds to an effective
      cost multiplier of ``100 / 150 = 0.666667``.
    - ``cost_multiplier``: direct multiplier applied to list cost (e.g. ``0.6667``
      for 33.3% off, ``1.0`` for list price).
    """

    id: str
    agent: str
    model_pattern: str = "*"
    mode: Literal["quota_boost", "cost_multiplier"] = "quota_boost"
    quota_boost_pct: float = 100.0
    cost_multiplier: float = 1.0
    enabled: bool = True
    label: str = ""
    preset: bool = False

    @field_validator("id", "agent")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("value must be non-empty")
        return cleaned

    @field_validator("model_pattern", mode="before")
    @classmethod
    def _normalize_pattern(cls, v: Any) -> str:
        if v is None:
            return "*"
        text = str(v).strip()
        return text or "*"

    @model_validator(mode="after")
    def _sync_multiplier(self) -> HarnessDiscountRule:
        if self.mode == "quota_boost":
            pct = float(self.quota_boost_pct)
            if not math.isfinite(pct) or pct < MIN_QUOTA_BOOST_PCT or pct > MAX_QUOTA_BOOST_PCT:
                raise ValueError(
                    f"quota_boost_pct must be between {MIN_QUOTA_BOOST_PCT:g} and {MAX_QUOTA_BOOST_PCT:g}"
                )
            self.quota_boost_pct = round(pct, 4)
            self.cost_multiplier = round(100.0 / pct, 6)
        else:
            mult = float(self.cost_multiplier)
            if not math.isfinite(mult) or mult < 0.0 or mult > MAX_COST_MULTIPLIER:
                raise ValueError(f"cost_multiplier must be between 0 and {MAX_COST_MULTIPLIER:g}")
            self.cost_multiplier = round(mult, 6)
            self.quota_boost_pct = round(100.0 / mult, 4) if mult > 0 else 0.0
        return self

    def effective_multiplier(self) -> float:
        if not self.enabled:
            return 1.0
        return self.cost_multiplier


class PricingConfig(BaseModel):
    """User-managed pricing overrides and harness discount rules (USD only)."""

    schema_version: int = 1
    currency: Literal["USD"] = "USD"
    models: dict[str, ModelRate] = Field(default_factory=dict)
    harness_discounts: list[HarnessDiscountRule] = Field(default_factory=list)


@dataclass(frozen=True)
class PresetModelEntry:
    key: str
    agent_hint: str
    rate: ModelRate
    source: str = "preset_opencodex"


def _rate(
    inp: float,
    out: float,
    cache_read: float = 0.0,
    cache_write: float = 0.0,
    note: str = "",
) -> ModelRate:
    return ModelRate(
        input=inp,
        output=out,
        cache_read=cache_read,
        cache_write=cache_write,
        note=note,
    )


# Reference USD/1M API list prices adapted from @bitkyc08/opencodex v2.100.6
# (MIT License, src/usage/expected-prices.ts).
PRESET_MODEL_CATALOG: tuple[PresetModelEntry, ...] = (
    # Google / Antigravity
    PresetModelEntry("gemini-4-argon", "antigravity", _rate(2.0, 10.0, 0.1, 0.0, "OpenCodex ref (2026-11)")),
    PresetModelEntry("gemini-3.8-flash", "antigravity", _rate(0.75, 3.75, 0.075, 0.0, "OpenCodex ref")),
    PresetModelEntry("gemini-3.7-flash", "antigravity", _rate(0.75, 3.75, 0.075, 0.0, "OpenCodex ref")),
    PresetModelEntry("gemini-3.6-flash", "antigravity", _rate(1.5, 7.5, 0.15, 0.0, "OpenCodex ref")),
    PresetModelEntry("gemini-3.1-pro", "antigravity", _rate(2.0, 12.0, 0.2, 0.0, "OpenCodex ref")),
    PresetModelEntry("gemini-pro-agent", "antigravity", _rate(2.0, 12.0, 0.2, 0.0, "OpenCodex ref")),
    PresetModelEntry("gemini-3-flash-agent", "antigravity", _rate(1.5, 7.5, 0.15, 0.0, "OpenCodex ref")),
    # OpenAI / Codex
    PresetModelEntry("gpt-6.2-sol", "codex", _rate(2.0, 10.0, 0.1, 2.5, "OpenCodex ref")),
    PresetModelEntry("gpt-6.2-astra", "codex", _rate(8.0, 40.0, 0.4, 10.0, "OpenCodex ref")),
    PresetModelEntry("gpt-6.1-sol", "codex", _rate(2.0, 10.0, 0.1, 2.5, "OpenCodex ref")),
    PresetModelEntry("gpt-6.1-terra", "codex", _rate(1.0, 5.0, 0.05, 1.25, "OpenCodex ref")),
    PresetModelEntry("gpt-6-astra", "codex", _rate(10.0, 50.0, 1.0, 12.5, "OpenCodex ref")),
    PresetModelEntry("gpt-6-sol", "codex", _rate(2.0, 10.0, 0.2, 2.5, "OpenCodex ref")),
    PresetModelEntry("gpt-6-luna", "codex", _rate(0.1, 0.5, 0.01, 0.125, "OpenCodex ref")),
    PresetModelEntry("gpt-5.6-sol", "codex", _rate(4.0, 20.0, 0.4, 5.0, "OpenCodex ref")),
    PresetModelEntry("gpt-5.6-terra", "codex", _rate(2.0, 12.0, 0.2, 2.5, "OpenCodex ref")),
    PresetModelEntry("gpt-5.6-luna", "codex", _rate(0.2, 1.2, 0.02, 0.25, "OpenCodex ref")),
    PresetModelEntry("daybreak-red-latest", "codex", _rate(12.5, 75.0, 1.25, 15.625, "OpenCodex ref")),
    PresetModelEntry("daybreak-blue-latest", "codex", _rate(4.0, 20.0, 0.4, 5.0, "OpenCodex ref")),
    # Anthropic / Claude
    PresetModelEntry("claude-opus-5-5", "claude", _rate(4.0, 20.0, 0.2, 5.0, "OpenCodex ref")),
    PresetModelEntry("claude-sonnet-5-5", "claude", _rate(2.0, 10.0, 0.1, 2.5, "OpenCodex ref")),
    PresetModelEntry("claude-haiku-5-5", "claude", _rate(0.1, 0.5, 0.01, 0.125, "OpenCodex ref")),
    PresetModelEntry("claude-fable-5-2", "claude", _rate(8.0, 40.0, 0.2, 10.0, "OpenCodex ref")),
    PresetModelEntry("claude-fable-5-1", "claude", _rate(10.0, 50.0, 0.25, 12.5, "OpenCodex ref")),
    PresetModelEntry("claude-opus-4-6", "claude", _rate(5.0, 25.0, 0.5, 6.25, "OpenCodex ref")),
    PresetModelEntry("claude-sonnet-4-6", "claude", _rate(3.0, 15.0, 0.3, 3.75, "OpenCodex ref")),
    # Z.AI / ZCode (GLM)
    PresetModelEntry("glm-5.3", "zcode", _rate(1.4, 4.4, 0.26, 0.0, "OpenCodex ref (Z.AI)")),
    PresetModelEntry("glm-5.3-flash", "zcode", _rate(0.15, 0.5, 0.03, 0.0, "OpenCodex ref (Z.AI)")),
    PresetModelEntry("glm-5.2", "zcode", _rate(1.4, 4.4, 0.26, 0.0, "OpenCodex ref (Z.AI)")),
    PresetModelEntry("glm-5.1", "zcode", _rate(1.4, 4.4, 0.26, 0.0, "OpenCodex ref (Z.AI)")),
    PresetModelEntry("glm-5", "zcode", _rate(1.0, 3.2, 0.2, 0.0, "OpenCodex ref (Z.AI)")),
    PresetModelEntry("glm-4.7", "zcode", _rate(0.6, 2.2, 0.11, 0.0, "OpenCodex ref (Z.AI)")),
    PresetModelEntry("glm-4.6", "zcode", _rate(0.6, 2.2, 0.11, 0.0, "OpenCodex ref (Z.AI)")),
    PresetModelEntry("glm-4.6v", "zcode", _rate(0.3, 0.9, 0.05, 0.0, "OpenCodex ref (Z.AI)")),
    # Moonshot / Kimi
    PresetModelEntry("k3.1-pro", "kimi", _rate(2.0, 10.0, 0.2, 2.0, "OpenCodex ref")),
    PresetModelEntry("k3.1-pro-highspeed", "kimi", _rate(4.0, 20.0, 0.2, 4.0, "OpenCodex ref")),
    PresetModelEntry("k3", "kimi", _rate(3.0, 15.0, 0.3, 3.0, "OpenCodex ref")),
    PresetModelEntry("kimi-k3.1-pro", "kimi", _rate(2.0, 10.0, 0.2, 2.0, "OpenCodex ref")),
    PresetModelEntry("kimi-k3", "kimi", _rate(3.0, 15.0, 0.3, 3.0, "OpenCodex ref")),
    PresetModelEntry("kimi-k2.7-code", "kimi", _rate(0.95, 4.0, 0.19, 0.95, "OpenCodex ref")),
    PresetModelEntry("kimi-k2.7-code-highspeed", "kimi", _rate(1.9, 8.0, 0.38, 1.9, "OpenCodex ref")),
    PresetModelEntry("kimi-k2.6", "kimi", _rate(0.95, 4.0, 0.16, 0.95, "OpenCodex ref")),
    PresetModelEntry("kimi-k2.5", "kimi", _rate(0.6, 3.0, 0.1, 0.6, "OpenCodex ref")),
    # xAI / Grok
    PresetModelEntry("grok-4.7", "grok", _rate(2.0, 6.0, 0.5, 0.0, "OpenCodex ref")),
    PresetModelEntry("grok-4.6", "grok", _rate(2.0, 6.0, 0.5, 0.0, "OpenCodex ref")),
    # DeepSeek / DSH
    PresetModelEntry("deepseek-v4.1-flash", "dsh", _rate(0.3, 1.2, 0.006, 0.0, "OpenCodex ref")),
    PresetModelEntry("deepseek-chat", "dsh", _rate(0.27, 1.1, 0.07, 0.0, "OpenCodex ref")),
    PresetModelEntry("deepseek-reasoner", "dsh", _rate(0.55, 2.19, 0.14, 0.0, "OpenCodex ref")),
    # MiniMax
    PresetModelEntry("minimax-m2.1-highspeed", "minimax", _rate(0.6, 2.4, 0.03, 0.375, "OpenCodex ref")),
    # Devin
    PresetModelEntry("swe-2", "devin", _rate(3.0, 15.0, 0.3, 0.0, "OpenCodex ref")),
    PresetModelEntry("swe-1-7", "devin", _rate(0.5, 2.5, 0.2, 0.0, "OpenCodex ref")),
    PresetModelEntry("swe-1-7-lightning", "devin", _rate(2.5, 12.5, 1.0, 0.0, "OpenCodex ref")),
    # OpenCode / Cursor
    PresetModelEntry("qwen3.8-max", "opencode", _rate(2.0, 6.0, 0.0, 0.0, "OpenCodex ref")),
    PresetModelEntry("qwen3.8-flash", "opencode", _rate(0.16, 0.47, 0.0, 0.0, "OpenCodex ref")),
    PresetModelEntry("cursor::auto", "cursor", _rate(1.25, 6.0, 0.25, 1.25, "OpenCodex ref")),
)

_PRESET_BY_KEY: dict[str, PresetModelEntry] = {entry.key.lower(): entry for entry in PRESET_MODEL_CATALOG}


def default_harness_discounts() -> list[HarnessDiscountRule]:
    """Default preset harness discount rules (editable/toggleable by user)."""
    return [
        HarnessDiscountRule(
            id="preset-zcode-glm-150",
            agent="zcode",
            model_pattern="glm*",
            mode="quota_boost",
            quota_boost_pct=150.0,
            enabled=True,
            label="ZCode GLM 150% 额度加成",
            preset=True,
        ),
    ]


def default_pricing_config() -> PricingConfig:
    return PricingConfig(
        schema_version=1,
        currency="USD",
        models={},
        harness_discounts=default_harness_discounts(),
    )


def bare_model_name(model: str) -> str:
    """Strip provider prefix (``provider/`` or ``provider\\``) and trailing variant/context suffix."""
    text = model.strip()
    if "\\" in text:
        text = text.rsplit("\\", 1)[-1]
    elif "/" in text:
        text = text.rsplit("/", 1)[-1]
    text = _VARIANT_SUFFIX_RE.sub("", text).strip()
    return text


def strip_effort_suffix(model: str) -> str:
    """Strip a known trailing effort/tier suffix once (e.g. ``gemini-4-argon-high`` -> ``gemini-4-argon``)."""
    return _EFFORT_SUFFIX_RE.sub("", model.strip())


@dataclass(frozen=True)
class PriceMatch:
    matched_key: str
    rate: ModelRate
    source: Literal["user", "user_derived", "preset", "preset_derived"]


def resolve_model_rate(
    agent: str,
    model: str | None,
    config: PricingConfig,
) -> PriceMatch | None:
    """Resolve USD/1M token rates for ``(agent, model)``.

    Precedence:
    1. User manual rate for ``agent::model`` (exact, case-insensitive)
    2. User manual rate for ``model`` (exact, case-insensitive)
    3. User manual rate for bare / effort-stripped model id (marked ``user_derived``)
    4. Built-in OpenCodex reference preset for ``agent::model`` / ``model``
    5. Built-in preset for bare / effort-stripped model id (marked ``preset_derived``)
    """
    raw_model = (model or "").strip()
    if not raw_model or raw_model == DEFAULT_MODEL_BUCKET:
        # Still allow explicit user pricing on `<default>` or `agent::<default>`.
        raw_model = DEFAULT_MODEL_BUCKET

    agent_l = agent.strip().lower()
    model_l = raw_model.lower()
    bare_l = bare_model_name(raw_model).lower()
    base_l = strip_effort_suffix(bare_l).lower()

    user_index: dict[str, tuple[str, ModelRate]] = {k.strip().lower(): (k, v) for k, v in config.models.items()}

    # 1-2. Exact user overrides
    for candidate in (f"{agent_l}::{model_l}", model_l):
        hit = user_index.get(candidate)
        if hit is not None:
            return PriceMatch(matched_key=hit[0], rate=hit[1], source="user")

    if raw_model == DEFAULT_MODEL_BUCKET:
        return None

    # 3. Derived user overrides (bare provider-stripped, then effort-stripped)
    for candidate in (
        f"{agent_l}::{bare_l}",
        bare_l,
        f"{agent_l}::{base_l}",
        base_l,
    ):
        if not candidate or candidate == model_l or candidate == f"{agent_l}::{model_l}":
            continue
        hit = user_index.get(candidate)
        if hit is not None:
            return PriceMatch(matched_key=hit[0], rate=hit[1], source="user_derived")

    # 4. Exact built-in preset
    for candidate in (f"{agent_l}::{model_l}", model_l):
        preset = _PRESET_BY_KEY.get(candidate)
        if preset is not None:
            return PriceMatch(matched_key=preset.key, rate=preset.rate, source="preset")

    # 5. Derived built-in preset (bare, then effort-stripped; also dot/dash normalization e.g. gpt-6-1-sol)
    alt_dot = re.sub(r"(gpt|claude|glm|swe)-(\d+)-(\d+)", r"\1-\2.\3", base_l)
    for candidate in (
        f"{agent_l}::{bare_l}",
        bare_l,
        f"{agent_l}::{base_l}",
        base_l,
        alt_dot,
    ):
        if not candidate:
            continue
        preset = _PRESET_BY_KEY.get(candidate)
        if preset is not None:
            return PriceMatch(matched_key=preset.key, rate=preset.rate, source="preset_derived")

    return None


def resolve_harness_discount(
    agent: str,
    model: str | None,
    config: PricingConfig,
) -> HarnessDiscountRule | None:
    """Return the highest-specificity enabled discount rule matching ``(agent, model)``."""
    agent_l = agent.strip().lower()
    raw_model = (model or "").strip().lower()
    bare_l = bare_model_name(raw_model).lower() if raw_model else ""

    best: tuple[int, int, int, int, HarnessDiscountRule] | None = None
    for idx, rule in enumerate(config.harness_discounts):
        if not rule.enabled:
            continue
        rule_agent = rule.agent.strip().lower()
        if rule_agent != "*" and rule_agent != agent_l:
            continue
        pat = rule.model_pattern.strip().lower() or "*"
        if pat != "*":
            if not raw_model:
                continue
            if not (fnmatch.fnmatch(raw_model, pat) or (bare_l and fnmatch.fnmatch(bare_l, pat))):
                continue
        agent_spec = 2 if rule_agent == agent_l else 1
        pat_spec = 0 if pat == "*" else (3 if "*" not in pat and "?" not in pat else 2)
        score = (agent_spec, pat_spec, len(pat), -idx)
        if best is None or score > best[:4]:
            best = (*score, rule)
    return best[4] if best is not None else None
