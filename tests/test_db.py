from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from src.storage.db import Database
from src.storage.models import Deal, PollWindow, PriceSnapshot, Route


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "test.db")
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


# --- get_cheapest_recent_snapshot ---

def test_get_cheapest_recent_snapshot(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    # Insert 3 snapshots: most recent is NOT cheapest
    for i, (price, hours_ago) in enumerate([(500, 1), (400, 24), (600, 48)]):
        snap = PriceSnapshot(
            snapshot_id=f"s{i}",
            route_id="ams-nrt",
            observed_at=now - timedelta(hours=hours_ago),
            source="serpapi_poll",
            passengers=2,
            outbound_date=date(2026, 10, 1),
            return_date=date(2026, 10, 15),
            lowest_price=Decimal(str(price)),
        )
        db.insert_snapshot(snap)
    cheapest = db.get_cheapest_recent_snapshot("ams-nrt", days=7)
    assert cheapest is not None
    assert cheapest.snapshot_id == "s1"  # €400, the cheapest
    assert float(cheapest.lowest_price) == 400.0


def test_get_cheapest_recent_snapshot_excludes_old(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    # Old cheap snapshot (10 days ago) should be excluded
    old_snap = PriceSnapshot(
        snapshot_id="s_old",
        route_id="ams-nrt",
        observed_at=now - timedelta(days=10),
        source="serpapi_poll",
        passengers=2,
        lowest_price=Decimal("100"),
    )
    recent_snap = PriceSnapshot(
        snapshot_id="s_recent",
        route_id="ams-nrt",
        observed_at=now - timedelta(hours=1),
        source="serpapi_poll",
        passengers=2,
        lowest_price=Decimal("500"),
    )
    db.insert_snapshot(old_snap)
    db.insert_snapshot(recent_snap)
    cheapest = db.get_cheapest_recent_snapshot("ams-nrt", days=7)
    assert cheapest is not None
    assert cheapest.snapshot_id == "s_recent"


def test_get_cheapest_recent_snapshot_none(db, sample_route):
    db.upsert_route(sample_route)
    assert db.get_cheapest_recent_snapshot("ams-nrt") is None


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


# --- get_last_alerted_price ---

def test_get_last_alerted_price_none(db, sample_route):
    db.upsert_route(sample_route)
    assert db.get_last_alerted_price("ams-nrt") is None


def test_get_last_alerted_price_returns_latest(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)

    # First alerted deal at €500
    snap1 = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now - timedelta(hours=2),
        source="serpapi_poll", passengers=2, lowest_price=Decimal("500"),
    )
    db.insert_snapshot(snap1)
    deal1 = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), urgency="book_now",
        alert_sent=True, alert_sent_at=now - timedelta(hours=2),
    )
    db.insert_deal(deal1)

    # Second alerted deal at €450
    snap2 = PriceSnapshot(
        snapshot_id="s2", route_id="ams-nrt", observed_at=now - timedelta(hours=1),
        source="serpapi_poll", passengers=2, lowest_price=Decimal("450"),
    )
    db.insert_snapshot(snap2)
    deal2 = Deal(
        deal_id="d2", snapshot_id="s2", route_id="ams-nrt",
        score=Decimal("0.90"), urgency="book_now",
        alert_sent=True, alert_sent_at=now - timedelta(hours=1),
    )
    db.insert_deal(deal2)

    assert db.get_last_alerted_price("ams-nrt") == 450.0


def test_get_last_alerted_price_ignores_non_alerted(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)

    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now,
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.60"), urgency="watch",
        alert_sent=False,
    )
    db.insert_deal(deal)

    assert db.get_last_alerted_price("ams-nrt") is None


# --- detect_price_inflection ---

def test_detect_inflection_not_enough_data(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)

    # Only 3 snapshots — need at least 4
    for i, price in enumerate([420, 410, 400]):
        snap = PriceSnapshot(
            snapshot_id=f"s{i}", route_id="ams-nrt",
            observed_at=now - timedelta(hours=3 - i),
            source="serpapi_poll", passengers=2,
            lowest_price=Decimal(str(price)),
        )
        db.insert_snapshot(snap)

    detected, bottom = db.detect_price_inflection("ams-nrt")
    assert detected is False
    assert bottom is None


def test_detect_inflection_price_still_dropping(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)

    # Still dropping: 500, 480, 460, 440
    for i, price in enumerate([500, 480, 460, 440]):
        snap = PriceSnapshot(
            snapshot_id=f"s{i}", route_id="ams-nrt",
            observed_at=now - timedelta(hours=4 - i),
            source="serpapi_poll", passengers=2,
            lowest_price=Decimal(str(price)),
        )
        db.insert_snapshot(snap)

    detected, bottom = db.detect_price_inflection("ams-nrt")
    assert detected is False


def test_detect_inflection_true(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)

    # Dropping then uptick: 500, 480, 460, 470 (most recent)
    prices = [500, 480, 460, 470]
    for i, price in enumerate(prices):
        snap = PriceSnapshot(
            snapshot_id=f"s{i}", route_id="ams-nrt",
            observed_at=now - timedelta(hours=len(prices) - i),
            source="serpapi_poll", passengers=2,
            lowest_price=Decimal(str(price)),
        )
        db.insert_snapshot(snap)

    detected, bottom = db.detect_price_inflection("ams-nrt")
    assert detected is True
    assert bottom == 460.0


def test_detect_inflection_no_uptick(db, sample_route):
    """Flat at the bottom is not an inflection."""
    db.upsert_route(sample_route)
    now = datetime.now(UTC)

    # 500, 480, 460, 460 — no uptick
    prices = [500, 480, 460, 460]
    for i, price in enumerate(prices):
        snap = PriceSnapshot(
            snapshot_id=f"s{i}", route_id="ams-nrt",
            observed_at=now - timedelta(hours=len(prices) - i),
            source="serpapi_poll", passengers=2,
            lowest_price=Decimal(str(price)),
        )
        db.insert_snapshot(snap)

    detected, bottom = db.detect_price_inflection("ams-nrt")
    assert detected is False


# --- Airport Transport ---

SAMPLE_AIRPORTS = [
    {
        "code": "AMS",
        "name": "Amsterdam Schiphol",
        "transport_mode": "train",
        "transport_cost_eur": 12,
        "transport_time_min": 45,
        "parking_cost_eur": None,
        "is_primary": True,
    },
    {
        "code": "BRU",
        "name": "Brussels",
        "transport_mode": "Thalys",
        "transport_cost_eur": 70,
        "transport_time_min": 150,
        "parking_cost_eur": None,
        "is_primary": False,
    },
    {
        "code": "EIN",
        "name": "Eindhoven",
        "transport_mode": "car",
        "transport_cost_eur": 30,
        "transport_time_min": 50,
        "parking_cost_eur": 50,
        "is_primary": False,
    },
]


def test_seed_airport_transport(db):
    db.seed_airport_transport(SAMPLE_AIRPORTS)
    all_airports = db.get_all_airport_transports()
    assert len(all_airports) == 3


def test_seed_airport_transport_upsert(db):
    db.seed_airport_transport(SAMPLE_AIRPORTS)
    updated = [{"code": "AMS", "name": "Schiphol Updated", "transport_mode": "train",
                "transport_cost_eur": 15, "transport_time_min": 45, "is_primary": True}]
    db.seed_airport_transport(updated)
    ams = db.get_airport_transport("AMS")
    assert ams["airport_name"] == "Schiphol Updated"
    assert ams["transport_cost_eur"] == 15
    assert db.get_all_airport_transports().__len__() == 3


def test_get_airport_transport(db):
    db.seed_airport_transport(SAMPLE_AIRPORTS)
    ams = db.get_airport_transport("AMS")
    assert ams is not None
    assert ams["airport_code"] == "AMS"
    assert ams["airport_name"] == "Amsterdam Schiphol"
    assert ams["transport_cost_eur"] == 12
    assert ams["is_primary"] is True


def test_get_airport_transport_not_found(db):
    db.seed_airport_transport(SAMPLE_AIRPORTS)
    assert db.get_airport_transport("JFK") is None


def test_get_all_airport_transports(db):
    db.seed_airport_transport(SAMPLE_AIRPORTS)
    all_airports = db.get_all_airport_transports()
    assert len(all_airports) == 3
    codes = {a["airport_code"] for a in all_airports}
    assert codes == {"AMS", "BRU", "EIN"}


def test_get_primary_airport(db):
    db.seed_airport_transport(SAMPLE_AIRPORTS)
    primary = db.get_primary_airport()
    assert primary is not None
    assert primary["airport_code"] == "AMS"
    assert primary["is_primary"] is True


def test_get_primary_airport_none(db):
    non_primary = [{"code": "BRU", "name": "Brussels", "is_primary": False}]
    db.seed_airport_transport(non_primary)
    assert db.get_primary_airport() is None


def test_get_secondary_airports(db):
    db.seed_airport_transport(SAMPLE_AIRPORTS)
    secondary = db.get_secondary_airports()
    assert len(secondary) == 2
    codes = {a["airport_code"] for a in secondary}
    assert codes == {"BRU", "EIN"}
    for ap in secondary:
        assert ap["is_primary"] is False


def test_get_secondary_airports_empty(db):
    primary_only = [{"code": "AMS", "name": "Amsterdam", "is_primary": True}]
    db.seed_airport_transport(primary_only)
    assert db.get_secondary_airports() == []


def test_airport_transport_parking_cost(db):
    db.seed_airport_transport(SAMPLE_AIRPORTS)
    ein = db.get_airport_transport("EIN")
    assert ein["parking_cost_eur"] == 50
    ams = db.get_airport_transport("AMS")
    assert ams["parking_cost_eur"] is None


# --- get_deals_pending_feedback ---

def test_get_deals_pending_feedback(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now - timedelta(days=4),
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)
    # Deal alerted 4 days ago, no feedback
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), alert_sent=True,
        alert_sent_at=now - timedelta(days=4),
    )
    db.insert_deal(deal)

    pending = db.get_deals_pending_feedback(older_than_days=3)
    assert len(pending) == 1
    assert pending[0]["deal_id"] == "d1"
    assert pending[0]["origin"] == "AMS"
    assert float(pending[0]["price"]) == 400.0


def test_get_deals_pending_feedback_excludes_recent(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now - timedelta(hours=12),
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)
    # Deal alerted 12 hours ago — too recent
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), alert_sent=True,
        alert_sent_at=now - timedelta(hours=12),
    )
    db.insert_deal(deal)

    pending = db.get_deals_pending_feedback(older_than_days=3)
    assert len(pending) == 0


def test_get_deals_pending_feedback_excludes_with_feedback(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now - timedelta(days=4),
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)
    # Deal with feedback already provided
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), alert_sent=True,
        alert_sent_at=now - timedelta(days=4), feedback="booked",
    )
    db.insert_deal(deal)

    pending = db.get_deals_pending_feedback(older_than_days=3)
    assert len(pending) == 0


# --- User CRUD ---

def test_create_and_get_user(db):
    user_id = db.create_user("12345", name="Alice")
    assert user_id is not None
    user = db.get_user(user_id)
    assert user["name"] == "Alice"
    assert user["telegram_chat_id"] == "12345"
    assert user["active"] is True
    assert user["onboarded"] is False


def test_get_user_by_chat_id(db):
    user_id = db.create_user("67890", name="Bob")
    user = db.get_user_by_chat_id("67890")
    assert user is not None
    assert user["user_id"] == user_id
    assert user["name"] == "Bob"


def test_get_user_by_chat_id_not_found(db):
    assert db.get_user_by_chat_id("nonexistent") is None


def test_update_user(db):
    user_id = db.create_user("111", name="Carol")
    db.update_user(user_id, name="Caroline", home_airport="LHR", onboarded=1)
    user = db.get_user(user_id)
    assert user["name"] == "Caroline"
    assert user["home_airport"] == "LHR"
    assert user["onboarded"] is True


def test_get_all_active_users(db):
    db.create_user("u1", name="One")
    uid2 = db.create_user("u2", name="Two")
    db.create_user("u3", name="Three")
    db.update_user(uid2, active=0)
    users = db.get_all_active_users()
    assert len(users) == 2
    names = {u["name"] for u in users}
    assert names == {"One", "Three"}


# --- Multi-user scoping ---

def test_routes_scoped_by_user(db):
    uid1 = db.create_user("a1", name="User1")
    uid2 = db.create_user("a2", name="User2")
    r1 = Route(route_id="r1", origin="AMS", destination="NRT")
    r2 = Route(route_id="r2", origin="LHR", destination="JFK")
    db.upsert_route(r1, user_id=uid1)
    db.upsert_route(r2, user_id=uid2)
    # Without user_id, get both
    assert len(db.get_active_routes()) == 2
    # With user_id, get only own
    assert len(db.get_active_routes(user_id=uid1)) == 1
    assert db.get_active_routes(user_id=uid1)[0].route_id == "r1"
    assert len(db.get_active_routes(user_id=uid2)) == 1
    assert db.get_active_routes(user_id=uid2)[0].route_id == "r2"


# --- follow-up spam fix: follow_up_count ---

def test_get_deals_pending_feedback_excludes_max_followups(db, sample_route):
    """Deals with follow_up_count >= 2 should be excluded from pending feedback."""
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now - timedelta(days=4),
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), alert_sent=True,
        alert_sent_at=now - timedelta(days=4),
    )
    db.insert_deal(deal)

    # Simulate 2 follow-ups already sent
    db.mark_follow_up_sent("d1")
    db.mark_follow_up_sent("d1")

    pending = db.get_deals_pending_feedback(older_than_days=3)
    assert len(pending) == 0


def test_mark_follow_up_sent_increments_counter(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now - timedelta(days=4),
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), alert_sent=True,
        alert_sent_at=now - timedelta(days=4),
    )
    db.insert_deal(deal)

    db.mark_follow_up_sent("d1")
    # Check count via raw SQL
    row = db._conn.execute("SELECT follow_up_count FROM deals WHERE deal_id = 'd1'").fetchone()
    assert row[0] == 1

    db.mark_follow_up_sent("d1")
    row = db._conn.execute("SELECT follow_up_count FROM deals WHERE deal_id = 'd1'").fetchone()
    assert row[0] == 2


def test_expire_stale_deals(db, sample_route):
    db.upsert_route(sample_route)
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt", observed_at=now - timedelta(days=10),
        source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
    )
    db.insert_snapshot(snap)
    # Deal with 2 follow-ups and no feedback
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), alert_sent=True,
        alert_sent_at=now - timedelta(days=10),
    )
    db.insert_deal(deal)
    db.mark_follow_up_sent("d1")
    db.mark_follow_up_sent("d1")

    # Deal with feedback should NOT be expired
    deal2 = Deal(
        deal_id="d2", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.80"), alert_sent=True,
        alert_sent_at=now - timedelta(days=10),
        feedback="booked",
    )
    db.insert_deal(deal2)

    db.expire_stale_deals()

    row1 = db._conn.execute("SELECT feedback FROM deals WHERE deal_id = 'd1'").fetchone()
    assert row1[0] == "expired"

    row2 = db._conn.execute("SELECT feedback FROM deals WHERE deal_id = 'd2'").fetchone()
    assert row2[0] == "booked"  # unchanged


def test_default_user_migration(tmp_path):
    """Existing data gets assigned to a default user on init."""
    database = Database(db_path=tmp_path / "migrate.db")
    # Create schema without users table first (simulate old DB)
    database._conn.executescript("""
        CREATE TABLE IF NOT EXISTS routes (
            route_id TEXT PRIMARY KEY,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            trip_type TEXT DEFAULT 'round_trip',
            earliest_departure TEXT,
            latest_return TEXT,
            date_flex_days INTEGER DEFAULT 3,
            max_stops INTEGER DEFAULT 1,
            passengers INTEGER DEFAULT 2,
            preferred_airlines TEXT,
            notes TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            trip_duration_type TEXT,
            trip_duration_days INTEGER,
            preferred_departure_days TEXT,
            preferred_return_days TEXT
        );
    """)
    database._conn.execute(
        "INSERT INTO routes (route_id, origin, destination) VALUES ('r1', 'AMS', 'NRT')"
    )
    database._conn.commit()
    # Now run init_schema which should create users + migrate
    database.init_schema()
    users = database.get_all_active_users()
    assert len(users) == 1
    assert users[0]["name"] == "barry"
    # Route should now have user_id
    routes = database.get_active_routes(user_id=users[0]["user_id"])
    assert len(routes) == 1
    database.close()
