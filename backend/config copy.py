"""
config.py — Central configuration for Fyers InsideBar Strategy
All secrets loaded from AWS SSM Parameter Store.
"""

import boto3

# ─────────────────────────────────────────────
# AWS
# ─────────────────────────────────────────────
AWS_REGION = "ap-south-1"
S3_BUCKET   = "dhan-trading-data"

# S3 paths
S3_INSIDEBAR_CSV  = "uploads/fyer_insiderbar_brekout.csv"
S3_TRADE_JOURNAL  = "uploads/fyers_trade_journal.csv"
S3_ACCESS_TOKEN   = "uploads/fyers_access_token.txt"

# SSM parameter names
SSM_CLIENT_ID      = "/fyers/CLIENT_ID"
SSM_SECRET_KEY     = "/fyers/SECRET_KEY"
SSM_REDIRECT_URI   = "/fyers/REDIRECT_URI"
SSM_TOTP_KEY       = "/fyers/TOTP_KEY"
SSM_USERNAME       = "/fyers/USERNAME"
SSM_PIN            = "/fyers/PIN"
SSM_TELEGRAM_TOKEN = "/fyers/TELEGRAM_TOKEN"
SSM_TELEGRAM_CHAT  = "/fyers/TELEGRAM_CHAT_ID"

# ─────────────────────────────────────────────
# STRATEGY CONSTANTS
# ─────────────────────────────────────────────
MIN_STOCKS_TO_TRADE   = 2          # need >2 rows to proceed
MAX_SL_PCT            = 2.0        # reject stock if actual SL% > 2%
ACCOUNT_RISK_INR      = 1000       # ₹ risked per trade
AVAILABLE_FUND_INR    = 5000    # ₹ capital
LEVERAGE              = 5          # intraday leverage multiplier
PRODUCT_TYPE          = "INTRADAY"
ORDER_TYPE_MARKET     = "1"        # Fyers: 1 = MARKET
ORDER_TYPE_STOP       = "3"        # Fyers: 3 = STOP (SL-M)
ORDER_SIDE_BUY        = 1
ORDER_SIDE_SELL       = -1
EXCHANGE_PREFIX       = "NSE"
SYMBOL_SUFFIX         = "-EQ"

# Trail levels: (r_multiple, book_fraction, move_sl_to_r)
# book_fraction=None means "move SL only, no booking at this level"
TRAIL_LEVELS = [
    (1, None, 0),      # 1R → move SL to breakeven (0R)
    (2, 0.50, 1),      # 2R → book 50%, move SL to 1R
    (3, 0.25, 2),      # 3R → book 25%, move SL to 2R
    (4, 0.15, 3),      # 4R → book 15%, move SL to 3R
    (5, 1.00, None),   # 5R → exit remaining 100%
]

# ─────────────────────────────────────────────
# SSM LOADER
# ─────────────────────────────────────────────
_ssm = boto3.client("ssm", region_name=AWS_REGION)
_cache: dict = {}

def get_ssm(name: str) -> str:
    if name not in _cache:
        _cache[name] = _ssm.get_parameter(
            Name=name, WithDecryption=True
        )["Parameter"]["Value"]
    return _cache[name]