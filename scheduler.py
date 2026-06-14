"""Windows Task Scheduler wrapper via schtasks.exe."""
import subprocess
import sys
import time
from pathlib import Path

TASK_NAME = "SteamPriceWatcher"
BASE = Path(__file__).parent
PYTHON = sys.executable
SCRIPT = BASE / "watch.py"
VBS = BASE / "run.vbs"

# Module-level cache for task_info() to avoid spawning schtasks.exe on every
# GUI refresh (each call takes 300-500 ms and blocks the UI thread).
_INFO_CACHE: dict = {"value": None, "expires_at": 0.0}
_INFO_TTL_SECONDS = 10.0


def _run(args: list[str]) -> tuple[int, str, str]:
    # CREATE_NO_WINDOW suppresses the console flash that schtasks.exe
    # would otherwise pop up — pythonw.exe has no console of its own,
    # so each subprocess.run was briefly creating a black box.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        args, capture_output=True, text=True,
        encoding="cp866", errors="replace",
        creationflags=creationflags,
    )
    return result.returncode, result.stdout, result.stderr


def _invalidate_cache() -> None:
    """Force the next task_info() call to hit schtasks.exe again."""
    _INFO_CACHE["expires_at"] = 0.0


def task_info(force: bool = False) -> dict:
    """Return dict with keys: exists, enabled, next_run, status.

    Result is cached for `_INFO_TTL_SECONDS` to avoid spawning schtasks.exe on
    every GUI refresh. Pass `force=True` to bypass the cache (used after
    create/enable/disable/delete so the UI reflects the new state immediately).
    """
    now = time.monotonic()
    if not force and _INFO_CACHE["value"] is not None and now < _INFO_CACHE["expires_at"]:
        return _INFO_CACHE["value"]

    rc, out, _ = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "CSV", "/NH"])
    if rc != 0:
        info = {"exists": False, "enabled": False, "next_run": None, "status": "not found"}
    else:
        lines = [l.strip('"') for l in out.strip().split(",")]
        # CSV columns: TaskName, Next Run Time, Status
        next_run = lines[1] if len(lines) > 1 else None
        status = lines[2] if len(lines) > 2 else "Unknown"
        enabled = status.lower() not in ("disabled",)
        info = {"exists": True, "enabled": enabled, "next_run": next_run, "status": status}

    _INFO_CACHE["value"] = info
    _INFO_CACHE["expires_at"] = now + _INFO_TTL_SECONDS
    return info


def create_or_update(interval_minutes: int = 5) -> tuple[bool, str]:
    """Create or update the scheduled task. Returns (success, message)."""
    cmd = [
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAME,
        "/TR", f'wscript.exe "{VBS}"',
        "/SC", "MINUTE",
        "/MO", str(interval_minutes),
    ]
    rc, out, err = _run(cmd)
    _invalidate_cache()
    if rc == 0:
        return True, f"Task '{TASK_NAME}' created/updated (every {interval_minutes} min)"
    return False, err.strip() or out.strip()


def enable() -> tuple[bool, str]:
    rc, out, err = _run(["schtasks", "/Change", "/TN", TASK_NAME, "/ENABLE"])
    _invalidate_cache()
    return rc == 0, (err or out).strip()


def disable() -> tuple[bool, str]:
    rc, out, err = _run(["schtasks", "/Change", "/TN", TASK_NAME, "/DISABLE"])
    _invalidate_cache()
    return rc == 0, (err or out).strip()


def run_now() -> tuple[bool, str]:
    rc, out, err = _run(["schtasks", "/Run", "/TN", TASK_NAME])
    # next_run changes after Run now → invalidate so UI picks it up.
    _invalidate_cache()
    return rc == 0, (err or out).strip()


def delete() -> tuple[bool, str]:
    rc, out, err = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    _invalidate_cache()
    return rc == 0, (err or out).strip()