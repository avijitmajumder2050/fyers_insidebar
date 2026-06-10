"""
telegram_notifier.py — Telegram notification helpers.
Token and chat ID are loaded from SSM at first use.
"""

import logging

import requests

from config import get_ssm, SSM_TELEGRAM_TOKEN, SSM_TELEGRAM_CHAT

logger = logging.getLogger(__name__)

_TOKEN: str | None = None
_CHAT_ID: str | None = None


def _init():
    global _TOKEN, _CHAT_ID
    if _TOKEN is None:
        _TOKEN   = get_ssm(SSM_TELEGRAM_TOKEN)
        _CHAT_ID = get_ssm(SSM_TELEGRAM_CHAT)


def send(message: str) -> None:
    """Send a plain-text message to the configured Telegram chat."""
    _init()
    url = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": _CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Telegram notification failed: %s", exc)


# ──────────────────────────────────────────────────────────────
# Pre-built message templates
# ──────────────────────────────────────────────────────────────

def notify_trade_entry(symbol: str, entry: float, sl: float, qty: int, sl_pct: float):
    send(
        f"🟢 <b>ENTRY</b> — {symbol}\n"
        f"Price : ₹{entry:.2f}\n"
        f"SL    : ₹{sl:.2f}  ({sl_pct:.2f}%)\n"
        f"Qty   : {qty}"
    )


def notify_sl_update(symbol: str, new_sl: float, reason: str):
    send(f"🔄 <b>SL UPDATE</b> — {symbol}\nNew SL : ₹{new_sl:.2f}\nReason : {reason}")


def notify_partial_exit(symbol: str, price: float, qty_sold: int, r_level: int):
    send(
        f"🟡 <b>PARTIAL EXIT</b> — {symbol}\n"
        f"Price : ₹{price:.2f}\n"
        f"Qty   : {qty_sold}\n"
        f"Level : {r_level}R reached"
    )


def notify_full_exit(symbol: str, price: float, pnl: float, rr: str):
    emoji = "✅" if pnl >= 0 else "🔴"
    send(
        f"{emoji} <b>FULL EXIT</b> — {symbol}\n"
        f"Price : ₹{price:.2f}\n"
        f"P&L   : ₹{pnl:.2f}\n"
        f"R:R   : {rr}"
    )


def notify_rejection(symbol: str, reason: str):
    send(f"❌ <b>REJECTED</b> — {symbol}\nReason: {reason}")


def notify_no_trade(reason: str):
    send(f"ℹ️ <b>NO TRADE TODAY</b>\n{reason}")