"""End-to-end integration test for R7 (ITEM-051).

Mock-HTTP-only: real DB, real models, real telegram client (with mocked HTTP send),
real scorer (with mocked Anthropic SDK call). Catches the case where a unit-tested
component never actually gets called by the orchestrator.

Currently skipped — assertions are activated once Builder lands T7 (telegram unified
4 messages), T8 (watching/skip buttons), and T17-supporting orchestrator hooks.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.storage.db import Database
from src.storage.models import Route as DBRoute


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "serpapi_with_baggage"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


def _mock_anthropic_response(reasoning_obj: dict, score: float = 0.85, urgency: str = "book_now") -> MagicMock:
    """Build a mock Anthropic API response carrying a structured 3-field reasoning."""
    payload = {
        "score": score,
        "urgency": urgency,
        "reasoning": reasoning_obj,
        "booking_window_hours": 48,
    }
    response = MagicMock()
    response.content = [MagicMock(text=json.dumps(payload))]
    response.usage = MagicMock(input_tokens=200, output_tokens=80)
    return response


def _mock_telegram_http():
    """Build a context-manager mock for httpx.AsyncClient used in TelegramNotifier."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "r7_integration.db")
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def seeded_db(db):
    """A DB with one user, primary AMS + secondary EIN/BRU, and one AMS→TYO route."""
    user_id = db.create_user("chat-barry", name="Barry")
    db.update_user(user_id, home_airport="AMS", onboarded=1, approved=1)
    db.seed_airport_transport([
        {"code": "AMS", "name": "Amsterdam Schiphol", "transport_mode": "train",
         "transport_cost_eur": 12, "transport_time_min": 45, "is_primary": True},
        {"code": "EIN", "name": "Eindhoven", "transport_mode": "car",
         "transport_cost_eur": 30, "transport_time_min": 90, "parking_cost_eur": 50,
         "is_primary": False},
        {"code": "BRU", "name": "Brussels", "transport_mode": "Thalys",
         "transport_cost_eur": 70, "transport_time_min": 150, "is_primary": False},
    ], user_id=user_id)
    route = DBRoute(
        route_id="ams-tyo",
        origin="AMS",
        destination="NRT",
        trip_type="round_trip",
        earliest_departure=date(2026, 10, 1),
        latest_return=date(2026, 10, 31),
        passengers=2,
        active=True,
    )
    db.upsert_route(route, user_id=user_id)
    return {"db": db, "user_id": user_id, "route": route}


# ---------------------------------------------------------------------------
# T19 — END-TO-END (non-negotiable)
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="T19 activates once Builder completes T7/T8 + auto-snooze hook")
@pytest.mark.asyncio
async def test_r7_end_to_end_deal_alert_to_auto_snooze(seeded_db):
    """Full poll → score → alert → click → auto-snooze → digest-skipped flow.

    1. SerpAPI mock returns synthetic baggage fixture.
    2. Anthropic mock returns structured 3-field reasoning.
    3. orchestrator.poll_routes() fires.
    4. Assert deal alert HTTP body contains:
       - cost breakdown WITH baggage line
       - 3 reasoning bullets (vs_dates / vs_range / vs_nearby)
       - 'we checked 2 airports' transparency footer (EIN/BRU below €75 threshold)
       - 3-button row: Book Now ✈️ (URL), Watching 👀 (deal:watch), Skip route 🔕 (route:snooze:7)
       - '📊 Details' button row 2
       - callback_data uses NEW deal:* / route:* prefixes
    5. Simulate clicking deal:book:{id} — assert deal feedback='booked' AND
       routes.snoozed_until is set ~30 days from now.
    6. Run send_daily_digest — assert this user's route is skipped (snoozed).
    """
    pytest.skip("Wire after Builder T7+T8+auto-snooze hook lands")


@pytest.mark.skip(reason="T19 activates once Builder completes T7/T8")
@pytest.mark.asyncio
async def test_r7_end_to_end_callback_consolidation(seeded_db):
    """Both new (deal:book:{id}) and legacy (book:{id}) callback paths fire auto-snooze."""
    pytest.skip("Wire after Builder T13+T9 lands")


@pytest.mark.skip(reason="T19 activates after Builder T10 (digest_fingerprint_gating)")
@pytest.mark.asyncio
async def test_r7_end_to_end_digest_fingerprint_skip(seeded_db):
    """Two consecutive digests with unchanged fingerprint → second is skipped."""
    pytest.skip("Wire after Builder T10 lands")
