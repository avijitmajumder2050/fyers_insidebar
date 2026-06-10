"""
s3_utils.py — S3 helpers for InsideBar strategy.

S3 stores TRADE DATA ONLY:
  - fyer_insiderbar_brekout.csv   (scanner output, read-only)
  - fyers_trade_journal.csv       (trade lifecycle state machine)

NO authentication data is written here.

Journal state machine:
  OPEN → ACTIVE → CLOSED
  Records are updated in-place — never duplicate-appended.
"""

import io
import logging
from datetime import date

import boto3
import pandas as pd

from config import (
    AWS_REGION, S3_BUCKET,
    S3_INSIDEBAR_CSV, S3_TRADE_JOURNAL,
    MIN_CANDIDATES, STATUS_OPEN, STATUS_ACTIVE, STATUS_CLOSED,
)

logger  = logging.getLogger(__name__)
_s3     = boto3.client("s3", region_name=AWS_REGION)

JOURNAL_COLUMNS = [
    "trade_date", "symbol", "entry_price", "sl_price",
    "qty", "exit_price", "pnl", "rr_achieved", "status",
]


# ─────────────────────────────────────────────────────────────
# Internal S3 I/O
# ─────────────────────────────────────────────────────────────

def _read_csv(key: str) -> pd.DataFrame:
    obj = _s3.get_object(Bucket=S3_BUCKET, Key=key)
    return pd.read_csv(io.BytesIO(obj["Body"].read()))


def _write_csv(df: pd.DataFrame, key: str) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    _s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.getvalue())


# ─────────────────────────────────────────────────────────────
# Candidates
# ─────────────────────────────────────────────────────────────

def load_today_candidates() -> pd.DataFrame:
    """
    Read insidebar CSV from S3, filter to today's rows, sort by sl_pct ASC.
    Returns an empty DataFrame if fewer than MIN_CANDIDATES rows found.
    """
    df = _read_csv(S3_INSIDEBAR_CSV)
    df.columns = df.columns.str.strip().str.lower()

    # CSV date format is  M/D/YYYY  e.g. "6/9/2026"
    today_str = date.today().strftime("%-m/%-d/%Y")
    df["trade_date"] = df["trade_date"].astype(str).str.strip()
    today_df = df[df["trade_date"] == today_str].copy()

    if len(today_df) < MIN_CANDIDATES:
        logger.info(
            "Only %d candidate(s) for today — need >= %d. Stopping.",
            len(today_df), MIN_CANDIDATES,
        )
        return pd.DataFrame()

    today_df.sort_values("sl_pct", ascending=True, inplace=True)
    today_df.reset_index(drop=True, inplace=True)
    logger.info("Loaded %d candidates (sorted by SL%%).", len(today_df))
    return today_df


# ─────────────────────────────────────────────────────────────
# Journal helpers
# ─────────────────────────────────────────────────────────────

def _load_journal() -> pd.DataFrame:
    """Load journal from S3; return empty frame if file doesn't exist yet."""
    try:
        df = _read_csv(S3_TRADE_JOURNAL)
        df.columns = df.columns.str.strip().str.lower()
        # Ensure all expected columns exist
        for col in JOURNAL_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[JOURNAL_COLUMNS]
    except _s3.exceptions.NoSuchKey:
        return pd.DataFrame(columns=JOURNAL_COLUMNS)
    except Exception as exc:
        logger.warning("Journal load failed (%s) — starting with empty journal.", exc)
        return pd.DataFrame(columns=JOURNAL_COLUMNS)


def has_trade_today() -> bool:
    """
    Returns True if any record with today's date exists regardless of status.
    Enforces the strict ONE-TRADE-PER-DAY rule.
    """
    journal = _load_journal()
    if journal.empty:
        return False
    today = date.today().isoformat()
    journal["trade_date"] = journal["trade_date"].astype(str).str.strip()
    active = journal[
        (journal["trade_date"] == today) &
        (journal["status"].isin([STATUS_OPEN, STATUS_ACTIVE, STATUS_CLOSED]))
    ]
    return not active.empty


def create_trade(record: dict) -> None:
    """
    Insert a new trade row.  Called ONCE at entry — status = OPEN.
    Raises if a trade for this symbol+date already exists.
    """
    journal = _load_journal()
    today   = date.today().isoformat()
    symbol  = record["symbol"]

    exists = (
        not journal.empty
        and any(
            (journal["trade_date"].astype(str) == today)
            & (journal["symbol"] == symbol)
        )
    )
    if exists:
        logger.warning("Duplicate trade detected for %s on %s — skipping insert.", symbol, today)
        return

    row     = {col: record.get(col, "") for col in JOURNAL_COLUMNS}
    updated = pd.concat([journal, pd.DataFrame([row])], ignore_index=True)
    _write_csv(updated, S3_TRADE_JOURNAL)
    logger.info("Journal: created OPEN record for %s.", symbol)


def update_trade(symbol: str, updates: dict) -> None:
    """
    Update an existing today's trade row in-place.
    Used for OPEN→ACTIVE transitions and partial exit tracking.
    """
    journal = _load_journal()
    today   = date.today().isoformat()
    mask    = (
        (journal["trade_date"].astype(str) == today)
        & (journal["symbol"] == symbol)
    )
    if not mask.any():
        logger.error("update_trade: no row found for %s on %s.", symbol, today)
        return
    for col, val in updates.items():
        journal.loc[mask, col] = val
    _write_csv(journal, S3_TRADE_JOURNAL)
    logger.info("Journal: updated %s → %s", symbol, updates)


def close_trade(symbol: str, exit_price: float, pnl: float, rr_achieved: str) -> None:
    """Transition a trade to CLOSED status — final state."""
    update_trade(symbol, {
        "exit_price":  exit_price,
        "pnl":         round(pnl, 2),
        "rr_achieved": rr_achieved,
        "status":      STATUS_CLOSED,
    })
    logger.info("Journal: closed %s  exit=₹%.2f  pnl=₹%.2f  rr=%s",
                symbol, exit_price, pnl, rr_achieved)
