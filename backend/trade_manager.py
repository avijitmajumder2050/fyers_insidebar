"""
trade_manager.py — Software-managed SL + trail loop.

NO exchange-side SL-M orders are placed.
The trade manager owns the full position lifecycle:

  • Polls LTP every POLL_INTERVAL_SEC seconds
  • If ltp <= current_sl → MARKET SELL all remaining qty immediately
  • On R-level targets:
      1R → update current_sl to entry (breakeven)
      2R → market sell 50%, update current_sl to 1R price
      3R → market sell 25%, update current_sl to 2R price
      4R → market sell 15%, update current_sl to 3R price
      5R → market sell 100% remaining, close trade
  • Journal transitions: OPEN → ACTIVE (first poll) → CLOSED
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
    ORDER_SIDE_SELL, PRODUCT_TYPE, ORDER_TYPE_MARKET,
    STATUS_ACTIVE, STATUS_CLOSED,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 5   # seconds between LTP polls


# ─────────────────────────────────────────────────────────────
# Trade state — no sl_order_id (software SL, no exchange order)
# ─────────────────────────────────────────────────────────────

@dataclass
class TradeState:
    symbol:         str    # "NSE:CEIGALL-EQ"
    display_symbol: str    # "CEIGALL"
    entry_price:    float
    sl_price:       float
    initial_qty:    int
    # No sl_order_id — SL enforced entirely by this module via LTP watch

    remaining_qty: int   = field(init=False)
    current_sl:    float = field(init=False)   # trails upward as targets are hit
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
# Order execution  (market sell only — no SL-M)
# ─────────────────────────────────────────────────────────────

def _market_sell(symbol: str, qty: int, reason: str) -> float:
    """
    Place a MARKET SELL for `qty` shares.
    Returns the LTP after the order (approximate fill price).
    Retries once on transient API errors.
    """
    payload = {
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
    }
    for attempt in (1, 2):
        try:
            resp = fyers.place_order(data=payload)
            if resp.get("code") != 200:
                raise RuntimeError(f"Sell rejected: {resp}")
            fill = _get_ltp(symbol)
            logger.info(
                "SELL (%s): qty=%d ~₹%.2f  [attempt %d]",
                reason, qty, fill, attempt,
            )
            return fill
        except Exception as exc:
            logger.error("Sell attempt %d failed (%s): %s", attempt, reason, exc)
            if attempt == 2:
                raise
            time.sleep(1)


# ─────────────────────────────────────────────────────────────
# SL update (in-memory only — no exchange order to modify)
# ─────────────────────────────────────────────────────────────

def _update_sl(state: TradeState, new_sl: float, reason: str) -> None:
    """Move the software SL price upward. Logs and notifies."""
    old_sl = state.current_sl
    state.current_sl = new_sl
    logger.info("SL trailed: ₹%.2f → ₹%.2f  (%s)", old_sl, new_sl, reason)
    tg.notify_sl_update(state.display_symbol, new_sl, reason)


# ─────────────────────────────────────────────────────────────
# Partial exit
# ─────────────────────────────────────────────────────────────

def _partial_exit(state: TradeState, fraction: float, r_level: int) -> float:
    """
    Sell `fraction` of remaining_qty at market.
    Updates state and journal. Returns fill price.
    """
    qty_to_sell = max(1, round(state.remaining_qty * fraction))
    qty_to_sell = min(qty_to_sell, state.remaining_qty)   # safety cap

    fill       = _market_sell(state.symbol, qty_to_sell, f"{r_level}R partial")
    pnl_chunk  = (fill - state.entry_price) * qty_to_sell
    state.partial_pnl   += pnl_chunk
    state.remaining_qty -= qty_to_sell

    logger.info(
        "Partial exit %dR: sold=%d @ ₹%.2f  chunk_pnl=₹%.2f  remaining=%d",
        r_level, qty_to_sell, fill, pnl_chunk, state.remaining_qty,
    )
    tg.notify_partial_exit(
        state.display_symbol, fill, qty_to_sell, r_level, state.remaining_qty,
    )
    # Update journal to show latest rr_achieved + keep ACTIVE
    s3_utils.update_trade(state.display_symbol, {
        "rr_achieved": state.rr_achieved,
        "status":      STATUS_ACTIVE,
    })
    return fill


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
        "CLOSED %s | exit=₹%.2f  pnl=₹%.2f  rr=%s  reason=%s",
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

    SL is enforced by this loop — when ltp <= state.current_sl
    a MARKET SELL is placed immediately for ALL remaining qty.

    Trail logic (from config.TRAIL_LEVELS):
      1R → update current_sl to entry (breakeven), no sell
      2R → sell 50%, trail SL to 1R price
      3R → sell 25%, trail SL to 2R price
      4R → sell 15%, trail SL to 3R price
      5R → sell 100% remaining, close

    Journal transitions:
      first poll success → ACTIVE
      close              → CLOSED
    """
    logger.info("Trade manager started: %s", state.display_symbol)
    first_poll = True

    while state.status == "OPEN":

        # ── 1. Fetch LTP ──────────────────────────────────────
        try:
            ltp = _get_ltp(state.symbol)
        except Exception as exc:
            logger.error("LTP fetch error: %s — retry in %ds", exc, POLL_INTERVAL_SEC)
            time.sleep(POLL_INTERVAL_SEC)
            continue

        # ── 2. Mark ACTIVE on first successful poll ───────────
        if first_poll:
            s3_utils.update_trade(state.display_symbol, {"status": STATUS_ACTIVE})
            first_poll = False
            logger.info("Trade ACTIVE: %s  ltp=₹%.2f", state.display_symbol, ltp)

        # ── 3. Software SL check — highest priority ───────────
        if ltp <= state.current_sl:
            logger.info(
                "SL triggered: ltp=₹%.2f <= sl=₹%.2f  — selling %d qty at market",
                ltp, state.current_sl, state.remaining_qty,
            )
            tg.notify_sl_hit(state.display_symbol, state.current_sl)
            try:
                fill = _market_sell(
                    state.symbol, state.remaining_qty, "SL hit"
                )
            except Exception as exc:
                logger.critical("SL market sell FAILED: %s  — retrying next tick", exc)
                time.sleep(POLL_INTERVAL_SEC)
                continue   # keep trying — position is still open

            _close_trade(
                state,
                fill,
                state.rr_achieved or "0R",
                f"SL hit at ₹{state.current_sl:.2f}",
            )
            break

        # ── 4. R-level target checks ──────────────────────────
        for r_mult, book_frac, move_sl_to in TRAIL_LEVELS:
            if r_mult in state.levels_hit:
                continue
            if ltp < state.targets[r_mult - 1]:
                continue

            # Target reached
            state.levels_hit.add(r_mult)
            state.rr_achieved = f"{r_mult}R"
            logger.info(
                "%dR target reached: ltp=₹%.2f  target=₹%.2f",
                r_mult, ltp, state.targets[r_mult - 1],
            )

            # 5R → full exit
            if r_mult == 5:
                fill = _market_sell(state.symbol, state.remaining_qty, "5R target")
                _close_trade(state, fill, "5R", "5R target reached")
                return

            # Partial exit (2R / 3R / 4R)
            if book_frac is not None:
                _partial_exit(state, book_frac, r_mult)

            # Trail SL upward (in memory only)
            if move_sl_to is not None:
                if move_sl_to == 0:
                    new_sl = state.entry_price
                    reason = "Breakeven (1R hit)"
                else:
                    new_sl = state.targets[move_sl_to - 1]
                    reason = f"Trailed to {move_sl_to}R"
                _update_sl(state, round(new_sl, 2), reason)

            # All qty sold via partials
            if state.remaining_qty <= 0:
                _close_trade(
                    state, ltp, state.rr_achieved,
                    "All qty exited via partial exits",
                )
                return

        time.sleep(POLL_INTERVAL_SEC)

    logger.info("Trade manager exited: %s", state.display_symbol)