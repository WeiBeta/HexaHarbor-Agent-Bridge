"""Dashboard & Windows tray localization helpers and shared preference storage.

Language preference is stored in ``<bridge_home>/billing/preferences.json`` —
intentionally separate from ``service.json`` so switching UI language never
affects Windows first-use defaults (``has_service_config``), autostart, tray
enablement, or background service lifecycle.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from agent_bridge.paths import billing_dir, bridge_home, dashboard_assets_dir
from agent_bridge.persist import atomic_write_json, file_lock, read_json

log = logging.getLogger(__name__)

LocaleCode = Literal["zh-CN", "en"]

DEFAULT_LOCALE: LocaleCode = "zh-CN"
SUPPORTED_LOCALES: tuple[LocaleCode, ...] = ("zh-CN", "en")
PREFERENCES_FILENAME = "preferences.json"
LOCK_TIMEOUT_SEC = 5.0

_LENIENT_LOCALE_ALIASES: dict[str, LocaleCode] = {
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "zh": "zh-CN",
    "cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh_hans": "zh-CN",
    "en": "en",
    "en-us": "en",
    "en_us": "en",
    "en-gb": "en",
    "en_gb": "en",
}

_STRICT_LOCALE_ALIASES: dict[str, LocaleCode] = {
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "en": "en",
}


class DashboardPreferences(BaseModel):
    """Persistent UI preferences shared by the web dashboard and Windows tray."""

    schema_version: int = 1
    locale: LocaleCode = DEFAULT_LOCALE


def normalize_locale(value: Any, *, strict: bool = False) -> LocaleCode:
    """Normalize a locale value to ``'zh-CN'`` or ``'en'``.

    With ``strict=False`` (reading from disk), missing or unrecognized values
    fall back to ``DEFAULT_LOCALE``. With ``strict=True`` (API mutations),
    unsupported values raise ``ValueError``.
    """
    if isinstance(value, str):
        cleaned = value.strip().lower()
        table = _STRICT_LOCALE_ALIASES if strict else _LENIENT_LOCALE_ALIASES
        matched = table.get(cleaned)
        if matched is not None:
            return matched
    if strict:
        raise ValueError("unsupported locale; expected 'zh-CN' or 'en'")
    return DEFAULT_LOCALE


def dashboard_preferences_path(home: Path | None = None) -> Path:
    return billing_dir(home) / PREFERENCES_FILENAME


def _preferences_lock(home: Path) -> Path:
    return dashboard_preferences_path(home).with_name(f"{PREFERENCES_FILENAME}.lock")


def dashboard_locales_dir() -> Path:
    return dashboard_assets_dir() / "locales"


def load_locale_catalog(locale: str) -> dict[str, Any]:
    """Load the bundled JSON translation catalog for a supported locale."""
    canonical = normalize_locale(locale, strict=True)
    path = dashboard_locales_dir() / f"{canonical}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"locale catalog {path} must be a JSON object")
    return data


def load_dashboard_preferences(home: Path | None = None) -> DashboardPreferences:
    """Load UI preferences without creating any files on disk.

    Handles missing files, partial JSON objects, extra unknown keys, corrupt
    JSON, and invalid locale values by falling back to ``DEFAULT_LOCALE``.
    """
    resolved = home or bridge_home()
    path = resolved / "billing" / PREFERENCES_FILENAME
    if not path.is_file():
        return DashboardPreferences()
    raw = read_json(path, None)
    if not isinstance(raw, dict):
        log.warning("invalid %s at %s; using defaults", PREFERENCES_FILENAME, path)
        return DashboardPreferences()
    raw_version = raw.get("schema_version")
    schema_version = raw_version if isinstance(raw_version, int) and not isinstance(raw_version, bool) and raw_version >= 1 else 1
    locale = normalize_locale(raw.get("locale"), strict=False)
    return DashboardPreferences(schema_version=schema_version, locale=locale)


def load_dashboard_locale(home: Path | None = None) -> LocaleCode:
    return load_dashboard_preferences(home).locale


def save_dashboard_preferences(
    prefs: DashboardPreferences | None = None,
    home: Path | None = None,
    *,
    locale: Any = None,
) -> DashboardPreferences:
    """Atomically save dashboard UI preferences under ``<home>/billing/preferences.json``.

    Preserves any unrelated extra keys already present in ``preferences.json``
    and never reads or modifies ``service.json``.
    """
    resolved = home or bridge_home()
    path = dashboard_preferences_path(resolved)
    with file_lock(_preferences_lock(resolved), timeout_sec=LOCK_TIMEOUT_SEC):
        existing_raw = read_json(path, None)
        merged: dict[str, Any] = dict(existing_raw) if isinstance(existing_raw, dict) else {}
        if prefs is not None:
            target_locale = normalize_locale(prefs.locale, strict=True)
            schema_version = prefs.schema_version if prefs.schema_version >= 1 else 1
        elif locale is not None:
            target_locale = normalize_locale(locale, strict=True)
            raw_ver = merged.get("schema_version")
            schema_version = raw_ver if isinstance(raw_ver, int) and not isinstance(raw_ver, bool) and raw_ver >= 1 else 1
        else:
            current = load_dashboard_preferences(resolved)
            target_locale = current.locale
            schema_version = current.schema_version

        validated = DashboardPreferences(schema_version=schema_version, locale=target_locale)
        merged["schema_version"] = validated.schema_version
        merged["locale"] = validated.locale
        atomic_write_json(path, merged)
    return validated
