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
        # "game:" must be in this list — without it the migration
        # mangled every game-alert key into "buy:game:…" on each run,
        # losing the antispam entry and re-alerting every 5 minutes
        # (the 2026-06-11 duplicate-alert bug).
        if not k.startswith(("buy:", "sell:", "game:")):
            state["buy:" + k] = state.pop(k)
            changed = True
        elif k.startswith("buy:game:"):
            # Heal keys already mangled by the buggy version: strip the
            # bogus prefix so the antispam history is preserved.
            state[k[len("buy:"):]] = state.pop(k)
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
    # no_check («Не перевіряти») rows stay in the file and the GUI but
    # are invisible to polling — exactly like closed ones, except the
    # user can flip them back any time.
    active = [c for c in items
              if c.get("status") not in _CLOSED_STATUSES
              and not c.get("no_check")]
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
            from steam import pretty_name
            log.info(
                f"clearing stale 'alerted' on {pretty_name(w)!r}: "
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
    from alerts import evaluate_and_alert, is_dnd_now

    # «Не турбувати»: computed once per poll — suppresses sends while the
    # local time is inside the quiet window (antispam state untouched).
    dnd_active = is_dnd_now(
        (config.get("notifications") or {}).get("dnd"), country)

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

    from steam import pretty_name
    for item in results:
        name = item.get("name", "?")
        appid = item.get("appid", "?")
        # `name` stays the raw market_hash_name for state-key plumbing.
        # `pretty` is the user-facing display name for log lines so the
        # Журнал tab reads "Merrin" rather than "1774580-Merrin".
        pretty = pretty_name(item)
        # kind-prefix keeps buy- and sell-side antispam independent for the
        # same card sitting in both lists.
        key = f"{kind}:{appid}:{name}"

        if item.get("error"):
            # ERROR (was WARNING): the price fetch genuinely failed for
            # this card, so the user loses one round of data. Promoting
            # the log level makes it pop in the Журнал tab's red ERROR
            # tint instead of getting buried among routine WARNINGs.
            log.error(t("log.fetch_error", name=pretty, err=item["error"]))
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
            log.warning(t("log.missing_target", name=pretty, lowest=lowest, target=target))
            continue

        # Successful fetch — auto-clear a stale "rate_limited" badge so the
        # row goes back to plain zebra. Don't touch other statuses.
        for w in _find_all(appid, name):
            if w.get("status") == "rate_limited":
                w["status"] = ""
                list_dirty = True

        log.info(t("log.card_status",
                   name=pretty[:40], lowest=lowest, target=target,
                   volume=item.get("volume")))

        # Update last_seen on EVERY row matching this card (duplicates
        # in the sell list all show the same current market price).
        matching = _find_all(appid, name)
        new_last_seen = item.get("lowest_price_raw") or f"{lowest:.2f}"
        for w in matching:
            if w.get("last_seen") != new_last_seen:
                w["last_seen"] = new_last_seen
                list_dirty = True

        # Leader suppression (sell only). When the user has 2+ copies of
        # the same card listed and at least one of them sits exactly at
        # the market minimum (target == lowest), the user IS the market
        # leader — the "undercut" the other copies see is their own
        # cheaper listing, not a competitor. Alerting on those would be
        # pure noise, so the whole group goes quiet. The rows keep their
        # normal red/green tinting in the GUI (price math is untouched);
        # only the Telegram side is muted. The moment a competitor
        # undercuts below the leader's price, target == lowest stops
        # holding and alerts flow again.
        if kind == "sell" and len(matching) >= 2:
            leader = any(
                isinstance(w.get("target_price"), (int, float))
                and abs(w["target_price"] - lowest) < 0.005
                for w in matching
            )
            if leader:
                # Regaining leadership resolves the situation the earlier
                # alert warned about — drop the stale "сповіщено" badge on
                # the whole group and wipe the antispam entry, so if a
                # competitor undercuts again later the group fires a fresh
                # first_alert instead of being muted by repeat_if_lower.
                for w in matching:
                    if w.get("status") == "alerted":
                        w["status"] = ""
                        list_dirty = True
                if state.pop(key, None) is not None:
                    state_dirty = True
                log.info(t("log.leader_suppressed", name=pretty[:40],
                           lowest=lowest))
                continue

        # «Не сповіщати» (all copies muted): mirror the would-be alert
        # as a silent "checked" badge instead of a Telegram message.
        # Mixed groups (some muted, some not) go through the normal
        # path — the alert fires for the unmuted copies' sake.
        if matching and all(w.get("no_alert") for w in matching):
            hit = (lowest < target) if kind == "sell" else (lowest <= target)
            for w in matching:
                if hit and w.get("status") in ("", "checked"):
                    if w.get("status") != "checked":
                        w["status"] = "checked"
                        list_dirty = True
                elif not hit and w.get("status") == "checked":
                    w["status"] = ""
                    list_dirty = True
            continue

        # Buy: alert when market hit OR dropped below my target ("good
        #   time to buy at my asking price").
        # Sell: alert only when market dropped STRICTLY below my target —
        #   equal price doesn't undercut my listing yet, no reason to
        #   panic-relist.
        # Real decision lives in alerts.evaluate_and_alert so the GUI's
        # manual "Оновити зараз" path can call the SAME logic — single
        # source of truth means antispam state stays consistent regardless
        # of who triggered the poll.
        sd, did_alert, did_reset, _reason = evaluate_and_alert(
            kind=kind, info=item, state=state,
            token=token, chat_id=chat_id, template=template,
            repeat_if_lower=repeat_if_lower,
            remind_after_hours=remind_after_hours, now=now,
            dnd_active=dnd_active,
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
        elif did_reset:
            # Price bounced back above target — drop the green "сповіщено"
            # tag on all matching rows so the user sees we're back to
            # waiting, and a subsequent drop fires a fresh first_alert.
            for w in matching:
                if w.get("status") == "alerted":
                    w["status"] = ""
                    list_dirty = True

    if list_dirty:
        save_json(list_path, items)
    return state_dirty, list_dirty


def _game_minimum(g: dict) -> float | None:
    """Historical-low price for one game (same math as the GUI).

    `lowest_cut` (deepest recorded Steam discount %) × current regular
    price. Snaps to Steam's own final price when today's discount
    matches the cut — Steam rounds (675 × 10% → 67, not 67.50).
    """
    cut = g.get("lowest_cut")
    regular = g.get("regular")
    if not isinstance(cut, int) or not isinstance(regular, (int, float)):
        return None
    if g.get("discount_pct") == cut and isinstance(g.get("price"), (int, float)):
        return g["price"]
    return round(regular * (100 - cut) / 100.0, 2)


def _process_games(*, config, state, country,
                   template, token, chat_id,
                   repeat_if_lower, remind_after_hours, now):
    """Poll the wishlist-games list («Ігри» tab) and alert on all-time lows.

    Gated on `ui.bonus_content` — switching the checkbox off stops the
    polling entirely while keeping gamelist.json intact. One GetItems
    batch per 50 games (store API, not the rate-limited market one),
    so even 350+ games cost just a handful of requests.

    Returns (state_dirty, list_dirty) like _process_list.
    """
    list_path = BASE / "gamelist.json"
    items = load_json(list_path) or []
    if not items:
        return False, False

    from steam import (fetch_game_info_batch, GAME_HEADER_IMAGE_URL,
                       GAME_STORE_URL)
    from alerts import is_dnd_now
    dnd_active = is_dnd_now(
        (config.get("notifications") or {}).get("dnd"), country)
    from alerts import evaluate_and_alert

    log.info(t("log.checking", count=len(items)) + "  (games)")
    info = fetch_game_info_batch([g["appid"] for g in items],
                                 country=country)

    state_dirty = False
    list_dirty = False
    for g in items:
        if g.get("no_check") or g.get("status") in _CLOSED_STATUSES:
            continue
        d = info.get(g.get("appid"))
        if not d:
            continue
        for field in ("price", "price_str", "regular", "discount_pct"):
            if g.get(field) != d[field]:
                g[field] = d[field]
                list_dirty = True
        # Keep the (rarely changing) name fresh too — Steam occasionally
        # renames editions.
        if d["name"] and g.get("name") != d["name"]:
            g["name"] = d["name"]
            list_dirty = True

        # Alert rule: ANY active sale (знижка > 0). The historical
        # minimum is NOT a gate — it only powers the 🔥 punch line and
        # the red row tint in the GUI. (The earlier cut>0 gate silenced
        # every game ITAD has no data for, which was wrong.)
        if not (g.get("discount_pct") or 0) > 0:
            if g.get("status") in ("alerted", "checked"):
                g["status"] = ""
                list_dirty = True
            key = f"game:{g.get('appid')}:{g.get('name')}"
            if state.pop(key, None) is not None:
                state_dirty = True
            continue

        if not isinstance(g.get("price"), (int, float)):
            continue
        minimum = _game_minimum(g)
        at_min = isinstance(minimum, (int, float)) and g["price"] <= minimum
        # «Не сповіщати»: silent "checked" badge instead of Telegram.
        if g.get("no_alert"):
            if g.get("status") != "checked":
                g["status"] = "checked"
                list_dirty = True
            continue
        alert_info = {
            "name": g.get("name") or str(g.get("appid")),
            "appid": g.get("appid"),
            "display_name": g.get("name") or "",
            "game_name": g.get("name") or "",
            "market_hash_name": g.get("name") or "",
            "image_url": GAME_HEADER_IMAGE_URL.format(appid=g["appid"]),
            "alert_url": GAME_STORE_URL.format(appid=g["appid"]),
            "lowest_price": g["price"],
            "lowest_price_raw": g.get("price_str") or f"{g['price']:.2f}",
            # Target == current price → "lowest <= target" always holds
            # during a sale; antispam governs repeats.
            "target_price": g["price"],
            "volume": f"-{g.get('discount_pct') or 0}%",
            "at_historical_min": at_min,
        }
        sd, did_alert, did_reset, _reason = evaluate_and_alert(
            kind="game", info=alert_info, state=state,
            token=token, chat_id=chat_id, template=template,
            repeat_if_lower=repeat_if_lower,
            remind_after_hours=remind_after_hours, now=now,
            dnd_active=dnd_active,
        )
        if sd:
            state_dirty = True
        if did_alert and g.get("status") != "alerted":
            g["status"] = "alerted"
            list_dirty = True
        elif did_reset and g.get("status") == "alerted":
            g["status"] = ""
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

    # NB: the «Не турбувати» window only SUPPRESSES sends here (per-list,
    # via alerts.is_dnd_now). The journal "started/ended" log lives in the
    # GUI on a short timer — watch.py runs every 5 min, so a short window
    # could slip entirely between two polls and never be logged here.

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

    # Wishlist games («Ігри» tab) — only when the bonus-content switch
    # is on, and never during a rate-limit cooldown armed above. Uses
    # the store API (separate rate budget from the market endpoints),
    # but skipping during a 429 keeps the whole run's behaviour simple.
    if ((config.get("ui") or {}).get("bonus_content")
            and _RATE_LIMIT_KEY not in state):
        sd, ld = _process_games(
            config=config, state=state, country=country,
            template=template, token=token, chat_id=chat_id,
            repeat_if_lower=repeat_if_lower,
            remind_after_hours=remind_after_hours, now=now,
        )
        if sd:
            state_changed = True
        any_processed = any_processed or ld

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