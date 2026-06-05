"""Steam Card Price Watch — GUI (ttkbootstrap, no console window)."""
import csv
import json
import logging
import os
import shutil
import subprocess
import threading
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
        if not key.startswith(("buy:", "sell:")):
            state["buy:" + key] = state.pop(key)
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
        "alerted":      {"background": "#2d4a3b", "foreground": "#d6f5e0"},
        "error":        {"background": "#5a2d2d", "foreground": "#f5d6d6"},
        # Rate-limited: muted gold. Distinct from green (alerted) and red
        # (real error) — Steam just told us to back off, the card isn't
        # broken, we just couldn't poll it this round.
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
        self._build_ui()
        # ttkbootstrap reshuffles some style maps as the notebook widget
        # comes online during _build_ui; re-pin the tab-selected colour
        # so the active tab is correctly tinted from the very first paint.
        self._configure_notebook_tab_style()
        # Tint the native Windows title bar to match the theme bg. Has to
        # happen after the window is fully realised (HWND exists).
        self._apply_native_titlebar_theme()
        self._refresh_watchlist()
        self._refresh_scheduler_status()
        self._refresh_history()
        self._start_log_autoupdate()
        # Backfill display_name/game_name for any pre-existing entries that
        # were saved before the metadata fields existed. Runs once on a
        # background thread so the GUI doesn't block on Steam Store.
        threading.Thread(target=self._backfill_metadata, daemon=True).start()

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

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
    _UNSORTABLE_COLS = {"num", "link"}

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
        placeholders at the end regardless of direction.
        """
        prev_col = getattr(tree, "_sort_col", None)
        prev_desc = getattr(tree, "_sort_desc", False)
        descending = (col == prev_col and not prev_desc)
        tree._sort_col = col
        tree._sort_desc = descending

        pairs = [(tree.set(iid, col), iid) for iid in tree.get_children("")]

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
                                    relief="sunken", anchor=W)
        self.statusbar.pack(fill=X, side=BOTTOM, ipady=2)

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
        self.tab_purchase  = ttk.Frame(self.notebook)
        self.tab_sales     = ttk.Frame(self.notebook)
        self.tab_history   = ttk.Frame(self.notebook)
        self.tab_scheduler = ttk.Frame(self.notebook)
        self.tab_log       = ttk.Frame(self.notebook)
        self.tab_settings  = ttk.Frame(self.notebook)

        for tab, key in [
            (self.tab_purchase,  "tab.purchase"),
            (self.tab_sales,     "tab.sales"),
            (self.tab_history,   "tab.history"),
            (self.tab_scheduler, "tab.scheduler"),
            (self.tab_log,       "tab.log"),
            (self.tab_settings,  "tab.settings"),
        ]:
            self.notebook.add(tab, text=t(key))

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
        self._build_settings_tab()
        self._build_scheduler_tab()
        self._build_history_tab()
        self._build_log_tab()

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
        cluster = ttk.Frame(self)
        cluster.place(relx=1.0, x=-14, y=5, anchor="ne")

        # Two-line text column (username on top, balance below). Right-aligned
        # so longer nicknames push leftward, leaving the avatar pinned.
        text_col = ttk.Frame(cluster)
        text_col.pack(side=LEFT, padx=(0, 8))

        self.lbl_username = ttk.Label(
            text_col, text="Username", anchor=E, font=("", 10, "bold"),
        )
        self.lbl_username.pack(side=TOP, anchor=E)

        # Wallet balance — same currency symbol the rest of the app uses
        # so it stays consistent if the user later switches currency.
        sym = self._currency_symbol()
        # Muted foreground — match Steam's secondary-text colour.
        muted_fg = "#888888"
        self.lbl_balance = ttk.Label(
            text_col, text=f"0.00 {sym}", anchor=E,
            foreground=muted_fg,
        )
        self.lbl_balance.pack(side=TOP, anchor=E)

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
            self.lbl_balance.configure(text=balance)
        if avatar_image is not None:
            # Keep a reference — Tk's image GC will collect it otherwise.
            self._user_avatar_ref = avatar_image
            self.avatar_canvas.delete("all")
            self.avatar_canvas.create_image(
                self._AVATAR_SIZE / 2, self._AVATAR_SIZE / 2,
                image=avatar_image,
            )

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

    # Steam Market currency code → display symbol. Used by history totals.
    # Codes come from Steam Market API; the four below are the only ones we
    # actually offer in the Settings dropdown.
    _CURRENCY_SYMBOLS = {1: "$", 3: "€", 5: "₽", 18: "₴"}

    def _currency_symbol(self) -> str:
        code = self.config_data.get("market", {}).get("currency", 18)
        return self._CURRENCY_SYMBOLS.get(code, "")

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
        cols = ("num", "name", "game", "target", "last", "spread", "status", "link")
        headings = [
            ("num",    t("col.num"),         40),
            ("name",   t("col.name"),       200),
            ("game",   t("col.game"),       170),
            ("target", t("col.target"),      80),
            ("last",   t("col.last"),        85),
            ("spread", t("col.spread"),     100),
            ("status", t("col.status"),     130),
            ("link",   t("col.link"),       110),
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
            elif col in ("num", "link"):
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
        btn_specs = [
            ("btn.add_by_url",      self._add_by_url),
            ("btn.edit_target",     self._edit_target),
            (move_key,              self._move_to_other_list),
            ("btn.check_now",       self._check_now),
            ("btn.remove",          self._remove_card),
        ]
        btn_move = None
        btn_check = None
        for key, cmd in btn_specs:
            btn = ttk.Button(row1, text=t(key), command=cmd)
            btn.pack(side=LEFT, padx=2)
            if key == move_key:
                btn_move = btn
            elif key == "btn.check_now":
                btn_check = btn

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
        self.list_action_buttons[kind] = {
            "completed": btn_completed,
            "not": btn_not,
            "move": btn_move,
            "check": btn_check,
            "import": btn_import,
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
        return dirty

    def _refresh_card_list(self, kind: str) -> None:
        from steam import pretty_name

        tree = self.list_trees.get(kind)
        if tree is None:
            return
        path = self._kind_path(kind)
        tree.delete(*tree.get_children())
        items = load_json(path, []) or []
        if self._ensure_ids(items):
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

            # Pick row tag: raw status overrides alternating colours.
            # We branch on the *raw* value, not the localised one, so that
            # row colours stay stable across language switches.
            zebra = "even" if row_index % 2 == 0 else "odd"
            if raw_status == "alerted":
                row_tag = "alerted"
            elif raw_status == "rate_limited":
                row_tag = "rate_limited"
            elif raw_status == "error":
                row_tag = "error"
            else:
                row_tag = zebra

            tree.insert(
                "", END,
                # iid = record's uuid (uniquely identifies even duplicate
                # mhn entries in salelist).
                iid=item["id"],
                values=(
                    row_index + 1,
                    display_name, game_name,
                    target_str, last, spread_str, status,
                    t("col.link.open"),
                ),
                tags=(row_tag,),
            )
            row_index += 1
        # Refresh wiped the tags — restore the "selected" marker so the
        # current selection stays visible.
        self._mark_selected_rows(tree)
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
        """Top up display_name/game_name/image_url on a list of card records.

        First tries to copy from `other` (a sibling list with the same
        market_hash_name — used to share metadata between watchlist.json
        and purchases.json without an extra HTTP call). Falls back to
        `fetch_fn(appid, mhn)` for anything still missing.

        Returns True if at least one record was modified.
        """
        dirty = False
        for item in records:
            mhn = item.get("market_hash_name", "")
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
                item["game_name"] = meta.get("game_name") or "—"
            if not item.get("image_url") and meta.get("image_url"):
                item["image_url"] = meta["image_url"]
            dirty = True
        return dirty

    # ------------------------------------------------------------------
    # Card-list Treeview click handling (link column)
    # ------------------------------------------------------------------

    # Identifier of the "link" column inside the card-list Treeview, as
    # returned by tree.identify_column(). Columns: (num, name, game, target,
    # last, spread, status, link) → link sits at position 8.
    _LINK_COL_ID = "#8"

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
                           fetch_card_metadata)

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

        items.append({
            "id": str(uuid.uuid4()),
            "name": market_hash_name,
            "appid": appid,
            "market_hash_name": market_hash_name,
            "display_name": meta["display_name"],
            "game_name": meta["game_name"],
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
        """Placeholder for the Phase-3 «Import from Steam» feature.

        When fully wired, this will fetch the user's active Steam Market
        listings (sell tab) or buy orders (buy tab) and offer to sync them
        with the local watchlist/salelist — see DESIGN.md «Phase 3» for
        the full conflict-resolution flow (new → add, same price → skip,
        different price → ask). For now the button just announces that
        the feature is in development, so the UI slot stays visible and
        the keyboard / mouse focus path is exercised.
        """
        messagebox.showinfo(t("dlg.import.title"),
                            t("dlg.import.in_development"), parent=self)

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
        f = ttk.Frame(self.tab_settings, padding=12)
        f.pack(fill=BOTH, expand=YES)

        def row(label_key, row_idx):
            ttk.Label(f, text=t(label_key), width=24, anchor=E
                      ).grid(row=row_idx, column=0, sticky=E, pady=4, padx=(0, 8))

        cfg = self.config_data
        tg = cfg.get("telegram", {})
        mkt = cfg.get("market", {})
        sched = cfg.get("schedule", {})
        ui = cfg.get("ui", {})
        spam = cfg.get("antispam", {})

        # Token + Chat ID are readonly by default with a small ✏ button
        # next to them — protects against accidental edits (one missing
        # character in a 40-char bot token is enough to break TG sends).
        # The button toggles to ✓ while editing and back to ✏ on confirm.
        row("lbl.bot_token", 0)
        self.var_token = tk.StringVar(value=tg.get("bot_token", ""))
        token_holder = ttk.Frame(f)
        token_holder.grid(row=0, column=1, sticky=W)
        self.entry_token = ttk.Entry(
            token_holder, textvariable=self.var_token, width=50, state="readonly"
        )
        self.entry_token.pack(side=LEFT)
        self.btn_edit_token = ttk.Button(
            token_holder, text="✏", width=3, bootstyle="link",
        )
        self.btn_edit_token.configure(
            command=lambda: self._toggle_edit(self.entry_token, self.btn_edit_token)
        )
        self.btn_edit_token.pack(side=LEFT, padx=(4, 0))

        row("lbl.chat_id", 1)
        self.var_chat_id = tk.StringVar(value=str(tg.get("chat_id", "")))
        chat_holder = ttk.Frame(f)
        chat_holder.grid(row=1, column=1, sticky=W)
        self.entry_chat = ttk.Entry(
            chat_holder, textvariable=self.var_chat_id, width=30, state="readonly"
        )
        self.entry_chat.pack(side=LEFT)
        self.btn_edit_chat = ttk.Button(
            chat_holder, text="✏", width=3, bootstyle="link",
        )
        self.btn_edit_chat.configure(
            command=lambda: self._toggle_edit(self.entry_chat, self.btn_edit_chat)
        )
        self.btn_edit_chat.pack(side=LEFT, padx=(4, 0))

        ttk.Button(f, text=t("btn.test_message"),
                   command=self._test_telegram, bootstyle="info"
                   ).grid(row=1, column=2, padx=8)

        row("lbl.currency", 2)
        self.var_currency = tk.IntVar(value=mkt.get("currency", 18))
        currency_map = {"UAH (18)": 18, "USD (1)": 1, "EUR (3)": 3, "RUB (5)": 5}
        cb_currency = ttk.Combobox(f, values=list(currency_map.keys()), state="readonly", width=12)
        reverse_map = {v: k for k, v in currency_map.items()}
        cb_currency.set(reverse_map.get(self.var_currency.get(), "UAH (18)"))
        cb_currency.grid(row=2, column=1, sticky=W)
        self._currency_map = currency_map
        self._cb_currency = cb_currency

        row("lbl.country", 3)
        self.var_country = tk.StringVar(value=mkt.get("country", "UA"))
        country_holder = ttk.Frame(f)
        country_holder.grid(row=3, column=1, sticky=W)
        self.entry_country = ttk.Entry(
            country_holder, textvariable=self.var_country, width=8, state="readonly"
        )
        self.entry_country.pack(side=LEFT)
        self.btn_edit_country = ttk.Button(
            country_holder, text="✏", width=3, bootstyle="link",
        )
        self.btn_edit_country.configure(
            command=lambda: self._toggle_edit(self.entry_country, self.btn_edit_country)
        )
        self.btn_edit_country.pack(side=LEFT, padx=(4, 0))

        row("lbl.interval", 4)
        self.var_interval = tk.IntVar(value=sched.get("interval_minutes", 5))
        ttk.Spinbox(f, from_=1, to=60, textvariable=self.var_interval, width=8).grid(row=4, column=1, sticky=W)

        # Pause between price-fetch requests inside one batch. Put just
        # under "Interval" — both knobs concern Steam-polling cadence.
        # Picked up on the fly: _save_settings refreshes self.config_data,
        # and `_check_now` re-reads market.poll_delay_sec on every click.
        # watch.py reads it fresh from disk on each scheduled run anyway.
        row("lbl.poll_delay", 5)
        self.var_poll_delay = tk.DoubleVar(value=mkt.get("poll_delay_sec", 1.5))
        ttk.Spinbox(
            f, from_=0.5, to=10.0, increment=0.1, format="%.1f",
            textvariable=self.var_poll_delay, width=8,
        ).grid(row=5, column=1, sticky=W)

        row("lbl.theme", 6)
        # Built-in ttkbootstrap themes — all 18 of them, in dark-then-light
        # order so users see the more contrast-y options first. Custom
        # themes from themes/*.json are appended at the end as requested.
        builtin_themes = [
            "cyborg", "darkly", "solar", "superhero", "vapor",
            "cerculean", "cosmo", "flatly", "journal", "litera",
            "lumen", "minty", "morph", "pulse", "sandstone",
            "simplex", "united", "yeti",
        ]
        custom_codes = [c["code"] for c in self._custom_themes]
        themes = builtin_themes + custom_codes
        self.var_theme = tk.StringVar(value=ui.get("theme", "superhero"))
        cb_theme = ttk.Combobox(f, values=themes, textvariable=self.var_theme, state="readonly", width=16)
        cb_theme.grid(row=6, column=1, sticky=W)
        cb_theme.bind("<<ComboboxSelected>>", self._on_theme_change)

        # Language picker — values are display names from each lang/*.json
        # `_meta.name`, but we map back to ISO codes when saving.
        row("lbl.language", 7)
        self._lang_options = i18n.available_languages()
        self._lang_name_to_code = {opt["name"]: opt["code"] for opt in self._lang_options}
        current_code = i18n.get_language()
        current_name = next(
            (opt["name"] for opt in self._lang_options if opt["code"] == current_code),
            current_code,
        )
        self.var_language = tk.StringVar(value=current_name)
        cb_lang = ttk.Combobox(
            f, values=[opt["name"] for opt in self._lang_options],
            textvariable=self.var_language, state="readonly", width=16,
        )
        cb_lang.grid(row=7, column=1, sticky=W)
        cb_lang.bind("<<ComboboxSelected>>", self._on_language_change)

        row("lbl.antispam_hours", 8)
        self.var_remind_hours = tk.IntVar(value=spam.get("remind_after_hours", 24))
        ttk.Spinbox(f, from_=1, to=168, textvariable=self.var_remind_hours, width=8).grid(row=8, column=1, sticky=W)

        row("lbl.repeat_if_lower", 9)
        self.var_repeat_lower = tk.BooleanVar(value=spam.get("repeat_if_lower", True))
        ttk.Checkbutton(f, variable=self.var_repeat_lower).grid(row=9, column=1, sticky=W)

        row("lbl.template", 10)
        template_holder = ttk.Frame(f)
        template_holder.grid(row=10, column=1, sticky=W, pady=4)
        # tk.Text isn't a ttk widget so it doesn't pick up the style-level
        # selection colours we configured for ttk.Entry. Apply explicitly so
        # selected text reads against the input background.
        self.txt_template = tk.Text(
            template_holder, width=55, height=5, wrap=tk.WORD,
            selectbackground=self._text_sel_bg,
            selectforeground=self._text_sel_fg,
        )
        # Order: explicit user override → language default. Empty/blank
        # override falls back to the language default too.
        template_value = (cfg.get("message_template") or "").strip() or t("tg.message.default")
        # Insert BEFORE switching to disabled — Text rejects insert() while
        # disabled. After locking, the user re-enables via the ✏ button.
        self.txt_template.insert("1.0", template_value)
        self.txt_template.configure(state="disabled")
        self.txt_template.pack(side=LEFT)
        self.btn_edit_template = ttk.Button(
            template_holder, text="✏", width=3, bootstyle="link",
        )
        self.btn_edit_template.configure(
            command=lambda: self._toggle_edit(
                self.txt_template, self.btn_edit_template, lock_state="disabled"
            )
        )
        # anchor=N keeps the button glued to the top edge of a multi-line
        # Text widget — looks much tidier than floating in the middle.
        self.btn_edit_template.pack(side=LEFT, padx=(4, 0), anchor=N)
        ttk.Label(f, text=t("lbl.template_vars"),
                  foreground="gray").grid(row=11, column=1, sticky=W)

        row("lbl.steam_login", 12)
        ttk.Button(f, text=t("btn.in_development"), state=DISABLED).grid(row=12, column=1, sticky=W)

        ttk.Button(f, text=t("btn.save"),
                   command=self._save_settings, bootstyle="success"
                   ).grid(row=13, column=1, sticky=W, pady=(16, 0))

    def _toggle_edit(self, widget, button: ttk.Button, lock_state: str = "readonly") -> None:
        """Flip a locked input widget between edit and locked modes.

        Works for both ttk.Entry (lock_state="readonly") and tk.Text
        (lock_state="disabled" — Text has no "readonly" state).

        Visual contract:
          * locked   → state=lock_state, button "✏" in neutral link colour
          * editing  → state=normal,     button "✓" in success (green)
        Pressing the button again locks the field; the underlying value
        (StringVar or Text buffer) keeps whatever the user typed.
        """
        if str(widget.cget("state")) == lock_state:
            widget.configure(state="normal")
            widget.focus_set()
            if hasattr(widget, "icursor"):
                widget.icursor("end")
            button.configure(text="✓", bootstyle="success")
        else:
            widget.configure(state=lock_state)
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
        save_json(CONFIG_PATH, cfg)
        self.config_data = cfg
        self._set_status(t("status.settings_saved"))

    # ---- Scheduler -------------------------------------------------------

    def _build_scheduler_tab(self):
        f = ttk.Frame(self.tab_scheduler, padding=16)
        f.pack(fill=BOTH, expand=YES)

        self.lbl_task_status = ttk.Label(f, text="…", font=("", 12))
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

        # Single bottom strip: action buttons on the left, totals panel on
        # the right. Keeps the bottom area the same height as before the
        # stats panel was added (one row of buttons high).
        bottom = ttk.Frame(self.tab_history)
        bottom.pack(side=BOTTOM, fill=X, padx=8, pady=(0, 8))
        btn_f = ttk.Frame(bottom)
        btn_f.pack(side=LEFT)

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
        # Same selection-tag trick as the watchlist tree so rows you click
        # actually look selected.
        self.hist_tree.bind("<<TreeviewSelect>>",
                            lambda _e: self._mark_selected_rows(self.hist_tree))
        # Click on the link column opens the market listing in a browser.
        self.hist_tree.bind("<Button-1>", self._on_hist_tree_click)
        self.hist_tree.bind("<Motion>", self._on_hist_tree_motion)
        # Same Ctrl+A toggle as the card-list trees.
        self.hist_tree.bind("<Control-KeyPress>", self._on_tree_ctrl_a)
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
        for key, cmd in [
            ("btn.history_open",   self._hist_open_browser),
            ("btn.history_export", self._hist_export_csv),
            ("btn.history_edit",   self._hist_edit),
            ("btn.history_readd",  self._hist_readd),
            ("btn.history_delete", self._hist_delete),
        ]:
            ttk.Button(btn_f, text=t(key), command=cmd).pack(side=LEFT, padx=2)

        # Totals panel — purchases / sales / spent — laid out inline in
        # one row so the whole bottom strip stays the height of a single
        # button. Lives on the right of the bottom strip; the action
        # buttons cling to the left. Value labels auto-size to their text
        # (no fixed `width=`) so there's no dead space between the colon
        # and the number. A subtle "│" separator splits the groups.
        stats = ttk.Frame(bottom)
        stats.pack(side=RIGHT)
        cells = [
            ("hist.total_buy",  "lbl_total_buy"),
            ("hist.total_sell", "lbl_total_sell"),
            ("hist.spent",      "lbl_spent"),
        ]
        for idx, (key, attr) in enumerate(cells):
            if idx > 0:
                sep = ttk.Label(stats, text="│")
                sep.configure(foreground="#666666")
                sep.pack(side=LEFT, padx=10)
            ttk.Label(stats, text=t(key)).pack(side=LEFT, padx=(0, 4))
            value_label = ttk.Label(stats, text="0.00")
            value_label.pack(side=LEFT)
            setattr(self, attr, value_label)

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

    def _hist_open_browser(self):
        from steam import market_url

        selected = self._require_hist_selection()
        if not selected:
            return
        # Dedup by (appid, mhn) — picking 5 rows for the same card
        # shouldn't open 5 identical tabs.
        unique_urls: list[str] = []
        seen: set[tuple] = set()
        for p in selected:
            key = (p.get("appid"), p.get("market_hash_name") or p.get("name"))
            if key in seen:
                continue
            seen.add(key)
            unique_urls.append(market_url(key[0], key[1]))
        if len(unique_urls) > 3:
            if not messagebox.askyesno(
                t("dlg.open_many.title"),
                t("dlg.open_many.body", count=len(unique_urls)),
                parent=self,
            ):
                return
        for url in unique_urls:
            webbrowser.open(url)

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
        from tkinter.filedialog import asksaveasfilename
        path = asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        purchases = load_json(PURCHASES_PATH, [])
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "name", "appid", "price", "target"])
            writer.writeheader()
            writer.writerows(purchases)
        messagebox.showinfo(t("dlg.export.title"), t("dlg.export.saved", path=path))

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

        Tab order: 0=Придбання, 1=Продаж, 2=Історія, 3=Планувальник,
        4=Журнал, 5=Налаштування.

        watch.py runs out-of-process (Task Scheduler / Run now), so the
        in-memory state in the GUI goes stale between user actions. The
        cheapest way to stay in sync is to re-read the relevant file when
        the user looks at the tab.
        """
        try:
            tab = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return
        if tab == 0:          # Придбання
            self._refresh_card_list("buy")
        elif tab == 1:        # Продаж
            self._refresh_card_list("sell")
        elif tab == 2:        # Історія
            self._refresh_history()
        elif tab == 4:        # Журнал
            self._refresh_log()

    def _start_log_autoupdate(self):
        def _tick():
            tab = self.notebook.index(self.notebook.select())
            if tab == 4:  # Журнал
                self._refresh_log()
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
        self.statusbar.configure(text="  " + text)


if __name__ == "__main__":
    app = App()
    app.mainloop()