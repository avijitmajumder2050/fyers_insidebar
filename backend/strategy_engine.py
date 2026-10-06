"""
strategy_engine.py — InsideBar Breakout Strategy Orchestrator.

Execution order:
  0.  Market regime filter  → NIFTYMIDSML400-INDEX change >= +50 pts  (HARD GATE)
  1.  Load today's candidates from S3 CSV  (>= 2 rows required)
  2.  One-trade-per-day global lock via S3 journal
  3.  Batch LTP fetch for all candidates in ONE API call
  4.  Rank by LIVE SL% ASC
  5.  For each candidate (cascade on failure):
        a. Reject if actual SL% > 2%
        b. Size position (live capital from Fyers funds API, leverage 5x)
        c. Place MARKET BUY INTRADAY
        d. Write OPEN record to S3 journal immediately after fill
        e. Hand off to trade_manager (software SL — NO exchange SL-M order)
  6.  Stop after first successful trade entry
"""

import logging
import math
from datetime import date
import time
import os
import re
import json

from autologin import fyers
import s3_utils
import telegram_notifier as tg
from config import (
    MARKET_INDEX_SYMBOL, MARKET_MIN_CHANGE_PTS,
    AVAILABLE_FUND_INR, LEVERAGE, MARGIN_SAFETY, ACCOUNT_RISK_INR,
    MAX_SL_PCT, MAX_CHASE_R, SKIP_CIRCUIT_BAND_PCT, PRODUCT_TYPE,
    ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT, ORDER_SIDE_BUY,
    EXCHANGE_PREFIX, SYMBOL_SUFFIX,
    STATUS_OPEN, STATUS_CLOSED,
)
from trade_manager import TradeState, run_trade_manager, SIGNAL_REENTRY, get_net_position_qty
from s3_log_handler import setup_logging


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Market data helpers
# ─────────────────────────────────────────────────────────────


START_FLAG_FILE = "/tmp/insidebar_started.flag"

def notify_start_once():
    if not os.path.exists(START_FLAG_FILE):
        tg.notify_system_start()
        open(START_FLAG_FILE, "w").close()

def _batch_quotes(symbols: list[str]) -> dict[str, dict]:
    """
    Single Fyers API call for multiple symbols.
    Returns { "NSE:SYMBOL-EQ": {"lp": float, "ch": float, "chp": float} }
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


def _circuit_band_pct(symbol: str) -> float | None:
    """
    NSE price band % from Fyers depth (upper/lower circuit are prev_close ± band).
    Returns None if unavailable.
    """
    try:
        resp = fyers.depth(data={"symbol": symbol, "ohlcv_flag": "1"})
        d = resp.get("d", {}).get(symbol, {})
        upper, lower = float(d["upper_ckt"]), float(d["lower_ckt"])
        return round((upper - lower) / (upper + lower) * 100, 1)
    except Exception as exc:
        logger.warning("Circuit band lookup failed for %s (%s).", symbol, exc)
        return None


def _get_available_capital() -> float:
    """
    Live available cash from Fyers funds API.
    Falls back to AVAILABLE_FUND_INR from config on any error.
    """
    try:
        resp = fyers.funds()
        for item in resp.get("fund_limit", []):
            if item.get("title") == "Available Balance":
                return float(item["equityAmount"])
    except Exception as exc:
        logger.warning("Funds API error (%s) — using config fallback ₹%.2f.", exc, AVAILABLE_FUND_INR)
    return AVAILABLE_FUND_INR


def _handle_reentry(today_trade, capital):
    raw = today_trade["symbol"]

    sym = to_fyers_symbol(raw)

    original_entry = float(today_trade["entry_price"])
    csv_sl         = float(today_trade["sl_price"])

    logger.info(
        "REENTRY MODE | %s | trigger > %.2f",
        raw,
        original_entry
    )

    while True:

        quote = _batch_quotes([sym])

        ltp = quote.get(sym, {}).get("lp")

        if ltp and ltp > original_entry:
            logger.info(
                "REENTRY BREAKOUT | %s | %.2f > %.2f",
                raw,
                ltp,
                original_entry
            )

            qty = _calc_qty(
                ltp,
                csv_sl,
                capital
            )

            order_id = _place_market_buy(
                sym,
                qty
            )

            fill = _await_fill(order_id)
            s3_utils.update_trade(
    raw,
    {
        "entry_price": fill,
        "sl_price": csv_sl,
        "qty": qty,
        "status": STATUS_OPEN,
        "rr_achieved": "0R",
        "exit_price": "",
        "pnl": ""
    }
)

            state = TradeState(
                symbol=sym,
                display_symbol=raw,
                entry_price=fill,
                sl_price=csv_sl,
                initial_qty=qty,
                is_reentry=True
            )

            return run_trade_manager(state)

        time.sleep(10)
# ─────────────────────────────────────────────────────────────
# Symbol conversion
# ─────────────────────────────────────────────────────────────

def to_fyers_symbol(raw: str) -> str:
    """CEIGALL → NSE:CEIGALL-EQ"""
    return f"{EXCHANGE_PREFIX}:{raw.strip().upper()}{SYMBOL_SUFFIX}"


# ─────────────────────────────────────────────────────────────
# Risk helpers
# ─────────────────────────────────────────────────────────────


def _calc_sl_pct(ltp: float, sl: float) -> float:
    """Actual SL% based on live LTP (not CSV entry price)."""
    return round(abs((ltp - sl) / ltp) * 100, 2)


def _calc_qty(ltp: float, sl: float, capital: float) -> int:
    risk_per_share = ltp - sl
    if risk_per_share <= 0:
        raise ValueError(f"LTP ₹{ltp} <= SL ₹{sl} — invalid candidate.")
    qty_by_risk  = math.floor(ACCOUNT_RISK_INR / risk_per_share)
    qty_by_funds = math.floor((capital * LEVERAGE * MARGIN_SAFETY) / ltp)
    return min(qty_by_risk, qty_by_funds)


# Symbols Fyers permanently rejected today (e.g. not in MIS basket) — skip, don't resend every minute
_blocked_today: dict[str, str] = {}
_blocked_day: date | None = None


def _blocked_reason(raw: str) -> str | None:
    global _blocked_day
    if _blocked_day != date.today():
        _blocked_today.clear()
        _blocked_day = date.today()
    return _blocked_today.get(raw)


def _place_buy_with_margin_retry(symbol: str, qty: int, limit_price: float) -> tuple[str, int]:
    """
    Place IOC LIMIT BUY; on 'Margin Shortfall' rejection, shrink qty to fit the
    margin Fyers reports as available and retry once. Returns (order_id, qty).
    """
    try:
        return _place_limit_buy_ioc(symbol, qty, limit_price), qty
    except RuntimeError as exc:
        m = re.search(r"Margin Shortfall:INR ([\d,.]+) Available:INR ([\d,.]+)", str(exc))
        if not m:
            raise
        shortfall, available = (float(x.replace(",", "")) for x in m.groups())
        new_qty = math.floor(qty * available / (available + shortfall) * 0.98)
        if new_qty <= 0 or new_qty >= qty:
            raise
        logger.warning(
            "Margin shortfall ₹%.2f on %s qty=%d — retrying with qty=%d",
            shortfall, symbol, qty, new_qty,
        )
        return _place_limit_buy_ioc(symbol, new_qty, limit_price), new_qty


# ─────────────────────────────────────────────────────────────
# Order helpers
# ─────────────────────────────────────────────────────────────
def _place_market_buy(symbol: str, qty: int) -> str:
    """Place MARKET BUY INTRADAY. Returns order_id."""

    payload = {
        "symbol": symbol,
        "qty": qty,
        "type": ORDER_TYPE_MARKET,
        "side": ORDER_SIDE_BUY,
        "productType": PRODUCT_TYPE,
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    }

    logger.info(
        "FYERS ORDER REQUEST:\n%s",
        json.dumps(payload, indent=2)
    )

    resp = fyers.place_order(data=payload)

    logger.info(
        "FYERS ORDER RESPONSE:\n%s",
        json.dumps(resp, indent=2)
    )

    if resp.get("s") != "ok":
        raise RuntimeError(f"Buy order rejected: {resp}")

    logger.info(
        "BUY placed: %s qty=%d id=%s",
        symbol,
        qty,
        resp["id"]
    )

    return resp["id"]

def _place_limit_buy_ioc(symbol: str, qty: int, limit_price: float) -> str:
    """
    LIMIT BUY INTRADAY with IOC validity: fills immediately at <= limit_price,
    any unfilled part is cancelled by the exchange — the fill can never be
    above the breakout window. Returns order_id.
    """
    payload = {
        "symbol": symbol,
        "qty": qty,
        "type": ORDER_TYPE_LIMIT,
        "side": ORDER_SIDE_BUY,
        "productType": PRODUCT_TYPE,
        "limitPrice": limit_price,
        "stopPrice": 0,
        "validity": "IOC",
        "disclosedQty": 0,
        "offlineOrder": False,
    }
    logger.info("FYERS ORDER REQUEST:\n%s", json.dumps(payload, indent=2))
    resp = fyers.place_order(data=payload)
    logger.info("FYERS ORDER RESPONSE:\n%s", json.dumps(resp, indent=2))
    if resp.get("s") != "ok":
        raise RuntimeError(f"Buy order rejected: {resp}")
    logger.info("BUY placed: %s qty=%d limit=₹%.2f IOC id=%s", symbol, qty, limit_price, resp["id"])
    return resp["id"]


def _await_buy_fill(order_id: str) -> tuple[float, int]:
    """
    Poll orderbook up to 10 s for the IOC buy's final state.
    Returns (avg traded price, filled qty) — filled qty may be less than ordered.
    Raises if nothing filled.
    """
    for _ in range(20):
        for o in fyers.orderbook().get("orderBook", []):
            if o.get("id") != order_id:
                continue
            status = o.get("status")
            filled = int(o.get("filledQty") or 0)
            if status == 2:                                   # fully traded
                return float(o["tradedPrice"]), int(o.get("qty") or filled)
            if status in (1, 5):                              # cancelled (IOC remainder) / rejected
                if filled > 0:
                    return float(o["tradedPrice"]), filled
                raise RuntimeError(f"IOC buy {order_id} not filled: {o.get('message', '')}")
        time.sleep(0.5)
    raise TimeoutError(f"Order {order_id} final state not confirmed within 10 s.")


def _floor_tick(price: float, tick: float = 0.05) -> float:
    return round(math.floor(round(price / tick, 6)) * tick, 2)


def _ceil_tick(price: float, tick: float = 0.05) -> float:
    return round(math.ceil(round(price / tick, 6)) * tick, 2)


def _await_fill(order_id: str) -> float:
    """Poll orderbook up to 10 s for fill confirmation. Returns tradedPrice."""
    import time
    for _ in range(20):
        for o in fyers.orderbook().get("orderBook", []):
            if o["id"] == order_id and o["status"] == 2:
                return float(o["tradedPrice"])
        time.sleep(0.5)
    raise TimeoutError(f"Order {order_id} fill not confirmed within 10 s.")


# ─────────────────────────────────────────────────────────────
# Gate 0 — Market regime filter (NON-NEGOTIABLE)
# ─────────────────────────────────────────────────────────────

def _check_market_regime() -> bool:
    """
    HARD PRE-CONDITION before any trading logic.

    Fetch NIFTYMIDSML400-INDEX via Fyers quotes API.
    Extract 'ch' (point change from previous close).

    RULE:
      ch >= +50  → PASS  (market is sufficiently strong)
      ch <  +50  → FAIL  (no trade today, system stops)

    Returns True to continue, False to abort.
    """
    try:
        data   = _batch_quotes([MARKET_INDEX_SYMBOL])
        info   = data.get(MARKET_INDEX_SYMBOL)

        if not info:
            logger.error(
                "Market regime: no data returned for %s — blocking trade.",
                MARKET_INDEX_SYMBOL,
            )
            #tg.notify_no_trade(f"Market filter FAILED: no data for {MARKET_INDEX_SYMBOL}")
            return False

        ltp    = info["lp"]
        change = info["ch"]

        logger.info(
            "Market regime check: %s  LTP=%.2f  Δ=%.2f pts  (need >= +%.0f)",
            MARKET_INDEX_SYMBOL, ltp, change, MARKET_MIN_CHANGE_PTS,
        )

        if change >= MARKET_MIN_CHANGE_PTS:
            #tg.notify_market_pass(MARKET_INDEX_SYMBOL, change)
            return True

        # FAIL — log clearly and block all further execution
        logger.info(
            "Market regime FAILED: Δ=%.2f < %.0f — NO TRADE TODAY.",
            change, MARKET_MIN_CHANGE_PTS,
        )
        #tg.notify_market_fail(MARKET_INDEX_SYMBOL, change)
        return False

    except Exception as exc:
        logger.error("Market regime check threw: %s — blocking trade as safe default.", exc)
        #tg.notify_no_trade(f"Market filter error: {exc}")
        return False


# ─────────────────────────────────────────────────────────────
# Main strategy orchestrator
# ─────────────────────────────────────────────────────────────

def run_strategy() -> None:
   
    logger.info("══════════════════════════════════════════════")
    logger.info("  InsideBar Breakout Strategy — session start  ")
    logger.info("══════════════════════════════════════════════")
    
    

    # ─────────────────────────────────────────────────────────
    # Gate 0: Market regime filter
    # NIFTYMIDSML400-INDEX must be up >= +50 pts.
    # Any failure here → hard stop, no trade, no further checks.
    # ─────────────────────────────────────────────────────────
    if not _check_market_regime():
        return

    # ─────────────────────────────────────────────────────────
    # Gate 1: Daily candidate count
    # Requires >= 2 rows for today in insidebar CSV.
    # ─────────────────────────────────────────────────────────
    logger.info("STEP-1 LOAD CANDIDATES")
    candidates = s3_utils.load_today_candidates()
    if candidates.empty:
        logger.info("Gate 1 FAILED: insufficient candidates today.")
        #tg.notify_no_trade("Fewer than 2 candidates in today's CSV.")
        return

    logger.info("Gate 1 PASSED: %d candidates loaded.", len(candidates))

    # ─────────────────────────────────────────────────────────
    # Gate 2: One-trade-per-day global lock
    # Check S3 journal for any record with today's date,
    # regardless of status (OPEN / ACTIVE / CLOSED).
    # ─────────────────────────────────────────────────────────
    logger.info("STEP-2 CANDIDATES LOADED")
    logger.info("STEP-2 TRADE JOURNAL CHECK")
    today_trade = s3_utils.get_today_trade()
    if today_trade:
        status = str(today_trade["status"]).upper()
        logger.info(
        "Today's trade found | Symbol=%s | Status=%s",
        today_trade["symbol"],
        status
    )
        # -----------------------------------
    # ACTIVE / OPEN → Resume management
    # -----------------------------------
        if status in ["OPEN", "ACTIVE"]:
             logger.info(
            "Active trade detected. Handing back to trade_manager."
        )
             # The journal can say ACTIVE while Fyers holds nothing (exit
             # rejected then broker auto-square-off, crash mid-exit, ...).
             # Resuming that would manage - and keep alive - a ghost trade.
             fy_sym  = to_fyers_symbol(today_trade["symbol"])
             net_qty = get_net_position_qty(fy_sym)
             if net_qty == 0:
                 logger.warning("Journal says %s but Fyers has no open position for %s — closing journal.",
                                status, today_trade["symbol"])
                 s3_utils.update_trade(today_trade["symbol"], {"status": STATUS_CLOSED})
                 tg.send(f"⚠️ {today_trade['symbol']} was {status} in journal but Fyers shows no position — marked CLOSED.")
                 return "DAY_FINISHED"
             state = TradeState(
            symbol=fy_sym,
            display_symbol=today_trade["symbol"],
            entry_price=float(today_trade["entry_price"]),
            sl_price=float(today_trade["sl_price"]),
            initial_qty=net_qty if net_qty else int(today_trade["qty"]),
        )
             run_trade_manager(state)
             logger.info(
            "Trade manager completed existing trade."
        )
             return
        
       
             
    # CLOSED → One trade already done
    # -----------------------------------
        elif status == "CLOSED":
            logger.info(
            "Trade already completed today. Stopping."
        )

            tg.notify_no_trade(
            "Trade already completed today."
        )

            return "DAY_FINISHED"
        
         # ── ADD THIS NEW CONDITION ──
        elif status == "SL_HIT":
            logger.info("Initial trade hit SL earlier. Resuming strategy to check for breakout re-entry.")
            # Let it drop past this gate to execute the re-entry polling block!
            #today_trade = None   # force fresh scan
            capital = _get_available_capital()
            return _handle_reentry(
        today_trade,
        capital
    )

        # EXIT_FAILED or anything unrecognised → today's trade is done
        # (needs manual attention); never fall through to a fresh entry.
        else:
            logger.info("Today's trade status is %s — no further trading today.", status)
            return "DAY_FINISHED"
             # -----------------------------------

    
   

    logger.info("Gate 2 PASSED: no trade recorded today.")

    # ─────────────────────────────────────────────────────────
    # Step 3: Batch LTP fetch — single API call for all symbols
    # ─────────────────────────────────────────────────────────
    
    logger.info("STEP-3 TRADE LOCK CHECK")
    fyers_symbols = [
        to_fyers_symbol(str(r["stock_name"]))
        for _, r in candidates.iterrows()
    ]
    try:
        quote_map = _batch_quotes(fyers_symbols)
    except Exception as exc:
        logger.exception("Batch quote fetch failed.")
        #tg.notify_no_trade(f"LTP batch fetch error: {exc}")
        return

    # ─────────────────────────────────────────────────────────
    # Step 4: Build ranked list sorted by LIVE SL% ASC
    # SL% is computed from live LTP, not CSV entry price.
    # ─────────────────────────────────────────────────────────
    logger.info("STEP-4 BUILD RANK CHECK")
    ranked = []
    for _, row in candidates.iterrows():
        raw = str(row["stock_name"]).strip()
        sym = to_fyers_symbol(raw)
        ltp = quote_map.get(sym, {}).get("lp")
        if not ltp:
            logger.warning("No LTP returned for %s — skipping.", raw)
            continue
        sl     = float(row["sl"])
        sl_pct = _calc_sl_pct(ltp, sl)
        ranked.append({
            "raw":    raw,
            "sym":    sym,
            "ltp":    ltp,
            "entry":  float(row["entry"]),
            "sl":     sl,
            "sl_pct": sl_pct,
        })

    ranked.sort(key=lambda x: x["sl_pct"])

    if not ranked:
        #tg.notify_no_trade("No live prices available for any candidate.")
        return

    logger.info("─── Candidate ranking (live SL%%) ───")
    for i, c in enumerate(ranked, 1):
        logger.info(
            "  %d. %-12s  LTP=₹%-8.2f  SL=₹%-8.2f  SL%%=%.2f",
            i, c["raw"], c["ltp"], c["sl"], c["sl_pct"],
        )

    # ─────────────────────────────────────────────────────────
    # Step 5: Fetch available capital once (live from Fyers)
    # ─────────────────────────────────────────────────────────
    logger.info("STEP-4 Fetch available capital once ")
    capital = _get_available_capital()
    logger.info("Available capital: ₹%.2f  Buying power (5x): ₹%.2f", capital, capital * LEVERAGE)

    # ─────────────────────────────────────────────────────────
    # Step 6: Cascade through ranked candidates
    # Stop at first successful entry. Skip on any failure.
    # ─────────────────────────────────────────────────────────
    for c in ranked:
        raw    = c["raw"]
        sym    = c["sym"]
        ltp    = c["ltp"]
        csv_sl = c["sl"]
        entry  = c["entry"]
        sl_pct = c["sl_pct"]

        logger.info("── Evaluating: %s | LTP=₹%.2f | SL=₹%.2f | SL%%=%.2f", raw, ltp, csv_sl, sl_pct)

        blocked = _blocked_reason(raw)
        if blocked:
            logger.info("SKIPPED %s — blocked earlier today: %s", raw, blocked)
            continue

        band = _circuit_band_pct(sym)
        if band is not None and band <= SKIP_CIRCUIT_BAND_PCT:
            _blocked_today[raw] = f"{band:.0f}% circuit stock"
            logger.info("REJECTED %s — %.0f%% circuit stock (skip <= %d%%)", raw, band, SKIP_CIRCUIT_BAND_PCT)
            continue

        # ── a. SL% filter ─────────────────────────────────────
        if sl_pct > MAX_SL_PCT:
            reason = f"Actual SL% {sl_pct:.2f}% exceeds max {MAX_SL_PCT}%"
            logger.info("REJECTED %s — %s", raw, reason)
            #tg.notify_rejection(raw, reason)
            continue

        # ── a0. STRUCTURE VALIDATION (CRITICAL FIX) ─────────────
        if csv_sl >= ltp:
            logger.warning(
            "REJECTED %s — INVALID LONG STRUCTURE | LTP=%.2f SL=%.2f (Case-3)",
            raw, ltp, csv_sl
        )
            continue

        # ── a1. Breakout window: entry <= LTP <= entry + MAX_CHASE_R × R ──
        # Not blocked for the day — next minute's scan re-checks (pullback / breakout).
        if csv_sl >= entry:
            logger.warning("REJECTED %s — CSV entry ₹%.2f not above SL ₹%.2f", raw, entry, csv_sl)
            continue
        window_top = entry + MAX_CHASE_R * (entry - csv_sl)
        if ltp < entry:
            logger.info("WAIT %s — LTP ₹%.2f below breakout entry ₹%.2f (no breakout yet / failed)", raw, ltp, entry)
            continue
        if ltp > window_top:
            logger.info(
                "WAIT %s — LTP ₹%.2f is %.2fR past entry ₹%.2f (max %.1fR = ₹%.2f), not chasing",
                raw, ltp, (ltp - entry) / (entry - csv_sl), entry, MAX_CHASE_R, window_top,
            )
            continue
        limit_price = _floor_tick(window_top)
        if limit_price < ltp:                     # tick rounding dropped below LTP
            limit_price = _ceil_tick(ltp)

        # ── b. Position sizing (worst-case fill = limit price) ─
        try:
            qty = _calc_qty(limit_price, csv_sl, capital)
        except ValueError as exc:
            #tg.notify_rejection(raw, str(exc))
            continue

        if qty <= 0:
            #tg.notify_rejection(raw, "Qty = 0 (insufficient capital or SL too wide).")
            continue

        risk_per_share = limit_price - csv_sl
        logger.info(
            "Sizing | qty=%d  entry=₹%.2f  limit=₹%.2f  R/share(max)=₹%.2f  total_risk(max)=₹%.2f  bp=₹%.2f",
            qty, entry, limit_price, risk_per_share, qty * risk_per_share, capital * LEVERAGE,
        )

        # ── c. IOC limit buy (capped at breakout window top) ──
        try:
            logger.info("Placing BUY: %s qty=%d limit=₹%.2f", sym, qty, limit_price)
            order_id, qty    = _place_buy_with_margin_retry(sym, qty, limit_price)
            entry_price, qty = _await_buy_fill(order_id)
        except Exception as exc:
            logger.error("BUY failed for %s: %s", sym, exc)
            if "Allowed Basket" in str(exc):
                _blocked_today[raw] = "not in Fyers MIS (intraday) basket"
            #tg.notify_rejection(raw, f"Order error: {exc}")
            continue

        logger.info("FILLED: %s  entry=₹%.2f  qty=%d", raw, entry_price, qty)
        #tg.notify_trade_entry(raw, entry_price, csv_sl, qty, sl_pct)

        # ── d. Write OPEN record to S3 journal ────────────────
        s3_utils.create_trade({
    "trade_date": date.today().isoformat(),
    "symbol": raw,
    "entry_price": entry_price,
    "sl_price": csv_sl,
    "qty": qty,
    "exit_price": "",
    "pnl": "",
    "rr_achieved": "0R",
    "status": STATUS_OPEN,
})

        # ── e. Hand off to trade manager ──────────────────────
        # SL is SOFTWARE-MANAGED inside trade_manager.
        # No exchange-side SL-M order is placed.
        # trade_manager returns SIGNAL_REENTRY if the initial
        # (untrailed) SL is hit on the first entry — we then
        # re-enter once with same symbol + same CSV SL.
        state = TradeState(
            symbol=sym,
            display_symbol=raw,
            entry_price=entry_price,
            sl_price=csv_sl,
            initial_qty=qty,
            is_reentry=False,
        )
        signal = run_trade_manager(state)

        # ── f. Re-entry (max 1 attempt) ────────────────────────
        if signal == SIGNAL_REENTRY:
            logger.info("Re-entry triggered for %s. Monitoring until LTP > original entry (₹%.2f)...", raw, entry_price)
            
            original_entry_trigger = entry_price
            reentry_triggered = False
            
            # Continuous polling loop for breakout validation
            while True:
                try:
                    reentry_quotes = _batch_quotes([sym])
                    reentry_ltp = reentry_quotes.get(sym, {}).get("lp")
                    
                    if not reentry_ltp:
                        logger.warning("Failed to receive live quote for re-entry validation. Retrying...")
                        time.sleep(10)
                        continue
                    
                    logger.info("Re-entry Track | %s | Live LTP: ₹%.2f | Re-entry Threshold: > ₹%.2f", raw, reentry_ltp, original_entry_trigger)
                    
                    # Validating the strategy condition: LTP > original entry price
                    if reentry_ltp > original_entry_trigger:
                        logger.info("🚀 Strategy condition met! LTP (₹%.2f) broken above original entry (₹%.2f)", reentry_ltp, original_entry_trigger)
                        reentry_triggered = True
                        break
                        
                except Exception as poll_exc:
                    logger.error("Error monitoring real-time quote for re-entry: %s", poll_exc)
                
                time.sleep(10)  # Sleep for 10 seconds between checks to conserve rate limits
            
            if reentry_triggered:
                try:
                    # Refresh quotes to perform immediate sizing parameters
                    final_quotes = _batch_quotes([sym])
                    final_ltp = final_quotes.get(sym, {}).get("lp") or reentry_ltp

                    reentry_sl_pct = _calc_sl_pct(final_ltp, csv_sl)
                    if reentry_sl_pct > MAX_SL_PCT:
                        raise ValueError(f"Re-entry SL% {reentry_sl_pct:.2f}% > max structural limit {MAX_SL_PCT}%")

                    reentry_qty = _calc_qty(final_ltp, csv_sl, capital)
                    if reentry_qty <= 0:
                        raise ValueError("Calculated re-entry quantity yielded 0. Insufficient funds.")

                    logger.info(
                        "Executing Re-entry: %s | LTP=₹%.2f | Initial SL=₹%.2f | SL%%=%.2f | Qty=%d",
                        raw, final_ltp, csv_sl, reentry_sl_pct, reentry_qty,
                    )
                    
                    tg.send(
                        f"🔁 <b>VALIDATED RE-ENTRY BREAKOUT</b> — {raw}\n"
                        f"Triggered LTP : ₹{final_ltp:.2f}\n"
                        f"Initial SL    : ₹{csv_sl:.2f} ({reentry_sl_pct:.2f}%)\n"
                        f"Qty           : {reentry_qty}"
                    )

                    reentry_order_id = _place_market_buy(sym, reentry_qty)
                    reentry_entry_price = _await_fill(reentry_order_id)

                    logger.info("RE-ENTRY FILLED SUCCESSFULLY: %s @ ₹%.2f qty=%d", raw, reentry_entry_price, reentry_qty)
                    tg.notify_trade_entry(raw, reentry_entry_price, csv_sl, reentry_qty, reentry_sl_pct)

                    # Create individual log entry suffix tracking for the secondary instance
                    s3_utils.update_trade({
                        "symbol":      raw ,
                        "entry_price": reentry_entry_price,
                        "sl_price":    csv_sl,
                        "qty":         reentry_qty,
                        "exit_price":  "",
                        "pnl":         "",
                        "rr_achieved": "0R",
                        "status":      STATUS_OPEN,
                    })

                    reentry_state = TradeState(
                        symbol=sym,
                        display_symbol=raw,
                        entry_price=reentry_entry_price,
                        sl_price=csv_sl,
                        initial_qty=reentry_qty,
                        is_reentry=True,   # Explicit flag safely locks engine from infinitely repeating
                    )
                    run_trade_manager(reentry_state)
                    logger.info("Re-entry lifecycle trade workflow completed for %s.", raw)

                except Exception as exc:
                    logger.error("Execution failed during setup of verified re-entry for %s: %s", raw, exc)
                    tg.send(f"⚠️ <b>RE-ENTRY EXECUTION FAILED</b> — {raw}\nReason: {exc}")

        logger.info("Session complete — trade closed for %s.", raw)
        return "TRADE_COMPLETED"

   

    # All candidates exhausted without a single entry
    logger.info("All candidates exhausted — no trade placed today.")
    #tg.notify_no_trade("All candidates rejected or orders failed.")



def run_strategy_forever():
    notify_start_once()

    while True:
        try:
            result = run_strategy()
            if result == SIGNAL_REENTRY:
                logger.info("Re-entry signal received — restarting immediately")
                continue

            if result in ["TRADE_COMPLETED", "DAY_FINISHED"]:
                logger.info(
                    "One-trade-per-day completed. Stopping."
                )
                time.sleep(1800)   # 30 minutes
                continue

        except Exception as e:
            logger.error("Strategy crashed: %s", e)

        time.sleep(60)
    logger.info("Exited while loop")
# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
     setup_logging()        # step 1 — S3 + console log handler ready
     run_strategy_forever()