from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

SERPAPI_BASE_URL = "https://serpapi.com/search"

# Monthly budget thresholds (SerpAPI calls)
MONTHLY_BUDGET_WARN = 700
MONTHLY_BUDGET_ERROR = 900
MONTHLY_BUDGET_HARD_CAP = 950


class SerpAPIError(Exception):
    """Raised when SerpAPI returns an error or unexpected response."""


class SerpAPIBudgetExhausted(SerpAPIError):
    """Raised when monthly SerpAPI budget hard cap is reached."""


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

    def parse_baggage(
        self,
        airline_code: str | None,
        leg_distance_km: float | None,
        baggage_needs: str | None,
        currency: str = "EUR",
    ) -> dict:
        """Extract baggage cost per direction from this response, falling back to the airline table.

        Returns a dict with shape `{outbound, return, source, currency}` per release plan §8.1.
        - `source = "serpapi"` when at least one extension was successfully parsed.
        - `source = "fallback_table"` when SerpAPI was silent and the airline lookup produced fees.
        - `source = "unknown"` when both paths returned zero — renderer suppresses the line.
        """
        from src.utils.baggage import parse_baggage_extensions, estimate

        outbound = None
        ret = None
        source = "fallback_table"

        try:
            # 1. Primary: booking_options[].together.extensions
            for opt in self.booking_options or []:
                if not isinstance(opt, dict):
                    continue
                together = opt.get("together") or {}
                exts = together.get("extensions") if isinstance(together, dict) else None
                parsed = parse_baggage_extensions(exts)
                if parsed:
                    outbound = parsed
                    ret = parsed
                    source = "serpapi"
                    break

            # 2. Secondary: scan flight-leg extensions for outbound vs return separately.
            if outbound is None:
                all_flights = (self.best_flights or []) + (self.other_flights or [])
                for f in all_flights:
                    if not isinstance(f, dict):
                        continue
                    legs = f.get("flights") or []
                    if not legs:
                        continue
                    # First leg is outbound; last leg is return for round-trips.
                    out_parsed = parse_baggage_extensions(legs[0].get("extensions"))
                    ret_parsed = (
                        parse_baggage_extensions(legs[-1].get("extensions"))
                        if len(legs) > 1 else out_parsed
                    )
                    if out_parsed or ret_parsed:
                        outbound = out_parsed
                        ret = ret_parsed or out_parsed
                        source = "serpapi"
                        break
        except Exception:
            logger.exception("Baggage parsing crashed; falling back to airline table")

        if outbound is None:
            outbound = estimate(airline_code, leg_distance_km, baggage_needs)
            ret = estimate(airline_code, leg_distance_km, baggage_needs)
            source = "fallback_table"

        # If both directions are zero AND we used the fallback table, mark as unknown so the
        # renderer suppresses the "+ €0 bags" line per Condition C5.
        total = (
            (outbound.get("carry_on", 0) or 0) + (outbound.get("checked", 0) or 0)
            + (ret.get("carry_on", 0) or 0) + (ret.get("checked", 0) or 0)
        )
        if total == 0 and source == "fallback_table":
            source = "unknown"

        return {
            "outbound": outbound,
            "return": ret,
            "source": source,
            "currency": currency,
        }


@dataclass
class DirectionsResult:
    """Distance + duration result from SerpAPI's google_maps_directions engine."""
    distance_km: float
    duration_min: int
    mode: str  # canonical: 'drive' or 'transit'


def _parse_directions_response(data: dict, canonical_mode: str) -> DirectionsResult:
    """Parse SerpAPI google_maps_directions response into a DirectionsResult.

    Defensive — SerpAPI's scraped response shape varies slightly by route, so
    we walk a couple of plausible paths and fall through to a SerpAPIError
    rather than crashing the onboarding caller.
    """
    # SerpAPI google_maps_directions returns `directions[]` with each direction
    # containing `travel_modes[]` whose entries have `total_duration` (seconds)
    # and `distance` (text + meters). Some responses put the values directly
    # on the top-level direction entry. We try both shapes.
    try:
        directions = data.get("directions") or []
        if not directions:
            raise SerpAPIError("SerpAPI directions: no routes returned")
        first = directions[0]
        # Shape A: travel_modes[0] contains the per-mode totals.
        modes_list = first.get("travel_modes") or []
        if modes_list:
            mode_entry = modes_list[0]
            total_seconds = mode_entry.get("total_duration") or 0
            distance_meters = (
                mode_entry.get("distance", {}).get("value")
                if isinstance(mode_entry.get("distance"), dict)
                else mode_entry.get("distance")
            ) or 0
        else:
            # Shape B: totals on the direction itself.
            total_seconds = first.get("total_duration") or first.get("duration", {}).get("value") or 0
            distance_meters = (
                first.get("distance", {}).get("value")
                if isinstance(first.get("distance"), dict)
                else first.get("distance")
            ) or 0
        if not total_seconds or not distance_meters:
            raise SerpAPIError(
                f"SerpAPI directions: missing duration/distance in response"
            )
        return DirectionsResult(
            distance_km=round(float(distance_meters) / 1000, 1),
            duration_min=int(round(float(total_seconds) / 60)),
            mode=canonical_mode,
        )
    except SerpAPIError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise SerpAPIError(f"SerpAPI directions parse failed: {e}") from e


def extract_lowest_price(
    result: FlightSearchResult,
    max_stops: int | None = None,
) -> float | None:
    """Extract the lowest price from a FlightSearchResult.

    Tries min(price) from best_flights + other_flights first (optionally
    filtered by max_stops), falls back to price_insights.lowest_price.
    """
    all_flights = result.best_flights + result.other_flights
    if max_stops is not None:
        all_flights = [
            f for f in all_flights
            if len(f.get("flights", [])) - 1 <= max_stops
        ]
    prices = [f["price"] for f in all_flights if "price" in f]
    if prices:
        return min(prices)
    return result.price_insights.get("lowest_price")


def extract_min_duration(result: FlightSearchResult) -> int | None:
    """Extract the minimum total_duration (minutes) from a FlightSearchResult."""
    all_flights = result.best_flights + result.other_flights
    durations = [f["total_duration"] for f in all_flights if "total_duration" in f]
    return min(durations) if durations else None


class SerpAPIClient:
    def __init__(self, api_key: str, currency: str = "EUR", cache_dir: str | None = None) -> None:
        self.api_key = api_key
        self.currency = currency
        self._calls_this_month: int = 0
        self._client = httpx.AsyncClient(timeout=60.0)
        self._cache = None
        if cache_dir:
            from src.apis.serpapi_cache import ResponseCache
            self._cache = ResponseCache(cache_dir)

    async def close(self) -> None:
        await self._client.aclose()

    async def search_flights(
        self,
        origin: str,
        destination: str,
        outbound_date: str | date,
        return_date: str | date | None = None,
        passengers: int = 2,
        trip_type: str = "round_trip",
        max_stops: int | None = None,
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

        if max_stops is not None:
            params["stops"] = max_stops + 1

        self._warn_rate_limit()

        logger.info(
            "Searching flights %s → %s (%s to %s, %d pax)",
            origin, destination, outbound_date, return_date, passengers,
        )

        # Check cache first
        if self._cache:
            cached = self._cache.get(params)
            if cached:
                logger.info("Using cached response for %s → %s", origin, destination)
                data = cached
                # Skip to result building (below)
                if "error" in data:
                    raise SerpAPIError(f"SerpAPI error: {data['error']}")
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
                logger.info("Results: %d best + %d other flights, lowest=€%s, level=%s",
                    len(result.best_flights), len(result.other_flights), lowest, level)
                return result

        response = await self._client.get(SERPAPI_BASE_URL, params=params)

        self._calls_this_month += 1

        if response.status_code != 200:
            logger.error("SerpAPI HTTP %d: %s", response.status_code, response.text[:500])
            raise SerpAPIError(f"SerpAPI returned HTTP {response.status_code}")

        data = response.json()

        # Cache the response
        if self._cache:
            self._cache.put(params, data)

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

        insights_lowest = result.price_insights.get("lowest_price")
        level = result.price_insights.get("price_level")
        n_best = len(result.best_flights)
        n_other = len(result.other_flights)
        logger.info(
            "Results: %d best + %d other flights, lowest=€%s, level=%s",
            n_best, n_other, insights_lowest, level,
        )

        # Compare price_insights.lowest_price vs actual flight prices
        actual_lowest = extract_lowest_price(result)
        logger.debug(
            "Price check: insights=€%s, extract=€%s",
            insights_lowest, actual_lowest,
        )
        if insights_lowest and actual_lowest and insights_lowest > 0:
            divergence = abs(actual_lowest - insights_lowest) / insights_lowest
            if divergence > 0.20:
                logger.warning(
                    "Price divergence >20%%: insights=€%s vs extract=€%s (%.0f%%)",
                    insights_lowest, actual_lowest, divergence * 100,
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

    async def directions(
        self,
        *,
        origin: str,
        destination: str,
        mode: str = "drive",
    ) -> "DirectionsResult":
        """Look up driving / transit distance + duration via SerpAPI's
        google_maps_directions engine.

        R9 ITEM-053: used during onboarding to auto-fill drive distance and
        transit duration between the user's home location and each nearby
        airport. Reuses the existing SerpAPI plan budget — no separate Google
        Cloud account or quota / billing setup required.

        `origin` / `destination` accept free-form strings (city names,
        addresses, IATA codes); SerpAPI passes them through to Google Maps
        which handles geocoding.

        `mode` is one of: drive | driving | car (= driving), train | transit |
        bus (= transit). Mapped to Google's travel_mode encoding internally.
        """
        # Google Maps travel_mode codes: 0=driving, 3=transit, 2=walking, 1=bicycling.
        # We only ever need driving and transit for ITEM-053.
        m = mode.lower().strip()
        if m in {"drive", "driving", "car"}:
            travel_mode = 0
            canonical_mode = "drive"
        elif m in {"train", "transit", "bus", "public transport"}:
            travel_mode = 3
            canonical_mode = "transit"
        else:
            raise SerpAPIError(f"Unsupported travel mode: {mode}")

        params: dict[str, str | int] = {
            "engine": "google_maps_directions",
            "api_key": self.api_key,
            "start_addr": origin,
            "end_addr": destination,
            "travel_mode": travel_mode,
            "hl": "en",
        }

        # Cache (saves quota when same query repeats — common during testing).
        if self._cache:
            cached = self._cache.get(params)
            if cached:
                return _parse_directions_response(cached, canonical_mode)

        self._warn_rate_limit()

        response = await self._client.get(SERPAPI_BASE_URL, params=params)
        self._calls_this_month += 1
        if response.status_code != 200:
            logger.error("SerpAPI directions HTTP %d: %s", response.status_code, response.text[:500])
            raise SerpAPIError(f"SerpAPI directions returned HTTP {response.status_code}")
        data = response.json()
        if "error" in data:
            raise SerpAPIError(f"SerpAPI directions error: {data['error']}")

        if self._cache:
            self._cache.put(params, data)

        return _parse_directions_response(data, canonical_mode)

    def _warn_rate_limit(self) -> None:
        if self._calls_this_month >= MONTHLY_BUDGET_HARD_CAP:
            raise SerpAPIBudgetExhausted(
                f"SerpAPI monthly budget exhausted: {self._calls_this_month} calls"
            )
        elif self._calls_this_month >= MONTHLY_BUDGET_ERROR:
            logger.error(
                "SerpAPI usage critical: %d calls this month (hard cap: %d)",
                self._calls_this_month, MONTHLY_BUDGET_HARD_CAP,
            )
        elif self._calls_this_month >= MONTHLY_BUDGET_WARN:
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
