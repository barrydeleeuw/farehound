from __future__ import annotations

import asyncio
from datetime import date, datetime, UTC, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.config import Route as ConfigRoute
from src.orchestrator import Orchestrator, _config_route_to_db, _generate_weekend_windows
from src.storage.models import Route as DBRoute, PriceSnapshot


def test_config_route_to_db_basic():
    config_route = ConfigRoute(
        id="ams-nrt-oct",
        origin="AMS",
        destination="NRT",
        trip_type="round_trip",
        earliest_departure="2026-10-01",
        latest_return="2026-10-31",
        date_flexibility_days=3,
        max_stops=1,
        passengers=2,
        preferred_airlines=["KLM"],
        notes="Japan trip",
    )
    db_route = _config_route_to_db(config_route)
    assert db_route.route_id == "ams-nrt-oct"
    assert db_route.origin == "AMS"
    assert db_route.destination == "NRT"
    assert db_route.earliest_departure == date(2026, 10, 1)
    assert db_route.latest_return == date(2026, 10, 31)
    assert db_route.date_flex_days == 3
    assert db_route.max_stops == 1
    assert db_route.passengers == 2
    assert db_route.preferred_airlines == ["KLM"]
    assert db_route.notes == "Japan trip"
    assert db_route.active is True


def test_config_route_to_db_no_dates():
    config_route = ConfigRoute(
        id="r1",
        origin="AMS",
        destination="IST",
    )
    db_route = _config_route_to_db(config_route)
    assert db_route.earliest_departure is None
    assert db_route.latest_return is None


def test_config_route_to_db_maps_flexibility():
    """date_flexibility_days in config maps to date_flex_days in DB model."""
    config_route = ConfigRoute(
        id="r1",
        origin="AMS",
        destination="IST",
        date_flexibility_days=7,
    )
    db_route = _config_route_to_db(config_route)
    assert db_route.date_flex_days == 7


# --- Shared polling tests ---

def _make_route(route_id, origin="AMS", dest="NRT", passengers=2, trip_type="round_trip"):
    return DBRoute(
        route_id=route_id,
        origin=origin,
        destination=dest,
        trip_type=trip_type,
        earliest_departure=date(2026, 10, 1),
        latest_return=date(2026, 10, 31),
        passengers=passengers,
        active=True,
    )


def _make_user(user_id, chat_id, name="test"):
    return {
        "user_id": user_id,
        "telegram_chat_id": chat_id,
        "name": name,
        "home_airport": "AMS",
        "preferences": None,
        "onboarded": True,
        "active": True,
    }


def _make_serpapi_result(lowest_price=500):
    """Create a mock SerpAPI result."""
    result = MagicMock()
    result.price_insights = {
        "lowest_price": lowest_price,
        "typical_price_range": [400, 800],
        "price_level": "low",
    }
    result.best_flights = [{"flights": [{"airline": "KL"}]}]
    result.other_flights = []
    result.search_params = {"origin": "AMS"}
    result.raw_response = {"search_metadata": {"google_flights_url": "https://flights.google.com"}}
    return result


def _make_orchestrator_with_mocks():
    """Create an Orchestrator with all external deps mocked."""
    config = MagicMock()
    config.serpapi.api_key = "test"
    config.serpapi.currency = "EUR"
    config.anthropic.api_key = "test"
    config.anthropic.model = "test"
    config.telegram_alerts = None
    config.telegram = None
    config.community_feeds = []
    config.scoring.poll_interval_hours = 6
    config.scoring.digest_time = (8, 0)
    config.scoring.alert_threshold = 0.75
    config.scoring.watch_threshold = 0.50
    config.traveller.name = "Barry"
    config.traveller.home_airport = "AMS"
    config.traveller.preferences = []
    config.airports = []
    config.routes = []

    with patch("src.orchestrator.SerpAPIClient"), \
         patch("src.orchestrator.DealScorer"), \
         patch("src.orchestrator.Database") as mock_db_cls:
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        orch = Orchestrator(config)

    return orch, mock_db


@pytest.mark.asyncio
async def test_poll_routes_shared_polling_dedup():
    """Two users watching AMS→NRT should result in ONE SerpAPI call."""
    orch, mock_db = _make_orchestrator_with_mocks()

    user1 = _make_user("u1", "chat1", "Alice")
    user2 = _make_user("u2", "chat2", "Bob")
    route1 = _make_route("r1-alice", "AMS", "NRT")
    route2 = _make_route("r2-bob", "AMS", "NRT")

    mock_db.get_all_active_users.return_value = [user1, user2]
    mock_db.get_active_routes.side_effect = lambda uid: {
        "u1": [route1], "u2": [route2]
    }[uid]

    # Mock window generation to return same windows for both
    windows = [(date(2026, 10, 5), date(2026, 10, 19))]
    orch._generate_windows_for_route = MagicMock(return_value=windows)
    orch._select_windows = AsyncMock(return_value=windows)

    # Mock SerpAPI
    serpapi_result = _make_serpapi_result(500)
    orch.serpapi.search_flights = AsyncMock(return_value=serpapi_result)

    # Mock _store_result_for_user to avoid DB interactions
    orch._store_result_for_user = AsyncMock()

    await orch.poll_routes()

    # SerpAPI should be called exactly ONCE (shared polling)
    assert orch.serpapi.search_flights.call_count == 1

    # But results should be stored for BOTH users
    assert orch._store_result_for_user.call_count == 2
    stored_users = {call.args[4]["user_id"] for call in orch._store_result_for_user.call_args_list}
    assert stored_users == {"u1", "u2"}


@pytest.mark.asyncio
async def test_poll_routes_different_routes_separate_calls():
    """Users watching different routes get separate SerpAPI calls."""
    orch, mock_db = _make_orchestrator_with_mocks()

    user1 = _make_user("u1", "chat1", "Alice")
    user2 = _make_user("u2", "chat2", "Bob")
    route1 = _make_route("r1", "AMS", "NRT")
    route2 = _make_route("r2", "AMS", "IST")

    mock_db.get_all_active_users.return_value = [user1, user2]
    mock_db.get_active_routes.side_effect = lambda uid: {
        "u1": [route1], "u2": [route2]
    }[uid]

    windows = [(date(2026, 10, 5), date(2026, 10, 19))]
    orch._generate_windows_for_route = MagicMock(return_value=windows)
    orch._select_windows = AsyncMock(return_value=windows)

    serpapi_result = _make_serpapi_result(500)
    orch.serpapi.search_flights = AsyncMock(return_value=serpapi_result)
    orch._store_result_for_user = AsyncMock()

    await orch.poll_routes()

    # Two different destinations = two SerpAPI calls
    assert orch.serpapi.search_flights.call_count == 2
    # Each result stored once
    assert orch._store_result_for_user.call_count == 2


@pytest.mark.asyncio
async def test_poll_routes_no_users():
    """poll_routes returns early when no active users exist."""
    orch, mock_db = _make_orchestrator_with_mocks()
    mock_db.get_all_active_users.return_value = []

    await orch.poll_routes()

    orch.serpapi.search_flights.assert_not_called()


@pytest.mark.asyncio
async def test_send_daily_digest_per_user():
    """Daily digest is sent per user to their chat_id."""
    orch, mock_db = _make_orchestrator_with_mocks()

    user1 = _make_user("u1", "chat1", "Alice")
    user2 = _make_user("u2", "chat2", "Bob")
    route1 = _make_route("r1", "AMS", "NRT")
    route2 = _make_route("r2", "AMS", "IST")

    mock_db.get_all_active_users.return_value = [user1, user2]
    mock_db.get_active_routes.side_effect = lambda uid: {
        "u1": [route1], "u2": [route2]
    }[uid]

    # Mock latest snapshot
    snapshot = MagicMock()
    snapshot.lowest_price = Decimal("500")
    snapshot.outbound_date = date(2026, 10, 5)
    snapshot.return_date = date(2026, 10, 19)
    snapshot.currency = "EUR"
    snapshot.observed_at = datetime.now(UTC)
    mock_db.get_latest_snapshot.return_value = snapshot
    mock_db.get_price_history.return_value = {"avg_price": 600, "count": 10}
    mock_db.get_deals_since.return_value = []
    mock_db.get_cheapest_recent_snapshot.return_value = snapshot
    # Smart digest: routes must have pending deals to be included
    mock_db.get_routes_with_pending_deals.side_effect = lambda uid: {
        "u1": {"r1": 500.0}, "u2": {"r2": 500.0}
    }[uid]

    # Set up notifier mock
    orch.telegram_notifier = AsyncMock()

    await orch.send_daily_digest()

    # Digest should be sent twice (once per user)
    assert orch.telegram_notifier.send_daily_digest.call_count == 2
    # Check chat_ids
    chat_ids = {
        call.kwargs["chat_id"]
        for call in orch.telegram_notifier.send_daily_digest.call_args_list
    }
    assert chat_ids == {"chat1", "chat2"}


@pytest.mark.asyncio
async def test_check_alerts_sends_to_user_chat_id():
    """Alert is sent to the specific user's chat_id."""
    orch, mock_db = _make_orchestrator_with_mocks()
    orch.config.scoring.alert_threshold = 0.75
    orch.config.scoring.watch_threshold = 0.50
    orch._cycle_best_prices = {}

    user = _make_user("u1", "chat-alice", "Alice")

    route = _make_route("r1", "AMS", "NRT")
    snapshot = PriceSnapshot(
        snapshot_id="snap1",
        route_id="r1",
        observed_at=datetime.now(UTC),
        source="serpapi_poll",
        passengers=2,
        outbound_date=date(2026, 10, 5),
        return_date=date(2026, 10, 19),
        lowest_price=Decimal("400"),
        currency="EUR",
        best_flight={"flights": [{"airline": "KL"}]},
        all_flights=[{"flights": [{"airline": "KL"}]}],
        price_level="low",
        search_params={"google_flights_url": "https://example.com"},
    )

    # Mock DB calls
    mock_db.get_price_history.return_value = {"avg_price": 600, "count": 10}
    mock_db.get_recent_feedback.return_value = []
    mock_db.get_last_alerted_price.return_value = None
    mock_db.detect_price_inflection.return_value = (False, None)
    mock_db.get_airport_transport.return_value = None
    mock_db.insert_deal.return_value = None

    # Mock scorer
    from src.analysis.scorer import DealScore
    orch.scorer.score_deal = AsyncMock(return_value=DealScore(
        score=0.85, urgency="book_now", reasoning="Great deal", booking_window_hours=48,
    ))

    # Set up notifier
    orch.telegram_notifier = AsyncMock()

    mock_db.get_secondary_airports.return_value = []

    await orch._check_alerts(route, snapshot, 400.0, None, user)

    # Alert is deferred, not sent immediately
    assert "r1" in orch._pending_alerts

    # Send the deferred alert
    await orch._send_deferred_alert(orch._pending_alerts["r1"])

    # Alert sent to Alice's chat_id
    orch.telegram_notifier.send_deal_alert.assert_called_once()
    call_kwargs = orch.telegram_notifier.send_deal_alert.call_args
    assert call_kwargs.kwargs["chat_id"] == "chat-alice"


@pytest.mark.asyncio
async def test_secondary_airports_uses_user_id():
    """Secondary airport polling uses user_id for DB lookups."""
    orch, mock_db = _make_orchestrator_with_mocks()

    user = _make_user("u1", "chat1", "Alice")
    route = _make_route("r1", "AMS", "NRT")
    windows = [(date(2026, 10, 5), date(2026, 10, 19))]

    mock_db.get_secondary_airports.return_value = []  # No secondary airports

    await orch._poll_secondary_airports(route, windows, user)

    # Verify user_id was passed
    mock_db.get_secondary_airports.assert_called_once_with("u1")


@pytest.mark.asyncio
async def test_shared_polling_different_passengers():
    """Same route but different passenger counts = separate API calls."""
    orch, mock_db = _make_orchestrator_with_mocks()

    user1 = _make_user("u1", "chat1")
    user2 = _make_user("u2", "chat2")
    route1 = _make_route("r1", "AMS", "NRT", passengers=2)
    route2 = _make_route("r2", "AMS", "NRT", passengers=1)

    mock_db.get_all_active_users.return_value = [user1, user2]
    mock_db.get_active_routes.side_effect = lambda uid: {
        "u1": [route1], "u2": [route2]
    }[uid]

    windows = [(date(2026, 10, 5), date(2026, 10, 19))]
    orch._generate_windows_for_route = MagicMock(return_value=windows)
    orch._select_windows = AsyncMock(return_value=windows)

    serpapi_result = _make_serpapi_result(500)
    orch.serpapi.search_flights = AsyncMock(return_value=serpapi_result)
    orch._store_result_for_user = AsyncMock()

    await orch.poll_routes()

    # Different passenger counts = separate calls
    assert orch.serpapi.search_flights.call_count == 2
