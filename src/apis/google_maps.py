"""Google Maps Distance Matrix API client.

R9 ITEM-053: used during onboarding to auto-fill drive + transit duration/distance
between the user's home location and each nearby airport. Free tier ($200/mo
credit) covers ~40,000 element-pairs/month — vastly exceeds expected usage.

Graceful skip: if `google_maps_api_key` is unset, every call raises
`GoogleMapsKeyMissing` which the caller catches and falls back to the
conversational onboarding flow (legacy ITEM-045).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

# Google Distance Matrix supports: driving, walking, bicycling, transit.
# We map our internal mode names to Google's expected `mode` param.
_MODE_MAP = {
    "drive": "driving",
    "driving": "driving",
    "car": "driving",
    "transit": "transit",
    "train": "transit",
    "bus": "transit",
    "walk": "walking",
    "walking": "walking",
}


class GoogleMapsError(Exception):
    """Raised on API error or unexpected response."""


class GoogleMapsKeyMissing(GoogleMapsError):
    """Raised when no API key is configured. Caller should fall back to
    conversational onboarding."""


@dataclass
class DirectionsResult:
    distance_km: float
    duration_min: int
    mode: str  # canonical: 'drive' or 'transit'


class GoogleMapsClient:
    """Thin wrapper around the Distance Matrix API.

    One instance per orchestrator, reused across onboarding sessions. The
    `api_key` is read once at construction; if absent, every call raises
    `GoogleMapsKeyMissing` so the caller's try/except is the only branch
    that needs to handle the key-absent case.
    """

    def __init__(self, api_key: str | None = None, timeout: float = 10.0) -> None:
        self._api_key = api_key.strip() if api_key else None
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def close(self) -> None:
        await self._client.aclose()

    async def directions(
        self,
        *,
        origin: str,
        destination: str,
        mode: str = "drive",
    ) -> DirectionsResult:
        """Look up distance + duration between two locations.

        `origin` and `destination` are free-form: lat/lng strings ("52.31,4.76"),
        addresses ("Schiphol Airport, Netherlands"), or IATA-code-style names
        ("AMS Airport"). Google handles the geocoding; we just pass through.

        `mode` is one of: drive, transit, train, bus, walking. Mapped internally
        to Google's API values.
        """
        if not self._api_key:
            raise GoogleMapsKeyMissing(
                "google_maps_api_key not configured — "
                "falling back to conversational onboarding"
            )
        google_mode = _MODE_MAP.get(mode.lower())
        if google_mode is None:
            raise GoogleMapsError(f"Unsupported mode: {mode}")
        params = {
            "origins": origin,
            "destinations": destination,
            "mode": google_mode,
            "units": "metric",
            "key": self._api_key,
        }
        try:
            response = await self._client.get(DISTANCE_MATRIX_URL, params=params)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise GoogleMapsError(f"Distance Matrix HTTP error: {e}") from e

        data = response.json()
        if data.get("status") != "OK":
            raise GoogleMapsError(
                f"Distance Matrix returned status={data.get('status')}: "
                f"{data.get('error_message', '')}"
            )
        try:
            element = data["rows"][0]["elements"][0]
        except (KeyError, IndexError) as e:
            raise GoogleMapsError(f"Unexpected response shape: {data}") from e
        if element.get("status") != "OK":
            raise GoogleMapsError(
                f"No route found ({element.get('status')}) "
                f"from '{origin}' to '{destination}' by {mode}"
            )

        distance_m = float(element["distance"]["value"])
        duration_s = float(element["duration"]["value"])
        canonical_mode = "transit" if google_mode == "transit" else "drive"
        return DirectionsResult(
            distance_km=round(distance_m / 1000, 1),
            duration_min=int(round(duration_s / 60)),
            mode=canonical_mode,
        )


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
