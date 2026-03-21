from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

SERPAPI_BASE_URL = "https://serpapi.com/search"


class SerpAPIError(Exception):
    """Raised when SerpAPI returns an error or unexpected response."""


@dataclass
class VerificationResult:
    verified: bool
    actual_price: float | None = None
    booking_url: str | None = None
    price_insights: dict = field(default_factory=dict)
    flights: list[dict] = field(default_factory=list)


@dataclass
class FlightSearchResult:
    best_flights: list[dict] = field(default_factory=list)
    other_flights: list[dict] = field(default_factory=list)
    price_insights: dict = field(default_factory=dict)
    booking_options: list[dict] = field(default_factory=list)
    search_params: dict = field(default_factory=dict)
    raw_response: dict = field(default_factory=dict)


class SerpAPIClient:
    def __init__(self, api_key: str, currency: str = "EUR") -> None:
        self.api_key = api_key
        self.currency = currency
        self._calls_this_month: int = 0

    async def search_flights(
        self,
        origin: str,
        destination: str,
        outbound_date: str | date,
        return_date: str | date | None = None,
        passengers: int = 2,
        trip_type: str = "round_trip",
    ) -> FlightSearchResult:
        """Search Google Flights via SerpAPI.

        Args:
            origin: IATA airport code (e.g. "AMS").
            destination: IATA airport code (e.g. "NRT").
            outbound_date: Departure date (YYYY-MM-DD string or date object).
            return_date: Return date. Required for round trips.
            passengers: Number of adult passengers.
            trip_type: "round_trip" or "one_way".
        """
        type_code = 1 if trip_type == "round_trip" else 2

        params: dict[str, str | int] = {
            "engine": "google_flights",
            "api_key": self.api_key,
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": str(outbound_date),
            "type": type_code,
            "adults": passengers,
            "currency": self.currency,
            "hl": "en",
            "deep_search": "true",
            "sort_by": 2,
        }

        if return_date and trip_type == "round_trip":
            params["return_date"] = str(return_date)

        self._warn_rate_limit()

        logger.info(
            "Searching flights %s → %s (%s to %s, %d pax)",
            origin, destination, outbound_date, return_date, passengers,
        )

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(SERPAPI_BASE_URL, params=params)

        self._calls_this_month += 1

        if response.status_code != 200:
            logger.error("SerpAPI HTTP %d: %s", response.status_code, response.text[:500])
            raise SerpAPIError(f"SerpAPI returned HTTP {response.status_code}")

        data = response.json()

        if "error" in data:
            logger.error("SerpAPI error: %s", data["error"])
            raise SerpAPIError(f"SerpAPI error: {data['error']}")

        # Strip api_key from stored params
        safe_params = {k: v for k, v in params.items() if k != "api_key"}

        result = FlightSearchResult(
            best_flights=data.get("best_flights", []),
            other_flights=data.get("other_flights", []),
            price_insights=data.get("price_insights", {}),
            booking_options=data.get("booking_options", []),
            search_params=safe_params,
            raw_response=data,
        )

        lowest = result.price_insights.get("lowest_price")
        level = result.price_insights.get("price_level")
        n_best = len(result.best_flights)
        n_other = len(result.other_flights)
        logger.info(
            "Results: %d best + %d other flights, lowest=€%s, level=%s",
            n_best, n_other, lowest, level,
        )

        return result

    async def verify_fare(
        self,
        origin: str,
        destination: str,
        outbound_date: str | date,
        return_date: str | date | None = None,
        expected_price: float = 0,
        passengers: int = 2,
    ) -> VerificationResult:
        """Verify a community-flagged fare via SerpAPI (Layer 2).

        Searches Google Flights for the given route and compares the actual
        price against the expected price. A fare is considered verified if
        the actual price is within 20% of the expected price.

        Args:
            origin: IATA airport code.
            destination: IATA airport code.
            outbound_date: Departure date.
            return_date: Return date (optional for one-way).
            expected_price: Price reported by the community source.
            passengers: Number of adult passengers.

        Returns:
            VerificationResult with verification status, actual price,
            booking URL, price insights, and flight options.
        """
        logger.info(
            "Verifying fare %s → %s (expected €%.0f, %d pax)",
            origin, destination, expected_price, passengers,
        )

        result = await self.search_flights(
            origin=origin,
            destination=destination,
            outbound_date=outbound_date,
            return_date=return_date,
            passengers=passengers,
            trip_type="round_trip" if return_date else "one_way",
        )

        # Find the lowest actual price from results
        all_flights = result.best_flights + result.other_flights
        actual_price: float | None = None
        if all_flights:
            prices = [f["price"] for f in all_flights if "price" in f]
            if prices:
                actual_price = min(prices)

        # Fall back to price_insights lowest_price
        if actual_price is None:
            actual_price = result.price_insights.get("lowest_price")

        # Extract booking URL from booking_options or fall back to Google Flights URL
        booking_url: str | None = None
        for option in result.booking_options:
            together = option.get("together", {})
            req = together.get("booking_request", {})
            if req.get("url"):
                booking_url = req["url"]
                break
        if not booking_url:
            gf_url = result.raw_response.get("search_metadata", {}).get(
                "google_flights_url"
            )
            if gf_url:
                booking_url = gf_url

        # Verified if actual price is within 20% of expected
        verified = False
        if actual_price is not None and expected_price > 0:
            verified = actual_price <= expected_price * 1.2

        logger.info(
            "Verification: actual=€%s, expected=€%.0f, verified=%s",
            actual_price, expected_price, verified,
        )

        return VerificationResult(
            verified=verified,
            actual_price=actual_price,
            booking_url=booking_url,
            price_insights=result.price_insights,
            flights=all_flights,
        )

    def _warn_rate_limit(self) -> None:
        if self._calls_this_month >= 900:
            logger.warning(
                "SerpAPI usage critical: %d calls this month", self._calls_this_month
            )
        elif self._calls_this_month >= 750:
            logger.warning(
                "SerpAPI usage high: %d calls this month", self._calls_this_month
            )

    def reset_monthly_counter(self) -> None:
        """Reset the call counter. Call at the start of each billing month."""
        self._calls_this_month = 0


def generate_date_windows(
    earliest_departure: date,
    latest_return: date,
    trip_duration_days: int,
    max_windows: int = 4,
) -> list[tuple[date, date]]:
    """Generate evenly-spaced (outbound, return) date pairs across a travel range.

    Example: earliest=Oct 1, latest=Oct 31, duration=14, max_windows=4
    → [(Oct 1, Oct 15), (Oct 6, Oct 20), (Oct 11, Oct 25), (Oct 16, Oct 30)]
    """
    last_possible_outbound = latest_return - timedelta(days=trip_duration_days)

    if last_possible_outbound < earliest_departure:
        raise ValueError(
            f"Travel range too short: {earliest_departure} to {latest_return} "
            f"cannot fit a {trip_duration_days}-day trip"
        )

    total_span_days = (last_possible_outbound - earliest_departure).days

    if max_windows <= 1 or total_span_days == 0:
        outbound = earliest_departure
        return [(outbound, outbound + timedelta(days=trip_duration_days))]

    # Space windows evenly across the range of possible departure dates
    step = total_span_days / (max_windows - 1)
    windows: list[tuple[date, date]] = []

    for i in range(max_windows):
        outbound = earliest_departure + timedelta(days=round(step * i))
        ret = outbound + timedelta(days=trip_duration_days)
        if ret > latest_return:
            ret = latest_return
        windows.append((outbound, ret))

    return windows


def build_google_flights_url(
    origin: str,
    destination: str,
    outbound_date: str | date,
    return_date: str | date | None = None,
) -> str:
    """Construct a Google Flights search URL for use in alerts."""
    params = {
        "hl": "en",
        "curr": "EUR",
    }
    # Google Flights URL format: /travel/flights/s/origin/dest/date[/return_date]
    # But the query-param form is more reliable for deep links.
    tfs = f"CBwQAhoeEgoyMDI2LTEwLTAxagcIARIDQU1TcgcIARIDTlJU"  # opaque, not useful
    # Simpler approach: use the flights search page with query params
    base = "https://www.google.com/travel/flights"
    parts = [
        f"q=Flights+from+{origin}+to+{destination}",
        f"d={outbound_date}",
    ]
    if return_date:
        parts.append(f"r={return_date}")

    return f"{base}?{('&'.join(parts))}"
