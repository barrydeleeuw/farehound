"""Tests for src/web/auth.py — Telegram initData HMAC validation."""

import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

import pytest

from src.web.auth import InitDataInvalid, validate_init_data


BOT_TOKEN = "1234567:test-bot-token-AAAA"


def _sign(fields: dict, bot_token: str = BOT_TOKEN) -> str:
    """Build a valid initData query string with a correct hash."""
    data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    fields = dict(fields)
    fields["hash"] = h
    return urlencode(fields)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)


def _user_field(user_id: int = 42, name: str = "Barry") -> str:
    return json.dumps({"id": user_id, "first_name": name})


class TestValidateInitData:
    def test_valid_initdata_returns_user(self):
        fields = {
            "auth_date": str(int(time.time())),
            "user": _user_field(42, "Barry"),
            "query_id": "abc",
        }
        init = _sign(fields)
        user = validate_init_data(init)
        assert user["id"] == 42
        assert user["first_name"] == "Barry"

    def test_empty_initdata_rejected(self):
        with pytest.raises(InitDataInvalid, match="empty"):
            validate_init_data("")

    def test_missing_hash_rejected(self):
        init = urlencode({"auth_date": str(int(time.time())), "user": _user_field()})
        with pytest.raises(InitDataInvalid, match="hash"):
            validate_init_data(init)

    def test_forged_hash_rejected(self):
        # Use the right key/value pairs but a fake hash
        fields = {
            "auth_date": str(int(time.time())),
            "user": _user_field(),
        }
        forged = urlencode({**fields, "hash": "deadbeef" * 8})
        with pytest.raises(InitDataInvalid, match="hash mismatch"):
            validate_init_data(forged)

    def test_wrong_token_rejected(self):
        # Sign with one token, validate with another — must reject
        fields = {
            "auth_date": str(int(time.time())),
            "user": _user_field(),
        }
        init = _sign(fields, bot_token="999:wrong-token")
        with pytest.raises(InitDataInvalid, match="hash mismatch"):
            validate_init_data(init)

    def test_tampered_user_rejected(self):
        fields = {
            "auth_date": str(int(time.time())),
            "user": _user_field(42, "Barry"),
        }
        init_str = _sign(fields)
        # Tamper with the user payload AFTER signing
        tampered = init_str.replace("Barry", "Eve")
        with pytest.raises(InitDataInvalid, match="hash mismatch"):
            validate_init_data(tampered)

    def test_missing_auth_date_rejected(self):
        fields = {"user": _user_field(), "query_id": "abc"}
        # Sign without auth_date — should fail before we even check the hash
        init = _sign(fields)
        with pytest.raises(InitDataInvalid, match="auth_date"):
            validate_init_data(init)

    def test_expired_initdata_rejected(self):
        old = int(time.time()) - 86400 * 2  # 2 days old
        fields = {
            "auth_date": str(old),
            "user": _user_field(),
        }
        init = _sign(fields)
        with pytest.raises(InitDataInvalid, match="expired"):
            validate_init_data(init, max_age_seconds=86400)

    def test_future_auth_date_rejected(self):
        future = int(time.time()) + 600  # 10 minutes in the future
        fields = {
            "auth_date": str(future),
            "user": _user_field(),
        }
        init = _sign(fields)
        with pytest.raises(InitDataInvalid, match="future"):
            validate_init_data(init)

    def test_missing_user_rejected(self):
        fields = {"auth_date": str(int(time.time())), "query_id": "abc"}
        init = _sign(fields)
        with pytest.raises(InitDataInvalid, match="user"):
            validate_init_data(init)

    def test_invalid_user_json_rejected(self):
        fields = {
            "auth_date": str(int(time.time())),
            "user": "not-json",
        }
        init = _sign(fields)
        with pytest.raises(InitDataInvalid, match="user JSON"):
            validate_init_data(init)

    def test_user_without_id_rejected(self):
        fields = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"first_name": "Barry"}),  # no id
        }
        init = _sign(fields)
        with pytest.raises(InitDataInvalid, match="id"):
            validate_init_data(init)

    def test_missing_token_rejected(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        fields = {
            "auth_date": str(int(time.time())),
            "user": _user_field(),
        }
        init = _sign(fields)
        with pytest.raises(InitDataInvalid, match="TELEGRAM_BOT_TOKEN"):
            validate_init_data(init)

    def test_max_age_zero_skips_expiry_check(self):
        # When max_age_seconds=0 (e.g. for testing) old initData is accepted
        old = int(time.time()) - 86400 * 30
        fields = {"auth_date": str(old), "user": _user_field()}
        init = _sign(fields)
        user = validate_init_data(init, max_age_seconds=0)
        assert user["id"] == 42
