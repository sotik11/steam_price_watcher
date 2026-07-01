"""Shared alert decision + send logic.

Used by both watch.py (scheduled polling) and gui.pyw (manual «Оновити зараз»).
Lives in its own module so neither caller has to import the other —
importing watch.py from gui.pyw would create a second RotatingFileHandler
on watch.log, leading to file-rotation conflicts.

The single function `evaluate_and_alert` takes everything it needs as
arguments (no globals, no module-level side effects), runs the
antispam-aware decision, calls telegram.send_alert if appropriate, and
mutates `state` in place. Caller is responsible for persisting state
to disk.
"""
import logging
from datetime import datetime, timedelta

from i18n import t

log = logging.getLogger("alerts")


def is_dnd_now(dnd: dict | None, country: str, now_local=None) -> bool:
    """Is the current local time inside the «Не турбувати» window?

    `dnd` is `{"from": "HH:MM", "to": "HH:MM"}` or falsy (disabled). Time
    is compared in the profile country's local timezone (regions.local_now),
    so the window means what the user sees on their clock. A window where
    `from > to` is treated as crossing midnight (e.g. 23:00–08:00). Equal
    endpoints = disabled (zero-length).
    """
    if not dnd or not dnd.get("from") or not dnd.get("to"):
        return False
    try:
        fh, fm = (int(x) for x in dnd["from"].split(":"))
        th, tm = (int(x) for x in dnd["to"].split(":"))
    except (ValueError, AttributeError):
        return False
    if now_local is None:
        from regions import local_now
        now_local = local_now(country)
    cur = now_local.hour * 60 + now_local.minute
    start, end = fh * 60 + fm, th * 60 + tm
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    # Crosses midnight: active from `start` to 24:00 and 00:00 to `end`.
    return cur >= start or cur < end


def maybe_alert_epic(*, game: dict, state: dict, token: str, chat_id: str,
                     repeat_if_lower: bool, remind_after_hours: int,
                     now: datetime, dnd_active: bool = False
                     ) -> tuple[bool, bool]:
    """Fire a Telegram alert when Epic undercuts Steam for one wishlist game.

    Reuses `evaluate_and_alert` with kind="epic" so the antispam + DND
    machinery (first_alert / repeat-if-lower / reminder) is shared, but in
    its OWN state-key namespace (`epic:appid:name`) — independent of the
    Steam sale alerts on the same game.

    Fires ONLY when Epic is **strictly cheaper** than Steam (epic < steam,
    epic > 0). Equal prices or a 0/no-price Epic offer are NOT a deal — we
    skip the send AND clear any stale antispam entry so a genuine future
    drop fires a fresh first-alert. No-op without both real prices + a
    matched Epic url, or without Telegram creds.

    Returns (state_dirty, did_alert).
    """
    from steam import GAME_HEADER_IMAGE_URL
    epic_price = game.get("epic_price")
    steam_price = game.get("price")
    epic_url = game.get("epic_url")
    if not (isinstance(epic_price, (int, float))
            and isinstance(steam_price, (int, float)) and epic_url):
        return False, False
    if not token or not chat_id:
        return False, False
    appid = game.get("appid")
    name = game.get("name") or str(appid)
    # Strictly cheaper only. Otherwise wipe any stale antispam so the next
    # real undercut alerts fresh (evaluate_and_alert's own reset path only
    # runs when we call it — and we deliberately don't, on equal/pricier).
    if not (epic_price > 0 and epic_price < steam_price):
        key = f"epic:{appid}:{name}"
        return (state.pop(key, None) is not None), False
    target_raw = (game.get("price_str") or f"{steam_price:.2f}").replace(
        ",", ".")
    info = {
        "name": name, "appid": appid,
        "display_name": name, "game_name": name, "market_hash_name": name,
        "image_url": GAME_HEADER_IMAGE_URL.format(appid=appid) if appid
        else "",
        "alert_url": epic_url,
        "button_text": t("tg.btn.open_epic"),
        "lowest_price": epic_price,
        "lowest_price_raw": (game.get("epic_price_str")
                             or f"{epic_price:.2f}").replace(",", "."),
        "target_price": steam_price,
        "target_raw": target_raw,
    }
    state_dirty, did_alert, _reset, _reason = evaluate_and_alert(
        kind="epic", info=info, state=state, token=token, chat_id=chat_id,
        template=t("tg.message.epic_cheaper"),
        repeat_if_lower=repeat_if_lower, remind_after_hours=remind_after_hours,
        now=now, dnd_active=dnd_active,
    )
    return state_dirty, did_alert


def evaluate_and_alert(*, kind: str, info: dict, state: dict,
                      token: str, chat_id: str, template: str,
                      repeat_if_lower: bool, remind_after_hours: int,
                      now: datetime, dnd_active: bool = False
                      ) -> tuple[bool, bool, bool, str]:
    """Decide whether to fire a Telegram alert for one polled card, and send it.

    Parameters:
        kind: "buy" or "sell" — picks the comparison rule.
            buy:  alert when lowest <= target  (we want to buy AT OR BELOW).
            sell: alert when lowest <  target  (someone undercut us strictly).
        info: a dict carrying at minimum: name, appid, market_hash_name,
            lowest_price (float), target_price (float), display_name,
            image_url, game_name. Same shape send_alert expects.
        state: the in-memory antispam state dict (mutated on send / reset).
        token, chat_id, template: Telegram config.
        repeat_if_lower: if True, re-alert when lowest dropped further than
            the last alerted price.
        remind_after_hours: re-alert after this many hours since last alert.
        now: naive UTC datetime — single source of "current time" so callers
            in the same loop iteration get consistent timestamps.

    Returns (state_dirty, did_alert, did_reset, reason):
        state_dirty: True if `state` was modified.
        did_alert: True if a Telegram message was actually sent (and state
            updated). False on skip OR send failure.
        did_reset: True if the antispam state was CLEARED because price
            climbed back above target. Caller should also wipe the matching
            cards' `status="alerted"` so they go back to plain zebra and
            the next dip triggers a fresh first_alert.
        reason: human-readable why we did/didn't alert (for logging).
    """
    # Internal id stays as raw market_hash_name (state-key needs it).
    # User-facing log lines go through pretty_name so the user sees
    # "Merrin" instead of "1774580-Merrin".
    from steam import pretty_name
    name = info.get("name", "?")
    pretty = pretty_name(info)
    appid = info.get("appid", "?")
    lowest = info.get("lowest_price")
    target = info.get("target_price")

    if lowest is None or target is None:
        return False, False, False, "no price or target"

    key = f"{kind}:{appid}:{name}"
    # buy + game share the inclusive rule (price AT the threshold is
    # already interesting: buy → "can buy at my price", game → "the
    # discount reached the historical low"). Sell stays strict — an
    # equal price doesn't undercut my listing.
    hit_target = (lowest < target) if kind == "sell" else (lowest <= target)
    if not hit_target:
        # Price is above target right now (for sell: lowest >= target,
        # because the rule is strict <). Two things to do:
        #   1) If we were holding an antispam entry from a previous
        #      alert, wipe it so the next dip fires a fresh "first_alert"
        #      instead of being silently blocked by repeat_if_lower.
        #   2) Always tell the caller `did_reset=True` so it can clear
        #      any stale `status="alerted"` badge — even if the antispam
        #      state was already empty (e.g. it got orphaned by a key
        #      migration, or the row was originally imported with status
        #      "alerted" set out-of-band).
        #
        # Earlier this used to gate `did_reset` on a real antispam pop,
        # which left "alerted" badges stuck on imported rows whose
        # state-key changed under them (the appid migration sweep was
        # the canonical example).
        state_dirty = state.pop(key, None) is not None
        if state_dirty:
            log.info(f"clearing antispam for {pretty!r}: lowest={lowest} "
                     f"climbed back above target={target}")
            reason = "rebounded above target → reset"
        else:
            reason = "above target"
        return state_dirty, False, True, reason

    entry = state.get(key, {})
    last_alerted_price = entry.get("last_alerted_price")
    last_alert_time_str = entry.get("last_alert_time")

    should_alert = False
    reason = ""
    if not last_alerted_price:
        should_alert = True
        reason = t("log.reason.first_alert")
    elif repeat_if_lower and lowest < last_alerted_price:
        should_alert = True
        reason = t("log.reason.price_dropped",
                   now=lowest, prev=last_alerted_price)
    elif last_alert_time_str:
        try:
            last_time = datetime.fromisoformat(last_alert_time_str)
            if now - last_time > timedelta(hours=remind_after_hours):
                # For games (and their Epic-cheaper pass) sales stay live for
                # ~a week with the SAME price — the reminder branch that's
                # useful for card prices (which move intraday) just spams the
                # user daily on an unchanged offer. Skip when nothing moved;
                # first_alert / repeat_if_lower still fire on genuine changes.
                if (kind in ("game", "epic")
                        and lowest == last_alerted_price):
                    log.info(t("log.reason.skip_reminder_same_price",
                               name=pretty, price=lowest))
                else:
                    should_alert = True
                    reason = t("log.reason.reminder",
                               hours=remind_after_hours)
        except ValueError:
            # Malformed timestamp — treat as "no last alert", let next
            # branch's plain "first_alert" handle it next call. Don't
            # alert this round so a bad state doesn't trigger a storm.
            pass

    if not should_alert:
        return False, False, False, "antispam"

    # «Не турбувати»: the alert IS due (passed antispam), but we're inside
    # the quiet window — suppress the send WITHOUT touching antispam state,
    # so the message goes out on the next poll after the window ends.
    if dnd_active:
        log.info(t("log.dnd_suppressed", name=pretty))
        return False, False, False, "do not disturb"

    log.info(t("log.sending_alert", name=pretty, reason=reason))
    # Lazy import — telegram.py loads requests + i18n, which is fine in
    # watch.py but in gui.pyw we want to keep the startup path lean.
    from telegram import send_alert
    # Hand the kind down to send_alert so the {operation} template variable
    # can resolve to "придбання" / "продаж". Don't mutate caller's dict.
    info_for_tg = {**info, "operation": kind}
    try:
        send_alert(token, chat_id, info_for_tg, template)
    except Exception as exc:
        log.error(t("log.alert_failed", name=pretty, err=str(exc)))
        return False, False, False, f"send failed: {exc}"

    state[key] = {
        "last_alerted_price": lowest,
        "last_alert_time": now.isoformat(),
    }
    log.info(t("log.alert_sent", name=pretty))
    return True, True, False, reason
