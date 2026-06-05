"""Telegram Bot API helpers — fire-and-forget, no polling."""
import html
import requests

from i18n import t

_BASE = "https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, **payload) -> dict:
    url = _BASE.format(token=token, method=method)
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(t("log.tg_error", desc=data.get("description", data)))
    return data


def send_alert(token: str, chat_id: str, item: dict, template: str) -> None:
    """Send a price-alert message.

    Layout (matches the user's mock-up):
      * Large link preview rendered ABOVE the text
      * Card name in bold + underline
      * Custom user template body below
      * The Steam Market URL appended in a <blockquote>
      * One inline button "Open on the market" with the same URL

    Implementation notes:
      * `parse_mode="HTML"` enables <b>, <u>, <blockquote>.
      * `link_preview_options.show_above_text + prefer_large_media`
        replicates what Telegram lets users toggle manually on a message
        (the "Move up" / "Enlarge photo" preview menu).
      * `steam://openurl/...` deep links are not allowed in
        inline_keyboard.url (Telegram rejects non-http(s) schemes), so
        we don't put them in a button — clicking the browser URL opens
        the Steam client anyway when it's installed.
      * Every dynamic value passed into the template is HTML-escaped so
        a card name with `&` / `<` / `>` can't break the markup.
    """
    from steam import market_url, pretty_name

    # `?buy=1` on the listing URL only makes sense for BUY alerts —
    # Steam then opens the page with the buy dialog already up, so the
    # user goes click → confirm instead of click → click buy → confirm.
    # For SELL alerts the user is reviewing their own listing / the
    # market, not buying anything, so the plain URL is right.
    browser_url = market_url(item["appid"], item["market_hash_name"])
    if item.get("operation") == "buy":
        browser_url += "?buy=1"

    # Display fields. Centralised in pretty_name() — never the raw mhn.
    display_name = pretty_name(item)
    game_name = item.get("game_name") or ""
    price = item.get("lowest_price_raw") or str(item.get("lowest_price", "?"))
    target = item.get("target_price", "?")
    volume = item.get("volume") or "—"
    # `operation` comes from alerts.py — "buy" or "sell". Telegram has its
    # own i18n keys (tg.operation.*) — separate from History's operation.*
    # because the alert wants short, punchy nouns ("покупка" / "продаж")
    # while History uses the verbal form ("придбання"). UPPER-case so it
    # visually pops on a phone notification.
    op_key = item.get("operation") or "buy"
    operation_label = t(f"tg.operation.{op_key}")
    if operation_label == f"tg.operation.{op_key}":  # i18n miss
        operation_label = op_key
    operation_label = operation_label.upper()

    safe = {
        # `{name}` is kept for backwards compat with older user templates,
        # but it now resolves to the *clean* display name, not the raw
        # market_hash_name — that ugly "238960-…" prefix shouldn't ever
        # surface in a finished alert.
        "name":         html.escape(str(display_name)),
        "display_name": html.escape(str(display_name)),
        "game":         html.escape(str(game_name)),
        "price":        html.escape(str(price)),
        "target":       html.escape(str(target)),
        "volume":       html.escape(str(volume)),
        "url":          html.escape(browser_url),
        "operation":    html.escape(str(operation_label)),
    }

    try:
        body = template.format(**safe)
    except (KeyError, IndexError, ValueError):
        # Fall back to the language default if the user's custom template
        # has an unknown placeholder or stray brace — better than crashing
        # the whole watch.py run.
        body = t("tg.message.default").format(**safe)

    # With the poster image above and the "Open in market" button below,
    # the URL doesn't need to live in the caption too — the button covers
    # both clicking and long-press-copy. Keeping the caption clean.
    text = body

    keyboard = {
        "inline_keyboard": [[
            {"text": t("tg.btn.open_market"), "url": browser_url},
        ]]
    }

    image_url = item.get("image_url")
    if image_url:
        # sendPhoto guarantees a large image regardless of what Telegram
        # infers from the listing page's og:image dimensions (the link
        # preview heuristic is inconsistent — wide card art comes out big,
        # square card art comes out as a thumbnail, even with
        # prefer_large_media set). Caption mirrors the link-preview text.
        _call(token, "sendPhoto",
              chat_id=chat_id,
              photo=image_url,
              caption=text,
              parse_mode="HTML",
              reply_markup=keyboard)
    else:
        # No cached image (older watchlist entry or fetch failed) — fall
        # back to the link-preview path. Better than nothing.
        link_preview = {
            "url": browser_url,
            "prefer_large_media": True,
            "show_above_text": True,
        }
        _call(token, "sendMessage",
              chat_id=chat_id,
              text=text,
              parse_mode="HTML",
              reply_markup=keyboard,
              link_preview_options=link_preview)


def send_test(token: str, chat_id: str) -> None:
    _call(token, "sendMessage", chat_id=chat_id, text=t("tg.test_message"))
