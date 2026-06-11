"""Steam Community Market API helpers."""
import logging
import re
import time
import urllib.parse
import requests

from i18n import t

log = logging.getLogger("steam")


class SteamSessionExpired(Exception):
    """Raised when an authenticated Steam endpoint signals stale cookies.

    Distinct from a plain RequestException / generic HTTP failure because
    callers (GUI import flow) want to:
      * surface a "log in again" message instead of "no data";
      * trigger the session-expired toast + badge.
    HTTP 401/403 are obvious; community endpoints like /market/mylistings
    also return 400 with a JSON envelope when the session is dead — we
    treat that the same.
    """

PRICE_OVERVIEW_URL = "https://steamcommunity.com/market/priceoverview/"
ORDERBOOK_URL = "https://steamcommunity.com/market/orderbook"
LISTINGS_URL = "https://steamcommunity.com/market/listings/{appid}/{name}"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

# Currency symbol per Steam currency code — for formatting orderbook
# prices into the human-readable string we store in `last_seen`.
# Steam's priceoverview used to return this string baked in; orderbook
# returns only the integer (in "kopecks" — 1/100 of base unit).
_CURRENCY_SYMBOLS = {
    1: "$",     # USD
    2: "£",     # GBP
    3: "€",     # EUR
    5: "₽",     # RUB
    18: "₴",    # UAH
}

# Steam's 2025-era rate limiting on priceoverview / itemordershistogram is
# fingerprint-based: a request that doesn't *look* like a real Chrome tab
# (right headers, real Referer pointing to the actual listing page, a
# persistent session cookie) gets banned after 10-100 requests for ~2h.
# A request that mirrors a real browser tab byte-for-byte stays under the
# radar. The headers below are what Chrome 120 sends on the listing page.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Headers shared by every steamcommunity.com request — exactly what Chrome
# sends. The per-request `Referer` is added in `get_price` (must point to
# the specific listing being queried — Steam checks this now).
_DEFAULT_HEADERS = {
    "User-Agent": _UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    # No "br" — requests doesn't bundle brotli, and if Steam picked it
    # we'd get binary garbage we can't decode. gzip/deflate is plenty.
    "Accept-Encoding": "gzip, deflate",
    "Origin": "https://steamcommunity.com",
    "Host": "steamcommunity.com",
    "Connection": "keep-alive",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# (connect_timeout, read_timeout) — fail fast if Steam is unresponsive
# instead of hanging for the full 10 s on connect and another 10 s on read.
_TIMEOUT = (5, 8)

# Persistent HTTP session so connection reuse keeps the per-request cost
# down — orderbook responses are ~250 B, the TLS handshake is most of the
# wall-clock budget if we open a fresh connection every call.
_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    """Lazily build a requests.Session with browser-like defaults.

    Single shared session means TCP/TLS connection reuse across batch
    requests in one process (watch.py) and across "Оновити зараз" clicks
    in the GUI. The market/orderbook endpoint doesn't require any session
    cookies, so we no longer warm up against the market home page — used
    to be needed for priceoverview's bot detection.
    """
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update(_DEFAULT_HEADERS)
    return _SESSION


# Maps Steam currency codes to regex patterns for stripping non-numeric chars
_STRIP_CURRENCY = re.compile(r"[^\d,\.]")


class RateLimitedError(Exception):
    """Steam returned HTTP 429 — we're being rate-limited.

    Carries `retry_after` (seconds) from the response header when available;
    None when Steam didn't send one. Callers can use this to back off
    intelligently (skip the whole batch, store a cooldown timestamp, etc.)
    instead of treating it like any other HTTP failure.
    """

    def __init__(self, market_hash_name: str = "?", retry_after: int | None = None):
        self.market_hash_name = market_hash_name
        self.retry_after = retry_after
        super().__init__(
            f"Steam rate-limited (429) on {market_hash_name}"
            + (f"; retry-after={retry_after}s" if retry_after else "")
        )


class _BatchResults(list):
    """List with an extra attribute for rate-limit signalling.

    Builtin `list` has no __dict__, so `setattr(plain_list, ...)` raises
    AttributeError. Subclassing gives us the attribute slot without
    changing the return type for the consumer (still iterable, indexable,
    `len()`-able — the rest of watch.py treats it as a list).
    """
    rate_limited_retry_after: int | None = None


def parse_price(price_str: str) -> float | None:
    """Convert a localized price string like '5,49₴' or '$1.23' to float."""
    if not price_str:
        return None
    cleaned = _STRIP_CURRENCY.sub("", price_str).strip(".,")
    # Steam returns prices with comma as decimal separator in some locales
    if "," in cleaned and "." in cleaned:
        # e.g. "1.234,56" → remove thousand-sep dot, use comma as decimal
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _format_price(amount_int: int, currency: int) -> str:
    """Format integer "kopecks" from orderbook into a display string.

    Orderbook returns prices like 500 (meaning 5.00 of the base unit).
    For UAH/USD/EUR we show "5,00₴" / "$5.00" / etc. Falls back to the
    plain number if we don't recognise the currency code.
    """
    value = amount_int / 100
    sym = _CURRENCY_SYMBOLS.get(currency)
    if sym is None:
        return f"{value:.2f}"
    # UAH uses the symbol after the number with a comma decimal separator
    # (that's what Steam's own priceoverview returned, e.g. "5,00₴").
    # For dollar-style currencies the symbol goes before.
    if currency == 18:  # UAH
        return f"{value:.2f}".replace(".", ",") + sym
    if currency in (1, 2):  # USD, GBP — symbol before, dot decimal
        return f"{sym}{value:.2f}"
    # EUR / RUB / others — symbol after with a comma decimal.
    return f"{value:.2f}".replace(".", ",") + sym


def get_price_via_orderbook(appid: str | int, market_hash_name: str,
                            currency: int = 18, country: str = "UA") -> dict:
    """Fetch lowest sell / highest buy from market/orderbook.

    This is the **primary** price source as of the 2026 Steam SPA rewrite.
    Endpoint URL:
        https://steamcommunity.com/market/orderbook?q=Load&qp=[appid,mhn]

    Returned `data`:
        amtMinSellOrder   — lowest active sell order, in 1/100 of base unit
        amtMaxBuyOrder    — highest active buy order, same units
        eCurrency         — currency code the response is denominated in
        cSellOrders       — count of active sell orders in the book
        cBuyOrders        — count of active buy orders in the book
        rgCompactSellOrders — flat [price, count, price, count, …] pairs
        rgCompactBuyOrders  — same shape

    Why this endpoint over priceoverview:
    - Tested at 300+ requests over 20 minutes with zero 429s.
    - No fingerprinting / cookies / item_nameid required.
    - Returns clean JSON (no HTML parsing).
    - Built for the same UI the user sees in the browser, so the request
      profile matches Steam's expected traffic shape.

    Returns the same dict shape as the legacy priceoverview path so
    callers don't need a branch:
        lowest_price (float|None), lowest_price_raw (str), volume (int|None), raw (dict).
    `volume` here is `cSellOrders` (number of active sell offers), not the
    24h sales volume that priceoverview gave — we trade that for not
    getting banned.
    """
    # User-facing log lines: strip the "{appid}-" prefix so we log
    # "Merrin" rather than "1774580-Merrin". The raw market_hash_name
    # still goes on the wire — Steam expects that exact form.
    short_name = clean_card_name(market_hash_name)[:40]
    # qp parameter is a JSON array stringified: [appid, "market_hash_name"]
    # We hand-build it instead of using json.dumps to keep the format
    # byte-identical to what Steam's own SPA sends (no extra spaces).
    qp = f'[{appid},"{market_hash_name}"]'
    params = {"q": "Load", "qp": qp}
    session = _get_session()
    # Referer points to the actual listing page — Steam apparently still
    # logs this, costs nothing to send.
    headers = {"Referer": market_url(appid, market_hash_name)}
    t0 = time.monotonic()
    try:
        resp = session.get(
            ORDERBOOK_URL, params=params,
            timeout=_TIMEOUT, headers=headers,
        )
    except requests.RequestException as exc:
        elapsed = time.monotonic() - t0
        log.warning(t("log.priceoverview_fail",
                      name=short_name, elapsed=elapsed, err=exc))
        raise

    elapsed = time.monotonic() - t0
    if resp.status_code == 429:
        # Shouldn't realistically happen for orderbook, but if it does
        # the rest of the machinery (RateLimitedError, cooldown) takes over.
        retry_after = None
        ra_header = resp.headers.get("Retry-After")
        if ra_header:
            try:
                retry_after = int(ra_header)
            except ValueError:
                retry_after = None
        log.error(t("log.priceoverview_rate_limited",
                    name=short_name, elapsed=elapsed,
                    retry=retry_after if retry_after is not None else "—"))
        raise RateLimitedError(market_hash_name, retry_after)
    log.info(t("log.priceoverview_ok",
               name=short_name, elapsed=elapsed,
               status=resp.status_code, bytes=len(resp.content)))
    resp.raise_for_status()

    try:
        data = resp.json()
    except ValueError as exc:
        head = resp.text[:120].replace("\n", " ")
        raise ValueError(
            t("log.priceoverview_html", name=short_name, head=head)
        ) from exc

    if not data.get("success"):
        # Surface the friendly card name (no "<appid>-" prefix) — this
        # exception bubbles into user-visible dialogs (_add_by_url warning,
        # "Оновити зараз" status), where the raw market_hash_name reads
        # like gibberish ("1774580-Merrin"). clean_card_name strips the
        # prefix in one place so every call site stays consistent.
        raise ValueError(t("log.api_failure",
                           name=clean_card_name(market_hash_name)))

    body = data.get("data", {}) or {}
    amt_sell = body.get("amtMinSellOrder")
    if isinstance(amt_sell, (int, float)):
        lowest = amt_sell / 100
        lowest_raw = _format_price(int(amt_sell), currency)
    else:
        lowest = None
        lowest_raw = ""

    # We expose cSellOrders as "volume" so existing UI / log code keeps
    # working unchanged. It's semantically different from priceoverview's
    # 24-hour volume, but it's the closest thing the orderbook payload
    # offers — and arguably more relevant for buy/sell decisions.
    c_sell = body.get("cSellOrders")
    volume = int(c_sell) if isinstance(c_sell, (int, float)) else None

    return {
        "lowest_price": lowest,
        "lowest_price_raw": lowest_raw,
        "volume": volume,
        "raw": data,
    }


def get_price(appid: str | int, market_hash_name: str,
              currency: int = 18, country: str = "UA") -> dict:
    """Façade for the active "what's the lowest price" implementation.

    Currently delegates to `get_price_via_orderbook`. The legacy
    `get_price_via_priceoverview` is kept around as a manual fallback —
    callers can switch over by name if Steam ever closes the orderbook
    endpoint.
    """
    return get_price_via_orderbook(appid, market_hash_name, currency, country)


def get_price_via_priceoverview(appid: str | int, market_hash_name: str,
                                currency: int = 18, country: str = "UA") -> dict:
    """LEGACY: fetch lowest_price, median_price, volume from priceoverview.

    Kept as an emergency fallback in case Steam ever closes the orderbook
    endpoint we now use by default. As of late 2026 priceoverview is
    aggressively rate-limited (10-100 req before a ~2h IP ban), so this
    code path should not be hit on every poll. Documented as legacy for
    that reason.

    Returns dict with keys: lowest_price (float|None), volume (int|None), raw (dict).
    Raises requests.HTTPError on non-2xx, requests.Timeout on timeout, or
    ValueError on parse failure / unexpected response shape.
    """
    params = {
        "country": country,
        "currency": currency,
        "appid": str(appid),
        "market_hash_name": market_hash_name,
    }
    short_name = clean_card_name(market_hash_name)[:40]
    session = _get_session()
    # The Referer MUST be the actual listing page for THIS card. Steam now
    # validates this — sending a random URL or no Referer gets you banned
    # faster than spamming raw requests did before.
    referer = market_url(appid, market_hash_name)
    headers = {"Referer": referer}
    t0 = time.monotonic()
    try:
        resp = session.get(
            PRICE_OVERVIEW_URL, params=params,
            timeout=_TIMEOUT, headers=headers,
        )
    except requests.RequestException as exc:
        elapsed = time.monotonic() - t0
        log.warning(t("log.priceoverview_fail",
                      name=short_name, elapsed=elapsed, err=exc))
        raise
    elapsed = time.monotonic() - t0
    # Surface 429 separately — it's not a generic "Steam is broken" — it's
    # "we hit Steam too hard, back off". Distinct exception lets the batch
    # layer bail early instead of grinding through the rest of the list.
    if resp.status_code == 429:
        retry_after = None
        ra_header = resp.headers.get("Retry-After")
        if ra_header:
            try:
                retry_after = int(ra_header)
            except ValueError:
                retry_after = None
        log.error(t("log.priceoverview_rate_limited",
                    name=short_name, elapsed=elapsed,
                    retry=retry_after if retry_after is not None else "—"))
        raise RateLimitedError(market_hash_name, retry_after)
    log.info(t("log.priceoverview_ok",
               name=short_name, elapsed=elapsed,
               status=resp.status_code, bytes=len(resp.content)))
    resp.raise_for_status()

    # Steam occasionally returns HTML (rate-limit / maintenance page) with
    # 200 OK. Catch that explicitly so we don't surface JSONDecodeError as
    # a mysterious crash.
    try:
        data = resp.json()
    except ValueError as exc:
        head = resp.text[:120].replace("\n", " ")
        raise ValueError(
            t("log.priceoverview_html", name=short_name, head=head)
        ) from exc

    if not data.get("success"):
        # Same friendly-name treatment as the orderbook path — see the
        # comment there for why we strip the "<appid>-" prefix.
        raise ValueError(t("log.api_failure",
                           name=clean_card_name(market_hash_name)))

    lowest = parse_price(data.get("lowest_price", ""))
    volume_str = data.get("volume", "").replace(",", "")
    try:
        volume = int(volume_str) if volume_str else None
    except ValueError:
        volume = None

    return {
        "lowest_price": lowest,
        "lowest_price_raw": data.get("lowest_price", ""),
        "volume": volume,
        "raw": data,
    }


def market_url(appid: str | int, market_hash_name: str) -> str:
    encoded = urllib.parse.quote(market_hash_name)
    return LISTINGS_URL.format(appid=appid, name=encoded)


def clean_card_name(market_hash_name: str) -> str:
    """Strip the leading "{appid}-" prefix from a community-item hash name.

    For Steam Community Items (appid=753) the hash is "{game_appid}-{title}".
    For game-native items it's just the title — we leave those untouched.
    """
    m = re.match(r"^\d+-(.+)$", market_hash_name)
    return m.group(1) if m else market_hash_name


def pretty_name(item: dict) -> str:
    """Return the user-facing card name from a watchlist / purchase item.

    The single source of truth for "how does a card name appear to the
    user". Anywhere we used to write
        item.get("display_name") or item.get("name", "?")
    or
        item.get("display_name") or clean_card_name(item["market_hash_name"])
    should call this instead — that way "238960-The Sceptre of God" can
    never leak into a dialog title or a Telegram caption again.

    Lookup order:
      1. `display_name` if present and not equal to the raw `market_hash_name`
         (which would mean it was never properly resolved).
      2. `clean_card_name(market_hash_name)` — strips the leading numeric
         appid prefix for Community Items.
      3. Literal "?" as a last-ditch placeholder.
    """
    mhn = item.get("market_hash_name") or item.get("name", "")
    stored = item.get("display_name")
    if stored and stored != mhn:
        return stored
    if mhn:
        return clean_card_name(mhn)
    return "?"


def extract_game_appid(market_hash_name: str) -> str | None:
    """For community items, the digits before the dash are the game's appid."""
    m = re.match(r"^(\d+)-", market_hash_name)
    return m.group(1) if m else None


# Cache: appid → game name. Resolved once per app per process; resolved
# values get persisted in watchlist.json so we don't hammer Steam on every
# GUI start.
_GAME_NAME_CACHE: dict[str, str] = {}


def fetch_game_name(game_appid: str | int) -> str:
    """Look up a game's display name by its Steam appid.

    Uses the public store.steampowered.com appdetails endpoint. Returns
    "—" on failure so the caller can store something sensible without
    branching on exceptions.
    """
    key = str(game_appid)
    if key in _GAME_NAME_CACHE:
        return _GAME_NAME_CACHE[key]
    try:
        resp = requests.get(
            APPDETAILS_URL,
            params={"appids": key, "filters": "basic", "l": "english"},
            timeout=_TIMEOUT, headers=_DEFAULT_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        entry = data.get(key, {})
        if entry.get("success") and isinstance(entry.get("data"), dict):
            name = entry["data"].get("name") or "—"
            _GAME_NAME_CACHE[key] = name
            return name
    except Exception as exc:
        log.warning("fetch_game_name(%s) failed: %s", key, exc)
    _GAME_NAME_CACHE[key] = "—"
    return "—"


def fetch_card_image_url(appid: str | int, market_hash_name: str) -> str | None:
    """Return the og:image URL from the card's Steam Market listing page.

    Used as the photo source for Telegram alerts so we get a consistently
    large preview regardless of whether Telegram's own `prefer_large_media`
    hint kicks in for the listing URL (it works for wide banner-style card
    art but falls back to a thumbnail-sized preview for square art).
    """
    url = market_url(appid, market_hash_name)
    try:
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("fetch_card_image_url(%s) failed: %s", market_hash_name, exc)
        return None
    m = re.search(
        r'<meta\s+property="og:image"\s+content="([^"]+)"',
        resp.text, re.IGNORECASE,
    )
    return m.group(1) if m else None


# Steam re-skinned the market listing page in 2024-25 — the old
# `<span class="market_listing_game_name">…</span>` block is gone; the
# game + item-type string now sits in a description-coloured span with
# an obfuscated class name. The CSS variable on the inline style is the
# only stable hook we have. The page actually puts the same span twice:
# the FIRST one is "<Game name> <item type>" (e.g. "STAR WARS Jedi:
# Survivor™ Trading Card"), the second is the "this is a commodity"
# explanation. We grab the first match.
_LISTING_GAME_NAME_RE = re.compile(
    r'<span[^>]*--text-color:var\(--color-text-body-description\)[^>]*>'
    r'([^<]+)</span>',
    re.IGNORECASE,
)
_LISTING_OG_IMAGE_RE = re.compile(
    r'<meta\s+property="og:image"\s+content="([^"]+)"',
    re.IGNORECASE,
)


def fetch_card_metadata(appid: str | int, market_hash_name: str) -> dict:
    """Resolve a card's pretty name, its game's display name, and a poster
    image URL we can send as a Telegram photo.

    Returns dict with keys: display_name, game_name, image_url.

    Strategy — one HTTP request, not two:
      We fetch the card's Steam Market listing page (the same page that
      `fetch_card_image_url` was hitting separately) and pull *both*
      `og:image` and the `market_listing_game_name` tag from the same
      HTML. That tag is the canonical "game name — item type" string
      Steam itself renders next to the card, so we get it without
      another network call.

      The Steam Store `appdetails` API (what `fetch_game_name` uses)
      has been throttling third-party callers with 403 increasingly
      often — this approach sidesteps that entirely for community
      cards, where the market listing page is always available.

    Falls back to `fetch_game_name(game_appid)` only if the listing
    page fails or doesn't carry the tag (rare — non-card community
    items, or Steam serving a redirect/error). Worst-case we still
    end up with "—" same as before.
    """
    display_name = clean_card_name(market_hash_name)

    image_url = None
    game_name = ""
    url = market_url(appid, market_hash_name)
    try:
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
        m_img = _LISTING_OG_IMAGE_RE.search(html)
        if m_img:
            image_url = m_img.group(1)
        m_game = _LISTING_GAME_NAME_RE.search(html)
        if m_game:
            game_name = m_game.group(1).strip()
    except Exception as exc:
        log.warning("fetch_card_metadata listing fetch failed (%s): %s",
                    market_hash_name, exc)

    # Fallback: hit Steam Store API only when the listing page didn't
    # give us a usable game name. Cached, so a 403 there only costs us
    # once per process.
    if not game_name:
        game_appid = extract_game_appid(market_hash_name) or str(appid)
        game_name = fetch_game_name(game_appid)

    return {
        "display_name": display_name,
        "game_name": game_name,
        "image_url": image_url,
    }


def steam_url(appid: str | int, market_hash_name: str) -> str:
    return f"steam://openurl/{market_url(appid, market_hash_name)}"


def parse_market_url(url: str) -> tuple[str, str] | None:
    """Extract (appid, market_hash_name) from a Steam Market listing URL.

    Accepts both /listings/APPID/NAME and query-param style URLs.
    Returns None if the URL doesn't look like a market listing.
    """
    # https://steamcommunity.com/market/listings/730/AK-47%20%7C%20Redline%20%28Field-Tested%29
    m = re.search(r"/market/listings/(\d+)/(.+)", url)
    if m:
        appid = m.group(1)
        name = urllib.parse.unquote(m.group(2).split("?")[0])
        return appid, name
    return None


def fetch_prices_batch(items: list[dict], currency: int = 18, country: str = "UA",
                       delay: float = 1.5) -> list[dict]:
    """Fetch prices for a list of watchlist items, respecting rate-limit delay.

    Each item must have 'appid' and 'market_hash_name'.
    Returns same list with 'lowest_price', 'volume', 'error' added.

    Behaviour on errors:
    - Generic exception on one card → log + capture in that row's "error",
      keep going for the rest of the list (so we still get a "Done." line).
    - HTTP 429 (RateLimitedError) on ANY card → ABORT the rest of the
      batch. All remaining items get "error": "rate-limited" without an
      HTTP call. Caller can detect this by `rate_limited_retry_after` on
      the returned list.

    `delay` default of 1.5s — orderbook endpoint accepts much faster bursts
    (tested at 0.3s with zero throttle), but we sit at 1.5s out of basic
    politeness so the request profile doesn't look DoS-y to whatever
    monitoring Steam runs. Configurable via `market.poll_delay_sec` in
    config.json so future tuning doesn't need a code change.
    """
    t_batch = time.monotonic()
    results: _BatchResults = _BatchResults()
    rate_limited_retry_after: int | None = None
    aborted_at: int | None = None
    for i, item in enumerate(items):
        if i > 0:
            time.sleep(delay)
        try:
            info = get_price(item["appid"], item["market_hash_name"], currency, country)
            results.append({**item, **info, "error": None})
        except RateLimitedError as exc:
            log.error(t("log.batch_abort_rate_limited",
                        name=pretty_name(item),
                        remaining=len(items) - i))
            rate_limited_retry_after = exc.retry_after
            results.append({**item, "lowest_price": None, "volume": None,
                            "error": "rate-limited"})
            aborted_at = i + 1
            break
        except Exception as exc:
            # Surfacing a failed price fetch as ERROR (was WARNING):
            # this is a real loss for the user — the card just won't
            # have a "current price" this round — and the Журнал tab
            # tints ERROR rows red so it stands out among the routine
            # INFO chatter.
            log.error(t("log.get_price_failed",
                        name=pretty_name(item),
                        kind=type(exc).__name__, err=exc))
            results.append({**item, "lowest_price": None, "volume": None, "error": str(exc)})
    # If we aborted mid-batch, synthesise "rate-limited" rows for the rest
    # so the caller's bookkeeping (last_seen update, etc.) sees the whole
    # input list, just with the tail marked as error.
    if aborted_at is not None:
        for rest in items[aborted_at:]:
            results.append({**rest, "lowest_price": None, "volume": None,
                            "error": "rate-limited"})
    log.info(t("log.batch_time", count=len(items),
               elapsed=time.monotonic() - t_batch))
    # _BatchResults supports attribute assignment (plain list doesn't).
    results.rate_limited_retry_after = rate_limited_retry_after
    return results


# ---------------------------------------------------------------------------
# Authenticated endpoints — require session cookies from Tier 1/2 login.
# ---------------------------------------------------------------------------

# Steam doesn't ship a clean JSON wallet-balance endpoint that can be hit
# without store-side fingerprinting; the JSON paths (`getfundedwallet`,
# `transactionhistory/getmoredata`) get blocked from third-party callers
# pretty quickly. The account page itself is straightforward HTML and
# carries the balance as a localised string in a known element.
_ACCOUNT_PAGE_URL = "https://store.steampowered.com/account/"
_WALLET_BALANCE_RE = re.compile(
    r'<a[^>]*id=["\']header_wallet_balance["\'][^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
# The account page embeds the user's region in a JS object on the
# server-rendered HTML:
#   g_rgWalletInfo = {"wallet_currency":18,"wallet_country":"UA",...};
# We MUST anchor parsing inside g_rgWalletInfo because the page has
# other JSON fragments mentioning "wallet_country" / "wallet_currency"
# — Steam renders region/wallet-recharge availability lists in the
# header and footer that include `"wallet_country":"TR"` and similar
# entries for countries that are NOT the user's. Grabbing the first
# match without anchoring picks one of those by chance and gives
# wrong results (the bug we just fixed).
#
# Strategy: pull the g_rgWalletInfo = { ... }; block first, then
# scan inside it for the two fields.
_WALLET_INFO_BLOCK_RE = re.compile(
    r'g_rgWalletInfo\s*=\s*(\{[^}]*\})\s*;',
    re.IGNORECASE,
)
_WALLET_CURRENCY_RE = re.compile(
    r'"wallet_currency"\s*:\s*(\d+)',
)
_WALLET_COUNTRY_RE = re.compile(
    r'"wallet_country"\s*:\s*"([A-Z]{2})"',
)
# (Account-page fallback regexes removed — the modern /account/ template
# no longer embeds g_rgWalletInfo / g_AccountData, so we rely on
# /market/ exclusively for currency + country.)


def fetch_wallet_balance(cookies: dict | None) -> str | None:
    """Scrape the user's wallet balance from `store.steampowered.com/account/`.

    Returns the balance verbatim as Steam renders it ("5,46₴", "$3.21",
    "1 234,56 ₽"), so the caller doesn't need to know currency-specific
    formatting rules — Steam's already done that for the user's locale.

    `cookies` is the per-domain dict from `browser_cookies.extract_steam_cookies`:
    `{"steamcommunity.com": {...}, "store.steampowered.com": {...}}`.
    We need the store-domain subset because steamcommunity / steampowered
    are two separate apex domains with independent cookies — community-
    only cookies get a redirect to the login page on the store.

    Returns None on:
      * no cookies passed (Tier 3 manual-ID path has no session)
      * no store cookies (user logged into community only, e.g. cookies
        captured from a browser that hadn't visited the store)
      * network failure / non-200 response
      * page didn't include the balance element (session expired
        server-side, layout change, etc.)

    Logs at debug for the success path and warning for the failure
    paths so a missing balance is traceable in watch.log without
    spamming the user.
    """
    store_cookies = (cookies or {}).get("store.steampowered.com") or {}
    if "steamLoginSecure" not in store_cookies:
        log.debug("wallet balance skipped — no store.steampowered.com cookie")
        return None
    try:
        resp = requests.get(
            _ACCOUNT_PAGE_URL,
            cookies=store_cookies,
            headers={
                "User-Agent": _UA,
                # Ask for the user's actual locale so the wallet string
                # uses their currency formatting (otherwise Steam might
                # serve English/USD).
                "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.7,en;q=0.5",
                "Accept": ("text/html,application/xhtml+xml,application/xml;"
                           "q=0.9,*/*;q=0.8"),
            },
            timeout=_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        log.warning("wallet balance fetch failed: %s", e)
        return None
    if resp.status_code != 200:
        log.warning("wallet balance HTTP %s", resp.status_code)
        return None
    # If the cookie was rejected, Steam serves the login page instead —
    # the regex just won't match and we return None.
    m = _WALLET_BALANCE_RE.search(resp.text)
    if not m:
        log.warning("wallet balance element not found "
                    "(session expired or layout changed)")
        return None
    # Steam wraps the balance in extra whitespace and sometimes an
    # `&nbsp;` between number and currency symbol. Normalise both so
    # the GUI doesn't have to.
    raw = m.group(1).replace(" ", " ").strip()
    log.debug("wallet balance: %r", raw)
    return raw


_MARKET_PAGE_URL = "https://steamcommunity.com/market/"


def fetch_wallet_info(cookies: dict | None) -> dict | None:
    """Two-step fetch: balance from /account/, currency+country from /market/.

    Steam used to embed `g_rgWalletInfo = {...}` on the account page,
    but the modern template no longer ships that JS object — only the
    formatted balance string survives there. The Community Market page
    DOES still render `g_rgWalletInfo` (it needs it to format listing
    prices in the user's local currency), so we use the two-page combo:

      * /account/                — store.steampowered.com  → balance
      * /market/                 — steamcommunity.com      → currency + country

    Returns:

        {
            "balance":  "5,46₴" | None,
            "currency": 18 | None,        # Steam internal int code
            "country":  "UA" | None,      # ISO-2
            "session_expired": True | False | None,
        }

    `session_expired` is the explicit "your cookies are stale" signal:
      * True  — we had cookies AND the request reached Steam but neither
                page returned the logged-in shape (balance element
                missing OR g_rgWalletInfo block missing). Steam's normal
                expired-cookie behaviour: a 200 with the login page HTML
                instead of the requested page, so we can't rely on HTTP
                status alone — content sniffing is the only reliable
                check.
      * False — at least one page returned the logged-in shape, so the
                session is alive even if the other page failed.
      * None  — we didn't try (no cookies on either domain).

    The caller (GUI _apply_wallet_info) uses this to switch on the
    "session expired" toast + ⚠ widget indicator without us doing any
    UI work down here.

    Any field is None independently if its source failed. Returns
    None entirely if both source-domain cookies are missing (Tier 3
    manual-ID flow — no session at all).

    Logging is at warning for failure paths so they show up in the
    Журнал tab; the per-call parsed-result line stays at debug to
    avoid noise after every successful refresh.
    """
    store_cookies = (cookies or {}).get("store.steampowered.com") or {}
    community_cookies = (cookies or {}).get("steamcommunity.com") or {}
    if ("steamLoginSecure" not in store_cookies
            and "steamLoginSecure" not in community_cookies):
        log.debug("wallet info skipped — no session cookies on either domain")
        return None

    balance: str | None = None
    currency: int | None = None
    country: str | None = None
    # Tri-state: True/False once we get any response, None until then.
    # Inverted at the end: any successful logged-in shape flips it False;
    # if every attempt produced "no logged-in markers" we leave True.
    expired_signals = 0  # how many domains returned "looks expired"
    domain_attempts = 0  # how many domains we actually queried

    # --- balance: store-domain /account/ page -----------------------
    if "steamLoginSecure" in store_cookies:
        domain_attempts += 1
        try:
            resp = requests.get(
                _ACCOUNT_PAGE_URL,
                cookies=store_cookies,
                headers={
                    "User-Agent": _UA,
                    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.7,en;q=0.5",
                    "Accept": ("text/html,application/xhtml+xml,application/xml;"
                               "q=0.9,*/*;q=0.8"),
                },
                timeout=_TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                m_bal = _WALLET_BALANCE_RE.search(resp.text)
                if m_bal:
                    balance = m_bal.group(1).replace("\xa0", " ").strip()
                else:
                    # 200 OK but no header_wallet_balance element — Steam
                    # served the login page in place of /account/. This
                    # is the canonical expired-cookies signature.
                    log.warning("wallet balance not found on account page "
                                "(store session likely expired)")
                    expired_signals += 1
            else:
                log.warning("wallet balance HTTP %s", resp.status_code)
                # 401/403/302→login on /account/ → also expired.
                if resp.status_code in (401, 403):
                    expired_signals += 1
        except requests.RequestException as e:
            log.warning("wallet balance fetch failed: %s", e)
            # Network errors don't count as expired — could be flaky DNS.
            # Leave session_expired alone.
            domain_attempts -= 1  # this attempt is inconclusive

    # --- currency + country: community-domain /market/ page ----------
    # The community market page embeds g_rgWalletInfo for logged-in
    # users so it can render listings in the user's local currency.
    # Needs steamcommunity.com cookies (the store-domain cookies don't
    # authenticate the community subdomain — different cookie scope).
    if "steamLoginSecure" in community_cookies:
        domain_attempts += 1
        try:
            resp = requests.get(
                _MARKET_PAGE_URL,
                cookies=community_cookies,
                headers={
                    "User-Agent": _UA,
                    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.7,en;q=0.5",
                    "Accept": ("text/html,application/xhtml+xml,application/xml;"
                               "q=0.9,*/*;q=0.8"),
                },
                timeout=_TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                text = resp.text
                m_block = _WALLET_INFO_BLOCK_RE.search(text)
                if m_block:
                    block = m_block.group(1)
                    m_cur = _WALLET_CURRENCY_RE.search(block)
                    if m_cur:
                        currency = int(m_cur.group(1))
                    m_cnt = _WALLET_COUNTRY_RE.search(block)
                    if m_cnt:
                        country = m_cnt.group(1)
                else:
                    log.warning("wallet info block missing on market page "
                                "(community session likely expired)")
                    expired_signals += 1
            else:
                log.warning("market page HTTP %s", resp.status_code)
                if resp.status_code in (401, 403):
                    expired_signals += 1
        except requests.RequestException as e:
            log.warning("market page fetch failed: %s", e)
            domain_attempts -= 1

    # Final expired verdict: only flag True if we actually tried at
    # least one domain AND every successful attempt produced an
    # "expired" signature. Mixed results (one fresh, one expired) =
    # session is alive — Steam sometimes drops one cookie sooner than
    # the other and the alive one is enough for our purposes.
    # Verdict policy: ANY domain returning an expired signature flags
    # the session as expired — even if the other domain is still alive.
    # Earlier logic let store-side liveness mask community-side death,
    # which is exactly how the user ended up with a working balance
    # widget but a broken "Import from Steam" (mylistings/orders both
    # 400 on a dead community session).
    if domain_attempts == 0:
        session_expired = None
    else:
        session_expired = (expired_signals > 0)

    log.debug("wallet info: balance=%r currency=%r country=%r expired=%r",
              balance, currency, country, session_expired)
    return {
        "balance": balance,
        "currency": currency,
        "country": country,
        "session_expired": session_expired,
    }




# ---------------------------------------------------------------------------
# Active listings + buy orders — for the Phase 3 "Import from Steam" flow.
# ---------------------------------------------------------------------------

# Steam's `mylistings/render/` endpoint returns a JSON envelope where the
# heavy data lives in three pieces:
#   * `assets` — keyed by appid → context → assetid, has the canonical
#                `market_hash_name`, `name`, and `market_fee_app` (the
#                real game appid for community items, which live under
#                Steam's own appid 753 / context 6).
#   * `hovers` — a blob of JS `CreateItemHoverFromContainer(...)` calls
#                that wire listingid → assetid; this is how we connect
#                a row to its asset metadata.
#   * `results_html` — rendered listing rows that carry the actual sell
#                      price as Steam-formatted text plus the game-name
#                      tag we'd otherwise have to fetch via Store API.
# Pagination is `start` + `count` (max 100 per page). 500-row safety cap
# because a user with thousands of active listings is plausible but
# absurd, and we'd rather stop early than rate-limit ourselves.
_MYLISTINGS_URL = "https://steamcommunity.com/market/mylistings/render/"
_MYLISTINGS_PAGE = 100
_MYLISTINGS_MAX = 500

# CreateItemHoverFromContainer( g_rgAssets, 'mylisting_<lid>_name', <community_appid>, '<context>', '<assetid>', 1 );
_HOVER_RE = re.compile(
    r"CreateItemHoverFromContainer\(\s*g_rgAssets,\s*"
    r"'mylisting_(\d+)_name',\s*(\d+),\s*'(\d+)',\s*'(\d+)'"
)
# Per-listing HTML block — listingid → buyer-pays price + game-name tag.
# `re.DOTALL` because the row spans many lines of pretty-printed HTML.
_LISTING_BLOCK_RE = re.compile(
    r'id="mylisting_(?P<lid>\d+)"'
    r'.*?title="This is the price the buyer pays\.">\s*'
    r'(?P<price>[^<]+?)\s*</span>'
    r'.*?<span class="market_listing_game_name">'
    r'(?P<game>[^<]*)</span>',
    re.DOTALL,
)


def fetch_market_listings(cookies: dict | None) -> list[dict]:
    """Fetch the user's currently-active Steam Market sale listings.

    Returns a list of dicts in the same shape our salelist.json entries
    use:
        {
            "listingid":         "<steam listing id>",
            "appid":             <int — real game appid, not 753>,
            "market_hash_name":  "<appid>-<card name>",
            "display_name":      "<card name without the appid prefix>",
            "game_name":         "<Steam Market subtitle, e.g. 'X Foil Trading Card'>",
            "price":             <float — what the buyer pays>,
            "price_raw":         "<formatted string Steam served>",
        }

    Pagination is handled internally (Steam caps at 100 per page); we
    stop at `_MYLISTINGS_MAX` or sooner if `total_count` is reached.

    `cookies` is the per-domain dict from `browser_cookies.extract_steam_cookies`;
    we use the community subset (listings live under steamcommunity.com).
    Returns an empty list — never raises — on:
      * no cookies / no community cookies (Tier 3 manual ID),
      * network failure / non-JSON / `success=false`.

    Failure modes are logged at warning level for diagnostic visibility
    but never propagate, because import is a polish feature on top of
    the normal app and a flaky network shouldn't tank the GUI.
    """
    community = (cookies or {}).get("steamcommunity.com") or {}
    if "steamLoginSecure" not in community:
        log.debug("mylistings skipped — no community cookies")
        return []

    out: list[dict] = []
    start = 0
    while start < _MYLISTINGS_MAX:
        try:
            resp = requests.get(
                _MYLISTINGS_URL,
                params={"query": "", "start": start, "count": _MYLISTINGS_PAGE},
                cookies=community,
                headers={
                    "User-Agent": _UA,
                    "Accept": "application/json, */*",
                    "Referer": "https://steamcommunity.com/market/",
                },
                timeout=_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as e:
            log.warning("mylistings network error at start=%d: %s", start, e)
            break
        if resp.status_code != 200:
            log.warning("mylistings HTTP %s at start=%d", resp.status_code, start)
            # Steam community endpoints reply 400 (with a JSON envelope)
            # for stale cookies, 401/403 for missing/revoked ones.
            # All three mean "log in again" — translate into the
            # explicit signal the import-flow handler watches for.
            if resp.status_code in (400, 401, 403):
                raise SteamSessionExpired(
                    f"mylistings returned HTTP {resp.status_code}"
                )
            break
        try:
            data = resp.json()
        except ValueError:
            log.warning("mylistings non-JSON at start=%d", start)
            break
        if not data.get("success"):
            log.warning("mylistings success=false at start=%d", start)
            break

        # listingid → assetid map for this page.
        lid_to_aid: dict[str, str] = {}
        for m in _HOVER_RE.finditer(data.get("hovers", "") or ""):
            lid_to_aid[m.group(1)] = m.group(4)

        # Asset metadata lives under appid 753 (Steam) / context 6
        # (Community items) for trading cards. The bigger nested form
        # `data["assets"][appid][context][assetid]` keeps us safe if
        # Steam later starts mixing in other appids/contexts here.
        assets_753 = (data.get("assets") or {}).get("753", {}).get("6", {})

        html = data.get("results_html", "") or ""
        for block in _LISTING_BLOCK_RE.finditer(html):
            listingid = block.group("lid")
            price_raw = block.group("price").strip()
            game_name = block.group("game").strip()

            assetid = lid_to_aid.get(listingid)
            asset = assets_753.get(assetid or "")
            if not asset:
                # No metadata for this listing — skip it. Happens if the
                # listing is for a non-community item or if hovers/assets
                # somehow disagree; either way we can't enrich it.
                log.debug("mylistings: no asset for listing %s", listingid)
                continue

            mhn = asset.get("market_hash_name") or ""
            display_name = asset.get("name") or asset.get("market_name") or ""

            # IMPORTANT: `appid` here is what goes into the Steam Market
            # URL (and every subsequent get_price call), NOT the game's
            # own appid. For trading cards that's always 753 — Steam
            # Community Items — and the *game* appid is encoded as the
            # "<game_appid>-" prefix on `market_hash_name`. Storing 753
            # is what `_add_by_url` does for manually-added cards too,
            # so imported and manual rows stay shape-compatible.
            # Using `market_fee_app` (the game appid) here used to break
            # subsequent get_price calls for every imported card — Steam
            # returns success=false because the orderbook expects the
            # 753+prefixed-mhn pair, not the game-appid+bare-name pair.
            out.append({
                "listingid":        listingid,
                "appid":            753,
                "market_hash_name": mhn,
                "display_name":     display_name,
                "game_name":        game_name,
                "price":            parse_price(price_raw),
                "price_raw":        price_raw,
            })

        total = int(data.get("total_count") or 0)
        start += _MYLISTINGS_PAGE
        if start >= total:
            break

    log.info("fetched %d active listings", len(out))
    return out


# Buy orders live on the Market home page (no separate JSON endpoint —
# Valve never built one). The HTML uses the same `market_listing_row`
# scaffolding as mylistings; rows have `id="mybuyorder_<id>"` and the
# linked listing URL carries `/<appid>/<mhn>` in the path so we don't
# need a second cross-reference to assets.
_MARKET_HOME_URL = "https://steamcommunity.com/market/"
# Buy-order row layout uses *two* `market_listing_price` spans:
#   1) Inside `market_listing_my_price` — has the inline "<qty> @" hint
#      followed by the per-unit price (`4₴` in the wild). This is what
#      we actually want.
#   2) Inside `market_listing_buyorder_qty` — bare quantity ("1").
# A naive `.*?market_listing_price.*?` grabs the first span's *first*
# text node, which is the inline-qty hint, then a bare `[^<]+?` captures
# its content instead of the real price. Anchor on `market_listing_my_price`
# and consume the inline-qty span explicitly to land on the unit price.
_BUYORDER_BLOCK_RE = re.compile(
    r'id="mybuyorder_(?P<oid>\d+)"'
    r'.*?<div class="market_listing_right_cell market_listing_my_price">'
    r'.*?<span class="market_listing_inline_buyorder_qty">[^<]*</span>'
    r'\s*(?P<price>[^<]+?)\s*</span>'
    r'.*?href="https://steamcommunity\.com/market/listings/'
    r'(?P<appid>\d+)/(?P<mhn_enc>[^"]+)"[^>]*>(?P<display>[^<]+)</a>'
    r'.*?<span class="market_listing_game_name">'
    r'(?P<game>[^<]*)</span>',
    re.DOTALL,
)


def fetch_buy_orders(cookies: dict | None) -> list[dict]:
    """Fetch the user's active buy orders from the Steam Market home page.

    Returns a list of dicts mirroring the sale-listings shape so the
    import dialog can treat both the same way downstream:
        {
            "buy_orderid":       "<id>",
            "appid":             <int>,
            "market_hash_name":  "<mhn>",
            "display_name":      "<card name>",
            "game_name":         "<game-name tag>",
            "price":             <float>,
            "price_raw":         "<formatted string>",
        }

    No JSON endpoint exists — Steam scaffolds buy orders into the same
    market home page that lists active sales. We scrape the HTML; the
    `mybuyorder_<id>` block layout has been stable for years but if
    Steam changes it the regex will silently return an empty list and
    we log a warning.

    Same fail-quiet contract as `fetch_market_listings`.
    """
    community = (cookies or {}).get("steamcommunity.com") or {}
    if "steamLoginSecure" not in community:
        return []

    try:
        resp = requests.get(
            _MARKET_HOME_URL,
            cookies=community,
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                # Force a content-language hint so Steam doesn't redirect
                # us into a geo-flavoured variant we'd then have to follow.
                "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.7,en;q=0.5",
            },
            timeout=_TIMEOUT,
            # Steam routinely 302s the market home page (region/currency
            # negotiation, trailing-slash normalisation). Follow them —
            # the final page on each redirect chain is the real market
            # home with the buy-orders section the way we expect.
            allow_redirects=True,
        )
    except requests.RequestException as e:
        log.warning("buy orders network error: %s", e)
        return []
    if resp.status_code != 200:
        log.warning("buy orders HTTP %s", resp.status_code)
        # Same cookies-stale signal as fetch_market_listings — raise
        # so the GUI import flow can show "log in again" instead of
        # silently rendering "nothing to import".
        if resp.status_code in (400, 401, 403):
            raise SteamSessionExpired(
                f"buy orders returned HTTP {resp.status_code}"
            )
        return []

    html = resp.text
    if "mybuyorder_" not in html:
        # Two cases land here: a real empty buy-orders list, OR a dead
        # session (Steam serves the login page with HTTP 200 instead of
        # refusing the request). We tell them apart by sniffing for a
        # POSITIVE "logged in" marker — `g_steamID = "76561…"` (steamID
        # as a JS string) on a real market page, vs `g_steamID = false`
        # on the login page. Negative markers (login URL, openid, etc.)
        # gave false positives because Steam puts a "Sign in" link in
        # the header for logged-in users too.
        m_id = re.search(r'g_steamID\s*=\s*"(\d+)"', html)
        if not m_id or m_id.group(1) == "0":
            log.warning("buy orders returned login page — session expired")
            raise SteamSessionExpired(
                "buy orders page has no g_steamID — reads as logged-out"
            )
        log.info("no active buy orders")
        return []

    out: list[dict] = []
    for m in _BUYORDER_BLOCK_RE.finditer(html):
        mhn = urllib.parse.unquote(m.group("mhn_enc"))
        # `appid` is the URL-side appid — for trading cards that's 753
        # (Steam Community Items), for game-native items it'd be the
        # game's own appid. Steam's orderbook / priceoverview need this
        # exact (appid, mhn) pair to look up the card. Previously we
        # overrode it with `extract_game_appid(mhn)` (the prefix from
        # the mhn), which made every imported buy order fail to refresh
        # because the orderbook returns success=false for that pair.
        try:
            appid = int(m.group("appid"))
        except ValueError:
            continue
        out.append({
            "buy_orderid":      m.group("oid"),
            "appid":            appid,
            "market_hash_name": mhn,
            "display_name":     m.group("display").strip(),
            "game_name":        m.group("game").strip(),
            "price":            parse_price(m.group("price")),
            "price_raw":        m.group("price").strip(),
        })

    if not out:
        # The token check above said there ARE orders but our regex
        # found none — likely a layout change. Worth a louder warning.
        log.warning("buy_order rows present but regex matched 0 — Steam layout changed?")

    log.info("fetched %d active buy orders", len(out))
    return out


# Steam's `market_listing_game_name` tag is really "game name + item
# type" smushed together — sometimes with an em-dash separator
# ("Path of Exile — Тло профілю"), sometimes plain concatenation
# ("METAL SLUG X Foil Trading Card"). We split it so the GUI can show
# the game and the item type as separate columns.
#
# Suffix list covers the trading-card universe in the languages the app
# actually supports plus English (Steam falls back to English for some
# games regardless of locale). Long suffixes first when matching so
# "Foil Trading Card" wins over plain "Trading Card".
_TYPE_SUFFIXES_DASH = [
    # Ukrainian
    " — Фольгована картка обміну",
    " — Картка обміну",
    " — Тло профілю",
    " — Емоція",
    # Russian
    " — Фольгированная карточка",
    " — Карточка обмена",
    " — Фон профиля",
    " — Смайлик",
    # English (with em-dash — uncommon but possible)
    " — Foil Trading Card",
    " — Trading Card",
    " — Profile Background",
    " — Emoticon",
]
_TYPE_SUFFIXES_PLAIN = [
    # English (Steam ships these as plain concatenation on most games)
    " Foil Trading Card",
    " Trading Card",
    " Profile Background",
    " Emoticon",
]


def split_game_and_type(game_name_raw: str) -> tuple[str, str]:
    """Split Steam's combined "game — item-type" string into two parts.

    Returns `(game, item_type)`. Either side can be empty:
      * empty input         → ("", "")
      * known dashed suffix → strip it, return game without the dash.
      * known plain suffix  → strip the trailing words.
      * unrecognised + has " — "  → split on the final em-dash anyway —
        better to expose a non-empty type even if we don't recognise it.
      * unrecognised, no dash → the whole thing becomes the game name;
        type stays "".

    Long suffixes are tried before short ones so "Foil Trading Card"
    doesn't get truncated to just "Trading Card".
    """
    if not game_name_raw:
        return ("", "")
    raw = game_name_raw.strip()

    # Sort by length descending — match longest suffix first.
    for suffix in sorted(_TYPE_SUFFIXES_DASH + _TYPE_SUFFIXES_PLAIN,
                         key=len, reverse=True):
        if raw.endswith(suffix):
            game = raw[: len(raw) - len(suffix)].rstrip(" —")
            item_type = suffix.lstrip(" —").strip()
            return (game, item_type)

    # Fallback for locales/games we don't have suffixes for: if the
    # string contains an em-dash separator at all, split on the last
    # one.
    if " — " in raw:
        game, _, item_type = raw.rpartition(" — ")
        return (game.strip(), item_type.strip())

    return (raw, "")