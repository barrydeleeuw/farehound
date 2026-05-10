"""R9 ITEM-053: SerpAPI google_maps_directions wrapper + curated airport datasets."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.apis.serpapi import (
    DirectionsResult,
    SerpAPIClient,
    SerpAPIError,
    _parse_directions_response,
)
from src.utils.airport_data import (
    estimate_drive_cost_eur,
    estimate_taxi_cost_eur,
    find_nearby_airports,
    get_airport_meta,
    get_parking_rate,
    get_train_fare,
)


# ---------- Cost heuristics ----------


def test_estimate_drive_cost_default_rate():
    assert estimate_drive_cost_eur(100) == 25.0


def test_estimate_drive_cost_custom_rate():
    assert estimate_drive_cost_eur(100, per_km_eur=0.30) == 30.0


def test_estimate_taxi_cost_default():
    assert estimate_taxi_cost_eur(50) == 125.0


# ---------- _parse_directions_response (defensive parsing) ----------


def test_parse_directions_shape_a_travel_modes():
    """Shape A: travel_modes[0] contains the totals."""
    response = {
        "directions": [{
            "travel_modes": [{
                "total_duration": 1500,
                "distance": {"value": 23000, "text": "23 km"},
            }]
        }]
    }
    result = _parse_directions_response(response, canonical_mode="drive")
    assert result.distance_km == 23.0
    assert result.duration_min == 25
    assert result.mode == "drive"


def test_parse_directions_shape_b_inline():
    """Shape B: totals on the direction entry directly."""
    response = {
        "directions": [{
            "total_duration": 2700,
            "distance": {"value": 38000},
        }]
    }
    result = _parse_directions_response(response, canonical_mode="transit")
    assert result.distance_km == 38.0
    assert result.duration_min == 45
    assert result.mode == "transit"


def test_parse_directions_shape_b_with_duration_object():
    """Shape B variant: duration as object."""
    response = {
        "directions": [{
            "duration": {"value": 600},
            "distance": {"value": 5000},
        }]
    }
    result = _parse_directions_response(response, canonical_mode="drive")
    assert result.distance_km == 5.0
    assert result.duration_min == 10


def test_parse_directions_distance_as_number():
    """Some scraped responses have distance as a bare number, not an object."""
    response = {
        "directions": [{
            "travel_modes": [{
                "total_duration": 600,
                "distance": 5000,
            }]
        }]
    }
    result = _parse_directions_response(response, canonical_mode="drive")
    assert result.distance_km == 5.0


def test_parse_directions_no_directions_raises():
    with pytest.raises(SerpAPIError, match="no routes"):
        _parse_directions_response({"directions": []}, canonical_mode="drive")
    with pytest.raises(SerpAPIError, match="no routes"):
        _parse_directions_response({}, canonical_mode="drive")


def test_parse_directions_missing_data_raises():
    with pytest.raises(SerpAPIError, match="missing duration/distance"):
        _parse_directions_response(
            {"directions": [{"travel_modes": [{}]}]}, canonical_mode="drive"
        )


# ---------- SerpAPIClient.directions ----------


@pytest.mark.asyncio
async def test_directions_unsupported_mode_raises():
    client = SerpAPIClient(api_key="test-key")
    with pytest.raises(SerpAPIError, match="Unsupported travel mode"):
        await client.directions(origin="A", destination="B", mode="teleport")
    await client.close()


@pytest.mark.asyncio
async def test_directions_drive_success():
    client = SerpAPIClient(api_key="test-key")
    fake_response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://serpapi.com/search"),
        json={
            "directions": [{
                "travel_modes": [{
                    "total_duration": 1500,
                    "distance": {"value": 23000, "text": "23 km"},
                }]
            }]
        },
    )
    with patch.object(client._client, "get", AsyncMock(return_value=fake_response)):
        result = await client.directions(
            origin="amsterdam", destination="AMS Airport", mode="drive",
        )
    assert result.distance_km == 23.0
    assert result.duration_min == 25
    assert result.mode == "drive"
    await client.close()


@pytest.mark.asyncio
async def test_directions_transit_success():
    client = SerpAPIClient(api_key="test-key")
    fake_response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://serpapi.com/search"),
        json={
            "directions": [{
                "travel_modes": [{
                    "total_duration": 2700,
                    "distance": {"value": 38000},
                }]
            }]
        },
    )
    with patch.object(client._client, "get", AsyncMock(return_value=fake_response)):
        result = await client.directions(
            origin="amsterdam", destination="EIN Airport", mode="train",
        )
    assert result.duration_min == 45
    assert result.mode == "transit"
    await client.close()


@pytest.mark.asyncio
async def test_directions_serp_error_response():
    client = SerpAPIClient(api_key="test-key")
    fake_response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://serpapi.com/search"),
        json={"error": "Invalid API key"},
    )
    with patch.object(client._client, "get", AsyncMock(return_value=fake_response)):
        with pytest.raises(SerpAPIError, match="Invalid API key"):
            await client.directions(origin="A", destination="B")
    await client.close()


@pytest.mark.asyncio
async def test_directions_http_error():
    client = SerpAPIClient(api_key="test-key")
    fake_response = httpx.Response(
        500,
        request=httpx.Request("GET", "https://serpapi.com/search"),
        text="server error",
    )
    with patch.object(client._client, "get", AsyncMock(return_value=fake_response)):
        with pytest.raises(SerpAPIError, match="HTTP 500"):
            await client.directions(origin="A", destination="B")
    await client.close()


# ---------- Curated dataset loaders ----------


def test_get_parking_rate_known_airport():
    rate = get_parking_rate("AMS")
    assert rate is not None and rate > 0


def test_get_parking_rate_case_insensitive():
    assert get_parking_rate("ams") == get_parking_rate("AMS")


def test_get_parking_rate_unknown_returns_none():
    assert get_parking_rate("ZZZ") is None


def test_get_train_fare_known_pair():
    fare = get_train_fare("amsterdam", "AMS")
    assert fare is not None and fare > 0


def test_get_train_fare_case_insensitive():
    assert get_train_fare("AMSTERDAM", "ams") == get_train_fare("amsterdam", "AMS")


def test_get_train_fare_unknown_returns_none():
    assert get_train_fare("nowhere", "ZZZ") is None


def test_find_nearby_airports_within_radius():
    nearby = find_nearby_airports(lat=52.37, lng=4.90, max_km=100, limit=5)
    codes = [a["iata"] for a in nearby]
    assert "AMS" in codes
    distances = [a["distance_km"] for a in nearby]
    assert distances == sorted(distances)


def test_find_nearby_airports_excludes_home():
    nearby = find_nearby_airports(
        lat=52.37, lng=4.90, max_km=200, limit=5, exclude={"AMS"}
    )
    codes = [a["iata"] for a in nearby]
    assert "AMS" not in codes


def test_find_nearby_airports_respects_radius():
    nearby = find_nearby_airports(lat=52.37, lng=4.90, max_km=50, limit=10)
    codes = [a["iata"] for a in nearby]
    assert "FRA" not in codes  # Frankfurt is too far from Amsterdam


def test_find_nearby_airports_limit_respected():
    nearby = find_nearby_airports(lat=52.37, lng=4.90, max_km=2000, limit=3)
    assert len(nearby) == 3


def test_get_airport_meta_known():
    meta = get_airport_meta("AMS")
    assert meta is not None
    assert meta["name"].startswith("Amsterdam")


def test_get_airport_meta_unknown():
    assert get_airport_meta("ZZZ") is None


# ---------- Curated JSON file schema ----------


def test_parking_dataset_schema_sane():
    from src.utils.airport_data import _DATA_DIR
    data = json.loads((_DATA_DIR / "airport_parking.json").read_text())
    airports = data.get("airports", {})
    assert len(airports) >= 20
    for code, entry in airports.items():
        assert isinstance(code, str) and len(code) == 3
        assert "daily_eur" in entry and entry["daily_eur"] > 0
        assert "last_verified" in entry


def test_train_fares_dataset_schema_sane():
    from src.utils.airport_data import _DATA_DIR
    data = json.loads((_DATA_DIR / "train_fares_eu.json").read_text())
    fares = data.get("fares", {})
    assert len(fares) >= 10
    for key, entry in fares.items():
        assert "|" in key
        assert "rt_per_pp_eur" in entry and entry["rt_per_pp_eur"] > 0


def test_viable_airports_dataset_schema_sane():
    from src.utils.airport_data import _DATA_DIR
    data = json.loads((_DATA_DIR / "viable_airports_eu.json").read_text())
    airports = data.get("airports", [])
    assert len(airports) >= 25
    iatas = set()
    for ap in airports:
        assert "iata" in ap
        assert "lat" in ap and -90 <= ap["lat"] <= 90
        assert "lng" in ap and -180 <= ap["lng"] <= 180
        iatas.add(ap["iata"])
    assert len(iatas) == len(airports), "duplicate IATA codes in viable-airports list"
