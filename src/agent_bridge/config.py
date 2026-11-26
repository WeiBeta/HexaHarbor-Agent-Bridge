from __future__ import annotations

import json
import os
import re
import tomllib
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, model_validator

from agent_bridge.paths import bridge_home, bundled_agents_toml
from agent_bridge.persist import atomic_write_text

DEFAULT_INHERIT_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "GROK_WEB_FETCH_PROXY",
    "AGENT_BRIDGE_HTTP_PROXY",
    "AGENT_BRIDGE_HTTPS_PROXY",
    "AGENT_BRIDGE_NO_PROXY",
    "DSH_HOME",
    "KIMI_CODE_HOME",
    "KIMI_SHELL_PATH",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "KIMI_CODE_BASE_URL",
    "KIMI_CODE_OAUTH_HOST",
    "KIMI_OAUTH_HOST",
    "KIMI_MODEL_BASE_URL",
    "KIMI_MODEL_NAME",
    "KIMI_MODEL_API_KEY",
    "KIMI_MODEL_PROVIDER_TYPE",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENCODE_API_KEY",
    "XAI_API_KEY",
    "GROK_API_KEY",
    "GROK_HOME",
    "GROK_AUTH_PATH",
    "GROK_CLI_CHAT_PROXY_BASE_URL",
    "GROK_XAI_API_BASE_URL",
    "GROK_MODELS_BASE_URL",
    "GROK_MODELS_LIST_URL",
    "GROK_OIDC_ISSUER",
    "GROK_OIDC_CLIENT_ID",
    "GROK_OAUTH2_ISSUER",
    "GROK_OAUTH2_CLIENT_ID",
    "GROK_LOCAL_AUTH",
    "GROK_AUTH_PROVIDER_COMMAND",
    "GROK_MANAGED_CONFIG_URL",
    "GROK_CODE_XAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CODEX_API_KEY",
    "CODEX_CLI_PATH",
    "CODEX_HOME",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    "OPENROUTER_API_KEY",
    "WINDSURF_API_KEY",
    "ZCODE_HOME",
    "ZCODE_BIN",
    "ZCODE_NODE",
    "ZCODE_MODEL",
    "ZCODE_PROVIDER",
    "ZCODE_BASE_URL",
    "ZCODE_ACP_MODE",
    "ZCODE_ACP_DEBUG",
    "MINIMAX_DATA_DIR",
    "MAVIS_DATA_DIR",
    "MAVIS_REGION",
    "MAVIS_BUILD_ENV",
    "MAVIS_PROFILE",
    "MCODE_CONFIG_DIR",
    "MCODE_API_BASE_URL",
    "MCODE_AUTH_BASE_URL",
    "MCODE_REGION",
    "MCODE_CLIENT_ID",
    "MCODE_SCOPE",
    "MCODE_AUTH_PROVIDER",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "LANG",
    "LC_ALL",
)


_SLOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UTC_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_FIXED_IANA_OFFSETS: dict[str, int] = {
    "etc/utc": 0,
    "etc/gmt": 0,
    "asia/shanghai": 8 * 60,
    "asia/chongqing": 8 * 60,
    "asia/harbin": 8 * 60,
    "asia/urumqi": 8 * 60,
    "asia/hong_kong": 8 * 60,
    "asia/macau": 8 * 60,
    "asia/taipei": 8 * 60,
    "asia/singapore": 8 * 60,
    "asia/kuala_lumpur": 8 * 60,
    "asia/manila": 8 * 60,
    "asia/tokyo": 9 * 60,
    "asia/seoul": 9 * 60,
    "asia/bangkok": 7 * 60,
    "asia/ho_chi_minh": 7 * 60,
    "asia/jakarta": 7 * 60,
    "asia/kolkata": 5 * 60 + 30,
    "asia/dubai": 4 * 60,
}

AGY_CLEANER_MODES = ("cli_run_command", "disabled")
DEFAULT_AGY_CLEANER_MODEL = "gemini-3.8-flash-low"


def resolve_rotation_timezone(tz_name: str | None) -> tzinfo:
    """Resolve a rotation timezone string without requiring ``tzdata`` on Windows.

    Accepts ``"local"`` (or empty/None), ``"UTC"``/``"Z"``, fixed UTC offsets
    such as ``"+08:00"`` / ``"UTC+8"``, and IANA zone names (via ``ZoneInfo``,
    with fixed-offset fallback for common non-DST Asian/UTC zones when Windows
    lacks ``tzdata``).
    """
    raw = (tz_name or "").strip()
    if not raw or raw.lower() == "local":
        return datetime.now().astimezone().tzinfo or UTC
    upper = raw.upper()
    if upper in {"UTC", "Z", "GMT"}:
        return UTC
    match = _UTC_OFFSET_RE.match(raw)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        hours = int(match.group(2))
        minutes = int(match.group(3) or "0")
        if hours > 23 or minutes > 59:
            raise ValueError(f"invalid UTC offset {tz_name!r}: hours must be 0..23 and minutes 0..59")
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        fallback_minutes = _FIXED_IANA_OFFSETS.get(raw.lower())
        if fallback_minutes is not None:
            return timezone(timedelta(minutes=fallback_minutes))
        raise ValueError(
            f"unknown or unavailable rotation_tz {tz_name!r}; "
            "install tzdata or use 'local', 'UTC', or a fixed offset like '+08:00'"
        ) from None


class AgySlotSpec(BaseModel):
    """One pre-created 0-folder Antigravity empty project slot."""

    name: str
    project_id: str

    @model_validator(mode="after")
    def _validate_spec(self) -> AgySlotSpec:
        self.name = self.name.strip()
        self.project_id = self.project_id.strip()
        if not self.name or not _SLOT_NAME_RE.match(self.name):
            raise ValueError(
                f"invalid agy slot name {self.name!r}; "
                "use 1..64 ASCII letters, digits, '.', '_' or '-'"
            )
        if not self.project_id or any(ch.isspace() or ord(ch) < 32 for ch in self.project_id):
            raise ValueError("invalid agy slot project_id")
        return self

    def with_canonical_id(self) -> AgySlotSpec:
        try:
            canonical = str(UUID(self.project_id))
        except ValueError:
            raise ValueError("invalid agy slot project_id; expected a UUID") from None
        return self.model_copy(update={"project_id": canonical})


class AgySlotConfig(BaseModel):
    """Opt-in daily slot rotation and safe ConversationService cleanup for AGY.

    Disabled by default so unconfigured installations behave identically to v0.1.4.
    """

    enabled: bool = False
    slots: list[AgySlotSpec] = Field(default_factory=list)
    rotation_tz: str = "local"
    grace_hours: int = Field(default=24, ge=0)
    cleaner_mode: str = "cli_run_command"
    cleaner_model: str = DEFAULT_AGY_CLEANER_MODEL
    cleaner_timeout_sec: int = Field(default=120, ge=10)
    cleaner_max_retries: int = Field(default=2, ge=0, le=5)

    @model_validator(mode="after")
    def _validate_slots(self) -> AgySlotConfig:
        self.rotation_tz = (self.rotation_tz or "local").strip() or "local"
        resolve_rotation_timezone(self.rotation_tz)
        mode = (self.cleaner_mode or "").strip().lower()
        if mode not in AGY_CLEANER_MODES:
            raise ValueError(
                f"unknown agy_slots.cleaner_mode {self.cleaner_mode!r}; "
                f"use one of {', '.join(AGY_CLEANER_MODES)}"
            )
        self.cleaner_mode = mode
        self.cleaner_model = (self.cleaner_model or DEFAULT_AGY_CLEANER_MODEL).strip() or DEFAULT_AGY_CLEANER_MODEL
        if self.enabled:
            # Disabled overlays keep their historical validation and never route
            # through these values. Normalize identities before enabling any use.
            self.slots = [slot.with_canonical_id() for slot in self.slots]
        seen_names: set[str] = set()
        seen_projects: set[str] = set()
        for slot in self.slots:
            if slot.name in seen_names:
                raise ValueError(f"duplicate agy slot name {slot.name!r}")
            if slot.project_id in seen_projects:
                raise ValueError(f"duplicate agy slot project_id {slot.project_id!r}")
            seen_names.add(slot.name)
            seen_projects.add(slot.project_id)
        if self.enabled and len(self.slots) < 2:
            raise ValueError(
                "agy_slots.enabled=true requires at least 2 configured slots "
                "(recommended: 3 slots for today / yesterday / releasable)"
            )
        return self


class AgentConfig(BaseModel):
    name: str
    protocol: str
    command: list[str]
    fallback_commands: list[list[str]] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    session_meta: dict[str, Any] = Field(default_factory=dict)
    revivable: bool = False
    idle_unload_sec: int = 0
    stall_timeout_sec: int = Field(default=1800, ge=0)
    # Hard cap on one turn's wall-clock length (0 = disabled). Catches a
    # worker that stays chatty forever without ever going silent —
    # stall_timeout_sec alone cannot see that case (H-16).
    turn_timeout_sec: int = Field(default=0, ge=0)
    print_timeout: str = "120m"
    # How Bridge answers worker permission prompts when no human is present:
    # "allow_once" grants each request for that call only (default, least
    # privilege), "allow_always" persists the grant inside the worker,
    # "deny" cancels every request.
    permission_policy: str = "allow_once"
    # Codex only: each item is passed as `codex exec -c <item>` (raw TOML
    # key=value). No key validation, no filtering, user config.toml is
    # never read.
    config_overrides: list[str] = Field(default_factory=list)
    # Antigravity CLI only: execution mode (--mode accept-edits|auto|plan),
    # optional terminal sandbox (--sandbox), explicit opt-in for
    # --dangerously-skip-permissions (disabled by default), and opt-in daily
    # empty-project slot rotation.
    agy_mode: str | None = "auto"
    agy_sandbox: bool = False
    skip_permissions: bool = False
    agy_slots: AgySlotConfig = Field(default_factory=AgySlotConfig)


class EnvConfig(BaseModel):
    """How Bridge rebuilds the environment Codex (or another MCP host) stripped."""

    inherit: list[str] = Field(default_factory=lambda: list(DEFAULT_INHERIT_KEYS))
    discover_proxy: bool = True
    set: dict[str, str] = Field(default_factory=dict)
    proxy_url: str | None = None
    no_proxy: str | None = None
    # Glob patterns of env keys a worker subprocess must never see; keys
    # explicitly assigned via ``set``/agent ``env`` always survive.
    deny: list[str] = Field(default_factory=list)


class ServerConfig(BaseModel):
    """Process-level server behavior (idle exit, skill install, agent filter)."""

    idle_exit_sec: int = 0
    auto_install_skill: bool = False
    enabled_agents: list[str] = Field(default_factory=list)


class QuotaConfig(BaseModel):
    """Remaining-quota lookup that rides along with ``list_agents``.

    ``timeout_sec`` bounds one worker's lookup; ``cache_sec`` is how long a
    reading is reused before the CLI is asked again. ``experimental`` unlocks
    the workers whose only quota source is the private endpoint their own
    ``/usage`` command calls (Grok Build, Claude Code).
    """

    enabled: bool = True
    timeout_sec: float = Field(default=4.0, gt=0)
    cache_sec: float = Field(default=300.0, ge=0)
    experimental: bool = False


COORDINATOR_MODES = ("manual", "auto", "eager")
# The notes that inspired this used safe/yolo; yolo already means
# "auto-approve tool calls" for Kimi and Grok, so the canonical names differ.
_MODE_ALIASES = {"safe": "manual", "yolo": "eager"}

# Sent back on every list_agents call so the coordinator re-reads the policy
# at the moment it matters, instead of relying on rules it saw turns ago.
COORDINATOR_MODE_HINTS = {
    "manual": (
        "dispatch only when the user explicitly asked for a worker; "
        "dispatch_task is blocked unless user_requested=true"
    ),
    "auto": "dispatch at your own judgment; weigh dispatch overhead against doing it yourself",
    "eager": (
        "prefer dispatching workers over doing multi-step work yourself; "
        "you keep architecture decisions and acceptance"
    ),
}


def normalize_coordinator_mode(raw: str | None, *, strict: bool = False) -> str:
    text = (raw or "").strip().lower()
    text = _MODE_ALIASES.get(text, text)
    if text in COORDINATOR_MODES:
        return text
    if strict:
        raise ValueError(
            f"unknown coordinator mode {raw!r}; use one of "
            f"{', '.join(COORDINATOR_MODES)} (aliases: safe, yolo)"
        )
    return "auto"


class CoordinatorConfig(BaseModel):
    """How eagerly the coordinator should dispatch, plus user routing preferences.

    Bridge does not interpret ``instructions``; it relays the text through
    ``list_agents`` for the coordinator LLM to read. ``mode`` is the only key
    Bridge acts on itself: ``manual`` hard-blocks dispatch_task without
    ``user_requested=true``.
    """

    mode: str = "auto"
    instructions: str = ""


class AppConfig(BaseModel):
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    env: EnvConfig = Field(default_factory=EnvConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    coordinator: CoordinatorConfig = Field(default_factory=CoordinatorConfig)
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    warnings: list[str] = Field(default_factory=list)

    def get(self, name: str) -> AgentConfig:
        if name not in self.agents:
            known = ", ".join(sorted(self.agents)) or "(none)"
            raise KeyError(f"unknown agent {name!r}; known: {known}")
        return self.agents[name]


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _raw_agents(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    block = raw.get("agents") or {}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in block.items():
        if isinstance(spec, dict):
            out[name] = dict(spec)
    return out


def _merge_agent_spec(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if value in (None, [], {}):
            continue
        if key in {"env", "agy_slots"} and isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def _coerce_env(raw: dict[str, Any]) -> dict[str, Any]:
    block = raw.get("env")
    if not isinstance(block, dict):
        return {}
    raw_proxy = block.get("proxy")
    proxy: dict[str, Any] = raw_proxy if isinstance(raw_proxy, dict) else {}
    out: dict[str, Any] = {}
    if "inherit" in block and block["inherit"] is not None:
        raw_inherit = block["inherit"]
        # A bare string is one name, not an iterable of characters.
        out["inherit"] = [raw_inherit] if isinstance(raw_inherit, str) else [str(item) for item in raw_inherit]
    if "discover_proxy" in block:
        out["discover_proxy"] = bool(block["discover_proxy"])
    if isinstance(block.get("set"), dict):
        out["set"] = {str(key): str(value) for key, value in block["set"].items() if value is not None}
    url = proxy.get("url") or block.get("proxy_url")
    if url:
        out["proxy_url"] = str(url).strip()
    no_proxy = proxy.get("no_proxy") or proxy.get("no") or block.get("no_proxy")
    if no_proxy:
        out["no_proxy"] = str(no_proxy).strip()
    if "deny" in block and block["deny"] is not None:
        raw_deny = block["deny"]
        # A bare string is one glob, not an iterable of characters.
        out["deny"] = [raw_deny] if isinstance(raw_deny, str) else [str(item) for item in raw_deny]
    return out


def _merge_env(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key == "set" and isinstance(value, dict):
            raw_set = out.get("set")
            current: dict[str, Any] = raw_set if isinstance(raw_set, dict) else {}
            out["set"] = {**current, **value}
        elif key == "deny" and isinstance(value, list):
            raw_deny = out.get("deny")
            deny_list: list[Any] = raw_deny if isinstance(raw_deny, list) else []
            out["deny"] = [*deny_list, *value]
        else:
            out[key] = value
    return out


def _coerce_server(raw: dict[str, Any]) -> dict[str, Any]:
    block = raw.get("server")
    if not isinstance(block, dict):
        return {}
    out: dict[str, Any] = {}
    if "idle_exit_sec" in block and block["idle_exit_sec"] is not None:
        out["idle_exit_sec"] = int(block["idle_exit_sec"])
    if "auto_install_skill" in block and block["auto_install_skill"] is not None:
        out["auto_install_skill"] = bool(block["auto_install_skill"])
    if "enabled_agents" in block and block["enabled_agents"] is not None:
        raw_enabled = block["enabled_agents"]
        out["enabled_agents"] = (
            [raw_enabled]
            if isinstance(raw_enabled, str)
            else [str(item) for item in raw_enabled if str(item).strip()]
        )
    return out


def _coerce_quota(raw: dict[str, Any]) -> dict[str, Any]:
    block = raw.get("quota")
    if not isinstance(block, dict):
        return {}
    out: dict[str, Any] = {}
    if block.get("enabled") is not None:
        out["enabled"] = bool(block["enabled"])
    if block.get("timeout_sec") is not None:
        out["timeout_sec"] = float(block["timeout_sec"])
    if block.get("cache_sec") is not None:
        out["cache_sec"] = float(block["cache_sec"])
    if block.get("experimental") is not None:
        out["experimental"] = bool(block["experimental"])
    return out


def _coerce_coordinator(raw: dict[str, Any]) -> dict[str, Any]:
    block = raw.get("coordinator")
    if not isinstance(block, dict):
        return {}
    out: dict[str, Any] = {}
    if block.get("mode") is not None:
        out["mode"] = str(block["mode"])
    if block.get("instructions") is not None:
        out["instructions"] = str(block["instructions"]).strip()
    return out


def _toml_string(value: str) -> str:
    if not value:
        return '""'
    if "\n" not in value and '"' not in value and "\\" not in value:
        return f'"{value}"'
    # Literal multiline: no escape processing, so Windows paths survive.
    if "'''" not in value and not value.endswith("'"):
        return f"'''\n{value}\n'''"
    return json.dumps(value, ensure_ascii=False)


_COORDINATOR_HEADER = re.compile(r"(?m)^\[coordinator\][ \t]*(#[^\n]*)?\n?")
_NEXT_TABLE = re.compile(r"(?m)^\[")


def _trim_trailing_blank_and_comment_lines(text: str, start: int, end: int) -> int:
    lines = text[start:end].splitlines(keepends=True)
    while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("#")):
        lines.pop()
    return start + sum(len(line) for line in lines)


def _coordinator_span(text: str) -> tuple[int, int] | None:
    """Return the [coordinator] table span, skipping `[` lines inside strings."""
    header = _COORDINATOR_HEADER.search(text)
    if header is None:
        return None
    start = header.start()
    header_end = header.end()
    ends = [header_end + match.start() for match in _NEXT_TABLE.finditer(text[header_end:])]
    ends.append(len(text))
    for end in ends:
        block = text[start:end]
        rest = text[:start] + text[end:]
        try:
            parsed_block = tomllib.loads(block)
            parsed_rest = tomllib.loads(rest) if rest.strip() else {}
        except tomllib.TOMLDecodeError:
            continue
        if set(parsed_block) == {"coordinator"} and "coordinator" not in parsed_rest:
            trimmed = _trim_trailing_blank_and_comment_lines(text, start, end)
            try:
                parsed_trimmed = tomllib.loads(text[start:trimmed])
            except tomllib.TOMLDecodeError:
                return (start, end)
            if set(parsed_trimmed) == {"coordinator"}:
                return (start, trimmed)
            return (start, end)
    raise ValueError("could not isolate the [coordinator] table")


def write_coordinator_overlay(
    home: Path,
    *,
    mode: str | None = None,
    instructions: str | None = None,
) -> Path:
    """Rewrite only the [coordinator] section of the user overlay, keeping the rest.

    Values not passed here keep whatever the overlay already pins (or stay
    unset so repo defaults keep flowing). Raises on a malformed overlay instead
    of clobbering a file the user hand-edited.
    """
    path = home / "agents.toml"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    try:
        original = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML ({exc}); fix it by hand first") from exc

    existing = original.get("coordinator", {}) if isinstance(original.get("coordinator"), dict) else {}
    new_mode = mode if mode is not None else existing.get("mode")
    new_instructions = instructions if instructions is not None else existing.get("instructions")

    lines = ["[coordinator]"]
    written_mode: str | None = None
    if new_mode is not None:
        written_mode = normalize_coordinator_mode(str(new_mode), strict=True)
        lines.append(f'mode = "{written_mode}"')
    if new_instructions is not None:
        lines.append(f"instructions = {_toml_string(str(new_instructions))}")
    block = "\n".join(lines) + "\n"

    try:
        span = _coordinator_span(text)
    except ValueError as exc:
        raise ValueError(f"could not isolate the [coordinator] table in {path}") from exc
    if span:
        start, end = span
        new_text = text[:start] + block + text[end:]
    else:
        new_text = block if not text.strip() else text.rstrip() + "\n\n" + block

    try:
        parsed = tomllib.loads(new_text)
        coord = parsed.get("coordinator")
        if not isinstance(coord, dict):
            raise ValueError("missing coordinator table")
        if written_mode is not None and coord.get("mode") != written_mode:
            raise ValueError("mode mismatch")
        if new_instructions is not None:
            got = (coord.get("instructions") or "").strip()
            expected = str(new_instructions or "").strip()
            if got != expected:
                raise ValueError("instructions mismatch")
        original_other = {key: value for key, value in original.items() if key != "coordinator"}
        parsed_other = {key: value for key, value in parsed.items() if key != "coordinator"}
        if original_other != parsed_other:
            raise ValueError("other tables changed")
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise ValueError(
            "refusing to write agents.toml: rewritten [coordinator] block failed self-check"
        ) from exc

    atomic_write_text(path, new_text)
    return path


def _merge_server(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    out.update(overlay)
    return out


def load_config(home: Path | None = None) -> AppConfig:
    bundled_raw = _load_toml(bundled_agents_toml())
    user_home = home or bridge_home()
    overlay_path = user_home / "agents.toml"
    try:
        overlay_raw = _load_toml(overlay_path)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"{overlay_path} is not valid TOML: {exc}. "
            "Fix or delete the file, then restart the Bridge."
        ) from exc
    supported_sections = {"agents", "env", "server", "coordinator", "quota"}
    unsupported = sorted(set(overlay_raw) - supported_sections)
    warnings = []
    if unsupported:
        warnings.append(
            "unsupported agents.toml section(s) ignored: " + ", ".join(unsupported)
        )
    bundled = _raw_agents(bundled_raw)
    overlay = _raw_agents(overlay_raw)
    merged: dict[str, dict[str, Any]] = {name: dict(spec) for name, spec in bundled.items()}
    for name, spec in overlay.items():
        merged[name] = _merge_agent_spec(merged.get(name, {}), spec)
    agents = {
        name: AgentConfig.model_validate({**spec, "name": name})
        for name, spec in merged.items()
    }
    if os.environ.get("AGENT_BRIDGE_ENABLE_FAKE") == "1":
        agents["fake"] = AgentConfig(
            name="fake",
            protocol="fake",
            command=["fake"],
            revivable=True,
            idle_unload_sec=0,
        )
    env = EnvConfig.model_validate(_merge_env(_coerce_env(bundled_raw), _coerce_env(overlay_raw)))
    server = ServerConfig.model_validate(
        _merge_server(_coerce_server(bundled_raw), _coerce_server(overlay_raw))
    )
    coord_raw = {**_coerce_coordinator(bundled_raw), **_coerce_coordinator(overlay_raw)}
    # Per-host override: each MCP host entry can set its own mode via env
    # (e.g. Codex manual, Cursor eager) without a second config file.
    env_mode = os.environ.get("AGENT_BRIDGE_MODE")
    if env_mode:
        coord_raw["mode"] = env_mode
    coord_raw["mode"] = normalize_coordinator_mode(coord_raw.get("mode"))
    coordinator = CoordinatorConfig.model_validate(coord_raw)
    quota = QuotaConfig.model_validate({**_coerce_quota(bundled_raw), **_coerce_quota(overlay_raw)})
    return AppConfig(
        agents=agents,
        env=env,
        server=server,
        coordinator=coordinator,
        quota=quota,
        warnings=warnings,
    )
