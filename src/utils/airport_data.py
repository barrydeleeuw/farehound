"""Loaders for the curated R9 ITEM-053 datasets.

Three JSON files under `data/`:
  - airport_parking.json   — daily parking rates for ~25 EU airports
  - train_fares_eu.json    — RT/pp train fare estimates for common home→airport pairs
  - viable_airports_eu.json — allow-list with lat/lng for nearby-airport discovery

Loaded lazily on first access; cached process-wide. Datasets are immutable
configuration; if a user wants to override a value, they edit it in Settings,
which writes the override into airport_transport_option (source='user_override').
"""

from __future__ import annotations

import json
import logging
import math
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


@lru_cache(maxsize=1)
def _load_parking() -> dict:
    path = _DATA_DIR / "airport_parking.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("airports", {})
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not load airport_parking.json: %s", e)
        return {}


@lru_cache(maxsize=1)
def _load_train_fares() -> dict:
    path = _DATA_DIR / "train_fares_eu.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("fares", {})
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not load train_fares_eu.json: %s", e)
        return {}


@lru_cache(maxsize=1)
def _load_viable_airports() -> list[dict]:
    path = _DATA_DIR / "viable_airports_eu.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("airports", [])
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not load viable_airports_eu.json: %s", e)
        return []


def get_parking_rate(airport_code: str) -> float | None:
    """Return curated daily parking rate (EUR) for an airport, or None if not curated."""
    entry = _load_parking().get(airport_code.upper())
    return float(entry["daily_eur"]) if entry else None


def get_train_fare(origin_city: str, airport_code: str) -> float | None:
    """Return curated RT/pp train fare (EUR) for a home-city → airport pair, or None."""
    key = f"{origin_city.lower().strip()}|{airport_code.upper()}"
    entry = _load_train_fares().get(key)
    return float(entry["rt_per_pp_eur"]) if entry else None


def find_nearby_airports(
    *, lat: float, lng: float, max_km: float = 200.0, limit: int = 3,
    exclude: set[str] | None = None,
) -> list[dict]:
    """Return airports from the viable-list within `max_km`, sorted nearest-first.

    `exclude` is a set of IATA codes to skip (e.g. the user's home airport).
    """
    exclude = {c.upper() for c in (exclude or set())}
    candidates = []
    for ap in _load_viable_airports():
        if ap["iata"].upper() in exclude:
            continue
        d = _haversine_km(lat, lng, ap["lat"], ap["lng"])
        if d <= max_km:
            candidates.append({**ap, "distance_km": round(d, 1)})
    candidates.sort(key=lambda x: x["distance_km"])
    return candidates[:limit]


def get_airport_meta(airport_code: str) -> dict | None:
    """Return the viable-list entry (with lat/lng/name) for a code, or None."""
    code = airport_code.upper()
    for ap in _load_viable_airports():
        if ap["iata"].upper() == code:
            return ap
    return None


def estimate_drive_cost_eur(distance_km: float, per_km_eur: float = 0.25) -> float:
    """Heuristic: petrol + wear estimate for a private car.

    Default rate is the ANWB ballpark for a midsize EU car. User can override
    via Settings (changes the stored cost_eur, not this rate).
    """
    return round(distance_km * per_km_eur, 2)


def estimate_taxi_cost_eur(distance_km: float, per_km_eur: float = 2.50) -> float:
    """Heuristic: rough EU taxi rate. No public API for fares.

    The user is expected to confirm/override at onboarding.
    """
    return round(distance_km * per_km_eur, 2)


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two lat/lng points in kilometres."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
