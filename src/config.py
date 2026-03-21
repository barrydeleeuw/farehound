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

    @classmethod
    def from_dict(cls, d: dict) -> TravellerConfig:
        return cls(name=d["name"], home_airport=d.get("home_airport", "AMS"))


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
class AlertConfig:
    notify_service: str
    base_url_env: str | None = None
    token_env: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> AlertConfig:
        ha = d.get("homeassistant", {})
        return cls(
            notify_service=ha["notify_service"],
            base_url_env=ha.get("base_url_env"),
            token_env=ha.get("token_env"),
        )

    @property
    def base_url(self) -> str | None:
        return _resolve_env(self.base_url_env) if self.base_url_env else None

    @property
    def token(self) -> str | None:
        return _resolve_env(self.token_env) if self.token_env else None


@dataclass
class ScoringConfig:
    alert_threshold: float = 0.75
    watch_threshold: float = 0.50
    poll_interval_hours: int = 4

    @classmethod
    def from_dict(cls, d: dict) -> ScoringConfig:
        return cls(
            alert_threshold=d.get("alert_threshold", 0.75),
            watch_threshold=d.get("watch_threshold", 0.50),
            poll_interval_hours=d.get("poll_interval_hours", 4),
        )


@dataclass
class CommunityFeedConfig:
    type: str
    channel: str
    filter_origins: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> CommunityFeedConfig:
        return cls(
            type=d["type"],
            channel=d["channel"],
            filter_origins=d.get("filter_origins", []),
        )


@dataclass
class TelegramConfig:
    api_id_env: str
    api_hash_env: str

    @classmethod
    def from_dict(cls, d: dict) -> TelegramConfig:
        return cls(api_id_env=d["api_id_env"], api_hash_env=d["api_hash_env"])

    @property
    def api_id(self) -> str:
        return _resolve_env(self.api_id_env)

    @property
    def api_hash(self) -> str:
        return _resolve_env(self.api_hash_env)


@dataclass
class AppConfig:
    serpapi: SerpAPIConfig
    anthropic: AnthropicConfig
    traveller: TravellerConfig
    routes: list[Route]
    alerts: AlertConfig
    scoring: ScoringConfig
    community_feeds: list[CommunityFeedConfig]
    telegram: TelegramConfig | None = None

    @classmethod
    def from_dict(cls, d: dict) -> AppConfig:
        return cls(
            serpapi=SerpAPIConfig.from_dict(d["serpapi"]),
            anthropic=AnthropicConfig.from_dict(d["anthropic"]),
            traveller=TravellerConfig.from_dict(d["traveller"]),
            routes=[Route.from_dict(r) for r in d.get("routes", [])],
            alerts=AlertConfig.from_dict(d["alerts"]),
            scoring=ScoringConfig.from_dict(d.get("scoring", {})),
            community_feeds=[
                CommunityFeedConfig.from_dict(f)
                for f in d.get("community_feeds", [])
            ],
            telegram=TelegramConfig.from_dict(d["telegram"])
            if "telegram" in d
            else None,
        )


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
        "alerts": {
            "homeassistant": {
                "notify_service": opts.get("ha_notify_service", "notify.mobile_app_phone"),
            },
        },
        "scoring": {
            "alert_threshold": opts.get("alert_threshold", 0.75),
            "poll_interval_hours": opts.get("poll_interval_hours", 4),
        },
        "community_feeds": [],
    }

    # Routes: HA options stores as JSON list
    if "routes" in opts:
        routes = opts["routes"]
        if isinstance(routes, str):
            routes = json.loads(routes)
        translated["routes"] = routes
    else:
        translated["routes"] = []

    # Telegram config (optional)
    if opts.get("telegram_api_id"):
        translated["telegram"] = {
            "api_id_env": "TELEGRAM_API_ID",
            "api_hash_env": "TELEGRAM_API_HASH",
        }

    # Merge config.yaml if it exists alongside HA options (routes, feeds, etc.)
    config_yaml = Path("/data/config.yaml")
    if config_yaml.exists():
        yaml_data = yaml.safe_load(config_yaml.read_text()) or {}
        # config.yaml routes take precedence if HA options has none
        if not translated["routes"] and "routes" in yaml_data:
            translated["routes"] = yaml_data["routes"]
        if not translated["community_feeds"] and "community_feeds" in yaml_data:
            translated["community_feeds"] = yaml_data["community_feeds"]

    return translated


def load_config(path: str | Path | None = None) -> AppConfig:
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

    config = AppConfig.from_dict(raw)
    _validate(config)
    return config
