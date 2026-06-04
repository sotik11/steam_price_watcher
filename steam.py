"""Steam Community Market API helpers."""
import logging
import re
import time
import urllib.parse
import requests

from i18n import t

log = logging.getLogger("steam")

PRICE_OVERVIEW_URL = "https://steamcommunity.com/market/priceoverview/"
LISTINGS_URL = "https://steamcommunity.com/market/listings/{appid}/{name}"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

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

# Persistent HTTP session so cookies (`sessionid`, `browserid`, `timezoneOffset`)
# survive across requests. Steam treats a session-cookie-less request as
# "random script" and ban-counters faster; a session that landed on the
# market home page first looks like a normal browsing tab.
_SESSION: requests.Session | None = None
_SESSION_WARMED_UP = False


def _get_session() -> requests.Session:
    """Lazily build a requests.Session with browser-like defaults.

    Single shared session means cookies persist across batch requests in
    one process (watch.py) and across "Оновити зараз" clicks in the GUI.
    """
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update(_DEFAULT_HEADERS)
    return _SESSION


def _warm_up_session() -> None:
    """Visit the market home page once to seed anonymous cookies.

    Steam's bot detection cares whether you've ever loaded a page on this
    "session". Loading the home page first puts `sessionid` + `browserid`
    in the cookie jar; subsequent priceoverview calls then look like
    AJAX from a tab the user already has open.

    Idempotent — runs once per process.
    """
    global _SESSION_WARMED_UP
    if _SESSION_WARMED_UP:
        return
    try:
        session = _get_session()
        # Use the home page (no specific Referer needed — first hit).
        # Replace the AJAX-y headers for this one navigation request.
        nav_headers = {
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,image/avif,image/webp,*/*;q=0.8"),
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        }
        session.get("https://steamcommunity.com/market/",
                    headers=nav_headers, timeout=_TIMEOUT)
        _SESSION_WARMED_UP = True
        log.info("steam session warmed up (cookies: %s)",
                 ", ".join(session.cookies.keys()) or "none")
    except Exception as exc:
        # Warm-up is best-effort. If it fails we still try priceoverview
        # afterwards — at worst we lose the cookie advantage, the call
        # itself can still succeed.
        log.warning("steam session warm-up failed: %s", exc)
        _SESSION_WARMED_UP = True  # don't retry on every call

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


def get_price(appid: str | int, market_hash_name: str, currency: int = 18, country: str = "UA") -> dict:
    """Fetch lowest_price, median_price, volume from priceoverview.

    Returns dict with keys: lowest_price (float|None), volume (int|None), raw (dict).
    Raises requests.HTTPError on non-2xx, requests.Timeout on timeout, or
    ValueError on parse failure / unexpected response shape.

    Important: in 2025 Steam started fingerprinting these requests. The call
    has to look like real AJAX from the matching listing page — that means
    a `Referer` pointing to THIS specific listing (not a random one, not
    blank), full Chrome-style headers, and a session that's already seen
    the home page. Without it we get banned after 10-100 requests for ~2h.
    """
    params = {
        "country": country,
        "currency": currency,
        "appid": str(appid),
        "market_hash_name": market_hash_name,
    }
    short_name = market_hash_name[:40]
    # Lazy session warm-up: visits steamcommunity.com/market/ once per
    # process to seed `sessionid` + `browserid` cookies. Subsequent calls
    # then look like AJAX from a tab the user already has open.
    _warm_up_session()
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
        raise ValueError(t("log.api_failure", name=market_hash_name))

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


def fetch_card_metadata(appid: str | int, market_hash_name: str) -> dict:
    """Resolve a card's pretty name, its game's display name, and a poster
    image URL we can send as a Telegram photo.

    Returns dict with keys: display_name, game_name, image_url.

    For Steam Community Items (appid=753) the hash name embeds the game's
    appid as a prefix; we strip it for display_name and hit appdetails for
    the game name. For game-native market items we fall back to using the
    market `appid` itself.
    """
    display_name = clean_card_name(market_hash_name)
    game_appid = extract_game_appid(market_hash_name) or str(appid)
    game_name = fetch_game_name(game_appid)
    image_url = fetch_card_image_url(appid, market_hash_name)
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
                       delay: float = 2.5) -> list[dict]:
    """Fetch prices for a list of watchlist items, respecting rate-limit delay.

    Each item must have 'appid' and 'market_hash_name'.
    Returns same list with 'lowest_price', 'volume', 'error' added.

    Behaviour on errors:
    - Generic exception on one card → log + capture in that row's "error",
      keep going for the rest of the list (so we still get a "Done." line).
    - HTTP 429 (RateLimitedError) on ANY card → ABORT the rest of the
      batch. All remaining items get "error": "rate-limited" without an
      HTTP call. Caller can detect this by `rate_limited_retry_after` on
      the returned list. Pounding Steam after the first 429 just earns us
      a longer ban — better to surface "we got told to back off" once and
      move on.

    `delay` default of 2.5s gives ~24 req/min, well under the ~30/min
    unofficial limit on priceoverview.
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
                        name=item.get("market_hash_name", "?"),
                        remaining=len(items) - i))
            rate_limited_retry_after = exc.retry_after
            results.append({**item, "lowest_price": None, "volume": None,
                            "error": "rate-limited"})
            aborted_at = i + 1
            break
        except Exception as exc:
            log.warning(t("log.get_price_failed",
                          name=item.get("market_hash_name", "?"),
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
