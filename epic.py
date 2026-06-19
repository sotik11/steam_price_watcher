"""Epic Games Store price lookup by game title.

Used by the «Ігри» tab to show an Epic price next to the Steam one. The
public Store GraphQL `searchStore` (the same call the storefront search box
makes) returns localized price + a store URL, no auth needed.

Matching is STRICT: searchStore ranks by relevancy and will happily return
a different game ("Resident Evil 5" → "Resident Evil Requiem"), so we only
accept a result whose normalized title is (near-)equal to the query — else
the caller shows "---". Batch helper adds a polite delay between requests.
"""
import html
import logging
import re

import requests

log = logging.getLogger("epic")

EPIC_GQL = "https://store.epicgames.com/graphql"

# searchStore mirrors the storefront search. country drives currency,
# locale drives the formatted price string + the /<locale>/p/ URL.
_QUERY = """
query searchStoreQuery($keywords: String!, $country: String!, $locale: String!) {
  Catalog {
    searchStore(keywords: $keywords, country: $country, locale: $locale,
                count: 8, sortBy: "relevancy", sortDir: "DESC") {
      elements {
        title
        releaseDate
        effectiveDate
        productSlug
        urlSlug
        catalogNs { mappings(pageType: "productHome") { pageSlug } }
        offerMappings { pageSlug }
        price(country: $country) {
          totalPrice {
            discountPrice
            originalPrice
            currencyCode
            fmtPrice(locale: $locale) { discountPrice originalPrice }
          }
        }
      }
    }
  }
}
"""

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
}

_TIMEOUT = 20

# Trademark / edition noise stripped before comparing titles, so
# "BioShock Infinite" matches "BioShock Infinite: Complete Edition". NB:
# "Bundle" is deliberately NOT here — a bundle is a different product (its
# price misleads), so "Prototype" must NOT match "Prototype Bundle".
_EDITION_WORDS = (
    "complete", "edition", "deluxe", "ultimate", "definitive", "enhanced",
    "remastered", "remaster", "goty", "game of the year", "standard",
    "gold", "special", "directors cut", "director s cut",
)

# Roman → arabic so "Assassin's Creed II" == "Assassin's Creed 2".
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
          "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def _normalize(title: str) -> str:
    """Lowercase, decode entities, drop ™®©/punctuation + edition words,
    and fold roman numerals to digits — the canonical form we compare on."""
    s = html.unescape(title or "").lower()
    s = s.replace("’", "'")
    s = re.sub(r"[™®©]", " ", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)       # keep letters/digits/apostrophe
    s = re.sub(r"\s+", " ", s).strip()
    for w in _EDITION_WORDS:
        s = re.sub(rf"\b{re.escape(w)}\b", " ", s)
    tokens = [_ROMAN.get(t, t) for t in s.split()]
    return " ".join(tokens).strip()


def _best_match(query: str, elements: list) -> dict | None:
    """Return the element whose normalized title EQUALS the query.

    Strict equality (after edition-strip + roman-fold) — no fuzzy ratio and
    no prefix shortcut, because those let sequels/spin-offs through
    ("Resident Evil" → "…Requiem", "Kingdom Come: Deliverance" → "…2").
    First relevancy-ranked exact match wins; otherwise no match.
    """
    nq = _normalize(query)
    if not nq:
        return None
    for el in elements:
        if _normalize(el.get("title", "")) == nq:
            return el
    return None


def _is_future(date_str) -> bool:
    """Is this ISO date in the future? (Epic stamps unreleased/delisted
    offers with a 2099 sentinel.) Bad/empty input → not future."""
    if not date_str:
        return False
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        return dt > datetime.now(timezone.utc)
    except (ValueError, AttributeError):
        return False


def _extract(el: dict, locale: str) -> dict | None:
    """Build {price, price_str, url} from a matched search element.

    Returns None (caller shows «---») when the offer isn't really for sale
    right now, not just on a missing price:
      * discountPrice 0 or None — Epic returns 0 for delisted / "no price"
        offers (e.g. GTA III: The Definitive Edition), NOT a real freebie
        worth comparing; the user asked for «---» on these.
      * a future releaseDate or effectiveDate — "coming soon" titles carry
        an MSRP placeholder + a 2099 sentinel date (e.g. Judas showed
        3639 ₴ with release 2099); that price is meaningless for us.
    """
    if _is_future(el.get("releaseDate")) or _is_future(el.get("effectiveDate")):
        return None
    tp = ((el.get("price") or {}).get("totalPrice") or {})
    disc = tp.get("discountPrice")
    if not disc or disc <= 0:
        return None
    # discountPrice is in minor units (kopecks/cents).
    price = disc / 100.0
    fmt = tp.get("fmtPrice") or {}
    price_str = fmt.get("discountPrice") or fmt.get("originalPrice") or ""
    slug = None
    ns = (el.get("catalogNs") or {}).get("mappings") or []
    if ns:
        slug = ns[0].get("pageSlug")
    if not slug:
        om = el.get("offerMappings") or []
        if om:
            slug = om[0].get("pageSlug")
    slug = slug or el.get("productSlug") or el.get("urlSlug")
    url = f"https://store.epicgames.com/{locale}/p/{slug}" if slug else ""
    return {"price": price, "price_str": price_str, "url": url}


def fetch_epic_price(title: str, country: str = "UA",
                     locale: str = "uk") -> dict | None:
    """Look up one game on Epic. Returns {price, price_str, url} or None.

    None = no confident match (caller shows "---"). Network/errors also
    return None — best-effort, never raises to the caller.
    """
    try:
        resp = requests.post(
            EPIC_GQL,
            json={"query": _QUERY,
                  "variables": {"keywords": title, "country": country,
                                "locale": locale}},
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.debug("epic lookup failed for %r: %s", title, exc)
        return None
    els = (((data or {}).get("data") or {}).get("Catalog") or {}) \
        .get("searchStore", {}).get("elements") or []
    el = _best_match(title, els)
    return _extract(el, locale) if el else None


def fetch_epic_prices(titles, country: str = "UA", locale: str = "uk",
                      delay: float = 0.8) -> dict:
    """Batch lookup. Returns {title: {price, price_str, url}} for matches.

    Titles with no confident match are simply absent from the dict. A
    polite `delay` between requests keeps us off Epic's radar.
    """
    import time
    out: dict[str, dict] = {}
    seen: set[str] = set()
    for title in titles:
        if not title or title in seen:
            continue
        seen.add(title)
        m = fetch_epic_price(title, country, locale)
        if m:
            out[title] = m
        time.sleep(delay)
    return out
