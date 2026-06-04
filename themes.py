"""Custom ttkbootstrap themes loaded from themes/*.json at startup.

File format (mirrors the lang/*.json convention):

    {
      "_meta": {
        "code": "claude",         # used as the theme ID — must be unique
        "name": "Claude",         # display name (currently unused in the
                                  #   dropdown, kept for future picker UX)
        "type": "dark"            # "dark" or "light"
      },
      "colors": {
        "primary":   "#D97757",
        "secondary": "#6B7280",
        "success":   "#16A34A",
        "info":      "#D97757",
        "warning":   "#F59E0B",
        "danger":    "#DC2626",
        "light":     "#F5F5F0",
        "dark":      "#1E1E1C",
        "bg":        "#262624",
        "fg":        "#F5F5F0",
        "selectbg":  "#D97757",
        "selectfg":  "#FFFFFF",
        "border":    "#3F3F3D",
        "inputfg":   "#F5F5F0",
        "inputbg":   "#3A3A38",
        "active":    "#3A3A38"
      }
    }

Anything with a missing/malformed file is logged and skipped — one bad
theme can't take the whole app down.
"""
import json
import logging
from pathlib import Path

import ttkbootstrap as tb
from ttkbootstrap.style import ThemeDefinition

_BASE = Path(__file__).parent
_THEMES_DIR = _BASE / "themes"
_log = logging.getLogger("themes")


def _load_files() -> list[dict]:
    """Read every themes/*.json into a list of metadata dicts."""
    themes: list[dict] = []
    if not _THEMES_DIR.is_dir():
        return themes
    for path in sorted(_THEMES_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            _log.warning("Failed to load theme %s: %s", path, exc)
            continue
        if not isinstance(data, dict):
            _log.warning("Theme %s is not a JSON object — skipped", path)
            continue
        meta = data.get("_meta", {}) if isinstance(data.get("_meta"), dict) else {}
        code = meta.get("code") or path.stem
        themes.append({
            "code": code,
            "name": meta.get("name", code),
            "type": meta.get("type", "dark"),
            "colors": data.get("colors", {}),
            # Optional per-theme override for the Treeview row-selection bg.
            # The default behaviour (auto-derived from inputbg) is fine for
            # most palettes; some themes want a brand-specific accent (e.g.
            # claude.json uses a muted gold for its selection highlight).
            "row_select_bg": meta.get("row_select_bg"),
            # Per-theme accent for the active (selected) notebook tab. Empty
            # → gui.pyw falls back to the theme's `primary` colour.
            "active_tab_bg": meta.get("active_tab_bg"),
            "path": str(path),
        })
    return themes


def register_all() -> list[dict]:
    """Register every themes/*.json with ttkbootstrap's Style singleton.

    Must be called after a Style has been created (i.e. after `tb.Window`
    has run __init__). Returns the list of successfully registered themes
    so the caller can populate the picker.
    """
    style = tb.Style()  # singleton — returns existing if already created
    registered: list[dict] = []
    for theme in _load_files():
        if not theme["colors"]:
            _log.warning("Theme %s has no colors block — skipped", theme["code"])
            continue
        if theme["code"] in style.theme_names():
            # Already registered (built-in name collision or hot-reload).
            registered.append(theme)
            continue
        try:
            tdef = ThemeDefinition(
                name=theme["code"],
                themetype=theme["type"],
                colors=theme["colors"],
            )
            style.register_theme(tdef)
            registered.append(theme)
        except Exception as exc:
            _log.warning("Failed to register theme %s: %s", theme["code"], exc)
    return registered
