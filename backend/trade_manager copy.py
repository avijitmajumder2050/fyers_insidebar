"""
trade_manager.py — Real-time trade manager for InsideBar strategy.

Responsibilities:
  • Poll LTP every N seconds
  • Detect R-level breaches
  • Execute partial exits at 2R / 3R / 4R
  • Modify SL orders (cancel old → place new SL-M)
  • Exit remaining position at 5R or SL hit
  • Write final record to trade journal

Runs in a blocking loop called from strategy_engine after entry fill.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from autologin import fyers
import s3_utils
import telegram_notifier as tg
from config import TRAIL_LEVELS, ORDER_SIDE_SELL, PRODUCT_TYPE, ORDER_TYPE_MARKET, ORDER_TYPE_STOP

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 5   # LTP poll frequency


# ──────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────

@dataclass
class TradeState:
    symbol: str            # e.g. "NSE:CEIGALL-EQ"
    display_symbol: str    # e.g. "CEIGALL"
    entry_price: float
    sl_price: float
    initial_qty: int
    sl_order_id: str       # ID of the active SL-M order

    remaining_qty: int = field(init=False)
    current_sl: float = field(init=False)
    r_value: float = field(init=False)
    targets: list[float] = field(default_factory=list)
    levels_hit: set[int] = field(default_factory=set)
    partial_pnl: float = 0.0
    status: str = "OPEN"   # OPEN | CLOSED
    exit_price: float = 0.0
    rr_achieved: str = "0R"

    def __post_init__(self):
        self.remaining_qty = self.initial_qty
        self.current_sl    = self.sl_price
        self.r_value       = self.entry_price - self.sl_price
        self.targets       = [
            round(self.entry_price + (i * self.r_value), 2)
            for i in range(1, 6)
        ]
        logger.info(
            "TradeState init: entry=%.2f  sl=%.2f  R=%.2f  targets=%s",
            self.entry_price, self.sl_price, self.r_value, self.targets,
        )


# ──────────────────────────────────────────────────────────────
# LTP helper
# ──────────────────────────────────────────────────────────────

def get_ltp(symbol: str) -> float:
    resp = fyers.quotes(data={"symbols": symbol})
    return float(resp["d"][0]["v"]["lp"])


# ──────────────────────────────────────────────────────────────
# Order helpers
# ──────────────────────────────────────────────────────────────

def _cancel_order(order_id: str) -> None:
    fyers.cancel_order(data={"id": order_id})
    logger.info("Cancelled order %s", order_id)


def _place_sl_order(symbol: str, qty: int, sl_price: float) -> str:
    """Place a STOP MARKET SELL order. Returns new order_id."""
    data = {
        "symbol":       symbol,
        "qty":          qty,
        "type":         int(ORDER_TYPE_STOP),
        "side":         ORDER_SIDE_SELL,
        "productType":  PRODUCT_TYPE,
        "limitPrice":   0,
        "stopPrice":    sl_price,
        "validity":     "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    }
    resp = fyers.place_order(data=data)
    oid = resp["id"]
    logger.info("SL order placed: %s @ ₹%.2f  id=%s", symbol, sl_price, oid)
    return oid


def _place_market_sell(symbol: str, qty: int) -> float:
    """Place MARKET SELL, return approximate fill price (LTP at time of call)."""
    data = {
        "symbol":       symbol,
        "qty":          qty,
        "type":         int(ORDER_TYPE_MARKET),
        "side":         ORDER_SIDE_SELL,
        "productType":  PRODUCT_TYPE,
        "limitPrice":   0,
        "stopPrice":    0,
        "validity":     "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    }
    fyers.place_order(data=data)
    ltp = get_ltp(symbol)
    logger.info("Market sell executed: %s  qty=%d  ~price=%.2f", symbol, qty, ltp)
    return ltp


# ──────────────────────────────────────────────────────────────
# SL modification
# ──────────────────────────────────────────────────────────────

def _move_sl(state: TradeState, new_sl: float, reason: str) -> None:
    """Cancel existing SL order and place a new one at new_sl."""
    try:
        _cancel_order(state.sl_order_id)
    except Exception as exc:
        logger.warning("Could not cancel old SL order %s: %s", state.sl_order_id, exc)

    state.sl_order_id = _place_sl_order(state.symbol, state.remaining_qty, new_sl)
    state.current_sl  = new_sl
    logger.info("SL moved to ₹%.2f (%s)", new_sl, reason)
    tg.notify_sl_update(state.display_symbol, new_sl, reason)


# ──────────────────────────────────────────────────────────────
# Partial exit
# ──────────────────────────────────────────────────────────────

def _partial_exit(state: TradeState, fraction: float, r_level: int) -> None:
    qty_to_sell = max(1, round(state.remaining_qty * fraction))
    # Don't sell more than remaining
    qty_to_sell = min(qty_to_sell, state.remaining_qty)

    fill_price = _place_market_sell(state.symbol, qty_to_sell)
    pnl_chunk  = (fill_price - state.entry_price) * qty_to_sell
    state.partial_pnl   += pnl_chunk
    state.remaining_qty -= qty_to_sell

    logger.info(
        "Partial exit at %dR: sold %d @ ₹%.2f  chunk_pnl=₹%.2f  remaining=%d",
        r_level, qty_to_sell, fill_price, pnl_chunk, state.remaining_qty,
    )
    tg.notify_partial_exit(state.display_symbol, fill_price, qty_to_sell, r_level)

    # Resize the open SL order to match remaining qty
    if state.remaining_qty > 0:
        try:
            _cancel_order(state.sl_order_id)
            state.sl_order_id = _place_sl_order(
                state.symbol, state.remaining_qty, state.current_sl
            )
        except Exception as exc:
            logger.error("Failed to resize SL order after partial exit: %s", exc)


# ──────────────────────────────────────────────────────────────
# Final close
# ──────────────────────────────────────────────────────────────

def _close_trade(state: TradeState, exit_price: float, rr_tag: str, reason: str) -> None:
    total_pnl = state.partial_pnl + (exit_price - state.entry_price) * state.remaining_qty
    state.exit_price   = exit_price
    state.rr_achieved  = rr_tag
    state.status       = "CLOSED"

    logger.info(
        "Trade CLOSED: %s  exit=₹%.2f  total_pnl=₹%.2f  rr=%s  reason=%s",
        state.display_symbol, exit_price, total_pnl, rr_tag, reason,
    )
    tg.notify_full_exit(state.display_symbol, exit_price, total_pnl, rr_tag)

    # Save to journal
    record = {
        "trade_date":  date.today().isoformat(),
        "symbol":      state.display_symbol,
        "entry_price": state.entry_price,
        "sl_price":    state.sl_price,
        "qty":         state.initial_qty,
        "exit_price":  exit_price,
        "pnl":         round(total_pnl, 2),
        "rr_achieved": rr_tag,
        "status":      "CLOSED",
    }
    s3_utils.save_trade(record)


# ──────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────

def run_trade_manager(state: TradeState) -> None:
    """
    Blocking loop. Polls LTP and manages the open position
    until the trade is closed (5R hit, SL hit, or error exit).
    """
    logger.info("Trade manager started for %s", state.display_symbol)

    while state.status == "OPEN":
        try:
            ltp = get_ltp(state.symbol)
        except Exception as exc:
            logger.error("LTP fetch error: %s — retrying in %ds", exc, POLL_INTERVAL_SEC)
            time.sleep(POLL_INTERVAL_SEC)
            continue

        # ── Check SL hit ──────────────────────────────────────
        if ltp <= state.current_sl:
            logger.info("SL hit: ltp=%.2f <= sl=%.2f", ltp, state.current_sl)
            exit_price = state.current_sl   # assume SL-M fills at SL price
            _close_trade(state, exit_price, state.rr_achieved or "0R", "SL hit")
            break

        # ── Check R-level targets ─────────────────────────────
        for level_cfg in TRAIL_LEVELS:
            r_mult, book_frac, move_sl_to = level_cfg
            if r_mult in state.levels_hit:
                continue
            target_price = state.targets[r_mult - 1]

            if ltp >= target_price:
                state.levels_hit.add(r_mult)
                logger.info("%dR reached: ltp=%.2f  target=%.2f", r_mult, ltp, target_price)
                state.rr_achieved = f"{r_mult}R"

                # 5R → full exit
                if r_mult == 5:
                    exit_price = _place_market_sell(state.symbol, state.remaining_qty)
                    _close_trade(state, exit_price, "5R", "5R target")
                    return

                # Partial exit if applicable
                if book_frac is not None:
                    _partial_exit(state, book_frac, r_mult)

                # Move SL
                if move_sl_to is not None:
                    if move_sl_to == 0:
                        new_sl = state.entry_price          # breakeven
                        reason = "Moved to breakeven (1R)"
                    else:
                        new_sl = state.targets[move_sl_to - 1]   # previous target
                        reason = f"Moved to {move_sl_to}R"
                    _move_sl(state, round(new_sl, 2), reason)

                # If nothing left to manage, close out
                if state.remaining_qty <= 0:
                    _close_trade(state, ltp, state.rr_achieved, "All qty exited via partials")
                    return

        time.sleep(POLL_INTERVAL_SEC)

    logger.info("Trade manager loop exited for %s", state.display_symbol)