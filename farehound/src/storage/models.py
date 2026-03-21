from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4


def _new_id() -> str:
    return uuid4().hex


@dataclass
class Route:
    route_id: str
    origin: str
    destination: str
    trip_type: str = "round_trip"
    earliest_departure: date | None = None
    latest_return: date | None = None
    date_flex_days: int = 3
    max_stops: int = 1
    passengers: int = 2
    preferred_airlines: list[str] = field(default_factory=list)
    notes: str | None = None
    active: bool = True
    created_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "route_id": self.route_id,
            "origin": self.origin,
            "destination": self.destination,
            "trip_type": self.trip_type,
            "earliest_departure": self.earliest_departure,
            "latest_return": self.latest_return,
            "date_flex_days": self.date_flex_days,
            "max_stops": self.max_stops,
            "passengers": self.passengers,
            "preferred_airlines": self.preferred_airlines,
            "notes": self.notes,
            "active": self.active,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: tuple, columns: list[str]) -> Route:
        d = dict(zip(columns, row))
        return cls(
            route_id=d["route_id"],
            origin=d["origin"],
            destination=d["destination"],
            trip_type=d.get("trip_type", "round_trip"),
            earliest_departure=d.get("earliest_departure"),
            latest_return=d.get("latest_return"),
            date_flex_days=d.get("date_flex_days", 3),
            max_stops=d.get("max_stops", 1),
            passengers=d.get("passengers", 2),
            preferred_airlines=d.get("preferred_airlines") or [],
            notes=d.get("notes"),
            active=d.get("active", True),
            created_at=d.get("created_at"),
        )


@dataclass
class PollWindow:
    window_id: str
    route_id: str
    outbound_date: date
    return_date: date | None = None
    priority: str = "normal"
    last_polled_at: datetime | None = None
    lowest_seen_price: Decimal | None = None
    created_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id,
            "route_id": self.route_id,
            "outbound_date": self.outbound_date,
            "return_date": self.return_date,
            "priority": self.priority,
            "last_polled_at": self.last_polled_at,
            "lowest_seen_price": self.lowest_seen_price,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: tuple, columns: list[str]) -> PollWindow:
        d = dict(zip(columns, row))
        return cls(
            window_id=d["window_id"],
            route_id=d["route_id"],
            outbound_date=d["outbound_date"],
            return_date=d.get("return_date"),
            priority=d.get("priority", "normal"),
            last_polled_at=d.get("last_polled_at"),
            lowest_seen_price=d.get("lowest_seen_price"),
            created_at=d.get("created_at"),
        )


@dataclass
class PriceSnapshot:
    snapshot_id: str
    route_id: str
    observed_at: datetime
    source: str
    passengers: int
    window_id: str | None = None
    outbound_date: date | None = None
    return_date: date | None = None
    lowest_price: Decimal | None = None
    currency: str = "EUR"
    best_flight: dict | None = None
    all_flights: list | None = None
    price_level: str | None = None
    typical_low: Decimal | None = None
    typical_high: Decimal | None = None
    price_history: list | None = None
    search_params: dict | None = None
    created_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "route_id": self.route_id,
            "window_id": self.window_id,
            "observed_at": self.observed_at,
            "source": self.source,
            "outbound_date": self.outbound_date,
            "return_date": self.return_date,
            "passengers": self.passengers,
            "lowest_price": self.lowest_price,
            "currency": self.currency,
            "best_flight": self.best_flight,
            "all_flights": self.all_flights,
            "price_level": self.price_level,
            "typical_low": self.typical_low,
            "typical_high": self.typical_high,
            "price_history": self.price_history,
            "search_params": self.search_params,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: tuple, columns: list[str]) -> PriceSnapshot:
        d = dict(zip(columns, row))
        return cls(
            snapshot_id=d["snapshot_id"],
            route_id=d["route_id"],
            window_id=d.get("window_id"),
            observed_at=d["observed_at"],
            source=d["source"],
            outbound_date=d.get("outbound_date"),
            return_date=d.get("return_date"),
            passengers=d["passengers"],
            lowest_price=d.get("lowest_price"),
            currency=d.get("currency", "EUR"),
            best_flight=d.get("best_flight"),
            all_flights=d.get("all_flights"),
            price_level=d.get("price_level"),
            typical_low=d.get("typical_low"),
            typical_high=d.get("typical_high"),
            price_history=d.get("price_history"),
            search_params=d.get("search_params"),
            created_at=d.get("created_at"),
        )


@dataclass
class Deal:
    deal_id: str
    snapshot_id: str
    route_id: str
    score: Decimal | None = None
    urgency: str | None = None
    reasoning: str | None = None
    booking_url: str | None = None
    alert_sent: bool = False
    alert_sent_at: datetime | None = None
    booked: bool = False
    feedback: str | None = None
    created_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "deal_id": self.deal_id,
            "snapshot_id": self.snapshot_id,
            "route_id": self.route_id,
            "score": self.score,
            "urgency": self.urgency,
            "reasoning": self.reasoning,
            "booking_url": self.booking_url,
            "alert_sent": self.alert_sent,
            "alert_sent_at": self.alert_sent_at,
            "booked": self.booked,
            "feedback": self.feedback,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: tuple, columns: list[str]) -> Deal:
        d = dict(zip(columns, row))
        return cls(
            deal_id=d["deal_id"],
            snapshot_id=d["snapshot_id"],
            route_id=d["route_id"],
            score=d.get("score"),
            urgency=d.get("urgency"),
            reasoning=d.get("reasoning"),
            booking_url=d.get("booking_url"),
            alert_sent=d.get("alert_sent", False),
            alert_sent_at=d.get("alert_sent_at"),
            booked=d.get("booked", False),
            feedback=d.get("feedback"),
            created_at=d.get("created_at"),
        )


@dataclass
class AlertRule:
    rule_id: str
    route_id: str
    rule_type: str
    threshold: Decimal | None = None
    channel: str = "ha_notify"
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "route_id": self.route_id,
            "rule_type": self.rule_type,
            "threshold": self.threshold,
            "channel": self.channel,
            "active": self.active,
        }

    @classmethod
    def from_row(cls, row: tuple, columns: list[str]) -> AlertRule:
        d = dict(zip(columns, row))
        return cls(
            rule_id=d["rule_id"],
            route_id=d["route_id"],
            rule_type=d["rule_type"],
            threshold=d.get("threshold"),
            channel=d.get("channel", "ha_notify"),
            active=d.get("active", True),
        )
