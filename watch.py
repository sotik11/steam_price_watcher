"""Single-run price watcher — invoked by Windows Task Scheduler."""
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from i18n import t

BASE = Path(__file__).parent

_log_handler = RotatingFileHandler(
    BASE / "watch.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_log_handler, logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("watch")


def load_json(path: Path) -> Any:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_json(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


_CLOSED_STATUSES = {"bought", "sold"}

# Reserved state.json key — not a watchlist entry, marks a cooldown window
# after Steam returned HTTP 429. Looks like "__rate_limited_until":
# "2026-06-04T22:15:30" (ISO, naive UTC, same format as other timestamps in
# state.json). When the wall clock is past that, the lock is gone.
_RATE_LIMIT_KEY = "__rate_limited_until"

# Default cooldown when Steam doesn't send a Retry-After header. 30 min is
# a sensible middle ground — Steam's IP bans on priceoverview tend to last
# anywhere from a few minutes to about an hour, and 30 min lets us recover
# without the next 6 scheduled runs pounding the API for nothing.
_DEFAULT_RATE_LIMIT_COOLDOWN_MIN = 30


def _migrate_state_keys(state: dict) -> bool:
    """Prepend "buy:" to legacy state keys (idempotent).

    Skips the reserved meta keys (prefix "__") so the cooldown marker stays
    on the top level, not under "buy:".
    """
    changed = False
    for k in list(state.keys()):
        if k.startswith("__"):
            continue
        if not k.startswith(("buy:", "sell:")):
            state["buy:" + k] = state.pop(k)
            changed = True
    return changed


def _rate_limit_active_until(state: dict, now: datetime) -> datetime | None:
    """Return the cooldown deadline if it's still in the future, else None.

    Also clears a stale marker from `state` (caller decides whether to
    persist). Lets the rest of watch.py treat the absence of the marker
    as the normal path.
    """
    raw = state.get(_RATE_LIMIT_KEY)
    if not raw:
        return None
    try:
        deadline = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        state.pop(_RATE_LIMIT_KEY, None)
        return None
    if now >= deadline:
        # Cooldown expired — wipe so the next run is unblocked cleanly.
        state.pop(_RATE_LIMIT_KEY, None)
        return None
    return deadline


def _set_rate_limit(state: dict, now: datetime, retry_after: int | None) -> None:
    """Stamp a cooldown deadline in `state`. Caller saves state to disk."""
    minutes = (max(1, (retry_after + 59) // 60) if retry_after
               else _DEFAULT_RATE_LIMIT_COOLDOWN_MIN)
    deadline = now + timedelta(minutes=minutes)
    state[_RATE_LIMIT_KEY] = deadline.isoformat()


def _process_list(kind: str, list_path: Path, *,
                  config, state, currency, country,
                  template, token, chat_id,
                  repeat_if_lower, remind_after_hours, now,
                  poll_delay=1.5):
    """Poll one card-list (buy or sell), update last_seen, send alerts.

    Returns (state_dirty, list_dirty) so the caller can decide whether to
    write the files back to disk.
    """
    items = load_json(list_path) or []
    active = [c for c in items if c.get("status") not in _CLOSED_STATUSES]
    if not active:
        return False, False

    state_dirty = False
    list_dirty = False

    # Self-heal stale "alerted" status. If the user lowered the target after
    # an alert fired (e.g. alerted at 5, then target dropped from 5 to 4),
    # the previous alert no longer matches the current rule — wipe it so the
    # GUI doesn't show a misleading "сповіщено" and antispam restarts cleanly
    # against the new target. The matching state entry goes too.
    for w in active:
        if w.get("status") != "alerted":
            continue
        target = w.get("target_price")
        if not isinstance(target, (int, float)):
            continue
        key = f"{kind}:{w.get('appid')}:{w.get('name')}"
        entry = state.get(key)
        if not entry:
            continue
        last_price = entry.get("last_alerted_price")
        if not isinstance(last_price, (int, float)):
            continue
        # Same condition the alert rule uses below.
        still_qualifies = (last_price <= target) if kind == "buy" else (last_price < target)
        if not still_qualifies:
            log.info(
                f"clearing stale 'alerted' on {w.get('name')!r}: "
                f"last_alerted_price={last_price} no longer hits target={target}"
            )
            w["status"] = ""
            list_dirty = True
            state.pop(key, None)
            state_dirty = True

    log.info(t("log.checking", count=len(active)) + f"  ({kind})")

    from steam import fetch_prices_batch
    # telegram.send_alert is called inside alerts.evaluate_and_alert; the
    # import lives there. Keep the alerts import here so we don't pay for it
    # if the early "no active cards" return fires.
    from alerts import evaluate_and_alert

    # Sell list may contain duplicate (appid, name) entries (the user is
    # selling multiple copies of the same card). De-dup before hitting
    # Steam — one priceoverview per unique card, then fan the result out
    # to every matching row in `items`.
    seen: set[tuple] = set()
    unique_active: list = []
    for c in active:
        ident = (c.get("appid"), c.get("name"))
        if ident in seen:
            continue
        seen.add(ident)
        unique_active.append(c)

    results = fetch_prices_batch(unique_active, currency=currency, country=country,
                                 delay=poll_delay)

    # If the batch was aborted by a 429, stamp the cooldown so the next
    # scheduled run skips entirely instead of pounding Steam again.
    retry_after = getattr(results, "rate_limited_retry_after", None)
    if retry_after is not None or any(
            r.get("error") == "rate-limited" for r in results):
        _set_rate_limit(state, now, retry_after)
        state_dirty = True

    def _find_all(appid_, name_):
        """All `items` rows matching this card identity (handles duplicates)."""
        return [w for w in items
                if w.get("appid") == appid_ and w.get("name") == name_]

    for item in results:
        name = item.get("name", "?")
        appid = item.get("appid", "?")
        # kind-prefix keeps buy- and sell-side antispam independent for the
        # same card sitting in both lists.
        key = f"{kind}:{appid}:{name}"

        if item.get("error"):
            log.warning(t("log.fetch_error", name=name, err=item["error"]))
            # Surface rate-limit visually in the GUI: tag every matching
            # row with status="rate_limited" (blue background) so the user
            # sees which cards weren't polled this round. We only overwrite
            # benign statuses — never trample an alerted/bought/sold flag.
            if item.get("error") == "rate-limited":
                for w in _find_all(appid, name):
                    if w.get("status") in ("", "error", "rate_limited"):
                        if w.get("status") != "rate_limited":
                            w["status"] = "rate_limited"
                            list_dirty = True
            continue

        lowest = item["lowest_price"]
        target = item.get("target_price")

        if lowest is None or target is None:
            log.warning(t("log.missing_target", name=name, lowest=lowest, target=target))
            continue

        # Successful fetch — auto-clear a stale "rate_limited" badge so the
        # row goes back to plain zebra. Don't touch other statuses.
        for w in _find_all(appid, name):
            if w.get("status") == "rate_limited":
                w["status"] = ""
                list_dirty = True

        log.info(t("log.card_status",
                   name=name[:40], lowest=lowest, target=target,
                   volume=item.get("volume")))

        # Update last_seen on EVERY row matching this card (duplicates
        # in the sell list all show the same current market price).
        matching = _find_all(appid, name)
        new_last_seen = item.get("lowest_price_raw") or f"{lowest:.2f}"
        for w in matching:
            if w.get("last_seen") != new_last_seen:
                w["last_seen"] = new_last_seen
                list_dirty = True

        # Buy: alert when market hit OR dropped below my target ("good
        #   time to buy at my asking price").
        # Sell: alert only when market dropped STRICTLY below my target —
        #   equal price doesn't undercut my listing yet, no reason to
        #   panic-relist.
        # Real decision lives in alerts.evaluate_and_alert so the GUI's
        # manual "Оновити зараз" path can call the SAME logic — single
        # source of truth means antispam state stays consistent regardless
        # of who triggered the poll.
        sd, did_alert, _reason = evaluate_and_alert(
            kind=kind, info=item, state=state,
            token=token, chat_id=chat_id, template=template,
            repeat_if_lower=repeat_if_lower,
            remind_after_hours=remind_after_hours, now=now,
        )
        if sd:
            state_dirty = True
        if did_alert:
            # Mark EVERY matching row as alerted (sell list may have
            # duplicates — they all hit target at the same time).
            for w in matching:
                if w.get("status") != "alerted":
                    w["status"] = "alerted"
                    list_dirty = True

    if list_dirty:
        save_json(list_path, items)
    return state_dirty, list_dirty


def main():
    config_path = BASE / "config.json"
    if not config_path.exists():
        log.error(t("log.config_missing"))
        sys.exit(1)

    config = load_json(config_path)
    state = load_json(BASE / "state.json") or {}
    # Legacy state.json had "{appid}:{name}" keys — bring them under the
    # buy-side namespace so antispam history isn't lost.
    state_changed = _migrate_state_keys(state)

    token = config["telegram"]["bot_token"]
    chat_id = str(config["telegram"]["chat_id"])
    currency = config["market"].get("currency", 18)
    country = config["market"].get("country", "UA")
    # Pause between price-fetch requests inside one poll. The orderbook
    # endpoint we use doesn't enforce a hard rate limit (tested at 0.3s
    # without throttle), but 1.5s keeps the traffic profile polite.
    poll_delay = float(config["market"].get("poll_delay_sec", 1.5))
    template = (config.get("message_template") or "").strip() or t("tg.message.default")
    antispam = config.get("antispam", {})
    remind_after_hours = antispam.get("remind_after_hours", 24)
    repeat_if_lower = antispam.get("repeat_if_lower", True)

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Skip the whole run if Steam recently 429'd us — pounding the API
    # during the cooldown just gets us banned for longer (and floods the
    # log). The marker was written by _process_list on a previous run and
    # auto-clears once `now >= deadline` (see _rate_limit_active_until).
    deadline = _rate_limit_active_until(state, now)
    if deadline is not None:
        # `deadline` is a naive UTC datetime (state stores UTC; everything
        # inside compares UTC-to-UTC). For the log line we want LOCAL time,
        # because the user reads the clock on their wall, not UTC. Tag it
        # as UTC, then convert to local naive.
        local_deadline = (deadline.replace(tzinfo=timezone.utc)
                                  .astimezone()
                                  .replace(tzinfo=None))
        log.warning(t("log.skip_run_rate_limited",
                      until=local_deadline.strftime("%H:%M:%S")))
        # Persist the deadline-clear that _rate_limit_active_until may
        # have done if we'd been past it (no-op here since we're inside
        # the active window, but cheap and keeps the file consistent).
        if state_changed:
            save_json(BASE / "state.json", state)
        return

    any_processed = False
    for kind, list_path in (("buy",  BASE / "watchlist.json"),
                            ("sell", BASE / "salelist.json")):
        if not list_path.exists():
            continue
        sd, ld = _process_list(
            kind, list_path,
            config=config, state=state,
            currency=currency, country=country,
            template=template, token=token, chat_id=chat_id,
            repeat_if_lower=repeat_if_lower,
            remind_after_hours=remind_after_hours,
            now=now, poll_delay=poll_delay,
        )
        if sd:
            state_changed = True
        any_processed = any_processed or (ld is not None)
        # If the buy-list run already hit a 429, the cooldown is now armed —
        # don't even try the sell list this round.
        if _RATE_LIMIT_KEY in state:
            break

    if state_changed:
        save_json(BASE / "state.json", state)

    if not any_processed:
        log.info(t("log.watchlist_empty"))

    log.info(t("log.done"))


if __name__ == "__main__":
    # Wrap main() so an unexpected crash (network stack hiccup, OS-level
    # signal, anything) leaves a traceback in watch.log instead of dying
    # silently — the previous "60 min of bare 'Checking' lines with no
    # follow-up" episode in the log had no traceback because the process
    # died between the first log call and the next one.
    t_run = time.monotonic()
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        log.exception(t("log.unhandled"))
        raise
    finally:
        log.info(t("log.run_finished", elapsed=time.monotonic() - t_run))
        # Flush handlers so a kill from Task Scheduler doesn't lose the tail.
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass