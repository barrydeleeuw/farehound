"""Tests for v2.1 release changes.

Covers ITEM-003 (smart digest), ITEM-016 (cost breakdown),
ITEM-013 (booking follow-up), ITEM-010 (RSS User-Agent fix),
and ITEM-014 (HA cleanup).
"""
from __future__ import annotations

import importlib
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.alerts.telegram import TelegramNotifier
from src.apis.community import RSSListener, CommunityFeedConfig
from src.storage.db import Database
from src.storage.models import Deal, PriceSnapshot, Route


# ────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "test.db")
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def notifier():
    return TelegramNotifier(bot_token="123:ABC")


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


CHAT_ID = "-100999"


def _mock_httpx():
    """Return patched httpx.AsyncClient and the mock client."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return patch("src.alerts.telegram.httpx.AsyncClient", return_value=mock_client), mock_client


# ────────────────────────────────────────────────────────
# ITEM-003: Smart daily digest
# ────────────────────────────────────────────────────────

class TestGetRoutesWithPendingDeals:
    def test_returns_routes_with_pending_deals(self, db, sample_route):
        db.upsert_route(sample_route)
        now = datetime.now(UTC)
        snap = PriceSnapshot(
            snapshot_id="s1", route_id="ams-nrt", observed_at=now,
            source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
        )
        db.insert_snapshot(snap)
        deal = Deal(
            deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
            score=Decimal("0.85"), alert_sent=True,
            alert_sent_at=now - timedelta(days=1),
        )
        db.insert_deal(deal)

        result = db.get_routes_with_pending_deals()
        assert "ams-nrt" in result
        assert result["ams-nrt"] == 400.0

    def test_excludes_routes_with_feedback(self, db, sample_route):
        db.upsert_route(sample_route)
        now = datetime.now(UTC)
        snap = PriceSnapshot(
            snapshot_id="s1", route_id="ams-nrt", observed_at=now,
            source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
        )
        db.insert_snapshot(snap)
        deal = Deal(
            deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
            score=Decimal("0.85"), alert_sent=True,
            alert_sent_at=now - timedelta(days=1), feedback="booked",
        )
        db.insert_deal(deal)

        result = db.get_routes_with_pending_deals()
        assert result == {}

    def test_empty_when_no_deals(self, db, sample_route):
        db.upsert_route(sample_route)
        result = db.get_routes_with_pending_deals()
        assert result == {}

    def test_excludes_non_alerted_deals(self, db, sample_route):
        db.upsert_route(sample_route)
        now = datetime.now(UTC)
        snap = PriceSnapshot(
            snapshot_id="s1", route_id="ams-nrt", observed_at=now,
            source="serpapi_poll", passengers=2, lowest_price=Decimal("400"),
        )
        db.insert_snapshot(snap)
        deal = Deal(
            deal_id="d1", snapshot_id="s1", route_id="ams-nrt",
            score=Decimal("0.60"), alert_sent=False,
        )
        db.insert_deal(deal)

        result = db.get_routes_with_pending_deals()
        assert result == {}


class TestSmartDigestSkipsNoDeals:
    @pytest.mark.asyncio
    async def test_digest_skips_routes_without_pending_deals(self):
        """send_daily_digest skips routes with no pending deals."""
        from src.orchestrator import Orchestrator

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

        user = {"user_id": "u1", "telegram_chat_id": "chat1", "name": "Alice",
                "home_airport": "AMS", "preferences": None, "onboarded": True, "active": True}
        route = Route(route_id="r1", origin="AMS", destination="NRT", passengers=2, active=True)

        mock_db.get_all_active_users.return_value = [user]
        mock_db.get_active_routes.return_value = [route]
        mock_db.get_routes_with_pending_deals.return_value = {}  # No pending deals

        orch.telegram_notifier = AsyncMock()

        await orch.send_daily_digest()

        # Digest should NOT be sent when no pending deals
        orch.telegram_notifier.send_daily_digest.assert_not_called()


class TestDigestMessaging:
    @pytest.mark.asyncio
    async def test_digest_header_contains_undecided_message(self, notifier):
        """Digest header says 'You haven't decided on these yet'."""
        routes = [
            {"origin": "AMS", "destination": "NRT", "lowest_price": 485, "trend": "down"},
        ]

        patcher, mock_client = _mock_httpx()
        with patcher:
            await notifier.send_daily_digest(routes, chat_id=CHAT_ID)

            calls = [c.kwargs.get("json") or c[1]["json"] for c in mock_client.post.call_args_list]
            header = calls[0]["text"]
            assert "haven't decided" in header

    @pytest.mark.asyncio
    async def test_digest_shows_price_change_since_alert(self, notifier):
        """Route summary shows price change since the original alert."""
        routes = [
            {
                "origin": "AMS", "destination": "NRT",
                "lowest_price": 450, "trend": "down",
                "alert_price": 500,  # was €500 when alerted, now €450
            },
        ]

        patcher, mock_client = _mock_httpx()
        with patcher:
            await notifier.send_daily_digest(routes, chat_id=CHAT_ID)

            # Route message (second call after header)
            calls = [c.kwargs.get("json") or c[1]["json"] for c in mock_client.post.call_args_list]
            route_text = calls[1]["text"]
            assert "Dropped" in route_text
            assert "€50" in route_text


# ────────────────────────────────────────────────────────
# ITEM-016: Transparent cost breakdown
# ────────────────────────────────────────────────────────

class TestCostBreakdown:
    @pytest.mark.asyncio
    async def test_per_person_pricing_shown_first(self, notifier):
        """Per-person price is always the first price line."""
        deal_info = {
            "origin": "AMS", "destination": "NRT",
            "price": 970, "passengers": 2,
            "score": 0.88, "airline": "KLM",
            "dates": "2026-10-01 to 2026-10-15",
        }

        patcher, mock_client = _mock_httpx()
        with patcher:
            await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)

            payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
            text = payload["text"]
            lines = text.split("\n")
            # Second line should contain per-person price
            assert "€485/pp" in lines[1]

    @pytest.mark.asyncio
    async def test_primary_airport_breakdown_always_shown(self, notifier):
        """Primary airport cost breakdown is shown even when transport/parking = 0."""
        deal_info = {
            "origin": "AMS", "destination": "NRT",
            "price": 970, "passengers": 2,
            "score": 0.88, "airline": "KLM",
            "dates": "2026-10-01 to 2026-10-15",
            # No primary_transport_cost or primary_parking_cost
        }

        patcher, mock_client = _mock_httpx()
        with patcher:
            await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)

            payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
            text = payload["text"]
            # Should always show "flights = total" breakdown
            assert "€970 flights" in text
            assert "total" in text

    @pytest.mark.asyncio
    async def test_primary_airport_with_transport_costs(self, notifier):
        """Primary airport breakdown includes transport and parking when present."""
        deal_info = {
            "origin": "AMS", "destination": "NRT",
            "price": 970, "passengers": 2,
            "score": 0.88, "airline": "KLM",
            "dates": "2026-10-01 to 2026-10-15",
            "primary_transport_cost": 24,
            "primary_transport_mode": "train",
            "primary_parking_cost": 0,
        }

        patcher, mock_client = _mock_httpx()
        with patcher:
            await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)

            payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
            text = payload["text"]
            assert "€970 flights" in text
            assert "€96 train" in text
            assert "€1,066 total" in text

    @pytest.mark.asyncio
    async def test_nearby_alternative_breakdown_format(self, notifier):
        """Nearby alternatives show per-person pricing and full cost breakdown."""
        deal_info = {
            "origin": "AMS", "destination": "NRT",
            "price": 1940, "passengers": 2,
            "score": 0.85, "airline": "KLM",
            "dates": "2026-10-01 to 2026-10-15",
            "nearby_comparison": [
                {
                    "airport_code": "BRU",
                    "airport_name": "Brussels",
                    "fare_pp": 1600.0,
                    "net_cost": 3480.0,
                    "savings": 460.0,
                    "transport_mode": "Thalys",
                    "transport_cost": 70.0,
                    "transport_time_min": 150,
                },
            ],
        }

        patcher, mock_client = _mock_httpx()
        with patcher:
            await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)

            payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
            text = payload["text"]
            assert "Brussels" in text
            assert "€1,600/pp" in text
            assert "save €460" in text
            # Full breakdown line
            assert "€3,200 flights" in text
            assert "€280 thalys" in text
            assert "€3,480 total" in text


# ────────────────────────────────────────────────────────
# ITEM-013: Booking follow-up
# ────────────────────────────────────────────────────────

class TestFollowUp:
    @pytest.mark.asyncio
    async def test_send_follow_up_format(self, notifier):
        """Follow-up message mentions route, price, and has action buttons."""
        deal_info = {
            "origin": "AMS", "destination": "NRT",
            "price": 485, "deal_id": "deal_789",
        }

        patcher, mock_client = _mock_httpx()
        with patcher:
            await notifier.send_follow_up(deal_info, chat_id=CHAT_ID)

            payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
            text = payload["text"]
            assert "Amsterdam" in text
            assert "Tokyo Narita" in text
            assert "€485" in text
            assert "three days ago" in text

            keyboard = payload["reply_markup"]["inline_keyboard"]
            row = keyboard[0]
            assert row[0]["text"] == "Yes, booked ✅"
            assert row[0]["callback_data"] == "booked:deal_789"
            assert row[1]["text"] == "Still watching 👀"
            assert row[1]["callback_data"] == "watching:deal_789"

    @pytest.mark.asyncio
    async def test_check_pending_feedback_sends_follow_ups(self):
        """_check_pending_feedback sends follow-up for pending deals."""
        from src.orchestrator import Orchestrator

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

        pending = [
            {"deal_id": "d1", "route_id": "r1", "origin": "AMS",
             "destination": "NRT", "price": 400.0},
        ]
        user = {"user_id": "u1", "telegram_chat_id": "chat1", "name": "Alice",
                "home_airport": "AMS", "preferences": None, "onboarded": True, "active": True}
        route = Route(route_id="r1", origin="AMS", destination="NRT", passengers=2, active=True)

        mock_db.get_deals_pending_feedback.return_value = pending
        mock_db.get_all_active_users.return_value = [user]
        mock_db.get_active_routes.return_value = [route]

        orch.telegram_notifier = AsyncMock()

        await orch._check_pending_feedback()

        orch.telegram_notifier.send_follow_up.assert_called_once()
        call_kwargs = orch.telegram_notifier.send_follow_up.call_args
        assert call_kwargs[0][0]["deal_id"] == "d1"
        assert call_kwargs.kwargs["chat_id"] == "chat1"

    @pytest.mark.asyncio
    async def test_check_pending_feedback_no_notifier(self):
        """_check_pending_feedback does nothing if telegram_notifier is None."""
        from src.orchestrator import Orchestrator

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

        orch.telegram_notifier = None

        await orch._check_pending_feedback()

        # Should return early without querying DB
        mock_db.get_deals_pending_feedback.assert_not_called()

    def test_get_deals_pending_feedback_db(self, db, sample_route):
        """get_deals_pending_feedback returns deals alerted 3+ days ago without feedback."""
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

        pending = db.get_deals_pending_feedback(older_than_days=3)
        assert len(pending) == 1
        assert pending[0]["deal_id"] == "d1"
        assert pending[0]["origin"] == "AMS"
        assert float(pending[0]["price"]) == 400.0


# ────────────────────────────────────────────────────────
# ITEM-010: RSS User-Agent fix
# ────────────────────────────────────────────────────────

class TestRSSUserAgent:
    @pytest.mark.asyncio
    async def test_rss_uses_browser_user_agent(self):
        """RSS client uses a browser-like User-Agent to avoid 403s from Reddit."""
        feed_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><title>Test</title>
          <item><title>AMS to NRT €450</title><guid>deal1</guid></item>
        </channel></rss>"""

        feeds = [CommunityFeedConfig(channel="test", filter_origins=[], url="https://example.com/rss")]
        listener = RSSListener(feeds=feeds)
        callback = AsyncMock()
        await listener.start(callback)

        with patch("src.apis.community.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.text = feed_xml
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            await listener._poll(seed=True)

            # Verify the httpx.AsyncClient was created with browser-like User-Agent
            call_kwargs = mock_cls.call_args
            headers = call_kwargs.kwargs.get("headers") or (call_kwargs[1].get("headers") if len(call_kwargs) > 1 else None)
            assert headers is not None
            ua = headers.get("User-Agent", "")
            # Should NOT be "FareHound/1.0" — should be a browser UA
            assert "FareHound" not in ua
            assert "Mozilla" in ua


# ────────────────────────────────────────────────────────
# ITEM-014: HA cleanup
# ────────────────────────────────────────────────────────

class TestHACleanup:
    def test_no_homeassistant_imports_in_src(self):
        """No source files import from homeassistant (HA-specific code removed)."""
        src_dir = Path(__file__).parent.parent / "src"
        for py_file in src_dir.rglob("*.py"):
            content = py_file.read_text()
            # Allow the alerts/homeassistant.py module itself to exist
            if py_file.name == "homeassistant.py":
                continue
            assert "from homeassistant" not in content, (
                f"{py_file} still imports from homeassistant"
            )
            assert "import homeassistant" not in content, (
                f"{py_file} still imports homeassistant"
            )

    def test_farehound_src_directory_exists_for_ha_build(self):
        """farehound/src/ must exist — HA Supervisor uses it as Docker build context."""
        project_root = Path(__file__).parent.parent
        assert (project_root / "farehound" / "src").exists(), (
            "farehound/src/ missing — needed for HA Supervisor build"
        )
