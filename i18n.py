"""Internationalisation — reads lang/*.json at import time.

Each language file is a flat key→string map. An optional `_meta` block
describes the language for the picker:

    {
      "_meta": {"code": "uk", "name": "Українська"},
      "app.title": "Steam Card Price Watch",
      "log.checking": "Перевіряю {count} картку(и)",
      ...
    }

Lookup order in `t(key)`:
    current language → fallback (en) → key itself (so missing keys are
    visible in the UI and easy to spot during development).
"""
import json
import logging
from pathlib import Path
from typing import Any

_BASE = Path(__file__).parent
_LANG_DIR = _BASE / "lang"
_CONFIG_PATH = _BASE / "config.json"
_FALLBACK_CODE = "en"

_log = logging.getLogger("i18n")

# Loaded translations: { code: { "_meta": {...}, "key": "value", ... } }
_TRANSLATIONS: dict[str, dict[str, Any]] = {}
_CURRENT_CODE: str = _FALLBACK_CODE


def _load_all() -> None:
    """Read every lang/*.json once at import time.

    Files whose JSON is malformed are skipped with a log warning so a single
    broken file doesn't take the whole app down.
    """
    if not _LANG_DIR.is_dir():
        _log.warning("lang/ directory not found at %s — i18n disabled", _LANG_DIR)
        return
    for path in sorted(_LANG_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            _log.warning("Failed to load language file %s: %s", path, exc)
            continue
        if not isinstance(data, dict):
            _log.warning("Language file %s is not a JSON object — skipped", path)
            continue
        meta = data.get("_meta", {}) if isinstance(data.get("_meta"), dict) else {}
        code = meta.get("code") or path.stem
        _TRANSLATIONS[code] = data


_load_all()


def available_languages() -> list[dict[str, str]]:
    """List of {code, name} for every successfully loaded language.

    Sorted by display name (case-insensitive), so the dropdown order is
    stable as long as the user doesn't rename files.
    """
    items = []
    for code, data in _TRANSLATIONS.items():
        meta = data.get("_meta", {}) if isinstance(data.get("_meta"), dict) else {}
        items.append({"code": code, "name": meta.get("name", code)})
    return sorted(items, key=lambda x: x["name"].lower())


def set_language(code: str) -> bool:
    """Switch current language. Returns True if the code is known.

    If `code` isn't loaded, falls back to en (or whatever's available) and
    returns False.
    """
    global _CURRENT_CODE
    if code in _TRANSLATIONS:
        _CURRENT_CODE = code
        return True
    if _FALLBACK_CODE in _TRANSLATIONS:
        _CURRENT_CODE = _FALLBACK_CODE
    elif _TRANSLATIONS:
        _CURRENT_CODE = next(iter(_TRANSLATIONS))
    return False


def get_language() -> str:
    return _CURRENT_CODE


def t(key: str, **kwargs) -> str:
    """Translate a key, with optional `str.format`-style substitution.

    Missing keys return the key itself — that way a forgotten translation
    surfaces visibly instead of silently rendering nothing.
    """
    src = _TRANSLATIONS.get(_CURRENT_CODE, {})
    s = src.get(key)
    if s is None and _CURRENT_CODE != _FALLBACK_CODE:
        s = _TRANSLATIONS.get(_FALLBACK_CODE, {}).get(key)
    if s is None:
        return key  # surface missing keys
    if kwargs:
        try:
            return s.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return s
    return s


# ---------------------------------------------------------------------------
# Bootstrap: pick up the language from config.json at startup
# ---------------------------------------------------------------------------

def _init_from_config() -> None:
    if not _CONFIG_PATH.exists():
        return
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        lang = cfg.get("ui", {}).get("language")
        if lang:
            set_language(lang)
    except Exception as exc:
        _log.warning("Could not read language from config.json: %s", exc)


_init_from_config()
