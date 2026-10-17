import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Load .env relative to this script, including when launched from another directory.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
KALSHI_BASE_URL = os.getenv(
    "KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2"
).rstrip("/")
SCAN_INTERVAL_SECONDS = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "900")))
MARKETS_PER_SCAN = max(1, min(int(os.getenv("MARKETS_PER_SCAN", "100")), 1000))
PAPER_BANKROLL = max(1.0, float(os.getenv("PAPER_BANKROLL", "40")))
PAPER_MAX_COST = max(0.5, float(os.getenv("PAPER_MAX_COST", "2")))
# Conservative placeholder reserve for combined fees/slippage per YES+NO pair.
# This is a screening assumption, not Kalshi's actual fee calculation.
FEE_SLIPPAGE_RESERVE = max(0.0, float(os.getenv("FEE_SLIPPAGE_RESERVE", "0.03")))
MIN_NET_EDGE = max(0.0, float(os.getenv("MIN_NET_EDGE", "0.02")))
ALERT_COOLDOWN_SECONDS = max(0, int(os.getenv("ALERT_COOLDOWN_SECONDS", "21600")))
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "paper_trades.sqlite3")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("kalshi-telegram-bot")


def authorized(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and ALLOWED_CHAT_ID and str(chat.id) == ALLOWED_CHAT_ID)


async def send_text(update: Update, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text[:4000])


def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db_connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                title TEXT NOT NULL,
                yes_ask REAL NOT NULL,
                no_ask REAL NOT NULL,
                combined_cost REAL NOT NULL,
                reserve REAL NOT NULL,
                estimated_net_edge REAL NOT NULL,
                seen_at INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                yes_ask REAL NOT NULL,
                no_ask REAL NOT NULL,
                combined_cost REAL NOT NULL,
                reserve REAL NOT NULL,
                estimated_net_edge REAL NOT NULL,
                contracts INTEGER NOT NULL,
                total_cost REAL NOT NULL,
                estimated_net_profit REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at INTEGER NOT NULL,
                markets_seen INTEGER NOT NULL,
                candidates INTEGER NOT NULL,
                errors TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS alert_cooldowns (
                ticker TEXT PRIMARY KEY,
                last_alert_at INTEGER NOT NULL
            )
        """)


def as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


async def fetch_markets() -> list[dict[str, Any]]:
    """Fetch public open markets. This does not authenticate or place orders."""
    url = f"{KALSHI_BASE_URL}/markets"
    params = {"status": "open", "limit": MARKETS_PER_SCAN}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    markets = payload.get("markets", [])
    return markets if isinstance(markets, list) else []


def get_ask(market: dict[str, Any], side: str) -> float | None:
    # Only use explicitly reported asks. Do not infer an executable ask from a bid.
    value = as_float(market.get(f"{side}_ask_dollars"))
    if value is None:
        # Some API payloads expose cent-denominated integer fields instead.
        cents = as_float(market.get(f"{side}_ask"))
        if cents is not None:
            value = cents / 100.0
    if value is None or value <= 0 or value >= 1:
        return None
    return value


def market_candidates(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Screen for a theoretical YES+NO pair: both asks must be present and their
    combined cost must leave a configured reserve and minimum edge below $1.
    This is only a candidate signal. It assumes both legs can fill at displayed
    asks and does not account for queue position, depth, latency, or actual fees.
    """
    candidates = []
    for m in markets:
        yes_ask = get_ask(m, "yes")
        no_ask = get_ask(m, "no")
        if yes_ask is None or no_ask is None:
            continue
        combined = yes_ask + no_ask
        estimated_edge = 1.0 - combined - FEE_SLIPPAGE_RESERVE
        if estimated_edge < MIN_NET_EDGE:
            continue
        ticker = str(m.get("ticker") or "").strip()
        if not ticker:
            continue
        candidates.append({
            "ticker": ticker,
            "title": str(m.get("title") or ticker),
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "combined_cost": combined,
            "reserve": FEE_SLIPPAGE_RESERVE,
            "estimated_net_edge": estimated_edge,
        })
    return sorted(candidates, key=lambda x: x["estimated_net_edge"], reverse=True)


def record_scan(markets_seen: int, candidates: list[dict[str, Any]], error: str | None = None) -> None:
    with db_connect() as con:
        con.execute(
            "INSERT INTO scan_runs(scanned_at, markets_seen, candidates, errors) VALUES (?, ?, ?, ?)",
            (int(time.time()), markets_seen, len(candidates), error),
        )
        for c in candidates:
            con.execute(
                """INSERT INTO signals
                   (ticker,title,yes_ask,no_ask,combined_cost,reserve,estimated_net_edge,seen_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (c["ticker"], c["title"], c["yes_ask"], c["no_ask"],
                 c["combined_cost"], c["reserve"], c["estimated_net_edge"], int(time.time())),
            )


def should_alert(ticker: str) -> bool:
    now = int(time.time())
    with db_connect() as con:
        row = con.execute("SELECT last_alert_at FROM alert_cooldowns WHERE ticker=?", (ticker,)).fetchone()
        if row and now - int(row["last_alert_at"]) < ALERT_COOLDOWN_SECONDS:
            return False
        con.execute(
            """INSERT INTO alert_cooldowns(ticker,last_alert_at) VALUES(?,?)
               ON CONFLICT(ticker) DO UPDATE SET last_alert_at=excluded.last_alert_at""",
            (ticker, now),
        )
    return True


def paper_enter(candidate: dict[str, Any]) -> tuple[bool, str]:
    """
    Record one hypothetical paired entry per ticker. This is intentionally
    separate from the scan loop and never submits orders to Kalshi.
    The estimate assumes both legs fill and a $1 combined settlement payout.
    """
    combined = candidate["combined_cost"]
    estimated_edge = candidate["estimated_net_edge"]
    if combined <= 0 or estimated_edge <= 0:
        return False, "No positive estimated edge."
    contracts = int(min(PAPER_MAX_COST // combined, PAPER_BANKROLL // combined))
    if contracts < 1:
        return False, (
            f"One YES+NO pair costs about ${combined:.2f}, above the configured "
            f"paper position cap ${min(PAPER_MAX_COST, PAPER_BANKROLL):.2f}."
        )
    total_cost = contracts * combined
    estimated_profit = contracts * estimated_edge
    try:
        with db_connect() as con:
            con.execute(
                """INSERT INTO paper_trades
                   (ticker,title,yes_ask,no_ask,combined_cost,reserve,estimated_net_edge,
                    contracts,total_cost,estimated_net_profit,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate["ticker"], candidate["title"], candidate["yes_ask"],
                 candidate["no_ask"], combined, candidate["reserve"], estimated_edge,
                 contracts, total_cost, estimated_profit, "OPEN", int(time.time())),
            )
    except sqlite3.IntegrityError:
        return False, "A paper entry for this ticker is already recorded; skipping duplicate."
    return True, (
        f"Paper entry recorded for {candidate['ticker']}: {contracts} paired contract(s), "
        f"simulated cost ${total_cost:.2f}, estimated net edge ${estimated_profit:.2f}. "
        "This is an optimistic estimate, not realized profit."
    )


def latest_candidates(limit: int = 5) -> list[sqlite3.Row]:
    with db_connect() as con:
        return con.execute(
            """SELECT ticker,title,yes_ask,no_ask,combined_cost,reserve,estimated_net_edge,seen_at
               FROM signals ORDER BY estimated_net_edge DESC, seen_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def report_text() -> str:
    with db_connect() as con:
        run = con.execute(
            "SELECT scanned_at,markets_seen,candidates,errors FROM scan_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        sig_count = con.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
        paper = con.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(total_cost),0) AS cost,
                      COALESCE(SUM(estimated_net_profit),0) AS est
               FROM paper_trades"""
        ).fetchone()
    if not run:
        return "No scans recorded yet. Use /scan first."
    last_time = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(run["scanned_at"]))
    lines = [
        "Paper-trading report",
        f"Last scan: {last_time}",
        f"Markets scanned: {run['markets_seen']}",
        f"Candidate signals in last scan: {run['candidates']}",
        f"Signals recorded (all scans): {sig_count}",
        f"Paper entries: {paper['n']}",
        f"Estimated paired-entry cost: ${paper['cost']:.2f}",
        f"Estimated net edge if both legs fill: ${paper['est']:.2f}",
        "",
        "Important: estimated edge is not realized P&L. The model assumes both legs fill at displayed asks and uses a configurable fee/slippage reserve; actual fees, depth, and execution may differ.",
    ]
    if run["errors"]:
        lines.insert(2, f"Last scan error: {run['errors'][:200]}")
    return "\n".join(lines)


def market_summary(markets: list[dict[str, Any]], count: int = 5) -> str:
    if not markets:
        return "Kalshi returned no open markets. Try /scan again later."

    def volume(m: dict[str, Any]) -> float:
        return as_float(m.get("volume_fp", m.get("volume"))) or 0.0

    selected = sorted(markets, key=volume, reverse=True)[:count]
    lines = [
        f"Kalshi market snapshot — top {len(selected)} by reported volume",
        "Public market data only; not a trade recommendation.",
        "",
    ]
    for index, market in enumerate(selected, start=1):
        title = str(market.get("title") or market.get("ticker") or "Untitled market")
        ticker = str(market.get("ticker") or "unknown")
        yes_bid = as_float(market.get("yes_bid_dollars"))
        no_bid = as_float(market.get("no_bid_dollars"))
        yes_ask = get_ask(market, "yes")
        no_ask = get_ask(market, "no")
        vol = volume(market)
        price = lambda v: f"${v:.2f}" if v is not None else "n/a"
        lines.append(f"{index}. {title[:90]}")
        lines.append(f"   YES bid {price(yes_bid)} | YES ask {price(yes_ask)} | NO ask {price(no_ask)} | volume {vol:,.0f}")
        lines.append(f"   ticker: {ticker}")
    lines.append("\nCommands: /signals /report /paper TICKER /on /off /status /help")
    return "\n".join(lines)


async def run_scan(send_alerts: bool, bot=None) -> tuple[list[dict[str, Any]], str]:
    markets = await fetch_markets()
    candidates = market_candidates(markets)
    record_scan(len(markets), candidates)
    if send_alerts and bot is not None:
        for c in candidates[:5]:
            if should_alert(c["ticker"]):
                msg = (
                    "POTENTIAL PAPER SIGNAL — NOT A LIVE TRADE\n"
                    f"{c['title'][:160]}\nTicker: {c['ticker']}\n"
                    f"YES ask: ${c['yes_ask']:.2f} | NO ask: ${c['no_ask']:.2f}\n"
                    f"Combined cost: ${c['combined_cost']:.2f}\n"
                    f"Estimated edge after configured reserve: ${c['estimated_net_edge']:.3f} per pair\n\n"
                    "Assumes both asks are executable and both legs fill. Verify fees, order-book depth, and contract rules."
                )
                await bot.send_message(chat_id=int(ALLOWED_CHAT_ID), text=msg[:4000])
    return candidates, market_summary(markets)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await send_text(
        update,
        "Kalshi paper scanner connected.\n"
        "Commands: /scan, /on, /off, /status, /signals, /paper TICKER, /report, /help.\n"
        "Live trading is not implemented; this bot never submits Kalshi orders."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await send_text(
        update,
        "/scan — fetch market snapshot and evaluate candidate pairs\n"
        "/on — enable periodic scanning and candidate alerts\n"
        "/off — pause periodic scanning/alerts\n"
        "/status — show scanner state and safety mode\n"
        "/signals — show strongest recent candidates saved in SQLite\n"
        "/paper TICKER — record a hypothetical paired entry for a currently qualifying ticker\n"
        "/report — summary of scans and estimated paper entries\n"
        "/help — show commands\n\n"
        "Strategy: screening for YES ask + NO ask below $1 after a configurable reserve. "
        "This is a candidate detector, not proof of arbitrage. Both legs may not fill at displayed prices. "
        "No live orders are implemented."
    )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await send_text(update, "Fetching Kalshi markets and checking candidate paired prices…")
    try:
        candidates, summary = await run_scan(send_alerts=False)
        await send_text(update, summary)
        if candidates:
            await send_text(update, f"Found {len(candidates)} candidate(s). Use /signals to inspect them.")
        else:
            await send_text(update, "No candidates passed the current edge threshold. That's normal; don't lower it just to force trades.")
    except (httpx.HTTPError, ValueError) as exc:
        log.exception("Market scan failed")
        with db_connect() as con:
            con.execute(
                "INSERT INTO scan_runs(scanned_at,markets_seen,candidates,errors) VALUES(?,?,?,?)",
                (int(time.time()), 0, 0, f"{type(exc).__name__}: {exc}"),
            )
        await send_text(update, f"Market scan failed: {type(exc).__name__}. Check terminal logs and try again.")


async def turn_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    context.application.bot_data["scan_enabled"] = True
    await send_text(update, f"Periodic scans and candidate alerts are ON (every {SCAN_INTERVAL_SECONDS} seconds). Live trading remains disabled.")


async def turn_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    context.application.bot_data["scan_enabled"] = False
    await send_text(update, "Periodic scans and candidate alerts are OFF. Use /scan for a one-off scan.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    enabled = bool(context.application.bot_data.get("scan_enabled", False))
    await send_text(
        update,
        f"Scanner: {'ON' if enabled else 'OFF'}\n"
        "Mode: public market data + paper tracking\n"
        "Live trading: NOT IMPLEMENTED\n"
        f"Interval: {SCAN_INTERVAL_SECONDS}s\n"
        f"Paper bankroll setting: ${PAPER_BANKROLL:.2f}\n"
        f"Max simulated position cost: ${PAPER_MAX_COST:.2f}\n"
        f"Fee/slippage reserve per pair: ${FEE_SLIPPAGE_RESERVE:.3f}\n"
        f"Minimum estimated net edge per pair: ${MIN_NET_EDGE:.3f}"
    )


async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    rows = latest_candidates(5)
    if not rows:
        await send_text(update, "No candidate signals saved yet. Run /scan or /on.")
        return
    lines = ["Recent strongest candidate signals (not verified trades):"]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i}. {r['ticker']} — YES ${r['yes_ask']:.2f} + NO ${r['no_ask']:.2f} "
            f"= ${r['combined_cost']:.2f}; estimated edge ${r['estimated_net_edge']:.3f}\n"
            f"   {r['title'][:100]}"
        )
    await send_text(update, "\n\n".join(lines))


async def paper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if not context.args:
        await send_text(update, "Usage: /paper TICKER\nExample: /paper SOME-MARKET-TICKER\nThis only records a simulated entry; it does not place an order.")
        return
    ticker = " ".join(context.args).strip()
    await send_text(update, f"Checking current public prices for {ticker}…")
    try:
        markets = await fetch_markets()
        candidate = next((c for c in market_candidates(markets) if c["ticker"].casefold() == ticker.casefold()), None)
        if candidate is None:
            await send_text(update, "That ticker is not in the current scan results or does not pass the candidate threshold. Run /scan and /signals.")
            return
        ok, message = paper_enter(candidate)
        await send_text(update, message)
    except (httpx.HTTPError, ValueError) as exc:
        log.exception("Paper entry failed")
        await send_text(update, f"Paper entry failed: {type(exc).__name__}. Check terminal logs.")


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await send_text(update, report_text())


async def scheduled_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.application.bot_data.get("scan_enabled", False):
        return
    try:
        candidates, _ = await run_scan(send_alerts=True, bot=context.bot)
        log.info("Scheduled scan: %d markets, %d candidates", MARKETS_PER_SCAN, len(candidates))
    except Exception as exc:
        log.exception("Scheduled scan failed")
        with db_connect() as con:
            con.execute(
                "INSERT INTO scan_runs(scanned_at,markets_seen,candidates,errors) VALUES(?,?,?,?)",
                (int(time.time()), 0, 0, f"{type(exc).__name__}: {exc}"),
            )
        try:
            await context.bot.send_message(
                chat_id=int(ALLOWED_CHAT_ID),
                text=f"Scheduled scan failed ({type(exc).__name__}). Check the bot terminal logs.",
            )
        except Exception:
            log.exception("Could not send failure alert")


async def post_init(application: Application) -> None:
    init_db()
    application.bot_data["scan_enabled"] = False
    if application.job_queue is None:
        raise RuntimeError("JobQueue is missing. Install dependencies with: pip install -r requirements.txt")
    application.job_queue.run_repeating(
        scheduled_scan,
        interval=SCAN_INTERVAL_SECONDS,
        first=SCAN_INTERVAL_SECONDS,
        name="kalshi-market-scan",
    )
    log.info("Bot initialized; scanner starts OFF; live order placement is not implemented")


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit(f"Missing TELEGRAM_BOT_TOKEN in {BASE_DIR / '.env'}")
    if not ALLOWED_CHAT_ID or not ALLOWED_CHAT_ID.lstrip("-").isdigit():
        raise SystemExit("Missing/invalid TELEGRAM_ALLOWED_CHAT_ID. Set your numeric private chat ID in .env.")

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("on", turn_on))
    app.add_handler(CommandHandler("off", turn_off))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("signals", signals))
    app.add_handler(CommandHandler("paper", paper))
    app.add_handler(CommandHandler("report", report))
    log.info("Starting Telegram polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
