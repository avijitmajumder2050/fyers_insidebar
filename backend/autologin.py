"""
autologin.py — Fyers access token lifecycle manager.

Auth source:  AWS SSM Parameter Store ONLY
              Key: /fyers/ACCESS_TOKEN (SecureString)

Startup sequence:
  1. Read token from SSM
  2. Validate via get_profile()
  3. If expired → TOTP headless re-login
  4. Write fresh token back to SSM (overwrite)
  5. Return initialised FyersModel — cached as module singleton

All other modules import:  from autologin import fyers
No repeated logins occur during the trading session.
"""

import logging
import time

import pyotp
import requests
from fyers_apiv3 import fyersModel
from fyers_apiv3.fyersModel import SessionModel

from config import (
    get_ssm, put_ssm,
    SSM_ACCESS_TOKEN, SSM_CLIENT_ID,SSM_APP_ID, SSM_SECRET_KEY,
    SSM_REDIRECT_URI, SSM_TOTP_KEY, SSM_USERNAME, SSM_PIN,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

def _build_fyers(client_id: str, token: str) -> fyersModel.FyersModel:
    return fyersModel.FyersModel(
        client_id=client_id,
        token=token,
        log_path="",
        is_async=False,
    )


def _is_valid(instance: fyersModel.FyersModel) -> bool:
    try:
        resp = instance.get_profile()
        return resp.get("code") == 200
    except Exception as exc:
        logger.warning("Token validation failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────
# TOTP headless login
# ─────────────────────────────────────────────────────────────

def _generate_fresh_token(client_id: str, secret_key: str, redirect_uri: str) -> str:
    """
    Fully headless TOTP login using Fyers v3 vagator API.
    Returns a new access_token string.
    Writes it to SSM before returning.
    """
    username = get_ssm(SSM_USERNAME)
    pin      = get_ssm(SSM_PIN)
    totp_key = get_ssm(SSM_TOTP_KEY)
    app_id = get_ssm(SSM_APP_ID)
    

    totp = pyotp.TOTP(totp_key)

    session = SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )

    logger.info("Starting TOTP login flow …")

    # ── Step 1: Send OTP ──────────────────────────────────────
    r1 = requests.post(
        "https://api-t2.fyers.in/vagator/v2/send_login_otp",
        json={"fy_id": username, "app_id": app_id},
        timeout=15,
    )
    r1.raise_for_status()
    request_key = r1.json()["request_key"]

    time.sleep(2)   # give TOTP clock a fresh window

    # ── Step 2: Verify OTP ────────────────────────────────────
    r2 = requests.post(
        "https://api-t2.fyers.in/vagator/v2/verify_otp",
        json={"request_key": request_key, "otp": totp.now()},
        timeout=15,
    )
    r2.raise_for_status()
    request_key = r2.json()["request_key"]

    # ── Step 3: Verify PIN ────────────────────────────────────
    r3 = requests.post(
        "https://api-t2.fyers.in/vagator/v2/verify_pin",
        json={
            "request_key":   request_key,
            "identity_type": "pin",
            "identifier":    pin,
        },
        timeout=15,
    )
    r3.raise_for_status()
    login_token = r3.json()["data"]["access_token"]

    # ── Step 4: Get auth code ─────────────────────────────────
    r4 = requests.post(
        "https://api-t2.fyers.in/api/v3/token",
        headers={"Authorization": f"Bearer {login_token}"},
        json={
            "fyers_id":      username,
            "app_id":        client_id.split("-")[0],
            "redirect_uri":  redirect_uri,
            "appType":       "100",
            "code_challenge": "",
            "state":         "None",
            "scope":         "",
            "nonce":         "",
            "response_type": "code",
            "create_cookie": True,
        },
        timeout=15,
    )
    r4.raise_for_status()
    auth_code = r4.json()["Url"].split("auth_code=")[1].split("&")[0]

    # ── Step 5: Exchange code → access token ──────────────────
    session.set_token(auth_code)
    token_resp   = session.generate_token()
    access_token = token_resp["access_token"]

    # ── Step 6: Persist to SSM (overwrite) ───────────────────
    put_ssm(SSM_ACCESS_TOKEN, access_token)
    logger.info("Fresh access token written to SSM.")
    return access_token


# ─────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────

def get_fyers_instance() -> fyersModel.FyersModel:
    """
    Called once at container startup.
    Returns a ready-to-use, validated FyersModel singleton.
    """
    client_id    = get_ssm(SSM_CLIENT_ID)
    secret_key   = get_ssm(SSM_SECRET_KEY)
    redirect_uri = get_ssm(SSM_REDIRECT_URI)

    # 1. Try token from SSM
    stored_token = get_ssm(SSM_ACCESS_TOKEN)
    if stored_token:
        instance = _build_fyers(client_id, stored_token)
        if _is_valid(instance):
            logger.info("SSM token is valid — skipping re-login.")
            return instance
        logger.info("SSM token expired — regenerating …")

    # 2. TOTP login → new token written to SSM inside the function
    new_token = _generate_fresh_token(client_id, secret_key, redirect_uri)
    instance  = _build_fyers(client_id, new_token)

    if not _is_valid(instance):
        raise RuntimeError("Freshly generated token failed validation. Aborting startup.")

    logger.info("Fyers SDK initialised successfully.")
    return instance


# ─────────────────────────────────────────────────────────────
# Module-level singleton
# Usage in other modules:  from autologin import fyers
# ─────────────────────────────────────────────────────────────
fyers: fyersModel.FyersModel = get_fyers_instance()
