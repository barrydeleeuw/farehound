from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import anthropic

from src.storage.models import PriceSnapshot

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a flight deal analyst with deep knowledge of airline pricing patterns, \
error fares, and historical deal trends. Respond with valid JSON only, no markdown.

When scoring deals, combine the hard data provided (current price, 90-day history, \
Google price insights) with your knowledge of pricing PATTERNS for each route: \
seasonal trends, typical sale periods, error fare likelihood, and what price ranges \
are considered exceptional vs normal. Do NOT cite specific historical prices — \
only use pattern knowledge (e.g. "Japan flights from Europe typically dip in Jan-Feb").

Scoring guidance:
- 0.9-1.0: Exceptional — price is near or below known historical lows / error fare territory
- 0.75-0.89: Good deal — clearly below typical pricing, worth booking
- 0.50-0.74: Decent — below average but not remarkable, worth watching
- 0.25-0.49: Typical — nothing special, wait for better
- 0.0-0.24: Overpriced — above average, definitely wait

IMPORTANT: Anchor your scoring primarily to the real data provided. Your historical \
knowledge should enrich the analysis, not override it. A price that is genuinely \
the lowest in 90 days of observations IS a good deal even if error fares have \
historically gone lower — the traveller can't wait forever for a unicorn."""

_SCORE_PROMPT = """\
TODAY: {today}
ROUTE: {origin} → {destination}
TRIP TYPE: {trip_type}
TRAVEL DATES: {outbound_date} to {return_date} (±{date_flex} days flexible)
PASSENGERS: {passengers}

CURRENT FARE:
{best_flight_json}

PRICE CONTEXT:
- Current lowest: €{lowest_price}
- Source: {source}
{price_history_section}
{serpapi_section}

TRAVELLER PREFERENCES:
- {traveller_name}, based at {home_airport}
- Travelling with {passengers} passenger(s)
{traveller_preferences_section}
{preferred_airlines_section}
{past_decisions_section}
{nearby_section}
Score this deal. Use these past decisions to calibrate your scoring. The traveller's revealed preferences from actual booking behavior matter more than stated preferences when they conflict. The "reasoning" field will be shown as a phone notification, so keep it to 2-3 short sentences that help the traveller decide: mention the price vs. history, any connection/timing concerns, and whether to act now or wait. Be specific (cite numbers), not generic. If a nearby airport offers significant savings, mention the best alternative in the reasoning.

Respond with JSON only:
{{
  "score": 0.0-1.0,
  "urgency": "book_now" | "watch" | "skip",
  "reasoning": "2-3 sentences — specific, actionable, phone-friendly",
  "booking_window_hours": estimated hours this fare is likely available
}}"""

_DEFAULT_TRAVELLER_PREFS = [
    "Prefers connections through quality hubs (DOH, IST, SIN) over regional airports",
    "Values reasonable layover times (1.5-4 hours)",
    "Departure flexibility: weekday evenings or weekends preferred",
    "Airline preferences: open, but values included baggage and legroom",
]


@dataclass
class DealScore:
    score: float
    urgency: str
    reasoning: str
    booking_window_hours: int


class DealScorer:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def score_deal(
        self,
        snapshot: PriceSnapshot,
        route,
        price_history: dict,
        community_flagged: bool = False,
        traveller_name: str = "Traveller",
        home_airport: str = "AMS",
        traveller_preferences: list[str] | None = None,
        past_feedback: list[dict] | None = None,
        nearby_comparison: list[dict] | None = None,
    ) -> DealScore:
        prompt = self._build_prompt(
            snapshot, route, price_history, community_flagged,
            traveller_name, home_airport, traveller_preferences,
            past_feedback, nearby_comparison,
        )

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        usage = response.usage
        logger.info(
            "Claude scoring: input_tokens=%d, output_tokens=%d",
            usage.input_tokens, usage.output_tokens,
        )

        if not response.content:
            raise ValueError("Empty response from Claude API")
        raw = response.content[0].text
        return self._parse_response(raw)

    def _build_prompt(
        self,
        snapshot: PriceSnapshot,
        route,
        price_history: dict,
        community_flagged: bool,
        traveller_name: str,
        home_airport: str,
        traveller_preferences: list[str] | None = None,
        past_feedback: list[dict] | None = None,
        nearby_comparison: list[dict] | None = None,
    ) -> str:
        # Price history section
        if price_history.get("count", 0) > 0:
            price_history_section = (
                f"- My observed average (last 90 days): €{price_history['avg_price']:,.0f}\n"
                f"- My observed minimum (last 90 days): €{price_history['min_price']:,.0f}\n"
                f"- My observed maximum (last 90 days): €{price_history['max_price']:,.0f}\n"
                f"- Sample count: {price_history['count']}"
            )
        else:
            price_history_section = "- No historical price data yet (first observation)"

        # SerpAPI price insights section
        serpapi_section = ""
        if snapshot.price_level or snapshot.typical_low:
            parts = []
            if snapshot.price_level:
                parts.append(f"- Google Flights price level: {snapshot.price_level}")
            if snapshot.typical_low and snapshot.typical_high:
                parts.append(
                    f"- Google Flights typical range: €{float(snapshot.typical_low):,.0f} - €{float(snapshot.typical_high):,.0f}"
                )
            if community_flagged:
                parts.append("- Possible error fare: YES (community flagged)")
            serpapi_section = "\n".join(parts)

        # Best flight JSON (compact)
        best_flight_json = json.dumps(snapshot.best_flight, indent=2) if snapshot.best_flight else "N/A"

        # Traveller preferences
        prefs = traveller_preferences if traveller_preferences else _DEFAULT_TRAVELLER_PREFS
        traveller_preferences_section = "\n".join(f"- {p}" for p in prefs)

        # Preferred airlines
        preferred = getattr(route, "preferred_airlines", None) or []
        preferred_airlines_section = (
            f"- Preferred airlines: {', '.join(preferred)}" if preferred else ""
        )

        # Past decisions section
        past_decisions_section = ""
        if past_feedback:
            lines = ["", "PAST DECISIONS (recent scored deals):"]
            for fb in past_feedback:
                label = (fb.get("feedback") or "Ignored").capitalize()
                route_str = f"{fb.get('origin', '?')}→{fb.get('destination', '?')}"
                price = fb.get("price")
                price_str = f" €{float(price):,.0f}" if price is not None else ""
                score = fb.get("score")
                score_str = f" — score {float(score):.2f}" if score is not None else ""
                lines.append(f"- {label}: {route_str}{price_str}{score_str}")
            lines.append("")
            past_decisions_section = "\n".join(lines)

        # Nearby airports section
        nearby_section = ""
        if nearby_comparison:
            from src.utils.airports import airport_name
            origin_name = airport_name(getattr(route, "origin", ""))
            lines = ["", "NEARBY AIRPORTS (door-to-door cost comparison):"]
            for alt in nearby_comparison:
                name = alt.get("airport_name") or alt.get("airport_code", "?")
                mode = alt.get("transport_mode", "transport")
                t_cost = alt.get("transport_cost", 0)
                t_min = alt.get("transport_time_min", 0)
                hours = t_min / 60
                fare = alt.get("fare_pp", 0)
                net = alt.get("net_cost", 0)
                savings = alt.get("savings", 0)
                lines.append(
                    f"- {name} ({mode}, €{t_cost:,.0f} return, {hours:.1f}h): "
                    f"€{fare:,.0f}/pp, €{net:,.0f} net → save €{savings:,.0f} vs {origin_name}"
                )
            lines.append("")
            nearby_section = "\n".join(lines)

        # Route fields — support both config Route and db Route
        origin = getattr(route, "origin", "")
        destination = getattr(route, "destination", "")
        trip_type = getattr(route, "trip_type", "round_trip")
        date_flex = getattr(route, "date_flex_days", None) or getattr(route, "date_flexibility_days", 3)
        passengers = getattr(route, "passengers", snapshot.passengers)

        return _SCORE_PROMPT.format(
            today=datetime.now(UTC).strftime("%Y-%m-%d"),
            origin=origin,
            destination=destination,
            trip_type=trip_type,
            outbound_date=snapshot.outbound_date or "flexible",
            return_date=snapshot.return_date or "flexible",
            date_flex=date_flex,
            passengers=passengers,
            best_flight_json=best_flight_json,
            lowest_price=snapshot.lowest_price or 0,
            source=snapshot.source,
            price_history_section=price_history_section,
            serpapi_section=serpapi_section,
            traveller_name=traveller_name,
            home_airport=home_airport,
            traveller_preferences_section=traveller_preferences_section,
            preferred_airlines_section=preferred_airlines_section,
            past_decisions_section=past_decisions_section,
            nearby_section=nearby_section,
        )

    def _parse_response(self, raw: str) -> DealScore:
        try:
            data = json.loads(raw)
            return DealScore(
                score=float(data["score"]),
                urgency=data["urgency"],
                reasoning=data["reasoning"],
                booking_window_hours=int(data["booking_window_hours"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            logger.warning("Malformed Claude response, using conservative defaults: %s", exc)
            return DealScore(
                score=0.3,
                urgency="watch",
                reasoning=f"Could not parse Claude response: {raw[:200]}",
                booking_window_hours=48,
            )
