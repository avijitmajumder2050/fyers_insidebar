"""
trade_manager.py — Real-time trade manager: SL trail + partial exits.

Lifecycle:
  TradeState created → journal status = OPEN
  First LTP poll     → journal status = ACTIVE
  SL hit / 5R exit   → journal status = CLOSED

The journal is the single source of truth for trade state.
Every significant event updates S3 immediately so a restart
can reconcile and not re-enter.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from autologin import fyers
import s3_utils
import telegram_notifier as tg
from config import (
    TRAIL_LEVELS,
    ORDER_SIDE_SELL, PRODUCT_TYPE,
    ORDER_TYPE_MARKET, ORDER_TYPE_STOP,
    STATUS_ACTIVE, STATUS_CLOSED,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 5


# ─────────────────────────────────────────────────────────────
# TradeState
# ─────────────────────────────────────────────────────────────

@dataclass
class TradeState:
    symbol:         str          # "NSE:CEIGALL-EQ"
    display_symbol: str          # "CEIGALL"
    entry_price:    float
    sl_price:       float
    initial_qty:    int
    sl_order_id:    str

    # Derived / mutable — set in __post_init__
    remaining_qty: int   = field(init=False)
    current_sl:    float = field(init=False)
    r_value:       float = field(init=False)
    targets:       list  = field(default_factory=list)
    levels_hit:    set   = field(default_factory=set)
    partial_pnl:   float = 0.0
    status:        str   = "OPEN"
    exit_price:    float = 0.0
    rr_achieved:   str   = "0R"

    def __post_init__(self) -> None:
        self.remaining_qty = self.initial_qty
        self.current_sl    = self.sl_price
        self.r_value       = round(self.entry_price - self.sl_price, 2)
        self.targets       = [
            round(self.entry_price + i * self.r_value, 2)
            for i in range(1, 6)
        ]
        logger.info(
            "TradeState | entry=₹%.2f  sl=₹%.2f  R=₹%.2f  targets=%s",
            self.entry_price, self.sl_price, self.r_value, self.targets,
        )


# ─────────────────────────────────────────────────────────────
# Market data
# ─────────────────────────────────────────────────────────────

def _get_ltp(symbol: str) -> float:
    resp = fyers.quotes(data={"symbols": symbol})
    return float(resp["d"][0]["v"]["lp"])


# ─────────────────────────────────────────────────────────────
# Order helpers  (exported: _place_sl_order used by strategy_engine)
# ─────────────────────────────────────────────────────────────

def _cancel_order(order_id: str) -> None:
    fyers.cancel_order(data={"id": order_id})
    logger.info("Cancelled order %s", order_id)


def _place_sl_order(symbol: str, qty: int, sl_price: float) -> str:
    """Place SL-M SELL. Returns order_id."""
    resp = fyers.place_order(data={
        "symbol":       symbol,
        "qty":          qty,
        "type":         ORDER_TYPE_STOP,
        "side":         ORDER_SIDE_SELL,
        "productType":  PRODUCT_TYPE,
        "limitPrice":   0,
        "stopPrice":    sl_price,
        "validity":     "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    })
    oid = resp["id"]
    logger.info("SL order placed: %s qty=%d @ ₹%.2f  id=%s", symbol, qty, sl_price, oid)
    return oid


def _place_market_sell(symbol: str, qty: int) -> float:
    """Market SELL. Returns approximate fill price (LTP after order)."""
    fyers.place_order(data={
        "symbol":       symbol,
        "qty":          qty,
        "type":         ORDER_TYPE_MARKET,
        "side":         ORDER_SIDE_SELL,
        "productType":  PRODUCT_TYPE,
        "limitPrice":   0,
        "stopPrice":    0,
        "validity":     "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    })
    fill = _get_ltp(symbol)
    logger.info("Market sell: %s qty=%d ~₹%.2f", symbol, qty, fill)
    return fill


# ─────────────────────────────────────────────────────────────
# SL move
# ─────────────────────────────────────────────────────────────

def _move_sl(state: TradeState, new_sl: float, reason: str) -> None:
    try:
        _cancel_order(state.sl_order_id)
    except Exception as exc:
        logger.warning("Could not cancel SL order %s: %s", state.sl_order_id, exc)

    state.sl_order_id = _place_sl_order(state.symbol, state.remaining_qty, new_sl)
    state.current_sl  = new_sl
    logger.info("SL moved → ₹%.2f  (%s)", new_sl, reason)
    tg.notify_sl_update(state.display_symbol, new_sl, reason)


# ─────────────────────────────────────────────────────────────
# Partial exit
# ─────────────────────────────────────────────────────────────

def _partial_exit(state: TradeState, fraction: float, r_level: int) -> None:
    qty_to_sell = max(1, round(state.remaining_qty * fraction))
    qty_to_sell = min(qty_to_sell, state.remaining_qty)

    fill        = _place_market_sell(state.symbol, qty_to_sell)
    pnl_chunk   = (fill - state.entry_price) * qty_to_sell
    state.partial_pnl   += pnl_chunk
    state.remaining_qty -= qty_to_sell

    logger.info(
        "Partial exit %dR: sold=%d @ ₹%.2f  chunk_pnl=₹%.2f  remaining=%d",
        r_level, qty_to_sell, fill, pnl_chunk, state.remaining_qty,
    )
    tg.notify_partial_exit(
        state.display_symbol, fill, qty_to_sell, r_level, state.remaining_qty
    )

    # Update S3 with partial state
    s3_utils.update_trade(state.display_symbol, {
        "rr_achieved": state.rr_achieved,
        "status":      STATUS_ACTIVE,
    })

    # Resize open SL order to match remaining qty
    if state.remaining_qty > 0:
        try:
            _cancel_order(state.sl_order_id)
            state.sl_order_id = _place_sl_order(
                state.symbol, state.remaining_qty, state.current_sl
            )
        except Exception as exc:
            logger.error("Failed to resize SL after partial exit: %s", exc)


# ─────────────────────────────────────────────────────────────
# Final close
# ─────────────────────────────────────────────────────────────

def _close_trade(state: TradeState, exit_price: float, rr_tag: str, reason: str) -> None:
    total_pnl = round(
        state.partial_pnl + (exit_price - state.entry_price) * state.remaining_qty,
        2,
    )
    state.exit_price  = exit_price
    state.rr_achieved = rr_tag
    state.status      = STATUS_CLOSED

    logger.info(
        "CLOSED %s  exit=₹%.2f  pnl=₹%.2f  rr=%s  reason=%s",
        state.display_symbol, exit_price, total_pnl, rr_tag, reason,
    )
    tg.notify_full_exit(state.display_symbol, exit_price, total_pnl, rr_tag)
    tg.notify_daily_summary(state.display_symbol, total_pnl, rr_tag)

    s3_utils.close_trade(state.display_symbol, exit_price, total_pnl, rr_tag)


# ─────────────────────────────────────────────────────────────
# Main polling loop
# ─────────────────────────────────────────────────────────────

def run_trade_manager(state: TradeState) -> None:
    """
    Blocks until the trade is fully closed.
    Transitions journal: OPEN → ACTIVE (first poll) → CLOSED.
    """
    logger.info("Trade manager started: %s", state.display_symbol)
    first_poll = True

    while state.status == "OPEN":

        # ── Fetch LTP ─────────────────────────────────────────
        try:
            ltp = _get_ltp(state.symbol)
        except Exception as exc:
            logger.error("LTP error: %s — retry in %ds", exc, POLL_INTERVAL_SEC)
            time.sleep(POLL_INTERVAL_SEC)
            continue

        # ── Mark ACTIVE on first successful poll ──────────────
        if first_poll:
            s3_utils.update_trade(state.display_symbol, {"status": STATUS_ACTIVE})
            first_poll = False
            logger.info("Trade status → ACTIVE for %s", state.display_symbol)

        # ── SL hit check ──────────────────────────────────────
        if ltp <= state.current_sl:
            logger.info("SL hit: ltp=%.2f <= sl=%.2f", ltp, state.current_sl)
            tg.notify_sl_hit(state.display_symbol, state.current_sl)
            _close_trade(state, state.current_sl, state.rr_achieved or "0R", "SL hit")
            break

        # ── R-level target checks ─────────────────────────────
        for r_mult, book_frac, move_sl_to in TRAIL_LEVELS:
            if r_mult in state.levels_hit:
                continue
            if ltp < state.targets[r_mult - 1]:
                continue

            # Target reached
            state.levels_hit.add(r_mult)
            state.rr_achieved = f"{r_mult}R"
            logger.info("%dR reached: ltp=%.2f  target=%.2f", r_mult, ltp, state.targets[r_mult - 1])

            # 5R → full exit
            if r_mult == 5:
                exit_price = _place_market_sell(state.symbol, state.remaining_qty)
                _close_trade(state, exit_price, "5R", "5R target hit")
                return

            # Partial exit
            if book_frac is not None:
                _partial_exit(state, book_frac, r_mult)

            # Move SL
            if move_sl_to is not None:
                if move_sl_to == 0:
                    new_sl = state.entry_price
                    reason = "Breakeven (1R hit)"
                else:
                    new_sl = state.targets[move_sl_to - 1]
                    reason = f"Trailed to {move_sl_to}R"
                _move_sl(state, round(new_sl, 2), reason)

            # All qty exited via partials
            if state.remaining_qty <= 0:
                _close_trade(state, ltp, state.rr_achieved, "All qty exited via partials")
                return

        time.sleep(POLL_INTERVAL_SEC)

    logger.info("Trade manager exited for %s", state.display_symbol)
