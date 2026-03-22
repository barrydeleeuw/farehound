from __future__ import annotations

import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.config import (
    AppConfig,
    SerpAPIConfig,
    AnthropicConfig,
    TravellerConfig,
    Route,
    ScoringConfig,
    CommunityFeedConfig,
    TelegramConfig,
    TelegramAlertConfig,
    _resolve_env,
    _translate_ha_options,
    _validate,
    load_config,
)


# --- _resolve_env ---

def test_resolve_env_returns_value():
    with patch.dict(os.environ, {"TEST_KEY": "secret123"}):
        assert _resolve_env("TEST_KEY") == "secret123"


def test_resolve_env_missing_raises():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="Environment variable 'MISSING' is not set"):
            _resolve_env("MISSING")


# --- SerpAPIConfig ---

def test_serpapi_config_from_dict_defaults():
    cfg = SerpAPIConfig.from_dict({"api_key_env": "KEY"})
    assert cfg.api_key_env == "KEY"
    assert cfg.currency == "EUR"
    assert cfg.deep_search is True


def test_serpapi_config_from_dict_custom():
    cfg = SerpAPIConfig.from_dict({"api_key_env": "K", "currency": "USD", "deep_search": False})
    assert cfg.currency == "USD"
    assert cfg.deep_search is False


def test_serpapi_api_key_property():
    cfg = SerpAPIConfig(api_key_env="MY_SERPAPI_KEY")
    with patch.dict(os.environ, {"MY_SERPAPI_KEY": "abc"}):
        assert cfg.api_key == "abc"


# --- AnthropicConfig ---

def test_anthropic_config_defaults():
    cfg = AnthropicConfig.from_dict({"api_key_env": "AK"})
    assert cfg.model == "claude-sonnet-4-20250514"


def test_anthropic_config_custom_model():
    cfg = AnthropicConfig.from_dict({"api_key_env": "AK", "model": "claude-opus-4-20250514"})
    assert cfg.model == "claude-opus-4-20250514"


# --- TravellerConfig ---

def test_traveller_config_defaults():
    cfg = TravellerConfig.from_dict({"name": "Alice"})
    assert cfg.home_airport == "AMS"
    assert cfg.preferences == []


# --- Route ---

def test_route_from_dict_minimal():
    r = Route.from_dict({"id": "r1", "origin": "AMS", "destination": "NRT"})
    assert r.id == "r1"
    assert r.trip_type == "round_trip"
    assert r.passengers == 2
    assert r.preferred_airlines == []


def test_route_from_dict_full():
    r = Route.from_dict({
        "id": "r2",
        "origin": "AMS",
        "destination": "IST",
        "trip_type": "one_way",
        "earliest_departure": "2026-06-01",
        "latest_return": "2026-09-30",
        "date_flexibility_days": 7,
        "max_stops": 0,
        "passengers": 3,
        "preferred_airlines": ["KLM", "TK"],
        "notes": "test",
    })
    assert r.trip_type == "one_way"
    assert r.max_stops == 0
    assert r.passengers == 3
    assert r.preferred_airlines == ["KLM", "TK"]


# --- ScoringConfig ---

def test_scoring_config_defaults():
    cfg = ScoringConfig.from_dict({})
    assert cfg.alert_threshold == 0.75
    assert cfg.digest_time == (8, 0)


def test_scoring_config_digest_time_parsing():
    cfg = ScoringConfig.from_dict({"digest_time": "19:30"})
    assert cfg.digest_time == (19, 30)


def test_scoring_config_invalid_digest_time():
    cfg = ScoringConfig.from_dict({"digest_time": 42})
    assert cfg.digest_time == (8, 0)


# --- CommunityFeedConfig ---

def test_community_feed_config():
    cfg = CommunityFeedConfig.from_dict({
        "type": "telegram_channel",
        "channel": "@test",
        "filter_origins": ["AMS"],
    })
    assert cfg.type == "telegram_channel"
    assert cfg.filter_origins == ["AMS"]


# --- TelegramConfig ---

def test_telegram_config():
    cfg = TelegramConfig.from_dict({"api_id_env": "TG_ID", "api_hash_env": "TG_HASH"})
    assert cfg.api_id_env == "TG_ID"


# --- TelegramAlertConfig ---

def test_telegram_alert_config_from_dict():
    cfg = TelegramAlertConfig.from_dict({
        "bot_token_env": "TG_BOT_TOKEN",
        "chat_id_env": "TG_CHAT_ID",
        "enabled": True,
    })
    assert cfg.bot_token_env == "TG_BOT_TOKEN"
    assert cfg.chat_id_env == "TG_CHAT_ID"
    assert cfg.enabled is True


def test_telegram_alert_config_defaults():
    cfg = TelegramAlertConfig.from_dict({
        "bot_token_env": "TG_BOT_TOKEN",
        "chat_id_env": "TG_CHAT_ID",
    })
    assert cfg.enabled is False


def test_telegram_alert_config_properties():
    cfg = TelegramAlertConfig(bot_token_env="TG_BOT", chat_id_env="TG_CHAT")
    with patch.dict(os.environ, {"TG_BOT": "token123", "TG_CHAT": "-100999"}):
        assert cfg.bot_token == "token123"
        assert cfg.chat_id == "-100999"


# --- _validate ---

def test_validate_no_routes_raises():
    config = AppConfig(
        serpapi=SerpAPIConfig(api_key_env="K"),
        anthropic=AnthropicConfig(api_key_env="K"),
        traveller=TravellerConfig(name="T"),
        routes=[],
        scoring=ScoringConfig(),
        community_feeds=[],
    )
    with pytest.raises(ValueError, match="At least one route"):
        _validate(config)


def test_validate_missing_origin_raises():
    config = AppConfig(
        serpapi=SerpAPIConfig(api_key_env="K"),
        anthropic=AnthropicConfig(api_key_env="K"),
        traveller=TravellerConfig(name="T"),
        routes=[Route(id="r1", origin="", destination="NRT")],
        scoring=ScoringConfig(),
        community_feeds=[],
    )
    with pytest.raises(ValueError, match="missing origin or destination"):
        _validate(config)


def test_validate_zero_passengers_raises():
    config = AppConfig(
        serpapi=SerpAPIConfig(api_key_env="K"),
        anthropic=AnthropicConfig(api_key_env="K"),
        traveller=TravellerConfig(name="T"),
        routes=[Route(id="r1", origin="AMS", destination="NRT", passengers=0)],
        scoring=ScoringConfig(),
        community_feeds=[],
    )
    with pytest.raises(ValueError, match="at least 1 passenger"):
        _validate(config)


# --- _translate_ha_options ---

def test_translate_ha_options_basic():
    opts = {
        "traveller_name": "Bob",
        "home_airport": "LHR",
        "ha_notify_service": "notify.bob",
        "routes": json.dumps([{"id": "r1", "origin": "LHR", "destination": "JFK"}]),
    }
    result = _translate_ha_options(opts)
    assert result["traveller"]["name"] == "Bob"
    assert result["traveller"]["home_airport"] == "LHR"
    assert len(result["routes"]) == 1


def test_translate_ha_options_telegram():
    opts = {
        "ha_notify_service": "notify.phone",
        "telegram_api_id": "123",
    }
    result = _translate_ha_options(opts)
    assert "telegram" in result
    assert result["telegram"]["api_id_env"] == "TELEGRAM_API_ID"


def test_translate_ha_options_no_telegram():
    opts = {"ha_notify_service": "notify.phone"}
    result = _translate_ha_options(opts)
    assert "telegram" not in result


# --- load_config ---

def test_load_config_yaml(tmp_path):
    config_data = {
        "serpapi": {"api_key_env": "SERPAPI_API_KEY"},
        "anthropic": {"api_key_env": "ANTHROPIC_API_KEY"},
        "traveller": {"name": "Test"},
        "routes": [{"id": "r1", "origin": "AMS", "destination": "NRT"}],
        "alerts": {"homeassistant": {"notify_service": "notify.test"}},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(config_data))
    config = load_config(str(p))
    assert config.traveller.name == "Test"
    assert len(config.routes) == 1


def test_load_config_json(tmp_path):
    config_data = {
        "serpapi": {"api_key_env": "SERPAPI_API_KEY"},
        "anthropic": {"api_key_env": "ANTHROPIC_API_KEY"},
        "traveller": {"name": "Test"},
        "routes": [{"id": "r1", "origin": "AMS", "destination": "NRT"}],
        "alerts": {"homeassistant": {"notify_service": "notify.test"}},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_data))
    config = load_config(str(p))
    assert config.traveller.name == "Test"
