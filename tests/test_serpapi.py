from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from src.apis.serpapi import (
    SerpAPIClient,
    SerpAPIError,
    FlightSearchResult,
    VerificationResult,
    generate_date_windows,
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


# --- build_google_flights_url ---

def test_build_google_flights_url_roundtrip():
    url = build_google_flights_url("AMS", "NRT", "2026-10-01", "2026-10-15")
    assert "AMS" in url
    assert "NRT" in url
    assert "d=2026-10-01" in url
    assert "r=2026-10-15" in url
    assert url.startswith("https://www.google.com/travel/flights")


def test_build_google_flights_url_oneway():
    url = build_google_flights_url("AMS", "NRT", date(2026, 10, 1))
    assert "AMS" in url
    assert "NRT" in url
    assert "r=" not in url


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

    with patch("src.apis.serpapi.httpx.AsyncClient") as mock_client_cls:
        mock_async_client = AsyncMock()
        mock_async_client.get.return_value = mock_response
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_async_client

        result = await client.search_flights("AMS", "NRT", "2026-10-01", "2026-10-15")
        assert len(result.best_flights) == 1
        assert result.price_insights["lowest_price"] == 485


@pytest.mark.asyncio
async def test_search_flights_http_error():
    client = SerpAPIClient(api_key="test-key")
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    with patch("src.apis.serpapi.httpx.AsyncClient") as mock_client_cls:
        mock_async_client = AsyncMock()
        mock_async_client.get.return_value = mock_response
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_async_client

        with pytest.raises(SerpAPIError, match="HTTP 500"):
            await client.search_flights("AMS", "NRT", "2026-10-01")


@pytest.mark.asyncio
async def test_search_flights_api_error():
    client = SerpAPIClient(api_key="test-key")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"error": "Invalid API key"}

    with patch("src.apis.serpapi.httpx.AsyncClient") as mock_client_cls:
        mock_async_client = AsyncMock()
        mock_async_client.get.return_value = mock_response
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_async_client

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
