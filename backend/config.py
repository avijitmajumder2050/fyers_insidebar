"""
config.py — Central configuration for Fyers InsideBar Strategy.

Auth:  AWS SSM Parameter Store (ONLY source of token)
Data:  S3 (ONLY for trade data — CSV, journal)
"""

import boto3

# ─────────────────────────────────────────────────────────────
# AWS
# ─────────────────────────────────────────────────────────────
AWS_REGION = "ap-south-1"
S3_BUCKET  = "dhan-trading-data"
AVAILABLE_FUND_INR = 10000

# S3 paths — trade data only, NO auth data here
S3_INSIDEBAR_CSV = "uploads/fyer_insiderbar_brekout.csv"
S3_TRADE_JOURNAL = "uploads/fyers_trade_journal.csv"

# SSM parameter names
SSM_ACCESS_TOKEN   = "/fyers/ACCESS_TOKEN"   # SecureString — the ONLY token store
SSM_CLIENT_ID      = "/fyers/CLIENT_ID"
SSM_SECRET_KEY     = "/fyers/SECRET_KEY"
SSM_REDIRECT_URI   = "/fyers/REDIRECT_URI"
SSM_TOTP_KEY       = "/fyers/TOTP_KEY"
SSM_USERNAME       = "/fyers/FY_ID"
SSM_PIN            = "/fyers/PIN"
SSM_TELEGRAM_TOKEN = "/trading-bot/telegram/BOT_TOKEN"
SSM_TELEGRAM_CHAT  = "/trading-bot/telegram/CHAT_ID"
SSM_APP_ID    = "/fyers/APP_ID"      # ABCDE

# ─────────────────────────────────────────────────────────────
# MARKET REGIME FILTER
# ─────────────────────────────────────────────────────────────
MARKET_INDEX_SYMBOL    = "NSE:NIFTYMIDSML400-INDEX"
MARKET_MIN_CHANGE_PTS  = -50        # NIFTYMIDSML400 must be up >= +50 pts

# ─────────────────────────────────────────────────────────────
# STRATEGY CONSTANTS
# ─────────────────────────────────────────────────────────────
MIN_CANDIDATES    = 2              # need >= 2 rows for today to proceed
MAX_SL_PCT        = 2.0            # reject stock if actual SL% > 2%
ACCOUNT_RISK_INR  = 1000           # ₹ risked per trade
LEVERAGE          = 5              # intraday leverage multiplier

PRODUCT_TYPE      = "INTRADAY"
ORDER_TYPE_MARKET = 1              # Fyers: 1 = MARKET
ORDER_TYPE_STOP   = 3              # Fyers: 3 = STOP-MARKET (SL-M)
ORDER_SIDE_BUY    = 1
ORDER_SIDE_SELL   = -1
EXCHANGE_PREFIX   = "NSE"
SYMBOL_SUFFIX     = "-EQ"

# Trail levels: (r_multiple, book_fraction_of_remaining, move_sl_to_r_multiple)
# book_fraction=None → only move SL, no exit at this level
# move_sl_to=None    → no SL move (5R full exit)
TRAIL_LEVELS = [
    (1, None, 0),      # 1R hit → SL to breakeven (0 = entry)
    (2, 0.50, 1),      # 2R hit → book 50% of remaining, SL to 1R
    (3, 0.25, 2),      # 3R hit → book 25% of remaining, SL to 2R
    (4, 0.15, 3),      # 4R hit → book 15% of remaining, SL to 3R
    (5, 1.00, None),   # 5R hit → exit 100% remaining
]

# Trade journal statuses
STATUS_OPEN   = "OPEN"
STATUS_ACTIVE = "ACTIVE"
STATUS_CLOSED = "CLOSED"

# ─────────────────────────────────────────────────────────────
# SSM LOADER (cached, lazy)
# ─────────────────────────────────────────────────────────────
_ssm_client = boto3.client("ssm", region_name=AWS_REGION)
_ssm_cache: dict[str, str] = {}


def get_ssm(name: str) -> str:
    """Fetch a parameter from SSM with in-process caching."""
    if name not in _ssm_cache:
        _ssm_cache[name] = _ssm_client.get_parameter(
            Name=name,
            WithDecryption=True,
        )["Parameter"]["Value"]
    return _ssm_cache[name]


def put_ssm(name: str, value: str) -> None:
    """Overwrite a SecureString parameter in SSM and refresh local cache."""
    _ssm_client.put_parameter(
        Name=name,
        Value=value,
        Type="SecureString",
        Overwrite=True,
    )
    _ssm_cache[name] = value
