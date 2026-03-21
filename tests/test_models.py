from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.storage.models import Route, PollWindow, PriceSnapshot, Deal, AlertRule


# --- Route ---

def test_route_defaults():
    r = Route(route_id="r1", origin="AMS", destination="NRT")
    assert r.trip_type == "round_trip"
    assert r.date_flex_days == 3
    assert r.max_stops == 1
    assert r.passengers == 2
    assert r.preferred_airlines == []
    assert r.active is True


def test_route_to_dict():
    r = Route(route_id="r1", origin="AMS", destination="NRT", notes="test")
    d = r.to_dict()
    assert d["route_id"] == "r1"
    assert d["origin"] == "AMS"
    assert d["notes"] == "test"
    assert d["active"] is True


def test_route_from_row():
    columns = ["route_id", "origin", "destination", "trip_type", "date_flex_days",
               "max_stops", "passengers", "preferred_airlines", "notes", "active"]
    row = ("r1", "AMS", "NRT", "round_trip", 5, 2, 3, ["KLM"], "note", True)
    r = Route.from_row(row, columns)
    assert r.route_id == "r1"
    assert r.date_flex_days == 5
    assert r.passengers == 3
    assert r.preferred_airlines == ["KLM"]


def test_route_from_row_minimal():
    columns = ["route_id", "origin", "destination"]
    row = ("r1", "AMS", "NRT")
    r = Route.from_row(row, columns)
    assert r.trip_type == "round_trip"
    assert r.date_flex_days == 3


def test_route_to_dict_roundtrip():
    r = Route(route_id="r1", origin="AMS", destination="NRT")
    d = r.to_dict()
    assert d["route_id"] == r.route_id
    assert d["origin"] == r.origin
    assert set(d.keys()) == {
        "route_id", "origin", "destination", "trip_type",
        "earliest_departure", "latest_return", "date_flex_days",
        "max_stops", "passengers", "preferred_airlines", "notes",
        "active", "created_at",
    }


# --- PollWindow ---

def test_poll_window_defaults():
    pw = PollWindow(window_id="w1", route_id="r1", outbound_date=date(2026, 10, 1))
    assert pw.priority == "normal"
    assert pw.lowest_seen_price is None


def test_poll_window_to_dict():
    pw = PollWindow(window_id="w1", route_id="r1", outbound_date=date(2026, 10, 1))
    d = pw.to_dict()
    assert d["window_id"] == "w1"
    assert d["outbound_date"] == date(2026, 10, 1)


def test_poll_window_from_row():
    columns = ["window_id", "route_id", "outbound_date", "return_date", "priority"]
    row = ("w1", "r1", date(2026, 10, 1), date(2026, 10, 15), "focus")
    pw = PollWindow.from_row(row, columns)
    assert pw.priority == "focus"
    assert pw.return_date == date(2026, 10, 15)


# --- PriceSnapshot ---

def test_price_snapshot_defaults():
    now = datetime(2026, 1, 1, 12, 0)
    ps = PriceSnapshot(
        snapshot_id="s1", route_id="r1", observed_at=now,
        source="serpapi_poll", passengers=2,
    )
    assert ps.currency == "EUR"
    assert ps.lowest_price is None
    assert ps.best_flight is None


def test_price_snapshot_to_dict():
    now = datetime(2026, 1, 1, 12, 0)
    ps = PriceSnapshot(
        snapshot_id="s1", route_id="r1", observed_at=now,
        source="serpapi_poll", passengers=2, lowest_price=Decimal("485.00"),
    )
    d = ps.to_dict()
    assert d["lowest_price"] == Decimal("485.00")
    assert d["source"] == "serpapi_poll"


def test_price_snapshot_from_row():
    columns = ["snapshot_id", "route_id", "observed_at", "source", "passengers",
               "lowest_price", "currency"]
    now = datetime(2026, 1, 1)
    row = ("s1", "r1", now, "serpapi_poll", 2, Decimal("500"), "EUR")
    ps = PriceSnapshot.from_row(row, columns)
    assert ps.lowest_price == Decimal("500")


# --- Deal ---

def test_deal_defaults():
    d = Deal(deal_id="d1", snapshot_id="s1", route_id="r1")
    assert d.alert_sent is False
    assert d.booked is False
    assert d.score is None


def test_deal_to_dict():
    d = Deal(deal_id="d1", snapshot_id="s1", route_id="r1", score=Decimal("0.85"), urgency="book_now")
    result = d.to_dict()
    assert result["score"] == Decimal("0.85")
    assert result["urgency"] == "book_now"


def test_deal_from_row():
    columns = ["deal_id", "snapshot_id", "route_id", "score", "urgency", "alert_sent", "booked"]
    row = ("d1", "s1", "r1", Decimal("0.9"), "book_now", True, False)
    d = Deal.from_row(row, columns)
    assert d.alert_sent is True
    assert d.score == Decimal("0.9")


# --- AlertRule ---

def test_alert_rule_defaults():
    ar = AlertRule(rule_id="a1", route_id="r1", rule_type="price_drop")
    assert ar.channel == "ha_notify"
    assert ar.active is True


def test_alert_rule_to_dict():
    ar = AlertRule(rule_id="a1", route_id="r1", rule_type="price_drop", threshold=Decimal("500"))
    d = ar.to_dict()
    assert d["threshold"] == Decimal("500")


def test_alert_rule_from_row():
    columns = ["rule_id", "route_id", "rule_type", "threshold", "channel", "active"]
    row = ("a1", "r1", "price_drop", Decimal("400"), "ha_notify", True)
    ar = AlertRule.from_row(row, columns)
    assert ar.threshold == Decimal("400")
