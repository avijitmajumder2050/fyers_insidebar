"""
strategy_engine.py — InsideBar Breakout Strategy Orchestrator.

Full flow per the spec:
  1.  Load today's candidates from S3 CSV (exit if ≤ 2)
  2.  Check trade journal — skip if already traded today
  3.  Convert symbol to NSE:SYMBOL-EQ format
  4.  Fetch LTP, calculate real SL% — reject if > 2%
  5.  Size the position
  6.  Place MARKET BUY → get fill price
  7.  Place initial SL-M order
  8.  Hand off to trade_manager for trail logic
  9.  On failure, cascade to next ranked candidate
"""

import logging
import math

from autologin import fyers
import s3_utils
import telegram_notifier as tg
from config import (
    AVAILABLE_FUND_INR, LEVERAGE, ACCOUNT_RISK_INR,
    MAX_SL_PCT, PRODUCT_TYPE, ORDER_TYPE_MARKET, ORDER_TYPE_STOP,
    ORDER_SIDE_BUY, ORDER_SIDE_SELL,
    EXCHANGE_PREFIX, SYMBOL_SUFFIX,
)
from trade_manager import TradeState, run_trade_manager, _place_sl_order

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Symbol helpers
# ──────────────────────────────────────────────────────────────

def to_fyers_symbol(raw: str) -> str:
    """SMART → NSE:SMART-EQ"""
    return f"{EXCHANGE_PREFIX}:{raw.strip().upper()}{SYMBOL_SUFFIX}"


# ──────────────────────────────────────────────────────────────
# LTP + SL% validation
# ──────────────────────────────────────────────────────────────

def get_ltp(symbol: str) -> float:
    resp = fyers.quotes(data={"symbols": symbol})
    return float(resp["d"][0]["v"]["lp"])

def get_multiple_ltps(symbols: list[str]) -> dict:

    resp = fyers.quotes(
        data={
            "symbols": ",".join(symbols)
        }
    )

    ltp_map = {}

    for item in resp.get("d", []):

        if item.get("s") == "ok":

            ltp_map[item["n"]] = float(
                item["v"]["lp"]
            )

    return ltp_map

def calc_actual_sl_pct(ltp: float, sl_price: float) -> float:
    return round(((ltp - sl_price) / ltp) * 100, 2)


# ──────────────────────────────────────────────────────────────
# Position sizing
# ──────────────────────────────────────────────────────────────

def calculate_qty(ltp: float, sl_price: float) -> int:
    buying_power  = AVAILABLE_FUND_INR * LEVERAGE
    risk_per_share = ltp - sl_price

    if risk_per_share <= 0:
        raise ValueError(f"LTP ({ltp}) <= SL ({sl_price}) — invalid candidate.")

    qty_by_risk  = math.floor(ACCOUNT_RISK_INR / risk_per_share)
    qty_by_funds = math.floor(buying_power / ltp)
    return min(qty_by_risk, qty_by_funds)


# ──────────────────────────────────────────────────────────────
# Order placement
# ──────────────────────────────────────────────────────────────

def place_market_buy(symbol: str, qty: int) -> str:
    """Place MARKET BUY. Returns order_id."""
    data = {
        "symbol":       symbol,
        "qty":          qty,
        "type":         int(ORDER_TYPE_MARKET),
        "side":         ORDER_SIDE_BUY,
        "productType":  PRODUCT_TYPE,
        "limitPrice":   0,
        "stopPrice":    0,
        "validity":     "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    }
    resp = fyers.place_order(data=data)
    if resp.get("code") != 200:
        raise RuntimeError(f"Buy order rejected: {resp}")
    logger.info("BUY order placed: %s  qty=%d  id=%s", symbol, qty, resp["id"])
    return resp["id"]


def get_order_fill_price(order_id: str) -> float:
    """Poll order book until fill, return average traded price."""
    import time
    for _ in range(20):   # max 10 s wait
        orders = fyers.orderbook()
        for o in orders.get("orderBook", []):
            if o["id"] == order_id and o["status"] == 2:   # 2 = filled
                return float(o["tradedPrice"])
        time.sleep(0.5)
    # Fallback: use LTP if order status not confirmed in time
    raise TimeoutError(f"Order {order_id} fill not confirmed within timeout.")


# ──────────────────────────────────────────────────────────────
# Main strategy entry point
# ──────────────────────────────────────────────────────────────

def run_strategy() -> None:


    logger.info("════════════════════════════════════════════")
    logger.info("InsideBar Breakout Strategy — session start")
    logger.info("════════════════════════════════════════════")

    # ----------------------------------------------------------
    # Load candidates
    # ----------------------------------------------------------

    candidates = s3_utils.load_today_candidates()
    logger.info(
    "Loaded %d candidates from S3",
    len(candidates)
)

    if candidates.empty or len(candidates) < 3:

        logger.info(
            "Only %d candidates found",
            len(candidates)
        )

        tg.notify_no_trade(
            f"Only {len(candidates)} candidates available today."
        )

        return

    # ----------------------------------------------------------
    # One trade per day guard
    # ----------------------------------------------------------

    if s3_utils.has_trade_today():

        logger.info(
            "Trade already exists for today."
        )

        tg.notify_no_trade(
            "A trade was already taken today."
        )

        return

    # ----------------------------------------------------------
    # Batch quote fetch
    # ----------------------------------------------------------

    symbols = [
        to_fyers_symbol(
            str(row["stock_name"])
        )
        for _, row in candidates.iterrows()
    ]

    try:

        ltp_map = get_multiple_ltps(symbols)

    except Exception as exc:

        logger.exception(
            "Batch quote fetch failed"
        )

        tg.notify_no_trade(
            f"LTP fetch failed: {exc}"
        )

        return

    # ----------------------------------------------------------
    # Build ranked candidate list
    # ----------------------------------------------------------

    candidate_list = []

    for _, row in candidates.iterrows():

        raw_symbol = str(
        row["stock_name"]
        ).strip()

        fyers_symbol = to_fyers_symbol(
            raw_symbol
        )

        ltp = ltp_map.get(
            fyers_symbol
        )

        if not ltp:
            continue

        sl_pct = calc_actual_sl_pct(
            ltp,
            float(row["sl"])
        )

        candidate_list.append({
            "row": row,
            "ltp": ltp,
            "sl_pct": sl_pct
        })

    candidate_list.sort(
        key=lambda x: x["sl_pct"]
    )

    if not candidate_list:

        logger.info(
            "No valid symbols received from quote API."
        )

        tg.notify_no_trade(
            "Unable to fetch live prices for candidates."
        )

        return

    logger.info(
        "===== Candidate Ranking ====="
    )

    for idx, c in enumerate(
        candidate_list,
        start=1
    ):

        logger.info(
            "%d. %s | LTP=%.2f | SL%%=%.2f",
            idx,
            c["row"]["stock_name"],
            c["ltp"],
            c["sl_pct"]
        )

    # ----------------------------------------------------------
    # Evaluate candidates
    # ----------------------------------------------------------

    for candidate in candidate_list:

        row = candidate["row"]

        raw_symbol = str(
            row["stock_name"]
        ).strip()

        fyers_symbol = to_fyers_symbol(
            raw_symbol
        )

        csv_sl = float(
            row["sl"]
        )

        ltp = candidate["ltp"]

        actual_sl_pct = candidate["sl_pct"]

        logger.info(
            "─── Evaluating: %s (%s) ───",
            raw_symbol,
            fyers_symbol
        )

        logger.info(
            "%s | LTP=%.2f | SL=%.2f | SL%%=%.2f",
            raw_symbol,
            ltp,
            csv_sl,
            actual_sl_pct
        )

        # ------------------------------------------------------
        # SL Validation
        # ------------------------------------------------------

        if actual_sl_pct > MAX_SL_PCT:

            reason = (
                f"SL% {actual_sl_pct:.2f}% "
                f"exceeds max {MAX_SL_PCT}%"
            )

            logger.info(
                "Rejected %s — %s",
                raw_symbol,
                reason
            )

            tg.notify_rejection(
                raw_symbol,
                reason
            )

            continue

        # ------------------------------------------------------
        # Position sizing
        # ------------------------------------------------------

        try:

            qty = calculate_qty(
                ltp,
                csv_sl
            )
            risk_per_share = ltp - csv_sl

            logger.info(
            "Risk/share=₹%.2f | AccountRisk=₹%.2f | Qty=%d",
            risk_per_share,
            ACCOUNT_RISK_INR,
            qty
            )

        except Exception as exc:

            tg.notify_rejection(
                raw_symbol,
                str(exc)
            )

            continue

        if qty <= 0:

            tg.notify_rejection(
                raw_symbol,
                "Calculated qty is 0."
            )

            continue

        logger.info(
            "Position: qty=%d ltp=₹%.2f sl=₹%.2f",
            qty,
            ltp,
            csv_sl
        )

        # ------------------------------------------------------
        # Market Buy
        # ------------------------------------------------------

        try:

            buy_order_id = place_market_buy(
                fyers_symbol,
                qty
            )
            logger.info(
            "Placing BUY order | Symbol=%s Qty=%d",
            fyers_symbol,
            qty
        )

            entry_price = get_order_fill_price(
                buy_order_id
            )

        except Exception as exc:

            logger.error(
                "Buy order failed for %s: %s",
                fyers_symbol,
                exc
            )

            tg.notify_rejection(
                raw_symbol,
                f"Order failed: {exc}"
            )

            continue

        logger.info(
            "Filled: %s entry=₹%.2f qty=%d",
            raw_symbol,
            entry_price,
            qty
        )

        tg.notify_trade_entry(
            raw_symbol,
            entry_price,
            csv_sl,
            qty,
            actual_sl_pct
        )

        # ------------------------------------------------------
        # Save Trade Journal
        # ------------------------------------------------------

        s3_utils.save_trade({

            "trade_date":
                __import__("datetime")
                .date.today()
                .isoformat(),

            "symbol": raw_symbol,

            "entry_price": entry_price,

            "sl_price": csv_sl,

            "qty": qty,

            "exit_price": "",

            "pnl": "",

            "rr_achieved": "0R",

            "status": "OPEN",
        })

        # ------------------------------------------------------
        # Place Initial SL
        # ------------------------------------------------------

        sl_order_id = None

        try:

            sl_order_id = _place_sl_order(
                fyers_symbol,
                qty,
                csv_sl
            )

            logger.info(
                "SL order placed: %s",
                sl_order_id
            )

        except Exception as exc:

            logger.error(
                "Could not place SL order for %s: %s",
                fyers_symbol,
                exc
            )

            tg.send(
                f"⚠️ Entry filled for {raw_symbol} "
                f"but SL order failed. Manual action required."
            )

            continue

        # ------------------------------------------------------
        # Start Trade Manager
        # ------------------------------------------------------

        state = TradeState(
            symbol=fyers_symbol,
            display_symbol=raw_symbol,
            entry_price=entry_price,
            sl_price=csv_sl,
            initial_qty=qty,
            sl_order_id=sl_order_id,
        )

        run_trade_manager(
            state
        )

        logger.info(
            "Session complete. Trade managed and closed for %s",
            raw_symbol
        )

        return

    logger.info(
        "All candidates exhausted without a successful entry."
    )

    tg.notify_no_trade(
        "All candidates rejected or failed."
    )


# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_strategy()