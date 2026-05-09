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
        "approved": True,
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
        "u1": {"r1": {"price": 500.0, "deal_ids": ["d1"]}},
        "u2": {"r2": {"price": 500.0, "deal_ids": ["d2"]}},
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
async def test_check_pending_feedback_groups_by_route():
    """Multiple deals for the same route should produce one follow-up."""
    orch, mock_db = _make_orchestrator_with_mocks()
    orch.telegram_notifier = AsyncMock()

    user = _make_user("u1", "chat1", "Alice")
    route = _make_route("r1", "AMS", "NRT")

    # Two deals for the same route, both pending
    pending_deals = [
        {"deal_id": "d1", "route_id": "r1", "origin": "AMS", "destination": "NRT", "price": 450},
        {"deal_id": "d2", "route_id": "r1", "origin": "AMS", "destination": "NRT", "price": 500},
    ]

    mock_db.get_deals_pending_feedback.return_value = pending_deals
    mock_db.get_all_active_users.return_value = [user]
    mock_db.get_active_routes.return_value = [route]

    await orch._check_pending_feedback()

    # Only ONE follow-up sent (grouped by route)
    assert orch.telegram_notifier.send_follow_up.call_count == 1
    # The follow-up should use the best (lowest) price deal
    call_args = orch.telegram_notifier.send_follow_up.call_args
    assert call_args.args[0]["price"] == 450
    # Both deals should be marked as follow-up sent
    assert mock_db.mark_follow_up_sent.call_count == 2
    # Expire stale deals should be called
    mock_db.expire_stale_deals.assert_called_once()


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


# =============================================================================
# T17 — R7 (ITEM-051): snooze + digest fingerprint + auto-snooze
# =============================================================================

from src.storage.db import Database
from src.bot.commands import TripBot


@pytest.fixture
def real_db(tmp_path):
    """Real Database instance for T17 — exercises snooze filtering at the DB layer."""
    database = Database(db_path=tmp_path / "t17.db")
    database.init_schema()
    yield database
    database.close()


def _seed_user_and_route(db, route_id="r1", origin="AMS", dest="NRT"):
    """Create one approved user with one active route. Returns (user_id, route)."""
    user_id = db.create_user(f"chat-{route_id}", name="Tester")
    db.update_user(user_id, home_airport=origin, onboarded=1, approved=1)
    route = DBRoute(
        route_id=route_id,
        origin=origin,
        destination=dest,
        trip_type="round_trip",
        earliest_departure=date(2026, 10, 1),
        latest_return=date(2026, 10, 31),
        passengers=2,
        active=True,
    )
    db.upsert_route(route, user_id=user_id)
    return user_id, route


# --- Snooze filtering at DB layer (poll_routes & send_daily_digest both rely on this) ---

class TestSnoozeFiltering:

    def test_get_active_routes_excludes_snoozed_by_default(self, real_db):
        user_id, route = _seed_user_and_route(real_db, "ams-nrt")
        real_db.snooze_route("ams-nrt", days=7)

        routes = real_db.get_active_routes(user_id=user_id)
        assert len(routes) == 0

    def test_get_active_routes_includes_snoozed_when_flag_set(self, real_db):
        user_id, route = _seed_user_and_route(real_db, "ams-nrt")
        real_db.snooze_route("ams-nrt", days=7)

        routes = real_db.get_active_routes(user_id=user_id, include_snoozed=True)
        assert len(routes) == 1
        assert routes[0].route_id == "ams-nrt"

    def test_unsnooze_route_re_includes(self, real_db):
        user_id, route = _seed_user_and_route(real_db, "ams-nrt")
        real_db.snooze_route("ams-nrt", days=7)
        assert real_db.get_active_routes(user_id=user_id) == []

        real_db.unsnooze_route("ams-nrt")
        routes = real_db.get_active_routes(user_id=user_id)
        assert len(routes) == 1
        assert routes[0].route_id == "ams-nrt"

    def test_expired_snooze_treated_as_active(self, real_db):
        """A snooze in the past must NOT filter out the route."""
        user_id, route = _seed_user_and_route(real_db, "ams-nrt")
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        real_db._conn.execute(
            "UPDATE routes SET snoozed_until = ? WHERE route_id = ?",
            [past, "ams-nrt"],
        )
        real_db._conn.commit()
        routes = real_db.get_active_routes(user_id=user_id)
        assert len(routes) == 1


# --- Auto-snooze on `booked` feedback (Condition C9) ---

class TestAutoSnoozeOnBooked:

    def _setup(self, real_db):
        user_id, route = _seed_user_and_route(real_db, "ams-nrt")
        # Insert a snapshot + deal that will be marked booked
        snap = PriceSnapshot(
            snapshot_id="s1", route_id="ams-nrt",
            observed_at=datetime.now(UTC),
            source="serpapi_poll", passengers=2,
            lowest_price=Decimal("450"),
        )
        real_db.insert_snapshot(snap)
        from src.storage.models import Deal
        deal = Deal(
            deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
            score=Decimal("0.85"), urgency="book_now",
            alert_sent=True, alert_sent_at=datetime.now(UTC),
        )
        real_db.insert_deal(deal)
        bot = TripBot(
            bot_token="123:ABC",
            db=real_db,
            anthropic_api_key="sk-test",
            anthropic_model="test-model",
        )
        return bot, user_id

    def test_auto_snooze_helper_sets_snoozed_until_30d(self, real_db):
        bot, user_id = self._setup(real_db)

        bot._auto_snooze_route_for_deal("d1", days=30)

        routes = real_db.get_active_routes(user_id=user_id, include_snoozed=True)
        assert routes[0].snoozed_until is not None
        # Should be ~30 days in the future. Model parsing may return tz-naive datetime.
        snoozed = routes[0].snoozed_until
        if snoozed.tzinfo is None:
            snoozed = snoozed.replace(tzinfo=UTC)
        delta_days = (snoozed - datetime.now(UTC)).days
        assert 29 <= delta_days <= 30

    def test_auto_snooze_filters_route_from_get_active_routes(self, real_db):
        bot, user_id = self._setup(real_db)

        bot._auto_snooze_route_for_deal("d1", days=30)

        routes = real_db.get_active_routes(user_id=user_id)
        assert routes == []  # filtered by default include_snoozed=False

    def test_auto_snooze_silent_on_missing_deal(self, real_db):
        """Looking up a non-existent deal does NOT raise."""
        bot, _user_id = self._setup(real_db)
        # Should be silent (try/except around the lookup)
        bot._auto_snooze_route_for_deal("nonexistent", days=30)


# --- Digest fingerprint helpers ---

class TestDigestFingerprintHelpers:

    def test_fingerprint_stable_for_same_summaries(self):
        s1 = [{"route_id": "r1", "lowest_price": 500.0},
              {"route_id": "r2", "lowest_price": 800.0}]
        s2 = [{"route_id": "r2", "lowest_price": 800.0},  # different order
              {"route_id": "r1", "lowest_price": 500.0}]
        assert Orchestrator._compute_digest_fingerprint(s1) == Orchestrator._compute_digest_fingerprint(s2)

    def test_fingerprint_changes_when_price_changes(self):
        s1 = [{"route_id": "r1", "lowest_price": 500.0}]
        s2 = [{"route_id": "r1", "lowest_price": 510.0}]
        assert Orchestrator._compute_digest_fingerprint(s1) != Orchestrator._compute_digest_fingerprint(s2)

    def test_fingerprint_rounds_to_whole_euro(self):
        """Sub-€1 movement does NOT change the fingerprint."""
        s1 = [{"route_id": "r1", "lowest_price": 500.49}]
        s2 = [{"route_id": "r1", "lowest_price": 500.50}]
        # round() banker's rounding: 500.49 → 500, 500.50 → 500 (round-half-even)
        # The contract is "round to whole euro" — both produce same hash
        assert Orchestrator._compute_digest_fingerprint(s1) == Orchestrator._compute_digest_fingerprint(s2)

    def test_format_digest_header_n_routes_m_moves(self):
        deltas = [
            {"route_id": "r1", "origin": "AMS", "destination": "NRT",
             "lowest_price": 1820.0, "delta": -40.0, "is_new_deal": False},
            {"route_id": "r2", "origin": "AMS", "destination": "BKK",
             "lowest_price": 620.0, "delta": None, "is_new_deal": True},
            {"route_id": "r3", "origin": "AMS", "destination": "LIS",
             "lowest_price": 220.0, "delta": 5.0, "is_new_deal": False},
        ]
        header = Orchestrator._format_digest_header(deltas, moved_count=1)
        # Header line — count routes & moves
        assert "3 routes" in header
        assert "1 price moved" in header  # singular
        # Per-route lines
        assert "dropped €40" in header
        assert "new low" in header
        assert "unchanged" in header  # delta=5 is below €10 threshold


# --- Digest skip predicate (full send_daily_digest path) ---

@pytest.mark.asyncio
async def test_digest_skip_when_fingerprint_unchanged_recent(real_db):
    """Skip when: fingerprint matches AND <3 days since last AND no new deals AND price moved <€10."""
    orch, _ = _make_orchestrator_with_mocks()
    orch.db = real_db  # swap mock DB for real one
    orch.telegram_notifier = AsyncMock()

    user_id, route = _seed_user_and_route(real_db, "ams-nrt")
    now = datetime.now(UTC)

    # Insert TWO snapshots so the delta calc has both `latest` and `previous`
    snap_old = PriceSnapshot(
        snapshot_id="s_old", route_id="ams-nrt",
        observed_at=now - timedelta(hours=24),
        source="serpapi_poll", passengers=2,
        outbound_date=date(2026, 10, 5), return_date=date(2026, 10, 19),
        lowest_price=Decimal("500"),
    )
    snap_new = PriceSnapshot(
        snapshot_id="s_new", route_id="ams-nrt",
        observed_at=now,
        source="serpapi_poll", passengers=2,
        outbound_date=date(2026, 10, 5), return_date=date(2026, 10, 19),
        lowest_price=Decimal("502"),  # only €2 move — under threshold
    )
    real_db.insert_snapshot(snap_old)
    real_db.insert_snapshot(snap_new)
    real_db._conn.execute(
        "UPDATE price_snapshots SET user_id = ? WHERE route_id = 'ams-nrt'", [user_id]
    )
    real_db._conn.commit()

    # Existing deal so route shows up in get_routes_with_pending_deals.
    # Push created_at back >1d so recent_deals is empty (skip predicate needs
    # new_deal_count == 0). Test isolates fingerprint matching, not new-deal flag.
    from src.storage.models import Deal
    deal = Deal(
        deal_id="d1", snapshot_id="s_new", route_id="ams-nrt",
        score=Decimal("0.85"), urgency="book_now",
        alert_sent=True, alert_sent_at=now - timedelta(days=2),
    )
    real_db.insert_deal(deal)
    real_db._conn.execute(
        "UPDATE deals SET user_id = ?, created_at = ? WHERE deal_id = 'd1'",
        [user_id, (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")],
    )
    real_db._conn.commit()

    # Pre-compute the fingerprint that send_daily_digest will calculate.
    # The digest summary uses `cheapest_recent_snapshot` (not latest), so €500.
    summary_for_fp = [{"route_id": "ams-nrt", "lowest_price": 500.0}]
    expected_fp = Orchestrator._compute_digest_fingerprint(summary_for_fp)

    # Seed user so skip predicate fires: matching fingerprint, sent 1 day ago.
    one_day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    real_db._conn.execute(
        "UPDATE users SET last_digest_fingerprint = ?, last_digest_sent_at = ? WHERE user_id = ?",
        [expected_fp, one_day_ago, user_id],
    )
    real_db._conn.commit()

    await orch.send_daily_digest()

    # Notifier never called — skipped.
    orch.telegram_notifier.send_daily_digest.assert_not_called()
    # Skip counter incremented
    user_row = real_db.get_user(user_id)
    assert user_row["digest_skip_count_7d"] == 1


@pytest.mark.asyncio
async def test_digest_sent_when_fingerprint_changed(real_db):
    """If price moved enough to change fingerprint, digest IS sent."""
    orch, _ = _make_orchestrator_with_mocks()
    orch.db = real_db
    orch.telegram_notifier = AsyncMock()

    user_id, route = _seed_user_and_route(real_db, "ams-nrt")
    now = datetime.now(UTC)

    snap_old = PriceSnapshot(
        snapshot_id="s_old", route_id="ams-nrt",
        observed_at=now - timedelta(hours=24),
        source="serpapi_poll", passengers=2,
        outbound_date=date(2026, 10, 5), return_date=date(2026, 10, 19),
        lowest_price=Decimal("500"),
    )
    snap_new = PriceSnapshot(
        snapshot_id="s_new", route_id="ams-nrt",
        observed_at=now,
        source="serpapi_poll", passengers=2,
        outbound_date=date(2026, 10, 5), return_date=date(2026, 10, 19),
        lowest_price=Decimal("450"),  # €50 move — way over threshold
    )
    real_db.insert_snapshot(snap_old)
    real_db.insert_snapshot(snap_new)
    real_db._conn.execute(
        "UPDATE price_snapshots SET user_id = ? WHERE route_id = 'ams-nrt'", [user_id]
    )
    real_db._conn.commit()

    from src.storage.models import Deal
    deal = Deal(
        deal_id="d1", snapshot_id="s_new", route_id="ams-nrt",
        score=Decimal("0.85"), urgency="book_now",
        alert_sent=True, alert_sent_at=now - timedelta(days=2),
    )
    real_db.insert_deal(deal)
    real_db._conn.execute("UPDATE deals SET user_id = ? WHERE deal_id = 'd1'", [user_id])
    real_db._conn.commit()

    # Stale fingerprint from a different price
    stale_fp = Orchestrator._compute_digest_fingerprint([{"route_id": "ams-nrt", "lowest_price": 999.0}])
    one_day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    real_db._conn.execute(
        "UPDATE users SET last_digest_fingerprint = ?, last_digest_sent_at = ? WHERE user_id = ?",
        [stale_fp, one_day_ago, user_id],
    )
    real_db._conn.commit()

    await orch.send_daily_digest()

    # Notifier WAS called — fingerprint changed.
    orch.telegram_notifier.send_daily_digest.assert_called_once()
    # New fingerprint persisted
    user_row = real_db.get_user(user_id)
    assert user_row["last_digest_fingerprint"] != stale_fp


@pytest.mark.asyncio
async def test_digest_sent_when_more_than_3d_ago(real_db):
    """Even if nothing changed, after 3+ days the digest goes out."""
    orch, _ = _make_orchestrator_with_mocks()
    orch.db = real_db
    orch.telegram_notifier = AsyncMock()

    user_id, route = _seed_user_and_route(real_db, "ams-nrt")
    now = datetime.now(UTC)

    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt",
        observed_at=now,
        source="serpapi_poll", passengers=2,
        outbound_date=date(2026, 10, 5), return_date=date(2026, 10, 19),
        lowest_price=Decimal("500"),
    )
    real_db.insert_snapshot(snap)
    real_db._conn.execute(
        "UPDATE price_snapshots SET user_id = ? WHERE route_id = 'ams-nrt'", [user_id]
    )
    real_db._conn.commit()

    from src.storage.models import Deal
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), urgency="book_now",
        alert_sent=True, alert_sent_at=now - timedelta(days=5),
    )
    real_db.insert_deal(deal)
    real_db._conn.execute("UPDATE deals SET user_id = ? WHERE deal_id = 'd1'", [user_id])
    real_db._conn.commit()

    # Same fingerprint as this digest will produce; but last sent 5 days ago.
    same_fp = Orchestrator._compute_digest_fingerprint([{"route_id": "ams-nrt", "lowest_price": 500.0}])
    five_days_ago = (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    real_db._conn.execute(
        "UPDATE users SET last_digest_fingerprint = ?, last_digest_sent_at = ? WHERE user_id = ?",
        [same_fp, five_days_ago, user_id],
    )
    real_db._conn.commit()

    await orch.send_daily_digest()

    orch.telegram_notifier.send_daily_digest.assert_called_once()


@pytest.mark.asyncio
async def test_digest_excludes_snoozed_route(real_db):
    """A snoozed route must not appear in the per-user digest summary."""
    orch, _ = _make_orchestrator_with_mocks()
    orch.db = real_db
    orch.telegram_notifier = AsyncMock()

    user_id, route = _seed_user_and_route(real_db, "ams-nrt")
    now = datetime.now(UTC)

    snap = PriceSnapshot(
        snapshot_id="s1", route_id="ams-nrt",
        observed_at=now,
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("500"),
    )
    real_db.insert_snapshot(snap)
    real_db._conn.execute(
        "UPDATE price_snapshots SET user_id = ? WHERE route_id = 'ams-nrt'", [user_id]
    )
    real_db._conn.commit()

    from src.storage.models import Deal
    deal = Deal(
        deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
        score=Decimal("0.85"), urgency="book_now",
        alert_sent=True, alert_sent_at=now,
    )
    real_db.insert_deal(deal)
    real_db._conn.execute("UPDATE deals SET user_id = ? WHERE deal_id = 'd1'", [user_id])
    real_db._conn.commit()

    # Snooze the only route
    real_db.snooze_route("ams-nrt", days=7)

    await orch.send_daily_digest()

    # No active routes for this user → no digest sent
    orch.telegram_notifier.send_daily_digest.assert_not_called()
