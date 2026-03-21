from __future__ import annotations

import json
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.commands import TripBot, _PARSE_PROMPT
from src.storage.db import Database
from src.storage.models import Route


@pytest.fixture
def db(tmp_path):
    d = Database(db_path=tmp_path / "test.db")
    d.init_schema()
    return d


@pytest.fixture
def bot(db):
    return TripBot(
        bot_token="123:ABC",
        chat_id="-100999",
        db=db,
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-4-20250514",
        home_airport="AMS",
    )


def _make_update(text: str, chat_id: str = "42") -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": int(chat_id)},
            "text": text,
        },
    }


# --- /trips ---

@pytest.mark.asyncio
async def test_trips_empty(bot):
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))
    await bot._handle_update(_make_update("/trips"), client)
    client.post.assert_called_once()
    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "No active routes" in payload["text"]


@pytest.mark.asyncio
async def test_trips_lists_routes(bot, db):
    route = Route(
        route_id="ams_nrt",
        origin="AMS",
        destination="NRT",
        earliest_departure=date(2026, 10, 1),
        latest_return=date(2026, 10, 15),
        passengers=2,
        active=True,
    )
    db.upsert_route(route)

    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))
    await bot._handle_update(_make_update("/trips"), client)
    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "Amsterdam" in payload["text"]
    assert "Tokyo Narita" in payload["text"]
    assert "ams_nrt" in payload["text"]


# --- /trip parsing ---

@pytest.mark.asyncio
async def test_trip_empty_text(bot):
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))
    await bot._handle_update(_make_update("/trip "), client)
    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "where you want to go" in payload["text"]


@pytest.mark.asyncio
async def test_trip_sends_confirmation(bot):
    parsed = {
        "origin": None,
        "destination": "NRT",
        "earliest_departure": "2026-10-18",
        "latest_return": "2026-11-08",
        "passengers": 2,
        "notes": None,
    }
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    with patch.object(bot, "_parse_route", return_value=parsed):
        await bot._handle_update(_make_update("/trip Tokyo, Oct 18 - Nov 8"), client)

    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "AMS" in payload["text"]  # default origin
    assert "NRT" in payload["text"]
    assert "/yes" in payload["text"]
    assert "42" in bot._pending  # chat_id stored


@pytest.mark.asyncio
async def test_trip_parse_failure(bot):
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    with patch.object(bot, "_parse_route", return_value=None):
        await bot._handle_update(_make_update("/trip asdf"), client)

    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "couldn't parse" in payload["text"]


# --- /yes and /no ---

@pytest.mark.asyncio
async def test_yes_adds_route(bot, db):
    bot._pending["42"] = {
        "action": "add",
        "origin": "AMS",
        "destination": "NRT",
        "earliest_departure": "2026-10-18",
        "latest_return": "2026-11-08",
        "passengers": 2,
        "notes": "",
    }
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    await bot._handle_update(_make_update("/yes"), client)

    routes = db.get_active_routes()
    assert len(routes) == 1
    assert routes[0].origin == "AMS"
    assert routes[0].destination == "NRT"
    assert "42" not in bot._pending

    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "added" in payload["text"]


@pytest.mark.asyncio
async def test_no_cancels(bot):
    bot._pending["42"] = {"action": "add", "origin": "AMS", "destination": "NRT"}
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    await bot._handle_update(_make_update("/no"), client)

    assert "42" not in bot._pending
    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "Cancelled" in payload["text"]


@pytest.mark.asyncio
async def test_yes_nothing_pending(bot):
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    await bot._handle_update(_make_update("/yes"), client)

    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "Nothing pending" in payload["text"]


# --- /remove ---

@pytest.mark.asyncio
async def test_remove_not_found(bot):
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    await bot._handle_update(_make_update("/remove bogus"), client)

    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "not found" in payload["text"]


@pytest.mark.asyncio
async def test_remove_confirm_and_yes(bot, db):
    route = Route(
        route_id="ams_nrt",
        origin="AMS",
        destination="NRT",
        active=True,
    )
    db.upsert_route(route)

    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    # Step 1: /remove asks for confirmation
    await bot._handle_update(_make_update("/remove ams_nrt"), client)
    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "Remove" in payload["text"]
    assert "/yes" in payload["text"]

    # Step 2: /yes deactivates
    await bot._handle_update(_make_update("/yes"), client)
    routes = db.get_active_routes()
    assert len(routes) == 0


# --- reload callback ---

@pytest.mark.asyncio
async def test_yes_calls_reload_callback(db):
    callback = AsyncMock()
    bot = TripBot(
        bot_token="123:ABC",
        chat_id="-100999",
        db=db,
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-4-20250514",
        home_airport="AMS",
        reload_callback=callback,
    )
    bot._pending["42"] = {
        "action": "add",
        "origin": "AMS",
        "destination": "NRT",
        "earliest_departure": "2026-10-18",
        "latest_return": "2026-11-08",
        "passengers": 2,
        "notes": "",
    }
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    await bot._handle_update(_make_update("/yes"), client)
    callback.assert_awaited_once()


# --- deactivate_route DB method ---

def test_deactivate_route(db):
    route = Route(route_id="ams_nrt", origin="AMS", destination="NRT", active=True)
    db.upsert_route(route)
    assert len(db.get_active_routes()) == 1

    db.deactivate_route("ams_nrt")
    assert len(db.get_active_routes()) == 0
