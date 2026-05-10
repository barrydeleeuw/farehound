"""Telegram WebApp `initData` HMAC validation.

Per Telegram's WebApp spec
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app),
authenticity is verified by:

  1. Parse the URL-encoded `initData` query string into key/value pairs.
  2. Remove the `hash` field; sort the remaining pairs alphabetically by key.
  3. Build a "data check string" by joining sorted `key=value` lines with `\n`.
  4. Compute the secret key as `HMAC-SHA256(key="WebAppData", data=bot_token)`.
  5. Compute `HMAC-SHA256(key=secret_key, data=data_check_string)`; compare against
     the supplied `hash` (hex). Constant-time comparison.

Returns the parsed `user` dict on success (with `id`, `first_name`, etc.) or raises
`InitDataInvalid`. The Telegram-supplied `auth_date` is checked against a max-age
window (default 24h) so old initData blobs can't be replayed indefinitely.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request

logger = logging.getLogger("farehound.web.auth")


class InitDataInvalid(Exception):
    """Raised when initData fails HMAC validation or expiry check."""


def _bot_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise InitDataInvalid("TELEGRAM_BOT_TOKEN not configured")
    return token


def validate_init_data(init_data: str, max_age_seconds: int = 86400) -> dict:
    """Validate raw `initData` query-string and return the parsed user dict.

    Args:
        init_data: the URL-encoded query string from `Telegram.WebApp.initData`.
        max_age_seconds: reject initData older than this many seconds (default 24h).

    Returns:
        The `user` dict embedded in the initData (id, first_name, etc.).

    Raises:
        InitDataInvalid: hash mismatch, missing fields, or expired auth_date.
    """
    if not init_data:
        raise InitDataInvalid("empty initData")

    # parse_qsl preserves order, but we'll sort for the data-check string anyway.
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    fields = dict(pairs)

    supplied_hash = fields.pop("hash", None)
    if not supplied_hash:
        raise InitDataInvalid("missing hash field")

    auth_date_str = fields.get("auth_date")
    if not auth_date_str:
        raise InitDataInvalid("missing auth_date")
    try:
        auth_date = int(auth_date_str)
    except ValueError:
        raise InitDataInvalid("invalid auth_date") from None

    if max_age_seconds > 0:
        age = time.time() - auth_date
        if age > max_age_seconds:
            raise InitDataInvalid(f"initData expired (age={age:.0f}s)")
        if age < -300:  # tolerate up to 5 minutes of clock skew on the future side
            raise InitDataInvalid("initData auth_date is in the future")

    # Build the data-check string: sorted alphabetically by key, joined by \n.
    data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))

    secret_key = hmac.new(b"WebAppData", _bot_token().encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, supplied_hash):
        raise InitDataInvalid("hash mismatch")

    user_json = fields.get("user")
    if not user_json:
        raise InitDataInvalid("missing user field")
    try:
        user = json.loads(user_json)
    except json.JSONDecodeError:
        raise InitDataInvalid("invalid user JSON") from None

    if not isinstance(user, dict) or "id" not in user:
        raise InitDataInvalid("user dict missing id")

    return user


async def require_user(request: Request) -> dict:
    """FastAPI dependency: validate initData from the request and return the user dict.

    Looks for initData in (in order): the `X-Telegram-Init-Data` header, then the
    `?tg=<initData>` query parameter (used by HTML page loads where setting a header
    isn't easy from the Telegram WebApp launch flow).

    The returned dict has at minimum `{"id": <telegram_user_id>, "first_name": ...}`.
    Raises HTTP 401 on validation failure.
    """
    init_data = request.headers.get("x-telegram-init-data") or request.query_params.get("tg") or ""

    # Local-development convenience: when explicitly enabled, skip validation and
    # return a stub user. Keeps the worktree-served preview functional.
    if os.environ.get("FAREHOUND_WEB_DEV_BYPASS_AUTH") == "1":
        logger.warning("DEV bypass active — returning stub user without HMAC validation")
        return {"id": int(os.environ.get("FAREHOUND_WEB_DEV_USER_ID", "0")), "first_name": "DevUser"}

    try:
        return validate_init_data(init_data)
    except InitDataInvalid as e:
        logger.info("initData rejected: %s", e)
        raise HTTPException(status_code=401, detail=f"initData invalid: {e}") from None
