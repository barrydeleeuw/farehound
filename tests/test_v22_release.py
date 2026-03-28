from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.analysis.scorer import DealScore, DealScorer
from src.storage.models import PriceSnapshot, Route as DBRoute


# --- Shared helpers ---

def _make_route(route_id="r1", origin="AMS", dest="NRT", passengers=2, trip_type="round_trip"):
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


def _make_user(user_id="u1", chat_id="chat1", name="Alice"):
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


def _make_snapshot(
    route_id="r1",
    lowest_price=400,
    passengers=2,
    outbound=date(2026, 10, 5),
    return_dt=date(2026, 10, 19),
    price_level=None,
    typical_low=None,
    typical_high=None,
):
    return PriceSnapshot(
        snapshot_id="snap1",
        route_id=route_id,
        observed_at=datetime.now(UTC),
        source="serpapi_poll",
        passengers=passengers,
        outbound_date=outbound,
        return_date=return_dt,
        lowest_price=Decimal(str(lowest_price)),
        currency="EUR",
        best_flight={"flights": [{"airline": "KL"}]},
        all_flights=[{"flights": [{"airline": "KL"}]}],
        price_level=price_level,
        typical_low=Decimal(str(typical_low)) if typical_low else None,
        typical_high=Decimal(str(typical_high)) if typical_high else None,
        search_params={"google_flights_url": "https://example.com"},
    )


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
        from src.orchestrator import Orchestrator
        orch = Orchestrator(config)

    return orch, mock_db


def _setup_check_alerts(orch, mock_db, last_alerted_price=None, avg_price=600, count=10,
                         inflection=False, bottom_price=None):
    """Wire up common mocks for _check_alerts tests."""
    orch._cycle_best_prices = {}
    mock_db.get_price_history.return_value = {
        "avg_price": avg_price, "count": count, "min_price": 400, "max_price": 800,
    }
    mock_db.get_recent_feedback.return_value = []
    mock_db.get_last_alerted_price.return_value = last_alerted_price
    mock_db.detect_price_inflection.return_value = (inflection, bottom_price)
    mock_db.get_airport_transport.return_value = None
    mock_db.insert_deal.return_value = None
    mock_db.get_secondary_airports.return_value = []

    orch.scorer.score_deal = AsyncMock(return_value=DealScore(
        score=0.50, urgency="watch", reasoning="Decent deal", booking_window_hours=48,
    ))
    orch.telegram_notifier = AsyncMock()


# =============================================================================
# ITEM-024: Price-drop alerting (no score gate)
# =============================================================================

class TestPriceDropAlerting:
    """Alert fires based on price drop vs last alerted price, NOT Claude score."""

    @pytest.mark.asyncio
    async def test_alert_fires_when_price_drops_below_last_alerted(self):
        """Alert should fire when current price < last alerted price, regardless of score."""
        orch, mock_db = _make_orchestrator_with_mocks()
        _setup_check_alerts(orch, mock_db, last_alerted_price=500)

        # Score is only 0.50 — below alert_threshold of 0.75
        # Under old logic this would NOT alert. Under new logic it SHOULD.
        route = _make_route()
        snapshot = _make_snapshot(lowest_price=400)

        await orch._check_alerts(route, snapshot, 400.0, None, _make_user())

        # Deal should be stored with alert_sent=True
        mock_db.insert_deal.assert_called_once()
        deal = mock_db.insert_deal.call_args[0][0]
        assert deal.alert_sent is True

    @pytest.mark.asyncio
    async def test_alert_does_not_fire_when_price_equals_last_alerted(self):
        """Alert should NOT fire when price == last alerted (strict less-than)."""
        orch, mock_db = _make_orchestrator_with_mocks()
        _setup_check_alerts(orch, mock_db, last_alerted_price=400)

        route = _make_route()
        snapshot = _make_snapshot(lowest_price=400)

        await orch._check_alerts(route, snapshot, 400.0, None, _make_user())

        mock_db.insert_deal.assert_called_once()
        deal = mock_db.insert_deal.call_args[0][0]
        assert deal.alert_sent is not True

    @pytest.mark.asyncio
    async def test_alert_does_not_fire_when_price_exceeds_last_alerted(self):
        """Alert should NOT fire when price > last alerted."""
        orch, mock_db = _make_orchestrator_with_mocks()
        _setup_check_alerts(orch, mock_db, last_alerted_price=350)

        route = _make_route()
        snapshot = _make_snapshot(lowest_price=400)

        await orch._check_alerts(route, snapshot, 400.0, None, _make_user())

        mock_db.insert_deal.assert_called_once()
        deal = mock_db.insert_deal.call_args[0][0]
        assert deal.alert_sent is not True

    @pytest.mark.asyncio
    async def test_alert_fires_on_first_ever_price(self):
        """Cold start: alert fires when no previous alert exists (first observation)."""
        orch, mock_db = _make_orchestrator_with_mocks()
        _setup_check_alerts(orch, mock_db, last_alerted_price=None, count=2)

        route = _make_route()
        snapshot = _make_snapshot(lowest_price=500)

        await orch._check_alerts(route, snapshot, 500.0, None, _make_user())

        mock_db.insert_deal.assert_called_once()
        deal = mock_db.insert_deal.call_args[0][0]
        assert deal.alert_sent is True

    @pytest.mark.asyncio
    async def test_no_alert_when_price_above_average(self):
        """Pre-filter: price above 90-day average should short-circuit (no scoring, no alert)."""
        orch, mock_db = _make_orchestrator_with_mocks()
        _setup_check_alerts(orch, mock_db, avg_price=500, count=10)

        route = _make_route()
        snapshot = _make_snapshot(lowest_price=600)

        await orch._check_alerts(route, snapshot, 600.0, None, _make_user())

        # Should return early — no deal inserted, no scoring
        mock_db.insert_deal.assert_not_called()
        orch.scorer.score_deal.assert_not_called()

    @pytest.mark.asyncio
    async def test_inflection_detection_triggers_alert(self):
        """Inflection detection fires alert even when price isn't a new low."""
        orch, mock_db = _make_orchestrator_with_mocks()
        _setup_check_alerts(
            orch, mock_db,
            last_alerted_price=350,  # current 400 > 350, so not a new low
            inflection=True,
            bottom_price=350,
        )

        route = _make_route()
        snapshot = _make_snapshot(lowest_price=400)

        await orch._check_alerts(route, snapshot, 400.0, None, _make_user())

        mock_db.insert_deal.assert_called_once()
        deal = mock_db.insert_deal.call_args[0][0]
        assert deal.alert_sent is True
        assert "bottomed out" in deal.reasoning


# =============================================================================
# ITEM-021: Nearby airport comparison in deal alerts
# =============================================================================

class TestNearbyAirportsInAlerts:
    """Secondary airports should be polled when a deferred alert is sent."""

    @pytest.mark.asyncio
    async def test_alert_deferred_to_pending(self):
        """When should_alert=True, alert is deferred to _pending_alerts (not sent immediately)."""
        orch, mock_db = _make_orchestrator_with_mocks()
        _setup_check_alerts(orch, mock_db, last_alerted_price=None, count=2)

        route = _make_route()
        snapshot = _make_snapshot(lowest_price=400)

        await orch._check_alerts(route, snapshot, 400.0, None, _make_user())

        assert route.route_id in orch._pending_alerts
        assert orch._pending_alerts[route.route_id]["price"] == 400.0

    @pytest.mark.asyncio
    async def test_deferred_keeps_cheapest_per_route(self):
        """When multiple windows trigger for same route, only the cheapest is kept."""
        orch, mock_db = _make_orchestrator_with_mocks()
        _setup_check_alerts(orch, mock_db, last_alerted_price=None, count=2)

        route = _make_route()

        # First window: €500
        snap1 = _make_snapshot(lowest_price=500, outbound=date(2026, 10, 5))
        await orch._check_alerts(route, snap1, 500.0, None, _make_user())

        # Second window: €400 (cheaper — should replace)
        snap2 = _make_snapshot(lowest_price=400, outbound=date(2026, 10, 10))
        await orch._check_alerts(route, snap2, 400.0, None, _make_user())

        assert orch._pending_alerts[route.route_id]["price"] == 400.0

        # Third window: €450 (more expensive — should NOT replace)
        snap3 = _make_snapshot(lowest_price=450, outbound=date(2026, 10, 15))
        await orch._check_alerts(route, snap3, 450.0, None, _make_user())

        assert orch._pending_alerts[route.route_id]["price"] == 400.0

    @pytest.mark.asyncio
    async def test_secondary_polled_in_deferred_send(self):
        """_send_deferred_alert polls secondary airports using the snapshot as primary baseline."""
        orch, mock_db = _make_orchestrator_with_mocks()
        _setup_check_alerts(orch, mock_db, last_alerted_price=None, count=2)

        route = _make_route()
        snapshot = _make_snapshot(lowest_price=400)

        await orch._check_alerts(route, snapshot, 400.0, None, _make_user())

        # Mock the secondary poll method
        orch._poll_secondary_airports_for_snapshot = AsyncMock()
        orch.telegram_notifier = AsyncMock()

        await orch._send_deferred_alert(orch._pending_alerts[route.route_id])

        orch._poll_secondary_airports_for_snapshot.assert_called_once()
        call_args = orch._poll_secondary_airports_for_snapshot.call_args[0]
        assert call_args[1] is snapshot  # snapshot passed directly as primary baseline

    @pytest.mark.asyncio
    async def test_no_deferred_alert_when_deduped(self):
        """Deduped deals are NOT added to _pending_alerts."""
        orch, mock_db = _make_orchestrator_with_mocks()
        _setup_check_alerts(orch, mock_db, last_alerted_price=350)

        route = _make_route()
        snapshot = _make_snapshot(lowest_price=400)  # 400 > 350, not a new low

        await orch._check_alerts(route, snapshot, 400.0, None, _make_user())

        assert route.route_id not in orch._pending_alerts

    @pytest.mark.asyncio
    async def test_secondary_airports_not_polled_when_no_alert(self):
        """Secondary airports should NOT be polled when the deal is deduped (no alert)."""
        orch, mock_db = _make_orchestrator_with_mocks()
        _setup_check_alerts(orch, mock_db, last_alerted_price=350)  # 400 > 350, no alert

        route = _make_route()
        snapshot = _make_snapshot(lowest_price=400)

        orch._poll_secondary_airports = AsyncMock()

        await orch._check_alerts(route, snapshot, 400.0, None, _make_user())

        orch._poll_secondary_airports.assert_not_called()


# =============================================================================
# ITEM-020: Scorer per-person pricing
# =============================================================================

class TestPerPersonScoring:
    """Scorer prompt should use per-person prices (total / passengers)."""

    def test_prompt_contains_per_person_lowest_price(self):
        """Current lowest price in prompt should be divided by passengers."""
        scorer = DealScorer.__new__(DealScorer)
        route = MagicMock()
        route.origin = "AMS"
        route.destination = "NRT"
        route.trip_type = "round_trip"
        route.date_flex_days = 3
        route.passengers = 2
        route.preferred_airlines = []
        route.earliest_departure = date(2026, 7, 1)

        snapshot = _make_snapshot(lowest_price=970, passengers=2)

        prompt = scorer._build_prompt(
            snapshot=snapshot,
            route=route,
            price_history={"count": 0},
            community_flagged=False,
            traveller_name="Barry",
            home_airport="AMS",
        )
        # 970 / 2 = 485
        assert "€485" in prompt

    def test_price_history_divided_by_passengers(self):
        """Price history avg/min/max should be divided by passengers in prompt."""
        scorer = DealScorer.__new__(DealScorer)
        route = MagicMock()
        route.origin = "AMS"
        route.destination = "NRT"
        route.trip_type = "round_trip"
        route.date_flex_days = 3
        route.passengers = 2
        route.preferred_airlines = []
        route.earliest_departure = date(2026, 7, 1)

        snapshot = _make_snapshot(lowest_price=970, passengers=2)

        prompt = scorer._build_prompt(
            snapshot=snapshot,
            route=route,
            price_history={"count": 10, "avg_price": 1200.0, "min_price": 900.0, "max_price": 1600.0},
            community_flagged=False,
            traveller_name="Barry",
            home_airport="AMS",
        )
        # avg: 1200/2=600, min: 900/2=450, max: 1600/2=800
        assert "€600" in prompt
        assert "€450" in prompt
        assert "€800" in prompt

    def test_typical_low_high_divided_by_passengers(self):
        """Google Flights typical range should be divided by passengers."""
        scorer = DealScorer.__new__(DealScorer)
        route = MagicMock()
        route.origin = "AMS"
        route.destination = "NRT"
        route.trip_type = "round_trip"
        route.date_flex_days = 3
        route.passengers = 2
        route.preferred_airlines = []
        route.earliest_departure = date(2026, 7, 1)

        snapshot = _make_snapshot(
            lowest_price=970,
            passengers=2,
            price_level="low",
            typical_low=800,
            typical_high=1600,
        )

        prompt = scorer._build_prompt(
            snapshot=snapshot,
            route=route,
            price_history={"count": 0},
            community_flagged=False,
            traveller_name="Barry",
            home_airport="AMS",
        )
        # 800/2=400, 1600/2=800
        assert "€400" in prompt
        assert "€800" in prompt

    def test_single_passenger_no_division(self):
        """With 1 passenger, per-person price equals total price."""
        scorer = DealScorer.__new__(DealScorer)
        route = MagicMock()
        route.origin = "AMS"
        route.destination = "NRT"
        route.trip_type = "round_trip"
        route.date_flex_days = 3
        route.passengers = 1
        route.preferred_airlines = []
        route.earliest_departure = date(2026, 7, 1)

        snapshot = _make_snapshot(lowest_price=485, passengers=1)

        prompt = scorer._build_prompt(
            snapshot=snapshot,
            route=route,
            price_history={"count": 10, "avg_price": 600.0, "min_price": 450.0, "max_price": 800.0},
            community_flagged=False,
            traveller_name="Barry",
            home_airport="AMS",
        )
        assert "€485" in prompt
        assert "€600" in prompt
        assert "€450" in prompt
        assert "€800" in prompt
