from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _resolve_env(env_var_name: str) -> str:
    val = os.environ.get(env_var_name)
    if val is None:
        raise ValueError(f"Environment variable '{env_var_name}' is not set")
    return val


@dataclass
class SerpAPIConfig:
    api_key_env: str
    currency: str = "EUR"
    deep_search: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> SerpAPIConfig:
        return cls(
            api_key_env=d["api_key_env"],
            currency=d.get("currency", "EUR"),
            deep_search=d.get("deep_search", True),
        )

    @property
    def api_key(self) -> str:
        return _resolve_env(self.api_key_env)


@dataclass
class AnthropicConfig:
    api_key_env: str
    model: str = "claude-sonnet-4-20250514"

    @classmethod
    def from_dict(cls, d: dict) -> AnthropicConfig:
        return cls(
            api_key_env=d["api_key_env"],
            model=d.get("model", "claude-sonnet-4-20250514"),
        )

    @property
    def api_key(self) -> str:
        return _resolve_env(self.api_key_env)


@dataclass
class TravellerConfig:
    name: str
    home_airport: str = "AMS"
    preferences: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> TravellerConfig:
        return cls(
            name=d["name"],
            home_airport=d.get("home_airport", "AMS"),
            preferences=d.get("preferences", []),
        )


@dataclass
class Route:
    id: str
    origin: str
    destination: str
    trip_type: str = "round_trip"
    earliest_departure: str | None = None
    latest_return: str | None = None
    date_flexibility_days: int = 3
    max_stops: int = 1
    passengers: int = 2
    preferred_airlines: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> Route:
        return cls(
            id=d["id"],
            origin=d["origin"],
            destination=d["destination"],
            trip_type=d.get("trip_type", "round_trip"),
            earliest_departure=d.get("earliest_departure"),
            latest_return=d.get("latest_return"),
            date_flexibility_days=d.get("date_flexibility_days", 3),
            max_stops=d.get("max_stops", 1),
            passengers=d.get("passengers", 2),
            preferred_airlines=d.get("preferred_airlines", []),
            notes=d.get("notes", ""),
        )


@dataclass
class ScoringConfig:
    alert_threshold: float = 0.75
    watch_threshold: float = 0.50
    poll_interval_hours: int = 4
    digest_time: tuple[int, int] = (8, 0)

    @classmethod
    def from_dict(cls, d: dict) -> ScoringConfig:
        raw_time = d.get("digest_time", "08:00")
        if isinstance(raw_time, str) and ":" in raw_time:
            h, m = raw_time.split(":")
            digest_time = (int(h), int(m))
        else:
            digest_time = (8, 0)
        return cls(
            alert_threshold=d.get("alert_threshold", 0.75),
            watch_threshold=d.get("watch_threshold", 0.50),
            poll_interval_hours=d.get("poll_interval_hours", 4),
            digest_time=digest_time,
        )


@dataclass
class TelegramAlertConfig:
    bot_token_env: str
    chat_id_env: str
    enabled: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> TelegramAlertConfig:
        return cls(
            bot_token_env=d["bot_token_env"],
            chat_id_env=d["chat_id_env"],
            enabled=d.get("enabled", False),
        )

    @property
    def bot_token(self) -> str:
        return _resolve_env(self.bot_token_env)

    @property
    def chat_id(self) -> str:
        return _resolve_env(self.chat_id_env)


@dataclass
class AppConfig:
    serpapi: SerpAPIConfig
    anthropic: AnthropicConfig
    traveller: TravellerConfig
    routes: list[Route]
    scoring: ScoringConfig
    telegram_alerts: TelegramAlertConfig | None = None
    airports: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> AppConfig:
        return cls(
            serpapi=SerpAPIConfig.from_dict(d["serpapi"]),
            anthropic=AnthropicConfig.from_dict(d["anthropic"]),
            traveller=TravellerConfig.from_dict(d["traveller"]),
            routes=[Route.from_dict(r) for r in d.get("routes", [])],
            scoring=ScoringConfig.from_dict(d.get("scoring", {})),
            telegram_alerts=TelegramAlertConfig.from_dict(d["telegram_alerts"])
            if "telegram_alerts" in d
            else None,
            airports=d.get("airports", []),
        )


def _load_airports_yaml() -> list[dict]:
    """Load airports from config/airports.yaml. Checks HA path first, then project root."""
    for base in [Path("/app/config"), Path(__file__).parent.parent / "config"]:
        airports_file = base / "airports.yaml"
        if airports_file.exists():
            data = yaml.safe_load(airports_file.read_text()) or {}
            return data.get("airports", [])
    return []


def _validate(config: AppConfig) -> None:
    if not config.routes:
        raise ValueError("At least one route is required")
    for route in config.routes:
        if not route.origin or not route.destination:
            raise ValueError(f"Route '{route.id}' missing origin or destination")
        if route.passengers < 1:
            raise ValueError(f"Route '{route.id}' must have at least 1 passenger")


def _translate_ha_options(opts: dict) -> dict:
    """Translate flat HA add-on options into the nested structure AppConfig expects."""
    translated: dict = {
        "serpapi": {
            "api_key_env": "SERPAPI_API_KEY",
            "currency": "EUR",
        },
        "anthropic": {
            "api_key_env": "ANTHROPIC_API_KEY",
        },
        "traveller": {
            "name": opts.get("traveller_name", "Traveller"),
            "home_airport": opts.get("home_airport", "AMS"),
        },
        "scoring": {
            "alert_threshold": opts.get("alert_threshold", 0.75),
            "poll_interval_hours": opts.get("poll_interval_hours", 4),
        },
    }

    # Routes: HA options stores as JSON list
    if "routes" in opts:
        routes = opts["routes"]
        if isinstance(routes, str):
            routes = json.loads(routes)
        translated["routes"] = routes
    else:
        translated["routes"] = []

    # Telegram alerts (optional — bot notifications)
    if opts.get("telegram_bot_token"):
        translated["telegram_alerts"] = {
            "bot_token_env": "TELEGRAM_BOT_TOKEN",
            "chat_id_env": "TELEGRAM_CHAT_ID",
            "enabled": True,
        }

    # Merge config.yaml if it exists (check /data first, then /app for baked-in config)
    config_yaml = Path("/data/config.yaml")
    if not config_yaml.exists():
        config_yaml = Path("/app/config.yaml")
    if config_yaml.exists():
        yaml_data = yaml.safe_load(config_yaml.read_text()) or {}
        # config.yaml routes take precedence if HA options has none
        if not translated["routes"] and "routes" in yaml_data:
            translated["routes"] = yaml_data["routes"]
        if "telegram_alerts" not in translated and "telegram_alerts" in yaml_data:
            translated["telegram_alerts"] = yaml_data["telegram_alerts"]

    return translated


def load_config(path: str | Path | None = None) -> AppConfig:
    try:
        if path is None:
            # HA add-on mode: /data/options.json
            ha_options = Path("/data/options.json")
            if ha_options.exists():
                raw = _translate_ha_options(json.loads(ha_options.read_text()))
            else:
                # Default: config.yaml in project root
                default_path = Path(__file__).parent.parent / "config.yaml"
                raw = yaml.safe_load(default_path.read_text())
        else:
            p = Path(path)
            if p.suffix == ".json":
                raw = json.loads(p.read_text())
            else:
                raw = yaml.safe_load(p.read_text())
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise ValueError(f"Failed to parse config file: {e}") from e

    config = AppConfig.from_dict(raw)
    if not config.airports:
        config.airports = _load_airports_yaml()
    _validate(config)
    return config
