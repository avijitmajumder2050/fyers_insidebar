"""
telegram_notifier.py — Non-blocking Telegram alerts.

Every send() call is fire-and-forget in a daemon thread.
Failures are logged but NEVER raise to the caller — trading
execution must never be delayed by a notification attempt.
"""

import logging
import threading

import requests

from config import get_ssm, SSM_TELEGRAM_TOKEN, SSM_TELEGRAM_CHAT

logger = logging.getLogger(__name__)

_TOKEN:   str | None = None
_CHAT_ID: str | None = None
_lock = threading.Lock()


def _init() -> None:
    global _TOKEN, _CHAT_ID
    if _TOKEN is None:
        with _lock:
            if _TOKEN is None:          # double-checked locking
                _TOKEN   = get_ssm(SSM_TELEGRAM_TOKEN)
                _CHAT_ID = get_ssm(SSM_TELEGRAM_CHAT)


def _do_send(message: str) -> None:
    """Runs in a daemon thread — never raises."""
    try:
        _init()
        requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            json={"chat_id": _CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        ).raise_for_status()
    except Exception as exc:
        logger.warning("Telegram send failed (non-fatal): %s", exc)


def send(message: str) -> None:
    """Fire-and-forget: dispatches to a daemon thread immediately."""
    t = threading.Thread(target=_do_send, args=(message,), daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────
# Typed alert helpers
# ─────────────────────────────────────────────────────────────

def notify_system_start() -> None:
    send("🚀 <b>InsideBar Strategy</b> — system started")


def notify_market_pass(index: str, change: float) -> None:
    send(f"✅ <b>Market filter PASSED</b>\n{index} Δ = +{change:.2f} pts")


def notify_market_fail(index: str, change: float) -> None:
    send(
        f"🚫 <b>Market filter FAILED — NO TRADE TODAY</b>\n"
        f"{index} Δ = {change:.2f} pts (need ≥ +50)"
    )


def notify_trade_entry(symbol: str, entry: float, sl: float, qty: int, sl_pct: float) -> None:
    send(
        f"🟢 <b>ENTRY</b> — {symbol}\n"
        f"Price  : ₹{entry:.2f}\n"
        f"SL     : ₹{sl:.2f}  ({sl_pct:.2f}%)\n"
        f"Qty    : {qty}"
    )


def notify_sl_update(symbol: str, new_sl: float, reason: str) -> None:
    send(f"🔄 <b>SL UPDATE</b> — {symbol}\nNew SL : ₹{new_sl:.2f}\nReason : {reason}")


def notify_partial_exit(symbol: str, price: float, qty_sold: int, r_level: int, remaining: int) -> None:
    send(
        f"🟡 <b>PARTIAL EXIT</b> — {symbol}\n"
        f"Price     : ₹{price:.2f}\n"
        f"Sold      : {qty_sold}\n"
        f"Remaining : {remaining}\n"
        f"Level     : {r_level}R"
    )


def notify_sl_hit(symbol: str, sl_price: float) -> None:
    send(f"🔴 <b>SL HIT</b> — {symbol}\nExited at ₹{sl_price:.2f}")


def notify_full_exit(symbol: str, price: float, pnl: float, rr: str) -> None:
    emoji = "✅" if pnl >= 0 else "🔴"
    send(
        f"{emoji} <b>FULL EXIT</b> — {symbol}\n"
        f"Price : ₹{price:.2f}\n"
        f"P&L   : ₹{pnl:.2f}\n"
        f"R:R   : {rr}"
    )


def notify_rejection(symbol: str, reason: str) -> None:
    send(f"❌ <b>REJECTED</b> — {symbol}\nReason : {reason}")


def notify_no_trade(reason: str) -> None:
    send(f"ℹ️ <b>NO TRADE TODAY</b>\n{reason}")


def notify_daily_summary(symbol: str, pnl: float, rr: str) -> None:
    emoji = "📈" if pnl >= 0 else "📉"
    send(
        f"{emoji} <b>Daily Summary</b>\n"
        f"Symbol : {symbol}\n"
        f"P&L    : ₹{pnl:.2f}\n"
        f"R:R    : {rr}"
    )
