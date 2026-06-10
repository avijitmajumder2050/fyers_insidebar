"""
strategy_engine.py — InsideBar Breakout Strategy Orchestrator.

Execution order (per spec):
  0.  Market regime filter  (NIFTYMIDSML400 change >= +50 pts)
  1.  Load today's candidates from S3 (>= 2 rows required)
  2.  One-trade-per-day global lock (S3 journal)
  3.  Batch LTP fetch for all candidates
  4.  Rank by actual SL% ASC (computed from live LTP)
  5.  For each candidate:
        a. Reject if SL% > 2%
        b. Size position using live capital from Fyers funds API
        c. Place MARKET BUY (INTRADAY)
        d. Write OPEN record to S3 journal
        e. Place initial SL-M order
        f. Hand off to trade_manager
  6.  Stop after first successful trade entry
  7.  Cascade to next candidate on any failure
"""

import logging
import math
from datetime import date

from autologin import fyers
import s3_utils
import telegram_notifier as tg
from config import (
    MARKET_INDEX_SYMBOL, MARKET_MIN_CHANGE_PTS,
    AVAILABLE_FUND_INR, LEVERAGE, ACCOUNT_RISK_INR,
    MAX_SL_PCT, PRODUCT_TYPE,
    ORDER_TYPE_MARKET, ORDER_SIDE_BUY,
    EXCHANGE_PREFIX, SYMBOL_SUFFIX,
    STATUS_OPEN,
)
from trade_manager import TradeState, run_trade_manager, _place_sl_order

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Market data helpers
# ─────────────────────────────────────────────────────────────

def _batch_quotes(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch LTP + change for multiple symbols in ONE API call.
    Returns { "NSE:SYMBOL-EQ": {"lp": float, "ch": float, "chp": float}, … }
    """
    resp   = fyers.quotes(data={"symbols": ",".join(symbols)})
    result = {}
    for item in resp.get("d", []):
        if item.get("s") == "ok":
            v = item["v"]
            result[item["n"]] = {
                "lp":  float(v.get("lp",  0)),
                "ch":  float(v.get("ch",  0)),
                "chp": float(v.get("chp", 0)),
            }
    return result


def _get_available_capital() -> float:
    """
    Query Fyers funds API and return available cash (equity segment).
    Falls back to configured AVAILABLE_FUND_INR on any error.
    """
    try:
        resp = fyers.funds()
        # Fyers returns fund_limit list; look for 'Available Balance'
        for item in resp.get("fund_limit", []):
            if item.get("title") == "Available Balance":
                return float(item["equityAmount"])
    except Exception as exc:
        logger.warning("Could not fetch live funds (%s) — using configured value.", exc)
    return AVAILABLE_FUND_INR


# ─────────────────────────────────────────────────────────────
# Symbol conversion
# ─────────────────────────────────────────────────────────────

def to_fyers_symbol(raw: str) -> str:
    return f"{EXCHANGE_PREFIX}:{raw.strip().upper()}{SYMBOL_SUFFIX}"


# ─────────────────────────────────────────────────────────────
# Risk helpers
# ─────────────────────────────────────────────────────────────

def _calc_sl_pct(ltp: float, sl: float) -> float:
    return round(((ltp - sl) / ltp) * 100, 2)


def _calc_qty(ltp: float, sl: float, capital: float) -> int:
    risk_per_share = ltp - sl
    if risk_per_share <= 0:
        raise ValueError(f"LTP {ltp} <= SL {sl} — invalid.")
    qty_by_risk  = math.floor(ACCOUNT_RISK_INR / risk_per_share)
    qty_by_funds = math.floor((capital * LEVERAGE) / ltp)
    return min(qty_by_risk, qty_by_funds)


# ─────────────────────────────────────────────────────────────
# Order helpers
# ─────────────────────────────────────────────────────────────

def _place_market_buy(symbol: str, qty: int) -> str:
    """Returns order_id. Raises on rejection."""
    resp = fyers.place_order(data={
        "symbol":       symbol,
        "qty":          qty,
        "type":         ORDER_TYPE_MARKET,
        "side":         ORDER_SIDE_BUY,
        "productType":  PRODUCT_TYPE,
        "limitPrice":   0,
        "stopPrice":    0,
        "validity":     "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    })
    if resp.get("code") != 200:
        raise RuntimeError(f"Buy order rejected: {resp}")
    logger.info("BUY placed: %s qty=%d  id=%s", symbol, qty, resp["id"])
    return resp["id"]


def _await_fill(order_id: str) -> float:
    """Poll orderbook up to 10 s for a fill. Returns tradedPrice."""
    import time
    for _ in range(20):
        for o in fyers.orderbook().get("orderBook", []):
            if o["id"] == order_id and o["status"] == 2:
                return float(o["tradedPrice"])
        time.sleep(0.5)
    raise TimeoutError(f"Order {order_id} not filled within 10 s.")


# ─────────────────────────────────────────────────────────────
# Gate 0 — Market regime filter
# ─────────────────────────────────────────────────────────────

def _check_market_regime() -> bool:
    """
    HARD PRE-CONDITION.
    NIFTYMIDSML400-INDEX change must be >= +50 pts.
    Returns True (proceed) or False (stop).
    """
    try:
        data   = _batch_quotes([MARKET_INDEX_SYMBOL])
        info   = data.get(MARKET_INDEX_SYMBOL, {})
        change = info.get("ch", 0.0)
        logger.info("Market filter: %s Δ = %.2f pts", MARKET_INDEX_SYMBOL, change)
        if change >= MARKET_MIN_CHANGE_PTS:
            tg.notify_market_pass(MARKET_INDEX_SYMBOL, change)
            return True
        tg.notify_market_fail(MARKET_INDEX_SYMBOL, change)
        return False
    except Exception as exc:
        logger.error("Market regime check failed: %s — blocking trade.", exc)
        tg.notify_no_trade(f"Market index fetch error: {exc}")
        return False


# ─────────────────────────────────────────────────────────────
# Main strategy
# ─────────────────────────────────────────────────────────────

def run_strategy() -> None:
    logger.info("══════════════════════════════════════════════")
    logger.info("  InsideBar Breakout Strategy — session start  ")
    logger.info("══════════════════════════════════════════════")
    tg.notify_system_start()

    # ── Gate 0: Market regime ─────────────────────────────────
    if not _check_market_regime():
        logger.info("Market filter FAILED. No trade today.")
        return

    # ── Gate 1: Candidate check ───────────────────────────────
    candidates = s3_utils.load_today_candidates()
    if candidates.empty:
        tg.notify_no_trade("Fewer than 2 candidates in CSV today.")
        return

    # ── Gate 2: One-trade-per-day lock ────────────────────────
    if s3_utils.has_trade_today():
        logger.info("Trade already exists for today — stopping.")
        tg.notify_no_trade("A trade was already recorded today.")
        return

    # ── Step 3: Batch LTP fetch ───────────────────────────────
    fyers_symbols = [to_fyers_symbol(str(r["stock_name"])) for _, r in candidates.iterrows()]
    try:
        quote_map = _batch_quotes(fyers_symbols)
    except Exception as exc:
        logger.exception("Batch quote fetch failed.")
        tg.notify_no_trade(f"LTP batch fetch error: {exc}")
        return

    # ── Step 4: Build ranked list (live SL% ASC) ─────────────
    ranked = []
    for _, row in candidates.iterrows():
        raw    = str(row["stock_name"]).strip()
        sym    = to_fyers_symbol(raw)
        ltp    = quote_map.get(sym, {}).get("lp")
        if not ltp:
            logger.warning("No LTP for %s — skipping.", raw)
            continue
        sl_pct = _calc_sl_pct(ltp, float(row["sl"]))
        ranked.append({"raw": raw, "sym": sym, "ltp": ltp, "sl": float(row["sl"]), "sl_pct": sl_pct})

    ranked.sort(key=lambda x: x["sl_pct"])

    if not ranked:
        tg.notify_no_trade("No live prices available for any candidate.")
        return

    logger.info("─── Candidate ranking (live SL%%) ───")
    for i, c in enumerate(ranked, 1):
        logger.info("  %d. %-12s  LTP=₹%-8.2f  SL%%=%.2f", i, c["raw"], c["ltp"], c["sl_pct"])

    # ── Step 5: Fetch available capital once ──────────────────
    capital = _get_available_capital()
    logger.info("Available capital: ₹%.2f", capital)

    # ── Step 6: Cascade through candidates ───────────────────
    for c in ranked:
        raw    = c["raw"]
        sym    = c["sym"]
        ltp    = c["ltp"]
        csv_sl = c["sl"]
        sl_pct = c["sl_pct"]

        logger.info("─── Evaluating: %s | LTP=₹%.2f | SL%%=%.2f ───", raw, ltp, sl_pct)

        # SL filter
        if sl_pct > MAX_SL_PCT:
            reason = f"SL% {sl_pct:.2f}% > max {MAX_SL_PCT}%"
            logger.info("Rejected %s — %s", raw, reason)
            tg.notify_rejection(raw, reason)
            continue

        # Position sizing
        try:
            qty = _calc_qty(ltp, csv_sl, capital)
        except ValueError as exc:
            tg.notify_rejection(raw, str(exc))
            continue

        if qty <= 0:
            tg.notify_rejection(raw, "Qty = 0 (insufficient funds or SL too wide).")
            continue

        risk_per_share = ltp - csv_sl
        logger.info(
            "Sizing: qty=%d  R/share=₹%.2f  total_risk=₹%.2f  buying_power=₹%.2f",
            qty, risk_per_share, qty * risk_per_share, capital * LEVERAGE,
        )

        # Market buy
        try:
            logger.info("Placing BUY: %s qty=%d", sym, qty)
            order_id    = _place_market_buy(sym, qty)
            entry_price = _await_fill(order_id)
        except Exception as exc:
            logger.error("Buy failed for %s: %s", sym, exc)
            tg.notify_rejection(raw, f"Order error: {exc}")
            continue

        logger.info("FILLED: %s  entry=₹%.2f  qty=%d", raw, entry_price, qty)
        tg.notify_trade_entry(raw, entry_price, csv_sl, qty, sl_pct)

        # Write OPEN record to journal
        s3_utils.create_trade({
            "trade_date":  date.today().isoformat(),
            "symbol":      raw,
            "entry_price": entry_price,
            "sl_price":    csv_sl,
            "qty":         qty,
            "exit_price":  "",
            "pnl":         "",
            "rr_achieved": "0R",
            "status":      STATUS_OPEN,
        })

        # Place initial SL-M order
        sl_order_id = None
        try:
            sl_order_id = _place_sl_order(sym, qty, csv_sl)
            logger.info("SL order placed: id=%s", sl_order_id)
        except Exception as exc:
            logger.error("SL order failed for %s: %s", sym, exc)
            tg.send(
                f"⚠️ <b>CRITICAL</b> — Entry filled for {raw} "
                f"but SL order FAILED.\nManual intervention required!"
            )
            # Do not cascade — we're now live without SL
            # trade_manager will handle SL via LTP polling
            sl_order_id = "MANUAL"

        # Hand off to trade manager
        state = TradeState(
            symbol=sym,
            display_symbol=raw,
            entry_price=entry_price,
            sl_price=csv_sl,
            initial_qty=qty,
            sl_order_id=sl_order_id,
        )
        run_trade_manager(state)

        logger.info("Session complete — trade closed for %s.", raw)
        return   # ONE trade per day — done

    # All candidates exhausted
    logger.info("All candidates exhausted — no trade placed.")
    tg.notify_no_trade("All candidates rejected or failed.")


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_strategy()
