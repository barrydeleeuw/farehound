from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.alerts.telegram import TelegramNotifier, find_cheapest_date
from src.bot.commands import TripBot
from src.storage.db import Database
from src.storage.models import Deal, PriceSnapshot, Route


# =============================================================================
# Shared fixtures
# =============================================================================

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


@pytest.fixture
def notifier():
    return TelegramNotifier(bot_token="123:ABC")


@pytest.fixture
def bot(db):
    return TripBot(
        bot_token="123:ABC",
        db=db,
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-4-20250514",
    )


def _insert_deal(db, route_id="ams-nrt", deal_id="d1", alert_sent=True, feedback=None, user_id=None, price=400):
    now = datetime.now(UTC)
    snap = PriceSnapshot(
        snapshot_id=f"snap_{deal_id}",
        route_id=route_id,
        observed_at=now,
        source="serpapi_poll",
        passengers=2,
        lowest_price=Decimal(str(price)),
    )
    db.insert_snapshot(snap)
    deal = Deal(
        deal_id=deal_id,
        snapshot_id=f"snap_{deal_id}",
        route_id=route_id,
        score=Decimal("0.85"),
        urgency="book_now",
        alert_sent=alert_sent,
        alert_sent_at=now if alert_sent else None,
        feedback=feedback,
    )
    db.insert_deal(deal)
    if user_id:
        db._conn.execute("UPDATE deals SET user_id = ? WHERE deal_id = ?", [user_id, deal_id])
        db._conn.execute("UPDATE price_snapshots SET user_id = ? WHERE snapshot_id = ?", [user_id, f"snap_{deal_id}"])
        db._conn.commit()


# =============================================================================
# ITEM-048: Digest buttons — DB layer
# =============================================================================

class TestGetRoutesWithPendingDeals:

    def test_returns_deal_ids_and_prices(self, db, sample_route):
        db.upsert_route(sample_route)
        _insert_deal(db, deal_id="d1", price=400)
        _insert_deal(db, deal_id="d2", price=450)

        result = db.get_routes_with_pending_deals()
        assert "ams-nrt" in result
        assert result["ams-nrt"]["price"] is not None
        assert "d1" in result["ams-nrt"]["deal_ids"]
        assert "d2" in result["ams-nrt"]["deal_ids"]

    def test_excludes_deals_with_feedback(self, db, sample_route):
        db.upsert_route(sample_route)
        _insert_deal(db, deal_id="d1", feedback="booked")
        _insert_deal(db, deal_id="d2", feedback=None)

        result = db.get_routes_with_pending_deals()
        assert "d1" not in result["ams-nrt"]["deal_ids"]
        assert "d2" in result["ams-nrt"]["deal_ids"]

    def test_excludes_non_alerted_deals(self, db, sample_route):
        db.upsert_route(sample_route)
        _insert_deal(db, deal_id="d1", alert_sent=False)

        result = db.get_routes_with_pending_deals()
        assert len(result) == 0

    def test_empty_when_no_deals(self, db, sample_route):
        db.upsert_route(sample_route)
        result = db.get_routes_with_pending_deals()
        assert result == {}

    def test_filters_by_user_id(self, db, sample_route):
        db.upsert_route(sample_route)
        _insert_deal(db, deal_id="d1", user_id="u1")
        _insert_deal(db, deal_id="d2", user_id="u2")

        result = db.get_routes_with_pending_deals(user_id="u1")
        assert "d1" in result["ams-nrt"]["deal_ids"]
        assert "d2" not in result["ams-nrt"]["deal_ids"]


class TestBulkDismissRouteDeals:

    def test_dismisses_all_pending_deals_for_route(self, db, sample_route):
        db.upsert_route(sample_route)
        _insert_deal(db, deal_id="d1", user_id="u1")
        _insert_deal(db, deal_id="d2", user_id="u1")

        count = db.bulk_dismiss_route_deals("ams-nrt", "u1")
        assert count == 2

        # Verify all are dismissed
        result = db.get_routes_with_pending_deals(user_id="u1")
        assert len(result) == 0

    def test_does_not_dismiss_other_users_deals(self, db, sample_route):
        db.upsert_route(sample_route)
        _insert_deal(db, deal_id="d1", user_id="u1")
        _insert_deal(db, deal_id="d2", user_id="u2")

        db.bulk_dismiss_route_deals("ams-nrt", "u1")

        result = db.get_routes_with_pending_deals(user_id="u2")
        assert "d2" in result["ams-nrt"]["deal_ids"]

    def test_does_not_dismiss_already_feedbacked(self, db, sample_route):
        db.upsert_route(sample_route)
        _insert_deal(db, deal_id="d1", user_id="u1", feedback="booked")
        _insert_deal(db, deal_id="d2", user_id="u1")

        count = db.bulk_dismiss_route_deals("ams-nrt", "u1")
        assert count == 1  # only d2 dismissed


# =============================================================================
# ITEM-048: Digest buttons — callback handlers
# =============================================================================

class TestDigestCallbackHandlers:

    def _make_callback(self, data: str, chat_id: int = 42) -> dict:
        return {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "data": data,
                "message": {
                    "message_id": 100,
                    "chat": {"id": chat_id},
                    "text": "Some digest message",
                },
            },
        }

    @pytest.mark.asyncio
    async def test_digest_booked_marks_deal(self, bot, db, sample_route):
        db.upsert_route(sample_route)
        uid = db.create_user("42", name="TestUser")
        db.update_user(uid, home_airport="AMS", onboarded=1, approved=1)
        _insert_deal(db, deal_id="d1")

        client = AsyncMock()
        client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

        await bot._handle_update(self._make_callback("digest_booked:d1"), client)

        # Should have answered callback and edited message
        assert client.post.call_count >= 1
        # Deal feedback should be "booked"
        now = datetime.now(UTC)
        deals = db.get_deals_since("ams-nrt", now - timedelta(hours=1))
        booked_deal = [d for d in deals if d.deal_id == "d1"][0]
        assert booked_deal.feedback == "booked"

    @pytest.mark.asyncio
    async def test_digest_dismiss_bulk_dismisses(self, bot, db, sample_route):
        db.upsert_route(sample_route)
        uid = db.create_user("42", name="TestUser")
        db.update_user(uid, home_airport="AMS", onboarded=1, approved=1)
        _insert_deal(db, deal_id="d1", user_id=uid)
        _insert_deal(db, deal_id="d2", user_id=uid)

        client = AsyncMock()
        client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

        await bot._handle_update(
            self._make_callback(f"digest_dismiss:ams-nrt:{uid}"), client
        )

        # All pending deals for route should be dismissed
        result = db.get_routes_with_pending_deals(user_id=uid)
        assert len(result) == 0


# =============================================================================
# ITEM-048: Digest messages include inline buttons
# =============================================================================

class TestDigestInlineButtons:

    @pytest.mark.asyncio
    async def test_digest_route_has_book_and_action_buttons(self, notifier):
        routes = [
            {
                "origin": "AMS",
                "destination": "NRT",
                "lowest_price": 485,
                "trend": "down",
                "deal_ids": ["d1"],
                "route_id": "ams-nrt",
                "user_id": "u1",
            },
        ]

        with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            await notifier.send_daily_digest(routes, chat_id="-100999")

            # 2 messages: header + 1 route
            assert mock_client.post.call_count == 2
            route_payload = mock_client.post.call_args_list[1].kwargs.get("json") or mock_client.post.call_args_list[1][1]["json"]
            keyboard = route_payload["reply_markup"]["inline_keyboard"]

            # R7 row 1: Book Now + Watching + Skip route
            assert keyboard[0][0]["text"] == "Book Now ✈️"
            assert "url" in keyboard[0][0]
            assert keyboard[0][1]["text"] == "Watching 👀"
            assert keyboard[0][1]["callback_data"] == "deal:watch:d1"
            assert keyboard[0][2]["text"] == "Skip route 🔕"
            assert keyboard[0][2]["callback_data"] == "route:snooze:7:ams-nrt"
            # Row 2: Details placeholder (Google Flights URL).
            assert keyboard[1][0]["text"] == "📊 Details"

    @pytest.mark.asyncio
    async def test_digest_route_without_deals_has_no_action_buttons(self, notifier):
        routes = [
            {
                "origin": "AMS",
                "destination": "NRT",
                "lowest_price": 485,
                "trend": "down",
            },
        ]

        with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            await notifier.send_daily_digest(routes, chat_id="-100999")

            route_payload = mock_client.post.call_args_list[1].kwargs.get("json") or mock_client.post.call_args_list[1][1]["json"]
            keyboard = route_payload["reply_markup"]["inline_keyboard"]
            # R7: row 1 is Book Now only (no deal_ids → no Watching, no route_id → no Skip);
            # row 2 is Details placeholder.
            assert len(keyboard) == 2
            assert keyboard[0][0]["text"] == "Book Now ✈️"
            assert len(keyboard[0]) == 1  # No Watching/Skip without deal_ids/route_id
            assert keyboard[1][0]["text"] == "📊 Details"


# =============================================================================
# ITEM-047: find_cheapest_date
# =============================================================================

class TestFindCheapestDate:

    def test_list_of_pairs_format(self):
        history = [
            ["2026-10-05", 900],
            ["2026-10-10", 700],
            ["2026-10-15", 800],
        ]
        result = find_cheapest_date(history, "2026-10-01", "2026-10-31", 900, 1)
        assert result is not None
        assert "Oct 10" in result

    def test_list_of_dicts_format(self):
        history = [
            {"date": "2026-10-05", "price": 900},
            {"date": "2026-10-10", "price": 700},
            {"date": "2026-10-15", "price": 800},
        ]
        result = find_cheapest_date(history, "2026-10-01", "2026-10-31", 900, 1)
        assert result is not None
        assert "Oct 10" in result

    def test_none_price_history_returns_none(self):
        assert find_cheapest_date(None, "2026-10-01", "2026-10-31", 500, 2) is None

    def test_empty_price_history_returns_none(self):
        assert find_cheapest_date([], "2026-10-01", "2026-10-31", 500, 2) is None

    def test_malformed_entries_skipped_gracefully(self):
        history = [
            "not a valid entry",
            None,
            42,
            ["bad-date", "not-a-price"],
            {"date": "2026-10-10", "price": 700},
        ]
        # Should still find the valid entry
        result = find_cheapest_date(history, "2026-10-01", "2026-10-31", 900, 1)
        assert result is not None
        assert "Oct 10" in result

    def test_all_malformed_returns_none(self):
        history = ["bad", None, 42]
        assert find_cheapest_date(history, "2026-10-01", "2026-10-31", 500, 1) is None

    def test_date_window_excludes_out_of_range(self):
        history = [
            ["2026-09-15", 300],  # before earliest
            ["2026-10-10", 700],  # in range
            ["2026-11-15", 200],  # after latest
        ]
        result = find_cheapest_date(history, "2026-10-01", "2026-10-31", 900, 1)
        assert result is not None
        assert "Oct 10" in result
        # Should NOT mention Sep or Nov dates
        assert "Sep" not in result
        assert "Nov" not in result

    def test_per_person_pricing_with_passengers(self):
        # Current price: 1000 total / 2 pax = 500/pp
        # Cheapest: 800 total / 2 pax = 400/pp
        # Saving: 100/pp > 20 threshold
        history = [["2026-10-10", 800]]
        result = find_cheapest_date(history, "2026-10-01", "2026-10-31", 1000, 2)
        assert result is not None
        assert "100" in result  # €100/pp saving

    def test_single_passenger_pricing(self):
        # Current: 500, cheapest: 400, saving: 100
        history = [["2026-10-10", 400]]
        result = find_cheapest_date(history, "2026-10-01", "2026-10-31", 500, 1)
        assert result is not None
        assert "100" in result

    def test_threshold_not_met_returns_none(self):
        # Current price: 500/pp, cheapest: 490/pp, saving: 10/pp < 20 threshold
        history = [["2026-10-10", 490]]
        result = find_cheapest_date(history, "2026-10-01", "2026-10-31", 500, 1)
        assert result is None

    def test_threshold_exactly_20_returns_none(self):
        # Saving of exactly 20 should not trigger (> 20, not >=)
        history = [["2026-10-10", 480]]
        result = find_cheapest_date(history, "2026-10-01", "2026-10-31", 500, 1)
        assert result is None

    def test_saving_just_above_threshold(self):
        # Saving of 21 should trigger
        history = [["2026-10-10", 479]]
        result = find_cheapest_date(history, "2026-10-01", "2026-10-31", 500, 1)
        assert result is not None

    def test_invalid_date_range_returns_none(self):
        history = [["2026-10-10", 400]]
        assert find_cheapest_date(history, "bad-date", "2026-10-31", 500, 1) is None
        assert find_cheapest_date(history, "2026-10-01", "bad-date", 500, 1) is None

    def test_none_dates_return_none(self):
        history = [["2026-10-10", 400]]
        assert find_cheapest_date(history, None, "2026-10-31", 500, 1) is None
        assert find_cheapest_date(history, "2026-10-01", None, 500, 1) is None

    def test_cheapest_is_current_price_returns_none(self):
        # If cheapest == current, saving is 0 (below threshold)
        history = [["2026-10-10", 500]]
        assert find_cheapest_date(history, "2026-10-01", "2026-10-31", 500, 1) is None


# =============================================================================
# ITEM-002: Savings tracker — DB layer
# =============================================================================

class TestSavingsLogTable:

    def test_savings_log_table_created(self, db):
        cursor = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='savings_log'"
        )
        assert cursor.fetchone() is not None

    def test_savings_log_columns(self, db):
        cursor = db._conn.execute("PRAGMA table_info(savings_log)")
        columns = {row[1] for row in cursor.fetchall()}
        expected = {
            "id", "user_id", "deal_id", "route_id", "primary_cost",
            "alternative_cost", "savings_amount", "airport_code",
            "snapshot_date", "created_at",
        }
        assert expected.issubset(columns)


class TestLogSaving:

    def test_insert_saving(self, db):
        db.log_saving(
            user_id="u1",
            route_id="ams-nrt",
            primary_cost=2000,
            alternative_cost=1500,
            savings_amount=500,
            airport_code="BRU",
            snapshot_date="2026-10-01",
        )
        cursor = db._conn.execute("SELECT * FROM savings_log WHERE user_id = 'u1'")
        rows = cursor.fetchall()
        assert len(rows) == 1

    def test_dedup_same_route_airport_date(self, db):
        kwargs = dict(
            user_id="u1",
            route_id="ams-nrt",
            primary_cost=2000,
            alternative_cost=1500,
            savings_amount=500,
            airport_code="BRU",
            snapshot_date="2026-10-01",
        )
        db.log_saving(**kwargs)
        db.log_saving(**kwargs)  # duplicate

        cursor = db._conn.execute("SELECT * FROM savings_log WHERE user_id = 'u1'")
        rows = cursor.fetchall()
        assert len(rows) == 1

    def test_different_dates_not_deduped(self, db):
        base = dict(
            user_id="u1",
            route_id="ams-nrt",
            primary_cost=2000,
            alternative_cost=1500,
            savings_amount=500,
            airport_code="BRU",
        )
        db.log_saving(**base, snapshot_date="2026-10-01")
        db.log_saving(**base, snapshot_date="2026-10-02")

        cursor = db._conn.execute("SELECT * FROM savings_log WHERE user_id = 'u1'")
        rows = cursor.fetchall()
        assert len(rows) == 2

    def test_with_deal_id(self, db, sample_route):
        db.upsert_route(sample_route)
        _insert_deal(db, deal_id="d1")
        db.log_saving(
            user_id="u1",
            route_id="ams-nrt",
            primary_cost=2000,
            alternative_cost=1500,
            savings_amount=500,
            airport_code="BRU",
            snapshot_date="2026-10-01",
            deal_id="d1",
        )
        cursor = db._conn.execute("SELECT deal_id FROM savings_log WHERE user_id = 'u1'")
        assert cursor.fetchone()[0] == "d1"


class TestGetTotalSavings:

    def test_returns_max_per_route_summed(self, db):
        # Route ams-nrt: two airports, should take max saving per route
        db.log_saving("u1", "ams-nrt", 2000, 1500, 500, "BRU", "2026-10-01")
        db.log_saving("u1", "ams-nrt", 2000, 1200, 800, "DUS", "2026-10-01")

        data = db.get_total_savings("u1")
        # Should pick the best per route (800 via DUS)
        assert data["total"] == 800
        assert data["route_count"] == 1
        assert len(data["details"]) == 1
        assert data["details"][0]["airport_code"] == "DUS"

    def test_multiple_routes(self, db):
        db.log_saving("u1", "ams-nrt", 2000, 1500, 500, "BRU", "2026-10-01")
        db.log_saving("u1", "ams-ist", 1000, 700, 300, "EIN", "2026-10-01")

        data = db.get_total_savings("u1")
        assert data["total"] == 800  # 500 + 300
        assert data["route_count"] == 2

    def test_empty_returns_zero(self, db):
        data = db.get_total_savings("u1")
        assert data == {"total": 0, "route_count": 0, "details": []}

    def test_filters_by_user(self, db):
        db.log_saving("u1", "ams-nrt", 2000, 1500, 500, "BRU", "2026-10-01")
        db.log_saving("u2", "ams-ist", 1000, 700, 300, "EIN", "2026-10-01")

        data = db.get_total_savings("u1")
        assert data["total"] == 500
        assert data["route_count"] == 1

    def test_max_per_airport_across_dates(self, db):
        # Same route+airport but different dates — should take MAX
        db.log_saving("u1", "ams-nrt", 2000, 1500, 500, "BRU", "2026-10-01")
        db.log_saving("u1", "ams-nrt", 2000, 1200, 800, "BRU", "2026-10-02")

        data = db.get_total_savings("u1")
        assert data["total"] == 800
        assert data["route_count"] == 1


# =============================================================================
# ITEM-002: /savings command output
# =============================================================================

class TestSavingsCommand:

    @pytest.fixture
    def user_id(self, db):
        uid = db.create_user("42", name="TestUser")
        db.update_user(uid, home_airport="AMS", onboarded=1, approved=1)
        return uid

    @pytest.mark.asyncio
    async def test_savings_empty_state(self, bot, db, user_id):
        client = AsyncMock()
        client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

        update = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "chat": {"id": 42},
                "text": "/savings",
            },
        }
        await bot._handle_update(update, client)

        payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
        assert "No savings tracked yet" in payload["text"]

    @pytest.mark.asyncio
    async def test_savings_with_data(self, bot, db, user_id, sample_route):
        db.upsert_route(sample_route)
        # Assign route to user
        db._conn.execute("UPDATE routes SET user_id = ? WHERE route_id = ?", [user_id, "ams-nrt"])
        db._conn.commit()
        db.log_saving(user_id, "ams-nrt", 2000, 1500, 500, "BRU", "2026-10-01")

        client = AsyncMock()
        client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

        update = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "chat": {"id": 42},
                "text": "/savings",
            },
        }
        await bot._handle_update(update, client)

        payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
        assert "€500" in payload["text"]
        assert "1 route" in payload["text"]
