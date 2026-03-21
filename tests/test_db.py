from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from src.storage.db import Database
from src.storage.models import Deal, PollWindow, PriceSnapshot, Route


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "test.duckdb")
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def sample_route():
    return Route(
        route_id="ams-nrt",
        origin="AMS",
        destination="NRT",
        trip_type="round_trip",
        earliest_departure=date(2026, 10, 1),
        latest_return=date(2026, 10, 31),
        passengers=2,
    )


# --- init_schema ---

def test_init_schema(db):
    # Should not raise on double init
    db.init_schema()


# --- upsert_route / get_active_routes ---

def test_upsert_and_get_routes(db, sample_route):
    db.upsert_route(sample_route)
    routes = db.get_active_routes()
    assert len(routes) == 1
    assert routes[0].route_id == "ams-nrt"
    assert routes[0].origin == "AMS"


def test_upsert_route_update(db, sample_route):
    db.upsert_route(sample_route)
    sample_route.notes = "updated"
    db.upsert_route(sample_route)
    routes = db.get_active_routes()
    assert len(routes) == 1
    assert routes[0].notes == "updated"


def test_get_active_routes_excludes_inactive(db, sample_route):
    db.upsert_route(sample_route)
    inactive = Route(route_id="inactive", origin="LHR", destination="JFK", active=False)
    db.upsert_route(inactive)
    routes = db.get_active_routes()
    assert len(routes) == 1
    assert routes[0].route_id == "ams-nrt"


# --- insert_snapshot / get_recent_snapshots ---

def test_insert_and_get_snapshots(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1",
        route_id="ams-nrt",
        observed_at=now,
        source="serpapi_poll",
        passengers=2,
        outbound_date=date(2026, 10, 1),
        return_date=date(2026, 10, 15),
        lowest_price=Decimal("485.00"),
        currency="EUR",
    )
    db.insert_snapshot(snap)
    recent = db.get_recent_snapshots("ams-nrt", limit=5)
    assert len(recent) == 1
    assert recent[0].snapshot_id == "s1"
    assert recent[0].lowest_price == Decimal("485.00")


def test_get_recent_snapshots_ordering(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    for i in range(3):
        snap = PriceSnapshot(
            snapshot_id=f"s{i}",
            route_id="ams-nrt",
            observed_at=now - timedelta(hours=i),
            source="serpapi_poll",
            passengers=2,
            lowest_price=Decimal(str(400 + i * 10)),
        )
        db.insert_snapshot(snap)
    recent = db.get_recent_snapshots("ams-nrt", limit=2)
    assert len(recent) == 2
    assert recent[0].snapshot_id == "s0"  # most recent first


# --- get_price_history ---

def test_get_price_history(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    for i, price in enumerate([400, 500, 600]):
        snap = PriceSnapshot(
            snapshot_id=f"s{i}",
            route_id="ams-nrt",
            observed_at=now - timedelta(days=i),
            source="serpapi_poll",
            passengers=2,
            lowest_price=Decimal(str(price)),
        )
        db.insert_snapshot(snap)
    history = db.get_price_history("ams-nrt", days=90)
    assert history["count"] == 3
    assert float(history["min_price"]) == 400.0
    assert float(history["max_price"]) == 600.0
    assert abs(float(history["avg_price"]) - 500.0) < 0.01


def test_get_price_history_empty(db, sample_route):
    db.upsert_route(sample_route)
    history = db.get_price_history("ams-nrt")
    assert history["count"] == 0
    assert history["avg_price"] is None


# --- get_latest_snapshot ---

def test_get_latest_snapshot(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    for i in range(3):
        snap = PriceSnapshot(
            snapshot_id=f"s{i}",
            route_id="ams-nrt",
            observed_at=now - timedelta(hours=i),
            source="serpapi_poll",
            passengers=2,
            lowest_price=Decimal(str(400 + i * 10)),
        )
        db.insert_snapshot(snap)
    latest = db.get_latest_snapshot("ams-nrt")
    assert latest is not None
    assert latest.snapshot_id == "s0"


def test_get_latest_snapshot_none(db, sample_route):
    db.upsert_route(sample_route)
    assert db.get_latest_snapshot("ams-nrt") is None


def test_get_latest_snapshot_ignores_null_price(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    # Snapshot with null price
    snap_null = PriceSnapshot(
        snapshot_id="s0",
        route_id="ams-nrt",
        observed_at=now,
        source="serpapi_poll",
        passengers=2,
        lowest_price=None,
    )
    snap_priced = PriceSnapshot(
        snapshot_id="s1",
        route_id="ams-nrt",
        observed_at=now - timedelta(hours=1),
        source="serpapi_poll",
        passengers=2,
        lowest_price=Decimal("500"),
    )
    db.insert_snapshot(snap_null)
    db.insert_snapshot(snap_priced)
    latest = db.get_latest_snapshot("ams-nrt")
    assert latest.snapshot_id == "s1"


# --- insert_deal / get_deals_since ---

def test_insert_and_get_deals(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now,
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)

    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), urgency="book_now",
        reasoning="Great deal",
    )
    db.insert_deal(deal)

    since = now - timedelta(hours=1)
    deals = db.get_deals_since("ams-nrt", since)
    assert len(deals) == 1
    assert deals[0].deal_id == "d1"


def test_get_deals_since_filters_old(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now,
        source="test", passengers=2,
    )
    db.insert_snapshot(snap)
    deal = Deal(deal_id="d1", snapshot_id="s1", route_id="ams-nrt")
    db.insert_deal(deal)

    # Querying from the future should return nothing
    future = now + timedelta(days=1)
    deals = db.get_deals_since("ams-nrt", future)
    assert len(deals) == 0


# --- poll_windows ---

def test_update_and_get_poll_windows(db, sample_route):
    db.upsert_route(sample_route)
    db.update_poll_window("ams-nrt", date(2026, 10, 1), date(2026, 10, 15), 485.0)
    windows = db.get_poll_windows("ams-nrt")
    assert len(windows) == 1
    assert windows[0].outbound_date == date(2026, 10, 1)
    assert float(windows[0].lowest_seen_price) == 485.0


def test_update_poll_window_lowers_price(db, sample_route):
    db.upsert_route(sample_route)
    db.update_poll_window("ams-nrt", date(2026, 10, 1), date(2026, 10, 15), 500.0)
    db.update_poll_window("ams-nrt", date(2026, 10, 1), date(2026, 10, 15), 450.0)
    windows = db.get_poll_windows("ams-nrt")
    assert float(windows[0].lowest_seen_price) == 450.0


def test_update_poll_window_keeps_lower(db, sample_route):
    db.upsert_route(sample_route)
    db.update_poll_window("ams-nrt", date(2026, 10, 1), date(2026, 10, 15), 400.0)
    db.update_poll_window("ams-nrt", date(2026, 10, 1), date(2026, 10, 15), 500.0)
    windows = db.get_poll_windows("ams-nrt")
    assert float(windows[0].lowest_seen_price) == 400.0


# --- update_deal_feedback / get_recent_feedback ---

def test_update_deal_feedback(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now,
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), urgency="book_now", reasoning="Great deal",
    )
    db.insert_deal(deal)

    db.update_deal_feedback("d1", "booked")

    # Verify feedback was stored by re-reading deals
    since = now - timedelta(hours=1)
    deals = db.get_deals_since("ams-nrt", since)
    assert len(deals) == 1
    assert deals[0].feedback == "booked"


def test_update_deal_feedback_overwrite(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now,
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), urgency="book_now",
    )
    db.insert_deal(deal)

    db.update_deal_feedback("d1", "dismissed")
    db.update_deal_feedback("d1", "booked")

    since = now - timedelta(hours=1)
    deals = db.get_deals_since("ams-nrt", since)
    assert deals[0].feedback == "booked"


def test_get_recent_feedback_with_data(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now,
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), urgency="book_now", reasoning="Great deal",
        feedback="booked",
    )
    db.insert_deal(deal)

    feedback = db.get_recent_feedback(limit=10)
    assert len(feedback) == 1
    assert feedback[0]["deal_id"] == "d1"
    assert feedback[0]["feedback"] == "booked"
    assert feedback[0]["origin"] == "AMS"
    assert feedback[0]["destination"] == "NRT"
    assert float(feedback[0]["price"]) == 400.0
    assert float(feedback[0]["score"]) == 0.85


def test_get_recent_feedback_empty(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now,
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)
    # Deal without feedback
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), urgency="book_now",
    )
    db.insert_deal(deal)

    feedback = db.get_recent_feedback()
    assert feedback == []


def test_get_recent_feedback_respects_limit(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now,
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)

    for i in range(5):
        deal = Deal(
            deal_id=f"d{i}", snapshot_id="s1", route_id="ams-nrt",
            score=Decimal("0.80"), urgency="book_now", feedback="booked",
        )
        db.insert_deal(deal)

    feedback = db.get_recent_feedback(limit=3)
    assert len(feedback) == 3
