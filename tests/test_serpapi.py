from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.apis.serpapi import (
    SerpAPIClient,
    SerpAPIError,
    FlightSearchResult,
    VerificationResult,
    generate_date_windows,
    generate_date_windows_with_duration_flex,
    build_google_flights_url,
)


# --- generate_date_windows ---

def test_generate_date_windows_basic():
    windows = generate_date_windows(
        earliest_departure=date(2026, 10, 1),
        latest_return=date(2026, 10, 31),
        trip_duration_days=14,
        max_windows=4,
    )
    assert len(windows) == 4
    # First window starts at earliest departure
    assert windows[0][0] == date(2026, 10, 1)
    assert windows[0][1] == date(2026, 10, 15)
    # Last window return should not exceed latest_return
    for _, ret in windows:
        assert ret <= date(2026, 10, 31)


def test_generate_date_windows_single():
    windows = generate_date_windows(
        earliest_departure=date(2026, 10, 1),
        latest_return=date(2026, 10, 15),
        trip_duration_days=14,
        max_windows=4,
    )
    assert len(windows) == 1
    assert windows[0] == (date(2026, 10, 1), date(2026, 10, 15))


def test_generate_date_windows_max_windows_1():
    windows = generate_date_windows(
        earliest_departure=date(2026, 10, 1),
        latest_return=date(2026, 10, 31),
        trip_duration_days=7,
        max_windows=1,
    )
    assert len(windows) == 1


def test_generate_date_windows_range_too_short():
    with pytest.raises(ValueError, match="Travel range too short"):
        generate_date_windows(
            earliest_departure=date(2026, 10, 1),
            latest_return=date(2026, 10, 10),
            trip_duration_days=14,
        )


def test_generate_date_windows_exact_fit():
    windows = generate_date_windows(
        earliest_departure=date(2026, 10, 1),
        latest_return=date(2026, 10, 15),
        trip_duration_days=14,
        max_windows=1,
    )
    assert len(windows) == 1
    assert windows[0] == (date(2026, 10, 1), date(2026, 10, 15))


# --- generate_date_windows_with_duration_flex ---

def test_duration_flex_zero_matches_baseline():
    """flex=0 should behave identically to generate_date_windows."""
    baseline = generate_date_windows(
        earliest_departure=date(2026, 6, 6),
        latest_return=date(2026, 6, 24),
        trip_duration_days=14,
        max_windows=2,
    )
    flexed = generate_date_windows_with_duration_flex(
        earliest_departure=date(2026, 6, 6),
        latest_return=date(2026, 6, 24),
        trip_duration_days=14,
        trip_duration_flex_days=0,
        max_windows=2,
    )
    assert sorted(flexed) == sorted(baseline)


def test_duration_flex_adds_shorter_and_longer_trips():
    """The AMS→VLC bug scenario: 'two weeks departing Jun 8' with ±3 flex
    should also poll 11-day and 17-day options, so the Sat-return combo
    (Jun 8 → Jun 20, 12 days) is reachable in the candidate set."""
    windows = generate_date_windows_with_duration_flex(
        earliest_departure=date(2026, 6, 6),
        latest_return=date(2026, 6, 24),
        trip_duration_days=14,
        trip_duration_flex_days=3,
        max_windows=2,
    )
    durations = {(r - o).days for o, r in windows}
    assert 14 in durations
    # Shorter and longer variants both sampled when they fit in the range.
    assert 11 in durations or 17 in durations
    # No duplicates.
    assert len(windows) == len(set(windows))
    # Sorted by outbound.
    assert windows == sorted(windows, key=lambda w: (w[0], w[1]))


def test_duration_flex_skips_durations_that_dont_fit():
    """If a flexed duration is longer than the available range, it's skipped
    instead of raising — the configured duration still gets sampled."""
    windows = generate_date_windows_with_duration_flex(
        earliest_departure=date(2026, 6, 6),
        latest_return=date(2026, 6, 20),
        trip_duration_days=14,
        trip_duration_flex_days=7,
        max_windows=1,
    )
    # 21-day variant doesn't fit (only 14 days of range); 7-day does.
    durations = {(r - o).days for o, r in windows}
    assert 14 in durations
    assert 21 not in durations


def test_duration_flex_clamps_to_minimum_one_day():
    """Don't produce zero- or negative-length trips when flex exceeds duration."""
    windows = generate_date_windows_with_duration_flex(
        earliest_departure=date(2026, 6, 1),
        latest_return=date(2026, 6, 30),
        trip_duration_days=2,
        trip_duration_flex_days=5,
        max_windows=1,
    )
    assert all((r - o).days >= 1 for o, r in windows)


# --- build_google_flights_url ---

def test_build_google_flights_url_roundtrip():
    """v0.11.6: switched to Google's own #flt= hash-fragment deep-link format."""
    url = build_google_flights_url("AMS", "NRT", "2026-10-01", "2026-10-15")
    assert url.startswith("https://www.google.com/travel/flights")
    # Hash fragment carries the structured query.
    assert "#flt=" in url
    # Both legs encoded as `ORIG.DEST.DATE` separated by `*`.
    assert "AMS.NRT.2026-10-01" in url
    assert "NRT.AMS.2026-10-15" in url  # return leg
    assert "*" in url  # round-trip leg separator
    assert ";t:f" in url  # round-trip flag


def test_build_google_flights_url_oneway():
    url = build_google_flights_url("AMS", "NRT", date(2026, 10, 1))
    assert "AMS.NRT.2026-10-01" in url
    assert "*" not in url  # no return leg
    assert ";t:c" in url  # one-way flag


def test_build_google_flights_url_with_passengers():
    url = build_google_flights_url("AMS", "NRT", "2026-10-01", "2026-10-15", passengers=3)
    assert ";px:3" in url


def test_build_google_flights_url_passengers_capped_at_9():
    url = build_google_flights_url("AMS", "NRT", "2026-10-01", "2026-10-15", passengers=12)
    assert ";px:9" in url
    assert ";px:12" not in url


def test_pick_representative_flight_prefers_best_flights():
    from src.apis.serpapi import FlightSearchResult, pick_representative_flight
    result = FlightSearchResult(
        best_flights=[{"airline": "KL", "price": 800}],
        other_flights=[{"airline": "FR", "price": 600}],
    )
    flight = pick_representative_flight(result)
    # Cheapest wins regardless of which list it came from.
    assert flight["airline"] == "FR"
    assert flight["price"] == 600


def test_pick_representative_flight_falls_back_to_other_flights_when_best_empty():
    """v0.11.8: bug fix — pre-fix this returned None when best_flights was
    empty, losing airline info needed for baggage estimation."""
    from src.apis.serpapi import FlightSearchResult, pick_representative_flight
    result = FlightSearchResult(
        best_flights=[],
        other_flights=[
            {"airline": "AF", "price": 1964, "flights": [{"airline": "AF"}]},
            {"airline": "KL", "price": 2100, "flights": [{"airline": "KL"}]},
        ],
    )
    flight = pick_representative_flight(result)
    assert flight is not None
    assert flight["airline"] == "AF"  # cheapest


def test_pick_representative_flight_returns_none_when_no_results():
    from src.apis.serpapi import FlightSearchResult, pick_representative_flight
    assert pick_representative_flight(FlightSearchResult()) is None


def test_pick_representative_flight_handles_missing_prices():
    """If no flight has a price, return the first valid dict."""
    from src.apis.serpapi import FlightSearchResult, pick_representative_flight
    result = FlightSearchResult(
        best_flights=[],
        other_flights=[{"airline": "KL"}, {"airline": "AF"}],
    )
    flight = pick_representative_flight(result)
    assert flight is not None
    assert flight["airline"] == "KL"


def test_build_google_flights_url_solo_traveler_no_pax_param():
    url = build_google_flights_url("AMS", "NRT", "2026-10-01", "2026-10-15", passengers=1)
    assert ";px:" not in url


# --- VerificationResult ---

def test_verification_result_defaults():
    vr = VerificationResult(verified=True)
    assert vr.actual_price is None
    assert vr.flights == []
    assert vr.price_insights == {}


def test_verification_result_full():
    vr = VerificationResult(
        verified=True,
        actual_price=485.0,
        booking_url="https://example.com",
        price_insights={"lowest_price": 485},
        flights=[{"price": 485}],
    )
    assert vr.actual_price == 485.0
    assert len(vr.flights) == 1


# --- SerpAPIClient.search_flights (mocked) ---

@pytest.mark.asyncio
async def test_search_flights_success():
    client = SerpAPIClient(api_key="test-key", currency="EUR")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "best_flights": [{"price": 485}],
        "other_flights": [{"price": 520}],
        "price_insights": {"lowest_price": 485, "price_level": "low"},
        "booking_options": [],
    }

    client._client = AsyncMock()
    client._client.get.return_value = mock_response

    result = await client.search_flights("AMS", "NRT", "2026-10-01", "2026-10-15")
    assert len(result.best_flights) == 1
    assert result.price_insights["lowest_price"] == 485


@pytest.mark.asyncio
async def test_search_flights_http_error():
    client = SerpAPIClient(api_key="test-key")
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    client._client = AsyncMock()
    client._client.get.return_value = mock_response

    with pytest.raises(SerpAPIError, match="HTTP 500"):
        await client.search_flights("AMS", "NRT", "2026-10-01")


@pytest.mark.asyncio
async def test_search_flights_api_error():
    client = SerpAPIClient(api_key="test-key")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"error": "Invalid API key"}

    client._client = AsyncMock()
    client._client.get.return_value = mock_response

    with pytest.raises(SerpAPIError, match="Invalid API key"):
        await client.search_flights("AMS", "NRT", "2026-10-01")


# --- Rate limit warning ---

def test_rate_limit_warning_750(caplog):
    import logging
    client = SerpAPIClient(api_key="test-key")
    client._calls_this_month = 750
    with caplog.at_level(logging.WARNING):
        client._warn_rate_limit()
    assert "usage high" in caplog.text


def test_rate_limit_warning_900(caplog):
    import logging
    client = SerpAPIClient(api_key="test-key")
    client._calls_this_month = 900
    with caplog.at_level(logging.WARNING):
        client._warn_rate_limit()
    assert "usage critical" in caplog.text


def test_reset_monthly_counter():
    client = SerpAPIClient(api_key="test-key")
    client._calls_this_month = 100
    client.reset_monthly_counter()
    assert client._calls_this_month == 0


# --- extract_lowest_price ---

from src.apis.serpapi import extract_lowest_price, extract_min_duration, SerpAPIBudgetExhausted


def test_extract_lowest_price_from_flights():
    result = FlightSearchResult(
        best_flights=[{"price": 500, "flights": [{"airline": "KL"}]}],
        other_flights=[{"price": 600}, {"price": 450}],
        price_insights={"lowest_price": 480},
    )
    assert extract_lowest_price(result) == 450


def test_extract_lowest_price_fallback_to_insights():
    result = FlightSearchResult(
        best_flights=[{"flights": [{"airline": "KL"}]}],  # no price key
        other_flights=[],
        price_insights={"lowest_price": 480},
    )
    assert extract_lowest_price(result) == 480


def test_extract_lowest_price_empty():
    result = FlightSearchResult(
        best_flights=[],
        other_flights=[],
        price_insights={},
    )
    assert extract_lowest_price(result) is None


def test_extract_lowest_price_with_max_stops_filter():
    result = FlightSearchResult(
        best_flights=[
            {"price": 400, "flights": [{"a": 1}, {"a": 2}, {"a": 3}]},  # 2 stops
            {"price": 500, "flights": [{"a": 1}, {"a": 2}]},  # 1 stop
        ],
        other_flights=[
            {"price": 450, "flights": [{"a": 1}]},  # direct
        ],
    )
    # max_stops=1 should exclude the 2-stop flight at 400
    assert extract_lowest_price(result, max_stops=1) == 450


def test_extract_lowest_price_max_stops_filters_all():
    result = FlightSearchResult(
        best_flights=[
            {"price": 400, "flights": [{"a": 1}, {"a": 2}, {"a": 3}]},  # 2 stops
        ],
        other_flights=[],
        price_insights={"lowest_price": 600},
    )
    # max_stops=0 filters all flights, falls back to insights
    assert extract_lowest_price(result, max_stops=0) == 600


# --- extract_min_duration ---

def test_extract_min_duration():
    result = FlightSearchResult(
        best_flights=[{"total_duration": 720}],
        other_flights=[{"total_duration": 600}, {"total_duration": 840}],
    )
    assert extract_min_duration(result) == 600


def test_extract_min_duration_empty():
    result = FlightSearchResult(best_flights=[], other_flights=[])
    assert extract_min_duration(result) is None


def test_extract_min_duration_missing_key():
    result = FlightSearchResult(
        best_flights=[{"price": 500}],  # no total_duration
        other_flights=[{"total_duration": 600}],
    )
    assert extract_min_duration(result) == 600


# --- SerpAPIBudgetExhausted ---

def test_budget_exhausted_at_hard_cap():
    client = SerpAPIClient(api_key="test-key")
    client._calls_this_month = 950
    with pytest.raises(SerpAPIBudgetExhausted):
        client._warn_rate_limit()
