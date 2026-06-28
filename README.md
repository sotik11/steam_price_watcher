# Steam Price Watcher

Windows GUI watcher for Steam Market trading cards and Steam wishlist
games, with Epic Games Store price comparison and Telegram alerts.

Polls the prices on a schedule (Windows Task Scheduler) and pings a
Telegram chat when a card hits a target price, a wishlist game goes on
sale, or the same game becomes cheaper on the Epic Games Store.

## Features

- **Buy / Sell card lists** — set a target buy/sell price per card,
  Telegram alert fires on hit (separate antispam per side; per-card
  «🚫 don't check» / «🔇 don't notify» flags).
- **Wishlist games** — pulls your Steam wishlist, tracks current and
  historical-low prices (data via the public Steam store API +
  AugmentedSteam for the all-time low). Telegram alert on any sale,
  with a dedicated «🔥 МІНІМАЛЬНА ЦІНА» message at the historic low.
- **Epic Games Store comparison** — for every wishlist game, looks up
  the matching Epic offer (strict name match, coming-soon / no-price
  offers are skipped). The «Поточна Epic» column shows the price; the
  row turns gold and a 🔥 marks the cell when Epic undercuts Steam,
  with its own Telegram alert (independent antispam namespace).
- **Telegram alerts** — large image preview, HTML formatting, deep
  market URL on the inline button. Supports a «Не турбувати» (DND)
  quiet window.
- **History tab** — every purchase / sale is logged with totals
  (купівлі / продажі / баланс).
- **UI niceties** — ttkbootstrap themes (including a custom Claude
  theme), uk/en localization, font scaling, persistent column widths,
  Excel-style header double-click autofit, Ctrl+A / Delete / Enter
  shortcuts on tables.
- **Backup** — Export / Import a flat zip with config + all lists;
  the format is interchangeable with the installer's first-run import.

## Requirements

- Windows 10 / 11
- Python 3.13.x (the installer bundles 3.13.7)
- Telegram bot (token + chat id) — used for alerts only

## Quick start

1. Install via the bundled installer (`installer/dist/Steam Price
   Watcher <version>.exe` after building) OR run from sources:
   ```
   setup_env.bat        :: creates .venv, installs requirements
   gui.bat              :: launches the GUI
   ```
2. On first launch, fill in Telegram **bot token** + **chat id** in
   Settings (use [@BotFather](https://t.me/BotFather) to create a bot,
   [@userinfobot](https://t.me/userinfobot) to get your chat id). Pick
   country/currency.
3. Add cards to the buy/sell lists, set targets. Optionally enable
   «Моніторинг ігрових знижок» in Settings to track your wishlist.
4. Use **Планувальник** tab to register the Windows Task Scheduler
   entry that polls prices in the background.

## Building the installer

```
cd installer
build_installer.bat      :: downloads bundled Python, runs Inno Setup
```
Output: `installer/dist/Steam Price Watcher <version>.exe`. Inno Setup
6 is required (`winget install JRSoftware.InnoSetup`).

## Project layout

```
steam_price_watcher/
  watch.py            # one-shot poll, run by Task Scheduler
  gui.pyw             # ttkbootstrap GUI
  steam.py            # Steam Market / Store API helpers
  epic.py             # Epic Games Store price lookup (GraphQL)
  alerts.py           # shared alert logic (antispam + DND)
  telegram.py         # Telegram Bot API helpers
  scheduler.py        # Windows Task Scheduler wrappers
  themes.py           # custom theme loader
  i18n.py             # uk/en localization
  regions.py          # Steam currency / country tables
  version.py          # APP_NAME + __version__
  lang/               # uk.json, en.json
  themes/             # claude.json (custom theme)
  assets/             # icons / images
  installer/          # Inno Setup config (.iss + build script)
```

## Configuration

`config.json` (gitignored) holds the bot token, chat id and per-user
settings. Use `config.example.json` as a starting template — the GUI
writes a real `config.json` on first save.

## License

Personal project, no license declared yet. Ask before reusing.
