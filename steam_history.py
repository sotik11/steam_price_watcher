"""Import of the user's Steam transaction history (market + store).

Two authenticated sources, merged into one local file (`steam_history.json`):

  * market — steamcommunity.com/market/myhistory: every card / item bought
    or sold on the Community Market, with exact time, price and metadata.
  * store  — store.steampowered.com/account/history: game purchases and
    refunds, plus the wallet balance after each wallet operation. Market
    operations show up there too, but collapsed ("5 Market Transactions")
    with no item names, so we skip those rows and take cards from `market`.

File shape:
    {
        "market": [row, ...],      # newest first
        "store":  [row, ...],      # newest first
        "wallet": {"balance": 834.24, "balance_raw": "834,24₴",
                   "updated": "2026-10-01T13:40:00"},
    }

Every row carries a stable `id` from Steam, so re-importing never
duplicates: `merge_rows` keeps one row per id.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import requests

import steam
from steam import SteamSessionExpired

log = logging.getLogger(__name__)

HISTORY_FILENAME = "steam_history.json"

_MYHISTORY_URL = "https://steamcommunity.com/market/myhistory/render/"
_MYHISTORY_PAGE = 500          # Steam's cap per request
_MYHISTORY_MAX_PAGES = 40      # 20 000 operations — safety stop, not a limit
_PAGE_DELAY_SEC = 2.0

# event_type values in myhistory: 1 = listing created, 2 = listing
# cancelled, 3 = the user's listing was sold, 4 = the user bought.
_EVENT_SOLD = 3
_EVENT_BOUGHT = 4

_ACCOUNT_HISTORY_URL = "https://store.steampowered.com/account/history/"
_ACCOUNT_HISTORY_MORE_URL = (
    "https://store.steampowered.com/account/AjaxLoadMoreHistory/")
_ACCOUNT_HISTORY_MAX_PAGES = 40

_ECONOMY_IMAGE_URL = "https://community.steamstatic.com/economy/image/"

_TIMEOUT = (5, 60)             # history pages are ~1.5 MB each

_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def empty_history() -> dict:
    return {"market": [], "store": [], "wallet": {}}


def load_history(path: Path) -> dict:
    """Read the history file; missing / broken file → empty history."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_history()
    if not isinstance(data, dict):
        return empty_history()
    result = empty_history()
    for section in ("market", "store"):
        if isinstance(data.get(section), list):
            result[section] = data[section]
    if isinstance(data.get("wallet"), dict):
        result["wallet"] = data["wallet"]
    return result


def save_history(path: Path, history: dict) -> None:
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(history, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    tmp_path.replace(path)


def merge_rows(existing: list[dict], fetched: list[dict]) -> tuple[list[dict], int]:
    """Merge `fetched` into `existing` by row id. Returns (rows, added_count).

    Fetched rows win on id collision (Steam may fill in fields later, e.g.
    a refund flag). Result is sorted newest first; `seq` breaks ties for
    store rows, which only have a date.
    """
    by_id = {row["id"]: row for row in existing}
    added = sum(1 for row in fetched if row["id"] not in by_id)
    for row in fetched:
        by_id[row["id"]] = row
    rows = sorted(by_id.values(),
                  key=lambda row: (row.get("timestamp", ""), -row.get("seq", 0)),
                  reverse=True)
    return rows, added


# ---------------------------------------------------------------------------
# Market history
# ---------------------------------------------------------------------------

def _market_row(event: dict, purchase: dict, asset: dict) -> dict:
    is_sale = event["event_type"] == _EVENT_SOLD
    if is_sale:
        # What the seller actually got, after Steam + publisher fees.
        amount_minor = purchase.get("received_amount") or 0
    else:
        amount_minor = ((purchase.get("paid_amount") or 0)
                        + (purchase.get("paid_fee") or 0))
    market_hash_name = asset.get("market_hash_name") or ""
    display_name = html.unescape(
        asset.get("name") or asset.get("market_name")
        or steam.clean_card_name(market_hash_name))
    asset_type = html.unescape(asset.get("type") or "")
    game_name, item_type = steam.split_game_and_type(asset_type)
    if not item_type:
        # Booster packs: type is just "Booster Pack", the game lives in
        # the item name ("<game> Booster Pack").
        item_type = asset_type
        game_name = display_name.removesuffix(" " + asset_type).strip()
    icon = asset.get("icon_url") or ""
    return {
        "id":               f"{event['listingid']}_{event['purchaseid']}",
        "source":           "market",
        "operation":        "sell" if is_sale else "buy",
        "timestamp":        datetime.fromtimestamp(
                                event["time_event"]).isoformat(timespec="seconds"),
        "appid":            str(asset.get("appid") or ""),
        "market_hash_name": market_hash_name,
        "display_name":     display_name,
        "game_name":        game_name,
        "item_type":        item_type,
        "image_url":        _ECONOMY_IMAGE_URL + icon if icon else "",
        "price":            amount_minor / 100,
    }


def fetch_market_history(cookies: dict | None,
                         known_ids: frozenset[str] | set[str] = frozenset(),
                         progress=None) -> list[dict]:
    """Fetch market buy/sell operations, newest first.

    `known_ids` makes the import incremental: paging stops at the first
    page that contains an already-stored operation. Pass an empty set for
    a full import.

    `progress(done, total)` is called after each page (GUI progress line).

    Raises SteamSessionExpired when cookies are missing / stale, and
    steam.RateLimitedError on HTTP 429.
    """
    community = (cookies or {}).get("steamcommunity.com") or {}
    if "steamLoginSecure" not in community:
        raise SteamSessionExpired("no steamcommunity.com session cookies")

    rows: list[dict] = []
    start = 0
    for page_index in range(_MYHISTORY_MAX_PAGES):
        if page_index:
            time.sleep(_PAGE_DELAY_SEC)
        resp = requests.get(
            _MYHISTORY_URL,
            params={"query": "", "start": start, "count": _MYHISTORY_PAGE,
                    "norender": 1, "l": "english"},
            cookies=community,
            headers={"User-Agent": steam._UA,
                     "Accept": "application/json, */*",
                     "Referer": "https://steamcommunity.com/market/"},
            timeout=_TIMEOUT,
            allow_redirects=False,
        )
        if resp.status_code == 429:
            raise steam.RateLimitedError("myhistory")
        if resp.status_code in (400, 401, 403):
            raise SteamSessionExpired(
                f"myhistory returned HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not data.get("success"):
            raise ValueError("myhistory: unexpected response")

        total = int(data.get("total_count") or 0)
        events = data.get("events") or []
        if not events:
            # A logged-out request also answers success=true with zero
            # rows. If we already hold history, that can only be a dead
            # session; with no local history it may be a brand-new account.
            if start == 0 and known_ids:
                raise SteamSessionExpired("myhistory returned no events")
            break

        purchases = data.get("purchases") or {}
        listings = data.get("listings") or {}
        assets = data.get("assets") or {}
        reached_known = False
        for event in events:
            if event.get("event_type") not in (_EVENT_SOLD, _EVENT_BOUGHT):
                continue
            row_id = f"{event['listingid']}_{event['purchaseid']}"
            if row_id in known_ids:
                reached_known = True
                continue
            purchase = purchases.get(row_id)
            listing_asset = (listings.get(event["listingid"]) or {}).get("asset") or {}
            asset = (assets.get(str(listing_asset.get("appid")), {})
                     .get(str(listing_asset.get("contextid")), {})
                     .get(str(listing_asset.get("id"))))
            if not purchase or not asset:
                log.warning("myhistory: no purchase/asset for %s — skipped", row_id)
                continue
            if purchase.get("failed") or purchase.get("needs_rollback"):
                continue
            rows.append(_market_row(event, purchase, asset))

        start += len(events)
        if progress:
            progress(min(start, total), total)
        if reached_known or start >= total:
            break

    log.info("market history: fetched %d operations", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Store (account) history
# ---------------------------------------------------------------------------

_ROW_RE = re.compile(
    r'(<tr[^>]*class="wallet_table_row[^"]*"[^>]*>)(.*?)</tr>', re.DOTALL)
_TRANSID_RE = re.compile(r"transid=(\d+)")
_CURSOR_RE = re.compile(r"g_historyCursor\s*=\s*(\{.*?\}|null)\s*;")
_ITEM_DIV_RE = re.compile(
    r'<div style="clear: both">(.*?)</div>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"(\d{1,2}) ([A-Za-z]{3}), (\d{4})")

# First line of the "Type" cell → our operation. Anything not listed
# (Market Transaction(s), Conversion, …) is skipped.
_STORE_OPERATIONS = {
    "Purchase":         "buy",
    "In-Game Purchase": "buy",
    "Gift Purchase":    "buy",
    "Refund":           "refund",
}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _cell(row_html: str, css_class: str) -> str:
    match = re.search(
        r'<td[^>]*class="' + css_class + r'[^"]*"[^>]*>(.*?)</td>',
        row_html, re.DOTALL)
    return match.group(1) if match else ""


def _signed_price(text: str) -> float | None:
    value = steam.parse_price(text)
    if value is None:
        return None
    return -value if text.lstrip().startswith("-") else value


def _parse_store_rows(page_html: str, seq_start: int) -> tuple[list[dict], str]:
    """Parse one chunk of account-history HTML.

    Returns (rows, newest_wallet_balance_raw). The balance comes from the
    first row in the chunk that has one — market rows included, since they
    are wallet operations too.
    """
    rows: list[dict] = []
    balance_raw = ""
    for seq, (row_tag, row_html) in enumerate(_ROW_RE.findall(page_html),
                                              start=seq_start):
        if not balance_raw:
            balance_raw = _text(_cell(row_html, "wht_wallet_balance"))

        type_cell = _cell(row_html, "wht_type")
        type_parts = [_text(part) for part in
                      re.findall(r"<div[^>]*>(.*?)</div>", type_cell, re.DOTALL)]
        type_name = type_parts[0] if type_parts else _text(type_cell)
        operation = _STORE_OPERATIONS.get(type_name)
        if operation is None:
            continue

        date_match = _DATE_RE.search(_text(_cell(row_html, "wht_date")))
        if not date_match or date_match.group(2) not in _MONTHS:
            log.warning("account history: unparsed date in row %d", seq)
            continue
        day, month, year = (int(date_match.group(1)),
                            _MONTHS[date_match.group(2)],
                            int(date_match.group(3)))

        items_cell = _cell(row_html, "wht_items")
        names = [_text(item) for item in _ITEM_DIV_RE.findall(items_cell)]
        names = [name for name in names if name] or [_text(items_cell)]

        total_raw = _text(_cell(row_html, "wht_total"))
        transid_match = _TRANSID_RE.search(row_tag)
        row_id = (transid_match.group(1) if transid_match
                  else f"{year:04d}{month:02d}{day:02d}|{'|'.join(names)}|{total_raw}")
        rows.append({
            # A refund reuses its purchase's transid, and a split
            # (wallet + card) refund is two rows on one transid — so the
            # operation and amount are part of the identity.
            "id":            f"store_{operation}_{row_id}_{total_raw}",
            "source":        "store",
            "operation":     operation,
            "timestamp":     f"{year:04d}-{month:02d}-{day:02d}T00:00:00",
            "seq":           seq,
            "names":         names,
            "display_name":  ", ".join(names),
            "payment":       type_parts[1] if len(type_parts) > 1 else "",
            "price":         steam.parse_price(total_raw) or 0.0,
            "wallet_change": _signed_price(
                                 _text(_cell(row_html, "wht_wallet_change"))),
        })
    return rows, balance_raw


def fetch_store_history(cookies: dict | None) -> dict:
    """Fetch game purchases / refunds and the current wallet balance.

    Returns {"rows": [...], "balance": float | None, "balance_raw": str}.
    Always a full fetch — the whole account history is two requests.

    Raises SteamSessionExpired when the store session is missing / stale.
    """
    store = (cookies or {}).get("store.steampowered.com") or {}
    if "steamLoginSecure" not in store:
        raise SteamSessionExpired("no store.steampowered.com session cookies")

    session = requests.Session()
    session.headers.update({"User-Agent": steam._UA})
    for name, value in store.items():
        session.cookies.set(name, value, domain="store.steampowered.com")

    # English page: type names and dates are parsed as text. The first
    # hit answers 302 to itself (sets country/language cookies), hence
    # redirects are followed — a dead session lands on /login instead.
    resp = session.get(_ACCOUNT_HISTORY_URL, params={"l": "english"},
                       timeout=_TIMEOUT)
    if "/login" in resp.url:
        raise SteamSessionExpired("account history redirected to login")
    resp.raise_for_status()

    rows, balance_raw = _parse_store_rows(resp.text, seq_start=0)
    row_count = len(_ROW_RE.findall(resp.text))
    cursor_match = _CURSOR_RE.search(resp.text)
    cursor = json.loads(cursor_match.group(1)) if cursor_match else None

    for _ in range(_ACCOUNT_HISTORY_MAX_PAGES):
        if not cursor:
            break
        time.sleep(1.0)
        payload = {f"cursor[{key}]": value for key, value in cursor.items()}
        payload["sessionid"] = store.get("sessionid", "")
        more = session.post(
            _ACCOUNT_HISTORY_MORE_URL, params={"l": "english"}, data=payload,
            headers={"Referer": _ACCOUNT_HISTORY_URL,
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=_TIMEOUT)
        more.raise_for_status()
        data = more.json()
        chunk = data.get("html") or ""
        chunk_rows, chunk_balance = _parse_store_rows(chunk, seq_start=row_count)
        rows += chunk_rows
        balance_raw = balance_raw or chunk_balance
        row_count += len(_ROW_RE.findall(chunk))
        cursor = data.get("cursor")

    log.info("store history: %d purchases/refunds out of %d rows",
             len(rows), row_count)
    return {"rows": rows,
            "balance": steam.parse_price(balance_raw),
            "balance_raw": balance_raw}


# ---------------------------------------------------------------------------
# One-call import
# ---------------------------------------------------------------------------

def import_history(path: Path, cookies: dict | None, progress=None) -> dict:
    """Fetch both sources, merge into the file at `path`, save.

    Returns {"market_added": n, "store_added": n, "history": dict,
             "store_error": str | None}.

    The market part is mandatory (its errors propagate). The store part is
    best-effort: its own session cookie expires independently, and losing
    it must not throw away a successful market import.
    """
    history = load_history(path)
    known_ids = {row["id"] for row in history["market"]}
    fetched = fetch_market_history(cookies, known_ids, progress)
    history["market"], market_added = merge_rows(history["market"], fetched)

    store_added = 0
    store_error = None
    try:
        store = fetch_store_history(cookies)
    except (SteamSessionExpired, requests.RequestException, ValueError) as exc:
        store_error = str(exc)
        log.warning("store history skipped: %s", exc)
    else:
        history["store"], store_added = merge_rows(history["store"], store["rows"])
        if store["balance"] is not None:
            history["wallet"] = {
                "balance": store["balance"],
                "balance_raw": store["balance_raw"],
                "updated": datetime.now().isoformat(timespec="seconds"),
            }

    save_history(path, history)
    return {"market_added": market_added, "store_added": store_added,
            "history": history, "store_error": store_error}
