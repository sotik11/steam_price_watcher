"""Browser-cookie extraction for Steam login (Tier 2).

Pulls the active `sessionid` + `steamLoginSecure` cookies from a desktop
browser (Chrome / Edge / Firefox / Opera), so we end up with an
authenticated session without the user ever typing a password.

Flow used by the GUI dialog (`gui.pyw → _open_browser_cookies_dialog`):

  1. `detect_installed_browsers()`   → list of supported browsers
                                       actually on this machine.
  2. User picks one (or it auto-picks if only one is installed).
  3. `kill_browser(spec.exe_names)`  → force-close every process the
                                       browser owns (Chrome holds the
                                       cookie SQLite DB locked while
                                       any of its processes are alive).
  4. `wait_until_gone(...)`          → poll tasklist until processes
                                       are actually gone (Windows is
                                       slow to release file handles).
  5. `extract_steam_cookies(spec)`   → read cookies from the cookie DB
                                       and pluck the two we care about.
  6. `parse_steamid_from_cookie(...)` → steamID64 lives inside the
                                       `steamLoginSecure` value, no
                                       extra network call needed.
  7. `verify_session(cookies)`       → confirm the cookies are actually
                                       authenticated (Steam may have
                                       expired them without telling the
                                       browser).
  8. `relaunch_browser(spec.exe_path)` → put the browser back where we
                                       found it. Tabs auto-restore on
                                       all four supported browsers.

Windows-only — uses `taskkill` / `tasklist` / Windows registry. The app
itself only targets Windows, so no cross-platform shim is needed.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import browser_cookie3
import requests

try:
    import rookiepy
    _HAS_ROOKIEPY = True
except ImportError:
    rookiepy = None
    _HAS_ROOKIEPY = False

log = logging.getLogger("browser_cookies")

# CREATE_NO_WINDOW on Windows — kill the black console flash when the
# helper subprocesses (taskkill / tasklist) launch. Same pattern as
# scheduler.py uses for schtasks.exe.
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_DETACHED_PROCESS = 0x00000008 if sys.platform == "win32" else 0

# Browser process names vary inside each family — Chrome alone runs as
# main + several `chrome.exe` children (renderer, GPU, utility, etc.).
# `taskkill /F /IM <name>` matches every process with that name, so
# listing the main exe is enough; we don't need to enumerate children.
#
# `app_paths_key` maps to the Windows registry entry that hands us the
# absolute path to the browser exe — used both for "is this browser
# installed?" detection and for relaunch.
#
# `cookies_fn` is the `browser_cookie3` callable that knows how to crack
# this browser's specific cookie DB format (Chrome SQLite + DPAPI, Firefox
# SQLite + NSS, Opera SQLite, …).


@dataclass
class BrowserSpec:
    """Everything we need to interact with one browser type.

    Built once at import time. `exe_path` is filled in by
    `detect_installed_browsers()`; everything else is immutable config.

    Fields:
      code, display_name, exe_names — UI + process management.
      app_paths_key   — primary registry lookup for the exe path.
      extra_exe_paths — fallback list (with `%VAR%` allowed) for installs
                        that don't register App Paths. Opera ships into
                        `%LOCALAPPDATA%\\Programs\\Opera`, which has no
                        registry entry, so we have to know where to look.
      cookies_fn      — browser_cookie3 callable for this browser's
                        cookie store format.
      profiles_dir    — for Chromium-based browsers that store multiple
                        profiles side by side (Chrome / Edge / Opera).
                        `%VAR%` expansion supported. None = browser has
                        a single profile or browser_cookie3 already knows
                        how to enumerate (Firefox).
    """
    code:           str
    display_name:   str
    exe_names:      list[str]                # taskkill /IM targets
    app_paths_key:  str                      # registry App Paths entry
    cookies_fn:     Callable                 # browser_cookie3.<x>
    extra_exe_paths: list[str] = field(default_factory=list)
    profiles_dir:   str | None = None        # chromium-style multi-profile root
    exe_path:       str | None = field(default=None, init=False)

    def is_installed(self) -> bool:
        return self.exe_path is not None


BROWSERS: list[BrowserSpec] = [
    BrowserSpec(
        code="chrome",  display_name="Chrome",
        exe_names=["chrome.exe"],
        app_paths_key="chrome.exe",
        cookies_fn=browser_cookie3.chrome,
        # Chrome stores profiles as "Default", "Profile 1", "Profile 2", …
        # each with its own Cookies SQLite. browser_cookie3.chrome()
        # without args reads only "Default", which silently misses users
        # whose Steam session lives in a secondary profile.
        profiles_dir=r"%LOCALAPPDATA%\Google\Chrome\User Data",
    ),
    BrowserSpec(
        code="edge",    display_name="Edge",
        exe_names=["msedge.exe"],
        app_paths_key="msedge.exe",
        cookies_fn=browser_cookie3.edge,
        profiles_dir=r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
    ),
    BrowserSpec(
        code="firefox", display_name="Firefox",
        exe_names=["firefox.exe"],
        app_paths_key="firefox.exe",
        cookies_fn=browser_cookie3.firefox,
        # browser_cookie3.firefox enumerates profiles internally via
        # profiles.ini, so we don't need a manual profiles_dir here.
    ),
    BrowserSpec(
        code="opera",   display_name="Opera",
        exe_names=["opera.exe", "launcher.exe"],
        # Opera doesn't register App Paths; rely on the known per-user
        # install path. The first match wins.
        app_paths_key="opera.exe",
        extra_exe_paths=[
            r"%LOCALAPPDATA%\Programs\Opera\opera.exe",
            r"%LOCALAPPDATA%\Programs\Opera\launcher.exe",
            r"%PROGRAMFILES%\Opera\opera.exe",
            r"%PROGRAMFILES(X86)%\Opera\opera.exe",
        ],
        cookies_fn=browser_cookie3.opera,
        # Opera follows Chromium's profile layout: cookies live in
        # `Default\Network\Cookies` under the profile root. `browser_cookie3.opera()`
        # without an explicit path probes a stale legacy location and
        # gives up — point it at the real root so the profile sweep finds
        # `Default\Network\Cookies` like it does for Chrome.
        profiles_dir=r"%APPDATA%\Opera Software\Opera Stable",
    ),
]


# ---- HTTP posture (matches steam_login.py / steam.py) ------------------

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


class BrowserCookiesError(Exception):
    """Anything that goes wrong during the cookie-extraction flow.

    The dialog catches this and surfaces .args[0] as a status line.
    """


# ----------------------------------------------------------------------
# Installed-browser detection (Windows registry App Paths)
# ----------------------------------------------------------------------

def _find_browser_exe(app_paths_name: str,
                      extra_paths: list[str] | None = None) -> str | None:
    """Look up the absolute exe path under HKCU/HKLM App Paths, with fallbacks.

    Order of attempts:
      1. HKCU `App Paths` — per-user installs (Chrome default since ~2014).
      2. HKLM `App Paths` — system-wide installs (Edge, IT rollouts).
      3. `extra_paths` — hard-coded fallback locations for installers
         that don't register App Paths (Opera, for example, drops itself
         under `%LOCALAPPDATA%\\Programs\\Opera` and never touches the
         registry's App Paths key). `%VAR%` is expanded.

    Returns None if no candidate exists on disk.
    """
    rel = fr"Software\Microsoft\Windows\CurrentVersion\App Paths\{app_paths_name}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rel) as key:
                # Default value (empty name) holds the absolute path.
                val, _ = winreg.QueryValueEx(key, "")
                if val and Path(val).exists():
                    return val
        except OSError:
            continue
    # Fall back to per-user install conventions (Opera et al.).
    for raw in (extra_paths or []):
        expanded = os.path.expandvars(raw)
        if Path(expanded).exists():
            return expanded
    return None


def detect_installed_browsers() -> list[BrowserSpec]:
    """Return only those browsers actually installed on this machine.

    Side-effect: stamps `exe_path` on each returned spec so the caller
    doesn't need a second registry call before launching.
    """
    found: list[BrowserSpec] = []
    for spec in BROWSERS:
        path = _find_browser_exe(spec.app_paths_key, spec.extra_exe_paths)
        if path:
            spec.exe_path = path
            found.append(spec)
    return found


# ----------------------------------------------------------------------
# Process control — kill + wait
# ----------------------------------------------------------------------

def is_browser_running(exe_names: list[str]) -> bool:
    """True if any of the named processes are currently alive.

    Uses `tasklist /FI` for one filter at a time — `tasklist` doesn't
    support OR'd filters from the CLI, so we just iterate.

    We run tasklist in binary mode (no text=True) because its output is
    in the system OEM codepage (CP866 on RU Windows etc.), and decoding
    that as UTF-8 / locale-default crashes randomly depending on what
    other process names happen to be in the listing. We only need a
    substring match against the exe name (always ASCII), so byte-level
    comparison is both safe and simpler.
    """
    for name in exe_names:
        try:
            result = subprocess.run(
                ["tasklist", "/NH", "/FI", f"IMAGENAME eq {name}"],
                capture_output=True,
                creationflags=_CREATE_NO_WINDOW,
                timeout=5,
            )
            # When no match, tasklist prints "INFO: No tasks are running…"
            # in the OEM codepage. The exe name itself is ASCII either way.
            if name.lower().encode("ascii") in (result.stdout or b"").lower():
                return True
        except subprocess.SubprocessError:
            # If we can't tell, be conservative and assume it's running
            # — that way we won't try to extract from a locked DB.
            return True
    return False


def kill_browser(exe_names: list[str]) -> None:
    """Force-kill every process with one of the given image names.

    `/F` forces, `/T` kills the whole process tree (Chrome's helper
    children). Errors from taskkill (e.g. "process not found") are
    intentionally swallowed — the goal is "make sure they're gone",
    not "fail loudly when nothing was there".
    """
    for name in exe_names:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", name],
                capture_output=True,
                creationflags=_CREATE_NO_WINDOW,
                timeout=5,
            )
        except subprocess.SubprocessError as e:
            log.warning("taskkill failed for %s: %s", name, e)


def wait_until_gone(exe_names: list[str], timeout_sec: float = 5.0,
                    poll_interval: float = 0.3) -> bool:
    """Poll until none of the named processes are running, or timeout.

    Returns True if everything died in time, False on timeout. Windows
    can take 1-2 seconds to actually release the cookie SQLite file
    handle after the last process exits, so the caller may still want
    to wait a bit before trying to read it.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not is_browser_running(exe_names):
            return True
        time.sleep(poll_interval)
    return False


# ----------------------------------------------------------------------
# Cookie extraction
# ----------------------------------------------------------------------

# Domains the browser stored Steam cookies under. steam_login_secure is
# scoped to steamcommunity.com; community/store share session cookies
# via the broader domain. browser_cookie3 wants the host, not the path.
_STEAM_DOMAIN = "steamcommunity.com"


def _is_current_process_admin() -> bool:
    """True when we're running with an admin access token.

    Used to decide whether the rookiepy fallback is worth attempting:
    rookiepy can decrypt Chrome v127+ App-Bound Encryption cookies, but
    only when elevated. From a non-admin GUI process it would just hit
    the same wall as browser_cookie3, so we don't bother.
    """
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _extract_via_rookiepy(spec: BrowserSpec) -> dict[str, str]:
    """ABE-aware extraction via the `rookiepy` Rust library.

    rookiepy uses a different decryption path that knows how to unwrap
    the App-Bound-Encryption-wrapped master key on Chrome v127+. It
    needs admin rights to do so (the wrap is bound to the Chrome.exe
    process context, and only an elevated reader can re-create that
    context); from a non-admin process it raises the same kind of
    "needs admin" RuntimeError that browser_cookie3 produces.

    Returns the same `{sessionid, steamLoginSecure}` dict shape as the
    main extract path, so callers don't need to branch on which library
    won the day.

    Raises BrowserCookiesError on failure (no_session if the call
    succeeded but the cookie wasn't there).
    """
    if not _HAS_ROOKIEPY:
        raise BrowserCookiesError("rookiepy_unavailable")
    fn = getattr(rookiepy, spec.code, None)
    if fn is None:
        raise BrowserCookiesError(f"rookiepy_no_browser:{spec.code}")
    try:
        cookies_list = fn([_STEAM_DOMAIN])
    except Exception as e:
        # rookiepy uses Rust panics that bubble up as RuntimeError.
        raise BrowserCookiesError(f"rookiepy:{e}") from e
    out: dict[str, str] = {}
    for c in cookies_list:
        # rookiepy returns dicts. Filter to steamcommunity.com and our
        # two cookies of interest.
        if "steamcommunity.com" not in (c.get("domain") or ""):
            continue
        name = c.get("name")
        if name in ("sessionid", "steamLoginSecure"):
            out[name] = c.get("value") or ""
    if "steamLoginSecure" not in out:
        raise BrowserCookiesError("no_session")
    return out


def _list_chromium_profile_cookies(profiles_dir: str) -> list[Path]:
    """Find every `Cookies` SQLite file inside a Chromium User Data dir.

    Chromium-family browsers (Chrome, Edge, Brave, Opera-on-Chromium)
    keep each user profile in its own subdirectory under
    `<browser>/User Data/`, named `Default` for the first profile and
    `Profile N` for subsequent ones. Each profile has its own `Cookies`
    SQLite file.

    Recent Chrome versions moved the file from the profile root into a
    `Network/` subdir, so we check both.

    Returns the list sorted with `Default` first (most users are logged
    into Steam there; we want the fast path to hit on the first try).
    """
    root = Path(os.path.expandvars(profiles_dir))
    if not root.is_dir():
        return []
    matches: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        # Skip dotfiles, "System Profile", "Guest Profile" — those don't
        # carry real user sessions.
        if child.name.startswith(".") or child.name.endswith(" Profile"):
            continue
        for candidate in (child / "Network" / "Cookies", child / "Cookies"):
            if candidate.is_file():
                matches.append(candidate)
                break
    # "Default" first, then "Profile 1", "Profile 2"… alphabetical for ties.
    matches.sort(key=lambda p: (p.parent.name != "Default"
                                and p.parent.parent.name != "Default",
                                p.parent.name, p.parent.parent.name))
    return matches


def _jar_to_cookies(jar) -> dict[str, str]:
    """Extract our two Steam cookies from a `http.cookiejar.CookieJar`.

    Pulled into a helper because both the single-profile and
    multi-profile extraction paths need to do the same filtering.
    """
    cookies: dict[str, str] = {}
    for c in jar:
        # Only keep cookies scoped to steamcommunity / its subdomains —
        # extra cookies (Google Analytics, etc.) would muddy the saved
        # state for no benefit.
        if c.domain and "steamcommunity.com" not in c.domain:
            continue
        if c.name in ("sessionid", "steamLoginSecure"):
            cookies[c.name] = c.value
    return cookies


def extract_steam_cookies(spec: BrowserSpec,
                          retries: int = 3,
                          retry_delay: float = 1.0) -> dict[str, str]:
    """Return `{sessionid, steamLoginSecure}` from `spec`'s cookie DB.

    For Chromium-family browsers with `profiles_dir` set we sweep every
    profile and return the first one whose jar contains a Steam session
    — Chrome users routinely have a "Work" + "Personal" split and Steam
    is almost never in the `Default` profile that browser_cookie3 reads
    by default.

    For single-profile or self-enumerating browsers (Firefox, Opera) we
    call `cookies_fn(domain_name=...)` directly and let it pick.

    Retries because Windows file-handle release after the browser exits
    isn't instant — first read after kill can still hit a sharing
    violation. 3 attempts at 1-sec intervals is plenty in practice.

    Raises:
      BrowserCookiesError —
        * `db_locked: <details>`  → SQLite couldn't read any candidate
          even after retries.
        * `no_session`            → at least one cookie DB was readable
          but none contained a `steamLoginSecure` cookie scoped to
          steamcommunity.com.
    """
    candidates: list[Path | None]
    if spec.profiles_dir:
        files = _list_chromium_profile_cookies(spec.profiles_dir)
        if not files:
            # Browser is installed but no User Data dir exists yet —
            # treat that as "no session" so the dialog says "log in",
            # not "DB locked".
            raise BrowserCookiesError("no_session")
        candidates = list(files)
    else:
        # Sentinel — `None` means "let browser_cookie3 pick its default".
        candidates = [None]

    last_err: Exception | None = None
    found_readable = False
    needs_admin = False

    for candidate in candidates:
        for attempt in range(retries):
            try:
                if candidate is None:
                    jar = spec.cookies_fn(domain_name=_STEAM_DOMAIN)
                else:
                    jar = spec.cookies_fn(
                        cookie_file=str(candidate),
                        domain_name=_STEAM_DOMAIN,
                    )
                found_readable = True
                cookies = _jar_to_cookies(jar)
                if "steamLoginSecure" in cookies:
                    log.info("steam cookies found in %s",
                             candidate if candidate else "default profile")
                    return cookies
                # Readable but no Steam session in this profile — move on.
                log.debug("no steam session in %s",
                          candidate if candidate else "default profile")
                break
            except Exception as e:
                # browser_cookie3 raises BrowserCookieError, sqlite3.OperationalError,
                # PermissionError — all mean "try again or give up". Treat as one
                # category to keep the caller simple.
                last_err = e
                # App-Bound Encryption signatures (Chrome v127+ / Edge /
                # Opera / Brave when they pulled in the same change):
                # `RequiresAdminError` is what browser_cookie3 raises when
                # the cookies are ABE-encrypted and we don't have admin;
                # `Unable to get key` is what it raises when the DPAPI
                # blob in `Local State` itself is ABE-wrapped. Either way
                # the only path forward without an admin context is to
                # surface that fact to the GUI so it can prompt for UAC.
                etype = type(e).__name__
                msg = str(e)
                if (etype == "RequiresAdminError"
                        or "requires admin" in msg.lower()
                        or "unable to get key" in msg.lower()):
                    needs_admin = True
                    # No point retrying — admin status won't change between
                    # attempts and the next call will fail identically.
                    break
                log.debug("extract attempt %d/%d on %s failed: %s",
                          attempt + 1, retries,
                          candidate if candidate else "default profile", e)
                time.sleep(retry_delay)
        if needs_admin:
            break

    # Three outcomes, in priority order:
    # 1) ABE — try the rookiepy fallback if we're admin (rookiepy knows
    #    how to unwrap App-Bound-Encryption keys, browser_cookie3 doesn't).
    #    From a non-admin process there's nothing more to try — raise so
    #    the GUI triggers the UAC elevation path.
    # 2) Couldn't open any cookie DB → db_locked (Windows hadn't released
    #    the file handle in time, or the file genuinely doesn't exist).
    # 3) Opened OK but no Steam cookie in any profile → no_session.
    if needs_admin:
        if _is_current_process_admin():
            log.info("browser_cookie3 hit ABE despite admin; trying rookiepy")
            try:
                return _extract_via_rookiepy(spec)
            except BrowserCookiesError as rp_err:
                log.warning("rookiepy fallback also failed: %s", rp_err)
                # If the fallback got something concrete (e.g. no_session),
                # surface that rather than the misleading needs_admin.
                if str(rp_err) == "no_session":
                    raise
                # Otherwise let the original needs_admin escape so the
                # GUI's existing handling kicks in.
        raise BrowserCookiesError(f"needs_admin: {last_err}") from last_err
    if not found_readable:
        raise BrowserCookiesError(f"db_locked: {last_err}") from last_err
    raise BrowserCookiesError("no_session")


def parse_steamid_from_cookie(steam_login_secure: str) -> str:
    """Pull the steamID64 out of the `steamLoginSecure` cookie value.

    The cookie value is `<steamid64>||<jwt-style-token>` — but stored
    URL-encoded, so `||` is `%7C%7C`. unquote first, split, return the
    left half. We don't care about the token itself for the moment;
    requests handles it as an opaque cookie value.
    """
    decoded = urllib.parse.unquote(steam_login_secure)
    sid = decoded.split("||", 1)[0]
    if not sid.isdigit() or not sid.startswith("7656119"):
        raise BrowserCookiesError(f"bad_steamid_in_cookie: {sid!r}")
    return sid


def verify_session(cookies: dict[str, str]) -> bool:
    """Ping an authenticated endpoint to confirm cookies are alive.

    Uses `mylistings/render?count=1` — fast, cheap, requires auth.
    Steam returns 200 + JSON `{success: true, ...}` for a valid session;
    expired / wrong cookies get a redirect or 401. We treat anything
    other than "200 + valid JSON + success: true" as a dead session.

    Network failures count as "dead" too — there's no point saving
    cookies we can't actually use right now.
    """
    url = "https://steamcommunity.com/market/mylistings/render/?query=&start=0&count=1"
    try:
        resp = requests.get(
            url, cookies=cookies, headers=_HEADERS,
            timeout=_TIMEOUT, allow_redirects=False,
        )
    except requests.RequestException as e:
        log.warning("session verify network error: %s", e)
        return False
    if resp.status_code != 200:
        return False
    try:
        data = resp.json()
    except ValueError:
        return False
    return bool(data.get("success"))


# ----------------------------------------------------------------------
# Relaunch
# ----------------------------------------------------------------------

def relaunch_browser(exe_path: str) -> bool:
    """Re-launch the browser as a fully detached process.

    DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP so the GUI's Python
    process doesn't hold an HWND / job handle on the browser — if the
    user closes the GUI, the browser keeps running normally.

    Returns False on any error so the dialog can fall back to telling
    the user to launch it manually.
    """
    if not exe_path or not Path(exe_path).exists():
        return False
    try:
        subprocess.Popen(
            [exe_path],
            creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
            close_fds=True,
        )
        return True
    except OSError as e:
        log.warning("relaunch failed for %s: %s", exe_path, e)
        return False


# ----------------------------------------------------------------------
# Admin-elevated extraction (App-Bound Encryption workaround)
# ----------------------------------------------------------------------

# Output written by the helper. JSON shape:
#   {"cookies": {"sessionid": "...", "steamLoginSecure": "..."}, "error": null}
# or
#   {"cookies": null, "error": "ErrType: message\n<traceback>"}
_HELPER_BASENAME = "cookie_extract_helper.py"


def extract_steam_cookies_admin(spec: BrowserSpec,
                                python_exe: str,
                                helper_script: str,
                                timeout_sec: float = 30.0,
                                poll_interval: float = 0.3) -> dict[str, str]:
    """Run the cookie extractor as admin via UAC, return its cookies.

    Triggers a UAC prompt by `ShellExecuteW(verb="runas", ...)`. Windows
    handles the elevation; the helper runs with admin rights and writes
    its result to a temp file we then read back.

    `python_exe` should point at the venv's pythonw.exe (no console
    window) so the helper runs invisibly. `helper_script` is the
    absolute path to `cookie_extract_helper.py`.

    Raises:
      BrowserCookiesError —
        * `admin_denied`   → user clicked No on the UAC prompt.
        * `admin_timeout`  → helper didn't produce an output file in time.
        * `<helper_error>` → helper ran but its extraction failed.
    """
    import ctypes
    import tempfile

    # Per-call output file so concurrent dialogs (shouldn't happen, but
    # defend) don't trample each other.
    out_fd, out_path = tempfile.mkstemp(suffix=".json", prefix="steamck_")
    os.close(out_fd)
    # Pre-delete so the polling loop sees "no file yet" rather than the
    # empty file mkstemp leaves behind.
    try:
        os.unlink(out_path)
    except OSError:
        pass

    # Build the parameter string. Quote everything that might contain a
    # space — Python paths in `Program Files` are the classic landmine.
    params = f'"{helper_script}" "{spec.code}" "{out_path}"'

    SW_HIDE = 0  # don't flash a window even briefly during launch
    SEE_MASK_DEFAULT = 0
    # ShellExecuteW returns > 32 on success, ≤ 32 on error.
    # 5 = SE_ERR_ACCESSDENIED = user clicked No on the UAC prompt.
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", python_exe, params, str(Path(helper_script).parent),
        SW_HIDE,
    )
    if rc <= 32:
        raise BrowserCookiesError("admin_denied")

    # Wait for the helper to produce its output file. The actual cookie
    # read is sub-second; the UAC prompt eats most of the time the user
    # sees. 30 seconds is plenty for the prompt + the work itself.
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if os.path.exists(out_path):
            # Brief settle delay — Windows hands us the file as soon as
            # the helper opens it for writing, even before it's closed.
            time.sleep(0.1)
            try:
                payload = json.loads(Path(out_path).read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                # Not fully written yet — keep polling.
                pass
        time.sleep(poll_interval)
    else:
        raise BrowserCookiesError("admin_timeout")

    # Tidy up the temp file — we have the payload in memory now.
    try:
        os.unlink(out_path)
    except OSError:
        pass

    if payload.get("error"):
        # Helper ran but extraction inside it failed. The most common
        # interesting case is "Windows didn't actually elevate us" — that
        # happens when UAC is disabled AND the current account isn't in
        # the local Administrators group, so `ShellExecuteW("runas", …)`
        # quietly launches the helper at the GUI's own level instead of
        # elevating it. The helper reports its own admin status; surface
        # that distinct case explicitly so the GUI can give actionable
        # advice instead of just repeating the original ABE error.
        if payload.get("is_admin") is False:
            raise BrowserCookiesError("not_elevated")
        raise BrowserCookiesError(payload["error"])
    cookies = payload.get("cookies") or {}
    if "steamLoginSecure" not in cookies:
        raise BrowserCookiesError("no_session")
    return cookies
