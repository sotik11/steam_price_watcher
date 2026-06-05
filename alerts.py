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
        # Price rebounded above target. If we were holding an antispam
        # entry from a previous alert, wipe it now — next drop should
        # fire a fresh "first_alert" instead of being silently blocked by
        # repeat_if_lower's strict-less comparison.
        if state.pop(key, None) is not None:
            log.info(f"clearing antispam for {pretty!r}: lowest={lowest} "
                     f"climbed back above target={target}")
            return True, False, True, "rebounded above target → reset"
        return False, False, False, "above target"

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
    try:
        send_alert(token, chat_id, info, template)
    except Exception as exc:
        log.error(t("log.alert_failed", name=pretty, err=str(exc)))
        return False, False, False, f"send failed: {exc}"

    state[key] = {
        "last_alerted_price": lowest,
        "last_alert_time": now.isoformat(),
    }
    log.info(t("log.alert_sent", name=pretty))
    return True, True, False, reason
