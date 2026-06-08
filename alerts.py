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


def evaluate_and_alert(*, kind: str, info: dict, state: dict,
                      token: str, chat_id: str, template: str,
                      repeat_if_lower: bool, remind_after_hours: int,
                      now: datetime) -> tuple[bool, bool, bool, str]:
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
    hit_target = (lowest <= target) if kind == "buy" else (lowest < target)
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
                should_alert = True
                reason = t("log.reason.reminder", hours=remind_after_hours)
        except ValueError:
            # Malformed timestamp — treat as "no last alert", let next
            # branch's plain "first_alert" handle it next call. Don't
            # alert this round so a bad state doesn't trigger a storm.
            pass

    if not should_alert:
        return False, False, False, "antispam"

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
