# Kalshi Telegram Scanner + Paper Tracker (v2)

This version adds SQLite logging, a candidate detector, Telegram alerts, simulated paired entries, and a report. It **does not place orders** and does not claim a strategy is profitable.

## What the strategy screens for

For each open market, it checks whether the explicitly reported YES ask plus NO ask is below $1 by enough to clear a configurable reserve and minimum edge:

`estimated_edge = 1.00 - yes_ask - no_ask - FEE_SLIPPAGE_RESERVE`

This is only a candidate detector. It assumes both sides can be bought at the displayed asks, which may be false due to limited order-book depth, latency, or partial fills. The reserve is a user-configured placeholder, **not Kalshi's official fee calculation**. Check the current official fee schedule and contract rules before relying on any result. A candidate is not proof of executable arbitrage.

## Install / upgrade

Back up your current project first. From Kali:

```bash
cd ~/Downloads
unzip kalshi-telegram-bot-v2.zip
```

This creates `kalshi-telegram-bot-v2/`. Copy your existing `.env` into the new directory (do not overwrite it with `.env.example`):

```bash
cp ~/Downloads/kalshi-telegram-bot/.env ~/Downloads/kalshi-telegram-bot-v2/.env
cd ~/Downloads/kalshi-telegram-bot-v2
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python bot.py
```

If you want to keep the same folder name, stop the old bot and copy the v2 files into the existing project after backing up `bot.py` and `.env`. Never run two instances with the same Telegram token at once.

## Telegram commands

- `/start` — welcome message
- `/scan` — fetch markets, record signals, and show a snapshot
- `/on` — enable scheduled scanning and candidate alerts
- `/off` — pause scheduled scans/alerts
- `/status` — show configuration and safety mode
- `/signals` — show strongest recent candidate signals
- `/paper TICKER` — record one hypothetical YES+NO paired entry for a ticker that currently qualifies
- `/report` — report scans and estimated paper entries
- `/help` — help

`/paper` is manual simulated entry only. It never places orders. Each ticker can be recorded only once to avoid duplicate entries.

## Configuration

Edit `.env`:

- `PAPER_BANKROLL=40`: paper bankroll assumption
- `PAPER_MAX_COST=2`: maximum total simulated cost per paired entry
- `FEE_SLIPPAGE_RESERVE=0.03`: conservative placeholder reserve per pair; not official fees
- `MIN_NET_EDGE=0.02`: minimum estimated edge per paired contract after reserve
- `ALERT_COOLDOWN_SECONDS=21600`: suppress repeat alerts for a ticker for six hours
- `SCAN_INTERVAL_SECONDS=900`: scan every 15 minutes

The paper report shows *estimated edge*, not realized P&L. Since the simulated pair assumes both sides fill and one side pays $1 at settlement, it is an optimistic model. Do not use its totals as evidence of actual profits. Improve the simulator with historical order-book depth, executable size, actual fee calculations, and fill probabilities before any live trading.

## Security and operations

- Keep `.env` private and never commit it.
- The bot ignores commands from any chat ID other than `TELEGRAM_ALLOWED_CHAT_ID`.
- The scanner starts OFF on every restart.
- Live trading is not implemented; no Kalshi trading credentials are used.
- The machine must stay online and the process running for Telegram commands and scheduled scans to work.
# kalshi-bot
