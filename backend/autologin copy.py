"""
autologin.py — Fyers access token lifecycle manager.

Startup sequence:
  1. Read token from S3
  2. Validate by calling get_profile()
  3. If invalid/expired → TOTP-based re-login → upload new token to S3
  4. Return initialised FyersModel instance (singleton for the session)

Called ONCE at container startup. The returned `fyers` object is imported
by all other modules — no repeated login during the trading session.
"""

import logging
import time

import pyotp
import requests
from fyers_apiv3 import fyersModel
from fyers_apiv3.fyersModel import SessionModel

import s3_utils
from config import (
    get_ssm,
    SSM_CLIENT_ID, SSM_SECRET_KEY, SSM_REDIRECT_URI,
    SSM_TOTP_KEY, SSM_USERNAME, SSM_PIN,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Token validation
# ──────────────────────────────────────────────────────────────

def _build_fyers(client_id: str, access_token: str) -> fyersModel.FyersModel:
    return fyersModel.FyersModel(
        client_id=client_id,
        token=access_token,
        log_path="",
        is_async=False,
    )


def _is_token_valid(fyers_instance: fyersModel.FyersModel) -> bool:
    try:
        resp = fyers_instance.get_profile()
        return resp.get("code") == 200
    except Exception as exc:
        logger.warning("Token validation failed: %s", exc)
        return False


# ──────────────────────────────────────────────────────────────
# TOTP-based auto-login (headless)
# ──────────────────────────────────────────────────────────────

def _generate_access_token(client_id: str, secret_key: str, redirect_uri: str) -> str:
    """
    Full TOTP login flow using Fyers v3 API.
    Returns a fresh access_token string.
    """
    username    = get_ssm(SSM_USERNAME)
    pin         = get_ssm(SSM_PIN)
    totp_key    = get_ssm(SSM_TOTP_KEY)

    totp = pyotp.TOTP(totp_key)

    session = SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )

    # Step 1: Send login OTP
    logger.info("Requesting TOTP login…")
    send_otp_url = "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2"
    send_resp = requests.post(
        send_otp_url,
        json={"fy_id": username, "app_id": "2"},
        timeout=10,
    )
    send_resp.raise_for_status()

    time.sleep(2)   # ensure TOTP window is fresh

    # Step 2: Verify OTP
    verify_url = "https://api-t2.fyers.in/vagator/v2/verify_otp"
    verify_resp = requests.post(
        verify_url,
        json={
            "request_key": send_resp.json()["request_key"],
            "otp": totp.now(),
        },
        timeout=10,
    )
    verify_resp.raise_for_status()

    # Step 3: Verify PIN
    pin_url = "https://api-t2.fyers.in/vagator/v2/verify_pin_v2"
    pin_resp = requests.post(
        pin_url,
        json={
            "request_key": verify_resp.json()["request_key"],
            "identity_type": "pin",
            "identifier": pin,
        },
        timeout=10,
    )
    pin_resp.raise_for_status()
    access_token_login = pin_resp.json()["data"]["access_token"]

    # Step 4: Get auth code
    auth_url = "https://api-t2.fyers.in/api/v3/token"
    headers = {"Authorization": f"Bearer {access_token_login}"}
    auth_resp = requests.post(
        auth_url,
        headers=headers,
        json={
            "fyers_id": username,
            "app_id": client_id.split("-")[0],
            "redirect_uri": redirect_uri,
            "appType": "100",
            "code_challenge": "",
            "state": "None",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True,
        },
        timeout=10,
    )
    auth_resp.raise_for_status()
    auth_code = auth_resp.json()["Url"].split("auth_code=")[1].split("&")[0]

    # Step 5: Exchange code for access token
    session.set_token(auth_code)
    token_resp = session.generate_token()
    access_token = token_resp["access_token"]
    logger.info("New access token generated successfully.")
    return access_token


# ──────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────

def get_fyers_instance() -> fyersModel.FyersModel:
    """
    Called once at startup. Returns a ready-to-use FyersModel.
    Handles token read → validate → refresh cycle automatically.
    """
    client_id    = get_ssm(SSM_CLIENT_ID)
    secret_key   = get_ssm(SSM_SECRET_KEY)
    redirect_uri = get_ssm(SSM_REDIRECT_URI)

    # 1. Try existing token from S3
    existing_token = s3_utils.read_token_from_s3()
    if existing_token:
        fyers_instance = _build_fyers(client_id, existing_token)
        if _is_token_valid(fyers_instance):
            logger.info("Existing token is valid — skipping re-login.")
            return fyers_instance
        logger.info("Stored token is invalid/expired — refreshing…")

    # 2. Generate fresh token
    new_token = _generate_access_token(client_id, secret_key, redirect_uri)
    s3_utils.write_token_to_s3(new_token)

    fyers_instance = _build_fyers(client_id, new_token)
    if not _is_token_valid(fyers_instance):
        raise RuntimeError("Newly generated token still fails validation. Aborting.")

    return fyers_instance


# ──────────────────────────────────────────────────────────────
# Module-level singleton — imported by other modules
# ──────────────────────────────────────────────────────────────
# Usage: from autologin import fyers
fyers: fyersModel.FyersModel = get_fyers_instance()