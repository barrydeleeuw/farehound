"""R9 ITEM-053: Google Maps Distance Matrix client + curated airport data."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.apis.google_maps import (
    GoogleMapsClient,
    GoogleMapsError,
    GoogleMapsKeyMissing,
    estimate_drive_cost_eur,
    estimate_taxi_cost_eur,
)
from src.utils.airport_data import (
    find_nearby_airports,
    get_airport_meta,
    get_parking_rate,
    get_train_fare,
)


# ---------- Cost heuristics ----------


def test_estimate_drive_cost_default_rate():
    # 100 km × €0.25/km = €25
    assert estimate_drive_cost_eur(100) == 25.0


def test_estimate_drive_cost_custom_rate():
    assert estimate_drive_cost_eur(100, per_km_eur=0.30) == 30.0


def test_estimate_taxi_cost_default():
    # 50 km × €2.50/km = €125
    assert estimate_taxi_cost_eur(50) == 125.0


# ---------- GoogleMapsClient ----------


@pytest.mark.asyncio
async def test_client_without_key_raises_typed_exception():
    client = GoogleMapsClient(api_key=None)
    assert client.is_configured is False
    with pytest.raises(GoogleMapsKeyMissing):
        await client.directions(origin="Amsterdam", destination="Schiphol")
    await client.close()


@pytest.mark.asyncio
async def test_client_with_empty_string_key_raises():
    client = GoogleMapsClient(api_key="   ")
    assert client.is_configured is False
    with pytest.raises(GoogleMapsKeyMissing):
        await client.directions(origin="Amsterdam", destination="Schiphol")
    await client.close()


@pytest.mark.asyncio
async def test_client_directions_success_drive():
    client = GoogleMapsClient(api_key="test-key")
    fake_response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://maps.googleapis.com/maps/api/distancematrix/json"),
        json={
            "status": "OK",
            "rows": [{
                "elements": [{
                    "status": "OK",
                    "distance": {"value": 23000, "text": "23 km"},
                    "duration": {"value": 1200, "text": "20 mins"},
                }]
            }],
        },
    )
    with patch.object(client._client, "get", AsyncMock(return_value=fake_response)):
        result = await client.directions(
            origin="52.3,4.7", destination="AMS Airport", mode="drive"
        )
    assert result.distance_km == 23.0
    assert result.duration_min == 20
    assert result.mode == "drive"
    await client.close()


@pytest.mark.asyncio
async def test_client_directions_success_transit():
    client = GoogleMapsClient(api_key="test-key")
    fake_response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://maps.googleapis.com/maps/api/distancematrix/json"),
        json={
            "status": "OK",
            "rows": [{
                "elements": [{
                    "status": "OK",
                    "distance": {"value": 38000, "text": "38 km"},
                    "duration": {"value": 2700, "text": "45 mins"},
                }]
            }],
        },
    )
    with patch.object(client._client, "get", AsyncMock(return_value=fake_response)):
        result = await client.directions(
            origin="Amsterdam", destination="EIN Airport", mode="train"
        )
    assert result.distance_km == 38.0
    assert result.duration_min == 45
    assert result.mode == "transit"
    await client.close()


@pytest.mark.asyncio
async def test_client_directions_unsupported_mode_raises():
    client = GoogleMapsClient(api_key="test-key")
    with pytest.raises(GoogleMapsError, match="Unsupported mode"):
        await client.directions(origin="A", destination="B", mode="teleport")
    await client.close()


@pytest.mark.asyncio
async def test_client_directions_api_error_status_raises():
    client = GoogleMapsClient(api_key="test-key")
    fake_response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://maps.googleapis.com/maps/api/distancematrix/json"),
        json={"status": "REQUEST_DENIED", "error_message": "Invalid key"},
    )
    with patch.object(client._client, "get", AsyncMock(return_value=fake_response)):
        with pytest.raises(GoogleMapsError, match="REQUEST_DENIED"):
            await client.directions(origin="A", destination="B")
    await client.close()


@pytest.mark.asyncio
async def test_client_directions_no_route_raises():
    client = GoogleMapsClient(api_key="test-key")
    fake_response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://maps.googleapis.com/maps/api/distancematrix/json"),
        json={"status": "OK", "rows": [{"elements": [{"status": "ZERO_RESULTS"}]}]},
    )
    with patch.object(client._client, "get", AsyncMock(return_value=fake_response)):
        with pytest.raises(GoogleMapsError, match="No route found"):
            await client.directions(origin="A", destination="B")
    await client.close()


@pytest.mark.asyncio
async def test_client_directions_http_error_wrapped():
    client = GoogleMapsClient(api_key="test-key")
    with patch.object(
        client._client, "get",
        AsyncMock(side_effect=httpx.HTTPError("network died")),
    ):
        with pytest.raises(GoogleMapsError, match="HTTP error"):
            await client.directions(origin="A", destination="B")
    await client.close()


# ---------- Curated dataset loaders ----------


def test_get_parking_rate_known_airport():
    rate = get_parking_rate("AMS")
    assert rate is not None
    assert rate > 0


def test_get_parking_rate_case_insensitive():
    assert get_parking_rate("ams") == get_parking_rate("AMS")


def test_get_parking_rate_unknown_returns_none():
    assert get_parking_rate("ZZZ") is None


def test_get_train_fare_known_pair():
    fare = get_train_fare("amsterdam", "AMS")
    assert fare is not None
    assert fare > 0


def test_get_train_fare_case_insensitive():
    assert get_train_fare("AMSTERDAM", "ams") == get_train_fare("amsterdam", "AMS")


def test_get_train_fare_unknown_returns_none():
    assert get_train_fare("nowhere", "ZZZ") is None


def test_find_nearby_airports_within_radius():
    # Amsterdam centre: lat 52.37, lng 4.90.
    nearby = find_nearby_airports(
        lat=52.37, lng=4.90, max_km=100, limit=5,
    )
    codes = [a["iata"] for a in nearby]
    assert "AMS" in codes  # Schiphol is right there
    # Sorted by distance, nearest first.
    distances = [a["distance_km"] for a in nearby]
    assert distances == sorted(distances)


def test_find_nearby_airports_excludes_home():
    nearby = find_nearby_airports(
        lat=52.37, lng=4.90, max_km=200, limit=5, exclude={"AMS"}
    )
    codes = [a["iata"] for a in nearby]
    assert "AMS" not in codes


def test_find_nearby_airports_respects_radius():
    # Within 50km of Amsterdam should NOT include FRA (Frankfurt).
    nearby = find_nearby_airports(lat=52.37, lng=4.90, max_km=50, limit=10)
    codes = [a["iata"] for a in nearby]
    assert "FRA" not in codes


def test_find_nearby_airports_limit_respected():
    nearby = find_nearby_airports(lat=52.37, lng=4.90, max_km=2000, limit=3)
    assert len(nearby) == 3


def test_get_airport_meta_known():
    meta = get_airport_meta("AMS")
    assert meta is not None
    assert meta["name"].startswith("Amsterdam")
    assert "lat" in meta
    assert "lng" in meta


def test_get_airport_meta_unknown():
    assert get_airport_meta("ZZZ") is None


# ---------- Schema sanity for the curated JSON files ----------


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
