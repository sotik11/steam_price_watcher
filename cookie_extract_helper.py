"""Admin-elevated cookie extractor for App-Bound Encryption browsers.

Modern Chrome (v127+) — and downstream Chromium browsers like Edge and
Opera that pulled in the same change — encrypt their cookies with a key
that's bound to the application context. The only way to decrypt them
from a separate process is to run as admin so the OS lets us read the
key material out of `Local State` via DPAPI's machine-level path.

This script is invoked via `ShellExecuteW(verb="runas", ...)` from
`gui.pyw` whenever the normal (non-admin) extraction hits
`RequiresAdminError`. It does the bare minimum:

  1. Read `<browser_code>` and `<output_json_path>` from argv.
  2. Run the same `browser_cookies.extract_steam_cookies(spec)` that the
     GUI would have run — only now we're elevated, so DPAPI cooperates.
  3. Write `{"cookies": {...}, "error": null}` (or
     `{"cookies": null, "error": "..."}`) to the output path as JSON.
  4. Exit. The GUI is polling the output path and will pick up the
     result without us ever sharing a stdio handle.

Keep this script tiny — every import here runs with admin rights, and
the smaller the attack surface, the better.
"""
from __future__ import annotations

import ctypes
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path


def _is_admin() -> bool:
    """True when the current process holds an admin token.

    Used purely for diagnostics — we want the helper's log to make it
    obvious whether the UAC elevation actually took effect (vs Windows
    silently launching the helper at the same level the GUI runs at,
    which happens when UAC is disabled on a non-admin account).
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _log(msg: str) -> None:
    """Append a diagnostic line to `cookie_helper.log` next to the helper.

    The GUI never sees the helper's stdout/stderr (we run it via
    ShellExecuteW which doesn't capture either), so without this log
    we'd have no way to find out what the helper actually did. Errors
    here are swallowed — diagnostics must never bring the helper down.
    """
    try:
        log_path = Path(__file__).parent / "cookie_helper.log"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat()} {msg}\n")
    except OSError:
        pass


def main() -> int:
    if len(sys.argv) != 3:
        # Misinvoked — nothing useful to write to the output file because
        # we don't know its path. Just exit non-zero.
        return 2

    browser_code = sys.argv[1]
    output_path = Path(sys.argv[2])

    _log(f"start browser={browser_code} out={output_path} admin={_is_admin()}")

    # Make the project root importable. Helper lives in the same dir as
    # browser_cookies.py, so __file__.parent is enough.
    sys.path.insert(0, str(Path(__file__).parent))

    result: dict = {
        "cookies": None, "error": None,
        "is_admin": _is_admin(), "trace": [],
    }
    try:
        import browser_cookies as bc

        spec = next((s for s in bc.BROWSERS if s.code == browser_code), None)
        if spec is None:
            raise RuntimeError(f"unknown browser code: {browser_code!r}")
        # The extraction code doesn't actually need exe_path for reading
        # cookies — it's only used by the GUI's relaunch step. Stub it so
        # any defensive `is_installed()` checks pass.
        spec.exe_path = "admin-context"

        # Helper runs admin (modulo the not_elevated case we report
        # separately). Prefer `rookiepy` since it's the one library that
        # actually handles Chrome v127+ App-Bound Encryption with admin
        # rights; `browser_cookie3` gives up at the same ABE wall whether
        # it's admin or not.
        #
        # If rookiepy fails (e.g. browser version it doesn't recognise,
        # or its hardcoded profile path differs from what we have), fall
        # back to browser_cookie3 — it handles older browsers and
        # browsers we've fixed up with explicit profiles_dir.
        cookies = None
        try:
            cookies = bc._extract_via_rookiepy(spec)
            result["trace"].append(f"rookiepy OK keys={list(cookies.keys())}")
            _log(f"rookiepy OK keys={list(cookies.keys())}")
        except Exception as rp_err:
            result["trace"].append(
                f"rookiepy FAIL: {type(rp_err).__name__}: {rp_err}"
            )
            _log(f"rookiepy FAIL: {type(rp_err).__name__}: {rp_err}")
            # Fall back to browser_cookie3 (with our internal profile
            # sweep). On ABE-affected browsers this will also fail — but
            # for completeness, try anyway and surface the better error.
            try:
                cookies = bc.extract_steam_cookies(spec)
                result["trace"].append(f"bc3 OK keys={list(cookies.keys())}")
                _log(f"bc3 OK keys={list(cookies.keys())}")
            except Exception as bc3_err:
                result["trace"].append(
                    f"bc3 FAIL: {type(bc3_err).__name__}: {bc3_err}"
                )
                _log(f"bc3 FAIL: {type(bc3_err).__name__}: {bc3_err}")
                # Re-raise with the rookiepy error as primary cause —
                # that's the one that matters for ABE diagnostics.
                raise rp_err
        result["cookies"] = cookies
    except Exception as e:
        # Stuff both the exception summary and a full traceback into
        # `error` — the GUI surfaces just the summary, the traceback is
        # there for the log file.
        result["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        _log(f"extract FAIL: {type(e).__name__}: {e}")

    try:
        output_path.write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8",
        )
        _log(f"wrote output, ok={result['cookies'] is not None}")
    except OSError as e:
        # If we can't even write the result, the GUI will see "no file
        # appeared within timeout" and surface its own error.
        _log(f"output write FAIL: {e}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
