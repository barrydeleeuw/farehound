from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.analysis.scorer import DealScore, DealScorer
from src.storage.models import PriceSnapshot


# --- DealScore defaults ---

def test_deal_score_creation():
    ds = DealScore(score=0.85, urgency="book_now", reasoning="Great deal", booking_window_hours=24)
    assert ds.score == 0.85
    assert ds.urgency == "book_now"
    assert ds.booking_window_hours == 24


# --- _parse_response (via DealScorer instance) ---

def test_parse_response_valid():
    scorer = DealScorer.__new__(DealScorer)
    raw = json.dumps({
        "score": 0.92,
        "urgency": "book_now",
        "reasoning": "Price is 30% below average",
        "booking_window_hours": 12,
    })
    result = scorer._parse_response(raw)
    assert result.score == 0.92
    assert result.urgency == "book_now"
    assert result.booking_window_hours == 12


def test_parse_response_malformed_json():
    scorer = DealScorer.__new__(DealScorer)
    result = scorer._parse_response("not json at all")
    assert result.score == 0.3
    assert result.urgency == "watch"
    assert result.booking_window_hours == 48


def test_parse_response_missing_key():
    scorer = DealScorer.__new__(DealScorer)
    raw = json.dumps({"score": 0.9})  # missing urgency, reasoning, etc.
    result = scorer._parse_response(raw)
    assert result.score == 0.3  # fallback


def test_parse_response_invalid_score_type():
    scorer = DealScorer.__new__(DealScorer)
    raw = json.dumps({
        "score": "high",
        "urgency": "book_now",
        "reasoning": "test",
        "booking_window_hours": 12,
    })
    result = scorer._parse_response(raw)
    assert result.score == 0.3  # fallback due to ValueError


# --- score_deal with mocked Anthropic ---

@pytest.mark.asyncio
async def test_score_deal_mocked():
    scorer = DealScorer.__new__(DealScorer)
    scorer._model = "claude-sonnet-4-20250514"

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps({
        "score": 0.88,
        "urgency": "book_now",
        "reasoning": "Excellent price",
        "booking_window_hours": 24,
    }))]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    scorer._client = mock_client

    snapshot = PriceSnapshot(
        snapshot_id="s1", route_id="r1",
        observed_at=datetime(2026, 1, 1),
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("485"),
        outbound_date=None, return_date=None,
    )

    route = MagicMock()
    route.origin = "AMS"
    route.destination = "NRT"
    route.trip_type = "round_trip"
    route.date_flex_days = 3
    route.passengers = 2
    route.preferred_airlines = []
    route.earliest_departure = date(2026, 7, 1)

    result = await scorer.score_deal(
        snapshot=snapshot,
        route=route,
        price_history={"count": 0},
    )
    assert result.score == 0.88
    assert result.urgency == "book_now"
    mock_client.messages.create.assert_called_once()


# --- _build_prompt ---

def test_build_prompt_no_history():
    scorer = DealScorer.__new__(DealScorer)
    snapshot = PriceSnapshot(
        snapshot_id="s1", route_id="r1",
        observed_at=datetime(2026, 1, 1),
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("485"),
    )
    route = MagicMock()
    route.origin = "AMS"
    route.destination = "NRT"
    route.trip_type = "round_trip"
    route.date_flex_days = 3
    route.passengers = 2
    route.preferred_airlines = []
    route.earliest_departure = date(2026, 7, 1)

    prompt = scorer._build_prompt(
        snapshot=snapshot,
        route=route,
        price_history={"count": 0},
        community_flagged=False,
        traveller_name="Barry",
        home_airport="AMS",
    )
    assert "AMS" in prompt
    assert "NRT" in prompt
    assert "No historical price data" in prompt
    assert "Barry" in prompt
    assert "DATA CONFIDENCE: 0 observations" in prompt


def test_build_prompt_with_history():
    scorer = DealScorer.__new__(DealScorer)
    snapshot = PriceSnapshot(
        snapshot_id="s1", route_id="r1",
        observed_at=datetime(2026, 1, 1),
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("485"),
        price_level="low",
        typical_low=Decimal("400"),
        typical_high=Decimal("800"),
    )
    route = MagicMock()
    route.origin = "AMS"
    route.destination = "NRT"
    route.trip_type = "round_trip"
    route.date_flex_days = 3
    route.passengers = 2
    route.preferred_airlines = ["KLM"]
    route.earliest_departure = date(2026, 7, 1)

    prompt = scorer._build_prompt(
        snapshot=snapshot,
        route=route,
        price_history={"count": 10, "avg_price": 600.0, "min_price": 450.0, "max_price": 800.0},
        community_flagged=True,
        traveller_name="Barry",
        home_airport="AMS",
    )
    assert "observed average" in prompt
    assert "error fare: YES" in prompt
    assert "KLM" in prompt
    assert "DATA CONFIDENCE: 10 observations" in prompt


def test_build_prompt_with_past_feedback():
    scorer = DealScorer.__new__(DealScorer)
    snapshot = PriceSnapshot(
        snapshot_id="s1", route_id="r1",
        observed_at=datetime(2026, 1, 1),
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("485"),
    )
    route = MagicMock()
    route.origin = "AMS"
    route.destination = "NRT"
    route.trip_type = "round_trip"
    route.date_flex_days = 3
    route.passengers = 2
    route.preferred_airlines = []
    route.earliest_departure = date(2026, 7, 1)

    past_feedback = [
        {
            "feedback": "booked",
            "origin": "AMS",
            "destination": "NRT",
            "price": 420.0,
            "score": 0.92,
        },
        {
            "feedback": "dismissed",
            "origin": "AMS",
            "destination": "IST",
            "price": 300.0,
            "score": 0.65,
        },
    ]

    prompt = scorer._build_prompt(
        snapshot=snapshot,
        route=route,
        price_history={"count": 0},
        community_flagged=False,
        traveller_name="Barry",
        home_airport="AMS",
        past_feedback=past_feedback,
    )
    assert "PAST DECISIONS" in prompt
    assert "Booked" in prompt
    assert "Dismissed" in prompt
    assert "AMS→NRT" in prompt
    assert "AMS→IST" in prompt
    assert "€420" in prompt
    assert "0.92" in prompt


def test_build_prompt_without_past_feedback():
    scorer = DealScorer.__new__(DealScorer)
    snapshot = PriceSnapshot(
        snapshot_id="s1", route_id="r1",
        observed_at=datetime(2026, 1, 1),
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("485"),
    )
    route = MagicMock()
    route.origin = "AMS"
    route.destination = "NRT"
    route.trip_type = "round_trip"
    route.date_flex_days = 3
    route.passengers = 2
    route.preferred_airlines = []
    route.earliest_departure = date(2026, 7, 1)

    prompt = scorer._build_prompt(
        snapshot=snapshot,
        route=route,
        price_history={"count": 0},
        community_flagged=False,
        traveller_name="Barry",
        home_airport="AMS",
        past_feedback=None,
    )
    assert "PAST DECISIONS" not in prompt


def test_build_prompt_past_feedback_null_fields():
    scorer = DealScorer.__new__(DealScorer)
    snapshot = PriceSnapshot(
        snapshot_id="s1", route_id="r1",
        observed_at=datetime(2026, 1, 1),
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("485"),
    )
    route = MagicMock()
    route.origin = "AMS"
    route.destination = "NRT"
    route.trip_type = "round_trip"
    route.date_flex_days = 3
    route.passengers = 2
    route.preferred_airlines = []
    route.earliest_departure = date(2026, 7, 1)

    past_feedback = [
        {"feedback": None, "origin": "AMS", "destination": "NRT", "price": None, "score": None},
    ]

    prompt = scorer._build_prompt(
        snapshot=snapshot,
        route=route,
        price_history={"count": 0},
        community_flagged=False,
        traveller_name="Barry",
        home_airport="AMS",
        past_feedback=past_feedback,
    )
    assert "PAST DECISIONS" in prompt
    assert "Ignored" in prompt  # None feedback becomes "Ignored"


def test_build_prompt_with_nearby_comparison():
    scorer = DealScorer.__new__(DealScorer)
    snapshot = PriceSnapshot(
        snapshot_id="s1", route_id="r1",
        observed_at=datetime(2026, 1, 1),
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("1940"),
    )
    route = MagicMock()
    route.origin = "AMS"
    route.destination = "NRT"
    route.trip_type = "round_trip"
    route.date_flex_days = 3
    route.passengers = 2
    route.preferred_airlines = []
    route.earliest_departure = date(2026, 7, 1)

    nearby = [
        {
            "airport_code": "BRU",
            "airport_name": "Brussels",
            "fare_pp": 1600.0,
            "net_cost": 3270.0,
            "savings": 610.0,
            "transport_mode": "Thalys",
            "transport_cost": 70.0,
            "transport_time_min": 150,
        },
        {
            "airport_code": "DUS",
            "airport_name": "Dusseldorf",
            "fare_pp": 1750.0,
            "net_cost": 3560.0,
            "savings": 320.0,
            "transport_mode": "train",
            "transport_cost": 60.0,
            "transport_time_min": 168,
        },
    ]

    prompt = scorer._build_prompt(
        snapshot=snapshot,
        route=route,
        price_history={"count": 0},
        community_flagged=False,
        traveller_name="Barry",
        home_airport="AMS",
        nearby_comparison=nearby,
    )
    assert "NEARBY AIRPORTS" in prompt
    assert "Brussels" in prompt
    assert "Thalys" in prompt
    assert "save €610" in prompt
    assert "Dusseldorf" in prompt
    assert "save €320" in prompt
    assert "vs Amsterdam" in prompt


def test_build_prompt_without_nearby_comparison():
    scorer = DealScorer.__new__(DealScorer)
    snapshot = PriceSnapshot(
        snapshot_id="s1", route_id="r1",
        observed_at=datetime(2026, 1, 1),
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("485"),
    )
    route = MagicMock()
    route.origin = "AMS"
    route.destination = "NRT"
    route.trip_type = "round_trip"
    route.date_flex_days = 3
    route.passengers = 2
    route.preferred_airlines = []
    route.earliest_departure = date(2026, 7, 1)

    prompt = scorer._build_prompt(
        snapshot=snapshot,
        route=route,
        price_history={"count": 0},
        community_flagged=False,
        traveller_name="Barry",
        home_airport="AMS",
        nearby_comparison=None,
    )
    assert "NEARBY AIRPORTS" not in prompt


@pytest.mark.asyncio
async def test_score_deal_with_past_feedback():
    scorer = DealScorer.__new__(DealScorer)
    scorer._model = "claude-sonnet-4-20250514"

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps({
        "score": 0.90,
        "urgency": "book_now",
        "reasoning": "Calibrated based on past decisions",
        "booking_window_hours": 12,
    }))]
    mock_response.usage = MagicMock(input_tokens=200, output_tokens=60)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    scorer._client = mock_client

    snapshot = PriceSnapshot(
        snapshot_id="s1", route_id="r1",
        observed_at=datetime(2026, 1, 1),
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("485"),
    )
    route = MagicMock()
    route.origin = "AMS"
    route.destination = "NRT"
    route.trip_type = "round_trip"
    route.date_flex_days = 3
    route.passengers = 2
    route.preferred_airlines = []
    route.earliest_departure = date(2026, 7, 1)

    past_feedback = [
        {"feedback": "booked", "origin": "AMS", "destination": "NRT", "price": 400.0, "score": 0.90},
    ]

    result = await scorer.score_deal(
        snapshot=snapshot,
        route=route,
        price_history={"count": 5, "avg_price": 600.0, "min_price": 400.0, "max_price": 800.0},
        past_feedback=past_feedback,
    )
    assert result.score == 0.90

    # Verify Claude was called with prompt containing past decisions
    call_args = mock_client.messages.create.call_args
    prompt_sent = call_args.kwargs["messages"][0]["content"]
    assert "PAST DECISIONS" in prompt_sent


# =============================================================================
# T18 — R7 (ITEM-051): structured 3-field reasoning contract (§6.1, §6.5)
# =============================================================================

from src.analysis.scorer import reasoning_to_bullets


# --- _parse_response with structured reasoning ---

def test_parse_response_returns_3_field_dict():
    """Valid 3-field reasoning → parsed dict on DealScore."""
    scorer = DealScorer.__new__(DealScorer)
    raw = json.dumps({
        "score": 0.78,
        "urgency": "watch",
        "reasoning": {
            "vs_dates": "Cheapest of 4 dates polled — Mar 12 saves €60/pp",
            "vs_range": "€80 below Google's typical low (€620–€780)",
            "vs_nearby": "AMS is best — €40 cheaper than EIN",
        },
        "booking_window_hours": 48,
    })
    result = scorer._parse_response(raw)
    assert isinstance(result.reasoning, dict)
    assert result.reasoning["vs_dates"] == "Cheapest of 4 dates polled — Mar 12 saves €60/pp"
    assert result.reasoning["vs_range"] == "€80 below Google's typical low (€620–€780)"
    assert result.reasoning["vs_nearby"] == "AMS is best — €40 cheaper than EIN"
    assert result.score == 0.78
    assert result.urgency == "watch"


def test_parse_response_malformed_falls_back_to_3_field_dict():
    """Malformed JSON → fallback synthetic 3-field reasoning (§6.5)."""
    scorer = DealScorer.__new__(DealScorer)
    result = scorer._parse_response("not json at all")
    assert isinstance(result.reasoning, dict)
    assert set(result.reasoning.keys()) == {"vs_dates", "vs_range", "vs_nearby"}
    assert "Static fallback" in result.reasoning["vs_range"]


def test_parse_response_missing_vs_nearby_field_synthesises_default():
    """Per §6.5, missing reasoning sub-field is replaced with a documented placeholder."""
    scorer = DealScorer.__new__(DealScorer)
    raw = json.dumps({
        "score": 0.7,
        "urgency": "watch",
        "reasoning": {
            "vs_dates": "Cheapest of 4",
            "vs_range": "€80 below typical",
            # vs_nearby intentionally absent
        },
        "booking_window_hours": 48,
    })
    result = scorer._parse_response(raw)
    assert isinstance(result.reasoning, dict)
    assert "vs_nearby" in result.reasoning
    # Documented placeholder for missing nearby data
    assert result.reasoning["vs_nearby"] == "No nearby airports configured"
    # Other two fields still pass through unchanged
    assert result.reasoning["vs_dates"] == "Cheapest of 4"
    assert result.reasoning["vs_range"] == "€80 below typical"


def test_parse_response_legacy_string_reasoning_back_compat():
    """A scorer response with legacy free-text reasoning (string) is gracefully coerced.

    Older response captures might still have string reasoning — _coerce_reasoning maps
    them to a 3-field dict so renderers don't break.
    """
    scorer = DealScorer.__new__(DealScorer)
    raw = json.dumps({
        "score": 0.85,
        "urgency": "book_now",
        "reasoning": "Price is 30% below 90-day average — solid book.",
        "booking_window_hours": 24,
    })
    result = scorer._parse_response(raw)
    assert isinstance(result.reasoning, dict)
    assert set(result.reasoning.keys()) == {"vs_dates", "vs_range", "vs_nearby"}
    # Legacy string lands in vs_dates per coercion contract
    assert result.reasoning["vs_dates"].startswith("Price is 30%")


def test_parse_response_invalid_urgency_falls_back_to_watch():
    """Per Builder's hardening: unknown urgency value coerces to 'watch'."""
    scorer = DealScorer.__new__(DealScorer)
    raw = json.dumps({
        "score": 0.7,
        "urgency": "definitely_buy",  # not in the enum
        "reasoning": {
            "vs_dates": "a", "vs_range": "b", "vs_nearby": "c",
        },
        "booking_window_hours": 24,
    })
    result = scorer._parse_response(raw)
    assert result.urgency == "watch"


# --- reasoning_to_bullets renderer (§6.4 back-compat for deals.reasoning legacy column) ---

def test_reasoning_to_bullets_three_lines():
    """Flattens 3-field dict to 3-line bullet string for legacy `deals.reasoning` reads."""
    reasoning = {
        "vs_dates": "Cheapest of 4 dates polled",
        "vs_range": "€80 below Google's typical low",
        "vs_nearby": "AMS is best",
    }
    flat = reasoning_to_bullets(reasoning)
    lines = flat.split("\n")
    assert len(lines) == 3
    for line in lines:
        assert line.startswith("✓ ")
    assert "Cheapest of 4 dates polled" in lines[0]
    assert "€80 below" in lines[1]
    assert "AMS is best" in lines[2]


def test_reasoning_to_bullets_string_passthrough():
    """A legacy string passes through unchanged for fully back-compat reads."""
    assert reasoning_to_bullets("Old free-text reasoning") == "Old free-text reasoning"


def test_reasoning_to_bullets_none_returns_empty():
    assert reasoning_to_bullets(None) == ""


def test_reasoning_to_bullets_skips_empty_fields():
    """Empty sub-fields are skipped (don't render `✓ ` empty bullets)."""
    reasoning = {
        "vs_dates": "Has data",
        "vs_range": "",  # falsy
        "vs_nearby": "Also has data",
    }
    flat = reasoning_to_bullets(reasoning)
    lines = flat.split("\n")
    assert len(lines) == 2
    assert "Has data" in flat
    assert "Also has data" in flat


# --- score_deal end-to-end with structured reasoning ---

@pytest.mark.asyncio
async def test_score_deal_returns_structured_reasoning():
    """Full score_deal flow: Claude returns 3-field dict → DealScore.reasoning is dict."""
    scorer = DealScorer.__new__(DealScorer)
    scorer._model = "claude-sonnet-4-20250514"

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps({
        "score": 0.85,
        "urgency": "book_now",
        "reasoning": {
            "vs_dates": "Cheapest of 4 dates polled",
            "vs_range": "€80 below Google's typical low",
            "vs_nearby": "AMS is best — €40 cheaper than EIN",
        },
        "booking_window_hours": 24,
    }))]
    mock_response.usage = MagicMock(input_tokens=200, output_tokens=80)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    scorer._client = mock_client

    snapshot = PriceSnapshot(
        snapshot_id="s1", route_id="r1",
        observed_at=datetime(2026, 1, 1),
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("485"),
    )
    route = MagicMock()
    route.origin = "AMS"
    route.destination = "NRT"
    route.trip_type = "round_trip"
    route.date_flex_days = 3
    route.passengers = 2
    route.preferred_airlines = []
    route.earliest_departure = date(2026, 7, 1)

    result = await scorer.score_deal(
        snapshot=snapshot, route=route, price_history={"count": 0},
    )
    assert isinstance(result.reasoning, dict)
    assert "AMS is best" in result.reasoning["vs_nearby"]


@pytest.mark.asyncio
async def test_score_deal_malformed_response_yields_dict_fallback():
    """When Claude returns malformed JSON, returned reasoning is still a 3-field dict."""
    scorer = DealScorer.__new__(DealScorer)
    scorer._model = "claude-sonnet-4-20250514"

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="this isn't json at all")]
    mock_response.usage = MagicMock(input_tokens=50, output_tokens=10)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    scorer._client = mock_client

    snapshot = PriceSnapshot(
        snapshot_id="s1", route_id="r1",
        observed_at=datetime(2026, 1, 1),
        source="serpapi_poll", passengers=2,
        lowest_price=Decimal("485"),
    )
    route = MagicMock()
    route.origin = "AMS"
    route.destination = "NRT"
    route.trip_type = "round_trip"
    route.date_flex_days = 3
    route.passengers = 2
    route.preferred_airlines = []
    route.earliest_departure = date(2026, 7, 1)

    result = await scorer.score_deal(
        snapshot=snapshot, route=route, price_history={"count": 0},
    )
    assert isinstance(result.reasoning, dict)
    assert set(result.reasoning.keys()) == {"vs_dates", "vs_range", "vs_nearby"}
    # Score and urgency are conservative defaults
    assert result.score == 0.3
    assert result.urgency == "watch"
