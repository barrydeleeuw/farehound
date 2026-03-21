from __future__ import annotations

from datetime import date

from src.config import Route as ConfigRoute
from src.orchestrator import _config_route_to_db


def test_config_route_to_db_basic():
    config_route = ConfigRoute(
        id="ams-nrt-oct",
        origin="AMS",
        destination="NRT",
        trip_type="round_trip",
        earliest_departure="2026-10-01",
        latest_return="2026-10-31",
        date_flexibility_days=3,
        max_stops=1,
        passengers=2,
        preferred_airlines=["KLM"],
        notes="Japan trip",
    )
    db_route = _config_route_to_db(config_route)
    assert db_route.route_id == "ams-nrt-oct"
    assert db_route.origin == "AMS"
    assert db_route.destination == "NRT"
    assert db_route.earliest_departure == date(2026, 10, 1)
    assert db_route.latest_return == date(2026, 10, 31)
    assert db_route.date_flex_days == 3
    assert db_route.max_stops == 1
    assert db_route.passengers == 2
    assert db_route.preferred_airlines == ["KLM"]
    assert db_route.notes == "Japan trip"
    assert db_route.active is True


def test_config_route_to_db_no_dates():
    config_route = ConfigRoute(
        id="r1",
        origin="AMS",
        destination="IST",
    )
    db_route = _config_route_to_db(config_route)
    assert db_route.earliest_departure is None
    assert db_route.latest_return is None


def test_config_route_to_db_maps_flexibility():
    """date_flexibility_days in config maps to date_flex_days in DB model."""
    config_route = ConfigRoute(
        id="r1",
        origin="AMS",
        destination="IST",
        date_flexibility_days=7,
    )
    db_route = _config_route_to_db(config_route)
    assert db_route.date_flex_days == 7
