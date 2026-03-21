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
    assert "Amsterdam" in payload["text"]  # default origin
    assert "Tokyo Narita" in payload["text"]
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
    assert "No route matching" in payload["text"]


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


# --- Conversational message interpretation ---

@pytest.mark.asyncio
async def test_natural_language_routed_to_interpret(bot):
    """Non-command messages go through _interpret_message."""
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    interpret_result = json.dumps({
        "intent": "general_chat",
        "parameters": {},
        "response_text": "The best time to fly to Japan is usually January or February.",
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=interpret_result)]

    with patch("src.bot.commands.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        await bot._handle_update(_make_update("when's the best time to fly to Japan?"), client)

    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "Japan" in payload["text"]
    assert "January" in payload["text"]


@pytest.mark.asyncio
async def test_natural_language_add_trip(bot):
    """Natural language 'track flights to Tokyo' triggers add_trip intent."""
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    interpret_result = json.dumps({
        "intent": "add_trip",
        "parameters": {
            "destination": "NRT",
            "earliest_departure": "2026-10-01",
            "latest_return": "2026-10-15",
            "passengers": 2,
            "max_stops": 1,
        },
        "response_text": "I'll set up monitoring for Tokyo!",
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=interpret_result)]

    with patch("src.bot.commands.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        await bot._handle_update(_make_update("track flights to Tokyo in October"), client)

    # Should have a pending add action
    assert "42" in bot._pending
    assert bot._pending["42"]["action"] == "add"
    assert bot._pending["42"]["destination"] == "NRT"


@pytest.mark.asyncio
async def test_natural_language_modify_trip(bot, db):
    """'Push Mexico to February' triggers modify_trip intent."""
    route = Route(route_id="ams_mex", origin="AMS", destination="MEX", active=True,
                  earliest_departure=date(2026, 1, 15), latest_return=date(2026, 1, 30))
    db.upsert_route(route)

    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    interpret_result = json.dumps({
        "intent": "modify_trip",
        "parameters": {
            "route_id": "ams_mex",
            "changes": {
                "earliest_departure": "2026-02-15",
                "latest_return": "2026-02-28",
            },
        },
        "response_text": "I'll push Mexico City to February.",
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=interpret_result)]

    with patch("src.bot.commands.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        await bot._handle_update(_make_update("push Mexico to February"), client)

    # Should have a pending modify action
    assert "42" in bot._pending
    assert bot._pending["42"]["action"] == "modify"
    assert bot._pending["42"]["route_id"] == "ams_mex"
    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "/yes" in payload["text"]


@pytest.mark.asyncio
async def test_yes_modifies_route(bot, db):
    """Confirming a modify_trip pending action updates the route in DB."""
    route = Route(route_id="ams_mex", origin="AMS", destination="MEX", active=True,
                  earliest_departure=date(2026, 1, 15), latest_return=date(2026, 1, 30))
    db.upsert_route(route)

    bot._pending["42"] = {
        "action": "modify",
        "route_id": "ams_mex",
        "changes": {
            "earliest_departure": "2026-02-15",
            "latest_return": "2026-02-28",
        },
    }

    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    await bot._handle_update(_make_update("/yes"), client)

    routes = db.get_active_routes()
    assert len(routes) == 1
    r = routes[0]
    assert str(r.earliest_departure) == "2026-02-15"
    assert str(r.latest_return) == "2026-02-28"

    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    assert "Updated" in payload["text"]


@pytest.mark.asyncio
async def test_natural_language_query_prices(bot, db):
    """'How's Japan looking?' triggers query_prices intent."""
    route = Route(route_id="ams_nrt", origin="AMS", destination="NRT", active=True)
    db.upsert_route(route)

    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    interpret_result = json.dumps({
        "intent": "query_prices",
        "parameters": {"route_id": "ams_nrt"},
        "response_text": "",
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=interpret_result)]

    with patch("src.bot.commands.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        await bot._handle_update(_make_update("how's Japan looking?"), client)

    payload = client.post.call_args.kwargs.get("json") or client.post.call_args[1]["json"]
    # Should mention the route (no prices yet)
    assert "Tokyo Narita" in payload["text"]


# --- Conversation history ---

def test_conversation_history_limit(bot):
    """History is capped at 5 messages per chat."""
    for i in range(8):
        bot._add_history("42", "user", f"msg {i}")
    history = bot._conversation_history["42"]
    assert len(history) == 5
    assert history[0]["text"] == "msg 3"  # oldest kept
    assert history[-1]["text"] == "msg 7"  # newest


def test_conversation_history_text_format(bot):
    bot._add_history("42", "user", "hello")
    bot._add_history("42", "assistant", "hi there")
    text = bot._get_history_text("42")
    assert "user: hello" in text
    assert "assistant: hi there" in text


def test_empty_history(bot):
    text = bot._get_history_text("99")
    assert "no prior messages" in text


# --- update_route DB method ---

def test_update_route(db):
    route = Route(route_id="ams_nrt", origin="AMS", destination="NRT", active=True,
                  earliest_departure=date(2026, 10, 1), latest_return=date(2026, 10, 15))
    db.upsert_route(route)

    updated = db.update_route("ams_nrt", earliest_departure="2026-11-01", latest_return="2026-11-15")
    assert updated is True

    routes = db.get_active_routes()
    assert str(routes[0].earliest_departure) == "2026-11-01"
    assert str(routes[0].latest_return) == "2026-11-15"


def test_update_route_no_fields(db):
    route = Route(route_id="ams_nrt", origin="AMS", destination="NRT", active=True)
    db.upsert_route(route)

    updated = db.update_route("ams_nrt", bogus_field="nope")
    assert updated is False


def test_update_route_nonexistent(db):
    updated = db.update_route("nonexistent", passengers=3)
    assert updated is False


# --- Modify callback ---

@pytest.mark.asyncio
async def test_yes_modify_calls_reload_callback(db):
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
    route = Route(route_id="ams_mex", origin="AMS", destination="MEX", active=True)
    db.upsert_route(route)

    bot._pending["42"] = {
        "action": "modify",
        "route_id": "ams_mex",
        "changes": {"passengers": 3},
    }
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))

    await bot._handle_update(_make_update("/yes"), client)
    callback.assert_awaited_once()

    routes = db.get_active_routes()
    assert routes[0].passengers == 3
