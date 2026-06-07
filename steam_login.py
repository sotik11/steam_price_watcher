"""Steam-login helpers — Tier 3 (manual ID) + public-profile fetch.

This module is the foundation for Phase 1 Steam login (see DESIGN.md →
Steam-логін (Phase 1)). It implements the *unauthenticated* path:

  * `parse_steam_id(s)`          — normalise any user input form.
  * `resolve_vanity(vanity)`     — vanity URL → steamID64 via public XML.
  * `fetch_public_profile(sid)`  — persona name, avatar URL, visibility.
  * `check_inventory_public(sid)`— does the trading-card inventory load
                                   without a session?
  * `download_avatar(url, size)` — bytes → circular Tk PhotoImage.

No passwords, no API key, no cookies — everything in here works against
public Steam endpoints anyone can hit. Tier 1 (QR) and Tier 2 (browser
cookies) will be added as separate modules in later stages.
"""
from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

import requests

log = logging.getLogger("steam_login")

# Match the User-Agent / timeout posture of steam.py so we don't look like
# a third party when the user has just been fetching prices from the same
# IP a moment earlier. Public endpoints don't need the full ceremony, but
# being consistent costs nothing.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}
_TIMEOUT = (5, 8)

# steamID64 is a 17-digit number that always starts with 7656119… —
# Valve allocates from a single namespace anchored at 76561197960265728.
# Anything shorter or with a different prefix is not a valid public ID.
_STEAMID64_RE = re.compile(r"^7656119\d{10}$")

# Vanity URLs are case-insensitive, 3-32 chars, [A-Za-z0-9_-]. Steam's UI
# enforces this exactly. We're a bit permissive (no length check) so we
# can still try resolving anything the user typed before bailing out —
# Steam's XML endpoint will tell us "no match" cheaply.
_VANITY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SteamLoginError(Exception):
    """Anything that goes wrong during a Steam-login attempt.

    Carries a human-readable message; the dialog surfaces it verbatim.
    """


def parse_steam_id(raw: str) -> tuple[str, str]:
    """Normalise whatever the user typed into ('id' | 'vanity', value).

    Accepted shapes:
      * 17-digit steamID64       → ('id', '7656119…')
      * URL .../profiles/{id}    → ('id', '7656119…')
      * URL .../id/{vanity}      → ('vanity', '{vanity}')
      * Bare vanity string       → ('vanity', '{vanity}')

    Strips trailing slashes, query strings, and the optional scheme. Raises
    SteamLoginError with a translated-ish message if nothing matched.
    """
    if raw is None:
        raise SteamLoginError("empty")
    s = raw.strip()
    if not s:
        raise SteamLoginError("empty")

    # Drop scheme + host so we can treat URLs and bare paths uniformly.
    # Anything between steamcommunity.com and the meaningful path segment
    # gets chopped — also handles user pastes of "https://" + path only.
    s = re.sub(r"^https?://", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^(www\.)?steamcommunity\.com/?", "", s, flags=re.IGNORECASE)
    # Trim a trailing query string / fragment, then any trailing AND
    # leading slashes — covers paste-only inputs like "/id/myvanity"
    # that didn't include the host.
    s = s.split("?", 1)[0].split("#", 1)[0].strip("/")

    # /profiles/{id} or /id/{vanity} — last meaningful segment wins.
    m = re.match(r"^profiles/(\d+)$", s, flags=re.IGNORECASE)
    if m:
        s = m.group(1)
    else:
        m = re.match(r"^id/([^/]+)$", s, flags=re.IGNORECASE)
        if m:
            s = m.group(1)

    if _STEAMID64_RE.match(s):
        return ("id", s)
    if _VANITY_RE.match(s):
        return ("vanity", s)
    raise SteamLoginError("bad_format")


def resolve_vanity(vanity: str) -> str:
    """Vanity URL → steamID64 via the public XML profile endpoint.

    `https://steamcommunity.com/id/{vanity}/?xml=1` returns either a full
    profile XML (with `<steamID64>`) or a `<response><error>` block when
    the vanity doesn't exist. Raises SteamLoginError on either error path.
    """
    url = f"https://steamcommunity.com/id/{vanity}/?xml=1"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise SteamLoginError(f"network: {e}") from e
    if resp.status_code != 200:
        raise SteamLoginError(f"http_{resp.status_code}")

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise SteamLoginError(f"bad_xml: {e}") from e

    # Steam returns <response><error>… for unknown vanity URLs (HTTP 200,
    # mind you — the request itself succeeded). Detect that path first.
    if root.tag == "response":
        err = root.findtext("error") or "not_found"
        raise SteamLoginError(f"vanity_not_found: {err}")

    sid = (root.findtext("steamID64") or "").strip()
    if not _STEAMID64_RE.match(sid):
        raise SteamLoginError("no_steamid64_in_response")
    return sid


def fetch_public_profile(steamid64: str) -> dict[str, Any]:
    """Public profile fields via the XML endpoint — no auth required.

    Returns a dict with: `steamid`, `persona`, `avatar_url`, `state`
    (one of `public` / `friendsOnly` / `private` / `unknown`).

    `avatar_url` is the medium variant (64×64) — the floating widget
    runs at 40px so we don't need full-size, and medium is the smallest
    asset Steam serves on a CDN URL we can hotlink.

    Raises SteamLoginError on network/HTTP/parse failures.
    """
    url = f"https://steamcommunity.com/profiles/{steamid64}/?xml=1"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise SteamLoginError(f"network: {e}") from e
    if resp.status_code != 200:
        raise SteamLoginError(f"http_{resp.status_code}")

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise SteamLoginError(f"bad_xml: {e}") from e

    if root.tag == "response":
        err = root.findtext("error") or "profile_not_found"
        raise SteamLoginError(f"profile_error: {err}")

    persona = (root.findtext("steamID") or "").strip()
    # `avatarMedium` is the 64×64 PNG; `avatarFull` is 184×184 if we ever
    # need a HiDPI variant. Either works for circular cropping.
    avatar_url = (root.findtext("avatarMedium") or "").strip()
    # privacyState is "public" / "friendsonly" / "private". Lowercase
    # comparison only — Steam isn't strict about casing.
    state_raw = (root.findtext("privacyState") or "").strip().lower()
    state = {
        "public": "public",
        "friendsonly": "friendsOnly",
        "private": "private",
    }.get(state_raw, "unknown")

    return {
        "steamid": steamid64,
        "persona": persona,
        "avatar_url": avatar_url,
        "state": state,
    }


def check_inventory_public(steamid64: str) -> bool:
    """Is the user's trading-card inventory readable without a session?

    GETs `/inventory/{id}/753/6?count=1`. App 753 = Steam, context 6 =
    community items / trading cards / gems. 200 + JSON `success: True`
    means readable; 401/403 / JSON `success: False` means hidden. Other
    errors (network, 5xx) return False — we'd rather show "private" than
    misleadingly flag a private inventory as public.
    """
    url = f"https://steamcommunity.com/inventory/{steamid64}/753/6?count=1"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException:
        return False
    if resp.status_code != 200:
        return False
    try:
        data = resp.json()
    except ValueError:
        return False
    return bool(data.get("success"))


def download_avatar(url: str, size: int):
    """Download an avatar URL and return a circular Tk PhotoImage.

    Mirrors `_draw_placeholder_avatar` in gui.pyw: resize to `size`×`size`,
    apply an elliptical alpha mask, wrap in ImageTk.PhotoImage. Caller is
    responsible for stashing the returned image somewhere so Tk's garbage
    collector doesn't snatch it.

    Returns None on any failure (missing PIL, dead URL, decoder issue) —
    caller falls back to the placeholder.
    """
    if not url:
        return None
    try:
        from PIL import Image, ImageDraw, ImageTk
    except ImportError:
        log.warning("PIL not available — avatar download skipped")
        return None
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code != 200:
            log.warning("avatar fetch HTTP %s for %s", resp.status_code, url)
            return None
        img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        img = img.resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
        img.putalpha(mask)
        return ImageTk.PhotoImage(img)
    except Exception as e:
        log.warning("avatar download failed for %s: %s", url, e)
        return None
