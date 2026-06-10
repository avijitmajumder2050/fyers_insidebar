"""
s3_utils.py — S3 helpers for InsideBar strategy.
Handles: CSV download, trade journal read/append, token read/write.
"""

import io
import logging
from datetime import date

import boto3
import pandas as pd

from config import AWS_REGION, S3_BUCKET, S3_INSIDEBAR_CSV, S3_TRADE_JOURNAL, S3_ACCESS_TOKEN

logger = logging.getLogger(__name__)
s3 = boto3.client("s3", region_name=AWS_REGION)


# ──────────────────────────────────────────────────────────────
# Generic helpers
# ──────────────────────────────────────────────────────────────

def _read_csv_from_s3(key: str) -> pd.DataFrame:
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return pd.read_csv(io.BytesIO(obj["Body"].read()))


def _write_csv_to_s3(df: pd.DataFrame, key: str) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.getvalue())


# ──────────────────────────────────────────────────────────────
# InsideBar breakout CSV
# ──────────────────────────────────────────────────────────────

def load_today_candidates() -> pd.DataFrame:
    """
    Read insidebar CSV, filter to today, sort by sl_pct ASC.
    Returns empty DataFrame if < MIN_STOCKS_TO_TRADE rows.
    """
    from config import MIN_STOCKS_TO_TRADE

    df = _read_csv_from_s3(S3_INSIDEBAR_CSV)
    df.columns = df.columns.str.strip().str.lower()

    today_str = date.today().strftime("%-m/%-d/%Y")   # matches '6/9/2026' format
    df["trade_date"] = df["trade_date"].astype(str).str.strip()
    today_df = df[df["trade_date"] == today_str].copy()

    if len(today_df) <= MIN_STOCKS_TO_TRADE - 1:
        logger.info("Only %d candidates today — below threshold. Exiting.", len(today_df))
        return pd.DataFrame()

    today_df.sort_values("sl_pct", ascending=True, inplace=True)
    today_df.reset_index(drop=True, inplace=True)
    logger.info("Loaded %d candidates for today (sorted by sl%%).", len(today_df))
    return today_df


# ──────────────────────────────────────────────────────────────
# Trade journal
# ──────────────────────────────────────────────────────────────

JOURNAL_COLUMNS = [
    "trade_date", "symbol", "entry_price", "sl_price",
    "qty", "exit_price", "pnl", "rr_achieved", "status",
]


def load_journal() -> pd.DataFrame:
    try:
        df = _read_csv_from_s3(S3_TRADE_JOURNAL)
        df.columns = df.columns.str.strip().str.lower()
        return df
    except s3.exceptions.NoSuchKey:
        return pd.DataFrame(columns=JOURNAL_COLUMNS)
    except Exception:
        return pd.DataFrame(columns=JOURNAL_COLUMNS)


def has_trade_today() -> bool:
    """Return True if an active/closed trade already exists for today."""
    journal = load_journal()
    if journal.empty:
        return False
    today = date.today().isoformat()
    journal["trade_date"] = journal["trade_date"].astype(str).str.strip()
    return any(journal["trade_date"] == today)


def save_trade(record: dict) -> None:
    """Append one trade record to the journal CSV on S3."""
    journal = load_journal()
    new_row = pd.DataFrame([record])
    updated = pd.concat([journal, new_row], ignore_index=True)
    _write_csv_to_s3(updated, S3_TRADE_JOURNAL)
    logger.info("Trade journal updated: %s", record)


def update_trade(symbol: str, updates: dict) -> None:
    """Update an existing today's trade row (e.g., on partial exit or close)."""
    journal = load_journal()
    today = date.today().isoformat()
    mask = (journal["trade_date"].astype(str) == today) & (journal["symbol"] == symbol)
    for col, val in updates.items():
        journal.loc[mask, col] = val
    _write_csv_to_s3(journal, S3_TRADE_JOURNAL)


# ──────────────────────────────────────────────────────────────
# Access token
# ──────────────────────────────────────────────────────────────

def read_token_from_s3() -> str | None:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=S3_ACCESS_TOKEN)
        return obj["Body"].read().decode("utf-8").strip()
    except Exception:
        return None


def write_token_to_s3(token: str) -> None:
    s3.put_object(Bucket=S3_BUCKET, Key=S3_ACCESS_TOKEN, Body=token.encode("utf-8"))
    logger.info("Access token saved to S3.")