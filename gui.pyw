"""Steam Card Price Watch — GUI (ttkbootstrap, no console window)."""
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import ttkbootstrap as tb
from ttkbootstrap.constants import *

import i18n
import themes as custom_themes
from i18n import t

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"
WATCHLIST_PATH = BASE / "watchlist.json"
SALELIST_PATH = BASE / "salelist.json"
STATE_PATH = BASE / "state.json"
PURCHASES_PATH = BASE / "purchases.json"
# Wishlist-games tracking («Ігри» tab). Survives the bonus_content
# checkbox being switched off — the tab hides, the data stays.
GAMELIST_PATH = BASE / "gamelist.json"
# Appids the wishlist import must never surface again — entries Steam's
# GetItems can't resolve to a name (delisted / region-locked / bare
# placeholders). Adding a game manually by URL un-blacklists it.
GAMEBLACKLIST_PATH = BASE / "gameblacklist.json"
LOG_PATH = BASE / "watch.log"

# Shared logger writing to watch.log so user-initiated actions ("Оновити
# зараз", "Запустити зараз") show up in the Журнал tab alongside what
# watch.py logs from its scheduled runs. Same rotation policy.
# Note: two processes (gui.pyw + watch.py) writing to the same file is
# safe enough in this single-user setup — Python's RotatingFileHandler
# uses opportunistic locking; rare interleave is acceptable for INFO-grade
# logs and the rotation moments are far apart in normal usage.
_gui_log_handler = RotatingFileHandler(
    LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
)
_gui_log_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
log = logging.getLogger("gui")
log.addHandler(_gui_log_handler)
log.setLevel(logging.INFO)
# Don't propagate to the root logger — steam.py's module-level logger has
# its own console handler when run standalone, and we don't want double
# lines in any case.
log.propagate = False

# Sibling loggers — steam.py / telegram.py / alerts.py / browser_cookies.py
# each set up their own `logging.getLogger("<name>")` at module level
# with no handler attached. Without this, anything they log from a GUI
# context goes to /dev/null and we lose the diagnostic trail (e.g.
# wallet-info parsing warnings). Tee them into the same file handler
# so the Журнал tab and watch.log capture them too.
for _name in ("steam", "telegram", "alerts", "browser_cookies"):
    _sib = logging.getLogger(_name)
    _sib.addHandler(_gui_log_handler)
    _sib.setLevel(logging.INFO)
    _sib.propagate = False

# Map a kind ("buy" / "sell") to its on-disk store. The two share a schema
# and most of the GUI surface, but live in separate files so they can be
# managed independently.
LIST_PATHS = {"buy": WATCHLIST_PATH, "sell": SALELIST_PATH}

# Status values that mean "the user already closed the deal on this card"
# (hide from the active list). "bought" is the legacy value still present
# in older watchlist.json files; new sale-side rows use "sold".
CLOSED_STATUSES = {"bought", "sold"}


def _migrate_state_keys(state: dict) -> bool:
    """Add a 'buy:' prefix to legacy state keys that pre-date multi-list.

    Old state keys were `{appid}:{name}`; new keys are
    `{kind}:{appid}:{name}` to keep buy- and sell-side antispam separate.
    Idempotent — running twice does nothing. Returns True if anything
    actually changed (caller can decide whether to save).
    """
    changed = False
    for key in list(state.keys()):
        if key.startswith("__"):
            continue
        # "game:" must be listed — without it this migration mangled
        # every game-alert key into "buy:game:…" on each run, losing
        # the antispam entry and re-alerting endlessly (2026-06-11).
        if not key.startswith(("buy:", "sell:", "game:")):
            state["buy:" + key] = state.pop(key)
            changed = True
        elif key.startswith("buy:game:"):
            # Heal keys already mangled by the buggy version.
            state[key[len("buy:"):]] = state.pop(key)
            changed = True
    return changed


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default=None):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config() -> dict:
    cfg = load_json(CONFIG_PATH)
    if cfg is None:
        example = BASE / "config.example.json"
        if example.exists():
            shutil.copy(example, CONFIG_PATH)
            cfg = load_json(CONFIG_PATH)
        else:
            cfg = {}
    return cfg


# ---------------------------------------------------------------------------
# Colour helpers (used for alternating Treeview rows)
# ---------------------------------------------------------------------------

def _hex_to_rgb(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    clamp = lambda v: max(0, min(255, v))
    return f"#{clamp(r):02x}{clamp(g):02x}{clamp(b):02x}"


def _is_dark(c: str) -> bool:
    try:
        r, g, b = _hex_to_rgb(c)
    except (ValueError, IndexError):
        return True
    # Rec. 709 luma
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 128


def _shift(c: str, delta: int) -> str:
    try:
        r, g, b = _hex_to_rgb(c)
    except (ValueError, IndexError):
        return c
    return _rgb_to_hex(r + delta, g + delta, b + delta)


def _try_parse_money(s) -> float | None:
    """Pull a float out of strings like '5,49 ₴', '$1.23', '—', or already-num.

    Returns None when the input is missing, a dash, or just unparseable.
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    text = str(s).strip()
    if not text or text in ("—", "-", "?"):
        return None
    # Strip everything except digits, comma, dot, minus.
    import re as _re
    cleaned = _re.sub(r"[^\d,.\-]", "", text)
    if not cleaned:
        return None
    # Comma-as-decimal locales: '1.234,56' or '5,49'
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App(tb.Window):
    # Treeview row tag colours — used by both Watchlist and History.
    # Picked to play with the superhero (dark) theme; if the user switches
    # to a light theme, _configure_styles re-derives them on the fly.
    _ROW_TAGS = {
        # Green/red are the "deal status" tints — applied per row based
        # on the lowest_price vs target_price comparison (direction
        # depends on kind: buy wants ≤, sell wants ≥). See the picker
        # in `_refresh_card_list`. Stayed at the same shades as the old
        # `alerted` tag so existing screenshots / muscle memory still
        # match — "alerted" itself is now meant as a state-column-only
        # signal, not a row colour.
        "good_match":   {"background": "#2d4a3b", "foreground": "#d6f5e0"},
        "bad_match":    {"background": "#5a2d2d", "foreground": "#f5d6d6"},
        # `error` is a separate concern — the card *failed* to poll
        # (HTTP failure, missing metadata). Same red as bad_match so
        # they read as "something's off" without us having to teach
        # three reds, but it's gated by status, not by price math.
        "error":        {"background": "#5a2d2d", "foreground": "#f5d6d6"},
        # Rate-limited: muted gold. Distinct from green (good_match)
        # and red (bad_match / error) — Steam just told us to back off,
        # the card isn't broken, we just couldn't poll it this round.
        "rate_limited": {"background": "#5D5119", "foreground": "#F5E9C0"},
        "even":     {},   # placeholder — filled in _configure_styles()
        "odd":      {},
        "selected": {},   # placeholder — appended last to win tag priority
    }

    def __init__(self):
        # One-off bookkeeping before the window comes up:
        #   * create salelist.json if it doesn't exist (so load_json never
        #     returns None for it),
        #   * migrate any legacy state.json keys to the kind-prefixed form.
        if not SALELIST_PATH.exists():
            save_json(SALELIST_PATH, [])
        try:
            _state = load_json(STATE_PATH) or {}
            if _migrate_state_keys(_state):
                save_json(STATE_PATH, _state)
        except Exception:
            pass

        self.config_data = load_config()
        ui_cfg = self.config_data.get("ui", {})
        theme = ui_cfg.get("theme", "superhero")
        # Apply log toggles BEFORE we start doing anything significant
        # — system_log routes uncaught exceptions to our logger, and
        # we want that hook in place before any callback can fire.
        # Tk.report_callback_exception is set on `self`, so we need
        # super().__init__ to have run first; defer that part until
        # after the bootstrap.
        self._pending_log_toggles = (
            bool(ui_cfg.get("system_log", False)),
            bool(ui_cfg.get("debug_log", False)),
        )
        # Apply UI language *before* building widgets so every label is
        # already in the right tongue. i18n bootstraps from config.json
        # automatically, but we re-apply explicitly so a config that's
        # been mutated in-process (rare) still wins.
        lang = ui_cfg.get("language")
        if lang:
            i18n.set_language(lang)
        # Bootstrap with a safe built-in theme — custom themes from themes/
        # aren't registered yet at this point. We switch to the user's
        # chosen theme below once Style exists and our custom themes are
        # loaded.
        super().__init__(title=t("app.title"), themename="superhero", size=(1100, 620))
        # Now that `self` is a real Tk root, we can hook
        # report_callback_exception. Logger levels can be done here too.
        self._apply_log_config(*self._pending_log_toggles)
        # Register every themes/*.json now that Style is alive. Returns
        # metadata used later to append them to the Settings picker.
        self._custom_themes = custom_themes.register_all()
        # Quick lookup for per-theme overrides (row selection colour etc.).
        self._custom_theme_by_code = {th["code"]: th for th in self._custom_themes}
        # Now switch to whatever the user actually wants.
        if theme != "superhero":
            try:
                if theme in self.style.theme_names():
                    self.style.theme_use(theme)
            except Exception:
                pass  # fall back to the safe initial theme
        self.resizable(True, True)
        self._configure_styles()
        self._install_clipboard_shortcuts()
        # Snapshot the default font sizes BEFORE any scaling, so the user
        # can ratchet up/down repeatedly without compounding rounding
        # errors. Then apply the saved scale (1..5) — done before
        # _build_ui so widgets are constructed at the final size instead
        # of resizing after first paint.
        self._snapshot_default_fonts()
        font_scale = int(ui_cfg.get("font_scale", 1) or 1)
        self._apply_font_scale(font_scale)
        self._build_ui()
        # ttkbootstrap reshuffles some style maps as the notebook widget
        # comes online during _build_ui; re-pin the tab-selected colour
        # so the active tab is correctly tinted from the very first paint.
        self._configure_notebook_tab_style()
        # Tint the native Windows title bar to match the theme bg. Has to
        # happen after the window is fully realised (HWND exists).
        self._apply_native_titlebar_theme()
        # Restore the previous window size + position. Applied AFTER the
        # widget tree is built so geometry isn't fighting with the initial
        # `size=(1100, 620)` arg passed to super().__init__. The string
        # format is Tk's "WxH+X+Y" — straight pass-through to self.geometry,
        # which tolerates "WxH" without coords too (Tk picks placement).
        saved_geom = ui_cfg.get("window_geometry")
        if saved_geom:
            try:
                self.geometry(saved_geom)
            except tk.TclError:
                pass
        # Persist window geometry on close — covers the "user resized to
        # taste, then quit" path. _save_settings also stamps the current
        # geometry, so a Save click while resized works too.
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_watchlist()
        self._refresh_scheduler_status()
        self._refresh_history()
        self._start_log_autoupdate()
        # Backfill display_name/game_name for any pre-existing entries that
        # were saved before the metadata fields existed. Runs once on a
        # background thread so the GUI doesn't block on Steam Store.
        threading.Thread(target=self._backfill_metadata, daemon=True).start()
        # Pull the saved Steam identity (if any) into the floating user
        # widget — persona/balance labels straight from config, avatar
        # downloaded on a background thread so we don't block the first
        # paint while Steam's CDN responds.
        self._load_steam_user_widget()

    def _on_close(self) -> None:
        """Persist window geometry then destroy.

        Loads + rewrites instead of overwriting because _save_settings is
        the canonical schema producer; we only want to nudge a single key
        without re-deriving the rest. Errors are swallowed — closing the
        app must never be blocked by a disk hiccup.
        """
        try:
            cfg = load_json(CONFIG_PATH, {}) or {}
            cfg.setdefault("ui", {})["window_geometry"] = self.geometry()
            save_json(CONFIG_PATH, cfg)
        except Exception:
            pass
        self.destroy()

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

    # Named Tk fonts that drive every standard widget — we scale ALL of
    # them together so the UI stays visually consistent (a half-scaled
    # combobox next to a full-scale label would look broken).
    _SCALABLE_FONTS = (
        "TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont",
        "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
        "TkIconFont", "TkTooltipFont",
    )
    # ttk.Treeview rowheight is tied to the row's font; we cache a base
    # value and multiply on scale. ~22 px is what ttkbootstrap settles on
    # for the default font; lookup at runtime in case the theme overrode.
    _BASE_ROW_HEIGHT = 22

    # User picks "За замовчуванням" / "x2".."x5" in Settings — each step
    # multiplies the baseline font sizes by these factors. Range topped
    # at 3.0 because anything bigger pushed buttons off-screen on a 1080p
    # monitor. Five buckets in [1.0, 3.0] gives a smooth ramp.
    _FONT_SCALE_FACTORS = {1: 1.0, 2: 1.5, 3: 2.0, 4: 2.5, 5: 3.0}

    def _font_scale_combo_values(self) -> list[str]:
        """Combobox labels for the font-scale dropdown.

        x1 is renamed to a localised «За замовчуванням» (Default) so the
        user has an obvious "go back to normal" choice — picking it after
        scaling up resets fonts to their baseline.
        """
        return [t("font_scale.default"), "x2", "x3", "x4", "x5"]

    def _font_scale_to_label(self, scale: int) -> str:
        """Map int 1..5 → the user-facing combobox label."""
        scale = max(1, min(5, int(scale or 1)))
        return t("font_scale.default") if scale == 1 else f"x{scale}"

    def _font_scale_from_label(self, label: str) -> int:
        """Parse a combobox label back to its 1..5 int scale."""
        s = (label or "").strip()
        if s == t("font_scale.default"):
            return 1
        try:
            return max(1, min(5, int(s.lstrip("xX"))))
        except ValueError:
            return 1

    def _snapshot_default_fonts(self) -> None:
        """Remember the *unscaled* size of every named font.

        Called once at startup before any scaling. _apply_font_scale then
        multiplies these baselines instead of compounding on top of the
        current size — important for ratcheting up and down through the
        Settings dropdown.
        """
        import tkinter.font as tkfont
        self._original_font_sizes = {}
        for name in self._SCALABLE_FONTS:
            try:
                f = tkfont.nametofont(name)
                # `actual('size')` gives the real point size after Tk's
                # own DPI scaling. We use absolute value because Tk uses
                # negative numbers for pixel-sized fonts.
                self._original_font_sizes[name] = abs(int(f.actual("size"))) or 9
            except tk.TclError:
                # Font not registered on this platform — skip silently.
                pass
        # Cache the theme's default rowheight too, with a sensible fallback.
        try:
            self._original_row_height = int(self.style.lookup("Treeview", "rowheight") or self._BASE_ROW_HEIGHT)
        except (tk.TclError, ValueError):
            self._original_row_height = self._BASE_ROW_HEIGHT
        if self._original_row_height <= 0:
            self._original_row_height = self._BASE_ROW_HEIGHT
        # Holds (widget, base_size, weight) tuples for labels that set
        # their font explicitly (font=("", 10, "bold") and the like).
        # Those don't follow named fonts so _apply_font_scale wouldn't
        # touch them otherwise. _scaled_font(...) registers + initial set.
        self._explicit_font_widgets = []
        self._font_scale = 1

    def _make_scalable_check(self, parent, variable: tk.BooleanVar) -> ttk.Label:
        """Build a font-scaling replacement for ttk.Checkbutton.

        ttkbootstrap renders Checkbutton's indicator as a PhotoImage baked
        at theme creation — the indicator box ignores named-font sizes,
        so at higher font scales the text grows but the box stays tiny.
        Workaround: a plain ttk.Label that flips between ☐ and ☑ glyphs
        on click. Both glyphs are real font characters so they ramp with
        the Settings font-scale knob, the same way every other label does.

        The BooleanVar stays the canonical source of truth — _save_settings
        keeps reading it untouched, and a `trace_add` keeps the glyph in
        sync if the var is mutated programmatically (e.g. on Reset).
        """
        # cursor="hand2" telegraphs "this is clickable" — without it the
        # bare glyph looks like a decorative label.
        lbl = ttk.Label(
            parent,
            text="☑" if variable.get() else "☐",
            cursor="hand2",
        )
        # Slightly larger baseline (13 vs default 9) so the glyph reads
        # well at x1; ramps to ~39pt at x5 which sits comfortably next to
        # the label text on its row.
        self._scaled_font(lbl, 13)

        def _sync(*_):
            lbl.configure(text="☑" if variable.get() else "☐")

        def _toggle(_event=None):
            variable.set(not variable.get())
            # The trace below will redraw the glyph — no need to do it
            # explicitly here.

        lbl.bind("<Button-1>", _toggle)
        # variable.trace_add fires on every .set() — covers both user clicks
        # and external resets (Скинути button).
        variable.trace_add("write", _sync)
        return lbl

    def _scaled_font(self, widget, base_size: int, weight: str = "") -> None:
        """Register a widget with an explicit font size for scaling.

        Sets the widget's `font` to its baseline size right now AND remembers
        it so future _apply_font_scale calls keep it in sync with the rest
        of the UI. Used for labels that needed a non-default font (the
        Steam username / wallet, the Scheduler status header, etc).
        """
        self._explicit_font_widgets.append((widget, base_size, weight))
        self._set_widget_font(widget, base_size, weight)

    def _set_widget_font(self, widget, base_size: int, weight: str) -> None:
        mult = self._FONT_SCALE_FACTORS.get(self._font_scale, 1.0)
        size = max(1, int(round(base_size * mult)))
        spec = ("", size, weight) if weight else ("", size)
        try:
            widget.configure(font=spec)
        except tk.TclError:
            pass

    def _apply_font_scale(self, scale: int) -> None:
        """Multiply every cached font size by the bucket factor.

        Treeview rowheight scales together with the font so rows don't
        clip the now-taller text. Explicit-font widgets registered via
        _scaled_font are recomputed too. No widgets are recreated — Tk
        redraws them automatically when their font changes.
        """
        import tkinter.font as tkfont
        scale = max(1, min(5, int(scale or 1)))
        self._font_scale = scale
        mult = self._FONT_SCALE_FACTORS[scale]
        for name, base in self._original_font_sizes.items():
            try:
                tkfont.nametofont(name).configure(size=max(1, int(round(base * mult))))
            except tk.TclError:
                pass
        for widget, base, weight in self._explicit_font_widgets:
            self._set_widget_font(widget, base, weight)
        try:
            self.style.configure("Treeview",
                                 rowheight=max(1, int(round(self._original_row_height * mult))))
        except tk.TclError:
            pass
        # The Steam user widget floats over the strip between title bar
        # and the notebook's tab row (notebook.pack pady top). At higher
        # font scales the widget grows taller and starts overlapping the
        # tabs unless we also grow that strip — scale the top pady together
        # with everything else. Guard with hasattr because _apply_font_scale
        # is also called once from __init__ BEFORE _build_ui creates the
        # notebook (so the very first paint already has the right size).
        if hasattr(self, "notebook"):
            try:
                self.notebook.pack_configure(
                    pady=(max(0, int(round(25 * mult))), 0)
                )
            except tk.TclError:
                pass
        # Force a full redraw — Entry / Spinbox widgets keep stale
        # rendering of the old font size otherwise (visible as clipped
        # text artefacts on the Settings tab). And every ScrolledFrame
        # has a Canvas inside; its scrollregion is sized off the inner
        # frame's reqsize at <Configure> time, which font changes don't
        # always trigger. Re-tag inner frames so the scrollregion catches up.
        self.update_idletasks()
        for child in self.winfo_children():
            self._refresh_scrolled_frames(child)
            self._force_redraw_inputs(child)

    def _force_redraw_inputs(self, widget) -> None:
        """Kick Entry / Spinbox / Combobox out of stale pixel caches.

        After a font *shrink* Tk's text-entry widgets keep drawing the
        characters in the bounding box computed for the previous (larger)
        font. The widgets allocate space for the new font correctly, but
        the inner text-render keeps old metrics — visible as ghost chars
        / clipped digits / "extra column" artefacts especially on the
        Settings tab. Toggling `width` by ±1 forces Tk to recompute
        geometry from scratch, which clears the cached metrics.
        """
        try:
            cls = widget.winfo_class()
        except tk.TclError:
            cls = ""
        if cls in ("TEntry", "TSpinbox", "TCombobox", "Entry", "Spinbox"):
            try:
                w = int(widget.cget("width"))
                widget.configure(width=w + 1)
                widget.configure(width=w)
            except (tk.TclError, ValueError):
                pass
        for grand in widget.winfo_children():
            self._force_redraw_inputs(grand)

    def _refresh_scrolled_frames(self, widget) -> None:
        """Walk the widget tree, kick every ScrolledFrame's geometry.

        After a font change the inner frame's reqsize shifts but the
        ScrolledFrame's internal `_measures()` only reruns on yview() or
        a real <Configure> event. We:
          1. update_idletasks so reqsize reflects the new font,
          2. call yview() — this re-snaps content_place to a valid
             offset (e.g. after font shrink it pulls content back to 0
             so we're not still scrolled past the now-shorter content),
          3. check `_measures()` and force-hide the scrollbar when the
             thumb covers the whole range — base autohide is mouse-based
             (Enter/Leave) and otherwise leaves a stale scrollbar visible
             after content shrinks.
        """
        try:
            from ttkbootstrap.widgets.scrolled import ScrolledFrame
        except ImportError:
            return
        if isinstance(widget, ScrolledFrame):
            try:
                widget.update_idletasks()
                # yview_moveto(0) — not yview() — because plain yview()
                # reads the previous `first` fraction from the scrollbar
                # and tries to preserve it. After a font shrink that
                # leaves content_place at a negative rely (content
                # scrolled OFF the top), even though the new shorter
                # content now fits in view. Snapping back to top is the
                # right "reset to baseline" behaviour for a font change.
                widget.yview_moveto(0)
                _, thumb = widget._measures()
                # Use < 0.999 not < 1.0 — floating-point rounding can
                # leave thumb = 0.9998 when content just barely fits.
                if thumb >= 0.999:
                    widget.hide_scrollbars()
            except (tk.TclError, AttributeError, ZeroDivisionError):
                pass
        for grand in widget.winfo_children():
            self._refresh_scrolled_frames(grand)

    def _configure_styles(self):
        """Tune ttk.Style — rowheight, alternating row colours, header borders.

        Called once at startup and again after a theme change so the row
        tints stay in step with the new palette.
        """
        s = self.style
        # Slightly taller rows so values breathe and the visual separation
        # between rows is more apparent (Tk Treeview has no real cell
        # gridlines, alternating colours + extra height is the practical
        # substitute).
        s.configure("Treeview", rowheight=26)

        # Headings: visible bordered cells so the column boundaries actually
        # read, plus a strong horizontal line under the header band by way
        # of relief='solid'. Padding gives the captions some air.
        s.configure(
            "Treeview.Heading",
            padding=(6, 6),
            relief="solid",
            borderwidth=1,
        )

        # Derive alt-row tint from the theme's field background so it
        # works on both dark and light themes. The contrast is intentionally
        # subtle — strong stripes get noisy on five-row tables.
        base_bg = s.lookup("Treeview", "background") or "#2b3e50"
        alt_bg = _shift(base_bg, +14) if _is_dark(base_bg) else _shift(base_bg, -10)
        # Selection tint: per-theme override wins (e.g. claude.json sets a
        # muted gold tone via _meta.row_select_bg), otherwise we lift the
        # base by the same amount as text-selection inside Entry so the two
        # highlights look consistent.
        current = s.theme_use()
        theme_meta = getattr(self, "_custom_theme_by_code", {}).get(current, {})
        sel_bg = theme_meta.get("row_select_bg") or (
            _shift(base_bg, +46) if _is_dark(base_bg) else _shift(base_bg, -32)
        )
        sel_fg = "#FFFFFF" if _is_dark(base_bg) else "#000000"
        self._ROW_TAGS["even"] = {"background": base_bg}
        self._ROW_TAGS["odd"] = {"background": alt_bg}
        self._ROW_TAGS["selected"] = {"background": sel_bg, "foreground": sel_fg}

        # Override ttk's state-based selection map. ttkbootstrap already
        # configures `background=[("selected", colors.selectbg)]` and that
        # state map wins over our per-row tag backgrounds — which is why
        # the "selected" row tag alone wasn't enough. Pinning the map to
        # our `sel_bg` here is what actually paints the row gold (or
        # whatever the theme override is).
        s.map("Treeview",
              background=[("selected", sel_bg)],
              foreground=[("selected", sel_fg)])

        self._configure_notebook_tab_style()

    def _configure_notebook_tab_style(self) -> None:
        """Apply the active-tab tint. Extracted so we can call it after
        `_build_ui` too — at startup the notebook doesn't exist yet when
        _configure_styles runs, and something in ttkbootstrap's lazy
        widget setup overwrites our `style.map` once the notebook is
        actually instantiated. Re-running it post-build pins the colour
        in for good.

        Themes can set _meta.active_tab_bg to override; otherwise we fall
        back to the theme's primary colour. ttkbootstrap paints the
        visible tab face via `lightcolor` (not just `background`), so we
        override both — and `bordercolor` so the edge doesn't look
        stitched onto a different hue.
        """
        s = self.style
        current = s.theme_use()
        theme_meta = getattr(self, "_custom_theme_by_code", {}).get(current, {})
        active_tab_bg = theme_meta.get("active_tab_bg") or s.colors.primary
        active_tab_fg = "#FFFFFF" if _is_dark(active_tab_bg) else "#000000"
        # Pin explicit tab padding so future ttkbootstrap updates can't
        # accidentally inflate the tab strip — the user widget floats in
        # the empty strip ABOVE the tabs (see notebook.pack pady in
        # _build_ui), and that strip's height assumes compact tabs.
        s.configure("TNotebook.Tab", padding=(10, 4))
        s.map("TNotebook.Tab",
              background=[("selected", active_tab_bg)],
              lightcolor=[("selected", active_tab_bg)],
              bordercolor=[("selected", active_tab_bg)],
              foreground=[("selected", active_tab_fg)])

        # Scrollbar accent. ttkbootstrap paints the thumb from PhotoImage
        # assets (not via ttk colour options), so plain `style.configure`
        # doesn't reach it. Instead each Scrollbar is constructed with
        # bootstyle="success" — and we alias `colors.success` in
        # themes/claude.json to the same Steam-green as the active tab.
        # One source of truth in the theme palette, nothing to do here.

        # Text-selection inside Entry / Spinbox / Combobox / Text. ttkbootstrap
        # uses the theme's `selectbg` both for "readonly Entry background" AND
        # "highlighted text in an editable Entry" — when the two collapse to
        # the same value (as in our Claude palette where selectbg sits next
        # to inputbg) the highlight becomes invisible. Override here with a
        # clearly lifted shade derived from inputbg.
        input_bg = s.colors.inputbg
        text_sel_bg = (
            _shift(input_bg, +46) if _is_dark(input_bg) else _shift(input_bg, -32)
        )
        text_sel_fg = "#FFFFFF" if _is_dark(input_bg) else "#000000"
        for style_name in ("TEntry", "TSpinbox", "TCombobox"):
            s.configure(style_name,
                        selectbackground=text_sel_bg,
                        selectforeground=text_sel_fg)
        # Cache so widgets created later (and tk.Text, which isn't ttk) can
        # be configured to match.
        self._text_sel_bg = text_sel_bg
        self._text_sel_fg = text_sel_fg
        # If the template Text widget already exists (theme switch path),
        # refresh its colours too.
        if hasattr(self, "txt_template") and self.txt_template is not None:
            self.txt_template.configure(
                selectbackground=text_sel_bg, selectforeground=text_sel_fg
            )

    # ------------------------------------------------------------------
    # Layout-independent clipboard shortcuts
    # ------------------------------------------------------------------

    def _install_clipboard_shortcuts(self) -> None:
        """Make Ctrl+C/V/X/A work on Cyrillic (and other non-Latin) layouts.

        Default Tk bindings react to the letter `v`, `c`, etc. as derived
        from the active keyboard layout. On a Cyrillic layout the OS hands
        Tk a Cyrillic letter instead, so `<Control-v>` never matches and
        paste/copy/cut/select-all silently do nothing.

        We dispatch on the hardware keycode (which is layout-independent on
        Windows: A=65, C=67, V=86, X=88) and fire the corresponding virtual
        event, which the standard widgets already know how to handle.
        """
        # Windows VK codes for A/C/V/X.
        VK = {65: "<<SelectAll>>", 67: "<<Copy>>",
              86: "<<Paste>>",      88: "<<Cut>>"}

        def _handler(event):
            virt = VK.get(getattr(event, "keycode", -1))
            if not virt:
                return None
            w = event.widget
            if virt == "<<SelectAll>>":
                # tk.Text doesn't ship a <<SelectAll>> by default — do it
                # by hand. ttk.Entry exposes <<SelectAll>> properly.
                try:
                    cls = w.winfo_class()
                except tk.TclError:
                    return None
                if cls == "Text":
                    w.tag_add("sel", "1.0", "end-1c")
                    return "break"
            try:
                w.event_generate(virt)
            except tk.TclError:
                return None
            return "break"

        for cls in ("Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"):
            self.bind_class(cls, "<Control-KeyPress>", _handler, add="+")

    # ------------------------------------------------------------------
    # Click-to-sort on Treeview headings
    # ------------------------------------------------------------------

    # Columns we never sort by: "num" stays 1..N (re-numbered after sort),
    # "link" is just a clickable icon — sorting it is meaningless.
    # `num` is just a 1..N row counter — sorting it is the same as
    # "reset to file order" and looked confusing. `link` is the «🌐
    # Відкрити на маркеті» click target — same string in every row,
    # nothing to sort by. `imported` IS sortable now: the value is
    # "📥" vs "" (empty for manually-added rows), so a click bunches
    # the imported rows together which is exactly what the user wants.
    _UNSORTABLE_COLS = {"num", "link"}
    # Columns whose values are TITLES — sorted lexicographically, never
    # via the money-parse heuristic (digits inside "Spider-Man 2" are
    # not prices). Spans all trees: cards (name/type/game), games
    # (name), history (card/game). See the text branch in _sort_tree.
    _TEXT_SORT_COLS = {"name", "type", "game", "card", "status",
                       "operation", "date"}

    def _setup_sortable_columns(self, tree: ttk.Treeview,
                                column_keys: list[str]) -> None:
        """Wire up "click heading to sort" on a Treeview.

        Re-applying tree.heading(col, command=…) overwrites any existing
        command, so this is safe to call multiple times (e.g. after the
        tab is rebuilt).
        """
        tree._sort_col: str | None = None
        tree._sort_desc: bool = False
        for col in column_keys:
            if col in self._UNSORTABLE_COLS:
                continue
            tree.heading(col,
                         command=lambda c=col, tr=tree: self._sort_tree(tr, c))

    def _sort_tree(self, tree: ttk.Treeview, col: str) -> None:
        """Re-order rows by a column. Toggles ascending/descending on
        repeated clicks of the same column.

        Sort key tries numeric first (so "5.49 ₴" beats "12.00 ₴" the
        right way round), then alphabetic, then dumps empty / "—"
        placeholders at the end regardless of direction — UNLESS the
        column treats empty as a meaningful value (see `imported`,
        where "" means "manually added" and the user expects clicking
        twice to reverse the groups).
        """
        prev_col = getattr(tree, "_sort_col", None)
        prev_desc = getattr(tree, "_sort_desc", False)
        descending = (col == prev_col and not prev_desc)
        tree._sort_col = col
        tree._sort_desc = descending

        pairs = [(tree.set(iid, col), iid) for iid in tree.get_children("")]

        # `imported` is a two-state column: "📥" vs "" (manually added).
        # Both are first-class values, so empty must reverse together
        # with non-empty on a second click — otherwise the toggle does
        # nothing visible (the user just saw this happen). Plain string
        # sort over the two values is enough; no numeric / empty-last
        # special-casing here.
        if col in ("imported", "no_check", "no_alert"):
            pairs.sort(key=lambda p: p[0] or "", reverse=descending)
        elif col in self._TEXT_SORT_COLS:
            # Name-like columns: NEVER try the numeric parse — game and
            # card titles routinely contain digits ("Spider-Man 2",
            # "Gothic 1 Remake") and _try_parse_money would happily
            # extract them, bunching every numbered title into a bogus
            # "numeric" bucket (the wishlist-sort bug). Lexicographic
            # only, grouped by script per the user's spec: CJK first,
            # then Cyrillic, then Latin/other — each alphabetical.
            def text_key(pair):
                val = (pair[0] or "").strip()
                if not val or val == "—":
                    return (3, "")
                ch = val[0]
                code = ord(ch)
                if 0x2E80 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF:
                    group = 0   # CJK / kana
                elif 0x0400 <= code <= 0x04FF:
                    group = 1   # Cyrillic
                else:
                    group = 2   # Latin + everything else
                return (group, val.casefold())
            pairs.sort(key=text_key, reverse=descending)
        else:
            def key(pair):
                val = pair[0]
                if val is None or val in ("", "—"):
                    return (2, "")  # always at the end
                num = _try_parse_money(val)
                if num is not None:
                    return (0, num)
                return (1, str(val).lower())

            pairs.sort(key=key, reverse=descending)
            # Empty / dash rows always at the end, regardless of direction.
            # The (2, "") tie-key already does that on the way up; on the way
            # down `reverse=True` would put them first, so we segregate.
            if descending:
                empties = [p for p in pairs if p[0] in ("", "—", None)]
                others = [p for p in pairs if p[0] not in ("", "—", None)]
                pairs = others + empties

        for i, (_, iid) in enumerate(pairs):
            tree.move(iid, "", i)

        self._renumber_tree(tree)
        self._update_sort_indicators(tree)
        # Persist so a restart restores the same order. Cheap — config.json
        # writes are tiny and clicks on a heading are rare.
        self._persist_sort_state(tree)

    # Maps a Treeview widget back to a stable config key. We pin this
    # to the three tables that support sort persistence (purchase /
    # sale lists + history). Returns None for any other tree (so the
    # persist/restore helpers safely no-op).
    def _tree_sort_key(self, tree: ttk.Treeview) -> str | None:
        if tree is self.list_trees.get("buy"):
            return "buy"
        if tree is self.list_trees.get("sell"):
            return "sell"
        if tree is getattr(self, "hist_tree", None):
            return "history"
        if tree is getattr(self, "games_tree", None):
            return "games"
        return None

    def _persist_sort_state(self, tree: ttk.Treeview) -> None:
        """Save the (column, descending) pair for this tree to config.

        Stored under `ui.sort_state.<key>`, where key is buy / sell /
        history. Load-merge writeback so we don't clobber unrelated
        config keys; failures are non-fatal (warning only).
        """
        key = self._tree_sort_key(tree)
        if key is None:
            return
        col = getattr(tree, "_sort_col", None)
        desc = bool(getattr(tree, "_sort_desc", False))
        entry = {"col": col, "desc": desc} if col else None
        # Mirror in self.config_data so a same-session call to
        # `_restore_sort_state` reads the latest value without
        # re-hitting disk.
        self.config_data.setdefault("ui", {}).setdefault(
            "sort_state", {})[key] = entry
        try:
            on_disk = load_json(CONFIG_PATH) or {}
        except Exception:
            on_disk = {}
        ui_block = on_disk.setdefault("ui", {})
        sort_block = ui_block.setdefault("sort_state", {})
        sort_block[key] = entry
        try:
            save_json(CONFIG_PATH, on_disk)
        except Exception as e:
            log.warning("could not persist sort state: %s", e)

    def _restore_sort_state(self, tree: ttk.Treeview) -> None:
        """Re-apply the saved column + direction after a tree refresh.

        Safe no-op when nothing is saved, or when the saved column
        doesn't exist anymore (e.g. config from an older schema).
        Called at the end of `_refresh_card_list` / `_refresh_history`.
        """
        key = self._tree_sort_key(tree)
        if key is None:
            return
        saved = (self.config_data.get("ui", {})
                                  .get("sort_state", {}) or {}).get(key)
        if not saved:
            return
        col = saved.get("col")
        if not col or col not in tree["columns"] or col in self._UNSORTABLE_COLS:
            return
        desc = bool(saved.get("desc"))
        # Reuse `_sort_tree`'s toggle logic by seeding the "previous"
        # state: if we want descending, pretend the column was just
        # clicked once (asc), so the next call flips to desc. For
        # ascending, leave prev_col=None so the first click goes asc.
        tree._sort_col = col if desc else None
        tree._sort_desc = False
        # `_sort_tree` itself re-saves to config; that's a no-op write
        # of the same value, so fine.
        self._sort_tree(tree, col)

    @staticmethod
    def _renumber_tree(tree: ttk.Treeview) -> None:
        """Refresh the "num" column to 1..N in the current visual order.

        Called after every sort or after row-reorder operations so the №
        column reads naturally regardless of the underlying file order.
        """
        for i, iid in enumerate(tree.get_children("")):
            vals = list(tree.item(iid, "values"))
            if vals:
                vals[0] = i + 1
                tree.item(iid, values=vals)

    def _update_sort_indicators(self, tree: ttk.Treeview) -> None:
        """Append ▲/▼ to the active column heading; strip from the rest."""
        active = getattr(tree, "_sort_col", None)
        desc = getattr(tree, "_sort_desc", False)
        for c in tree["columns"]:
            text = tree.heading(c, "text")
            # Trim any trailing arrow we put there before.
            text = text.rstrip().rstrip("▲▼").rstrip()
            if c == active:
                text = f"{text}  {'▼' if desc else '▲'}"
            tree.heading(c, text=text)

    def _apply_row_tags(self, tree: ttk.Treeview) -> None:
        """Register all known row tags on a Treeview widget.

        Treeview tag configuration is per-widget, not per-style, so we have
        to do this once per tree we create.
        """
        for tag, opts in self._ROW_TAGS.items():
            if opts:
                tree.tag_configure(tag, **opts)

    @staticmethod
    def _autohide_scrollbar(sb: ttk.Scrollbar, first, last) -> None:
        """yscrollcommand wrapper: hides the scrollbar when content fits.

        Standard ttk.Scrollbar doesn't have a built-in auto-hide behaviour —
        it stays at full size even when there's nothing to scroll, eating
        horizontal space and looking like dead UI. This wrapper inspects
        the `first`/`last` fractions Tk hands us on each scroll update:
        if they span the entire range [0.0, 1.0] the content fits in view
        and we hide the bar via grid_remove; otherwise we put it back with
        grid() (grid_remove preserves the previous grid options, so calling
        grid() with no args restores them).

        Requires the scrollbar to be packed via `grid()`, not `pack()` —
        pack_forget loses the position info and re-pack ends up appending
        the bar to the wrong slot, making it never reappear.
        """
        first_f, last_f = float(first), float(last)
        if first_f <= 0.0 and last_f >= 1.0:
            sb.grid_remove()
        else:
            sb.grid()
        sb.set(first, last)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Pack the status bar FIRST. Tk's pack manager allocates space
        # in pack order; if the notebook (which expand=YES) packs before
        # the status bar, the status bar gets pushed out of view when the
        # window is resized smaller. Reserving the BOTTOM slot now means
        # whatever notebook claims is "everything except the bottom row".
        self.statusbar = ttk.Label(self, text="  " + t("status.ready"),
                                    relief="sunken", anchor=W,
                                    justify=LEFT)
        self.statusbar.pack(fill=X, side=BOTTOM, ipady=2)
        # Wrap instead of clip: when the window is narrow the status
        # line flows onto a second row (the Label grows, pack fill=X
        # re-reserves the height) so the full text is always readable.
        self.statusbar.bind(
            "<Configure>",
            lambda e: self.statusbar.configure(
                wraplength=max(200, e.width - 16)),
        )

        self.notebook = ttk.Notebook(self)
        # pady=(25, 0) leaves a 25-px gap between the title bar and the
        # tab row. The Steam user widget floats into that gap via place(),
        # so it visually lives there without claiming a pack row of its own.
        self.notebook.pack(fill=BOTH, expand=YES, padx=8, pady=(25, 0))

        # Steam user widget — placed into the empty strip above the
        # notebook's tab row. Floats over `self`, anchored to top-right.
        self._build_user_widget()

        # New tab order: Придбання | Продаж | Історія | Планувальник |
        # Журнал | Налаштування. Index 0 = buy list, 1 = sell list — used by
        # _active_kind() to figure out which file an action targets.
        # Tabs that are tall by nature (full table + two rows of buttons +
        # action strip below) get a ScrolledFrame inside — at higher
        # font scales their content overflows the window vertically and
        # the buttons drift off-screen. autohide=True keeps the scrollbar
        # invisible when content fits.
        # Notebook can only host plain ttk.Frame children, so each
        # scrollable tab is a small holder Frame with a ScrolledFrame
        # packed inside, and we expose the ScrolledFrame as `self.tab_*`.
        from ttkbootstrap.widgets.scrolled import ScrolledFrame

        def _scrollable_tab() -> ScrolledFrame:
            holder = ttk.Frame(self.notebook)
            # Don't pass bootstyle to ScrolledFrame — it forwards it to
            # the INNER content frame too (see scrolled.py: super().__init__
            # gets `bootstyle=bootstyle.replace('round', '')`), which makes
            # ttkbootstrap paint a tinted border around every scrollable
            # tab. We only want the scrollbar coloured; we set its bootstyle
            # directly below.
            sf = ScrolledFrame(holder, autohide=True)
            sf.pack(fill=BOTH, expand=YES)
            # Stash the holder on the ScrolledFrame so the notebook.add
            # loop below can find it without us juggling two refs everywhere.
            sf._holder = holder
            # Tint ONLY the vertical scrollbar (matches Treeview's green
            # accent). Leaves the content frame styled like a plain Frame.
            try:
                sf.vscroll.configure(bootstyle="success")
            except tk.TclError:
                pass
            # ttkbootstrap's autohide is mouse-based — entering the frame
            # always pop the scrollbar, even when the content fits and
            # there's nothing to scroll. Wrap show_scrollbars so it only
            # actually shows when the thumb is shorter than the viewport
            # (i.e. content overflows).
            _orig_show = sf.show_scrollbars
            def _smart_show(_orig=_orig_show, _sf=sf):
                try:
                    _, thumb = _sf._measures()
                except (AttributeError, tk.TclError, ZeroDivisionError):
                    thumb = 0.0
                if thumb < 0.999:
                    _orig()
                else:
                    _sf.hide_scrollbars()
            sf.show_scrollbars = _smart_show
            # Stretch the inner content frame so it fills the container's
            # vertical extent when content is SHORTER than the container.
            # Default ScrolledFrame places its content at `height=reqheight`
            # (sum of children's natural sizes) — so a `pack(expand=YES)`
            # child inside has no extra space to expand into when the tab
            # area is taller than the content's reqheight. Visible bug:
            # a table whose data rows don't fill the treeview leaves the
            # bottom buttons floating in the middle of the tab instead of
            # near the table's bottom edge.
            def _stretch(_e=None, _sf=sf):
                try:
                    c_h = _sf.container.winfo_height()
                    req_h = _sf.winfo_reqheight()
                    target_h = max(req_h, c_h)
                    # Skip when nothing changes — content_place would
                    # re-fire Configure and loop.
                    if abs(_sf.winfo_height() - target_h) > 1:
                        _sf.content_place(rely=0.0, relwidth=1.0,
                                          height=target_h)
                except (tk.TclError, AttributeError):
                    pass
            sf.container.bind("<Configure>", _stretch, add="+")
            return sf

        self.tab_purchase  = _scrollable_tab()
        self.tab_sales     = _scrollable_tab()
        self.tab_games     = _scrollable_tab()
        self.tab_history   = _scrollable_tab()
        self.tab_scheduler = ttk.Frame(self.notebook)
        self.tab_log       = ttk.Frame(self.notebook)
        self.tab_settings  = _scrollable_tab()

        for tab, key in [
            (self.tab_purchase,  "tab.purchase"),
            (self.tab_sales,     "tab.sales"),
            (self.tab_games,     "tab.games"),
            (self.tab_history,   "tab.history"),
            (self.tab_scheduler, "tab.scheduler"),
            (self.tab_log,       "tab.log"),
            (self.tab_settings,  "tab.settings"),
        ]:
            # If `tab` is a ScrolledFrame, give the notebook its holder
            # frame (notebook only accepts plain ttk.Frame children).
            # For regular ttk.Frame tabs, add directly.
            real_tab = getattr(tab, "_holder", tab)
            self.notebook.add(real_tab, text=t(key))

        # «Ігри» is gated by the Settings «Бонусний контент» checkbox.
        # notebook.hide keeps the widget (and its data) alive and
        # remembers the position, so re-enabling restores it exactly
        # between Продаж and Історія. notebook.add(holder) un-hides.
        if not (self.config_data.get("ui", {}) or {}).get("bonus_content"):
            self.notebook.hide(self.tab_games._holder)

        # When the user switches tabs, re-read the underlying JSON so any
        # change made by an out-of-process watch.py run is reflected
        # without manual intervention.
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Per-kind widget dicts so action callbacks can dispatch on
        # _active_kind() and not duplicate state. Filled in by
        # _build_card_list_tab().
        self.list_trees: dict[str, ttk.Treeview] = {}
        self.list_action_buttons: dict[str, dict[str, ttk.Button]] = {}

        self._build_card_list_tab(self.tab_purchase, "buy")
        self._build_card_list_tab(self.tab_sales,    "sell")
        self._build_games_tab()
        self._build_settings_tab()
        self._build_scheduler_tab()
        self._build_history_tab()
        self._build_log_tab()

        # Cap the minimum window size so the user can't crush it into
        # something that hides the last tab behind the user widget, or
        # collapses the table to fewer than five visible rows.
        # `update()` (not just update_idletasks) forces Tk to actually
        # render the notebook tab strip, so bbox(i) returns real coords
        # instead of zeros on the first measurement.
        self.update()
        self._apply_min_size()

        # Global click handler — clears card-list selection when the user
        # clicks on "neutral" GUI area (not on a Treeview, not on a button
        # that needs the selection to do its job). Lets the user dismiss a
        # selection without having to Ctrl+click each row, which is the
        # standard expectation from desktop apps. See `_on_global_click`
        # for the widget-type filter.
        self.bind_all("<Button-1>", self._on_global_click, add="+")

    def _on_global_click(self, event):
        """Clear card-list selections when the click is outside actionable widgets.

        Action buttons consume the selection (Видалити, Змінити ціль,
        etc.) so we never clear when clicking those — the button's
        command needs the selection to be intact. Treeviews and their
        scrollbars handle their own selection behaviour. Anything else
        (Frames, Labels, the empty space between widgets) is "neutral"
        and clicking there should dismiss the selection.

        Walks `event.widget.master` chain so descendants of an action
        button or Treeview are caught too (in practice neither has many
        descendants, but the walk is cheap).

        `event.widget` is normally a widget instance but Tk sometimes
        hands us its pathname STRING instead — happens when the source
        is a transient widget that's already been destroyed (e.g. a
        tk_popup menu just dismissed), or in edge cases on Windows for
        clicks inside Toplevel popups. A string has no `.master`, so we
        resolve it back to a widget via `self.nametowidget(...)` (Tk's
        own name→widget lookup). Fall back to bailing if the name can't
        be resolved either — the widget is truly gone and there's
        nothing to walk.
        """
        widget = event.widget
        if isinstance(widget, str):
            try:
                widget = self.nametowidget(widget)
            except (KeyError, tk.TclError):
                return
        cur = widget
        while cur is not None:
            if isinstance(cur, (ttk.Button, ttk.Treeview,
                                ttk.Scrollbar, ttk.Entry,
                                tk.Text, ttk.Combobox, ttk.Spinbox,
                                ttk.Radiobutton, ttk.Checkbutton,
                                tk.Menu)):
                # Click landed on (or inside) an interactive control —
                # leave the selection alone so the control's own
                # behaviour can rely on it. tk.Menu added so right-click
                # menu interactions don't trip the neutral-area dismiss.
                return
            cur = getattr(cur, "master", None)
        # Neutral area click — drop selection on every card-list tree.
        for tree in self.list_trees.values():
            try:
                sel = tree.selection()
                if sel:
                    tree.selection_remove(*sel)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # Steam user widget (top-right)
    # ------------------------------------------------------------------

    # Avatar canvas dimensions. Steam's site header uses ~32 px round
    # avatars; we bump to 40 so the icon is comfortably readable inside
    # the 25-px floating strip above the tab row.
    _AVATAR_SIZE = 40

    def _build_user_widget(self) -> None:
        """Floating Steam user cluster in the strip above the tabs.

        Layout mirrors Steam's site header — username on top, balance in
        smaller muted text below, round avatar pinned to the right edge.
        Placed via `place(relx=1.0, anchor='ne', ...)` so the widget tracks
        the right edge of the window when resized, and floats over the
        25-px gap between the title bar and the notebook's tab row (see
        notebook.pack pady in _build_ui).

        Default state: bundled Steam logo + "Username" + "0.00 ₴".
        `_update_user_widget` swaps them once Steam login is wired up.
        """
        # x=-14 keeps a margin from the right edge; y=5 nudges down a
        # touch so the cluster doesn't crash into the title bar above.
        # Stored as `self.user_cluster` so _apply_min_size can ask for
        # its width when computing the window's minsize.
        cluster = ttk.Frame(self)
        self.user_cluster = cluster
        cluster.place(relx=1.0, x=-14, y=5, anchor="ne")

        # Two-line text column (username on top, balance below). Right-aligned
        # so longer nicknames push leftward, leaving the avatar pinned.
        text_col = ttk.Frame(cluster)
        text_col.pack(side=LEFT, padx=(0, 8))

        self.lbl_username = ttk.Label(text_col, text="Username", anchor=E)
        # Registered for font scaling — non-default size so we can't rely
        # on the named-font path picking it up automatically.
        self._scaled_font(self.lbl_username, 10, "bold")
        self.lbl_username.pack(side=TOP, anchor=E)
        # Click → open the user's Steam Community profile. URL uses the
        # numeric steamID (works whether the account has a vanity URL or
        # not — Steam redirects /profiles/{id} → /id/{vanity} when one
        # exists). Set up here once so we don't have to re-bind every
        # time `_update_user_widget` runs.
        self._make_widget_clickable(
            self.lbl_username, self._open_profile_link,
        )

        # Wallet balance — same currency symbol the rest of the app uses
        # so it stays consistent if the user later switches currency.
        sym = self._currency_symbol()
        # Muted foreground — match Steam's secondary-text colour.
        muted_fg = "#888888"
        self.lbl_balance = ttk.Label(
            text_col, text=f"0.00 {sym}", anchor=E,
            foreground=muted_fg,
        )
        self.lbl_balance.pack(side=TOP, anchor=E)
        # Click → Steam's store-transactions history page. This is where
        # the wallet balance actually breaks down (purchases, top-ups,
        # market sales credited as wallet funds).
        self._make_widget_clickable(
            self.lbl_balance,
            lambda: webbrowser.open(
                "https://store.steampowered.com/account/store_transactions/"
            ),
        )

        # Round avatar canvas. By default carries the Steam logo from
        # assets/steam_icon.png (circular-masked); when login lands, the
        # user's real avatar swaps in via _update_user_widget.
        self.avatar_canvas = tk.Canvas(
            cluster, width=self._AVATAR_SIZE, height=self._AVATAR_SIZE,
            highlightthickness=0, borderwidth=0,
        )
        self.avatar_canvas.pack(side=LEFT)
        # Match canvas background to theme so the round-corner letterbox
        # blends in instead of showing a flat square.
        bg = self.style.colors.bg
        self.avatar_canvas.configure(background=bg)
        self._draw_placeholder_avatar()

        # The notebook is packed BEFORE the user-widget is placed (see
        # _build_ui). Without lift(), the notebook's frame paints over
        # our placed cluster and the avatar disappears. lift() raises us
        # in the z-order so the overlay stays visible.
        cluster.lift()

    def _draw_placeholder_avatar(self) -> None:
        """Show the bundled Steam logo, circular-cropped, on the avatar canvas.

        Falls back to a hand-drawn 'S' circle if Pillow / the asset are
        missing — happens on a fresh checkout before the user runs
        `pip install -r requirements.txt`.
        """
        size = self._AVATAR_SIZE
        c = self.avatar_canvas
        c.delete("all")
        asset_path = BASE / "assets" / "steam_icon.png"
        try:
            from PIL import Image, ImageDraw, ImageTk
            img = Image.open(asset_path).convert("RGBA")
            img = img.resize((size, size), Image.LANCZOS)
            # Circular alpha mask so the square PNG ends up as a round
            # avatar that blends with the theme background.
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
            img.putalpha(mask)
            # Keep a reference on the instance — Tk drops images that are
            # only held by canvas items (the canvas keeps a weakref).
            self._avatar_placeholder_photo = ImageTk.PhotoImage(img)
            c.create_image(size / 2, size / 2,
                           image=self._avatar_placeholder_photo)
        except Exception:
            # Pillow missing or asset not bundled — paint a basic Steam-blue
            # circle with a white "S" so something is still on screen.
            c.create_oval(1, 1, size - 1, size - 1,
                          fill="#1B2838", outline="")
            c.create_text(size / 2, size / 2,
                          text="S", fill="#FFFFFF",
                          font=("Segoe UI", int(size * 0.55), "bold"))

    # ------------------------------------------------------------------
    # Session-expired UX: ⚠ badge on the avatar + toast above the widget
    # ------------------------------------------------------------------
    #
    # Triggered by `fetch_wallet_info` returning `session_expired=True`
    # — Steam responded but with the logged-out page shape, i.e. our
    # cookies are stale. We surface this two ways:
    #   * A small ⚠ badge painted on the bottom-right of the avatar
    #     canvas, blinking once per second so it draws the eye.
    #   * A toast (borderless Toplevel) just below the user-widget,
    #     auto-dismissing after ~12 seconds or on click.
    # Both are clickable and open the Steam-login dialog.
    #
    # State is idempotent through `_set_session_warning` — calling with
    # the same value as the current state is a no-op, so the worker
    # thread can fire it on every refresh without flicker.

    # Badge dimensions inside the avatar canvas. Sized so the ⚠ glyph
    # is readable but doesn't crowd the avatar — about 40% of canvas.
    _SESSION_BADGE_DIAMETER = 18
    _SESSION_BADGE_BLINK_MS = 700

    def _set_session_warning(self, active: bool) -> None:
        """Flip the session-expired indicator on or off (idempotent).

        Persists the active state on `self._session_warning_active` so
        the toast and blink loop know whether they're still wanted.
        Tolerates being called before `_build_user_widget` (early
        bootstrap path) — bails silently when the avatar canvas isn't
        there yet.
        """
        # Lazy init — these attrs don't exist until first call.
        if not hasattr(self, "_session_warning_active"):
            self._session_warning_active = False
            self._session_warning_blink_visible = True
            self._session_warning_blink_job = None
            self._session_warning_toast = None
        if active == self._session_warning_active:
            return
        self._session_warning_active = active
        canvas = getattr(self, "avatar_canvas", None)
        if canvas is None:
            return
        if active:
            log.info("steam session expired — showing badge + toast")
            self._draw_session_badge()
            # Tag-bind so only clicks on the badge itself trigger login
            # — clicks elsewhere on the avatar still do nothing (and
            # the username/balance labels keep their own bindings).
            try:
                canvas.tag_bind("session_warning", "<Button-1>",
                                lambda _e: self._open_steam_login_dialog())
                canvas.tag_bind("session_warning", "<Enter>",
                                lambda _e: canvas.configure(cursor="hand2"))
                canvas.tag_bind("session_warning", "<Leave>",
                                lambda _e: canvas.configure(cursor=""))
            except tk.TclError:
                pass
            self._start_session_badge_blink()
            self._show_session_expired_toast()
        else:
            log.info("steam session restored — clearing badge")
            if self._session_warning_blink_job is not None:
                try:
                    self.after_cancel(self._session_warning_blink_job)
                except (tk.TclError, ValueError):
                    pass
                self._session_warning_blink_job = None
            try:
                canvas.delete("session_warning")
            except tk.TclError:
                pass
            self._dismiss_session_expired_toast()

    def _draw_session_badge(self) -> None:
        """Paint the ⚠ badge on the avatar canvas (idempotent)."""
        canvas = getattr(self, "avatar_canvas", None)
        if canvas is None:
            return
        try:
            canvas.delete("session_warning")
        except tk.TclError:
            return
        if not self._session_warning_blink_visible:
            return
        d = self._SESSION_BADGE_DIAMETER
        size = self._AVATAR_SIZE
        # Bottom-right corner, with a 1 px margin from the canvas edge.
        x1 = size - d - 1
        y1 = size - d - 1
        x2 = size - 1
        y2 = size - 1
        # Yellow disk + dark outline so it reads on both bright and
        # dark avatars.
        canvas.create_oval(
            x1, y1, x2, y2,
            fill="#F5C518", outline="#1B2838", width=2,
            tags=("session_warning",),
        )
        # Hand-drawn "!" — Tk's font rendering of "⚠" varies wildly
        # across platforms, so a centred exclamation glyph is more
        # reliable. Two pieces: vertical stroke + dot at the bottom.
        cx = (x1 + x2) / 2
        # Vertical bar
        canvas.create_line(
            cx, y1 + 4, cx, y2 - 5,
            fill="#1B2838", width=2, capstyle="round",
            tags=("session_warning",),
        )
        # Dot
        canvas.create_oval(
            cx - 1, y2 - 4, cx + 1, y2 - 2,
            fill="#1B2838", outline="",
            tags=("session_warning",),
        )

    def _start_session_badge_blink(self) -> None:
        """Schedule the badge blink loop while warning is active.

        Toggles `_session_warning_blink_visible` and redraws every
        `_SESSION_BADGE_BLINK_MS` ms. Self-stops when the warning is
        cleared (the `_session_warning_active` check in the callback).
        """
        if not self._session_warning_active:
            return
        self._session_warning_blink_visible = \
            not self._session_warning_blink_visible
        self._draw_session_badge()
        self._session_warning_blink_job = self.after(
            self._SESSION_BADGE_BLINK_MS,
            self._start_session_badge_blink,
        )

    def _show_session_expired_toast(self) -> None:
        """Pop a small clickable toast just under the user-widget.

        Implemented as a `place`d `tk.Frame` INSIDE the main window
        (not a standalone Toplevel) — same trick as the avatar cluster.
        A Toplevel positioned in screen coords drifted off-screen when
        the main window sat near the top edge of the display; a placed
        child can't leave the window and tracks it on move/resize for
        free. Auto-dismisses after ~12 s or on click (→ login dialog).
        Re-entrant: if a toast is already up, do nothing.
        """
        if (self._session_warning_toast is not None
                and self._session_warning_toast.winfo_exists()):
            return
        # Fill with the ACTIVE-TAB accent (Steam-green on the claude
        # theme) — the earlier danger-red blended into the red row
        # tints of the table behind it.
        current_theme = self.style.theme_use()
        theme_meta = getattr(self, "_custom_theme_by_code", {}) \
            .get(current_theme, {})
        bg = theme_meta.get("active_tab_bg") or self.style.colors.success
        fg = "#000000"
        # Plain tk.Frame child of the main window so we can colour-fill
        # the bg (ttk.Frame ignores background overrides on most themes).
        frame = tk.Frame(self, padx=12, pady=8,
                         highlightthickness=1, highlightbackground="#000000")
        self._session_warning_toast = frame
        lbl = tk.Label(
            frame,
            text=t("toast.session_expired"),
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        lbl.pack()
        # Colours applied POST-creation on purpose: ttkbootstrap hooks
        # the vanilla tk widget constructors and slaps the theme's
        # background on top of whatever kwargs we passed. A .configure()
        # after __init__ wins because the hook only fires at construction.
        frame.configure(background=bg)
        lbl.configure(background=bg, foreground=fg)
        # Click handler — both label + frame so the user can hit either.
        def _on_click(_e=None):
            self._dismiss_session_expired_toast()
            self._open_steam_login_dialog()
        for w in (frame, lbl):
            w.bind("<Button-1>", _on_click)
        # Place top-right, just below the avatar cluster. relx=1.0 +
        # anchor="ne" + x=-14 mirrors the cluster's own placement, so
        # the toast's right edge lines up with the avatar's. y is the
        # cluster's bottom + a small gap (cluster is place'd at y=5).
        self.update_idletasks()
        cluster = getattr(self, "user_cluster", None)
        cluster_h = cluster.winfo_height() if cluster else 40
        frame.place(relx=1.0, x=-14, y=5 + cluster_h + 4, anchor="ne")
        frame.lift()
        # Auto-dismiss after 12 seconds. Stored as an after-id so a
        # manual dismiss can cancel it.
        frame._dismiss_job = frame.after(
            12_000, self._dismiss_session_expired_toast,
        )

    def _dismiss_session_expired_toast(self) -> None:
        """Tear down the session-expired toast if it's up."""
        frame = self._session_warning_toast
        self._session_warning_toast = None
        if frame is None:
            return
        try:
            if getattr(frame, "_dismiss_job", None) is not None:
                frame.after_cancel(frame._dismiss_job)
        except (tk.TclError, ValueError, AttributeError):
            pass
        try:
            frame.destroy()
        except tk.TclError:
            pass

    def _measure_last_tab_right(self, nb: ttk.Notebook, n_tabs: int) -> int:
        """Right pixel coordinate of the last tab in the strip.

        Tries `notebook.bbox(i)` first — that's authoritative once Tk has
        rendered the strip. If bbox returns zeros (window not realised
        yet on this platform / first paint hasn't completed) we estimate
        from the tab labels' rendered text width: sum every tab's text
        width + 2× horizontal tab-padding + 2 px of border-ish slop per
        tab. Slightly over-counts, which is fine — under-counting would
        let the user widget creep onto the last tab.
        """
        last_tab_right = 0
        for i in range(n_tabs):
            try:
                x, _y, w, _h = nb.bbox(i)
                last_tab_right = max(last_tab_right, x + w)
            except (tk.TclError, TypeError, ValueError):
                pass
        if last_tab_right > 0:
            return last_tab_right

        # Fallback — measure text width of each tab label using the
        # current TkDefaultFont (named-font scaling already applied).
        import tkinter.font as tkfont
        try:
            f = tkfont.nametofont("TkDefaultFont")
        except tk.TclError:
            return 0
        # padding=(10, 4) → 10 px on each side = 20 horizontal.
        tab_h_padding = 20
        # +2 px slop per tab for the border / separator.
        slop = 2
        total = 0
        for i in range(n_tabs):
            try:
                text = nb.tab(i, "text") or ""
            except tk.TclError:
                text = ""
            total += f.measure(text) + tab_h_padding + slop
        return total

    def _apply_min_size(self) -> None:
        """Lock a sensible floor for window resize.

        Two visual contracts to preserve when the user shrinks the window:

        - **Width**: the last notebook tab must remain fully visible,
          NOT covered by the user widget (which floats via place() and
          would otherwise overlap on a narrow window).
        - **Height**: at least 5 rows of the table must be visible.

        We measure live geometry (tab right-edges via `notebook.bbox(i)`,
        user widget width via `winfo_reqwidth`, treeview row height from
        the active style) instead of hard-coding pixel constants — that
        way changing language / font / themes doesn't break the floor.
        """
        self.update_idletasks()

        # --- Width ---
        nb = self.notebook
        n_tabs = nb.index("end")
        last_tab_right = self._measure_last_tab_right(nb, n_tabs)

        # `winfo_reqwidth` on a freshly placed widget can return tiny
        # values before Tk has rendered the avatar + text — fall back to
        # a realistic minimum (~160 px is what the cluster needs for the
        # Steam icon + a longer username at x1).
        widget_w = max(self.user_cluster.winfo_reqwidth(), 160)
        # Scale-aware extra breathing — at higher font_scale even
        # winfo_reqwidth lags reality by a few characters worth.
        scale_mult = self._FONT_SCALE_FACTORS.get(self._font_scale, 1.0)
        widget_w = int(max(widget_w, 160 * scale_mult))

        # 12 px of breathing room between the last tab and the widget,
        # plus the notebook's own 8 px padx on each side, plus 14 px the
        # widget keeps from the right edge (see _build_user_widget x=-14).
        min_w = last_tab_right + 12 + widget_w + 14 + 16

        # --- Height ---
        # ttk.Style stores the row height for Treeview under
        # "Treeview" rowheight; fall back to 25 px if it's unset.
        try:
            row_h = int(self.style.lookup("Treeview", "rowheight") or 25)
        except (ValueError, tk.TclError):
            row_h = 25
        # Five data rows + table header + tab strip + button rows + status
        # bar + top gap. Rough but reliable numbers that match what we
        # actually pack/place above and below the treeview.
        chrome_h = (
            25   # title-bar gap (notebook.pack pady top)
            + 28 # notebook tab strip
            + 28 # treeview header row
            + 36 # row1 of buttons
            + 36 # row2 of buttons
            + 22 # status bar
            + 24 # frame paddings / borders
        )
        min_h = 5 * row_h + chrome_h

        self.minsize(min_w, min_h)

    def _make_widget_clickable(self, widget, callback) -> None:
        """Wire `widget` to call `callback()` on click + hand cursor on hover.

        Used for the floating Steam-widget labels (username → profile,
        balance → store transactions). We do the cursor switch + click
        bind once at widget-create time so the link "feel" is there
        even before there's a real session — clicking the placeholder
        username still opens whatever profile is currently saved, or
        no-ops if nothing is saved.
        """
        widget.configure(cursor="hand2")
        widget.bind("<Button-1>", lambda e: callback())

    def _open_profile_link(self) -> None:
        """Open the current user's Steam Community profile in a browser.

        Uses `/profiles/{steamID64}` rather than `/id/{vanity}` because
        the numeric form always works (Steam auto-redirects to the
        vanity URL if one exists). No-op when no Steam ID is saved —
        the floating widget exists in placeholder mode for users who
        haven't logged in yet, and we don't want the click to lead
        nowhere noisy.
        """
        sid = ((self.config_data.get("steam") or {}).get("id") or "").strip()
        if not sid:
            return
        webbrowser.open(f"https://steamcommunity.com/profiles/{sid}/")

    @staticmethod
    def _humanise_balance(raw: str) -> str:
        """Insert a thin space between the number and the currency symbol.

        Steam's wallet string varies by locale: "5,46₴" / "$1.23" /
        "1 234,56 ₽". Some include a space already, some don't. We
        normalise to "<number>\\u2009<symbol>" (THIN SPACE — visually
        a dot-width gap) so the widget reads cleanly regardless of
        which currency the user is on.

        Conservative: if the string doesn't contain any of the known
        currency glyphs, return it unchanged so we don't accidentally
        mangle something Steam decided to format differently in the
        future.
        """
        if not raw:
            return raw
        # Strip whatever whitespace Steam already put between digits and
        # symbol, then put back our own narrow gap. NARROW NO-BREAK
        # SPACE ( ) is preferred over THIN SPACE ( ) because
        # it doesn't wrap — important since the widget is narrow.
        symbols = "₴$€₽£¥"
        # Find the symbol position (last char that's a symbol).
        for i, ch in enumerate(raw):
            if ch in symbols:
                # Split into number + symbol; trim trailing whitespace
                # from number side.
                number = raw[:i].rstrip()
                # Take the symbol + anything after it (currency codes
                # sometimes follow, e.g. "1,23 €EUR").
                tail = raw[i:].lstrip()
                return f"{number} {tail}"
        return raw

    def _update_user_widget(self, *, username: str | None = None,
                            balance: str | None = None,
                            avatar_image: tk.PhotoImage | None = None) -> None:
        """Public API for the Steam-login feature to plug real data in.

        Pass any subset — the others keep their current value. `avatar_image`
        should already be cropped to a circle and sized to _AVATAR_SIZE;
        passing None redraws the placeholder.
        """
        if username is not None:
            self.lbl_username.configure(text=username)
        if balance is not None:
            # Normalise the gap between the number and currency symbol
            # — Steam's wallet string is inconsistent across locales
            # ("5,46₴" vs "$1.23"), and the placeholder we synthesise
            # at startup uses a regular space. _humanise_balance gives
            # us a single canonical look.
            self.lbl_balance.configure(text=self._humanise_balance(balance))
        if avatar_image is not None:
            # Keep a reference — Tk's image GC will collect it otherwise.
            self._user_avatar_ref = avatar_image
            self.avatar_canvas.delete("all")
            self.avatar_canvas.create_image(
                self._AVATAR_SIZE / 2, self._AVATAR_SIZE / 2,
                image=avatar_image,
            )

    # ------------------------------------------------------------------
    # Steam login (Phase 1 — Stage 1: manual ID only)
    #
    # The dialog is wired up below in _open_steam_login_dialog. This block
    # is the glue between the floating user-widget and `config.json.steam`
    # — load on startup, refresh after a successful manual save, wipe on
    # disconnect. QR + browser-cookie tiers will plug into the same widget
    # API in later stages.
    # ------------------------------------------------------------------

    def _load_steam_user_widget(self) -> None:
        """Apply saved `steam.{persona, avatar_url}` to the floating widget.

        No-op when the steam section is empty (fresh install / disconnect).
        The avatar download is offloaded to a daemon thread so a slow CDN
        response can't block the first paint; the persona label updates
        synchronously since it's already in memory.
        """
        steam_cfg = (self.config_data.get("steam") or {})
        persona = (steam_cfg.get("persona") or "").strip()
        avatar_url = (steam_cfg.get("avatar_url") or "").strip()
        if not persona and not avatar_url:
            return
        if persona:
            self._update_user_widget(username=persona)
        if avatar_url:
            self._fetch_avatar_async(avatar_url)
        # Pull wallet balance only if Tier 2 (browser cookies) ran —
        # _refresh_wallet_balance bails early when there are no cookies,
        # so this is safe to call unconditionally.
        self._refresh_wallet_balance()

    def _fetch_avatar_async(self, url: str) -> None:
        """Download `url`, circular-crop to _AVATAR_SIZE, push to the widget.

        Wraps `steam_login.download_avatar` in a daemon thread + after()
        bounce back onto the Tk main thread. Failures leave the existing
        avatar in place (placeholder or whatever was there before).
        """
        import steam_login

        size = self._AVATAR_SIZE

        def worker() -> None:
            photo = steam_login.download_avatar(url, size)
            if photo is None:
                return
            # PhotoImage must be created on the Tk thread to be safe — but
            # in practice CPython lets us build it off-thread; what we
            # *must* do on the main thread is the canvas update. Schedule
            # both the assignment and the redraw via after(0, ...).
            self.after(0, lambda p=photo: self._update_user_widget(avatar_image=p))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_wallet_balance(self) -> None:
        """Pull wallet balance + currency + country in a worker thread.

        Tier 2 (browser cookies) is the only login flow that gets us a
        real session; Tier 3 (manual ID) leaves `steam.cookies` empty
        and we just leave the widget showing the placeholder. The same
        applies after a Disconnect — `_disconnect_steam` wipes cookies
        AND resets the balance label to the placeholder.

        Beyond the floating-widget balance string, this also reads the
        account's wallet_currency + wallet_country and pushes them
        into `config.json.market`. Why here: the account page (the
        only Steam endpoint we already authenticate against) carries
        all three pieces of info in one request, so a single call
        synchronises everything. Without this, the Settings pickers
        would lie about the live account after a cookies login —
        which is exactly the bug the user hit.

        Persisted fields (`market.currency`, `market.country`) drive
        Steam-Market polling and the History totals symbol, so getting
        them in sync with the actual Steam account is what makes the
        rest of the GUI honest about which locale we're operating in.
        """
        import steam

        steam_cfg = self.config_data.get("steam") or {}
        cookies = steam_cfg.get("cookies")
        if not cookies:
            return

        def worker() -> None:
            try:
                info = steam.fetch_wallet_info(cookies)
            except Exception as e:
                log.warning("wallet refresh raised: %s", e)
                return
            if not info:
                return
            self.after(0, lambda i=info: self._apply_wallet_info(i))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_wallet_info(self, info: dict) -> None:
        """Tk-thread half of `_refresh_wallet_balance` — apply fetched info.

        Receives a dict from `steam.fetch_wallet_info` with the keys
        balance, currency, country (any can be None independently).
        Updates floating widget + Settings pickers + persisted config.

        We re-load config from disk before writing so we don't clobber
        unrelated keys written by another process (watch.py mutates
        `state.json` but not config.json — still, the load-merge
        pattern keeps us future-proof).
        """
        balance = info.get("balance")
        currency = info.get("currency")
        country = info.get("country")
        expired = info.get("session_expired")

        # Session-expired UX: only flip on a definitive True/False; None
        # means "couldn't tell" (network error etc.) — leave the
        # previous state alone so a flaky moment doesn't trigger the
        # warning theatre.
        if expired is True:
            self._set_session_warning(True)
        elif expired is False:
            self._set_session_warning(False)

        if balance:
            self._update_user_widget(balance=balance)

        # Persist currency/country to config.market when they differ
        # from what's there now. Skip when fields are None — `fetch_wallet_info`
        # leaves them None if the page didn't carry the data, and we
        # don't want to wipe a user-set value over that.
        market = self.config_data.setdefault("market", {})
        changed = False
        if currency is not None and market.get("currency") != currency:
            market["currency"] = currency
            changed = True
        if country and market.get("country") != country:
            market["country"] = country
            changed = True
        if changed:
            try:
                on_disk = load_json(CONFIG_PATH) or {}
            except Exception:
                on_disk = {}
            on_disk_market = on_disk.setdefault("market", {})
            if currency is not None:
                on_disk_market["currency"] = currency
            if country:
                on_disk_market["country"] = country
            try:
                save_json(CONFIG_PATH, on_disk)
            except Exception as e:
                log.warning("could not persist market region from wallet: %s", e)

        # Push the new values into the Settings pickers so the user
        # sees them updated immediately (without having to close and
        # reopen the tab). Combobox `set()` updates the visible label;
        # `var_currency`/`var_country` get updated so Save still sees
        # the right values.
        self._sync_market_pickers()

        # History totals carry the currency symbol — recompute on the
        # tab so a freshly-applied account currency takes effect
        # without a restart. Guarded so we don't trip pre-build.
        if changed and hasattr(self, "_refresh_history"):
            try:
                self._refresh_history()
            except tk.TclError:
                pass

    def _sync_market_pickers(self) -> None:
        """Reflect `config.market.currency`/`country` in Settings pickers.

        Idempotent — safe to call even if Settings tab hasn't been
        built yet (just bails). Used after a cookies-tier login to
        push the account's wallet region into the visible dropdowns.
        """
        from regions import (STEAM_CURRENCIES, STEAM_COUNTRIES,
                             currency_label, country_label)
        market = self.config_data.get("market") or {}
        cur_code = market.get("currency")
        cnt_iso = market.get("country")

        cb_cur = getattr(self, "_cb_currency", None)
        cb_cnt = getattr(self, "_cb_country", None)
        var_cur = getattr(self, "var_currency", None)
        var_cnt = getattr(self, "var_country", None)

        if cur_code is not None and cb_cur is not None and var_cur is not None:
            try:
                var_cur.set(int(cur_code))
                if int(cur_code) in STEAM_CURRENCIES:
                    cb_cur.set(currency_label(int(cur_code)))
            except (tk.TclError, ValueError):
                pass

        if cnt_iso and cb_cnt is not None and var_cnt is not None:
            try:
                var_cnt.set(cnt_iso)
                name = next((n for iso, n, _c in STEAM_COUNTRIES
                             if iso == cnt_iso), None)
                if name:
                    cb_cnt.set(country_label(cnt_iso, name))
            except tk.TclError:
                pass

    def _open_steam_login_dialog(self) -> None:
        """Three-tier login dialog. Tier 3 (manual ID) is fully wired.

        Layout (top → bottom):
          * Tier 1 — QR section (stub label + "I don't have Steam Mobile"
            button that just jumps focus to the Tier 3 entry).
          * Tier 2 — browser section (stub label + disabled "Extract from
            browser" button).
          * Tier 3 — manual entry: Entry + "Save", live status line below,
            "Disconnect" button visible only when already connected.

        Saves to `config.json.steam` on success, refreshes the floating
        user-widget, and updates the dialog's own "connected as" line so
        the user gets immediate visual confirmation.
        """
        # Refuse to open twice — the dialog itself isn't reentrant-safe
        # (status labels and entry refs live on `self`).
        existing = getattr(self, "_steam_login_dlg", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        dlg = tk.Toplevel(self)
        self._steam_login_dlg = dlg
        dlg.title(t("dlg.steam_login.title"))
        dlg.transient(self)
        # Keep it modal-ish — easier to reason about state than a window
        # that survives application close or stale config_data.
        dlg.grab_set()
        dlg.resizable(False, False)

        outer = ttk.Frame(dlg, padding=14)
        outer.pack(fill=BOTH, expand=YES)

        muted_fg = self.style.colors.secondary

        # ---- Tier 1: QR (stub) -----------------------------------------
        qr_frame = ttk.LabelFrame(outer, text=t("dlg.steam_login.qr_title"), padding=10)
        qr_frame.pack(fill=X, pady=(0, 8))
        ttk.Label(
            qr_frame, text=t("dlg.steam_login.qr_body"),
            foreground=muted_fg, wraplength=480, justify=LEFT,
        ).pack(anchor=W)
        qr_row = ttk.Frame(qr_frame)
        qr_row.pack(anchor=W, pady=(6, 0))
        ttk.Label(qr_row, text=t("btn.in_development"),
                  foreground=muted_fg).pack(side=LEFT, padx=(0, 10))
        # "I don't have Steam Mobile" — courtesy shortcut: nudges focus
        # to the manual-entry field instead of having the user scroll.
        ttk.Button(
            qr_row, text=t("dlg.steam_login.qr_no_mobile"),
            bootstyle="link",
            command=lambda: self._steam_dlg_manual_entry.focus_set(),
        ).pack(side=LEFT)

        # ---- Tier 2: browser cookies (live) ----------------------------
        br_frame = ttk.LabelFrame(outer, text=t("dlg.steam_login.browser_title"), padding=10)
        br_frame.pack(fill=X, pady=(0, 8))
        ttk.Label(
            br_frame, text=t("dlg.steam_login.browser_body"),
            foreground=muted_fg, wraplength=480, justify=LEFT,
        ).pack(anchor=W)
        br_row = ttk.Frame(br_frame)
        br_row.pack(anchor=W, pady=(6, 0))
        # Stash the button reference so `_steam_dlg_refresh_connected_state`
        # can disable it while a Steam session is already saved — re-running
        # the cookie import while connected would just overwrite the saved
        # data with potentially-different cookies. The "Від'єднати" button
        # is the canonical way to clear state before re-importing.
        self._steam_dlg_browser_btn = ttk.Button(
            br_row, text=t("dlg.steam_login.browser_btn"),
            bootstyle="info",
            command=self._open_browser_cookies_dialog,
        )
        self._steam_dlg_browser_btn.pack(side=LEFT, padx=(0, 10))

        # ---- Tier 3: manual ID (live) ----------------------------------
        man_frame = ttk.LabelFrame(outer, text=t("dlg.steam_login.manual_title"), padding=10)
        man_frame.pack(fill=X, pady=(0, 8))
        ttk.Label(
            man_frame, text=t("dlg.steam_login.manual_body"),
            wraplength=480, justify=LEFT,
        ).pack(anchor=W)

        ent_row = ttk.Frame(man_frame)
        ent_row.pack(fill=X, pady=(8, 0))
        self._steam_dlg_manual_entry = ttk.Entry(ent_row, width=46)
        self._steam_dlg_manual_entry.pack(side=LEFT, fill=X, expand=YES)
        # Prefill with whatever's saved — makes it obvious what's currently
        # set and easy to overwrite. Empty if nothing saved yet.
        prev_id = ((self.config_data.get("steam") or {}).get("id") or "").strip()
        if prev_id:
            self._steam_dlg_manual_entry.insert(0, prev_id)
        ttk.Button(
            ent_row, text=t("dlg.steam_login.manual_btn"),
            bootstyle="success",
            command=self._steam_dlg_save_manual,
        ).pack(side=LEFT, padx=(8, 0))

        ttk.Label(
            man_frame, text=t("dlg.steam_login.manual_hint"),
            foreground=muted_fg,
        ).pack(anchor=W, pady=(4, 0))

        # Status line — also doubles as the "connected as" summary when
        # a profile is already saved. Initialised below right after the
        # button row so disconnect can target it too.
        self._steam_dlg_status = ttk.Label(man_frame, text="", wraplength=480, justify=LEFT)
        self._steam_dlg_status.pack(anchor=W, pady=(8, 0))

        # ---- Bottom row: Disconnect (if connected) + Close --------------
        bottom = ttk.Frame(outer)
        bottom.pack(fill=X, pady=(8, 0))
        self._steam_dlg_disconnect_btn = ttk.Button(
            bottom, text=t("dlg.steam_login.disconnect_btn"),
            bootstyle="danger-outline",
            command=self._steam_dlg_disconnect,
        )
        # Visibility toggled by _steam_dlg_refresh_connected_state below.
        ttk.Button(
            bottom, text=t("dlg.steam_login.close"),
            command=dlg.destroy,
        ).pack(side=RIGHT)

        # Cleanup on close — drop the dialog ref + window grab.
        def _on_dialog_close() -> None:
            try:
                dlg.grab_release()
            except tk.TclError:
                pass
            self._steam_login_dlg = None
            dlg.destroy()
        dlg.protocol("WM_DELETE_WINDOW", _on_dialog_close)

        # Initial state — show "connected as" line if a profile was already
        # saved earlier, else leave the status blank.
        self._steam_dlg_refresh_connected_state()

        # Centre over the main window so the dialog isn't hiding off-screen
        # on a multi-monitor setup. update_idletasks first so reqsize is real.
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_reqheight()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        self._steam_dlg_manual_entry.focus_set()

    def _steam_dlg_refresh_connected_state(self) -> None:
        """Pin the dialog's status line + Disconnect button to current config.

        Called on dialog open and after every successful save/disconnect.
        Keeps the visual state honest with what's actually persisted on disk.
        """
        steam_cfg = (self.config_data.get("steam") or {})
        persona = (steam_cfg.get("persona") or "").strip()
        inv = steam_cfg.get("inventory_public")
        if persona:
            inv_text = ""
            if inv is True:
                inv_text = f"  •  {t('dlg.steam_login.inventory_public')}"
            elif inv is False:
                inv_text = f"  •  {t('dlg.steam_login.inventory_private')}"
            line = t("dlg.steam_login.connected_as", persona=persona) + inv_text
            self._steam_dlg_status.configure(
                text=line, foreground=self.style.colors.success,
            )
            self._steam_dlg_disconnect_btn.pack(side=LEFT)
            # Lock the Tier 2 import button while a session is saved —
            # re-import without explicit Disconnect would clobber the
            # current state.
            if hasattr(self, "_steam_dlg_browser_btn"):
                self._steam_dlg_browser_btn.configure(state=DISABLED)
        else:
            self._steam_dlg_status.configure(text="", foreground="")
            self._steam_dlg_disconnect_btn.pack_forget()
            if hasattr(self, "_steam_dlg_browser_btn"):
                self._steam_dlg_browser_btn.configure(state=NORMAL)

    def _steam_dlg_save_manual(self) -> None:
        """Parse the manual-entry field, resolve it, persist, refresh widget.

        Runs the network calls in a background thread so the GUI stays
        responsive during the XML round-trip. Status label updates flow
        through `after(0, ...)` from the worker.
        """
        import steam_login

        raw = self._steam_dlg_manual_entry.get().strip()
        if not raw:
            self._steam_dlg_set_status(
                t("dlg.steam_login.status_empty"), kind="warning",
            )
            return

        self._steam_dlg_set_status(
            t("dlg.steam_login.status_resolving"), kind="muted",
        )

        def worker() -> None:
            try:
                kind, value = steam_login.parse_steam_id(raw)
            except steam_login.SteamLoginError:
                self.after(0, lambda: self._steam_dlg_set_status(
                    t("dlg.steam_login.status_bad_format"), kind="warning",
                ))
                return
            try:
                if kind == "vanity":
                    sid = steam_login.resolve_vanity(value)
                else:
                    sid = value
                profile = steam_login.fetch_public_profile(sid)
            except steam_login.SteamLoginError as e:
                msg = str(e)
                if "not_found" in msg or "profile_error" in msg:
                    self.after(0, lambda: self._steam_dlg_set_status(
                        t("dlg.steam_login.status_not_found"), kind="danger",
                    ))
                else:
                    self.after(0, lambda m=msg: self._steam_dlg_set_status(
                        t("dlg.steam_login.status_network", err=m), kind="danger",
                    ))
                return

            # Inventory check is best-effort — failure here just means we
            # store None and the dialog won't show the inventory line.
            inv_public = steam_login.check_inventory_public(sid)

            # Persist + refresh widget on the Tk thread.
            self.after(0, lambda: self._steam_apply_profile(profile, inv_public))

        threading.Thread(target=worker, daemon=True).start()

    def _steam_apply_profile(self, profile: dict, inv_public: bool,
                             cookies: dict | None = None) -> None:
        """Write profile into config + refresh widget + dialog status.

        Runs on the Tk main thread (scheduled via after() from the worker).
        Mutates `self.config_data` in place AND rewrites config.json — the
        in-memory copy is the source of truth for the rest of the GUI
        session, the disk copy is what other processes (watch.py) see.

        `cookies` is an optional dict like `{"sessionid": ..., "steamLoginSecure": ...}`
        from the browser-cookies tier. Manual-ID tier passes None (no auth).
        When None, any previously-saved cookies are dropped — switching tiers
        shouldn't leave stale auth lying around.
        """
        steam_cfg = {
            "id":               profile.get("steamid", ""),
            "persona":          profile.get("persona", ""),
            "avatar_url":       profile.get("avatar_url", ""),
            "inventory_public": inv_public,
            "cookies":          cookies,  # null for manual-ID tier
        }
        # load-merge instead of overwriting the whole file — same pattern
        # as _on_close for window geometry. Keeps unrelated config keys
        # (e.g. message_template override) intact.
        try:
            on_disk = load_json(CONFIG_PATH) or {}
        except Exception:
            on_disk = {}
        on_disk["steam"] = steam_cfg
        try:
            save_json(CONFIG_PATH, on_disk)
        except Exception as e:
            log.warning("could not persist steam config: %s", e)
        self.config_data["steam"] = steam_cfg

        # Refresh floating widget + dialog status.
        self._update_user_widget(username=steam_cfg["persona"])
        if steam_cfg["avatar_url"]:
            self._fetch_avatar_async(steam_cfg["avatar_url"])
        # If this save came from the cookies tier we now have a live
        # session — pull the wallet balance straight away so the user
        # sees the visible "yes the login actually worked" payoff
        # without waiting for the next app restart.
        if cookies:
            self._refresh_wallet_balance()
        # If the manual-tier entry exists (login dialog open), mirror the
        # new ID there so users see "yes this is the connected account".
        # Browser tier specifically wants this for the "I know who you are
        # now" affordance even though the user didn't type anything.
        entry = getattr(self, "_steam_dlg_manual_entry", None)
        if entry is not None:
            try:
                entry.delete(0, END)
                entry.insert(0, steam_cfg["id"])
            except tk.TclError:
                pass
        self._steam_dlg_refresh_connected_state()
        # Now that we have a Steam ID, Currency + Country must lock to
        # the account's region — see _update_currency_country_state.
        self._update_currency_country_state()

    def _steam_dlg_disconnect(self) -> None:
        """Clear `config.json.steam`, reset widget to the placeholder.

        Used both from the dialog's Disconnect button and (in future) any
        "session expired" handler that wants to wipe state and prompt for
        a fresh login.
        """
        try:
            on_disk = load_json(CONFIG_PATH) or {}
        except Exception:
            on_disk = {}
        empty = {"id": "", "persona": "", "avatar_url": "",
                 "inventory_public": None, "cookies": None}
        on_disk["steam"] = empty
        try:
            save_json(CONFIG_PATH, on_disk)
        except Exception as e:
            log.warning("could not persist steam disconnect: %s", e)
        self.config_data["steam"] = empty

        # Wipe the floating widget back to the bundled Steam logo + the
        # placeholder "Username" / "0.00 ₴" labels. Balance defaults
        # to the currency symbol from market.currency so the placeholder
        # matches what the widget showed at first paint.
        sym = self._CURRENCY_SYMBOLS.get(
            (self.config_data.get("market") or {}).get("currency", 18), "₴",
        )
        self._update_user_widget(username="Username", balance=f"0.00 {sym}")
        self._draw_placeholder_avatar()
        self._user_avatar_ref = None
        # Disconnect explicitly resolves any lingering "session expired"
        # state — the user has acknowledged it and started over.
        self._set_session_warning(False)

        # Also wipe the dialog's entry + status, so the next manual attempt
        # starts on a clean slate.
        self._steam_dlg_manual_entry.delete(0, END)
        self._steam_dlg_set_status(
            t("dlg.steam_login.status_disconnected"), kind="muted",
        )
        # _refresh_connected_state will hide the Disconnect button now
        # that persona is empty.
        self._steam_dlg_refresh_connected_state()
        # Override the cleared status line so the user sees confirmation
        # of the disconnect (refresh_connected_state would have left it blank).
        self._steam_dlg_set_status(
            t("dlg.steam_login.status_disconnected"), kind="muted",
        )
        # No account → Currency + Country become editable display
        # prefs again.
        self._update_currency_country_state()

    def _steam_dlg_set_status(self, text: str, kind: str = "muted") -> None:
        """Recolour + retext the dialog's status line.

        `kind` maps to a theme palette colour so messages read consistently
        with the rest of the GUI:
          * muted   → secondary  (neutral progress)
          * warning → warning    (user input issue)
          * danger  → danger     (network / not-found)
          * success → success    (handled by refresh_connected_state)
        """
        colours = self.style.colors
        fg = {
            "muted":   colours.secondary,
            "warning": colours.warning,
            "danger":  colours.danger,
            "success": colours.success,
        }.get(kind, colours.secondary)
        try:
            self._steam_dlg_status.configure(text=text, foreground=fg)
        except tk.TclError:
            # Dialog was closed mid-fetch — just drop the update.
            pass

    # ------------------------------------------------------------------
    # Steam login Tier 2 — extract session cookies from a browser.
    #
    # User flow: pick browser → kill it → read cookies → fetch profile →
    # save → relaunch browser. Everything that talks to subprocess or
    # the network runs in a worker thread; only Tk updates are posted
    # back via self.after(0, ...).
    # ------------------------------------------------------------------

    def _open_browser_cookies_dialog(self) -> None:
        """Sub-dialog that drives the Tier 2 (browser cookies) flow.

        Modal child of the login dialog. Detects installed browsers up
        front; if zero → shows a status line and a Close button; if ≥1 →
        radio selector (hidden when only one) + the "close & import"
        button + a live status area.
        """
        import browser_cookies as bc

        # Don't stack two at once — the worker thread state lives on
        # self so reentry would tangle status updates.
        existing = getattr(self, "_steam_browser_dlg", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        installed = bc.detect_installed_browsers()

        # Build the sub-Toplevel. Parent is the login dialog so window
        # management (focus, transient) chains correctly.
        parent = getattr(self, "_steam_login_dlg", None) or self
        dlg = tk.Toplevel(parent)
        self._steam_browser_dlg = dlg
        dlg.title(t("dlg.steam_browser.title"))
        dlg.transient(parent)
        dlg.grab_set()
        dlg.resizable(False, False)

        outer = ttk.Frame(dlg, padding=14)
        outer.pack(fill=BOTH, expand=YES)

        muted_fg = self.style.colors.secondary

        # ---- Empty path: no supported browsers installed --------------
        if not installed:
            ttk.Label(
                outer, text=t("dlg.steam_browser.none_found"),
                foreground=self.style.colors.warning,
                wraplength=420, justify=LEFT,
            ).pack(anchor=W, pady=(0, 10))
            ttk.Button(
                outer, text=t("dlg.steam_browser.cancel_btn"),
                command=lambda: self._close_browser_dialog(),
            ).pack(anchor=E)
            self._steam_browser_specs = []
            self._position_browser_dialog(dlg)
            dlg.protocol("WM_DELETE_WINDOW", self._close_browser_dialog)
            return

        self._steam_browser_specs = installed
        # Pre-select the first installed browser. StringVar holds the
        # `code` so we can look up the spec by code in the worker.
        self._steam_browser_var = tk.StringVar(value=installed[0].code)
        # Track every browser we've force-closed during this dialog
        # session so the close handler can put them all back. Keyed by
        # spec.code; cleared once the spec is actually relaunched.
        self._killed_browsers: dict[str, "browser_cookies.BrowserSpec"] = {}
        # Remembers the last browser that errored out — its radio button
        # gets the "Try again" label until the user switches to a
        # different browser (which resets back to the default).
        self._steam_browser_retry_code: str | None = None

        # ---- Browser selector (only shown when more than one) ---------
        if len(installed) > 1:
            ttk.Label(outer, text=t("dlg.steam_browser.choose")
                      ).pack(anchor=W, pady=(0, 4))
            for spec in installed:
                ttk.Radiobutton(
                    outer, text=spec.display_name,
                    variable=self._steam_browser_var, value=spec.code,
                    command=self._refresh_browser_dialog_labels,
                ).pack(anchor=W, padx=(8, 0))

        # ---- Warning: data loss ---------------------------------------
        self._steam_browser_warn = ttk.Label(
            outer, text="", foreground=self.style.colors.warning,
            wraplength=420, justify=LEFT,
        )
        self._steam_browser_warn.pack(anchor=W, pady=(10, 0))

        # ---- Status line ----------------------------------------------
        self._steam_browser_status = ttk.Label(
            outer, text="", wraplength=420, justify=LEFT,
        )
        self._steam_browser_status.pack(anchor=W, pady=(8, 0))

        # ---- Bottom buttons -------------------------------------------
        btn_row = ttk.Frame(outer)
        btn_row.pack(fill=X, pady=(12, 0))
        ttk.Button(
            btn_row, text=t("dlg.steam_browser.cancel_btn"),
            command=self._close_browser_dialog,
        ).pack(side=LEFT)
        self._steam_browser_action_btn = ttk.Button(
            btn_row, text="",  # filled in by _refresh_browser_dialog_labels
            bootstyle="success",
            command=self._start_browser_cookie_extraction,
        )
        self._steam_browser_action_btn.pack(side=RIGHT)

        # Initial label fill — keeps the {browser} substitution in one
        # place so radio-toggle and first-paint both go through the same
        # path.
        self._refresh_browser_dialog_labels()

        dlg.protocol("WM_DELETE_WINDOW", self._close_browser_dialog)
        self._position_browser_dialog(dlg)

    def _position_browser_dialog(self, dlg: tk.Toplevel) -> None:
        """Centre the sub-dialog over its parent."""
        dlg.update_idletasks()
        parent = dlg.master
        try:
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
        except tk.TclError:
            return
        x = px + (pw - dlg.winfo_reqwidth()) // 2
        y = py + (ph - dlg.winfo_reqheight()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _refresh_browser_dialog_labels(self) -> None:
        """Re-render warning + action-button labels for the chosen browser.

        Called both on first paint, whenever the user clicks a different
        radio button, and after the worker reports a terminal status. Two
        button-label modes:

          * default: "Close {browser} and import" — first attempt or after
                     switching to a browser we haven't tried yet.
          * retry:   "Try again" — the currently selected browser was the
                     last one to error out and the user is presumably going
                     to retry it without closing it again (the browser is
                     already dead — no need to ask "close it" twice).

        Picking a different radio resets retry mode: the new browser is
        still alive, so the "close it first" warning text is honest again.
        """
        spec = self._current_browser_spec()
        if spec is None:
            return
        self._steam_browser_warn.configure(
            text=t("dlg.steam_browser.warn_unsaved", browser=spec.display_name),
        )
        if self._steam_browser_retry_code == spec.code:
            # Same browser as the last error → retry, no close-and-extract.
            self._steam_browser_action_btn.configure(
                text=t("dlg.steam_browser.retry_btn"),
            )
        else:
            # Different browser (or first attempt) → drop any stale retry
            # state and show the standard close+extract label.
            self._steam_browser_retry_code = None
            self._steam_browser_action_btn.configure(
                text=t("dlg.steam_browser.close_and_extract", browser=spec.display_name),
            )

    def _current_browser_spec(self):
        """Return the BrowserSpec matching the currently-selected radio."""
        code = self._steam_browser_var.get()
        for spec in self._steam_browser_specs:
            if spec.code == code:
                return spec
        return None

    def _close_browser_dialog(self) -> None:
        """Drop the sub-dialog cleanly + relaunch any still-closed browsers.

        Anything we force-closed during this dialog session that we
        haven't put back yet (i.e. its entry is still in `_killed_browsers`)
        gets relaunched now. The success path inside the worker already
        pops its own spec after a successful relaunch, so what's left here
        is exactly the set of browsers the user closed via "Try import"
        but never got back — typically failed attempts on browsers they
        didn't end up using.
        """
        import browser_cookies as bc

        dlg = getattr(self, "_steam_browser_dlg", None)
        if dlg is None:
            return
        # Relaunch leftovers BEFORE destroying the window so any failures
        # are at least logged before the dialog state is gone.
        for spec in list(getattr(self, "_killed_browsers", {}).values()):
            try:
                bc.relaunch_browser(spec.exe_path or "")
            except Exception as e:
                log.warning("dialog-close relaunch failed for %s: %s",
                            spec.display_name, e)
        self._killed_browsers = {}
        try:
            dlg.grab_release()
        except tk.TclError:
            pass
        self._steam_browser_dlg = None
        try:
            dlg.destroy()
        except tk.TclError:
            pass

    def _set_browser_status(self, text: str, kind: str = "muted") -> None:
        """Status line update — same palette mapping as the login dialog."""
        colours = self.style.colors
        fg = {
            "muted":   colours.secondary,
            "warning": colours.warning,
            "danger":  colours.danger,
            "success": colours.success,
        }.get(kind, colours.secondary)
        try:
            self._steam_browser_status.configure(text=text, foreground=fg)
        except tk.TclError:
            pass

    def _start_browser_cookie_extraction(self) -> None:
        """Kick off the kill → wait → extract → fetch → relaunch worker.

        Disables the action button while the worker runs so an impatient
        user can't queue up a second pass mid-flight. Re-enabled on every
        terminal status (success, retryable error, or fatal).
        """
        spec = self._current_browser_spec()
        if spec is None:
            return
        self._steam_browser_action_btn.configure(state=DISABLED)

        def worker() -> None:
            self._browser_cookie_pipeline(spec)

        threading.Thread(target=worker, daemon=True).start()

    def _browser_cookie_pipeline(self, spec) -> None:
        """The actual kill→extract→verify→save flow. Runs off the Tk thread.

        Posts every user-visible state change back via self.after(0, ...).
        Re-enables the action button on every exit so a retry is always
        one click away.
        """
        import browser_cookies as bc
        import steam_login as sl

        name = spec.display_name

        def status(text: str, kind: str = "muted") -> None:
            self.after(0, lambda: self._set_browser_status(text, kind))

        def reenable_with_retry() -> None:
            """Re-enable the action button and flip its label to 'Try again'
            for the browser that just failed. Called from every error
            terminal path. Radio-switch handler resets retry state when
            the user picks a different browser.
            """
            def apply() -> None:
                self._steam_browser_retry_code = spec.code
                self._refresh_browser_dialog_labels()
                self._steam_browser_action_btn.configure(state=NORMAL)
            self.after(0, apply)

        def reenable_normal() -> None:
            """Re-enable the action button without the retry-label flip.
            Used after success so dialog auto-close can take over.
            """
            self.after(0, lambda: self._steam_browser_action_btn.configure(state=NORMAL))

        try:
            # 1. Kill any running instance — Chrome holds the cookie DB
            #    locked while any process owns it. taskkill swallows
            #    "process not found" so always-closed browsers are fine.
            status(t("dlg.steam_browser.status_killing", browser=name))
            bc.kill_browser(spec.exe_names)

            # 2. Wait for processes to actually disappear (1-2 sec on
            #    Windows after taskkill returns). If they don't, abort
            #    with a "close it manually" message.
            if not bc.wait_until_gone(spec.exe_names, timeout_sec=5.0):
                status(t("dlg.steam_browser.kill_timeout", browser=name), kind="danger")
                reenable_with_retry()
                return

            # The browser is now confirmed dead — register it as "owed
            # back" so dialog close (Cancel / X / success) can relaunch
            # it. Idempotent: re-trying the same browser is fine.
            self._killed_browsers[spec.code] = spec

            # 3. Extract cookies. Built-in retries (3×1s) absorb the file-
            #    handle-release latency that lingers after the last
            #    process exits.
            #
            # On failure we deliberately leave the browser closed — the
            # user is going to want to retry, and another extraction
            # attempt against a freshly-relaunched browser would hit the
            # exact same locked-DB problem. They can launch it manually
            # if they need to fix something (log in to Steam, etc.).
            status(t("dlg.steam_browser.status_extracting"))
            cookies = None
            try:
                cookies = bc.extract_steam_cookies(spec)
            except bc.BrowserCookiesError as e:
                msg = str(e)
                log.warning("cookie extract failed for %s: %s", name, msg)
                if msg.startswith("needs_admin"):
                    # Modern Chrome/Edge/Opera ABE — only way through is
                    # an elevated helper process. Show a short heads-up
                    # before the UAC prompt steals focus so the pop-up
                    # doesn't look like it came out of nowhere; 0.4 sec
                    # is enough to register without feeling laggy.
                    status(t("dlg.steam_browser.needs_admin"), kind="warning")
                    time.sleep(0.4)
                    status(t("dlg.steam_browser.status_admin"))
                    try:
                        cookies = bc.extract_steam_cookies_admin(
                            spec,
                            python_exe=str(Path(sys.executable).parent / "pythonw.exe"),
                            helper_script=str(BASE / "cookie_extract_helper.py"),
                        )
                    except bc.BrowserCookiesError as e2:
                        msg2 = str(e2)
                        if msg2 == "admin_denied":
                            status(t("dlg.steam_browser.admin_denied", browser=name),
                                   kind="warning")
                        elif msg2 == "admin_timeout":
                            status(t("dlg.steam_browser.admin_timeout"),
                                   kind="danger")
                        elif msg2 == "not_elevated":
                            # Helper ran but Windows didn't elevate it —
                            # user needs to relaunch the GUI as admin.
                            status(t("dlg.steam_browser.not_elevated"),
                                   kind="danger")
                        elif "decrypt_encrypted_value failed" in msg2:
                            # rookiepy got past the admin check and the
                            # key-derivation step but the actual cookie
                            # decryption failed — happens on Chrome v131+
                            # where ABE v2 added an extra binding layer
                            # that no third-party library knows how to
                            # reverse. Be honest with the user: this path
                            # is a dead end until rookiepy (or Google)
                            # changes something. Steer them to Opera /
                            # manual ID.
                            status(t("dlg.steam_browser.abe_blocked", browser=name),
                                   kind="danger")
                        elif msg2 == "no_session":
                            status(t("dlg.steam_browser.no_session", browser=name),
                                   kind="warning")
                        else:
                            # Helper-relayed error — keep it compact for the
                            # status line; full traceback already in watch.log.
                            short = msg2.splitlines()[0] if msg2 else "unknown"
                            status(t("dlg.steam_browser.network", err=short),
                                   kind="danger")
                        reenable_with_retry()
                        return
                elif msg.startswith("db_locked"):
                    cause = msg.split(":", 1)[1].strip() if ":" in msg else ""
                    line = t("dlg.steam_browser.db_locked", browser=name)
                    if cause:
                        line += f"\n[{cause}]"
                    status(line, kind="danger")
                    reenable_with_retry()
                    return
                elif msg == "no_session":
                    status(t("dlg.steam_browser.no_session", browser=name), kind="warning")
                    reenable_with_retry()
                    return
                else:
                    status(t("dlg.steam_browser.network", err=msg), kind="danger")
                    reenable_with_retry()
                    return

            # 4. Confirm the cookies actually authenticate. They might
            #    have been freshly logged out (cookie present but token
            #    expired server-side) — Steam doesn't always clear the
            #    cookie when the session dies.
            status(t("dlg.steam_browser.status_verifying"))
            if not bc.verify_session(cookies):
                status(t("dlg.steam_browser.session_invalid"), kind="warning")
                reenable_with_retry()
                return

            # 5. Pull steamID64 out of the cookie value, then run the same
            #    public-profile fetch as the manual-ID tier — same widget
            #    payload either way (persona / avatar / inventory check).
            try:
                # cookies is now {community: {...}, store: {...}}; the
                # steamID is embedded in either side's steamLoginSecure
                # (same JWT value cross-domain). Prefer the community
                # one because it's the more reliably-present of the two
                # — store cookies are missing for users who logged in
                # via community only.
                community = cookies.get("steamcommunity.com") or {}
                store = cookies.get("store.steampowered.com") or {}
                login_token = (community.get("steamLoginSecure")
                               or store.get("steamLoginSecure") or "")
                sid = bc.parse_steamid_from_cookie(login_token)
            except bc.BrowserCookiesError:
                status(t("dlg.steam_browser.bad_cookie"), kind="danger")
                reenable_with_retry()
                return

            status(t("dlg.steam_browser.status_loading"))
            try:
                profile = sl.fetch_public_profile(sid)
            except sl.SteamLoginError as e:
                status(t("dlg.steam_browser.network", err=str(e)), kind="danger")
                reenable_with_retry()
                return
            # Inventory check is best-effort — same as manual tier.
            inv_public = sl.check_inventory_public(sid)

            # 6. Persist (with cookies this time) + refresh widget +
            #    auto-fill Tier 3 entry — handled inside _steam_apply_profile.
            self.after(0, lambda p=profile, i=inv_public, c=cookies:
                       self._steam_apply_profile(p, i, cookies=c))

            # 7. Relaunch — separate concern from "save was OK", so we
            #    report success either way but vary the trailing line.
            #    On successful relaunch we pop the spec from _killed_browsers
            #    so the dialog-close handler doesn't try to relaunch it a
            #    second time (which would either open a duplicate window
            #    or fail noisily).
            status(t("dlg.steam_browser.status_relaunching", browser=name))
            relaunched = bc.relaunch_browser(spec.exe_path or "")
            persona = profile.get("persona") or ""
            if relaunched:
                self.after(0, lambda: self._killed_browsers.pop(spec.code, None))
                # Plain success — single line is plenty.
                status(t("dlg.steam_browser.success", persona=persona), kind="success")
            else:
                # Save succeeded but we couldn't put the browser back —
                # the user still needs the win-state, just with a manual
                # follow-up nudge.
                status(
                    t("dlg.steam_browser.success", persona=persona) + " " +
                    t("dlg.steam_browser.relaunch_failed", browser=name),
                    kind="warning",
                )
            # Auto-close after a short delay so the user reads the
            # success message but isn't stuck having to dismiss the
            # sub-dialog manually. Any other browsers still in
            # _killed_browsers (failed attempts on different browsers)
            # get relaunched by _close_browser_dialog.
            reenable_normal()
            self.after(2000, self._close_browser_dialog)
        except Exception as e:
            # Any unforeseen error path — log it for postmortem, surface
            # a generic-network message to the user, and re-enable the
            # button so they can retry.
            log.exception("browser cookie pipeline failed")
            status(t("dlg.steam_browser.network", err=str(e)), kind="danger")
            reenable_with_retry()

    # ------------------------------------------------------------------
    # Active-kind helpers — used by action callbacks to figure out which
    # file / tree to operate on, based on the currently-focused tab.
    # ------------------------------------------------------------------

    # Tab index → kind. Only the first two tabs are card lists; the rest
    # return None.
    _TAB_KIND = {0: "buy", 1: "sell"}

    def _active_kind(self) -> str | None:
        try:
            idx = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return None
        return self._TAB_KIND.get(idx)

    def _active_tree(self) -> ttk.Treeview | None:
        kind = self._active_kind()
        return self.list_trees.get(kind) if kind else None

    @staticmethod
    def _kind_path(kind: str) -> Path:
        return LIST_PATHS[kind]

    # Steam Market currency code → display symbol. Now sourced from
    # `regions.CURRENCY_SYMBOLS` so the Settings currency dropdown, the
    # floating-widget balance placeholder, and the History totals stay
    # in sync — a single source of truth covers all 39 Steam currencies
    # instead of the legacy four.
    @property
    def _CURRENCY_SYMBOLS(self) -> dict:
        from regions import CURRENCY_SYMBOLS
        return CURRENCY_SYMBOLS

    def _currency_symbol(self) -> str:
        from regions import currency_symbol
        code = self.config_data.get("market", {}).get("currency", 18)
        return currency_symbol(code, fallback="")

    # ------------------------------------------------------------------
    # Native Windows title-bar tinting via DWM
    # ------------------------------------------------------------------

    def _apply_native_titlebar_theme(self) -> None:
        """Tint the OS title bar to match the current theme background.

        Windows 10 (build 18985+) accepts DWMWA_USE_IMMERSIVE_DARK_MODE
        and gives us a dark title bar; Windows 11 additionally accepts
        DWMWA_CAPTION_COLOR / DWMWA_TEXT_COLOR for exact colour control.
        Anything older — or any non-Windows host — silently no-ops.
        """
        import sys
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import byref, sizeof, c_int, c_uint32
        except ImportError:
            return

        self.update_idletasks()
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
        except Exception:
            return
        if not hwnd:
            return

        s = self.style
        bg = s.colors.bg
        fg = s.colors.fg
        is_dark = _is_dark(bg)

        dwmapi = ctypes.windll.dwmapi

        # 1) Dark/light title bar (Win10 1909+ and Win11).
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        try:
            dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                byref(c_int(1 if is_dark else 0)), sizeof(c_int),
            )
        except Exception:
            pass
        # Older Windows 10 used attribute 19 for the same flag — try it too.
        try:
            dwmapi.DwmSetWindowAttribute(
                hwnd, 19,
                byref(c_int(1 if is_dark else 0)), sizeof(c_int),
            )
        except Exception:
            pass

        # 2) Exact caption + text colours (Windows 11 only). COLORREF is a
        # 0x00BBGGRR uint — note the swapped channel order vs HTML.
        def hex_to_colorref(hex_str: str) -> int | None:
            h = hex_str.lstrip("#").lstrip("$")
            if len(h) != 6:
                return None
            try:
                r = int(h[0:2], 16)
                g = int(h[2:4], 16)
                b = int(h[4:6], 16)
            except ValueError:
                return None
            return (b << 16) | (g << 8) | r

        DWMWA_CAPTION_COLOR = 35
        DWMWA_TEXT_COLOR = 36

        bg_ref = hex_to_colorref(bg)
        fg_ref = hex_to_colorref(fg)
        if bg_ref is not None:
            try:
                dwmapi.DwmSetWindowAttribute(
                    hwnd, DWMWA_CAPTION_COLOR,
                    byref(c_uint32(bg_ref)), sizeof(c_uint32),
                )
            except Exception:
                pass
        if fg_ref is not None:
            try:
                dwmapi.DwmSetWindowAttribute(
                    hwnd, DWMWA_TEXT_COLOR,
                    byref(c_uint32(fg_ref)), sizeof(c_uint32),
                )
            except Exception:
                pass

        # On Windows 10 the dark-mode attribute is "armed" by DwmSet… but
        # the non-client area isn't actually repainted until something
        # triggers a real frame recalc. SWP_FRAMECHANGED on a no-op
        # SetWindowPos *should* do it according to docs, but in practice
        # multiple Win10 builds ignore it. The bulletproof fallback is a
        # 1-pixel resize and snap back — flicker is one frame and the
        # title bar always redraws.
        try:
            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            rect = RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 1 and h > 1:
                SWP_NOMOVE = 0x0002
                SWP_NOZORDER = 0x0004
                SWP_FRAMECHANGED = 0x0020
                flags = SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED
                ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, w + 1, h, flags)
                ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, w, h, flags)
        except Exception:
            pass

    def _ask_operation_kind(self, name: str) -> str | None:
        """Modal "Придбати / Продати" picker.

        Returns "buy", "sell", or None if the user closed the dialog
        without choosing. Modal via grab_set so the main window can't
        receive events until the dialog is dismissed.
        """
        dlg = tk.Toplevel(self)
        dlg.title(t("dlg.choose_op.title"))
        dlg.transient(self)
        dlg.resizable(False, False)
        # Default close (×) is a "cancel".
        result: dict = {"value": None}
        dlg.protocol("WM_DELETE_WINDOW", lambda: dlg.destroy())

        ttk.Label(
            dlg, text=t("dlg.choose_op.body", name=name), wraplength=360,
        ).pack(padx=20, pady=(20, 12))

        btn_row = ttk.Frame(dlg)
        btn_row.pack(padx=20, pady=(0, 20))

        def choose(kind: str) -> None:
            result["value"] = kind
            dlg.destroy()

        # Cards only ever go to Покупка / Продаж. Game records never
        # reach this dialog — _hist_readd routes them straight back to
        # the «Ігри» list (a game can't become a market card).
        ttk.Button(btn_row, text=t("dlg.choose_op.buy"),
                   command=lambda: choose("buy"),
                   bootstyle="success").pack(side=LEFT, padx=6)
        ttk.Button(btn_row, text=t("dlg.choose_op.sell"),
                   command=lambda: choose("sell"),
                   bootstyle="info").pack(side=LEFT, padx=6)

        # Centre on the main window before grabbing focus.
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        dlg.grab_set()
        dlg.focus_set()
        dlg.wait_window()
        return result["value"]

    # ---- Card lists (Придбання / Продаж) --------------------------------

    def _build_card_list_tab(self, parent: ttk.Frame, kind: str) -> None:
        """Build a card-list tab — same UI for buy and sell, separate trees.

        The action ("Купив" / "Продав") button text differs by kind; the
        rest — columns, click behaviour, action callbacks — is identical
        and dispatches on the currently-active tab.
        """
        # "type" sits between "name" and "game" — Steam's market tag bakes
        # both into one string and splitting them out as separate columns
        # gives users a cleaner read for cards from games whose titles are
        # easily confused with item types (e.g. several "METAL SLUG …"
        # entries all ending in "Foil Trading Card").
        # "imported" is a single-glyph column at the end — a 📥 marker for
        # rows that came from the Steam-import flow, blank otherwise. Lets
        # the user distinguish manually-added cards from synced ones at a
        # glance.
        cols = ("num", "name", "type", "game",
                "target", "last", "spread", "status",
                "link", "imported", "no_check", "no_alert")
        headings = [
            ("num",      t("col.num"),       40),
            ("name",     t("col.name"),     200),
            ("type",     t("col.type"),     110),
            ("game",     t("col.game"),     150),
            ("target",   t("col.target"),    80),
            ("last",     t("col.last"),      85),
            ("spread",   t("col.spread"),   100),
            ("status",   t("col.status"),   130),
            ("link",     t("col.link"),     110),
            ("imported", t("col.imported"),  60),
            ("no_check", t("col.no_check"),  40),
            ("no_alert", t("col.no_alert"),  40),
        ]

        btn_frame = ttk.Frame(parent)
        btn_frame.pack(side=BOTTOM, fill=X, padx=8, pady=(0, 8))

        tree_frame = ttk.Frame(parent, borderwidth=1, relief="solid")
        tree_frame.pack(side=TOP, fill=BOTH, expand=YES, padx=8, pady=8)

        # selectmode="extended" enables Ctrl+click (toggle one) and
        # Shift+click (range) out of the box. Ctrl+A we wire up by hand
        # below — Treeview doesn't ship a default <<SelectAll>>.
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="extended")
        for col, text, width in headings:
            if col in ("target", "last", "spread"):
                anchor = E
            elif col in ("num", "link", "imported", "no_check", "no_alert"):
                anchor = CENTER
            else:
                anchor = W
            tree.heading(col, text=text, anchor=anchor)
            tree.column(col, width=width, anchor=anchor)
        self._apply_row_tags(tree)
        self._setup_sortable_columns(tree, list(cols))
        # Click + motion handlers dispatch by event.widget — same code path
        # for both kinds.
        tree.bind("<Button-1>", self._on_card_tree_click)
        tree.bind("<Motion>",   self._on_card_tree_motion)
        tree.bind("<<TreeviewSelect>>", self._on_card_tree_select)
        # Ctrl+A → select all rows. event.keycode is layout-independent
        # (A=65), so this works on Cyrillic keyboard too — same trick as
        # _install_clipboard_shortcuts.
        tree.bind("<Control-KeyPress>", self._on_tree_ctrl_a)
        # Right-click → context menu (each action delegates back to the
        # same handler the toolbar buttons use, so all the
        # enable/disable rules ride along for free — see
        # `_show_card_context_menu`).
        tree.bind("<Button-3>",
                  lambda e, k=kind: self._show_card_context_menu(e, k))

        # bootstyle="success" gives ttkbootstrap's image-rendered thumb the
        # `s.colors.success` tint — which we re-aliased to the active-tab
        # accent (Steam-green for claude). Plain `style.configure` on
        # Vertical.TScrollbar doesn't reach the thumb because ttkbootstrap
        # paints it from PhotoImage assets, not ttk colours.
        vsb = ttk.Scrollbar(tree_frame, orient=VERTICAL, command=tree.yview,
                            bootstyle="success")
        tree.configure(yscrollcommand=lambda f, l, sb=vsb: self._autohide_scrollbar(sb, f, l))
        # Grid layout — auto-hide via grid_remove() preserves the column/row
        # slot so the scrollbar can pop back in the same place when needed.
        # weight=1 on tree-column lets the table expand into the freed space
        # while the scrollbar is hidden.
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        self.list_trees[kind] = tree

        row1 = ttk.Frame(btn_frame)
        row1.pack(fill=X, pady=(0, 4))
        # Button order in row1: Add → Edit target → Move-to-other-list →
        # Check now → Remove. The "Open in browser" action used to live
        # here too, but it was redundant — every row already has a clickable
        # link in the "Посилання" column. The move button's text is
        # kind-dependent ("→ До продажу" on buy tab, "→ До покупки" on
        # sell tab); we keep a reference to it so _update_action_buttons
        # can enable/disable it together with the other selection-dependent
        # actions.
        move_key = "btn.move_to_sell" if kind == "buy" else "btn.move_to_buy"
        # The duplicate action is sell-list only: salelist allows multiple
        # entries per market_hash_name (you might be selling several
        # copies of the same card at different prices). The buy list
        # dedupes by mhn, so duplicating there would have nothing useful
        # to do — the second copy would just collide on insert.
        btn_specs = [
            ("btn.add_by_url",      self._add_by_url),
            ("btn.edit_target",     self._edit_target),
        ]
        if kind == "sell":
            btn_specs.append(("btn.duplicate", self._duplicate_card))
        btn_specs += [
            (move_key,              self._move_to_other_list),
            ("btn.check_now",       self._check_now),
            ("btn.remove",          self._remove_card),
        ]
        btn_move = None
        btn_check = None
        btn_remove = None
        btn_duplicate = None
        for key, cmd in btn_specs:
            btn = ttk.Button(row1, text=t(key), command=cmd)
            btn.pack(side=LEFT, padx=2)
            if key == move_key:
                btn_move = btn
            elif key == "btn.check_now":
                btn_check = btn
            elif key == "btn.remove":
                # Start disabled — "Видалити" only makes sense when
                # something's actually selected; greying it out also
                # prevents an accidental delete on an empty table.
                # `_update_action_buttons` flips state + bootstyle when
                # the selection becomes non-empty.
                btn.configure(state=DISABLED)
                btn_remove = btn
            elif key == "btn.duplicate":
                # Same disabled-by-default story as "Видалити" — needs
                # a selection to do anything useful.
                btn.configure(state=DISABLED)
                btn_duplicate = btn

        row2 = ttk.Frame(btn_frame)
        row2.pack(fill=X)
        # btn.bought for buy, btn.sold for sell — confirms the transaction
        # and shoves the card into purchases.json with the right operation.
        action_key = "btn.bought" if kind == "buy" else "btn.sold"
        btn_completed = ttk.Button(row2, text=t(action_key),
                                   command=self._mark_completed,
                                   bootstyle="success")
        btn_completed.pack(side=LEFT, padx=2)
        btn_not = ttk.Button(row2, text=t("btn.not_bought"),
                             command=self._mark_not_bought,
                             bootstyle="warning")
        btn_not.pack(side=LEFT, padx=2)
        # Import-from-Steam placeholder — yellow/gold to match the other
        # call-to-action buttons here. Action is wired up in Phase 3 (TODO),
        # where it will pull the user's current Steam Market listings /
        # buy orders and offer to sync them into watchlist/salelist.json.
        # For now it just announces "in development" so the slot is visible.
        btn_import = ttk.Button(row2, text=t("btn.import"),
                                command=self._import_from_steam,
                                bootstyle="warning")
        btn_import.pack(side=LEFT, padx=2)
        # Polling/alert mute toggles — flag-flippers on the selection,
        # same handlers the context menu uses (see _toggle_flag).
        ttk.Button(row2, text=t("btn.no_check"),
                   command=lambda k=kind: self._toggle_flag(k, "no_check")
                   ).pack(side=LEFT, padx=2)
        ttk.Button(row2, text=t("btn.no_alert"),
                   command=lambda k=kind: self._toggle_flag(k, "no_alert")
                   ).pack(side=LEFT, padx=2)
        self.list_action_buttons[kind] = {
            "completed": btn_completed,
            "not": btn_not,
            "move": btn_move,
            "check": btn_check,
            "import": btn_import,
            "remove": btn_remove,
            "duplicate": btn_duplicate,
        }
        self._update_action_buttons()

    def _refresh_watchlist(self):
        """Refresh both card-list tabs from disk.

        Kept as the entry point for older call sites; under the hood it
        defers to _refresh_card_list per kind.
        """
        self._refresh_card_list("buy")
        self._refresh_card_list("sell")

    @staticmethod
    def _ensure_ids(records: list) -> bool:
        """Backfill `id` (uuid4 str) on legacy records that don't have one.

        Returns True if at least one record was modified, so callers can
        decide whether to write the file back. Used at every refresh — first
        time fills everything, subsequent calls are no-ops.

        We need stable per-record ids because salelist now allows duplicates
        (same card listed multiple times if I'm selling multiple copies) —
        market_hash_name is no longer unique, and the Treeview iid + state
        cleanup logic both depend on a stable, unique key.
        """
        dirty = False
        for r in records:
            if not r.get("id"):
                r["id"] = str(uuid.uuid4())
                dirty = True
            # One-off migration for the early-import bug (fixed): records
            # imported from Steam used to store `appid` = the *game's*
            # appid (e.g. 238960 for Path of Exile) when the mhn already
            # carried the prefix ("238960-The Sceptre of God"). The right
            # value for community items is 753 — the URL appid — because
            # that's the (appid, mhn) pair Steam's orderbook /
            # priceoverview want. Heuristic: if `appid` isn't 753 *and*
            # `market_hash_name` looks like "<digits>-<name>" *and* the
            # prefix matches the stored appid, this is the broken case.
            # Quietly fix it so refresh starts working again.
            mhn = (r.get("market_hash_name") or r.get("name") or "")
            appid = r.get("appid")
            if (isinstance(appid, int) and appid != 753
                    and "-" in mhn):
                prefix = mhn.split("-", 1)[0]
                if prefix.isdigit() and int(prefix) == appid:
                    r["appid"] = 753
                    # Also migrate the matching antispam-state key on
                    # disk. State keys are `{kind}:{appid}:{name}` —
                    # changing appid without renaming the key orphans
                    # the entry, which leaves stale `status="alerted"`
                    # badges stuck because evaluate_and_alert can't
                    # find the entry to clear it.
                    App._rename_state_appid_key(
                        old_appid=appid, new_appid=753,
                        name=r.get("name") or mhn,
                    )
                    dirty = True
        return dirty

    @staticmethod
    def _rename_state_appid_key(*, old_appid: int, new_appid: int,
                                name: str) -> None:
        """Move antispam-state entries when a card's appid changes.

        Best-effort: silently skips if state.json is missing or
        malformed. Run from `_ensure_ids` so any record whose appid we
        migrate also drags its `state.json` entry along (otherwise
        evaluate_and_alert can't find the antispam slot under the new
        key and a stale "alerted" badge gets stuck forever).
        """
        try:
            state = load_json(STATE_PATH, {}) or {}
        except Exception:
            return
        dirty = False
        for kind in ("buy", "sell"):
            old_key = f"{kind}:{old_appid}:{name}"
            new_key = f"{kind}:{new_appid}:{name}"
            if old_key in state and new_key not in state:
                state[new_key] = state.pop(old_key)
                dirty = True
        if dirty:
            try:
                save_json(STATE_PATH, state)
            except Exception:
                pass

    def _refresh_card_list(self, kind: str) -> None:
        from steam import pretty_name

        tree = self.list_trees.get(kind)
        if tree is None:
            return
        path = self._kind_path(kind)
        tree.delete(*tree.get_children())
        items = load_json(path, []) or []
        list_dirty = False
        if self._ensure_ids(items):
            list_dirty = True
        # Synchronous self-heal: clear stale "alerted" badges where the
        # current last_seen no longer meets the alert rule. Same condition
        # as evaluate_and_alert (buy: lowest <= target → alert; sell:
        # lowest < target). Catches rows whose price climbed back to or
        # past target between price polls — the next watch.py run would
        # also fix them, but doing it on every refresh keeps the GUI
        # honest immediately after import / target edits / etc.
        for w in items:
            if w.get("status") != "alerted":
                continue
            target = w.get("target_price")
            last_seen_num = _try_parse_money(w.get("last_seen"))
            if not isinstance(target, (int, float)) or last_seen_num is None:
                continue
            still_qualifies = (last_seen_num <= target if kind == "buy"
                               else last_seen_num < target)
            if not still_qualifies:
                w["status"] = ""
                list_dirty = True
        if list_dirty:
            save_json(path, items)
        row_index = 0
        for item in items:
            if item.get("status") in CLOSED_STATUSES:
                continue
            raw_status = item.get("status", "")
            status = t(f"status.value.{raw_status}") if raw_status else ""
            if status == f"status.value.{raw_status}":  # i18n miss
                status = raw_status
            last = item.get("last_seen", "—")
            target = item.get("target_price", "")
            target_str = f"{target:.2f}" if isinstance(target, (int, float)) else str(target)

            # Spread = last_seen − target. "+" means above target (waiting),
            # "−" means below or equal (alert-worthy).
            spread_str = "—"
            last_num = _try_parse_money(last)
            target_num = target if isinstance(target, (int, float)) else None
            if last_num is not None and target_num is not None:
                diff = last_num - target_num
                spread_str = f"{diff:+.2f}"

            # display_name and game_name are normally written by the metadata
            # fetcher (on add or in the background refresh). For rows that
            # haven't been resolved yet pretty_name falls back to the cleaned
            # hash name; game_name has no such fallback so we show "—".
            display_name = pretty_name(item)
            game_name = item.get("game_name") or "—"

            # Pick row tag. Priority order:
            #   1. rate_limited / error — Steam-side problems, gold/red
            #      from the status, not from a price comparison.
            #   2. price vs target — green when the deal direction is
            #      met, red when it isn't. Rules differ by list kind:
            #         buy  → green when lowest ≤ target  (can buy now)
            #         sell → green when lowest ≥ target  (price still
            #                holds; flip red when undercut).
            #      `alerted` is NO LONGER a row tint — the status
            #      column already tells the user, and a green row that
            #      meets the threshold reads the same intent. This
            #      keeps the table calmer (no double-encoding) and
            #      makes "I undershot my sell price" visually obvious.
            #   3. zebra fallback when last_seen / target are missing.
            # Branching on raw status (not the localised string) so
            # row colours don't shift when the user changes language.
            zebra = "even" if row_index % 2 == 0 else "odd"
            if raw_status == "rate_limited":
                row_tag = "rate_limited"
            elif raw_status == "error":
                row_tag = "error"
            elif last_num is not None and target_num is not None:
                if kind == "buy":
                    meets = last_num <= target_num
                else:  # sell
                    meets = last_num >= target_num
                row_tag = "good_match" if meets else "bad_match"
            else:
                row_tag = zebra

            # `item_type` is filled in by the Steam import path; older
            # records added manually before the type column existed will
            # show an empty cell here until something refills the field.
            # `imported` is the 📥 glyph for rows whose origin was the
            # Steam-import sync — handy when a list mixes manually
            # added cards with synced ones.
            item_type = item.get("item_type") or ""
            imported_glyph = "📥" if item.get("imported") else ""
            # Presentation-only «пропущено» for no_check rows — keeps
            # the stored status intact for when the flag comes off.
            if item.get("no_check"):
                status = t("status.value.skipped")
            tree.insert(
                "", END,
                # iid = record's uuid (uniquely identifies even duplicate
                # mhn entries in salelist).
                iid=item["id"],
                values=(
                    row_index + 1,
                    display_name, item_type, game_name,
                    target_str, last, spread_str, status,
                    t("col.link.open"),
                    imported_glyph,
                    "🚫" if item.get("no_check") else "",
                    "🔇" if item.get("no_alert") else "",
                ),
                tags=(row_tag,),
            )
            row_index += 1
        # Refresh wiped the tags — restore the "selected" marker so the
        # current selection stays visible.
        self._mark_selected_rows(tree)
        # Re-apply any persisted sort order so a restart (or a
        # «Запустити зараз» followed by refresh) keeps the column the
        # user picked last time, instead of falling back to file order.
        self._restore_sort_state(tree)
        self._update_action_buttons()
        self._update_statusbar()

    def _on_card_tree_select(self, event=None):
        """Highlight the selected row + sync action button state."""
        # Tree dispatched by event.widget so the same handler serves both
        # purchase and sales trees.
        tree = event.widget if event is not None else self._active_tree()
        if tree is not None:
            self._mark_selected_rows(tree)
        self._update_action_buttons()

    @staticmethod
    def _mark_selected_rows(tree: ttk.Treeview) -> None:
        selection = set(tree.selection())
        for iid in tree.get_children():
            tags = [tag for tag in tree.item(iid, "tags") if tag != "selected"]
            if iid in selection:
                tags.append("selected")
            tree.item(iid, tags=tuple(tags))

    def _update_action_buttons(self):
        """Sync Придбав|Продав + Ще ні enabled state with current selection.

        Придбав/Продав is always available whenever a row is selected — the
        user might've bought/sold a card outside our app entirely.

        "Ще ні, стежити далі" only makes sense for rows that already got an
        alert (status="alerted"); otherwise there's nothing to dismiss.
        """
        for kind, tree in self.list_trees.items():
            buttons = self.list_action_buttons.get(kind, {})
            sel = tree.selection() if tree is not None else ()
            sel_items: list[dict] = []
            if sel:
                items = load_json(self._kind_path(kind), []) or []
                by_id = {x.get("id"): x for x in items if x.get("id")}
                sel_items = [by_id[iid] for iid in sel if iid in by_id]
            # Придбав/Продав and Move enable when ≥ 1 row selected — they
            # all fan out cleanly over a multi-selection.
            completed_state = NORMAL if sel_items else DISABLED
            move_state = NORMAL if sel_items else DISABLED
            # "Ще ні" only makes sense for alerted rows. Allow it whenever
            # the selection contains at least one alerted card (it'll
            # silently skip non-alerted ones).
            has_alerted = any(x.get("status") == "alerted" for x in sel_items)
            not_state = NORMAL if has_alerted else DISABLED
            # "Оновити зараз" — enabled whenever at least one row is selected.
            # The old hard-cap of 3 cards was a safety against the priceoverview
            # IP ban; we've since switched to the orderbook endpoint which
            # took 300+ requests in 20 min without a single 429, so the cap
            # no longer earns its keep. If we ever switch back to priceoverview,
            # cap restoration is one if-clause.
            check_state = NORMAL if sel_items else DISABLED
            if "completed" in buttons:
                buttons["completed"].configure(state=completed_state)
            if "not" in buttons:
                buttons["not"].configure(state=not_state)
            if "move" in buttons:
                buttons["move"].configure(state=move_state)
            if "check" in buttons and buttons["check"] is not None:
                buttons["check"].configure(state=check_state)
            # "Видалити" — only enabled when something's selected, and
            # flips to danger-red as a visual "this is destructive" cue
            # the moment the click would do real work. Default style
            # (empty bootstyle) when disabled so a greyed-out button
            # doesn't look like it's begging to be clicked.
            if buttons.get("remove") is not None:
                if sel_items:
                    buttons["remove"].configure(
                        state=NORMAL, bootstyle="danger",
                    )
                else:
                    buttons["remove"].configure(
                        state=DISABLED, bootstyle="",
                    )
            # "Здублювати" — sell-tab-only, same enable-when-selected
            # contract as Видалити. No special colour: it's an additive
            # action, not destructive.
            if buttons.get("duplicate") is not None:
                buttons["duplicate"].configure(
                    state=NORMAL if sel_items else DISABLED,
                )

    def _update_hist_delete_state(self) -> None:
        """Mirror card-list «Видалити» contract on the History tab.

        Disabled + plain-style when nothing is selected; enabled with
        the `danger` bootstyle the moment the click would do real work.
        Bound to `hist_tree.<<TreeviewSelect>>` so the state tracks
        whatever the user has highlighted, including multi-select.
        """
        btn = getattr(self, "btn_hist_delete", None)
        tree = getattr(self, "hist_tree", None)
        if btn is None or tree is None:
            return
        try:
            has_sel = bool(tree.selection())
        except tk.TclError:
            return
        if has_sel:
            btn.configure(state=NORMAL, bootstyle="danger")
        else:
            btn.configure(state=DISABLED, bootstyle="")

    # ------------------------------------------------------------------
    # «Ігри» tab — wishlist game price tracking (bonus content)
    # ------------------------------------------------------------------
    #
    # A third tracked list, but for store games instead of market cards:
    #   * rows come from the user's Steam wishlist (one-click import);
    #   * "Мінімум" is the historical-low price reconstructed from the
    #     deepest recorded discount (Augmented Steam / ITAD data — the
    #     same number SteamDB shows as "Lowest Recorded Price") applied
    #     to Steam's regular price in the user's currency;
    #   * alert rule: current price <= minimum → the discount matched
    #     (or beat) the all-time low. kind="game" in the shared
    #     antispam state.
    # Lives behind the «Бонусний контент» Settings checkbox: hiding the
    # tab stops polling/alerts but keeps gamelist.json intact.

    _GAMES_LINK_COL_ID = "#8"   # «Посилання» column in the games tree

    def _build_games_tab(self) -> None:
        parent = self.tab_games
        cols = ("num", "name", "regular", "discount", "price", "minimum",
                "status", "link", "imported", "no_check", "no_alert")
        headings = [
            ("num",      "col.num",            40),
            ("name",     "col.games.name",    280),
            ("regular",  "col.games.regular", 90),
            ("discount", "col.games.discount", 80),
            ("price",    "col.games.price",   100),
            ("minimum",  "col.games.minimum", 100),
            ("status",   "col.status",        110),
            ("link",     "col.link",          110),
            ("imported", "col.imported",       55),
            ("no_check", "col.no_check",       40),
            ("no_alert", "col.no_alert",       40),
        ]

        btn_frame = ttk.Frame(parent)
        btn_frame.pack(side=BOTTOM, fill=X, padx=8, pady=(0, 8))
        tree_frame = ttk.Frame(parent, borderwidth=1, relief="solid")
        tree_frame.pack(side=TOP, fill=BOTH, expand=YES, padx=8, pady=8)

        tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                            selectmode="extended")
        for col, key, width in headings:
            if col in ("price", "minimum", "regular"):
                anchor = E
            elif col in ("num", "discount", "link", "imported",
                         "no_check", "no_alert"):
                anchor = CENTER
            else:
                anchor = W
            tree.heading(col, text=t(key), anchor=anchor)
            tree.column(col, width=width, anchor=anchor)
        self._apply_row_tags(tree)
        self._setup_sortable_columns(tree, list(cols))
        self.games_tree = tree

        tree.bind("<<TreeviewSelect>>", self._on_games_select)
        tree.bind("<Button-1>", self._on_games_click)
        tree.bind("<Motion>", self._on_games_motion)
        tree.bind("<Control-KeyPress>", self._on_tree_ctrl_a)
        tree.bind("<Button-3>", self._show_games_context_menu)

        vsb = ttk.Scrollbar(tree_frame, orient=VERTICAL, command=tree.yview,
                            bootstyle="success")
        tree.configure(yscrollcommand=lambda f, l, sb=vsb:
                       self._autohide_scrollbar(sb, f, l))
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        # Row 1: per-row actions. Row 2: bulk imports + the mute/skip
        # toggles — mirrors the card tabs' "actions on top, imports
        # below" split and keeps row 1 short enough for narrow windows.
        row1 = ttk.Frame(btn_frame)
        row1.pack(fill=X, pady=(0, 4))
        ttk.Button(row1, text=t("btn.add_by_url"),
                   command=self._games_add_by_url).pack(side=LEFT, padx=2)
        ttk.Button(row1, text=t("btn.games_import"),
                   command=self._games_import_wishlist,
                   bootstyle="warning").pack(side=LEFT, padx=2)
        ttk.Button(row1, text=t("btn.games_import_lows"),
                   command=self._games_import_lows,
                   bootstyle="warning").pack(side=LEFT, padx=2)
        ttk.Button(row1, text=t("btn.check_now"),
                   command=self._games_check_now).pack(side=LEFT, padx=2)
        self.btn_games_delete = ttk.Button(
            row1, text=t("btn.remove"), command=self._games_delete,
            state=DISABLED,
        )
        self.btn_games_delete.pack(side=LEFT, padx=2)

        row2 = ttk.Frame(btn_frame)
        row2.pack(fill=X)
        # «Вже придбав» / «Ще ні» act on the selection — disabled until
        # one exists, same contract as Видалити (see _on_games_select).
        self.btn_games_bought = ttk.Button(
            row2, text=t("btn.bought"), command=self._games_mark_bought,
            state=DISABLED,
        )
        self.btn_games_bought.pack(side=LEFT, padx=2)
        self.btn_games_notyet = ttk.Button(
            row2, text=t("btn.not_bought"),
            command=lambda: self._reset_status("game"),
            state=DISABLED,
        )
        self.btn_games_notyet.pack(side=LEFT, padx=2)
        ttk.Button(row2, text=t("btn.no_check"),
                   command=lambda: self._toggle_flag("game", "no_check")
                   ).pack(side=LEFT, padx=2)
        ttk.Button(row2, text=t("btn.no_alert"),
                   command=lambda: self._toggle_flag("game", "no_alert")
                   ).pack(side=LEFT, padx=2)

        self._refresh_games_list()

    # ---- rendering ---------------------------------------------------

    @staticmethod
    def _game_minimum(g: dict) -> float | None:
        """Historical-low price in the user's currency, or None.

        `lowest_cut` (deepest recorded Steam discount, %) is the stored
        fact; the price is derived from the CURRENT regular price. When
        today's discount equals the historical cut, snap to Steam's own
        final price — Steam rounds (675 × 10% → 67, not 67.50) and the
        snapped value matches what the user actually sees in the store.
        """
        cut = g.get("lowest_cut")
        regular = g.get("regular")
        if not isinstance(cut, int) or not isinstance(regular, (int, float)):
            return None
        if g.get("discount_pct") == cut and isinstance(
                g.get("price"), (int, float)):
            return g["price"]
        return round(regular * (100 - cut) / 100.0, 2)

    def _refresh_games_list(self) -> None:
        tree = getattr(self, "games_tree", None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        items = load_json(GAMELIST_PATH, []) or []
        # Closed rows (bought) stay in the file for History lineage but
        # leave the visible table — same contract as the card lists.
        items = [g for g in items
                 if g.get("status") not in CLOSED_STATUSES]
        for i, g in enumerate(items):
            price = g.get("price")
            regular = g.get("regular")
            minimum = self._game_minimum(g)
            disc = g.get("discount_pct") or 0
            # Presentation-only status remap: a no_check row reads
            # «пропущено» regardless of what's stored — the stored
            # status stays intact for when the flag comes off.
            raw_status = "skipped" if g.get("no_check") \
                else g.get("status", "")
            status = t(f"status.value.{raw_status}") if raw_status else ""
            if status == f"status.value.{raw_status}":
                status = raw_status
            zebra = "even" if i % 2 == 0 else "odd"
            # Row tint: RED when the price is at the all-time low (act
            # now!), GREEN when any sale is on, zebra otherwise. The
            # red check requires a real discount so never-discounted
            # games (minimum == regular) don't light up.
            if (disc > 0 and (g.get("lowest_cut") or 0) > 0
                    and isinstance(price, (int, float))
                    and isinstance(minimum, (int, float))
                    and price <= minimum):
                row_tag = "bad_match"
            elif disc > 0:
                row_tag = "good_match"
            else:
                row_tag = zebra
            tree.insert(
                "", END, iid=g["id"],
                values=(
                    i + 1,
                    g.get("name") or f"app {g.get('appid')}",
                    f"{regular:.2f}" if isinstance(regular, (int, float))
                    else "—",
                    f"-{disc}%" if disc else "—",
                    g.get("price_str") or "—",
                    f"{minimum:.2f}" if minimum is not None else "—",
                    status,
                    t("col.link.open"),
                    "📥" if g.get("imported") else "",
                    "🚫" if g.get("no_check") else "",
                    "🔇" if g.get("no_alert") else "",
                ),
                tags=(row_tag,),
            )
        self._mark_selected_rows(tree)
        self._restore_sort_state(tree)
        self._on_games_select()
        # Status bar shows "додано ігор: N" while this tab is active.
        self._update_statusbar()

    def _games_selected(self) -> list[dict]:
        tree = getattr(self, "games_tree", None)
        if tree is None:
            return []
        sel = set(tree.selection())
        items = load_json(GAMELIST_PATH, []) or []
        return [g for g in items if g.get("id") in sel]

    def _on_games_select(self, _event=None) -> None:
        tree = getattr(self, "games_tree", None)
        btn = getattr(self, "btn_games_delete", None)
        if tree is None or btn is None:
            return
        self._mark_selected_rows(tree)
        try:
            has_sel = bool(tree.selection())
        except tk.TclError:
            return
        btn.configure(state=NORMAL if has_sel else DISABLED,
                      bootstyle="danger" if has_sel else "")
        # Same enable-on-select contract for the deal-closing pair;
        # colours only while armed so the disabled state reads neutral.
        b_bought = getattr(self, "btn_games_bought", None)
        b_notyet = getattr(self, "btn_games_notyet", None)
        if b_bought is not None:
            b_bought.configure(state=NORMAL if has_sel else DISABLED,
                               bootstyle="success" if has_sel else "")
        if b_notyet is not None:
            b_notyet.configure(state=NORMAL if has_sel else DISABLED,
                               bootstyle="warning" if has_sel else "")

    def _on_games_click(self, event):
        tree = event.widget
        if (tree.identify_region(event.x, event.y) == "cell"
                and tree.identify_column(event.x) == self._GAMES_LINK_COL_ID):
            iid = tree.identify_row(event.y)
            if iid:
                items = load_json(GAMELIST_PATH, []) or []
                g = next((x for x in items if x.get("id") == iid), None)
                if g:
                    from steam import GAME_STORE_URL
                    webbrowser.open(GAME_STORE_URL.format(appid=g["appid"]))

    def _on_games_motion(self, event):
        tree = event.widget
        in_link = (
            tree.identify_region(event.x, event.y) == "cell"
            and tree.identify_column(event.x) == self._GAMES_LINK_COL_ID
            and tree.identify_row(event.y)
        )
        tree.configure(cursor="hand2" if in_link else "")

    def _show_games_context_menu(self, event) -> None:
        tree = event.widget
        iid = tree.identify_row(event.y)
        if not iid:
            return
        if iid not in tree.selection():
            tree.selection_set(iid)
            tree.focus(iid)
            self._on_games_select()
        menu = tk.Menu(self, tearoff=0, font=self._context_menu_font())
        menu.add_command(label=t("btn.check_now"),
                         command=self._games_check_now)
        menu.add_command(label=t("ctx.update_min"),
                         command=self._games_update_min_selected)
        menu.add_command(label=t("ctx.open_store"),
                         command=self._games_open_store)
        menu.add_separator()
        menu.add_command(label=t("btn.no_check"),
                         command=lambda: self._toggle_flag("game", "no_check"))
        menu.add_command(label=t("btn.no_alert"),
                         command=lambda: self._toggle_flag("game", "no_alert"))
        menu.add_command(label=t("btn.reset_status"),
                         command=lambda: self._reset_status("game"))
        menu.add_command(label=t("btn.reset_min"),
                         command=self._games_reset_min)
        menu.add_separator()
        menu.add_command(label=t("btn.bought"),
                         command=self._games_mark_bought)
        menu.add_command(label=t("btn.not_bought"),
                         command=lambda: self._reset_status("game"))
        menu.add_separator()
        menu.add_command(label=t("btn.remove"), command=self._games_delete)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _toggle_flag(self, kind: str, flag: str) -> None:
        """Flip no_check / no_alert on the selected rows of any list.

        Group semantics for multi-select: if at least one selected row
        does NOT carry the flag, the click SETS it everywhere (make
        the selection uniform); only when all of them have it does the
        click clear it. Matches how checkbox tri-state toggles behave.

        Side effects on SET:
          * no_check → nothing else; the row simply stops being polled
            (status renders as «пропущено» at display time).
          * no_alert → nothing immediately; alert paths will write
            status="checked" instead of "alerted" on the next hit.
        On CLEAR of no_alert we also demote a stored "checked" status
        back to "" so the row doesn't keep a stale badge.
        """
        if kind == "game":
            selected = self._games_selected()
            path = GAMELIST_PATH
        else:
            selected = self._require_selection()
            path = self._kind_path(kind)
        if not selected:
            return
        sel_ids = {r["id"] for r in selected}
        items = load_json(path, []) or []
        targets = [r for r in items if r.get("id") in sel_ids]
        new_value = not all(r.get(flag) for r in targets)
        for r in targets:
            r[flag] = new_value
            if flag == "no_alert" and not new_value \
                    and r.get("status") == "checked":
                r["status"] = ""
        save_json(path, items)
        if kind == "game":
            self._refresh_games_list()
        else:
            self._refresh_card_list(kind)

    def _reset_status(self, kind: str) -> None:
        """Wipe status + antispam history on the selected rows.

        Testing aid mostly: a row stuck on «сповіщено» blocks repeat
        alerts via antispam; this clears both the badge and the state
        entry so the very next check treats the row as never-alerted
        and fires a fresh first_alert if the condition still holds.
        """
        if kind == "game":
            selected = self._games_selected()
            path = GAMELIST_PATH
        else:
            selected = self._require_selection()
            path = self._kind_path(kind)
        if not selected:
            return
        sel_ids = {r["id"] for r in selected}
        items = load_json(path, []) or []
        state = load_json(STATE_PATH, {}) or {}
        state_dirty = False
        for r in items:
            if r.get("id") not in sel_ids:
                continue
            if r.get("status") not in CLOSED_STATUSES:
                r["status"] = ""
            key = f"{kind}:{r.get('appid')}:{r.get('name')}"
            if state.pop(key, None) is not None:
                state_dirty = True
        save_json(path, items)
        if state_dirty:
            save_json(STATE_PATH, state)
        if kind == "game":
            self._refresh_games_list()
        else:
            self._refresh_card_list(kind)
        self._set_status(t("status.reset_done", count=len(sel_ids)))

    def _games_open_store(self) -> None:
        sel = self._games_selected()
        if sel:
            from steam import GAME_STORE_URL
            webbrowser.open(GAME_STORE_URL.format(appid=sel[0]["appid"]))

    # ---- actions -----------------------------------------------------

    def _games_delete(self) -> None:
        sel = self._games_selected()
        if not sel:
            return
        names = ", ".join((g.get("name") or "?")[:40] for g in sel[:5])
        if len(sel) > 5:
            names += " …"
        if not messagebox.askyesno(
                t("dlg.games_remove.title"),
                t("dlg.games_remove.body", count=len(sel), names=names),
                parent=self):
            return
        sel_ids = {g["id"] for g in sel}
        items = load_json(GAMELIST_PATH, []) or []
        items = [g for g in items if g.get("id") not in sel_ids]
        save_json(GAMELIST_PATH, items)
        # Clear antispam entries so a re-added game starts fresh.
        state = load_json(STATE_PATH, {}) or {}
        dirty = False
        for g in sel:
            if state.pop(f"game:{g.get('appid')}:{g.get('name')}", None) is not None:
                dirty = True
        if dirty:
            save_json(STATE_PATH, state)
        self._refresh_games_list()
        self._set_status(t("status.games_removed", count=len(sel)))

    def _games_reset_min(self) -> None:
        """Context-menu: forget the stored historical-low for selection.

        Clears `lowest_cut`, so «Мінімальна» reads "—" until the next
        «Імпорт мін. цін» / «Оновити мінімальну ціну». Useful when ITAD
        served stale data and the user wants a clean re-fetch marker.
        """
        sel = self._games_selected()
        if not sel:
            return
        sel_ids = {g["id"] for g in sel}
        items = load_json(GAMELIST_PATH, []) or []
        for g in items:
            if g.get("id") in sel_ids:
                g["lowest_cut"] = None
        save_json(GAMELIST_PATH, items)
        self._refresh_games_list()
        self._set_status(t("status.reset_done", count=len(sel_ids)))

    def _games_mark_bought(self) -> None:
        """«Вже придбав» for games — same contract as the card lists.

        Per selected game: price dialog (default = the current
        discounted price), a purchases.json record with operation
        "buy" + kind "game" (History link handler uses the kind to
        open the STORE page instead of a market listing), then
        status="bought" hides the row from the tab and the polls
        while keeping it in gamelist.json. History totals pick the
        spend up exactly like card purchases.
        """
        sel = self._games_selected()
        if not sel:
            return
        sym = self._currency_symbol()
        ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        items = load_json(GAMELIST_PATH, []) or []
        by_id = {g.get("id"): g for g in items}
        state = load_json(STATE_PATH, {}) or {}
        new_purchases: list[dict] = []
        closed = 0
        for chosen in sel:
            g = by_id.get(chosen.get("id"))
            if g is None or g.get("status") in CLOSED_STATUSES:
                continue
            price = g.get("price")
            default_str = (f"{price:.2f}"
                           if isinstance(price, (int, float)) else "")
            ans = simpledialog.askstring(
                t("dlg.completed.title"),
                t("dlg.completed.body_buy", name=g.get("name") or "?",
                  sym=sym),
                initialvalue=default_str, parent=self,
            )
            if ans is None:
                continue
            try:
                price_val = float(ans.replace(",", "."))
            except ValueError:
                messagebox.showerror(t("dlg.error.title"),
                                     t("dlg.bad_number"), parent=self)
                continue
            from steam import GAME_HEADER_IMAGE_URL
            new_purchases.append({
                "name": g.get("name"),
                "display_name": g.get("name"),
                "game_name": g.get("name"),
                "image_url": GAME_HEADER_IMAGE_URL.format(appid=g["appid"]),
                "appid": g.get("appid"),
                "market_hash_name": g.get("name"),
                "price": f"{price_val:.2f} {sym}".rstrip(),
                "target": self._game_minimum(g),
                "operation": "buy",
                "kind": "game",
                "timestamp": ts,
            })
            g["status"] = "bought"
            state.pop(f"game:{g.get('appid')}:{g.get('name')}", None)
            closed += 1
        if not closed:
            return
        purchases = load_json(PURCHASES_PATH, []) or []
        purchases.extend(new_purchases)
        save_json(GAMELIST_PATH, items)
        save_json(PURCHASES_PATH, purchases)
        save_json(STATE_PATH, state)
        self._refresh_games_list()
        self._refresh_history()

    def _games_import_wishlist(self) -> None:
        """Import / refresh from the Steam wishlist.

        Existing rows (matched by appid) get their name + price data
        overwritten silently — re-import IS the refresh. Games present
        in the wishlist but not in the list go through a checklist
        dialog (pre-checked; the user unchecks what to skip, or cancels
        the whole add). Games removed from the wishlist are left alone —
        the local list is curated by the user, not mirrored.
        """
        steam_cfg = self.config_data.get("steam") or {}
        steamid = (steam_cfg.get("id") or "").strip()
        if not steamid:
            messagebox.showinfo(t("dlg.games_import.title"),
                                t("dlg.games_import.no_steamid"),
                                parent=self)
            return
        self._set_status(t("status.games_importing"))

        def worker():
            import steam as steam_mod
            try:
                wl = steam_mod.fetch_wishlist(steamid)
                appids = [w["appid"] for w in wl if w.get("appid")]
                info = steam_mod.fetch_game_info_batch(
                    appids,
                    country=(self.config_data.get("market") or {})
                    .get("country", "UA"),
                )
            except Exception as e:
                log.error("wishlist import failed: %s", e)
                self.after(0, lambda err=str(e): self._set_status(
                    t("status.games_import_error", err=err)))
                return
            self.after(0, lambda: self._games_apply_import(appids, info))

        threading.Thread(target=worker, daemon=True).start()

    def _games_apply_import(self, appids: list[int],
                            info: dict[int, dict]) -> None:
        """Tk-thread half of the wishlist import.

        Unresolvable wishlist entries — appids GetItems can't give a
        NAME for (delisted, region-locked, bare placeholder pages) —
        are useless rows ("app 2986410" with dashes everywhere). They
        go to gameblacklist.json: skipped on this and every future
        import, and any such rows already sitting in the list are
        pruned. Manually adding a game by URL removes it from the
        blacklist (explicit user intent beats the auto-filter).
        """
        blacklist = set(load_json(GAMEBLACKLIST_PATH, []) or [])
        bl_dirty = False
        items = load_json(GAMELIST_PATH, []) or []
        # Prune existing junk rows: blacklisted OR still nameless.
        pruned = [g for g in items
                  if g.get("appid") not in blacklist and g.get("name")]
        list_dirty = len(pruned) != len(items)
        items = pruned
        by_appid = {g.get("appid"): g for g in items}
        updated = 0
        new_games: list[dict] = []
        for appid in appids:
            if appid in blacklist:
                continue
            d = info.get(appid)
            if not d or not d.get("name"):
                # Steam can't even name it — blacklist so it never
                # clutters the table again.
                blacklist.add(appid)
                bl_dirty = True
                log.info("game %s unresolvable — blacklisted", appid)
                continue
            if appid in by_appid:
                g = by_appid[appid]
                g["name"] = d["name"] or g.get("name") or ""
                g["price"] = d["price"]
                g["price_str"] = d["price_str"]
                g["regular"] = d["regular"]
                g["discount_pct"] = d["discount_pct"]
                g["imported"] = True
                updated += 1
            else:
                new_games.append({
                    "id": str(uuid.uuid4()),
                    "appid": appid,
                    "name": d["name"],
                    "price": d["price"],
                    "price_str": d["price_str"],
                    "regular": d["regular"],
                    "discount_pct": d["discount_pct"],
                    "lowest_cut": None,
                    "status": "",
                    "imported": True,
                    "added": datetime.now(timezone.utc).isoformat(),
                })
        if bl_dirty:
            save_json(GAMEBLACKLIST_PATH, sorted(blacklist))
        if updated or list_dirty:
            save_json(GAMELIST_PATH, items)
        if not new_games:
            self._refresh_games_list()
            discounted = sum(1 for g in items
                             if (g.get("discount_pct") or 0) > 0
                             and g.get("status") not in CLOSED_STATUSES)
            self._set_status(t("status.games_import_done",
                               added=0, updated=updated,
                               discounted=discounted))
            return
        picked = self._games_pick_new_dialog(new_games)
        if picked:
            items.extend(picked)
            save_json(GAMELIST_PATH, items)
        self._refresh_games_list()
        discounted = sum(1 for g in items
                         if (g.get("discount_pct") or 0) > 0
                         and g.get("status") not in CLOSED_STATUSES)
        self._set_status(t("status.games_import_done",
                           added=len(picked), updated=updated,
                           discounted=discounted))

    def _games_pick_new_dialog(self, new_games: list[dict]) -> list[dict]:
        """Checklist modal for wishlist games not yet in the list.

        Same interaction contract as the cards import dialog: every row
        pre-checked, click toggles ☑/☐, «Додати» applies the checked
        subset, «Скасувати» adds nothing. Blocks via wait_window.
        """
        result: list[dict] = []
        dlg = tk.Toplevel(self)
        dlg.title(t("dlg.games_import.title"))
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("640x480")

        outer = ttk.Frame(dlg, padding=10)
        outer.pack(fill=BOTH, expand=YES)
        ttk.Label(outer, text=t("dlg.games_import.found",
                                count=len(new_games))).pack(anchor=W)

        btn_row = ttk.Frame(outer)
        btn_row.pack(side=BOTTOM, fill=X, pady=(8, 0))

        frame = ttk.Frame(outer, borderwidth=1, relief="solid")
        frame.pack(fill=BOTH, expand=YES, pady=(8, 0))
        cols = ("chk", "name", "price", "discount")
        tv = ttk.Treeview(frame, columns=cols, show="headings",
                          selectmode="none")
        tv.heading("chk", text="✓", anchor=CENTER)
        tv.column("chk", width=40, anchor=CENTER, stretch=False)
        tv.heading("name", text=t("col.games.name"), anchor=W)
        tv.column("name", width=330, anchor=W)
        tv.heading("price", text=t("col.games.price"), anchor=E)
        tv.column("price", width=100, anchor=E)
        tv.heading("discount", text=t("col.games.discount"), anchor=CENTER)
        tv.column("discount", width=80, anchor=CENTER)
        self._apply_row_tags(tv)
        vsb = ttk.Scrollbar(frame, orient=VERTICAL, command=tv.yview,
                            bootstyle="success")
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        checked: dict[str, bool] = {}
        for i, g in enumerate(new_games):
            iid = g["id"]
            checked[iid] = True
            disc = g.get("discount_pct") or 0
            tv.insert("", END, iid=iid, values=(
                "☑", g["name"], g.get("price_str") or "—",
                f"-{disc}%" if disc else "—",
            ), tags=("even" if i % 2 == 0 else "odd",))

        def _toggle(event):
            iid = tv.identify_row(event.y)
            if not iid:
                return
            checked[iid] = not checked.get(iid, False)
            vals = list(tv.item(iid, "values"))
            vals[0] = "☑" if checked[iid] else "☐"
            tv.item(iid, values=vals)
        tv.bind("<Button-1>", _toggle)

        def _apply():
            result.extend(g for g in new_games if checked.get(g["id"]))
            dlg.destroy()
        ttk.Button(btn_row, text=t("dlg.games_import.add"),
                   bootstyle="success", command=_apply
                   ).pack(side=RIGHT, padx=(6, 0))
        ttk.Button(btn_row, text=t("dlg.import.btn_cancel"),
                   bootstyle="danger", command=dlg.destroy
                   ).pack(side=RIGHT)

        dlg.wait_window()
        return result

    def _games_add_by_url(self) -> None:
        """Add a single game from a Steam store URL.

        Accepts anything containing /app/{appid} (full store URLs with
        slugs and query params included) or a bare numeric appid.
        Manual add un-blacklists the appid — explicit user intent beats
        the import's junk filter. Row gets imported=False so it reads
        as hand-picked in the 📥 column.
        """
        ans = simpledialog.askstring(
            t("dlg.games_import.title"), t("dlg.games_add_url.ask"),
            parent=self)
        if not ans:
            return
        m = re.search(r"/app/(\d+)", ans) or re.fullmatch(r"\s*(\d+)\s*", ans)
        if not m:
            messagebox.showerror(t("dlg.error.title"),
                                 t("dlg.games_add_url.bad"), parent=self)
            return
        appid = int(m.group(1))
        items = load_json(GAMELIST_PATH, []) or []
        if any(g.get("appid") == appid for g in items):
            self._set_status(t("status.games_already_listed"))
            return
        self._set_status(t("status.games_importing"))

        def worker():
            import steam as steam_mod
            try:
                info = steam_mod.fetch_game_info_batch(
                    [appid],
                    country=(self.config_data.get("market") or {})
                    .get("country", "UA"),
                )
            except Exception as e:
                self.after(0, lambda err=str(e): self._set_status(
                    t("status.games_import_error", err=err)))
                return
            self.after(0, lambda: self._games_apply_add(appid, info))

        threading.Thread(target=worker, daemon=True).start()

    def _games_apply_add(self, appid: int, info: dict[int, dict]) -> None:
        d = info.get(appid)
        if not d or not d.get("name"):
            self._set_status(t("status.games_import_error",
                               err=f"app {appid} not resolvable"))
            return
        # Un-blacklist: the user explicitly wants this one tracked.
        blacklist = set(load_json(GAMEBLACKLIST_PATH, []) or [])
        if appid in blacklist:
            blacklist.discard(appid)
            save_json(GAMEBLACKLIST_PATH, sorted(blacklist))
        items = load_json(GAMELIST_PATH, []) or []
        items.append({
            "id": str(uuid.uuid4()),
            "appid": appid,
            "name": d["name"],
            "price": d["price"],
            "price_str": d["price_str"],
            "regular": d["regular"],
            "discount_pct": d["discount_pct"],
            "lowest_cut": None,
            "status": "",
            "imported": False,
            "added": datetime.now(timezone.utc).isoformat(),
        })
        save_json(GAMELIST_PATH, items)
        self._refresh_games_list()
        self._set_status(t("status.games_added", name=d["name"]))

    def _games_import_lows(self) -> None:
        """Fetch historical-low discounts for EVERY game in the list."""
        items = load_json(GAMELIST_PATH, []) or []
        if not items:
            self._set_status(t("status.games_empty"))
            return
        self._set_status(t("status.games_lows_importing"))
        appids = [g["appid"] for g in items]

        def worker():
            import steam as steam_mod
            try:
                lows = steam_mod.fetch_historical_lows(
                    appids,
                    country=(self.config_data.get("market") or {})
                    .get("country", "UA"),
                )
            except Exception as e:
                log.error("historical lows import failed: %s", e)
                self.after(0, lambda err=str(e): self._set_status(
                    t("status.games_import_error", err=err)))
                return
            self.after(0, lambda: self._games_apply_lows(lows))

        threading.Thread(target=worker, daemon=True).start()

    def _games_apply_lows(self, lows: dict[int, int],
                          only_appids: set[int] | None = None) -> None:
        """Apply fetched historical-low cuts.

        `only_appids` scopes the whole pass (updates, missing-data
        warnings, the one-shot alert sweep) to that subset — the
        context-menu «Оновити мінімальну ціну» works on the selection
        ONLY, while the toolbar import covers the entire list.
        """
        items = load_json(GAMELIST_PATH, []) or []
        resolved = 0
        for g in items:
            appid = g.get("appid")
            if only_appids is not None and appid not in only_appids:
                continue
            cut = lows.get(appid)
            if isinstance(cut, int):
                g["lowest_cut"] = cut
                resolved += 1
            elif g.get("lowest_cut") is None:
                # No data on ITAD for this app — note it in the log so
                # the user knows why «Мінімум» stays a dash.
                log.warning(t("log.games_no_low", name=g.get("name") or "?"))
        save_json(GAMELIST_PATH, items)
        self._refresh_games_list()
        # Fresh minimums may reveal that a game is AT its all-time low
        # right now — tell the user immediately instead of waiting for
        # the next scheduler tick. Reuses the standard alert pipeline
        # (cached prices, no refetch), so antispam makes it one-shot:
        # the next poll sees the state entry and stays silent.
        self._games_apply_check({}, evaluate_stored=True, force_at_min=True,
                                only_appids=only_appids, quiet=True)
        # Status line LAST — both refreshes above run _update_statusbar
        # (the scheduler summary), which would overwrite anything set
        # earlier. Whatever writes last owns the bar.
        def _is_at_min(g: dict) -> bool:
            """Red-row condition: on sale AND at/below the all-time low."""
            return ((g.get("discount_pct") or 0) > 0
                    and isinstance(g.get("price"), (int, float))
                    and isinstance(self._game_minimum(g), (int, float))
                    and g["price"] <= self._game_minimum(g)
                    and g.get("status") not in CLOSED_STATUSES)

        if only_appids is not None:
            # Selection refresh: report only about the updated rows —
            # the global counters would drown the answer the user
            # actually asked for ("did MY game move?").
            at_min_sel = sum(1 for g in items
                             if g.get("appid") in only_appids
                             and _is_at_min(g))
            if at_min_sel:
                self._set_status(t("status.games_lows_updated_min",
                                   count=resolved, at_min=at_min_sel))
            else:
                self._set_status(t("status.games_lows_updated",
                                   count=resolved))
        else:
            at_min = sum(1 for g in items if _is_at_min(g))
            self._set_status(t("status.games_lows_done",
                               count=resolved, total=len(items),
                               at_min=at_min))

    def _games_update_min_selected(self) -> None:
        """Context-menu: refresh the historical low for selected games only."""
        sel = self._games_selected()
        if not sel:
            return
        appids = [g["appid"] for g in sel]
        scope = set(appids)
        self._set_status(t("status.games_lows_importing"))

        def worker():
            import steam as steam_mod
            try:
                lows = steam_mod.fetch_historical_lows(
                    appids,
                    country=(self.config_data.get("market") or {})
                    .get("country", "UA"),
                )
            except Exception as e:
                log.error("historical low refresh failed: %s", e)
                self.after(0, lambda err=str(e): self._set_status(
                    t("status.games_import_error", err=err)))
                return
            # Scope the apply to the selection — without it the pass
            # touched (and force-alerted) the WHOLE list.
            self.after(0, lambda: self._games_apply_lows(
                lows, only_appids=scope))

        threading.Thread(target=worker, daemon=True).start()

    def _games_check_now(self) -> None:
        """Re-poll prices for the selected games (or all when none picked).

        One GetItems batch per 50 games — no per-item delay needed, the
        store API isn't the rate-limited market endpoint.
        """
        sel = self._games_selected()
        items_all = load_json(GAMELIST_PATH, []) or []
        # no_check rows are excluded even when explicitly selected —
        # «Не перевіряти» means exactly that. Closed (bought) rows are
        # done deals, nothing to poll.
        targets = [g for g in (sel or items_all)
                   if not g.get("no_check")
                   and g.get("status") not in CLOSED_STATUSES]
        if not targets:
            self._set_status(t("status.games_empty"))
            return
        appids = [g["appid"] for g in targets]
        self._set_status(t("status.checking_multi", count=len(appids)))

        def worker():
            import steam as steam_mod
            try:
                info = steam_mod.fetch_game_info_batch(
                    appids,
                    country=(self.config_data.get("market") or {})
                    .get("country", "UA"),
                )
            except Exception as e:
                log.error("games price check failed: %s", e)
                self.after(0, lambda err=str(e): self._set_status(
                    t("status.games_import_error", err=err)))
                return
            self.after(0, lambda: self._games_apply_check(info))

        threading.Thread(target=worker, daemon=True).start()

    def _games_apply_check(self, info: dict[int, dict],
                           evaluate_stored: bool = False,
                           force_at_min: bool = False,
                           only_appids: set[int] | None = None,
                           quiet: bool = False) -> None:
        """Apply freshly polled prices + run the alert evaluation.

        `evaluate_stored=True` runs the alert pass over rows that have
        NO fresh fetch in `info`, using the prices already on record —
        the post-«Імпорт мін. цін» path uses this to fire immediate
        alerts for games that just turned out to be at their all-time
        low, without re-polling Steam.

        `force_at_min=True` (same path) clears the antispam entry for
        games sitting AT the minimum before evaluating, so the user
        gets the promised one-shot Telegram message even if the game
        was already alerted earlier under a plain-sale rule. One-shot
        per import click — the evaluation re-arms antispam right after.
        """
        from alerts import evaluate_and_alert
        from steam import GAME_HEADER_IMAGE_URL, GAME_STORE_URL

        items = load_json(GAMELIST_PATH, []) or []
        state = load_json(STATE_PATH, {}) or {}
        state_dirty = False
        alerted = 0
        cfg = self.config_data
        tg_cfg = cfg.get("telegram", {})
        token = tg_cfg.get("bot_token", "")
        chat_id = str(tg_cfg.get("chat_id", ""))
        template = (cfg.get("message_template") or "").strip() \
            or t("tg.message.default")
        spam = cfg.get("antispam", {})
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)

        for g in items:
            if g.get("no_check") or g.get("status") in CLOSED_STATUSES:
                continue
            if (only_appids is not None
                    and g.get("appid") not in only_appids):
                continue
            d = info.get(g.get("appid"))
            if d:
                g["price"] = d["price"]
                g["price_str"] = d["price_str"]
                g["regular"] = d["regular"]
                g["discount_pct"] = d["discount_pct"]
            elif not evaluate_stored:
                continue
            minimum = self._game_minimum(g)
            # Alert rule: ANY active sale (знижка > 0). The historical
            # minimum is NOT a gate — plenty of wishlist games have no
            # ITAD data and the user still wants to hear about their
            # sales. The minimum only powers the 🔥 punch line in the
            # Telegram message + the red row tint.
            if not (g.get("discount_pct") or 0) > 0:
                # Sale ended → clear alert state so the next sale fires
                # a fresh first_alert.
                if g.get("status") in ("alerted", "checked"):
                    g["status"] = ""
                key = f"game:{g.get('appid')}:{g.get('name')}"
                if state.pop(key, None) is not None:
                    state_dirty = True
                continue
            if (not token or not chat_id
                    or not isinstance(g.get("price"), (int, float))):
                continue
            at_min = (isinstance(minimum, (int, float))
                      and g["price"] <= minimum)
            # «Не сповіщати»: still polled (prices updated above), but
            # the Telegram path is skipped. The sale is running, so the
            # row earns the silent "checked" badge.
            if g.get("no_alert"):
                if g.get("status") != "checked":
                    g["status"] = "checked"
                continue
            if force_at_min and at_min:
                # Post-lows one-shot: drop the antispam entry so the
                # evaluation below fires a fresh first_alert even for a
                # game already alerted under the plain-sale rule.
                if state.pop(f"game:{g.get('appid')}:{g.get('name')}",
                             None) is not None:
                    state_dirty = True
            alert_info = {
                "name": g.get("name") or str(g.get("appid")),
                "appid": g.get("appid"),
                "display_name": g.get("name") or "",
                "game_name": g.get("name") or "",
                "market_hash_name": g.get("name") or "",
                "image_url": GAME_HEADER_IMAGE_URL.format(appid=g["appid"]),
                "alert_url": GAME_STORE_URL.format(appid=g["appid"]),
                "lowest_price": g["price"],
                "lowest_price_raw": g.get("price_str") or f"{g['price']:.2f}",
                # Target == current price → evaluate_and_alert's
                # "lowest <= target" always holds while a sale runs;
                # antispam governs repeats (deeper cut → re-alert via
                # repeat_if_lower; otherwise remind_after_hours).
                "target_price": g["price"],
                "volume": f"-{g.get('discount_pct') or 0}%",
                "at_historical_min": at_min,
            }
            sd, did_alert, did_reset, _reason = evaluate_and_alert(
                kind="game", info=alert_info, state=state,
                token=token, chat_id=chat_id, template=template,
                repeat_if_lower=spam.get("repeat_if_lower", True),
                remind_after_hours=spam.get("remind_after_hours", 24),
                now=now_dt,
            )
            if sd:
                state_dirty = True
            if did_alert:
                alerted += 1
                g["status"] = "alerted"
            elif did_reset and g.get("status") == "alerted":
                g["status"] = ""

        save_json(GAMELIST_PATH, items)
        if state_dirty:
            save_json(STATE_PATH, state)
        self._refresh_games_list()
        # `quiet` callers (the post-lows sweep) own the status line —
        # don't clobber their "мінімальні ціни: …" message with the
        # generic "Перевірено N".
        if not quiet:
            self._set_status(t("status.games_checked",
                               count=len(info), alerted=alerted))

    # ------------------------------------------------------------------
    # Metadata backfill
    # ------------------------------------------------------------------

    def _backfill_metadata(self):
        """Fill in display_name + game_name for legacy watchlist entries.

        Runs on a background thread at startup. For every entry missing
        the metadata fields we hit fetch_card_metadata (cached per appid,
        so two cards from the same game share one HTTP call). Saves
        watchlist.json and schedules a UI refresh only if something actually
        changed — keeping the disk and the screen calm when there's nothing
        to do.
        """
        from steam import fetch_card_metadata
        try:
            buy_list = load_json(WATCHLIST_PATH, []) or []
            sell_list = load_json(SALELIST_PATH, []) or []
            buy_dirty = self._backfill_records(buy_list, fetch_card_metadata, other=sell_list)
            sell_dirty = self._backfill_records(sell_list, fetch_card_metadata, other=buy_list)
            if buy_dirty:
                save_json(WATCHLIST_PATH, buy_list)
                self.after(0, lambda: self._refresh_card_list("buy"))
            if sell_dirty:
                save_json(SALELIST_PATH, sell_list)
                self.after(0, lambda: self._refresh_card_list("sell"))

            # Same treatment for purchases.json — older entries (made before
            # _mark_completed started saving display_name / game_name / image_url)
            # would otherwise show «—» in History forever.
            purchases = load_json(PURCHASES_PATH, []) or []
            ph_dirty = self._backfill_records(
                purchases, fetch_card_metadata,
                other=(buy_list + sell_list),
            )
            if ph_dirty:
                save_json(PURCHASES_PATH, purchases)
                self.after(0, self._refresh_history)
        except Exception:
            # Backfill is best-effort — never crash the UI thread over it.
            pass

    @staticmethod
    def _backfill_records(records: list, fetch_fn, other: list | None) -> bool:
        """Top up display_name/game_name/image_url/item_type on a list of records.

        First tries to copy from `other` (a sibling list with the same
        market_hash_name — used to share metadata between watchlist.json
        and purchases.json without an extra HTTP call). Falls back to
        `fetch_fn(appid, mhn)` for anything still missing.

        Also splits Steam's combined "Game — Item type" string into
        separate `game_name` and `item_type` fields so legacy rows
        (added before the Type column existed) start showing the type
        cell after their first backfill pass.

        Returns True if at least one record was modified.
        """
        from steam import split_game_and_type

        dirty = False
        for item in records:
            mhn = item.get("market_hash_name", "")
            # If game_name still contains a known type suffix, treat that
            # as "type needs extracting" — old rows have things like
            # "STAR WARS Jedi: Survivor™ Trading Card" sitting in game_name.
            type_missing = not item.get("item_type")
            if type_missing and item.get("game_name"):
                g, ty = split_game_and_type(item["game_name"])
                if ty:
                    item["game_name"] = g
                    item["item_type"] = ty
                    dirty = True
                    type_missing = False
            needs = (
                not item.get("display_name")
                or item.get("display_name") == mhn
                or not item.get("game_name")
                or item.get("game_name") == "—"
                or not item.get("image_url")
            )
            if not needs:
                continue
            meta = None
            if other:
                twin = next(
                    (x for x in other if x.get("market_hash_name") == mhn),
                    None,
                )
                if twin and twin.get("display_name") and twin.get("display_name") != mhn:
                    meta = {
                        "display_name": twin.get("display_name"),
                        "game_name": twin.get("game_name"),
                        "image_url": twin.get("image_url"),
                    }
            if meta is None:
                try:
                    meta = fetch_fn(item["appid"], mhn)
                except Exception:
                    continue
            if not item.get("display_name") or item["display_name"] == mhn:
                item["display_name"] = meta.get("display_name") or mhn
            if not item.get("game_name") or item["game_name"] == "—":
                raw_game = meta.get("game_name") or "—"
                # Same split for freshly-fetched meta — keeps type out of
                # the Game column.
                g, ty = split_game_and_type(raw_game)
                if ty:
                    item["game_name"] = g or raw_game
                    if not item.get("item_type"):
                        item["item_type"] = ty
                else:
                    item["game_name"] = raw_game
            if not item.get("image_url") and meta.get("image_url"):
                item["image_url"] = meta["image_url"]
            dirty = True
        return dirty

    # ------------------------------------------------------------------
    # Card-list Treeview click handling (link column)
    # ------------------------------------------------------------------

    # Identifier of the "link" column inside the card-list Treeview, as
    # returned by tree.identify_column(). Columns: (num, name, type, game,
    # target, last, spread, status, link, imported) → link is position 9.
    _LINK_COL_ID = "#9"

    def _on_card_tree_click(self, event):
        """Open the listing URL when the user clicks the link column."""
        tree = event.widget
        if tree.identify_region(event.x, event.y) != "cell":
            return
        if tree.identify_column(event.x) != self._LINK_COL_ID:
            return
        iid = tree.identify_row(event.y)
        if not iid:
            return
        # The clicked tree tells us which list to look in. iid = record's
        # uuid (since salelist allows duplicate mhn).
        kind = "buy" if tree is self.list_trees.get("buy") else "sell"
        items = load_json(self._kind_path(kind), [])
        item = next((x for x in items if x.get("id") == iid), None)
        if not item:
            return
        from steam import market_url
        webbrowser.open(market_url(item["appid"], item["market_hash_name"]))

    def _show_card_context_menu(self, event, kind: str) -> None:
        """Right-click handler — pop the context menu over the clicked row.

        Selection model:
          * If the user right-clicks INSIDE the current selection (single
            or multi), we leave the selection alone — they want to act
            on the whole set.
          * If they right-click a row that ISN'T currently selected, we
            replace selection with just that row. This matches the
            convention every file manager / IDE uses.
          * If they right-click empty space (below the rows), do nothing
            — no menu, per the user's preference. Same rule as Explorer
            when you right-click in dead space inside a listview.

        Menu items themselves are plain `command=self._existing_method`
        — same handlers the toolbar buttons use. Enable/disable state
        is read from those buttons via `_update_action_buttons` so
        we don't re-derive the rules per item.
        """
        tree = event.widget
        iid = tree.identify_row(event.y)
        if not iid:
            return  # empty area — no menu

        current_sel = tree.selection()
        if iid not in current_sel:
            # Reset selection to just this row before popping menu.
            tree.selection_set(iid)
            tree.focus(iid)
            # Touch the highlight tags + downstream button state so the
            # menu fires against the same row visually selected.
            self._mark_selected_rows(tree)
            self._update_action_buttons()

        menu = self._build_card_context_menu(kind)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            # tk_popup leaves the menu grabbed; release so clicks
            # elsewhere don't get swallowed by it on some WMs.
            menu.grab_release()

    # Context-menu font scaling: follow the global Settings dropdown
    # but cap at x3 — bigger menus push items off the screen on a
    # 1080p monitor and we lose the "compact pick list" feel.
    _CONTEXT_MENU_MAX_SCALE = 3

    def _context_menu_font(self) -> tuple:
        """Compute (family, size [, weight]) for the right-click menu.

        Reads the current global font scale (`self._font_scale`),
        clamps to `_CONTEXT_MENU_MAX_SCALE`, and multiplies the
        unscaled TkMenuFont size we cached at startup. Using the
        cached size — not the live TkMenuFont — keeps us from
        double-scaling when the global TkMenuFont has already been
        bumped by `_apply_font_scale`.
        """
        import tkinter.font as tkfont
        try:
            src = tkfont.nametofont("TkMenuFont")
            family = src.cget("family") or "Segoe UI"
            weight = src.cget("weight") or "normal"
        except tk.TclError:
            family, weight = "Segoe UI", "normal"
        base = (self._original_font_sizes or {}).get("TkMenuFont", 9)
        scale = min(
            max(1, int(getattr(self, "_font_scale", 1) or 1)),
            self._CONTEXT_MENU_MAX_SCALE,
        )
        mult = self._FONT_SCALE_FACTORS[scale]
        size = max(1, int(round(base * mult)))
        # Drop the weight tuple slot unless it's actually bold —
        # ("Segoe UI", 12) is friendlier to Tk than the 3-tuple with
        # weight="normal".
        if weight and weight != "normal":
            return (family, size, weight)
        return (family, size)

    def _build_card_context_menu(self, kind: str) -> tk.Menu:
        """Construct (or rebuild) the right-click menu for one list kind.

        Built fresh on each right-click so item state (enabled/disabled,
        i18n text) is current — caching across language switches or
        selection changes would be a maintenance trap for a six-item
        menu. The cost is trivial.

        Item commands point to the same `self._foo` handlers wired to
        the toolbar buttons — see `btn_specs` in the card-list tab
        builder. That means new behaviour (e.g. confirmation dialog
        added to `_remove_card`) ripples here automatically.
        """
        # Pass the scaled font here (not via `menu.configure` after
        # creation) — on some Tk builds, after-the-fact configure on
        # a tk.Menu doesn't propagate to its items. Setting it at
        # construction time is the reliable path.
        menu = tk.Menu(self, tearoff=0, font=self._context_menu_font())
        # The toolbar's enable/disable state is the single source of
        # truth — same conditions apply here. We pull it from the
        # button refs that `_update_action_buttons` already maintains.
        btns = self.list_action_buttons.get(kind) or {}

        def _state_of(btn_key: str) -> str:
            """Mirror the toolbar button's state on the menu item."""
            btn = btns.get(btn_key)
            if btn is None:
                return "normal"
            try:
                return "disabled" if str(btn.cget("state")) == "disabled" \
                    else "normal"
            except tk.TclError:
                return "normal"

        # The selection-aware actions: Check now / Edit target /
        # Duplicate / Move-to-other / Bought-Sold / Not bought / Remove.
        # The toolbar buttons in row1 don't all map to keys in
        # list_action_buttons — only the ones with selection-dependent
        # state do. The rest are always-enabled (Check now / Edit target
        # / link), so we just leave their state="normal".
        menu.add_command(
            label=t("btn.check_now"),
            command=self._check_now,
            state=_state_of("check"),
        )
        menu.add_command(
            label=t("col.link.open"),
            command=self._open_selected_market_link,
            state=_state_of("check"),  # same gate: needs a selection
        )
        menu.add_separator()
        menu.add_command(
            label=t("btn.edit_target"),
            command=self._edit_target,
            # _edit_target itself bails if no selection — but disabling
            # via the same gate as the toolbar's «Видалити» button
            # keeps the UX consistent.
            state=_state_of("remove"),
        )
        if kind == "sell":
            menu.add_command(
                label=t("btn.duplicate"),
                command=self._duplicate_card,
                state=_state_of("duplicate"),
            )
        move_key = "btn.move_to_sell" if kind == "buy" else "btn.move_to_buy"
        menu.add_command(
            label=t(move_key),
            command=self._move_to_other_list,
            state=_state_of("move"),
        )
        menu.add_separator()
        # "Придбав" on buy tab, "Продав" on sell tab.
        action_key = "btn.bought" if kind == "buy" else "btn.sold"
        menu.add_command(
            label=t(action_key),
            command=self._mark_completed,
            state=_state_of("completed"),
        )
        menu.add_command(
            label=t("btn.not_bought"),
            command=self._mark_not_bought,
            state=_state_of("not"),
        )
        menu.add_separator()
        menu.add_command(
            label=t("btn.no_check"),
            command=lambda k=kind: self._toggle_flag(k, "no_check"),
            state=_state_of("remove"),
        )
        menu.add_command(
            label=t("btn.no_alert"),
            command=lambda k=kind: self._toggle_flag(k, "no_alert"),
            state=_state_of("remove"),
        )
        menu.add_command(
            label=t("btn.reset_status"),
            command=lambda k=kind: self._reset_status(k),
            state=_state_of("remove"),
        )
        menu.add_separator()
        menu.add_command(
            label=t("btn.remove"),
            command=self._remove_card,
            state=_state_of("remove"),
        )
        return menu

    def _open_selected_market_link(self) -> None:
        """Open the first selected card's Steam Market listing URL.

        Used by the right-click menu's "Відкрити на маркеті" entry —
        the toolbar didn't have a dedicated button (the link column
        in each row was the entry point), but a menu item asking for
        the same thing is a natural ask. Pulls the active tree from
        the card-list helper so the same code serves buy + sell.
        """
        tree = self._active_tree()
        if tree is None:
            return
        sel = tree.selection()
        if not sel:
            return
        kind = "buy" if tree is self.list_trees.get("buy") else "sell"
        items = load_json(self._kind_path(kind), []) or []
        item = next((x for x in items if x.get("id") == sel[0]), None)
        if not item:
            return
        from steam import market_url
        webbrowser.open(market_url(item["appid"], item["market_hash_name"]))

    def _on_card_tree_motion(self, event):
        """Switch cursor to a hand when hovering the link column."""
        tree = event.widget
        in_link = (
            tree.identify_region(event.x, event.y) == "cell"
            and tree.identify_column(event.x) == self._LINK_COL_ID
            and tree.identify_row(event.y)
        )
        tree.configure(cursor="hand2" if in_link else "")

    def _on_tree_ctrl_a(self, event):
        """Ctrl+A in a Treeview → toggle "select all" / "deselect all".

        First press selects everything; pressing it again with all rows
        already selected clears the selection. We dispatch on event.keycode
        (A=65, layout-independent on Windows) so this works on a Cyrillic
        keyboard layout too.
        """
        if getattr(event, "keycode", -1) != 65:
            return None
        tree = event.widget
        all_iids = tree.get_children()
        if not all_iids:
            return "break"
        if set(tree.selection()) == set(all_iids):
            # Everything's already selected → second press clears.
            tree.selection_remove(*all_iids)
        else:
            tree.selection_set(all_iids)
        return "break"

    def _selected_items(self) -> list[dict]:
        """Return ALL currently-selected items from the active card-list tab.

        Returns [] (no warning) if the tab isn't a card list or nothing's
        selected. Callers decide what to do with an empty list — most will
        show a `dlg.select` warning themselves so the message timing is
        right (after dialog confirmation, not before).
        """
        kind = self._active_kind()
        tree = self.list_trees.get(kind) if kind else None
        if tree is None:
            return []
        sel = tree.selection()
        if not sel:
            return []
        items = load_json(self._kind_path(kind), []) or []
        by_id = {x.get("id"): x for x in items if x.get("id")}
        return [by_id[iid] for iid in sel if iid in by_id]

    def _selected_item(self):
        """Back-compat shim: return the first selected item, or None.

        Shows a warning if nothing's selected. Use _selected_items() when
        the action should fan out over multiple selected rows.
        """
        sel = self._selected_items()
        if not sel:
            messagebox.showwarning(t("dlg.select.title"), t("dlg.select.body"), parent=self)
            return None
        return sel[0]

    def _require_selection(self) -> list[dict]:
        """Like _selected_items, but pops a warning when empty. Returns []."""
        sel = self._selected_items()
        if not sel:
            messagebox.showwarning(t("dlg.select.title"), t("dlg.select.body"), parent=self)
        return sel

    def _add_by_url(self):
        from steam import (parse_market_url, get_price, clean_card_name,
                           fetch_card_metadata, split_game_and_type)

        kind = self._active_kind()
        if kind is None:
            # Add only makes sense on a card-list tab.
            return
        path = self._kind_path(kind)
        url = simpledialog.askstring(
            t("dlg.add_url.title"), t("dlg.add_url.body"), parent=self,
        )
        if not url:
            return
        parsed = parse_market_url(url.strip())
        if not parsed:
            messagebox.showerror(t("dlg.error.title"), t("dlg.add_url.bad_url"),
                                 parent=self)
            return
        appid, market_hash_name = parsed
        # Pretty name without a network round-trip — strips the "{appid}-"
        # prefix from Community Items so the user sees "Walker (Foil)"
        # instead of "3357650-Walker (Foil)" in every prompt below.
        pretty = clean_card_name(market_hash_name)

        # Pasting a URL from another app often leaves focus there; bring
        # ourselves to the foreground so the next simpledialog grabs focus
        # cleanly. parent=self alone isn't always enough on Windows.
        self.lift()
        self.focus_force()
        target_str = simpledialog.askstring(
            t("dlg.target.title"),
            t("dlg.target.body", name=pretty),
            parent=self,
        )
        if not target_str:
            return
        try:
            target = float(target_str.replace(",", "."))
        except ValueError:
            messagebox.showerror(t("dlg.error.title"), t("dlg.bad_number"), parent=self)
            return

        self._set_status(t("status.checking", name=pretty))
        cfg = self.config_data
        currency = cfg.get("market", {}).get("currency", 18)
        country = cfg.get("market", {}).get("country", "UA")
        try:
            info = get_price(appid, market_hash_name, currency, country)
            lowest = info.get("lowest_price")
            messagebox.showinfo(
                t("dlg.check.title"),
                t("dlg.check.body",
                  price=info.get("lowest_price_raw", lowest),
                  target=f"{target:.2f}"),
                parent=self,
            )
        except Exception as exc:
            if not messagebox.askyesno(
                t("dlg.warn.title"),
                t("dlg.warn.cant_get_price", error=str(exc)),
                parent=self,
            ):
                self._set_status(t("status.cancelled"))
                return

        items = load_json(path, [])
        # Duplicate guard for the buy list only — I want to be reminded
        # that the card is already there so I don't accidentally add it
        # twice. The sell list intentionally allows duplicates: I might
        # be selling 3 copies of the same card and want to track each one.
        if kind == "buy" and any(
                x.get("market_hash_name") == market_hash_name for x in items):
            messagebox.showinfo(
                t("dlg.already.title"),
                t("dlg.already.body", name=pretty),
                parent=self,
            )
            return

        # Resolve game name + poster image. Falls back gracefully — we'd
        # rather add the card than block on Steam Store API being slow.
        try:
            meta = fetch_card_metadata(appid, market_hash_name)
        except Exception:
            meta = {
                "display_name": pretty,
                "game_name": "—",
                "image_url": None,
            }

        # Steam's listing page bakes "<Game name> <item type>" into one
        # string (e.g. "STAR WARS Jedi: Survivor™ Trading Card"). Split
        # so the card-list shows the type in its own column instead of
        # leaking into the Game column.
        clean_game, item_type = split_game_and_type(meta.get("game_name") or "")
        # `imported` is False here because this row came from a manual
        # URL paste, not from the Steam-import sync flow. We persist the
        # field explicitly so the column renders an empty cell rather
        # than the placeholder for a missing field.
        items.append({
            "id": str(uuid.uuid4()),
            "name": market_hash_name,
            "appid": appid,
            "market_hash_name": market_hash_name,
            "display_name": meta["display_name"],
            "game_name": clean_game or (meta.get("game_name") or ""),
            "item_type": item_type,
            "imported": False,
            "image_url": meta.get("image_url"),
            "target_price": target,
            "status": "",
            "last_seen": "—",
        })
        save_json(path, items)
        self._refresh_card_list(kind)
        self._set_status(t("status.added", name=meta["display_name"]))

    def _edit_target(self):
        from steam import pretty_name

        selected = self._require_selection()
        if not selected:
            return
        kind = self._active_kind()
        path = self._kind_path(kind)

        # Pick the dialog text + initial value depending on selection size.
        if len(selected) == 1:
            item = selected[0]
            body = t("dlg.target.body_existing", name=pretty_name(item))
            initial = str(item.get("target_price", ""))
        else:
            body = t("dlg.target.body_multi", count=len(selected))
            # No good single initial value across heterogeneous targets —
            # leave the field empty so the user types one number.
            initial = ""

        new_val = simpledialog.askstring(
            t("dlg.target.title"), body, initialvalue=initial, parent=self,
        )
        if new_val is None:
            return
        try:
            target = float(new_val.replace(",", "."))
        except ValueError:
            messagebox.showerror(t("dlg.error.title"), t("dlg.bad_number_short"), parent=self)
            return

        # Build an id-set so the disk pass is O(N+M) instead of O(N·M).
        # We also memorise old targets per id to drive the self-heal check.
        sel_ids = {item["id"] for item in selected}
        old_targets = {item["id"]: item.get("target_price") for item in selected}

        items = load_json(path, [])
        state = load_json(STATE_PATH, {}) or {}
        state_dirty = False
        for w in items:
            if w.get("id") not in sel_ids:
                continue
            old_target = old_targets.get(w["id"])
            w["target_price"] = target
            # Manual edit clears the "imported from Steam" marker — the
            # price the user just typed is theirs, not Steam's anymore,
            # so the 📥 column should stop claiming otherwise. Re-running
            # import later will re-stamp it if Steam still agrees.
            if w.get("imported"):
                w["imported"] = False
            # Self-heal stale "alerted" status if the new target is stricter.
            # Same rule as watch.py / _check_now: under the new threshold, the
            # previously-alerted price wouldn't qualify anymore.
            if (isinstance(old_target, (int, float))
                    and target < old_target
                    and w.get("status") == "alerted"):
                w["status"] = ""
                state_key = f"{kind}:{w.get('appid')}:{w.get('name')}"
                # Sell may have duplicates sharing the same (appid, name) →
                # only pop state if no other row keeps it alive.
                if state.pop(state_key, None) is not None:
                    state_dirty = True
        save_json(path, items)
        if state_dirty:
            save_json(STATE_PATH, state)
        self._refresh_card_list(kind)

    def _duplicate_card(self):
        """Duplicate selected sell-list row(s) into salelist.

        Salelist allows multiple entries per market_hash_name (one row
        per copy you're listing). The duplicate action gives the user a
        quick way to bulk-grow that: pick one or more existing rows and
        spawn copies of each with either the same target price or a new
        one.

        Per-row dialog (`_ask_duplicate_action`) returns one of:
          * `same`   → append a fresh copy at the source's price.
          * `new`    → ask for a new price, then append.
          * `skip`   → leave this row alone, move to the next selection.
          * `cancel` → abort the whole batch. Already-duplicated rows
                       stay applied — only the unprocessed ones are
                       dropped, matching the import dialog's contract.

        Newly-spawned copies always reset `status="" / last_seen="—"`
        so they read like fresh additions; we don't carry the original's
        "alerted" / "rate_limited" badges into the duplicate (the new
        row is its own thing and should earn its own status from the
        next price poll).
        """
        from steam import pretty_name

        kind = self._active_kind()
        if kind != "sell":
            # Defensive: the button only exists on the sell tab, but
            # don't trust the UI to be the only filter.
            return
        selected = self._require_selection()
        if not selected:
            return

        path = self._kind_path(kind)
        items = load_json(path, []) or []
        new_count = 0
        cancelled = False

        for src in selected:
            name = pretty_name(src)
            cur_price = src.get("target_price")
            choice = self._ask_duplicate_action(name, cur_price)
            if choice == "cancel":
                cancelled = True
                break
            if choice == "skip":
                continue

            # "same" → reuse the source's price; "new" → ask, parse, re-prompt
            # on parse failure (less annoying than a hard error then re-click).
            new_price = cur_price
            if choice == "new":
                initial = (f"{cur_price:.2f}"
                           if isinstance(cur_price, (int, float)) else "")
                ans = simpledialog.askstring(
                    t("dlg.duplicate.title"),
                    t("dlg.duplicate.ask_price", name=name),
                    initialvalue=initial, parent=self,
                )
                if ans is None or ans == "":
                    # User cancelled the price prompt — treat as "skip
                    # this row" rather than aborting the whole batch.
                    continue
                try:
                    new_price = float(ans.replace(",", "."))
                except ValueError:
                    messagebox.showerror(
                        t("dlg.error.title"), t("dlg.bad_number"),
                        parent=self,
                    )
                    continue

            # Spawn the copy. New uuid so the Treeview iid is unique;
            # status / last_seen reset so the duplicate doesn't inherit
            # the original's "alerted" / "rate_limited" tag.
            new_rec = {
                **src,
                "id": str(uuid.uuid4()),
                "target_price": new_price,
                "status": "",
                "last_seen": "—",
            }
            items.append(new_rec)
            new_count += 1

        if new_count:
            save_json(path, items)
        self._refresh_card_list(kind)
        if cancelled:
            log.info("duplicate batch cancelled by user; added %d rows so far",
                     new_count)
        if new_count:
            self._set_status(f"Здубльовано: {new_count}")

    def _ask_duplicate_action(self, name: str, price) -> str:
        """4-button modal: same / new / skip / cancel.

        Same pattern as `_ask_sell_conflict` but with the per-action
        labels and intent of the duplicate flow. Blocks via `wait_window`
        until the user picks one; returns one of the literal strings.
        """
        result = {"choice": "cancel"}  # default if window closed via X

        dlg = tk.Toplevel(self)
        dlg.title(t("dlg.duplicate.title"))
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)

        outer = ttk.Frame(dlg, padding=14)
        outer.pack(fill=BOTH, expand=YES)
        price_fmt = (f"{price:.2f}" if isinstance(price, (int, float))
                     else str(price))
        ttk.Label(
            outer,
            text=t("dlg.duplicate.body", name=name, price=price_fmt),
            wraplength=420, justify=LEFT,
        ).pack(anchor=W, pady=(0, 12))

        def pick(value: str) -> None:
            result["choice"] = value
            try:
                dlg.grab_release()
            except tk.TclError:
                pass
            dlg.destroy()

        # Top row — constructive choices (same / new) in success/info.
        row1 = ttk.Frame(outer)
        row1.pack(fill=X)
        ttk.Button(
            row1, text=t("dlg.duplicate.same"), bootstyle="success",
            command=lambda: pick("same"),
        ).pack(side=LEFT, padx=(0, 6), expand=YES, fill=X)
        ttk.Button(
            row1, text=t("dlg.duplicate.new_price"), bootstyle="info",
            command=lambda: pick("new"),
        ).pack(side=LEFT, expand=YES, fill=X)
        # Bottom row — skip / cancel (neutral / destructive).
        row2 = ttk.Frame(outer)
        row2.pack(fill=X, pady=(6, 0))
        ttk.Button(
            row2, text=t("dlg.duplicate.skip"), bootstyle="secondary",
            command=lambda: pick("skip"),
        ).pack(side=LEFT, padx=(0, 6), expand=YES, fill=X)
        ttk.Button(
            row2, text=t("dlg.duplicate.cancel"), bootstyle="danger",
            command=lambda: pick("cancel"),
        ).pack(side=LEFT, expand=YES, fill=X)

        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_reqheight()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.protocol("WM_DELETE_WINDOW", lambda: pick("cancel"))
        dlg.wait_window()
        return result["choice"]

    def _remove_card(self):
        from steam import pretty_name

        selected = self._require_selection()
        if not selected:
            return
        kind = self._active_kind()
        path = self._kind_path(kind)

        if len(selected) == 1:
            body = t("dlg.delete.body", name=pretty_name(selected[0]))
        else:
            body = t("dlg.delete.body_multi", count=len(selected))
        if not messagebox.askyesno(t("dlg.delete.title"), body, parent=self):
            return

        sel_ids = {item["id"] for item in selected}
        items = load_json(path, [])
        # Drop the selected rows.
        items = [w for w in items if w.get("id") not in sel_ids]
        save_json(path, items)

        # State cleanup: a state key is shared by all duplicates with the
        # same (appid, name). Only drop the key if NO remaining row keeps
        # it alive — otherwise we'd wipe antispam for the surviving copy.
        state = load_json(STATE_PATH, {}) or {}
        survivors = {(w.get("appid"), w.get("name")) for w in items}
        state_dirty = False
        for item in selected:
            ident = (item.get("appid"), item.get("name"))
            if ident in survivors:
                continue
            key = f"{kind}:{ident[0]}:{ident[1]}"
            if state.pop(key, None) is not None:
                state_dirty = True
        if state_dirty:
            save_json(STATE_PATH, state)
        self._refresh_card_list(kind)

    def _check_now(self):
        from steam import pretty_name

        selected = self._require_selection()
        if not selected:
            return
        # «Не перевіряти» rows are excluded even from an explicit manual
        # check — the flag means "leave this one alone, period".
        selected = [w for w in selected if not w.get("no_check")]
        if not selected:
            return
        kind = self._active_kind()
        path = self._kind_path(kind)

        # Dedup the network requests by (appid, market_hash_name) — if the
        # user picked 5 duplicates of the same card in the sell list, we
        # still only hit Steam once.
        unique_cards: dict[tuple, dict] = {}
        for item in selected:
            key = (item.get("appid"), item.get("market_hash_name"))
            unique_cards.setdefault(key, item)

        sel_ids = {item["id"] for item in selected}

        if len(selected) == 1:
            pretty = pretty_name(selected[0])
            self._set_status(t("status.checking", name=pretty))
            log.info(f"GUI: Оновити зараз ({kind}) — {pretty!r}")
        else:
            self._set_status(t("status.checking_multi", count=len(selected)))
            log.info(f"GUI: Оновити зараз ({kind}) — {len(selected)} карток "
                     f"(унікальних: {len(unique_cards)})")

        def _work():
            from steam import get_price, RateLimitedError
            from alerts import evaluate_and_alert
            import time
            t_start = time.monotonic()
            cfg = self.config_data
            currency = cfg.get("market", {}).get("currency", 18)
            country = cfg.get("market", {}).get("country", "UA")

            results: dict[tuple, dict] = {}
            errors: list[str] = []
            first_err = None
            rate_limited = False
            poll_delay = float(cfg.get("market", {}).get("poll_delay_sec", 1.5))
            for i, (key, sample) in enumerate(unique_cards.items()):
                if i > 0:
                    # Same pause that watch.py uses between batch requests.
                    # Configurable via market.poll_delay_sec in config.json.
                    time.sleep(poll_delay)
                try:
                    info = get_price(key[0], key[1], currency, country)
                    results[key] = info
                except RateLimitedError:
                    # Steam said back off — stop the rest of the batch
                    # immediately, surface a clear message to the user.
                    rate_limited = True
                    break
                except Exception as exc:
                    msg = f"{pretty_name(sample)}: {exc}"
                    errors.append(msg)
                    if first_err is None:
                        first_err = str(exc)

            items = load_json(path, []) or []
            state = load_json(STATE_PATH, {}) or {}
            state_dirty = False
            alerted_count = 0
            now_dt = datetime.now(timezone.utc).replace(tzinfo=None)

            # If Steam rate-limited us, write the cooldown stamp now so that
            # watch.py's next scheduled run picks it up and skips politely
            # instead of pounding again.
            if rate_limited:
                deadline = now_dt + timedelta(minutes=30)
                state["__rate_limited_until"] = deadline.isoformat()
                state_dirty = True

            # Telegram / antispam config for the alert-evaluation path.
            tg_cfg = cfg.get("telegram", {})
            token = tg_cfg.get("bot_token", "")
            chat_id = str(tg_cfg.get("chat_id", ""))
            template = (cfg.get("message_template") or "").strip() or t("tg.message.default")
            spam = cfg.get("antispam", {})
            repeat_if_lower = spam.get("repeat_if_lower", True)
            remind_after_hours = spam.get("remind_after_hours", 24)

            for w in items:
                if w.get("id") not in sel_ids:
                    continue
                key = (w.get("appid"), w.get("market_hash_name"))
                info = results.get(key)
                if not info:
                    # No price came back — either Steam was rate-limited or
                    # this card sat in the un-fetched tail after a 429. Tag
                    # the row blue so it stands out. Don't overwrite an
                    # already-alerted (green) badge.
                    if rate_limited and w.get("status") in ("", "error", "rate_limited"):
                        if w.get("status") != "rate_limited":
                            w["status"] = "rate_limited"
                    continue
                w["last_seen"] = info.get(
                    "lowest_price_raw", str(info.get("lowest_price"))
                )
                # Successful fetch — auto-clear a stale "rate_limited" badge.
                if w.get("status") == "rate_limited":
                    w["status"] = ""
                # Self-heal stale "alerted": if the previously-alerted price
                # no longer hits the current target (user lowered target
                # after the alert), wipe stale status + state.
                if w.get("status") == "alerted":
                    target = w.get("target_price")
                    state_key = f"{kind}:{w.get('appid')}:{w.get('name')}"
                    entry = state.get(state_key) or {}
                    last_price = entry.get("last_alerted_price")
                    if (isinstance(target, (int, float))
                            and isinstance(last_price, (int, float))):
                        still_ok = (last_price <= target) if kind == "buy" else (last_price < target)
                        if not still_ok:
                            w["status"] = ""
                            if state.pop(state_key, None) is not None:
                                state_dirty = True

                # Leader suppression (sell only) — mirror of watch.py's
                # rule: with 2+ copies of this card listed and one of
                # them at exactly the market minimum, the user leads the
                # market and the rest of the group stays silent. Row
                # colours are untouched (they're derived from price math
                # at render time); only the alert path is muted.
                if kind == "sell":
                    ident_l = (w.get("appid"), w.get("name"))
                    lowest_now = info.get("lowest_price")
                    group = [x for x in items
                             if (x.get("appid"), x.get("name")) == ident_l]
                    if (len(group) >= 2
                            and isinstance(lowest_now, (int, float))
                            and any(
                                isinstance(x.get("target_price"), (int, float))
                                and abs(x["target_price"] - lowest_now) < 0.005
                                for x in group)):
                        # Same cleanup as watch.py: leadership regained →
                        # stale "сповіщено" badges come off the whole
                        # group and the antispam entry resets, so the
                        # next real undercut fires a fresh first_alert.
                        for x in group:
                            if x.get("status") == "alerted":
                                x["status"] = ""
                        state_key = f"{kind}:{w.get('appid')}:{w.get('name')}"
                        if state.pop(state_key, None) is not None:
                            state_dirty = True
                        log.info(t("log.leader_suppressed",
                                   name=pretty_name(w)[:40],
                                   lowest=lowest_now))
                        continue

                # «Не сповіщати»: price data above is already applied;
                # mirror the would-be alert as a silent "checked" badge
                # and skip the Telegram path entirely.
                if w.get("no_alert"):
                    target_na = w.get("target_price")
                    lowest_na = info.get("lowest_price")
                    if (isinstance(target_na, (int, float))
                            and isinstance(lowest_na, (int, float))):
                        hit = (lowest_na < target_na) if kind == "sell" \
                            else (lowest_na <= target_na)
                        if hit and w.get("status") in ("", "checked"):
                            w["status"] = "checked"
                        elif not hit and w.get("status") == "checked":
                            w["status"] = ""
                    continue

                # Now run the alert evaluation — same logic watch.py uses.
                # Merge the polled price info onto the card record so
                # `info` has both target_price (from card) and lowest_price
                # (from poll) plus all the metadata send_alert wants.
                if token and chat_id:
                    alert_info = {**w, **info}
                    sd, did_alert, did_reset, _reason = evaluate_and_alert(
                        kind=kind, info=alert_info, state=state,
                        token=token, chat_id=chat_id, template=template,
                        repeat_if_lower=repeat_if_lower,
                        remind_after_hours=remind_after_hours,
                        now=now_dt,
                    )
                    if sd:
                        state_dirty = True
                    ident = (w.get("appid"), w.get("name"))
                    if did_alert:
                        alerted_count += 1
                        # Mark every duplicate of this card alerted, not
                        # just the one we walked — matches watch.py.
                        for x in items:
                            if (x.get("appid"), x.get("name")) == ident:
                                if x.get("status") != "alerted":
                                    x["status"] = "alerted"
                    elif did_reset:
                        # Price rebounded above target — clear the stale
                        # "alerted" badge on every duplicate so the row
                        # goes back to plain zebra; next drop will fire
                        # a fresh notification.
                        for x in items:
                            if (x.get("appid"), x.get("name")) == ident:
                                if x.get("status") == "alerted":
                                    x["status"] = ""

            save_json(path, items)
            if state_dirty:
                save_json(STATE_PATH, state)

            # Log the outcome to watch.log so the Журнал tab shows what
            # the user-triggered Refresh actually did — they were missing
            # this feedback before, especially on multi-select where there's
            # no result dialog.
            ok_count = sum(1 for v in results.values() if v is not None)
            elapsed = time.monotonic() - t_start
            alert_suffix = f", сповіщень: {alerted_count}" if alerted_count else ""
            if rate_limited:
                log.error(f"GUI: Оновити зараз ({kind}) — Steam rate-limited; "
                          f"оновлено {ok_count}, відмінено "
                          f"{len(unique_cards) - ok_count}; зайняло {elapsed:.2f}с")
            elif errors:
                log.warning(f"GUI: Оновити зараз ({kind}) — оновлено {ok_count}/"
                            f"{len(unique_cards)} карток{alert_suffix}, "
                            f"помилок: {len(errors)} (перша: {first_err!r}); "
                            f"зайняло {elapsed:.2f}с")
            else:
                log.info(f"GUI: Оновити зараз ({kind}) — успішно, оновлено "
                         f"{ok_count}/{len(unique_cards)} карток{alert_suffix}; "
                         f"зайняло {elapsed:.2f}с")

            # UX after fetch:
            # - rate-limited → loud warning dialog (this is THE explanation
            #   for "why didn't prices update?", deserves the modal);
            # - single card OK → detailed price dialog (legacy behaviour);
            # - multi → status-bar summary.
            if rate_limited:
                self.after(0, lambda: messagebox.showwarning(
                    t("dlg.rate_limited.title"),
                    t("dlg.rate_limited.body", minutes=30),
                    parent=self,
                ))
            elif len(selected) == 1 and not errors:
                key = (selected[0].get("appid"), selected[0].get("market_hash_name"))
                info = results.get(key, {})
                pretty = pretty_name(selected[0])
                body = t(
                    "dlg.current_price.body",
                    name=pretty,
                    price=info.get("lowest_price_raw", info.get("lowest_price")),
                    volume=info.get("volume", "—"),
                )
                self.after(0, lambda: messagebox.showinfo(
                    t("dlg.current_price.title"), body, parent=self))
            elif len(selected) == 1 and errors:
                self.after(0, lambda: messagebox.showerror(
                    t("dlg.error.title"), first_err, parent=self))
            else:
                # Brief status-bar feedback — exact prices are visible in the
                # refreshed table.
                done = len(selected) - len(errors)
                self.after(0, lambda d=done: self._set_status(
                    t("status.updated_n", count=d)))

            self.after(0, lambda k=kind: self._refresh_card_list(k))
            if len(selected) == 1:
                self.after(0, lambda: self._set_status(t("status.ready")))

        threading.Thread(target=_work, daemon=True).start()

    def _mark_completed(self):
        """User confirms the transaction(s) — buy or sell, depending on tab.

        Group selected rows by card identity (appid, name):
        - Same card in N rows → one dialog applies one price to all copies
          (typical bulk: sold 5 copies at the same price).
        - Different cards → one dialog per unique card, in sequence;
          Cancel on a dialog skips THAT card (its rows stay in the list
          untouched) and moves on to the next.
        """
        from steam import pretty_name

        selected = self._require_selection()
        if not selected:
            return
        kind = self._active_kind()  # "buy" or "sell"
        path = self._kind_path(kind)
        sym = self._currency_symbol()
        closed_status = "bought" if kind == "buy" else "sold"
        ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

        # Group by card identity. Preserve insertion order so the dialog
        # sequence matches what the user expects from top-to-bottom.
        groups: dict[tuple, list[dict]] = {}
        for item in selected:
            ident = (item.get("appid"), item.get("name"))
            groups.setdefault(ident, []).append(item)

        # Walk groups, ask per-group; collect closed ids + purchase rows.
        body_key_single = ("dlg.completed.body_buy" if kind == "buy"
                           else "dlg.completed.body_sell")
        body_key_multi = ("dlg.completed.body_buy_multi" if kind == "buy"
                          else "dlg.completed.body_sell_multi")

        closed_ids: set = set()
        new_purchases: list[dict] = []
        for ident, group in groups.items():
            sample = group[0]
            pretty = pretty_name(sample)
            if len(group) == 1:
                body = t(body_key_single, name=pretty, sym=sym)
                target = sample.get("target_price")
                default_str = (f"{target:.2f}" if isinstance(target, (int, float))
                               else str(target or ""))
            else:
                body = t(body_key_multi, count=len(group), sym=sym)
                # Heterogeneous targets across duplicates are unusual but
                # possible — leave the field empty rather than guess.
                default_str = ""
            price_str = simpledialog.askstring(
                t("dlg.completed.title"), body,
                initialvalue=default_str, parent=self,
            )
            if price_str is None:
                # Cancel → skip this card entirely, keep going.
                continue
            try:
                price_val = float(price_str.replace(",", "."))
            except ValueError:
                # Bad number on this card → show error, skip it.
                messagebox.showerror(
                    t("dlg.error.title"), t("dlg.bad_number"), parent=self,
                )
                continue
            price_formatted = f"{price_val:.2f} {sym}".rstrip()
            for item in group:
                closed_ids.add(item["id"])
                new_purchases.append({
                    "name": item.get("name"),
                    "display_name": item.get("display_name") or item.get("name"),
                    "game_name": item.get("game_name"),
                    "image_url": item.get("image_url"),
                    "appid": item.get("appid"),
                    "market_hash_name": item.get("market_hash_name"),
                    "price": price_formatted,
                    "target": item.get("target_price"),
                    "operation": kind,
                    "timestamp": ts,
                })

        if not closed_ids:
            # Either the user cancelled everything or all groups errored out.
            return

        items = load_json(path, [])
        purchases = load_json(PURCHASES_PATH, [])
        for w in items:
            if w.get("id") in closed_ids:
                w["status"] = closed_status
        purchases.extend(new_purchases)
        save_json(path, items)
        save_json(PURCHASES_PATH, purchases)
        self._refresh_card_list(kind)
        self._refresh_history()

    def _mark_not_bought(self):
        selected = self._require_selection()
        if not selected:
            return
        # Only touch the alerted ones — the rest don't need a status reset.
        targets = [x for x in selected if x.get("status") == "alerted"]
        if not targets:
            return
        kind = self._active_kind()
        path = self._kind_path(kind)
        items = load_json(path, [])
        state = load_json(STATE_PATH, {}) or {}
        sel_ids = {x["id"] for x in targets}
        for w in items:
            if w.get("id") in sel_ids:
                w["status"] = ""
        # State key is shared by all duplicates with the same (appid, name).
        # Only drop the key if NO remaining alerted duplicate keeps it alive.
        remaining_alerted = {
            (w.get("appid"), w.get("name"))
            for w in items
            if w.get("status") == "alerted"
        }
        for x in targets:
            ident = (x.get("appid"), x.get("name"))
            if ident in remaining_alerted:
                continue
            state.pop(f"{kind}:{ident[0]}:{ident[1]}", None)
        save_json(path, items)
        save_json(STATE_PATH, state)
        self._refresh_card_list(kind)

    def _import_from_steam(self):
        """Open the Steam-import dialog scoped to the current tab's list.

        The list we're looking at decides which side of Steam we ask for:

          * Purchase tab → only buy orders (watchlist sync target).
          * Sales tab    → only sale listings (salelist sync target).

        Pre-flight: we need cookies. Tier 3 (manual ID) leaves
        `steam.cookies` empty; without an authenticated session we'd
        just bounce off Steam's login page, so we point the user at
        the right Settings button instead.
        """
        kind = self._active_kind()
        if kind not in ("buy", "sell"):
            # Button only exists on card-list tabs, but be defensive.
            return

        cookies = (self.config_data.get("steam") or {}).get("cookies")
        community = (cookies or {}).get("steamcommunity.com") or {}
        if "steamLoginSecure" not in community:
            messagebox.showinfo(
                t("dlg.import.title"),
                t("dlg.import.no_cookies"),
                parent=self,
            )
            return

        # Refuse double-stack — the dialog mutates instance state
        # (`_import_*`) that re-entry would tangle.
        existing = getattr(self, "_import_dlg", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        self._open_import_dialog(cookies, kind)

    def _open_import_dialog(self, cookies: dict, kind: str) -> None:
        """Build the import Toplevel and start the background fetch.

        `kind` controls which side of Steam we'll surface — "buy" pulls
        only buy orders, "sell" pulls only sale listings. Modal-ish via
        `transient` + `grab_set`; the actual fetch runs on a daemon
        thread so the UI stays responsive while Steam responds (a slow
        market home page can take 2-3 sec).
        """
        self._import_kind = kind
        dlg = tk.Toplevel(self)
        self._import_dlg = dlg
        dlg.title(t("dlg.import.title"))
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("780x520")

        outer = ttk.Frame(dlg, padding=12)
        outer.pack(fill=BOTH, expand=YES)

        # Status line at the top — narrow, no expand. Used during the
        # loading phase to tell the user we're talking to Steam, and
        # for terminal-error messages ("session expired", "nothing
        # found", etc.). `wraplength` is dynamically resized in the
        # <Configure> handler below so long messages wrap to dialog
        # width instead of being clipped at the right edge.
        self._import_status = ttk.Label(
            outer, text=t("dlg.import.loading"),
            wraplength=600, justify="left",
        )
        self._import_status.pack(side=TOP, anchor=W, pady=(0, 8), fill=X)
        # Track dialog resize → keep wraplength ~ dialog_width − 2×padding.
        # Bound on the dialog so widget moves inside `outer` don't fire it.
        def _resize_status(_e):
            try:
                width = max(200, dlg.winfo_width() - 40)
                self._import_status.configure(wraplength=width)
            except tk.TclError:
                pass
        dlg.bind("<Configure>", _resize_status)

        # Button row BEFORE the content area — packing it with side=BOTTOM
        # reserves its slot first so the expanding section above can't push
        # it off-screen on a tall payload (18 listings + 1 buy order would
        # otherwise need a much taller dialog before the buttons came back
        # into view). Same trick the main UI uses for the status bar vs
        # notebook.
        btn_row = ttk.Frame(outer)
        btn_row.pack(side=BOTTOM, fill=X, pady=(10, 0))
        # "Cancel" on the left in danger-red — easy to bail out without
        # confusing it with the success-coloured apply button.
        ttk.Button(
            btn_row, text=t("dlg.import.btn_cancel"),
            bootstyle="danger",
            command=self._close_import_dialog,
        ).pack(side=LEFT)
        # Apply button starts disabled; `_on_import_selection_change`
        # flips it on as soon as something gets ticked. Success bootstyle
        # = green, the obvious "go" colour.
        self._import_apply_btn = ttk.Button(
            btn_row, text=t("dlg.import.btn_import"),
            bootstyle="success", state=DISABLED,
            command=self._apply_import_selection,
        )
        self._import_apply_btn.pack(side=RIGHT)

        # Content area fills the remaining space between status and buttons.
        self._import_content = ttk.Frame(outer)
        self._import_content.pack(side=TOP, fill=BOTH, expand=YES)

        dlg.protocol("WM_DELETE_WINDOW", self._close_import_dialog)

        # Centre over parent. update_idletasks first so reqsize is real.
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 4
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

        # We render exactly one Treeview — the side of Steam matching
        # the active tab. Stored on self so the apply step can walk it.
        self._import_tree: ttk.Treeview | None = None
        # iid → fetched dict, populated when the section is built.
        self._import_rows: dict[str, dict] = {}

        # Kick off the fetch. Whichever side isn't relevant for `kind`
        # we skip — saves a network request and keeps the UI focused
        # on the one operation the user actually clicked.
        def worker() -> None:
            import steam
            err: str | None = None
            payload: list[dict] = []
            try:
                if kind == "sell":
                    payload = steam.fetch_market_listings(cookies)
                else:
                    payload = steam.fetch_buy_orders(cookies)
            except steam.SteamSessionExpired:
                # Specific: tell the user it's a session problem (not
                # "you have no listings") AND raise the global toast
                # + ⚠ badge so they have a single click to relogin.
                err = t("import.session_expired")
                log.warning("import aborted — community session expired")
                self.after(0, lambda: self._set_session_warning(True))
            except Exception as e:
                err = str(e)
                log.exception("import fetch failed")
            self.after(0, lambda: self._import_render_section(payload, err))

        threading.Thread(target=worker, daemon=True).start()

    def _close_import_dialog(self) -> None:
        """Tear down the import dialog + clear the instance ref."""
        dlg = getattr(self, "_import_dlg", None)
        if dlg is None:
            return
        try:
            dlg.grab_release()
        except tk.TclError:
            pass
        self._import_dlg = None
        try:
            dlg.destroy()
        except tk.TclError:
            pass

    def _import_render_section(self, rows: list[dict],
                               err: str | None) -> None:
        """Populate the dialog with a single CheckListBox-like Treeview.

        Called on the Tk main thread once the fetch worker returns.
        Three terminal states:

          * error → status line goes red, no section rendered.
          * empty → status line says "nothing to import".
          * data  → status line clears; the section appears with rows
                    pre-selected when importing them would actually do
                    something. "same" rows stay deselected (no-op).

        Status classification differs slightly for sales vs buys:

          * Buys (watchlist — one entry per mhn): plain match on
            (appid, mhn). Same target → "same", different → "changed".
          * Sales (salelist — duplicates allowed): consume one local
            row per Steam row. Local copy with exact-price match is
            preferred → "same"; else first remaining local at a
            different price → "changed"; otherwise → "new". This
            handles "I have 2 copies at P, Steam has 3 at P": the
            third Steam row correctly classifies as "new" so the
            user can pick it up.
        """
        if not self._import_dlg or not self._import_dlg.winfo_exists():
            # User cancelled during fetch.
            return

        if err:
            self._import_status.configure(
                text=t("dlg.import.network_error", err=err),
                foreground=self.style.colors.danger,
            )
            return
        if not rows:
            self._import_status.configure(
                text=t("dlg.import.nothing_found"),
                foreground=self.style.colors.warning,
            )
            return

        # Greyed-out status — done loading; real action is in the section.
        self._import_status.configure(
            text="", foreground=self.style.colors.secondary,
        )

        kind = self._import_kind
        local_rows = load_json(
            SALELIST_PATH if kind == "sell" else WATCHLIST_PATH, []
        ) or []

        if kind == "sell":
            self._classify_sales(rows, local_rows)
            title = t("dlg.import.section_sales", count=len(rows))
        else:
            self._classify_buys(rows, local_rows)
            title = t("dlg.import.section_buy", count=len(rows))

        self._import_tree = self._build_import_section(
            self._import_content,
            title=title, rows=rows, row_store=self._import_rows,
        )
        # Wire the apply button to follow the selection state.
        self._import_tree.bind("<<TreeviewSelect>>",
                               self._on_import_selection_change)
        # Make sure the initial pre-selection enables the button.
        self._on_import_selection_change()

    @staticmethod
    def _classify_buys(steam_rows: list[dict],
                       local_rows: list[dict]) -> None:
        """Annotate each Steam buy-order with `_import_status` in place.

        Buy orders are unique-per-mhn in the watchlist, so the match
        rule is simple: any local entry with the same (appid, mhn)
        wins; price comparison gives us "same" vs "changed:<old>".
        """
        for s in steam_rows:
            appid, mhn = s["appid"], s["market_hash_name"]
            price = s.get("price")
            match = next(
                (r for r in local_rows
                 if r.get("appid") == appid
                 and (r.get("market_hash_name") or r.get("name")) == mhn),
                None,
            )
            if match is None:
                s["_import_status"] = "new"
                continue
            old = match.get("target_price")
            if old is not None and price is not None \
                    and abs(float(old) - float(price)) < 0.005:
                s["_import_status"] = "same"
            else:
                s["_import_status"] = f"changed:{old}"

    @staticmethod
    def _classify_sales(steam_rows: list[dict],
                        local_rows: list[dict]) -> None:
        """Annotate Steam sale listings with `_import_status` in place.

        Count-aware: salelist allows multiple copies of the same card,
        so for each Steam listing we try to consume one matching local
        copy. Exact-price matches consume first, then any remaining
        local at a different price counts as "changed", then the
        leftover Steam rows are flagged "new".

        Example: Steam shows 3 copies of card X at price P; local has
        2 copies at P. Two Steam rows match "same" and consume both
        locals; the third Steam row finds nothing left → "new".
        Importing that last row brings local up to 3, mirroring Steam.
        """
        # Build a working list per mhn we can pop from as matches consume.
        # `defaultdict(list)` would be cleaner but introducing the import
        # for one use site isn't worth the line.
        avail: dict[tuple, list[dict]] = {}
        for r in local_rows:
            key = (r.get("appid"),
                   r.get("market_hash_name") or r.get("name"))
            avail.setdefault(key, []).append(r)

        for s in steam_rows:
            key = (s["appid"], s["market_hash_name"])
            price = s.get("price")
            pool = avail.get(key, [])
            if not pool:
                s["_import_status"] = "new"
                continue
            # Prefer an exact-price match so a "same" doesn't get
            # consumed by a "changed" that came first in the list.
            chosen_idx = None
            for i, r in enumerate(pool):
                old = r.get("target_price")
                if old is not None and price is not None \
                        and abs(float(old) - float(price)) < 0.005:
                    chosen_idx = i
                    break
            if chosen_idx is not None:
                pool.pop(chosen_idx)
                s["_import_status"] = "same"
            else:
                old = pool.pop(0).get("target_price")
                s["_import_status"] = f"changed:{old}"

    def _on_import_selection_change(self, _event=None) -> None:
        """Enable/disable the apply button based on row selection state.

        Apply is meaningful only when the user has at least one row
        ticked — otherwise the import would be a no-op and the green
        button would be lying about what it does.
        """
        if self._import_tree is None or not self._import_apply_btn:
            return
        has_selection = bool(self._import_tree.selection())
        self._import_apply_btn.configure(
            state=NORMAL if has_selection else DISABLED,
        )

    def _build_import_section(self, parent, *, title: str,
                              rows: list[dict], row_store: dict[str, dict]):
        """Build the one Treeview section that lives inside the dialog.

        Visual contract:
          [LabelFrame: <title>]
            [Toolbar] [Select all] [Clear all]
            [Treeview: Card | Game | Price | Status] + vertical scrollbar

        Multi-select Treeview (`selectmode="extended"`) — the "checked"
        rows are simply the selected rows. Rows whose status is "new"
        or "changed:" get pre-selected because importing them does
        something; "same" rows stay deselected (they'd be a no-op).

        Ctrl+A → select every row, same convention as the other tabs.
        """
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.pack(fill=BOTH, expand=YES, pady=(0, 8))

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=X, pady=(0, 4))

        cols = ("card", "game", "price", "status")
        col_headings = (
            t("dlg.import.col_card"),
            t("dlg.import.col_game"),
            t("dlg.import.col_price"),
            t("dlg.import.col_status"),
        )
        tree = ttk.Treeview(
            frame, columns=cols, show="headings", selectmode="extended",
            height=min(12, max(3, len(rows))),
        )
        for c, h in zip(cols, col_headings):
            tree.heading(c, text=h)
        tree.column("card", width=240, anchor=W)
        tree.column("game", width=180, anchor=W)
        tree.column("price", width=80, anchor=E)
        tree.column("status", width=180, anchor=W)

        # Match the green-thumb scrollbars used in the main card-list
        # tabs. `bootstyle="success"` tints the ttkbootstrap-rendered
        # thumb image — plain `style.configure` doesn't reach it because
        # ttkbootstrap paints thumbs from PhotoImage assets.
        vsb = ttk.Scrollbar(frame, orient=VERTICAL, command=tree.yview,
                            bootstyle="success")
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=LEFT, fill=BOTH, expand=YES)
        vsb.pack(side=RIGHT, fill=Y)

        # "same"-status rows render in the muted secondary colour so the
        # user can tell at a glance which rows are duplicates (no-op on
        # import). Configured here once, applied via tags on insert.
        tree.tag_configure("muted",
                           foreground=self.style.colors.secondary)

        pre_select: list[str] = []
        for r in rows:
            stat = r.get("_import_status", "new")
            if stat == "new":
                stat_text = t("dlg.import.status.new")
            elif stat == "same":
                stat_text = t("dlg.import.status.same")
            elif stat.startswith("changed:"):
                old = stat.split(":", 1)[1]
                stat_text = t(
                    "dlg.import.status.changed",
                    old=f"{float(old):.2f}" if old not in (None, "None") else "—",
                    new=f"{r['price']:.2f}",
                )
            else:
                stat_text = stat
            row_tags = ("muted",) if stat == "same" else ()
            iid = tree.insert("", "end", values=(
                r.get("display_name") or r.get("market_hash_name") or "?",
                r.get("game_name") or "",
                r.get("price_raw") or f"{r.get('price', 0):.2f}",
                stat_text,
            ), tags=row_tags)
            row_store[iid] = r
            if stat == "new" or stat.startswith("changed:"):
                pre_select.append(iid)

        if pre_select:
            tree.selection_set(*pre_select)

        # Ctrl+A — same handler as the main card-list / history trees,
        # uses event.keycode so it works on Cyrillic layouts too.
        tree.bind("<Control-KeyPress>", self._on_tree_ctrl_a)

        # Select all / clear all buttons use the same iids we just inserted.
        ttk.Button(
            toolbar, text=t("dlg.import.toggle_all"), bootstyle="link",
            command=lambda tr=tree: tr.selection_set(*tr.get_children()),
        ).pack(side=LEFT)
        ttk.Button(
            toolbar, text=t("dlg.import.toggle_none"), bootstyle="link",
            command=lambda tr=tree: tr.selection_remove(*tr.get_children()),
        ).pack(side=LEFT, padx=(8, 0))

        return tree

    def _apply_import_selection(self) -> None:
        """Walk the tree, persist additions/updates, close the dialog.

        Decision matrix per row, depending on `_import_status`:

          Buys (watchlist — deduped by mhn):
            * "new"          → append fresh row.
            * "same"         → silently skip (no-op even if selected).
            * "changed:<old>"→ modal Yes/No "Replace target X → Y?".
                               Yes updates the existing row's
                               target_price; No leaves it alone.

          Sales (salelist — duplicates allowed):
            * "new"          → append fresh row.
            * "same"         → silently skip.
            * "changed:<old>"→ modal per-row 4-button dialog:
                * Replace → update existing row's target.
                * Add +1  → append as a fresh copy (so we end up with
                            one more local row at the Steam price).
                * Skip    → no-op for this row.
                * Cancel  → abort the rest of the import. Already-
                            decided rows stay applied.
        """
        if self._import_tree is None:
            return

        kind = self._import_kind
        path = SALELIST_PATH if kind == "sell" else WATCHLIST_PATH
        rows = load_json(path, []) or []
        added = updated = 0
        cancelled = False

        for iid in self._import_tree.selection():
            src = self._import_rows[iid]
            stat = src.get("_import_status", "new")

            if stat == "new":
                rows.append(self._import_make_record(src))
                added += 1
                continue
            if stat == "same":
                # Already in sync — even a selected "same" is a no-op
                # by design, so the user can't accidentally double-add.
                continue

            # stat is "changed:<old>"
            old_target = stat.split(":", 1)[1]
            try:
                old_fmt = f"{float(old_target):.2f}"
            except (TypeError, ValueError):
                old_fmt = str(old_target)
            new_fmt = f"{src.get('price', 0):.2f}"
            name = src.get("display_name") or src.get("market_hash_name") or "?"

            if kind == "buy":
                # Yes/No: replace the target or leave it alone.
                if messagebox.askyesno(
                    t("dlg.import.conflict.title"),
                    t("dlg.import.conflict.buy_body",
                      name=name, old=old_fmt, new=new_fmt),
                    parent=self._import_dlg,
                ):
                    self._import_update_target(rows, src)
                    updated += 1
                # else: skip silently.
                continue

            # kind == "sell" → 4-button modal.
            choice = self._ask_sell_conflict(name, old_fmt, new_fmt)
            if choice == "replace":
                self._import_update_target(rows, src)
                updated += 1
            elif choice == "add":
                rows.append(self._import_make_record(src))
                added += 1
            elif choice == "skip":
                pass  # silent skip — selected but user vetoed
            elif choice == "cancel":
                cancelled = True
                break

        save_json(path, rows)
        # Both card-list panes share `_refresh_watchlist` because either
        # of them might have grown rows that need to render.
        self._refresh_watchlist()
        self._close_import_dialog()

        # Summary uses the same keys for both kinds; the irrelevant pair
        # stays at zero, which reads cleanly enough.
        if kind == "sell":
            sell_new, sell_upd, buy_new, buy_upd = added, updated, 0, 0
        else:
            sell_new, sell_upd, buy_new, buy_upd = 0, 0, added, updated
        messagebox.showinfo(
            t("dlg.import.title"),
            t("dlg.import.summary",
              sell_new=sell_new, sell_upd=sell_upd,
              buy_new=buy_new, buy_upd=buy_upd),
            parent=self,
        )
        if cancelled:
            # Soft note that the import didn't finish. No-op when the
            # user explicitly clicked "Cancel import" — they already
            # know — but easy to skip if it gets noisy in feedback.
            log.info("import cancelled by user mid-flow")

    def _ask_sell_conflict(self, name: str, old: str, new: str) -> str:
        """Modal 4-button conflict dialog for sale-side imports.

        Returns one of:
          * "replace" → update the existing salelist row's price.
          * "add"     → add a new copy with the Steam price.
          * "skip"    → leave both alone, move on to the next row.
          * "cancel"  → bail out of the import; already-applied rows stay.

        Custom Toplevel because `messagebox` tops out at 3 buttons. The
        dialog grabs focus and blocks the calling apply loop until the
        user picks one — `wait_window` does the heavy lifting.
        """
        result = {"choice": "cancel"}  # default if window is closed via X

        dlg = tk.Toplevel(self._import_dlg or self)
        dlg.title(t("dlg.import.conflict.title"))
        dlg.transient(self._import_dlg or self)
        dlg.grab_set()
        dlg.resizable(False, False)

        outer = ttk.Frame(dlg, padding=14)
        outer.pack(fill=BOTH, expand=YES)
        ttk.Label(
            outer,
            text=t("dlg.import.conflict.sell_body",
                   name=name, old=old, new=new),
            wraplength=420, justify=LEFT,
        ).pack(anchor=W, pady=(0, 12))

        def pick(value: str) -> None:
            result["choice"] = value
            try:
                dlg.grab_release()
            except tk.TclError:
                pass
            dlg.destroy()

        # Button row. Top row holds the "constructive" choices (replace
        # / add) in success/info colours; bottom row carries the
        # "destructive-ish" ones (skip / cancel).
        row1 = ttk.Frame(outer)
        row1.pack(fill=X)
        ttk.Button(
            row1, text=t("dlg.import.conflict.replace"), bootstyle="success",
            command=lambda: pick("replace"),
        ).pack(side=LEFT, padx=(0, 6), expand=YES, fill=X)
        ttk.Button(
            row1, text=t("dlg.import.conflict.add_one"), bootstyle="info",
            command=lambda: pick("add"),
        ).pack(side=LEFT, expand=YES, fill=X)
        row2 = ttk.Frame(outer)
        row2.pack(fill=X, pady=(6, 0))
        ttk.Button(
            row2, text=t("dlg.import.conflict.skip"), bootstyle="secondary",
            command=lambda: pick("skip"),
        ).pack(side=LEFT, padx=(0, 6), expand=YES, fill=X)
        ttk.Button(
            row2, text=t("dlg.import.conflict.cancel"), bootstyle="danger",
            command=lambda: pick("cancel"),
        ).pack(side=LEFT, expand=YES, fill=X)

        # Centre + focus + block until decision.
        dlg.update_idletasks()
        px = (self._import_dlg or self).winfo_rootx()
        py = (self._import_dlg or self).winfo_rooty()
        pw = (self._import_dlg or self).winfo_width()
        ph = (self._import_dlg or self).winfo_height()
        x = px + (pw - dlg.winfo_reqwidth()) // 2
        y = py + (ph - dlg.winfo_reqheight()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.protocol("WM_DELETE_WINDOW", lambda: pick("cancel"))
        dlg.wait_window()
        return result["choice"]

    @staticmethod
    def _import_make_record(src: dict) -> dict:
        """Turn a Steam-side dict into a watchlist/salelist entry.

        `image_url` is intentionally `None` — the GUI's metadata
        backfill thread (`_backfill_metadata`) will fill it in on the
        next run, so we don't slow down the import by hitting Steam
        Store once per imported card.

        `imported` and `item_type` are the new fields surfaced as their
        own columns in the card-list tab; we set them here so synced
        rows render with both a 📥 marker AND a split-out "type" cell
        from the get-go.
        """
        from steam import split_game_and_type
        mhn = src.get("market_hash_name") or ""
        # Steam's market_listing_game_name smushes "game — type" or
        # "game type" into one string; split it so the tab can show
        # them in separate columns.
        game, item_type = split_game_and_type(src.get("game_name") or "")
        return {
            "id":               str(uuid.uuid4()),
            "name":             mhn,             # legacy alias
            "appid":            src.get("appid"),
            "market_hash_name": mhn,
            "display_name":     src.get("display_name") or "",
            "game_name":        game or (src.get("game_name") or ""),
            "item_type":        item_type,
            "imported":         True,
            "image_url":        None,
            "target_price":     src.get("price"),
            "status":           "",
            "last_seen":        "—",
        }

    @staticmethod
    def _import_update_target(rows: list[dict], src: dict) -> None:
        """Replace the first row matching `src` with fresh Steam data.

        Updates target_price plus the import-side metadata (item_type,
        cleaned game_name) and stamps `imported=True` so the 📥 marker
        in the card-list shows up regardless of whether this row was
        originally manually added or already imported. The "imported"
        flag is a "this row's price came from Steam" claim — a Steam-
        driven update qualifies just as much as a Steam-driven insert.

        The flag gets cleared the moment the user manually edits the
        target via «Змінити ціль» — see `_edit_target` for that side.
        """
        from steam import split_game_and_type
        game, item_type = split_game_and_type(src.get("game_name") or "")
        appid = src.get("appid")
        mhn = src.get("market_hash_name") or ""
        new_price = src.get("price")
        for r in rows:
            if r.get("appid") == appid and \
                    (r.get("market_hash_name") or r.get("name")) == mhn:
                r["target_price"] = new_price
                r["imported"] = True
                r["item_type"] = item_type
                # Refresh game_name if Steam has a cleaner version
                # (older manually-added rows often have "—" here).
                if game:
                    r["game_name"] = game
                return

    def _move_to_other_list(self):
        """Move selected card(s) from the active list to the other one.

        Use case: cards I was watching to buy turn into things I now
        want to sell (or vice versa). Metadata (display_name, game_name,
        image_url, last_seen) travels with each card; status resets to ""
        and the user picks one fresh target price applied to the whole
        selection. Source antispam state is cleared so the destination
        tracks from scratch.
        """
        from steam import pretty_name

        selected = self._require_selection()
        if not selected:
            return
        src_kind = self._active_kind()
        if src_kind not in ("buy", "sell"):
            return
        dest_kind = "sell" if src_kind == "buy" else "buy"
        sym = self._currency_symbol()

        if len(selected) == 1:
            item = selected[0]
            body_key = ("dlg.move.body_to_sell" if dest_kind == "sell"
                        else "dlg.move.body_to_buy")
            body = t(body_key, name=pretty_name(item), sym=sym)
            current = item.get("target_price")
            default_str = (f"{current:.2f}" if isinstance(current, (int, float))
                           else str(current or ""))
        else:
            body_key = ("dlg.move.body_to_sell_multi" if dest_kind == "sell"
                        else "dlg.move.body_to_buy_multi")
            body = t(body_key, count=len(selected), sym=sym)
            default_str = ""

        new_target_str = simpledialog.askstring(
            t("dlg.move.title"), body,
            initialvalue=default_str, parent=self,
        )
        if new_target_str is None:
            return
        try:
            new_target = float(new_target_str.replace(",", "."))
        except ValueError:
            messagebox.showerror(t("dlg.error.title"), t("dlg.bad_number"), parent=self)
            return

        src_path = self._kind_path(src_kind)
        dest_path = self._kind_path(dest_kind)
        src_items = load_json(src_path, []) or []
        dest_items = load_json(dest_path, []) or []
        state = load_json(STATE_PATH, {}) or {}

        sel_ids = {item["id"] for item in selected}
        moved_idents: set[tuple] = set()
        for item in selected:
            moved_idents.add((item.get("appid"), item.get("name")))
            mhn = item.get("market_hash_name")
            moved_entry = {
                "id": str(uuid.uuid4()),
                "name": item.get("name"),
                "appid": item.get("appid"),
                "market_hash_name": mhn,
                "display_name": item.get("display_name"),
                "game_name": item.get("game_name"),
                "image_url": item.get("image_url"),
                "target_price": new_target,
                "status": "",
                "last_seen": item.get("last_seen", "—"),
            }
            # Buy-side dest: no duplicates allowed — merge metadata into
            # an existing row instead of appending. Sell-side dest: always
            # append (duplicates are the whole point of this list now).
            existing = (next(
                (x for x in dest_items if x.get("market_hash_name") == mhn),
                None,
            ) if dest_kind == "buy" else None)
            if existing is not None:
                existing.update(moved_entry)
                # Don't overwrite the existing record's id with the new uuid —
                # keep the dest-side id stable so any selection sticks.
                existing["id"] = existing.get("id") or moved_entry["id"]
            else:
                dest_items.append(moved_entry)

        # Drop moved rows from source by id (id is unique even across dup mhn).
        src_items = [x for x in src_items if x.get("id") not in sel_ids]

        # Source state cleanup: a state key is shared across duplicates of the
        # same (appid, name); only drop it if NO surviving source row keeps
        # it alive.
        src_survivors = {(w.get("appid"), w.get("name")) for w in src_items}
        for ident in moved_idents:
            if ident in src_survivors:
                continue
            state.pop(f"{src_kind}:{ident[0]}:{ident[1]}", None)

        save_json(STATE_PATH, state)
        save_json(src_path, src_items)
        save_json(dest_path, dest_items)

        self._refresh_card_list(src_kind)
        self._refresh_card_list(dest_kind)

        dest_label = t("tab.sales" if dest_kind == "sell" else "tab.purchase").strip()
        if len(selected) == 1:
            self._set_status(t("status.moved",
                               name=pretty_name(selected[0]), dest=dest_label))
        else:
            self._set_status(t("status.moved_multi",
                               count=len(selected), dest=dest_label))

    # ---- Settings --------------------------------------------------------

    def _build_settings_tab(self):
        """Two-column form for app settings.

        Left column: text/dropdown form fields (token, chat ID, theme,
        language, font scale). Standard "label → input" alignment.
        Right column: numeric/short fields (currency, country, interval,
        poll delay, antispam). Inverse "input → label" alignment so the
        small spinboxes and dropdowns sit closer to the page centre and
        leave room for the labels on the right edge.

        Below the two columns: full-width template editor. Variables and
        HTML cheatsheet are split into two separate lines for readability.

        Bottom: Steam-login button on the left, Test-message + "Telegram"
        anchor label on the right, then a centred Save / Reset row.
        """
        cfg = self.config_data
        tg = cfg.get("telegram", {})
        mkt = cfg.get("market", {})
        sched = cfg.get("schedule", {})
        ui = cfg.get("ui", {})
        spam = cfg.get("antispam", {})

        outer = ttk.Frame(self.tab_settings, padding=12)
        outer.pack(fill=BOTH, expand=YES)

        # ---- Two-column form (top) -----------------------------------
        cols = ttk.Frame(outer)
        cols.pack(fill=X, pady=(0, 12))
        # weight=0 on each column — fixed-width left/right blocks with
        # a stretchy gap (col 2) absorbing extra horizontal space at
        # bigger window widths. Keeps fields tight against their labels.
        cols.columnconfigure(0, weight=0)   # left labels
        cols.columnconfigure(1, weight=0)   # left fields
        cols.columnconfigure(2, weight=1)   # gap absorber
        cols.columnconfigure(3, weight=0)   # right fields
        cols.columnconfigure(4, weight=0)   # right labels

        def left_label(label_key, row_idx):
            ttk.Label(cols, text=t(label_key), anchor=E
                      ).grid(row=row_idx, column=0, sticky=E,
                             pady=4, padx=(0, 8))

        def right_label(label_key, row_idx, **kwargs):
            # Strip the trailing colon — the right column reads
            # "value → label" so the colon on the label would face
            # away from the input it describes. Left column keeps its
            # colons (label → value, colons stay).
            text = t(label_key).rstrip(":").rstrip()
            ttk.Label(cols, text=text, anchor=W
                      ).grid(row=row_idx, column=4, sticky=W,
                             pady=4, padx=(8, 0), **kwargs)

        # ---- LEFT column ---------------------------------------------
        # Token + Chat ID are readonly + masked by default — protects
        # against accidental edits AND keeps the secret value off-screen
        # for over-the-shoulder situations. Click ✏ to reveal + edit.
        # Token + Chat ID rows use a slightly different layout from the
        # rows below: the Entry itself sits in col 1 (right-aligned, so
        # its right edge matches the comboboxes underneath), and the ✏
        # edit button sits in col 2 (the gap absorber). That way the
        # button "leaks" past the virtual right-edge line — the user
        # asked for that explicitly: line up inputs, but the pencil
        # icons may overhang.
        left_label("lbl.bot_token", 0)
        self.var_token = tk.StringVar(value=tg.get("bot_token", ""))
        self.entry_token = ttk.Entry(
            cols, textvariable=self.var_token,
            width=18, state="readonly", show="•",
        )
        self.entry_token.grid(row=0, column=1, sticky=E)
        self.btn_edit_token = ttk.Button(
            cols, text="✏", width=3, bootstyle="link",
        )
        self.btn_edit_token.configure(
            command=lambda: self._toggle_edit(
                self.entry_token, self.btn_edit_token, mask_char="•",
            )
        )
        self.btn_edit_token.grid(row=0, column=2, sticky=W, padx=(4, 0))

        left_label("lbl.chat_id", 1)
        self.var_chat_id = tk.StringVar(value=str(tg.get("chat_id", "")))
        self.entry_chat = ttk.Entry(
            cols, textvariable=self.var_chat_id,
            width=18, state="readonly", show="•",
        )
        self.entry_chat.grid(row=1, column=1, sticky=E)
        self.btn_edit_chat = ttk.Button(
            cols, text="✏", width=3, bootstyle="link",
        )
        self.btn_edit_chat.configure(
            command=lambda: self._toggle_edit(
                self.entry_chat, self.btn_edit_chat, mask_char="•",
            )
        )
        self.btn_edit_chat.grid(row=1, column=2, sticky=W, padx=(4, 0))

        left_label("lbl.theme", 2)
        builtin_themes = [
            "cyborg", "darkly", "solar", "superhero", "vapor",
            "cerculean", "cosmo", "flatly", "journal", "litera",
            "lumen", "minty", "morph", "pulse", "sandstone",
            "simplex", "united", "yeti",
        ]
        custom_codes = [c["code"] for c in self._custom_themes]
        themes = builtin_themes + custom_codes
        self.var_theme = tk.StringVar(value=ui.get("theme", "superhero"))
        cb_theme = ttk.Combobox(
            cols, values=themes, textvariable=self.var_theme,
            state="readonly", width=10,
        )
        cb_theme.grid(row=2, column=1, sticky=E)
        cb_theme.bind("<<ComboboxSelected>>", self._on_theme_change)

        left_label("lbl.language", 3)
        self._lang_options = i18n.available_languages()
        self._lang_name_to_code = {opt["name"]: opt["code"]
                                   for opt in self._lang_options}
        current_code = i18n.get_language()
        current_name = next(
            (opt["name"] for opt in self._lang_options
             if opt["code"] == current_code),
            current_code,
        )
        self.var_language = tk.StringVar(value=current_name)
        cb_lang = ttk.Combobox(
            cols, values=[opt["name"] for opt in self._lang_options],
            # Match the Theme combobox width above so the left column
            # has a single vertical edge — "Українська" / "English" both
            # fit comfortably in 10.
            textvariable=self.var_language, state="readonly", width=10,
        )
        cb_lang.grid(row=3, column=1, sticky=E)
        cb_lang.bind("<<ComboboxSelected>>", self._on_language_change)

        left_label("lbl.font_scale", 4)
        current_scale = int(ui.get("font_scale", 1) or 1)
        # First slot reads "За замовчуванням" (= x1, the unscaled
        # baseline). The next four are x2..x5, each ramping the factor
        # by 0.5 up to 3.0× the original.
        font_scale_values = self._font_scale_combo_values()
        self.var_font_scale = tk.StringVar(
            value=self._font_scale_to_label(current_scale),
        )
        cb_font = ttk.Combobox(
            cols, values=font_scale_values,
            textvariable=self.var_font_scale, state="readonly", width=4,
        )
        cb_font.grid(row=4, column=1, sticky=E)

        # ---- Log verbosity toggles (left column rows 5-6) ------------
        # Two opt-in controls (off by default) for capturing more
        # diagnostic detail in the Журнал tab. Live-applied via the
        # var trace and persisted to config.json immediately, so the
        # user doesn't need to hit Save after flipping them.
        #
        # Layout note: the labels here are noticeably longer than the
        # other left-column labels ("Розмір шрифту:" etc), so putting
        # them in column 0 would push the input column out — making
        # the whole form jagged. Instead we span both grid columns
        # with a single holder Frame anchored to the RIGHT (`sticky=E`).
        # That keeps the checkboxes' right edge aligned with the input
        # column above, and keeps the long labels from inflating column 0.
        ui_cur = self.config_data.get("ui", {}) or {}
        self.var_system_log = tk.BooleanVar(
            value=bool(ui_cur.get("system_log", False)),
        )
        sys_holder = ttk.Frame(cols)
        sys_holder.grid(row=5, column=0, columnspan=2, sticky=E, pady=4)
        ttk.Label(sys_holder, text=t("lbl.system_log"),
                  anchor=E).pack(side=LEFT)
        self._make_scalable_check(
            sys_holder, self.var_system_log,
        ).pack(side=LEFT, padx=(8, 0))

        self.var_debug_log = tk.BooleanVar(
            value=bool(ui_cur.get("debug_log", False)),
        )
        dbg_holder = ttk.Frame(cols)
        dbg_holder.grid(row=6, column=0, columnspan=2, sticky=E, pady=4)
        ttk.Label(dbg_holder, text=t("lbl.debug_log"),
                  anchor=E).pack(side=LEFT)
        self._make_scalable_check(
            dbg_holder, self.var_debug_log,
        ).pack(side=LEFT, padx=(8, 0))

        # Attach trace AFTER the initial value is set, otherwise the
        # `set(...)` above would fire `_on_log_toggle_change` and write
        # config.json during widget construction.
        self.var_system_log.trace_add(
            "write", lambda *_: self._on_log_toggle_change())
        self.var_debug_log.trace_add(
            "write", lambda *_: self._on_log_toggle_change())

        # ---- RIGHT column --------------------------------------------
        # Numeric/short fields, with the value on the left and label on
        # the right — keeps the small spinboxes from sliding far away
        # from the centre of the window.
        from regions import (STEAM_CURRENCIES, STEAM_COUNTRIES,
                              currency_label, country_label)

        # Full Steam currency list, sorted by code. Display format is
        # "UAH (18)" — matches the old shape so existing config files
        # / Reset paths keep working unchanged.
        self.var_currency = tk.IntVar(value=mkt.get("currency", 18))
        currency_map = {currency_label(code): code
                        for code in sorted(STEAM_CURRENCIES.keys())}
        cb_currency = ttk.Combobox(
            cols, values=list(currency_map.keys()),
            # Match Country combobox width (11) for a tidy vertical edge.
            state="readonly", width=11,
        )
        reverse_map = {v: k for k, v in currency_map.items()}
        cb_currency.set(reverse_map.get(self.var_currency.get(),
                                        currency_label(18)))
        cb_currency.grid(row=0, column=3, sticky=W, pady=4)
        self._currency_map = currency_map
        self._cb_currency = cb_currency
        right_label("lbl.currency", 0)

        # Country dropdown: every Steam-supported region we know about.
        # Picking one auto-syncs the currency dropdown (`_on_country_pick`)
        # — that's a "nudge", not a hard binding: the user can switch
        # currency manually after, e.g. someone in PL who wants Market
        # in EUR. Display values are "Ukraine (UA)"; we store the raw
        # ISO code in `var_country` so config.json schema doesn't change.
        self.var_country = tk.StringVar(value=mkt.get("country", "UA"))
        country_map = {country_label(iso, name): iso
                       for iso, name, _cur in STEAM_COUNTRIES}
        self._country_map = country_map
        # Currency lookup keyed by ISO code — used by the auto-sync
        # handler when the user picks a country.
        self._country_to_currency = {iso: cur for iso, _n, cur
                                     in STEAM_COUNTRIES}
        country_reverse = {iso: label for label, iso in country_map.items()}
        # Display name for the currently-saved country, or fall back to
        # showing the raw ISO if the country isn't in our list.
        current_country_label = country_reverse.get(
            self.var_country.get(), self.var_country.get(),
        )
        cb_country = ttk.Combobox(
            cols, values=list(country_map.keys()),
            # Width sized to hide the trailing ISO-code parenthesis on
            # long country names ("United States" → entry shows "United
            # States", the "(US)" lives in the dropdown). Shorter names
            # like "Ukraine (UA)" fit whole. Dropdown still shows the
            # full "Name (ISO)" label.
            state="readonly", width=11,
        )
        cb_country.set(current_country_label)
        cb_country.grid(row=1, column=3, sticky=W, pady=4)
        cb_country.bind("<<ComboboxSelected>>", self._on_country_pick)
        self._cb_country = cb_country
        right_label("lbl.country", 1)

        # All three right-column spinboxes share width=4 — enough for
        # "10.0" (the widest value: poll-delay), aligned with the others
        # for a tidy column edge. Visual consistency matters more than
        # pixel-perfect-sized boxes per value range.
        self.var_interval = tk.IntVar(value=sched.get("interval_minutes", 5))
        ttk.Spinbox(
            cols, from_=1, to=60, textvariable=self.var_interval, width=4,
        ).grid(row=2, column=3, sticky=W, pady=4)
        right_label("lbl.interval", 2)

        # Pause between price-fetch requests inside one batch. Right
        # next to "Interval" — both knobs concern Steam-polling cadence.
        self.var_poll_delay = tk.DoubleVar(
            value=mkt.get("poll_delay_sec", 1.5),
        )
        ttk.Spinbox(
            cols, from_=0.5, to=10.0, increment=0.1, format="%.1f",
            textvariable=self.var_poll_delay, width=4,
        ).grid(row=3, column=3, sticky=W, pady=4)
        right_label("lbl.poll_delay", 3)

        self.var_remind_hours = tk.IntVar(
            value=spam.get("remind_after_hours", 24),
        )
        ttk.Spinbox(
            cols, from_=1, to=168, textvariable=self.var_remind_hours,
            width=4,
        ).grid(row=4, column=3, sticky=W, pady=4)
        right_label("lbl.antispam_hours", 4)

        # "Повторити, якщо нижче" — single row spanning both right
        # columns. The custom check exists because ttkbootstrap's
        # Checkbutton indicator is a PhotoImage baked at theme init
        # and doesn't scale with font size.
        self.var_repeat_lower = tk.BooleanVar(
            value=spam.get("repeat_if_lower", True),
        )
        repeat_holder = ttk.Frame(cols)
        repeat_holder.grid(row=5, column=3, columnspan=2,
                           sticky=W, pady=4, padx=(0, 0))
        self._make_scalable_check(repeat_holder,
                                  self.var_repeat_lower).pack(side=LEFT)
        ttk.Label(repeat_holder, text=t("lbl.repeat_if_lower"),
                  anchor=W).pack(side=LEFT, padx=(8, 0))

        # «Бонусний контент» — opt-in switch for the upcoming wishlist-
        # tracking tab. Persisted immediately on flip (no Save needed),
        # same live-toggle contract as the log checkboxes: the tab is
        # meant to appear/disappear on the fly. Layout mirrors the
        # repeat-checkbox row right above.
        ui_cfg_now = self.config_data.get("ui", {}) or {}
        self.var_bonus_content = tk.BooleanVar(
            value=bool(ui_cfg_now.get("bonus_content", False)),
        )
        bonus_holder = ttk.Frame(cols)
        bonus_holder.grid(row=6, column=3, columnspan=2,
                          sticky=W, pady=4, padx=(0, 0))
        self._make_scalable_check(bonus_holder,
                                  self.var_bonus_content).pack(side=LEFT)
        ttk.Label(bonus_holder, text=t("lbl.bonus_content"),
                  anchor=W).pack(side=LEFT, padx=(8, 0))
        # Trace AFTER initial set so construction doesn't write config.
        self.var_bonus_content.trace_add(
            "write", lambda *_: self._on_bonus_content_toggle())

        # ---- Template section ----------------------------------------
        # Two-column layout: editor + var cheatsheet on the LEFT, live
        # Telegram-style preview on the RIGHT. The left column stacks
        # vertically — label, textarea+✏, then the variables/HTML
        # hints — and is sized to roughly match the preview's vertical
        # footprint so the whole row reads as one coherent block.
        tpl_section = ttk.Frame(outer)
        # Extra top-pady (≈ one row height) creates a visual break
        # between the form rows above and the template editor block —
        # asked for by the user so the form doesn't run straight into
        # the bigger Шаблон / Превью section.
        tpl_section.pack(fill=X, pady=(20, 12))

        # Left column container packed first. Preview gets `side=RIGHT`
        # further down so it docks to the right edge regardless of the
        # left column's expanded size; we build the preview last because
        # it binds to `self.txt_template` which has to exist already.
        tpl_left = ttk.Frame(tpl_section)
        tpl_left.pack(side=LEFT, anchor=NW, fill=BOTH, expand=YES,
                      padx=(0, 12))

        ttk.Label(tpl_left, text=t("lbl.template")
                  ).pack(anchor=W, pady=(0, 4))

        template_holder = ttk.Frame(tpl_left)
        template_holder.pack(anchor=W)
        # tk.Text isn't a ttk widget so it doesn't pick up the
        # style-level selection colours we configured for ttk.Entry.
        # Apply explicitly so selected text reads against the input bg.
        #
        # height=10 keeps the editor matched to the preview pane;
        # width=72 makes the textarea a touch wider than the "Змінні:"
        # cheatsheet that sits under it, so the cheatsheet looks anchored
        # under the editor rather than spilling past its right edge.
        self.txt_template = tk.Text(
            template_holder, width=72, height=10, wrap=tk.WORD,
            selectbackground=self._text_sel_bg,
            selectforeground=self._text_sel_fg,
        )
        # Order: explicit user override → language default. Empty/blank
        # override falls back to the language default too.
        template_value = (cfg.get("message_template") or "").strip() \
            or t("tg.message.default")
        # Insert BEFORE switching to disabled — Text rejects insert()
        # while disabled. After locking, the user re-enables via ✏.
        self.txt_template.insert("1.0", template_value)
        self.txt_template.configure(state="disabled")
        self.txt_template.pack(side=LEFT)

        # Two-button column to the right of the editor: ✏ toggles
        # edit/commit; ✕ discards changes and exits edit mode. The
        # cancel button needs to know what to restore — we snapshot
        # the editor contents the moment ✏ is clicked (entering edit
        # mode), and clear that snapshot after either ✓ or ✕ resolves.
        self._template_original: str | None = None
        tpl_btn_col = ttk.Frame(template_holder)
        tpl_btn_col.pack(side=LEFT, padx=(4, 0), anchor=N)
        self.btn_edit_template = ttk.Button(
            tpl_btn_col, text="✏", width=3, bootstyle="link",
            command=self._on_template_edit_toggle,
        )
        self.btn_edit_template.pack(side=TOP)
        # Cancel button — only shown while editing. Mirrors the look
        # of the ✓ commit button (filled red square via plain
        # `bootstyle="danger"`) so the two button states sit side by
        # side as matched "commit / discard" affordances. Hidden in
        # the readonly state via pack_forget — there's no "cancel" if
        # nothing's being edited, and the empty slot would just look
        # like dead UI.
        self.btn_cancel_template = ttk.Button(
            tpl_btn_col, text="✕", width=3, bootstyle="danger",
            command=self._on_template_cancel,
        )
        # NOT packed initially — `_on_template_edit_toggle` packs it
        # on edit entry and `pack_forget`s it on commit / cancel.

        # Variables + HTML hints sit UNDER the editor, still in the
        # left column. Two separate lines for readability.
        ttk.Label(tpl_left, text=t("lbl.template_vars"),
                  foreground="gray").pack(anchor=W, pady=(8, 0))
        ttk.Label(tpl_left, text=t("lbl.template_html"),
                  foreground="gray").pack(anchor=W)

        # Preview pane on the right — built last because it binds to
        # `self.txt_template` which only exists after the editor block
        # above. `side=RIGHT` still parks it on the right edge of
        # tpl_section regardless of build order.
        self._build_template_preview(tpl_section)

        # ---- Bottom row: Steam login | Test message + Telegram label -
        # `pady=(24, ...)` gives a clear visual break between the
        # template preview row above and the bottom-row services —
        # otherwise the buttons end up sitting flush against the
        # preview's bottom border.
        bottom = ttk.Frame(outer)
        bottom.pack(fill=X, pady=(24, 0))

        steam_block = ttk.Frame(bottom)
        steam_block.pack(side=LEFT)
        ttk.Label(steam_block, text=t("lbl.steam_login")).pack(side=LEFT,
                                                                padx=(0, 8))
        ttk.Button(
            steam_block, text=t("btn.steam_login_setup"),
            command=self._open_steam_login_dialog,
            bootstyle="info",
        ).pack(side=LEFT)

        tg_block = ttk.Frame(bottom)
        tg_block.pack(side=RIGHT)
        ttk.Button(tg_block, text=t("btn.test_message"),
                   command=self._test_telegram, bootstyle="info"
                   ).pack(side=LEFT, padx=(0, 8))
        # Plain label — same default font as the rest of the page;
        # bolding it made the right edge feel heavier than the matching
        # "Вхід у Steam:" anchor on the left.
        ttk.Label(tg_block, text=t("lbl.telegram_section")
                  ).pack(side=LEFT)

        # ---- Save / Reset — centred, well below the bottom services -
        # Big `pady` makes the destructive Reset button a deliberate
        # reach — you have to actively scroll your eye down past
        # everything else to find it. Saves a few accidental clicks.
        save_row = ttk.Frame(outer)
        save_row.pack(pady=(40, 0))
        # Reset is bootstyle="danger" so it's visually weighted as
        # "destructive"; the confirmation dialog catches the second-
        # thought case before anything is wiped.
        ttk.Button(save_row, text=t("btn.save"),
                   command=self._save_settings, bootstyle="success"
                   ).pack(side=LEFT, padx=(0, 8))
        ttk.Button(save_row, text=t("btn.reset"),
                   command=self._reset_settings_to_defaults,
                   bootstyle="danger"
                   ).pack(side=LEFT)

        # Lock Currency + Country if a Steam ID is already attached.
        # Has to happen AFTER the pickers are gridded — _update… reads
        # `_cb_currency` / `_cb_country` off `self`. Idempotent: every
        # tab rebuild (theme change, etc.) re-applies the right state.
        self._update_currency_country_state()

    def _build_template_preview(self, parent) -> None:
        """Build the Telegram-style preview pane next to the template editor.

        Layout — a single Frame with a 1-px solid border:
          ┌──────────────────────┐
          │  [poster image]      │  ← fixed PNG from preview/
          │  rendered template   │  ← updates as user types
          └──────────────────────┘

        The image is the bundled `preview/led-backlit.png` — same kind
        of poster Steam shows above a Telegram-style alert, just used
        statically here so the user can see what the live message
        layout will look like without sending one.

        Tk has no built-in HTML rendering, so we parse the three tags
        the message template supports (<b>, <u>, <blockquote>) into
        Text widget tags. Anything else passes through literally.
        """
        # The pane lives inside the same horizontal holder as the
        # template Text + ✏ button so they share a baseline. Border is
        # `relief="solid"` with borderwidth=1 — matches the other
        # bordered Frames in the app (Treeview tree_frame, etc.).
        preview_frame = ttk.Frame(parent, relief="solid", borderwidth=1)
        # `side=RIGHT` anchors the pane to the right edge of the
        # template row; the left-column container then expands to fill
        # whatever's left between it and the preview.
        preview_frame.pack(side=RIGHT, anchor=NE)

        # Poster image — load once at startup, hold a reference on
        # self so Tk's image GC doesn't collect it the moment this
        # method returns. Resize to a reasonable width via PIL
        # `thumbnail` so the pane doesn't dominate the page.
        try:
            from PIL import Image, ImageTk
            img = Image.open(BASE / "preview" / "led-backlit.png")
            img.thumbnail((220, 280), Image.LANCZOS)
            self._template_preview_img = ImageTk.PhotoImage(img)
            ttk.Label(preview_frame,
                      image=self._template_preview_img).pack(
                side=TOP, padx=4, pady=(4, 0),
            )
        except Exception as exc:
            log.warning("template preview image not loaded: %s", exc)

        # Rendered text below the image. tk.Text gives us per-region
        # tags for bold/underline/blockquote; ttk.Label would only
        # let us apply one font to the whole string.
        self.txt_template_preview = tk.Text(
            preview_frame, width=28, height=4, wrap=tk.WORD,
            relief="flat", borderwidth=0, state="disabled",
            selectbackground=self._text_sel_bg,
            selectforeground=self._text_sel_fg,
        )
        # Tag styling — mirrors how the actual Telegram message renders.
        # Sizes tuned to roughly match the preview image's apparent text
        # scale; user-tweakable later if needed.
        self.txt_template_preview.tag_configure(
            "b", font=("Segoe UI", 10, "bold"),
        )
        self.txt_template_preview.tag_configure("u", underline=True)
        self.txt_template_preview.tag_configure(
            "blockquote",
            lmargin1=15, lmargin2=15,
            foreground=self.style.colors.secondary,
        )
        self.txt_template_preview.pack(side=TOP, fill=X, padx=4, pady=4)

        # Live updates: bind <<Modified>> on the editor. tk.Text's
        # `edit_modified` flag has to be reset after each handler call,
        # otherwise the event only fires once per session.
        self.txt_template.bind("<<Modified>>", self._on_template_changed)
        # Render once with the current contents so the preview isn't
        # empty on first paint.
        self._render_template_preview()

    def _on_template_edit_toggle(self) -> None:
        """✏ button — enter edit mode, or commit changes and lock again.

        Custom replacement for the generic `_toggle_edit` so we can
        snapshot the editor contents on entry (powers the ✕ cancel
        button) and show / hide the cancel button by packing it on the
        fly. Cancel is only meaningful while editing — keeping it
        visible at all times would leave dead UI sitting next to the
        readonly editor.
        """
        if str(self.txt_template.cget("state")) == "disabled":
            # Entering edit mode — capture current text so ✕ has
            # something to roll back to.
            self._template_original = self.txt_template.get("1.0", "end-1c")
            self.txt_template.configure(state="normal")
            self.txt_template.focus_set()
            try:
                self.txt_template.mark_set("insert", "end-1c")
            except tk.TclError:
                pass
            self.btn_edit_template.configure(text="✓", bootstyle="success")
            # Pack ✕ right under ✓ so the two filled squares read as
            # a matched "commit / discard" pair while editing.
            self.btn_cancel_template.pack(side=TOP, pady=(4, 0))
        else:
            # Committing — keep whatever the user typed, drop snapshot,
            # hide the ✕ button so the readonly state stays uncluttered.
            self.txt_template.configure(state="disabled")
            self.btn_edit_template.configure(text="✏", bootstyle="link")
            self.btn_cancel_template.pack_forget()
            self._template_original = None

    def _on_template_cancel(self) -> None:
        """✕ button — discard changes and exit edit mode."""
        if self._template_original is None:
            return
        snapshot = self._template_original
        self.txt_template.configure(state="normal")
        self.txt_template.delete("1.0", "end")
        self.txt_template.insert("1.0", snapshot)
        self.txt_template.configure(state="disabled")
        self.btn_edit_template.configure(text="✏", bootstyle="link")
        self.btn_cancel_template.pack_forget()
        self._template_original = None
        # Repaint the preview pane against the restored text so the
        # right-hand side reflects what the saved template actually says.
        self._render_template_preview()

    def _on_template_changed(self, _event=None) -> None:
        """`<<Modified>>` handler — re-render the preview."""
        try:
            if not self.txt_template.edit_modified():
                return
            self.txt_template.edit_modified(False)
        except tk.TclError:
            return
        self._render_template_preview()

    # Sample values used in the preview. Kept tiny — the goal is to
    # show "this is where the card name lands", not to look like real
    # data the user might compare against. Locale-sensitive {operation}
    # comes from i18n so the preview reads the same language as the
    # actual alerts will.
    @staticmethod
    def _template_preview_sample() -> dict:
        return {
            "display_name": "Sample Card",
            "name":         "Sample Card",
            "game":         "Sample Game",
            "price":        "5.00",
            "target":       "4.00",
            "volume":       "12",
            "url":          "https://steamcommunity.com/market/listings/...",
            "operation":    t("tg.operation.sell"),
        }

    def _render_template_preview(self) -> None:
        """Substitute placeholders + render HTML tags into the preview Text."""
        # Pull the current editor contents (works in both readonly and
        # edit states — Text always reports its buffer).
        template = self.txt_template.get("1.0", "end-1c")
        try:
            filled = template.format_map(self._template_preview_sample())
        except (KeyError, IndexError, ValueError):
            # Malformed format string — show the raw template so the
            # user can see what they broke instead of a stack trace.
            filled = template

        self._insert_html_into_text(self.txt_template_preview, filled)

    @staticmethod
    def _insert_html_into_text(text_widget, html: str) -> None:
        """Naive HTML-to-Tk-tags renderer.

        Supports <b>, <u>, <blockquote>. Walks the string with a regex,
        maintaining a stack of currently-open tags; each inserted run
        gets the stack as its tag tuple so nested tags layer correctly
        (bold + underline both apply when both are open).

        Unrecognised tags pass through as literal text — keeps the
        preview honest if the user types something we don't render
        (Telegram would also just show it verbatim).
        """
        import re

        text_widget.configure(state="normal")
        text_widget.delete("1.0", "end")

        pattern = re.compile(r"<(/?)(b|u|blockquote)>", re.IGNORECASE)
        tag_stack: list[str] = []
        pos = 0
        for m in pattern.finditer(html):
            if m.start() > pos:
                text_widget.insert(
                    "end", html[pos:m.start()], tuple(tag_stack),
                )
            is_close = bool(m.group(1))
            name = m.group(2).lower()
            if is_close:
                # Pop the most-recent matching open. If they don't
                # nest properly (e.g. "<b><u></b></u>") we still close
                # the most-recent one — minor visual quirk, but the
                # alternative (refusing to close) would leave bold/
                # underline bleeding through the rest of the message.
                for i in range(len(tag_stack) - 1, -1, -1):
                    if tag_stack[i] == name:
                        tag_stack.pop(i)
                        break
            else:
                tag_stack.append(name)
            pos = m.end()
        # Trailing run after the last tag.
        if pos < len(html):
            text_widget.insert("end", html[pos:], tuple(tag_stack))

        text_widget.configure(state="disabled")

    def _toggle_edit(self, widget, button: ttk.Button,
                     lock_state: str = "readonly",
                     mask_char: str | None = None) -> None:
        """Flip a locked input widget between edit and locked modes.

        Works for both ttk.Entry (lock_state="readonly") and tk.Text
        (lock_state="disabled" — Text has no "readonly" state).

        Visual contract:
          * locked   → state=lock_state, button "✏" in neutral link colour
                       Entry shows `mask_char` instead of real text when
                       `mask_char` is non-empty (used for the Telegram
                       token and chat ID — secrets we don't want over
                       the shoulder).
          * editing  → state=normal,     button "✓" in success (green)
                       Mask cleared so the user can read what they're
                       typing.
        Pressing the button again locks the field; the underlying value
        (StringVar or Text buffer) keeps whatever the user typed.
        """
        if str(widget.cget("state")) == lock_state:
            widget.configure(state="normal")
            # Drop the mask while editing so the user can see the value
            # they're correcting. ttk.Text has no `show` option — only
            # apply for widgets that support it.
            if mask_char is not None and "show" in widget.keys():
                widget.configure(show="")
            widget.focus_set()
            if hasattr(widget, "icursor"):
                widget.icursor("end")
            button.configure(text="✓", bootstyle="success")
        else:
            widget.configure(state=lock_state)
            if mask_char is not None and "show" in widget.keys():
                widget.configure(show=mask_char)
            button.configure(text="✏", bootstyle="link")

    def _test_telegram(self):
        from telegram import send_test
        token = self.var_token.get().strip()
        chat_id = self.var_chat_id.get().strip()
        if not token or not chat_id:
            messagebox.showwarning(t("dlg.test.title"), t("dlg.test.empty_creds"))
            return
        try:
            send_test(token, chat_id)
            messagebox.showinfo(t("dlg.test.title"), t("dlg.test.sent"))
        except Exception as exc:
            messagebox.showerror(t("dlg.test.title"), str(exc))

    def _on_theme_change(self, _event=None):
        theme = self.var_theme.get()
        try:
            self.style.theme_use(theme)
        except Exception:
            pass
        # New palette → new alt-row tint. Re-derive and reapply.
        self._configure_styles()
        for tree in self.list_trees.values():
            self._apply_row_tags(tree)
        if hasattr(self, "hist_tree"):
            self._apply_row_tags(self.hist_tree)
        self._refresh_watchlist()
        self._refresh_history()
        # New theme → new title-bar colour.
        self._apply_native_titlebar_theme()

    def _on_country_pick(self, _event=None):
        """Country dropdown changed — sync the currency dropdown to match.

        We treat the mapping as a nudge, not a binding: we update the
        currency picker for the new country, but the user's free to
        change it back to whatever they want before pressing Save.
        That covers e.g. a PL-based user who wants Market priced in EUR.

        Side effect: the floating-widget's placeholder balance refreshes
        its currency glyph too, so "0.00 ₴" → "0.00 €" without the user
        having to save first.
        """
        from regions import currency_label

        label = self._cb_country.get()
        iso = self._country_map.get(label)
        if not iso:
            return
        self.var_country.set(iso)
        # Look up the country's default currency. Missing entry (we
        # didn't catalogue every Steam country) means we just leave the
        # currency picker alone — same outcome as the user not touching it.
        new_currency = self._country_to_currency.get(iso)
        if new_currency is not None:
            self._cb_currency.set(currency_label(new_currency))
            self.var_currency.set(new_currency)
            # Repaint the placeholder balance so the symbol flips
            # immediately — feels more responsive than waiting for Save.
            self._refresh_balance_placeholder(new_currency)

    def _refresh_balance_placeholder(self, currency_code: int) -> None:
        """Repaint the floating-widget balance placeholder for a new currency.

        Only meaningful when we DON'T have a live Steam session — the
        widget shows "0.00 X" as a placeholder, and the X should match
        whatever currency the user picked in Settings. When store
        cookies are present, Steam serves the real wallet balance
        (number + native symbol baked into the string), and we MUST
        NOT touch it: Valve picks the format from the account, not
        from our Market settings, and any substitution we'd do would
        either lie about the currency or flicker. The real balance
        gets refreshed via `_refresh_wallet_balance` after Save.
        """
        from regions import currency_symbol
        steam_cfg = self.config_data.get("steam") or {}
        cookies = steam_cfg.get("cookies") or {}
        if (cookies.get("store.steampowered.com") or {}).get("steamLoginSecure"):
            # Live session — leave Steam's formatted balance alone.
            return
        sym = currency_symbol(currency_code, fallback="")
        self._update_user_widget(balance=f"0.00 {sym}")

    def _update_currency_country_state(self) -> None:
        """Lock Currency + Country pickers iff a cookies-based login is active.

        Rationale: when we have Steam session cookies, Currency +
        Country are dictated by THAT account's Steam region — the
        Market endpoints and the wallet widget all use the cookies'
        account, so letting the user fiddle with these would create a
        contradictory display ("I'm logged in as a US user but the
        widget shows UAH").

        Manual-ID tier (Tier 3) does NOT lock the fields: there's no
        real session, no wallet, no account-bound Market context — the
        ID is just a profile pointer for the avatar / persona. The
        user retains full control over which currency + country they
        want Market requests and History totals to use.

        Implementation note — we DON'T use `state="disabled"` here.
        ttkbootstrap dims the disabled foreground to a near-unreadable
        grey on dark themes, and `style.map("TCombobox",
        foreground=[("disabled", fg_normal)])` doesn't reliably win
        against ttkbootstrap's own state maps. So we keep the widget
        in `readonly` (where text stays at normal foreground) and
        suppress all dropdown-opening interactions via bindings.
        Visual cue: the field looks identical to its editable form —
        the user finds out it's locked when they try to click. Worth
        it for the readability win.
        """
        # Pickers only exist while the Settings tab is built. Bail
        # silently if called before that — callers (login, disconnect)
        # may fire before the user has even opened the tab.
        cb_cur = getattr(self, "_cb_currency", None)
        cb_cnt = getattr(self, "_cb_country", None)
        if cb_cur is None or cb_cnt is None:
            return
        steam_cfg = self.config_data.get("steam") or {}
        cookies = steam_cfg.get("cookies") or {}
        # Tier 2 marker: store-domain cookies contain the wallet token.
        # We check that specific cookie so an empty/wiped cookies dict
        # doesn't accidentally pass the truthy check.
        has_session = bool(
            (cookies.get("store.steampowered.com") or {}).get("steamLoginSecure")
        )

        # Remove any previous lock-bindings before either re-applying
        # them or leaving the field free. Tracking funcids per-widget
        # so we only remove OUR handlers, not class-level ones (e.g.
        # the ttk default keyboard nav).
        if not hasattr(self, "_currency_country_lock_binds"):
            self._currency_country_lock_binds = {}
        bind_track = self._currency_country_lock_binds  # alias
        for cb in (cb_cur, cb_cnt):
            for seq, fid in bind_track.get(cb, []):
                try:
                    cb.unbind(seq, fid)
                except tk.TclError:
                    pass
            bind_track[cb] = []

        # Country combobox needs its <<ComboboxSelected>> handler
        # to stay attached when unlocked but be suppressed when locked.
        # Easiest: re-bind it here every time (cheap), gated on lock state.
        try:
            cb_cnt.unbind("<<ComboboxSelected>>")
        except tk.TclError:
            pass

        if not has_session:
            # Unlocked: re-attach the country auto-sync handler, leave
            # all other interactions alone. Done.
            cb_cnt.bind("<<ComboboxSelected>>", self._on_country_pick)
            return

        # Locked: block every gesture that opens the dropdown or
        # advances selection. Returning "break" stops the event from
        # propagating to the class-level handler that actually opens
        # the listbox. We cover:
        #   - <Button-1>   click anywhere in the field or arrow
        #   - <Down>/<Up>  keyboard-nav across items
        #   - <Return>     keyboard-activated dropdown
        #   - <space>      same, some themes bind it
        #   - <MouseWheel> scroll-to-change behaviour
        blocker = lambda _e: "break"  # noqa: E731 — trivial inline lambda
        for cb in (cb_cur, cb_cnt):
            for seq in ("<Button-1>", "<Down>", "<Up>",
                        "<Return>", "<space>", "<MouseWheel>"):
                try:
                    fid = cb.bind(seq, blocker)
                    bind_track[cb].append((seq, fid))
                except tk.TclError:
                    pass

    # ------------------------------------------------------------------
    # Log verbosity toggles — Settings checkboxes for "Системний лог" /
    # "Debug log". Two orthogonal concerns:
    #   * system_log → route uncaught Python AND Tk-callback exceptions
    #                  into our logger as ERROR. Without it, those
    #                  tracebacks evaporate (pythonw discards stderr),
    #                  which makes "weird click crashed the widget"
    #                  reports unreproducible.
    #   * debug_log  → flip every named-logger level from INFO down to
    #                  DEBUG. Floods the Журнал with raw HTTP request
    #                  lines, wallet-info parse details, etc.
    # ------------------------------------------------------------------

    def _apply_log_config(self, system_log: bool, debug_log: bool) -> None:
        """Apply the two log toggles to the running process.

        Idempotent — safe to call from `__init__` (bootstrap) and from
        the `var_*.trace_add` callback (live toggle). Stores the
        original `sys.excepthook` once so disabling later restores it
        instead of leaving our hook in place forever.
        """
        import sys
        import traceback

        # --- debug level --------------------------------------------
        level = logging.DEBUG if debug_log else logging.INFO
        # Match the sibling loggers we register at module load (see top
        # of file). If we add new loggers later, update both lists.
        for name in ("gui", "steam", "telegram", "alerts",
                     "browser_cookies"):
            logging.getLogger(name).setLevel(level)

        # --- system log: uncaught exception routing -----------------
        if not hasattr(self, "_default_excepthook"):
            self._default_excepthook = sys.excepthook
        if system_log:
            def _gui_excepthook(exc_type, exc_value, exc_tb):
                tb_text = "".join(traceback.format_exception(
                    exc_type, exc_value, exc_tb,
                ))
                log.error("uncaught exception:\n%s", tb_text.rstrip())
                # Chain to the original hook so anything else watching
                # stderr (PyCharm debugger, console) still sees it.
                self._default_excepthook(exc_type, exc_value, exc_tb)

            sys.excepthook = _gui_excepthook

            # Tk has its own callback-exception path that doesn't go
            # through sys.excepthook — set ours on the root window.
            def _tk_callback_exception(exc_type, exc_value, exc_tb):
                tb_text = "".join(traceback.format_exception(
                    exc_type, exc_value, exc_tb,
                ))
                log.error("tk callback exception:\n%s", tb_text.rstrip())

            self.report_callback_exception = _tk_callback_exception
        else:
            # Disable: restore the saved defaults so we don't leave a
            # stale hook running after the user toggles off.
            sys.excepthook = self._default_excepthook
            try:
                # Drop the instance-level override so Tk falls back to
                # the base class default (prints to stderr).
                del self.report_callback_exception
            except AttributeError:
                pass

    def _on_log_toggle_change(self) -> None:
        """Reapply log config + persist to disk when a checkbox flips.

        Bound via `var_system_log.trace_add("write", …)` and same for
        `var_debug_log`. Apply-and-save is immediate (no Save button
        required) — log toggles are the kind of thing the user tries
        on and expects to survive a restart even if they forgot to
        hit Save.
        """
        sys_log = bool(self.var_system_log.get())
        dbg_log = bool(self.var_debug_log.get())
        self._apply_log_config(sys_log, dbg_log)
        # Load-merge persist so we don't clobber unrelated config keys
        # written by another process / tab between sessions.
        try:
            on_disk = load_json(CONFIG_PATH) or {}
        except Exception:
            on_disk = {}
        ui_block = on_disk.setdefault("ui", {})
        ui_block["system_log"] = sys_log
        ui_block["debug_log"] = dbg_log
        self.config_data.setdefault("ui", {})["system_log"] = sys_log
        self.config_data.setdefault("ui", {})["debug_log"] = dbg_log
        try:
            save_json(CONFIG_PATH, on_disk)
        except Exception as e:
            log.warning("could not persist log toggles: %s", e)
        log.info("log toggles: system_log=%s debug_log=%s", sys_log, dbg_log)

    def _on_bonus_content_toggle(self) -> None:
        """Persist the «Бонусний контент» switch + show/hide the «Ігри» tab.

        notebook.hide keeps the tab's widget tree (and the Treeview's
        contents) alive and remembers the position; notebook.add on a
        hidden-but-managed child restores it to that same slot, so the
        tab reliably re-appears between Продаж and Історія. gamelist.json
        is untouched either way — disabling the tab merely stops the
        polling/alerts (watch.py checks this same config flag).
        """
        enabled = bool(self.var_bonus_content.get())
        self.config_data.setdefault("ui", {})["bonus_content"] = enabled
        try:
            on_disk = load_json(CONFIG_PATH) or {}
        except Exception:
            on_disk = {}
        on_disk.setdefault("ui", {})["bonus_content"] = enabled
        try:
            save_json(CONFIG_PATH, on_disk)
        except Exception as e:
            log.warning("could not persist bonus_content toggle: %s", e)
        # Live tab toggle. Guarded — the checkbox var can fire during
        # Settings construction before the games tab exists.
        holder = getattr(getattr(self, "tab_games", None), "_holder", None)
        if holder is not None:
            try:
                if enabled:
                    self.notebook.add(holder)
                    self._refresh_games_list()
                else:
                    self.notebook.hide(holder)
            except tk.TclError:
                pass
        log.info("bonus content %s", "enabled" if enabled else "disabled")

    def _on_language_change(self, _event=None):
        """Save the new language and prompt for a restart.

        We don't try to rebuild every widget at runtime — Tkinter has no
        reliable way to swap text on a notebook tab or a Treeview heading
        for an already-realised window without flicker, and the gain is
        marginal. Restart is honest and simple.
        """
        name = self.var_language.get()
        new_code = self._lang_name_to_code.get(name)
        if not new_code or new_code == i18n.get_language():
            return
        # Persist immediately so a restart picks it up — don't wait for Save.
        cfg = load_json(CONFIG_PATH, {}) or {}
        cfg.setdefault("ui", {})["language"] = new_code
        save_json(CONFIG_PATH, cfg)
        self.config_data = cfg
        messagebox.showinfo(t("dlg.lang_changed.title"),
                            t("dlg.lang_changed.body"))

    def _save_settings(self):
        currency_str = self._cb_currency.get()
        currency_val = self._currency_map.get(currency_str, 18)
        # Selected language → ISO code; fall back to the running language
        # if the picker is empty for some reason.
        lang_code = self._lang_name_to_code.get(
            self.var_language.get(), i18n.get_language()
        )
        template_override = self.txt_template.get("1.0", "end-1c").strip()
        cfg = {
            "telegram": {
                "bot_token": self.var_token.get().strip(),
                "chat_id": self.var_chat_id.get().strip(),
            },
            "market": {
                "currency": currency_val,
                "country": self.var_country.get().strip().upper(),
                # Float — rounded to one decimal to match the Spinbox display.
                # Picked up "on the fly": self.config_data is reassigned below,
                # _check_now re-reads it on every click. watch.py reads its
                # own copy of config.json on every scheduled run.
                "poll_delay_sec": round(float(self.var_poll_delay.get()), 1),
            },
            "schedule": {
                "interval_minutes": self.var_interval.get(),
            },
            "ui": {
                "theme": self.var_theme.get(),
                "language": lang_code,
                # Combobox value → 1..5 (handles both "За замовчуванням"
                # and "x2"..."x5"). _apply_font_scale clamps the result
                # so hand-edited configs can't push out of range either.
                "font_scale": self._font_scale_from_label(self.var_font_scale.get()),
                # Log toggles. Trace-write already persists on every
                # flip, but Save sweeps them in too so a Reset-then-
                # Save can wipe them, and so a hand-edited config can
                # be normalised from one place.
                "system_log": bool(self.var_system_log.get()),
                "debug_log": bool(self.var_debug_log.get()),
                # Snapshot of the current window size+position. Restored
                # on next launch (see __init__). Reset writes "1100x620"
                # here via self.geometry() right before calling us, so
                # reset naturally wipes the saved value back to default.
                "window_geometry": self.geometry(),
            },
            "antispam": {
                "repeat_if_lower": self.var_repeat_lower.get(),
                "remind_after_hours": self.var_remind_hours.get(),
            },
        }
        # Only persist message_template if the user actually changed it from
        # the language default — otherwise saved configs lock to old text
        # when the user switches language.
        if template_override and template_override != t("tg.message.default"):
            cfg["message_template"] = template_override
        # Preserve the Steam-login section as-is. It's owned by a different
        # subsystem (steam_login.py + the login dialog) and the Settings
        # form has no fields for it, so a Settings "Save" must NOT wipe it.
        # Pulls from self.config_data because the login dialog mutates that
        # dict in place after every successful manual save / disconnect.
        steam_section = self.config_data.get("steam")
        if steam_section is not None:
            cfg["steam"] = steam_section
        save_json(CONFIG_PATH, cfg)
        self.config_data = cfg
        # Apply font scale live so the change is visible without a restart.
        # Re-cap min size after — bigger fonts need a bigger floor.
        self._apply_font_scale(cfg["ui"]["font_scale"])
        self.after(50, self._apply_min_size)
        # Currency / country may have changed. Two paths:
        #   * No Steam session — `_refresh_balance_placeholder` repaints
        #     the "0.00 X" placeholder with the new symbol.
        #   * Live Steam session — placeholder helper bails (Steam owns
        #     the wallet display), and `_refresh_wallet_balance` re-pulls
        #     the formatted balance from store.steampowered.com. Steam
        #     picks the symbol from the account, not from our market
        #     settings, so the display reflects whatever Valve serves.
        self._refresh_balance_placeholder(currency_val)
        self._refresh_wallet_balance()
        self._set_status(t("status.settings_saved"))

    def _reset_settings_to_defaults(self):
        """Wipe all settings back to config.example.json defaults.

        Telegram credentials (bot_token, chat_id) are deliberately preserved —
        the user pastes those in once and losing them silently would be
        cruel. The confirmation dialog says so up front.

        After the reset we:
          * mutate every settings-tab Var so the form reflects the new state,
          * re-apply theme + font_scale + DWM title-bar tint live,
          * shrink the window back to the launch size (1100×620) and re-cap
            the minsize floor for the now-baseline font,
          * persist by reusing _save_settings — single source of truth for
            on-disk schema and post-save bookkeeping.
        """
        if not messagebox.askyesno(t("dlg.reset.title"), t("dlg.reset.body")):
            return

        # Capture current language so we can decide whether to prompt for
        # restart at the end — i18n only swaps on app launch (every tab,
        # heading, button text is already realised), so a language reset
        # would otherwise leave the dropdown saying "English" while the
        # UI keeps speaking Ukrainian until the next start.
        current_lang_code = i18n.get_language()

        # Mirror of config.example.json — single place to bump when
        # defaults change. Telegram creds intentionally NOT here.
        defaults = {
            "currency":          18,
            "country":           "UA",
            "poll_delay_sec":    1.5,
            "interval_minutes":  5,
            "theme":             "superhero",
            "language":          "en",
            "font_scale":        1,
            "repeat_if_lower":   True,
            "remind_after_hours": 24,
        }

        # ---- Reset all the StringVars / IntVars the form reads from ----
        self.var_currency.set(defaults["currency"])
        # Combobox display value uses "UAH (18)" labels — look up via the
        # reverse map we cached when building the form.
        reverse_currency = {v: k for k, v in self._currency_map.items()}
        self._cb_currency.set(reverse_currency.get(defaults["currency"], "UAH (18)"))

        self.var_country.set(defaults["country"])
        # Country is now a Combobox showing "Ukraine (UA)" — find the
        # label that matches the default ISO so the dropdown actually
        # reads "Ukraine (UA)" after reset, not the bare "UA".
        reverse_country = {iso: lbl for lbl, iso in self._country_map.items()}
        self._cb_country.set(
            reverse_country.get(defaults["country"], defaults["country"])
        )
        self.var_poll_delay.set(defaults["poll_delay_sec"])
        self.var_interval.set(defaults["interval_minutes"])
        self.var_theme.set(defaults["theme"])

        # Language: var stores display name, not ISO code.
        default_lang_name = next(
            (opt["name"] for opt in self._lang_options
             if opt["code"] == defaults["language"]),
            defaults["language"],
        )
        self.var_language.set(default_lang_name)

        self.var_font_scale.set(self._font_scale_to_label(defaults["font_scale"]))
        self.var_remind_hours.set(defaults["remind_after_hours"])
        # Triggers the trace_add → glyph redraws to ☑/☐ automatically.
        self.var_repeat_lower.set(defaults["repeat_if_lower"])
        # Log toggles reset to off — Reset is "back to defaults", and
        # defaults are off for both. Trace will fire and persist for us.
        self.var_system_log.set(False)
        self.var_debug_log.set(False)

        # Template field is a tk.Text in disabled state — unlock briefly
        # to replace its contents with the language default, then relock.
        # Whatever edit-button state was in play gets reset alongside.
        self.txt_template.configure(state="normal")
        self.txt_template.delete("1.0", "end")
        self.txt_template.insert("1.0", t("tg.message.default"))
        self.txt_template.configure(state="disabled")
        self.btn_edit_template.configure(text="✏", bootstyle="link")

        # ---- Apply live: theme + titlebar + treeview colours ----
        try:
            self.style.theme_use(defaults["theme"])
        except Exception:
            pass
        self._configure_styles()
        for tree in self.list_trees.values():
            self._apply_row_tags(tree)
        if hasattr(self, "hist_tree"):
            self._apply_row_tags(self.hist_tree)
        self._refresh_watchlist()
        self._refresh_history()
        self._apply_native_titlebar_theme()

        # Shrink window back to the launch geometry. minsize re-caps after
        # the font scale change so it doesn't refuse the smaller size.
        self.geometry("1100x620")

        # Persist via _save_settings so the schema written to disk matches
        # what a normal Save would produce — and font-scale / minsize live
        # update happens for free (it's already in _save_settings).
        self._save_settings()

        # If the language actually changed, prompt for restart — same
        # contract as the language picker's own on-change handler. Done
        # at the very end so the user sees the visual reset complete
        # before the modal pops up.
        if current_lang_code != defaults["language"]:
            messagebox.showinfo(t("dlg.lang_changed.title"),
                                t("dlg.lang_changed.body"))

    # ---- Scheduler -------------------------------------------------------

    def _build_scheduler_tab(self):
        f = ttk.Frame(self.tab_scheduler, padding=16)
        f.pack(fill=BOTH, expand=YES)

        self.lbl_task_status = ttk.Label(f, text="…")
        # Slightly bigger than default so the line stands out. Registered
        # so it follows the Settings font-scale knob.
        self._scaled_font(self.lbl_task_status, 12)
        self.lbl_task_status.pack(pady=(0, 12))

        btn_row = ttk.Frame(f)
        btn_row.pack()
        for key, cmd in [
            ("btn.scheduler_create",  self._sched_create),
            ("btn.scheduler_enable",  self._sched_enable),
            ("btn.scheduler_disable", self._sched_disable),
            ("btn.scheduler_run_now", self._sched_run_now),
            ("btn.scheduler_delete",  self._sched_delete),
        ]:
            ttk.Button(btn_row, text=t(key), command=cmd).pack(side=LEFT, padx=4)

        ttk.Button(f, text=t("btn.scheduler_refresh"),
                   command=self._refresh_scheduler_status).pack(pady=12)

    def _refresh_scheduler_status(self):
        """Kick off `schtasks /Query` on a worker thread.

        `schtasks` is a heavyweight subprocess that routinely takes
        300-2000 ms (depending on how many tasks live in the Windows Task
        Scheduler). Running it on the Tk main thread froze the UI on
        every Refresh click and every periodic auto-update — now we hand
        it off to a daemon thread and update the widget from the main
        thread via `self.after(0, ...)`.
        """
        # Immediate visual cue that we're working — replaces stale text
        # so the user doesn't read outdated info while the query runs.
        self.lbl_task_status.configure(text="…")

        def _fetch():
            import scheduler
            try:
                info = scheduler.task_info(force=True)
            except Exception as exc:
                info = {"exists": False, "enabled": False,
                        "next_run": None, "status": str(exc)}
            self.after(0, lambda i=info: self._apply_scheduler_status(i))

        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_scheduler_status(self, info: dict) -> None:
        """Render scheduler info into the label — called on the UI thread."""
        if not info.get("exists"):
            self.lbl_task_status.configure(text=t("scheduler.not_created"))
        else:
            state = (t("scheduler.state_active") if info.get("enabled")
                     else t("scheduler.state_paused"))
            nxt = info.get("next_run") or "—"
            self.lbl_task_status.configure(
                text=t("scheduler.status_line", state=state, next=nxt)
            )
        self._update_statusbar()

    # ------------------------------------------------------------------
    # Scheduler action helpers (async)
    # ------------------------------------------------------------------

    def _run_scheduler_action(self, fn, args=(), refresh_after=True,
                              success_key: str | None = None):
        """Run a scheduler mutating call on a worker thread.

        Every `schtasks /Create|/Change|/Run|/Delete` call blocks for
        several hundred ms — running them on the Tk main thread freezes
        the window. We push the work to a daemon thread and pop the
        messagebox + status refresh back onto the UI thread via
        `self.after(0, …)`.

        `success_key` is the i18n key for the success message (lets us
        replace schtasks.exe's locale-dependent stdout — e.g. Russian on
        a Windows-RU machine — with our own translated text).
        """
        def _work():
            try:
                ok, msg = fn(*args)
            except Exception as exc:
                ok, msg = False, str(exc)
            self.after(0, lambda: self._after_scheduler_action(
                ok, msg, refresh_after, success_key))
        threading.Thread(target=_work, daemon=True).start()

    def _after_scheduler_action(self, ok: bool, msg: str, refresh_after: bool,
                                success_key: str | None) -> None:
        title = t("dlg.scheduler.title")
        if ok:
            # Translated success text — never the raw schtasks.exe stdout
            # (which is in the Windows display language, usually Russian).
            body = t(success_key) if success_key else t("dlg.scheduler.success_generic")
            messagebox.showinfo(title, body, parent=self)
        else:
            # Errors still carry the raw schtasks message — it usually has
            # the actual reason (access denied, task not found, etc.) and
            # the user can debug from there. We just wrap it in a translated
            # prefix.
            body = (t("dlg.scheduler.error", err=msg.strip()) if msg
                    else t("dlg.scheduler.error_generic"))
            messagebox.showerror(title, body, parent=self)
        if refresh_after:
            self._refresh_scheduler_status()

    def _sched_create(self):
        interval = self.config_data.get("schedule", {}).get("interval_minutes", 5)
        import scheduler
        self._run_scheduler_action(
            scheduler.create_or_update, (interval,),
            success_key="dlg.scheduler.created",
        )

    def _sched_enable(self):
        import scheduler
        self._run_scheduler_action(
            scheduler.enable, success_key="dlg.scheduler.enabled",
        )

    def _sched_disable(self):
        import scheduler
        self._run_scheduler_action(
            scheduler.disable, success_key="dlg.scheduler.disabled",
        )

    def _sched_run_now(self):
        import scheduler
        # Run now changes "Next Run Time", so a refresh afterwards keeps
        # the status label honest.
        self._run_scheduler_action(
            scheduler.run_now, success_key="dlg.scheduler.running",
        )
        # watch.py is spawned out-of-process (via the VBS shim → pythonw.exe)
        # and typically finishes in 2-4 s on a 2-card list. Schedule a
        # deferred Watchlist refresh so any status / last_seen / state
        # change it writes lands in the UI without the user having to
        # switch tabs back and forth.
        self.after(6000, self._refresh_watchlist)

    def _sched_delete(self):
        if not messagebox.askyesno(t("dlg.scheduler.confirm_delete.title"),
                                   t("dlg.scheduler.confirm_delete.body"),
                                   parent=self):
            return
        import scheduler
        self._run_scheduler_action(
            scheduler.delete, success_key="dlg.scheduler.deleted",
        )

    # ---- History ---------------------------------------------------------

    def _build_history_tab(self):
        # AppID hidden here for the same reason as in Watchlist. Target /
        # delta dropped because they're irrelevant once the card is closed.
        # Operation tells buy vs sell at a glance. Price relabelled to a
        # plain "Ціна" (no "покупки", since it now covers sales too).
        cols = ("num", "date", "name", "game", "operation", "price", "link")

        # Bottom strip: action buttons on the left, totals panel on the
        # right. Uses grid so the totals can be re-flowed underneath the
        # buttons when the window is too narrow to fit both on one row
        # (otherwise the stats get clipped off the right edge). The
        # reflow is wired via _reflow_history_bottom below.
        bottom = ttk.Frame(self.tab_history)
        bottom.pack(side=BOTTOM, fill=X, padx=8, pady=(0, 8))
        bottom.columnconfigure(0, weight=1)
        btn_f = ttk.Frame(bottom)
        # sticky="nw" so buttons stay at the TOP-LEFT of the cell — without
        # the "n" they'd center vertically when the row's height grows to
        # fit a multi-row stats panel, drifting away from the table edge.
        btn_f.grid(row=0, column=0, sticky="nw")
        self._hist_bottom = bottom
        self._hist_btn_f = btn_f

        hist_frame = ttk.Frame(self.tab_history, borderwidth=1, relief="solid")
        hist_frame.pack(side=TOP, fill=BOTH, expand=YES, padx=8, pady=8)

        self.hist_tree = ttk.Treeview(hist_frame, columns=cols, show="headings", selectmode="extended")
        for col, label_key, width in [
            ("num",       "col.num",       40),
            ("date",      "col.date",     140),
            ("name",      "col.card",     220),
            ("game",      "col.game",     170),
            ("operation", "col.operation", 90),
            ("price",     "col.price",     90),
            ("link",      "col.link",     110),
        ]:
            if col == "price":
                anchor = E
            elif col in ("num", "link"):
                anchor = CENTER
            else:
                anchor = W
            self.hist_tree.heading(col, text=t(label_key), anchor=anchor)
            self.hist_tree.column(col, width=width, anchor=anchor)
        self._apply_row_tags(self.hist_tree)
        self._setup_sortable_columns(self.hist_tree, list(cols))
        # Selection handler is wired AFTER the action-button row builds —
        # see further down (it needs `self.btn_hist_delete` to exist so
        # the same callback can flip its enabled state). The
        # `_mark_selected_rows` part lives in the combined handler.
        # Click on the link column opens the market listing in a browser.
        self.hist_tree.bind("<Button-1>", self._on_hist_tree_click)
        self.hist_tree.bind("<Motion>", self._on_hist_tree_motion)
        # Same Ctrl+A toggle as the card-list trees.
        self.hist_tree.bind("<Control-KeyPress>", self._on_tree_ctrl_a)
        # Right-click context menu mirroring the History buttons.
        self.hist_tree.bind("<Button-3>", self._show_hist_context_menu)
        vsb2 = ttk.Scrollbar(hist_frame, orient=VERTICAL, command=self.hist_tree.yview,
                             bootstyle="success")
        self.hist_tree.configure(
            yscrollcommand=lambda f, l, sb=vsb2: self._autohide_scrollbar(sb, f, l)
        )
        # Grid layout — same auto-hide pattern as the card-list trees.
        self.hist_tree.grid(row=0, column=0, sticky="nsew")
        vsb2.grid(row=0, column=1, sticky="ns")
        hist_frame.grid_rowconfigure(0, weight=1)
        hist_frame.grid_columnconfigure(0, weight=1)
        # Button order is intentional. "Історія придбань" first as the
        # broad "where did my money go?" link (selection-independent —
        # opens Steam's full market-transactions log). "Знов до списку"
        # is placed before "Редагувати ціну" because re-adding is the
        # more common follow-up to looking at a past purchase; price
        # editing is a niche cleanup action and lives next to Delete.
        self.btn_hist_delete = None
        for key, cmd in [
            ("btn.history_market_log", self._open_market_history),
            ("btn.history_export",     self._hist_export_csv),
            ("btn.history_readd",      self._hist_readd),
            ("btn.history_edit",       self._hist_edit),
            ("btn.history_delete",     self._hist_delete),
        ]:
            btn = ttk.Button(btn_f, text=t(key), command=cmd)
            btn.pack(side=LEFT, padx=2)
            if key == "btn.history_delete":
                # Same enable-on-select / red-when-active contract as
                # the «Видалити» button on the Покупка / Продаж tabs.
                # State flips via `_update_hist_delete_state`, bound to
                # the tree's <<TreeviewSelect>> below.
                btn.configure(state=DISABLED, bootstyle="")
                self.btn_hist_delete = btn

        # React to selection changes — the existing handler already
        # paints the row highlights; we just chain our button update
        # after it. Wrapping rather than re-binding so we don't
        # replace the original handler.
        prev_select_cb = self.hist_tree.bind("<<TreeviewSelect>>")
        def _on_hist_select(_e=None, _orig=prev_select_cb):
            self._mark_selected_rows(self.hist_tree)
            self._update_hist_delete_state()
        self.hist_tree.bind("<<TreeviewSelect>>", _on_hist_select)

        # Totals panel — purchases / sales / spent — laid out adaptively
        # by _reflow_history_bottom (three modes: inline / vertical /
        # stacked). Each "cell" is its own little Frame holding `label +
        # value` so we can re-pack the whole group between horizontal
        # and vertical orientations without recreating widgets. The "│"
        # separators only render in inline mode.
        stats = ttk.Frame(bottom)
        stats.grid(row=0, column=1, sticky="ne")
        self._hist_stats = stats
        # Persistent definitions of which cells exist — used by
        # _apply_stats_layout to recreate cells on every relayout.
        # We recreate (rather than reparent) because Tk's pack-with-in_
        # works geometrically but doesn't propagate reqsize back to the
        # cell's master — so an `in_=inner` cell renders correctly inside
        # `inner`, but `stats` thinks it's still empty (0 reqwidth) and
        # the grid column collapses, leaving the whole panel invisible.
        # Destroy-and-recreate avoids the issue at the cost of churning
        # widgets on every reflow (cheap — three Labels per cell).
        self._hist_cells_def = [
            ("hist.total_buy",  "lbl_total_buy"),
            ("hist.total_sell", "lbl_total_sell"),
            ("hist.spent",      "lbl_spent"),
        ]
        self._hist_stats_rows = []   # row Frames (children of stats)
        self._hist_stats_cells = []  # cell Frames (children of an inner frame inside a row)
        # Signature of last-applied layout for idempotency — prevents
        # Configure-event flap during drag resizes.
        self._hist_stats_signature = None
        # Approximate width of "│" separator + 2×padx(10) — used in
        # _reflow's width math. Refined to a real measurement on the
        # first reflow that renders a sep (sep widgets are short-lived,
        # so a constant estimate is good enough until then).
        self._hist_sep_width = 30

        # Build the initial inline layout so the cells appear right away.
        # _reflow_history_bottom will swap to wrap / below mode on first
        # Configure event if the measured width prefers them.
        self._apply_stats_layout(
            [list(range(len(self._hist_cells_def)))], "e", True
        )

        # Re-flow on every resize: pick the widest layout that still
        # fits in the available space. Without this the stats either
        # get clipped off the right edge (when packed) or always
        # consume an extra row even when there's plenty of room.
        bottom.bind("<Configure>", self._reflow_history_bottom)
        # Initial layout — schedule after current event loop tick so all
        # children have their reqsize computed by the time we measure.
        self.after_idle(self._reflow_history_bottom)

    def _apply_stats_layout(self, rows_spec, anchor, use_seps) -> None:
        """Rebuild the stats panel for the given multi-row spec.

        rows_spec: list of lists of cell indices. e.g. [[0, 1], [2]] →
            two rows; row 0 holds cells 0+1, row 1 holds cell 2.
        anchor: "e" (right-aligned inside each row, used when stats lives
            to the right of buttons) or "w" (left-aligned, for below mode).
        use_seps: True → render "│" between cells on the same row
            (inline / below-inline modes). False → no separator, just
            horizontal padding (wrap mode — the line break itself does
            the visual grouping).

        Implementation: full destroy + recreate. Row Frames, cells, and
        value Labels are all freshly built each call. Cells must be
        direct children of `inner` (not `stats` with `in_=inner`) so Tk
        propagates their reqsize through to `stats` and the grid column
        sizes correctly — see the note where these widgets are first
        introduced for the full story. Callers must invoke
        _refresh_history_stats() after this to repopulate the new
        value-label texts.
        """
        for row in self._hist_stats_rows:
            try:
                row.destroy()
            except tk.TclError:
                pass
        self._hist_stats_rows = []
        # `cells` need to be indexed by cell_def position (0..n-1) so the
        # reflow function can find them in original order regardless of
        # how rows_spec scrambled them — build a temp dict, then assemble
        # the ordered list at the end.
        new_cells_by_idx = {}

        stats = self._hist_stats
        cells_def = self._hist_cells_def
        for indices in rows_spec:
            row_frame = ttk.Frame(stats)
            row_frame.pack(side=TOP, fill=X, pady=1)
            self._hist_stats_rows.append(row_frame)
            # Inner sub-frame so cells can be right-aligned or left-aligned
            # cleanly — pack(side=RIGHT) on `inner` makes it hug the right
            # edge of the row, then cells pack(side=LEFT) inside `inner`
            # render in natural reading order. Same trick mirrored for "w".
            inner = ttk.Frame(row_frame)
            inner.pack(side=RIGHT if anchor == "e" else LEFT)
            for i, cell_idx in enumerate(indices):
                if i > 0 and use_seps:
                    sep = ttk.Label(inner, text="│",
                                    foreground="#666666")
                    sep.pack(side=LEFT, padx=10)
                    # First sep we render this session — refine our
                    # constant estimate so future reflow decisions are
                    # accurate to within a couple of pixels.
                    if self._hist_sep_width == 30:
                        try:
                            sep.update_idletasks()
                            measured = sep.winfo_reqwidth() + 20
                            if measured > 0:
                                self._hist_sep_width = measured
                        except tk.TclError:
                            pass

                key, attr = cells_def[cell_idx]
                cell = ttk.Frame(inner)
                ttk.Label(cell, text=t(key)).pack(side=LEFT, padx=(0, 4))
                value_label = ttk.Label(cell, text="0.00")
                value_label.pack(side=LEFT)
                setattr(self, attr, value_label)
                new_cells_by_idx[cell_idx] = cell

                # Padding between cells: separator already gives breathing
                # room; without one we add an explicit 12px on the left.
                pad = 0 if (i == 0 or use_seps) else (12, 0)
                cell.pack(side=LEFT, padx=pad)

        self._hist_stats_cells = [
            new_cells_by_idx[i] for i in range(len(cells_def))
        ]

    @staticmethod
    def _wrap_cells_greedy(widths, sep_w, avail):
        """Greedy left-to-right wrap into rows that each fit `avail`.

        Multi-cell rows include `sep_w` of horizontal spacing between
        adjacent cells. A row is allowed to contain a single cell even
        if its width slightly exceeds `avail` — better than infinite
        loop / no rows at all. (Caller should fall back to stacked
        below-mode when even the widest single cell can't fit, see
        _reflow_history_bottom's decision tree.)
        """
        rows, current, current_w = [], [], 0
        for i, w in enumerate(widths):
            addition = w if not current else (sep_w + w)
            if current and current_w + addition > avail:
                rows.append(current)
                current, current_w = [i], w
            else:
                current.append(i)
                current_w += addition
        if current:
            rows.append(current)
        return rows

    def _reflow_history_bottom(self, _event=None) -> None:
        """Pick the best stats layout for the current bottom-strip width.

        Three tiers (matches the user's spec):
          1. `right_avail` ≥ inline_w → all cells in one row right of
             buttons, with "│" separators between them.
          2. else, `right_avail` ≥ widest cell width → wrap cells into
             multiple right-aligned rows (still right of buttons), each
             row holding as many cells as fit; no separators (the line
             break itself groups them).
          3. else → drop the whole group below the buttons in a single
             left-aligned row with separators. Last resort — only when
             even one full cell can't fit to the right of the buttons.

        Idempotent: a layout signature is cached and a no-op runs when
        the target matches it — protects against Configure-flap during
        drag resizes.
        """
        bottom = getattr(self, "_hist_bottom", None)
        btn_f = getattr(self, "_hist_btn_f", None)
        stats = getattr(self, "_hist_stats", None)
        cells = getattr(self, "_hist_stats_cells", None)
        if not bottom or not btn_f or not stats or not cells:
            return
        try:
            avail = bottom.winfo_width()
            if avail <= 1:
                return
            cell_widths = [c.winfo_reqwidth() for c in cells]
            sep_w = self._hist_sep_width
            btn_w = btn_f.winfo_reqwidth()
            # 24 = breathing room between buttons block and stats group
            # so they don't visually kiss when layout is "just barely fits".
            right_avail = avail - btn_w - 24
            inline_w = sum(cell_widths) + sep_w * (len(cell_widths) - 1)
            max_cell_w = max(cell_widths) if cell_widths else 0

            if right_avail >= inline_w:
                rows_spec = [list(range(len(cell_widths)))]
                target_grid = {"row": 0, "column": 1,
                               "sticky": "ne", "pady": 0}
                anchor = "e"
                use_seps = True
            elif right_avail >= max_cell_w:
                # Wrap mode keeps the "│" separator between cells that
                # share a row (per user request). sep_w fed into the wrap
                # algorithm so the row capacity math matches what we'll
                # actually render.
                rows_spec = self._wrap_cells_greedy(
                    cell_widths, sep_w, right_avail
                )
                target_grid = {"row": 0, "column": 1,
                               "sticky": "ne", "pady": 0}
                anchor = "e"
                use_seps = True
            else:
                rows_spec = [list(range(len(cell_widths)))]
                target_grid = {"row": 1, "column": 0,
                               "sticky": "w", "pady": (6, 0)}
                anchor = "w"
                use_seps = True

            sig = (target_grid["row"], target_grid["column"],
                   target_grid["sticky"], anchor, use_seps,
                   tuple(tuple(r) for r in rows_spec))
            if sig == self._hist_stats_signature:
                return

            self._apply_stats_layout(rows_spec, anchor, use_seps)
            stats.grid_configure(**target_grid)
            self._hist_stats_signature = sig
            # _apply_stats_layout recreates the value labels — repopulate
            # them with current totals so they don't read "0.00" until
            # the next data refresh. Cheap (just reads purchases.json).
            self._refresh_history_stats()
        except (tk.TclError, KeyError, ValueError):
            pass

    def _refresh_history(self):
        from steam import pretty_name

        self.hist_tree.delete(*self.hist_tree.get_children())
        purchases = load_json(PURCHASES_PATH, [])
        for row_index, p in enumerate(reversed(purchases)):
            ts = p.get("timestamp", "")[:19].replace("T", " ")
            price_str = str(p.get("price", "—"))
            display_name = pretty_name(p)
            game_name = p.get("game_name") or "—"
            # Operation defaults to "buy" for legacy purchase records that
            # were written before the sale-side existed.
            op_key = p.get("operation") or "buy"
            operation = t(f"operation.{op_key}")
            if operation == f"operation.{op_key}":  # i18n miss
                operation = op_key
            row_tag = "even" if row_index % 2 == 0 else "odd"
            # iid = timestamp + mhn → uniquely identifies the row even when
            # the same card was bought multiple times. Used by _hist_delete
            # / _hist_selected to match the exact record (display_name
            # alone would collide on duplicates).
            iid = f"{p.get('timestamp', '')}|{p.get('market_hash_name', '')}"
            self.hist_tree.insert("", END, iid=iid, values=(
                row_index + 1,
                ts, display_name, game_name, operation, price_str,
                t("col.link.open"),
            ), tags=(row_tag,))
        self._mark_selected_rows(self.hist_tree)
        # Persisted sort order across restarts — same contract as the
        # card-list trees.
        self._restore_sort_state(self.hist_tree)
        # Re-sync the «Видалити» button — refresh wipes selection,
        # which fires TreeviewSelect, but the button state could be
        # stale if e.g. we just deleted the last selected row.
        self._update_hist_delete_state()
        self._refresh_history_stats(purchases)

    def _refresh_history_stats(self, purchases: list | None = None) -> None:
        """Recompute "Всього покупок / Сума продажів / Витрачено" totals.

        Pass `purchases` to avoid a second disk read when called from
        _refresh_history; otherwise we load it ourselves.
        """
        if purchases is None:
            purchases = load_json(PURCHASES_PATH, []) or []
        total_buy = 0.0
        total_sell = 0.0
        for p in purchases:
            amount = _try_parse_money(p.get("price"))
            if amount is None:
                continue
            # Legacy entries without `operation` default to "buy".
            if (p.get("operation") or "buy") == "sell":
                total_sell += amount
            else:
                total_buy += amount
        spent = total_buy - total_sell

        if hasattr(self, "lbl_total_buy"):
            sym = self._currency_symbol()
            self.lbl_total_buy.configure(text=f"{total_buy:.2f} {sym}")
            self.lbl_total_sell.configure(text=f"{total_sell:.2f} {sym}")
            # Spent: green-ish if positive (we earned more than spent? no —
            # actually spent = buy - sell, so negative means we earned).
            # Per the user's mock-up, negative goes red.
            # We compute `spent = buy − sell`, but display the OPPOSITE
            # sign so the number reads like a balance: `+X` means earned,
            # `−X` means lost. Colour matches: red on a loss, green on a
            # gain, neutral when even.
            display = -spent
            self.lbl_spent.configure(text=f"{display:+.2f} {sym}")
            if spent > 0:        # purchases outweigh sales → loss
                self.lbl_spent.configure(foreground="#FF6B6B")
            elif spent < 0:      # sales outweigh purchases → gain
                self.lbl_spent.configure(foreground="#16A34A")
            else:
                self.lbl_spent.configure(foreground="")

    def _hist_selected(self):
        """Return the first purchase record under the currently-selected row.

        Back-compat shim — most history actions are now multi-select aware
        and use _hist_selected_items() instead.
        """
        sel = self._hist_selected_items()
        if not sel:
            messagebox.showwarning(t("dlg.select.title"), t("dlg.select.body"), parent=self)
            return None
        return sel[0]

    def _hist_selected_items(self) -> list[dict]:
        """All currently-selected purchase records.

        Each Treeview iid is "timestamp|mhn" — uniquely identifies a row
        even when the same card was bought twice on the same minute.
        Returns [] silently if nothing's selected.
        """
        sel = self.hist_tree.selection()
        if not sel:
            return []
        purchases = load_json(PURCHASES_PATH, []) or []
        # Index by (timestamp, mhn) for O(1) lookup.
        index = {(p.get("timestamp", ""), p.get("market_hash_name", "")): p
                 for p in purchases}
        out: list[dict] = []
        for iid in sel:
            try:
                ts, mhn = iid.split("|", 1)
            except ValueError:
                continue
            p = index.get((ts, mhn))
            if p is not None:
                out.append(p)
        return out

    def _require_hist_selection(self) -> list[dict]:
        """Like _hist_selected_items, but pops a warning if empty."""
        sel = self._hist_selected_items()
        if not sel:
            messagebox.showwarning(t("dlg.select.title"), t("dlg.select.body"), parent=self)
        return sel

    def _hist_delete(self):
        """Remove selected purchase record(s) + refresh totals."""
        from steam import pretty_name

        selected = self._require_hist_selection()
        if not selected:
            return
        if len(selected) == 1:
            body = t("dlg.hist_delete.body", name=pretty_name(selected[0]))
        else:
            body = t("dlg.hist_delete.body_multi", count=len(selected))
        if not messagebox.askyesno(t("dlg.hist_delete.title"), body, parent=self):
            return
        targets = {
            (p.get("timestamp"), p.get("market_hash_name"))
            for p in selected
        }
        purchases = load_json(PURCHASES_PATH, [])
        purchases = [
            x for x in purchases
            if (x.get("timestamp"), x.get("market_hash_name")) not in targets
        ]
        save_json(PURCHASES_PATH, purchases)
        self._refresh_history()  # also recalculates the totals panel

    # ------------------------------------------------------------------
    # History link-column click handling
    # ------------------------------------------------------------------

    _HIST_LINK_COL_ID = "#7"  # num=#1, date=#2, name=#3, game=#4, operation=#5, price=#6, link=#7

    def _on_hist_tree_click(self, event):
        """Open the listing URL when the user clicks the History link cell."""
        if self.hist_tree.identify_region(event.x, event.y) != "cell":
            return
        if self.hist_tree.identify_column(event.x) != self._HIST_LINK_COL_ID:
            return
        # _hist_selected() already returns the purchase under the focused
        # row by name lookup, but here we have the click coords so we can
        # be precise even before <<TreeviewSelect>> fires.
        iid = self.hist_tree.identify_row(event.y)
        if not iid:
            return
        # Make sure the row is selected so _hist_selected can resolve it.
        self.hist_tree.selection_set(iid)
        p = self._hist_selected()
        if not p:
            return
        # Game purchases (kind == "game", written by «Вже придбав» on
        # the Ігри tab) link to the STORE page — they have no market
        # listing to open.
        if p.get("kind") == "game":
            from steam import GAME_STORE_URL
            webbrowser.open(GAME_STORE_URL.format(appid=p["appid"]))
            return
        from steam import market_url
        webbrowser.open(market_url(p["appid"], p.get("market_hash_name", p.get("name"))))

    def _on_hist_tree_motion(self, event):
        """Hand cursor when hovering the link column."""
        in_link = (
            self.hist_tree.identify_region(event.x, event.y) == "cell"
            and self.hist_tree.identify_column(event.x) == self._HIST_LINK_COL_ID
            and self.hist_tree.identify_row(event.y)
        )
        self.hist_tree.configure(cursor="hand2" if in_link else "")

    def _open_market_history(self):
        """Open Steam's market transactions log in the user's browser.

        Selection-independent — this is the broad "show me everything
        I bought/sold on Steam Market" link, separate from the per-card
        market URL the table's «🔗» link column still gives.

        Steam routes `/market/#myhistory` to the user's actual purchase
        history once they're logged in; for not-logged-in browsers it
        bounces to the login flow, which is the right behaviour either
        way (browsing → see what's there, not logged in → log in).
        """
        webbrowser.open("https://steamcommunity.com/market/#myhistory")

    def _hist_edit(self):
        """Edit the price on selected History record(s).

        Multi-select: one dialog per record, with the current price as
        initial value. Cancel skips THAT record and moves on to the next —
        the rest of the selection keeps going.
        """
        from steam import pretty_name

        selected = self._require_hist_selection()
        if not selected:
            return
        sym = self._currency_symbol()
        purchases = load_json(PURCHASES_PATH, []) or []
        # Index by (timestamp, mhn) — that's the unique key per history row.
        index = {(p.get("timestamp", ""), p.get("market_hash_name", "")): p
                 for p in purchases}
        edited = 0
        for chosen in selected:
            key = (chosen.get("timestamp", ""), chosen.get("market_hash_name", ""))
            target = index.get(key)
            if target is None:
                continue
            current = _try_parse_money(target.get("price"))
            default_str = (f"{current:.2f}" if isinstance(current, (int, float))
                           else "")
            price_str = simpledialog.askstring(
                t("dlg.hist_edit.title"),
                t("dlg.hist_edit.body", name=pretty_name(target), sym=sym),
                initialvalue=default_str,
                parent=self,
            )
            if price_str is None:
                # Cancel → skip this record, keep walking the selection.
                continue
            try:
                price_val = float(price_str.replace(",", "."))
            except ValueError:
                # Malformed number → show error, skip this row (don't abort).
                messagebox.showerror(
                    t("dlg.error.title"), t("dlg.bad_number"), parent=self,
                )
                continue
            target["price"] = f"{price_val:.2f} {sym}".rstrip()
            edited += 1

        if edited:
            save_json(PURCHASES_PATH, purchases)
            self._refresh_history()  # also recalculates the totals panel

    def _hist_export_csv(self):
        """Save the History tab to a CSV — only the columns the user sees.

        Output mirrors the visible History Treeview exactly: Дата /
        Картка / Назва гри / Операція / Ціна / Посилання (URL).
        We intentionally drop appid, market_hash_name and target since
        those aren't on screen — the export is for humans browsing
        their own history in Excel, not for re-importing into the app.

        Localised headers (via the same col.* i18n keys the Treeview
        uses) so the CSV reads the same as the tab when opened in a
        spreadsheet.

        The old export wrote `fieldnames=["timestamp","name","appid",
        "price","target"]` with the default `extrasaction="raise"` and
        crashed on the very first row because the on-disk records grew
        more fields (display_name, game_name, operation, …) over time
        — leaving the file with just the header line. Building each
        row explicitly keeps the column set stable regardless of what
        extra keys live on the source dict.
        """
        from tkinter.filedialog import asksaveasfilename
        from steam import market_url, pretty_name

        # Default to the user's localised "Purchase history" — Tkinter
        # appends `.csv` itself thanks to `defaultextension`.
        path = asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=t("dlg.export.filename_default"),
        )
        if not path:
            return
        purchases = load_json(PURCHASES_PATH, []) or []
        # (csv-column-key, header-text) pairs — keys are internal and
        # never user-visible; headers come from the same i18n keys the
        # tab uses for its column captions so the CSV header line reads
        # the same as the table the user just looked at.
        columns = [
            ("date",      t("col.date")),
            ("card",      t("col.card")),
            ("game",      t("col.game")),
            ("operation", t("col.operation")),
            ("price",     t("col.price")),
            ("link",      t("col.link")),
        ]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([header for _, header in columns])
            for p in purchases:
                appid = p.get("appid")
                mhn = p.get("market_hash_name") or p.get("name") or ""
                op_key = p.get("operation", "buy")
                # Translate the raw "buy"/"sell" into the localised
                # word the History column shows ("придбання"/"продаж").
                op_label = t(f"operation.{op_key}")
                writer.writerow([
                    p.get("timestamp", ""),
                    pretty_name(p),
                    p.get("game_name", ""),
                    op_label,
                    p.get("price", ""),
                    market_url(appid, mhn) if appid and mhn else "",
                ])
        messagebox.showinfo(t("dlg.export.title"), t("dlg.export.saved", path=path))

    def _show_hist_context_menu(self, event) -> None:
        """Right-click menu on the History table — mirrors its buttons.

        Same selection convention as the card lists: clicking inside
        the current selection keeps it, clicking an unselected row
        re-targets the selection to that row, empty space → no menu.
        Selection-dependent entries grey out without a selection.
        """
        tree = self.hist_tree
        iid = tree.identify_row(event.y)
        if not iid:
            return
        if iid not in tree.selection():
            tree.selection_set(iid)
            tree.focus(iid)
            self._mark_selected_rows(tree)
            self._update_hist_delete_state()
        menu = tk.Menu(self, tearoff=0, font=self._context_menu_font())
        menu.add_command(label=t("btn.history_market_log"),
                         command=self._open_market_history)
        menu.add_command(label=t("btn.history_export"),
                         command=self._hist_export_csv)
        menu.add_separator()
        menu.add_command(label=t("btn.history_readd"),
                         command=self._hist_readd)
        menu.add_command(label=t("btn.history_edit"),
                         command=self._hist_edit)
        menu.add_separator()
        menu.add_command(label=t("btn.history_delete"),
                         command=self._hist_delete)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _games_readd_records(self, recs: list[dict]) -> None:
        """Return game purchases from History to the «Ігри» list.

        Dedup by appid — the games list doesn't tolerate duplicates: an
        active row means "already tracked" and is reported, a closed
        (bought) row is resurrected, a missing one is recreated minimal
        (prices fill in on the next check). Always ends with a visible
        messagebox so the move is never silent.
        """
        gitems = load_json(GAMELIST_PATH, []) or []
        gstate = load_json(STATE_PATH, {}) or {}
        returned: list[str] = []
        already: list[str] = []
        seen: set = set()
        for p in recs:
            appid = p.get("appid")
            if appid in seen:
                continue
            seen.add(appid)
            gname = p.get("game_name") or p.get("name") or f"app {appid}"
            existing = next(
                (g for g in gitems if g.get("appid") == appid), None)
            if existing is not None and \
                    existing.get("status") not in CLOSED_STATUSES:
                already.append(gname)
                continue
            gstate.pop(f"game:{appid}:{gname}", None)
            if existing is not None:
                existing["status"] = ""
            else:
                gitems.append({
                    "id": str(uuid.uuid4()),
                    "appid": appid,
                    "name": gname,
                    "price": None, "price_str": "",
                    "regular": None, "discount_pct": 0,
                    "lowest_cut": None, "status": "",
                    "imported": False,
                    "added": datetime.now(timezone.utc).isoformat(),
                })
            returned.append(gname)
        if returned:
            save_json(GAMELIST_PATH, gitems)
            save_json(STATE_PATH, gstate)
            self._refresh_games_list()
        lines = []
        if returned:
            lines.append(t("dlg.games_readd.returned",
                           names=", ".join(returned)))
        if already:
            lines.append(t("dlg.games_readd.already",
                           names=", ".join(already)))
        if lines:
            messagebox.showinfo(t("dlg.games_readd.title"),
                                "\n\n".join(lines), parent=self)

    def _hist_readd(self):
        """Re-add selected history record(s) back into the watch/sale list.

        Multi-select aware. Behaviour:
        - Pick the destination kind (buy/sell) ONCE for the whole selection.
        - Group selected purchases by card identity (appid, name). Same card
          picked multiple times in the History = one dialog asking the
          target price; Cancel on that dialog skips the whole group.
        - For sell destination: each row in the group adds a new copy
          (duplicates are the point of the sell list). For buy destination:
          dedup to one row per mhn — resurrect a closed entry if there is
          one, otherwise add a single fresh row.
        """
        from steam import pretty_name

        selected = self._require_hist_selection()
        if not selected:
            return

        # Game purchases (kind == "game") can ONLY go back to the «Ігри»
        # list — no destination dialog for them. Cards proceed through
        # the usual Покупка/Продаж picker. A mixed selection handles
        # both halves independently.
        game_recs = [p for p in selected if p.get("kind") == "game"]
        selected = [p for p in selected if p.get("kind") != "game"]
        if game_recs:
            self._games_readd_records(game_recs)
        if not selected:
            return

        # Group by card identity. Preserve discovery order for predictable
        # dialog sequence.
        groups: dict[tuple, list[dict]] = {}
        for p in selected:
            ident = (p.get("appid"), p.get("name"))
            groups.setdefault(ident, []).append(p)

        # Destination kind — one choice for the whole batch (otherwise the
        # UX would explode with N prompts asking buy/sell).
        prompt_name = (pretty_name(selected[0]) if len(selected) == 1
                       else t("dlg.choose_op.multi", count=len(selected)))
        kind = self._ask_operation_kind(prompt_name)
        if kind is None:
            return
        path = self._kind_path(kind)

        items = load_json(path, [])
        state = load_json(STATE_PATH, {}) or {}
        state_dirty = False
        added_mhns: list[str] = []
        already_active: list[str] = []

        for ident, group in groups.items():
            sample = group[0]
            mhn = sample.get("market_hash_name", sample.get("name", "?"))
            pretty = pretty_name(sample)

            # Per-group price dialog — Cancel skips this card entirely.
            target_str = simpledialog.askstring(
                t("dlg.readd.title"),
                t("dlg.readd.body", name=pretty),
                initialvalue=str(sample.get("target", "")),
                parent=self,
            )
            if target_str is None:
                continue
            try:
                target = float(target_str.replace(",", "."))
            except ValueError:
                messagebox.showerror(
                    t("dlg.error.title"), t("dlg.bad_number_short"), parent=self,
                )
                continue

            # Reset antispam — re-add should track from scratch.
            state_key = f"{kind}:{sample.get('appid')}:{mhn}"
            if state.pop(state_key, None) is not None:
                state_dirty = True

            if kind == "buy":
                existing = next(
                    (w for w in items if w.get("market_hash_name") == mhn),
                    None,
                )
                if existing is not None:
                    if existing.get("status") in CLOSED_STATUSES:
                        existing["status"] = ""
                        existing["target_price"] = target
                        existing["last_seen"] = "—"
                        if (not existing.get("display_name")
                                or existing["display_name"] == mhn):
                            existing["display_name"] = pretty
                        if not existing.get("id"):
                            existing["id"] = str(uuid.uuid4())
                        added_mhns.append(mhn)
                    else:
                        already_active.append(pretty)
                    continue
                # Buy + fresh add
                items.append({
                    "id": str(uuid.uuid4()),
                    "name": sample.get("name"),
                    "display_name": pretty,
                    "game_name": sample.get("game_name") or "—",
                    "image_url": sample.get("image_url"),
                    "appid": sample.get("appid"),
                    "market_hash_name": mhn,
                    "target_price": target,
                    "status": "",
                    "last_seen": "—",
                })
                added_mhns.append(mhn)
            else:
                # Sell: replicate the user's history selection — N picked,
                # N rows added (each as its own copy on sale).
                for _ in group:
                    items.append({
                        "id": str(uuid.uuid4()),
                        "name": sample.get("name"),
                        "display_name": pretty,
                        "game_name": sample.get("game_name") or "—",
                        "image_url": sample.get("image_url"),
                        "appid": sample.get("appid"),
                        "market_hash_name": mhn,
                        "target_price": target,
                        "status": "",
                        "last_seen": "—",
                    })
                added_mhns.append(mhn)

        save_json(path, items)
        if state_dirty:
            save_json(STATE_PATH, state)
        self._refresh_card_list(kind)

        # Background-fetch metadata + a price tick for everything just added.
        # _post_readd_refresh is per-mhn — dedup first.
        for mhn in dict.fromkeys(added_mhns):
            self._post_readd_refresh(mhn, kind)

        # Final user feedback — single confirmation, no per-group spam.
        if added_mhns or already_active:
            if len(added_mhns) == 1 and not already_active:
                # Keep the legacy single-card confirmation phrasing.
                messagebox.showinfo(
                    t("dlg.readd.title"),
                    t("dlg.readd.done", name=pretty_name(selected[0])),
                    parent=self,
                )
            else:
                lines = []
                if added_mhns:
                    lines.append(t("dlg.readd.done_count",
                                   count=len(added_mhns)))
                if already_active:
                    lines.append(t("dlg.readd.already_list",
                                   names=", ".join(already_active)))
                messagebox.showinfo(t("dlg.readd.title"),
                                    "\n".join(lines), parent=self)

    def _post_readd_refresh(self, market_hash_name: str, kind: str) -> None:
        """Background-fetch metadata + current price for a freshly-readded card.

        Operates on the list matching `kind` (buy → watchlist.json, sell →
        salelist.json). Runs on a daemon thread so the dialog can close
        immediately.
        """
        path = self._kind_path(kind)

        def _work():
            from steam import fetch_card_metadata, get_price
            cfg = self.config_data
            try:
                items = load_json(path, [])
                w = next(
                    (x for x in items if x.get("market_hash_name") == market_hash_name),
                    None,
                )
                if not w:
                    return
                needs_meta = (
                    not w.get("game_name") or w["game_name"] == "—"
                    or not w.get("image_url")
                )
                if needs_meta:
                    try:
                        meta = fetch_card_metadata(w["appid"], market_hash_name)
                        if not w.get("display_name") or w["display_name"] == market_hash_name:
                            w["display_name"] = meta["display_name"]
                        if not w.get("game_name") or w["game_name"] == "—":
                            w["game_name"] = meta["game_name"]
                        if not w.get("image_url") and meta.get("image_url"):
                            w["image_url"] = meta["image_url"]
                    except Exception:
                        pass
                try:
                    info = get_price(
                        w["appid"], market_hash_name,
                        cfg.get("market", {}).get("currency", 18),
                        cfg.get("market", {}).get("country", "UA"),
                    )
                    w["last_seen"] = info.get("lowest_price_raw") or f"{info.get('lowest_price'):.2f}"
                except Exception:
                    pass
                save_json(path, items)
            except Exception:
                pass
            self.after(0, lambda k=kind: self._refresh_card_list(k))
        threading.Thread(target=_work, daemon=True).start()

    # ---- Log -------------------------------------------------------------

    def _build_log_tab(self):
        btn_f = ttk.Frame(self.tab_log)
        btn_f.pack(side=BOTTOM, fill=X, padx=8, pady=(0, 8))

        log_frame = ttk.Frame(self.tab_log, borderwidth=1, relief="solid")
        log_frame.pack(side=TOP, fill=BOTH, expand=YES, padx=8, pady=8)

        self.log_text = tk.Text(log_frame, wrap=tk.WORD, state=DISABLED,
                                font=("Consolas", 9), borderwidth=0)
        vsb3 = ttk.Scrollbar(log_frame, orient=VERTICAL, command=self.log_text.yview,
                             bootstyle="success")
        self.log_text.configure(
            yscrollcommand=lambda f, l, sb=vsb3: self._autohide_scrollbar(sb, f, l)
        )
        # Grid layout — same auto-hide pattern as the trees.
        self.log_text.grid(row=0, column=0, sticky="nsew")
        vsb3.grid(row=0, column=1, sticky="ns")
        log_frame.grid_rowconfigure(0, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        # Severity tags — colours chosen for the superhero (dark) theme, but
        # they stay readable on light themes too (red/orange/grey are
        # high-contrast on white as well).
        self.log_text.tag_configure("ERROR", foreground="#ff6b6b")
        self.log_text.tag_configure("WARN",  foreground="#ffc94d")
        self.log_text.tag_configure("DEBUG", foreground="#888888")
        # The "Сейчас" / "Готов" prefix gets highlighted too — easier to skim.
        self.log_text.tag_configure("INFO",  foreground="")

        ttk.Button(btn_f, text=t("btn.log_clear"),       command=self._clear_log).pack(side=LEFT, padx=2)
        ttk.Button(btn_f, text=t("btn.log_open_folder"), command=self._open_log_folder).pack(side=LEFT, padx=2)
        ttk.Button(btn_f, text=t("btn.log_refresh"),     command=self._refresh_log).pack(side=LEFT, padx=2)

    def _refresh_log(self):
        if not LOG_PATH.exists():
            lines = [t("log.file_missing") + "\n"]
        else:
            try:
                with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                lines = lines[-300:]
            except Exception as exc:
                lines = [f"{exc}\n"]

        self.log_text.configure(state=NORMAL)
        self.log_text.delete("1.0", END)

        # Insert line by line so we can tag the whole line by its severity
        # (cheaper than scanning the buffer afterwards for [ERROR] etc.).
        for line in lines:
            if "[ERROR]" in line:
                tag = "ERROR"
            elif "[WARNING]" in line:
                tag = "WARN"
            elif "[DEBUG]" in line:
                tag = "DEBUG"
            else:
                tag = "INFO"
            self.log_text.insert(END, line, tag)

        self.log_text.see(END)
        self.log_text.configure(state=DISABLED)

    def _clear_log(self):
        if not messagebox.askyesno(t("dlg.clear_log.title"), t("dlg.clear_log.body")):
            return
        if LOG_PATH.exists():
            LOG_PATH.write_text("", encoding="utf-8")
        self._refresh_log()

    def _open_log_folder(self):
        os.startfile(str(BASE))

    def _on_tab_changed(self, _event=None) -> None:
        """Refresh tab contents from disk when the user switches to them.

        Dispatches on the selected tab's WIDGET PATH, not its index —
        the «Ігри» tab is dynamically hidden/shown by the bonus-content
        checkbox, and index-based dispatch would silently re-route every
        tab to the wrong refresher whenever the set of visible tabs
        changes.

        watch.py runs out-of-process (Task Scheduler / Run now), so the
        in-memory state in the GUI goes stale between user actions. The
        cheapest way to stay in sync is to re-read the relevant file when
        the user looks at the tab.
        """
        try:
            selected = self.notebook.select()  # tab window pathname
        except tk.TclError:
            return

        def _path(tab) -> str:
            return str(getattr(tab, "_holder", tab))

        if selected == _path(self.tab_purchase):
            self._refresh_card_list("buy")
        elif selected == _path(self.tab_sales):
            self._refresh_card_list("sell")
        elif selected == _path(self.tab_games):
            self._refresh_games_list()
        elif selected == _path(self.tab_history):
            self._refresh_history()
        elif selected == _path(self.tab_log):
            self._refresh_log()

    def _start_log_autoupdate(self):
        def _tick():
            # Widget-path comparison, same reason as _on_tab_changed —
            # the dynamic «Ігри» tab makes numeric indices unstable.
            try:
                if self.notebook.select() == str(self.tab_log):
                    self._refresh_log()
            except tk.TclError:
                pass
            # Scheduler tab no longer auto-refreshes — schtasks /Query is
            # expensive and the status only changes when the user mutates
            # the task through our own buttons (which already trigger a
            # refresh). Hitting "Refresh" works for the rare external case.
            self.after(2000, _tick)
        self.after(2000, _tick)

    # ---- Status bar ------------------------------------------------------

    def _set_status(self, msg: str):
        self.statusbar.configure(text=f"  {msg}")

    def _update_statusbar(self):
        import scheduler
        info = scheduler.task_info()

        def _active_count(path):
            items = load_json(path, []) or []
            # Only count rows that are still tracked — i.e. not closed
            # (bought / sold). Matches what gets shown in the table.
            return sum(1 for w in items if w.get("status") not in CLOSED_STATUSES)

        buy_count = _active_count(WATCHLIST_PATH)
        sell_count = _active_count(SALELIST_PATH)

        if info["exists"] and info["enabled"]:
            nxt = info.get("next_run") or "—"
            text = t("status.task_active", next=nxt, buy=buy_count, sell=sell_count)
        elif info["exists"]:
            text = t("status.task_disabled", buy=buy_count, sell=sell_count)
        else:
            text = t("status.task_missing", buy=buy_count, sell=sell_count)
        # Games count rides along only while the «Ігри» tab is in front —
        # it's noise in the card-centric tabs.
        try:
            games_holder = getattr(getattr(self, "tab_games", None),
                                   "_holder", None)
            if (games_holder is not None
                    and self.notebook.select() == str(games_holder)):
                games = load_json(GAMELIST_PATH, []) or []
                active_games = [g for g in games
                                if g.get("status") not in CLOSED_STATUSES]
                discounted = sum(1 for g in active_games
                                 if (g.get("discount_pct") or 0) > 0)
                text += t("status.games_count",
                          count=len(active_games), discounted=discounted)
        except tk.TclError:
            pass
        self.statusbar.configure(text="  " + text)


if __name__ == "__main__":
    app = App()
    app.mainloop()