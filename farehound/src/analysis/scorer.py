from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

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
historically gone lower — the traveller can't wait forever for a unicorn.

HONESTY RULES:
- NEVER claim certainty about future price direction. State facts about where the \
price sits relative to the data provided.
- With fewer than 10 data points, always acknowledge limited data in your reasoning.
- Do NOT use urgency language ("book now", "act fast", "don't wait") unless the \
price is genuinely exceptional: below Google's typical low AND below our observed minimum.

URGENCY FIELD RULES:
- "book_now": ONLY when price < Google's typical low AND sample_count >= 5
- "watch": good price but limited data (< 5 samples), OR price is in the lower \
half of the typical range
- "skip": price is above typical range or above our observed average"""

_SCORE_PROMPT = """\
TODAY: {today}
ROUTE: {origin} → {destination}
TRIP TYPE: {trip_type}
TRAVEL DATES: {outbound_date} to {return_date} (±{date_flex} days flexible)
DEPARTURE: {days_until_departure} days away
PASSENGERS: {passengers}

CURRENT FARE:
{best_flight_json}

PRICE CONTEXT:
- Current lowest: €{lowest_price}/pp
- Source: {source}
{price_history_section}
{serpapi_section}

PRE-COMPUTED ANALYSIS (use these facts — do not contradict them):
{pre_analysis}

DATA CONFIDENCE: {sample_count} observations over {days_observed} days

TRAVELLER PREFERENCES:
- {traveller_name}, based at {home_airport}
- Travelling with {passengers} passenger(s)
{traveller_preferences_section}
{preferred_airlines_section}
{past_decisions_section}
{nearby_section}
Score this deal. Use these past decisions to calibrate your scoring. The traveller's revealed preferences from actual booking behavior matter more than stated preferences when they conflict. The "reasoning" field will be shown as a phone notification, so keep it to 2-3 short sentences that help the traveller decide: mention the price vs. ranges, any connection/timing concerns, and whether the data supports acting or waiting. Be factual. State price vs ranges. Acknowledge uncertainty when data is limited. Do not predict future prices. If a nearby airport offers significant savings, mention the best alternative in the reasoning.

Respond with JSON only:
{{
  "score": 0.0-1.0,
  "urgency": "book_now" | "watch" | "skip",
  "reasoning": {{
    "vs_dates": "Compare price across dates polled. Cite the cheapest date if savings > €20/pp; otherwise note the date is the best available. ≤120 chars.",
    "vs_range": "Compare to Google's typical low-high range OR my 90-day average. Cite the absolute or percentage difference. ≤120 chars.",
    "vs_nearby": "Compare to nearby alternative airports. If none configured, return \\"No nearby airports configured\\". If primary is best, return \\"Yours is best\\". Otherwise cite the cheapest alternative. ≤120 chars."
  }},
  "booking_window_hours": estimated hours this fare is likely available
}}"""

_DEFAULT_TRAVELLER_PREFS = [
    "Prefers connections through quality hubs (DOH, IST, SIN) over regional airports",
    "Values reasonable layover times (1.5-4 hours)",
    "Departure flexibility: weekday evenings or weekends preferred",
    "Airline preferences: open, but values included baggage and legroom",
]


_REASONING_FIELDS = ("vs_dates", "vs_range", "vs_nearby")


def reasoning_to_bullets(reasoning: dict | str | None) -> str:
    """Flatten a 3-field reasoning dict to a `\\n`-joined bullet string for legacy `deals.reasoning` reads.

    Accepts a string for back-compat (returns it unchanged) or None (returns "").
    """
    if reasoning is None:
        return ""
    if isinstance(reasoning, str):
        return reasoning
    if isinstance(reasoning, dict):
        return "\n".join(f"✓ {reasoning[k]}" for k in _REASONING_FIELDS if reasoning.get(k))
    return ""


@dataclass
class DealScore:
    score: float
    urgency: str
    reasoning: dict
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
        passengers = getattr(route, "passengers", snapshot.passengers)

        # Price history section (per-person prices)
        if price_history.get("count", 0) > 0:
            price_history_section = (
                f"- My observed average (last 90 days): €{price_history['avg_price'] / passengers:,.0f}\n"
                f"- My observed minimum (last 90 days): €{price_history['min_price'] / passengers:,.0f}\n"
                f"- My observed maximum (last 90 days): €{price_history['max_price'] / passengers:,.0f}\n"
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
                    f"- Google Flights typical range: €{float(snapshot.typical_low) / passengers:,.0f} - €{float(snapshot.typical_high) / passengers:,.0f}"
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
                t_min = alt.get("transport_time_min") or 0
                hours = t_min / 60
                fare = alt.get("fare_pp", 0)
                net = alt.get("net_cost", 0)
                savings = alt.get("savings", 0)
                flight_dur = alt.get("flight_duration_min")
                primary_dur = alt.get("primary_flight_duration_min")
                dur_str = ""
                if flight_dur:
                    dur_h = flight_dur / 60
                    if primary_dur and flight_dur != primary_dur:
                        diff_h = (flight_dur - primary_dur) / 60
                        sign = "+" if diff_h > 0 else ""
                        dur_str = f", {dur_h:.0f}h flight ({sign}{diff_h:.0f}h vs primary)"
                    else:
                        dur_str = f", {dur_h:.0f}h flight"
                lines.append(
                    f"- {name} ({mode}, €{t_cost:,.0f} return, {hours:.1f}h): "
                    f"€{fare:,.0f}/pp, €{net:,.0f} net → save €{savings:,.0f} vs {origin_name}{dur_str}"
                )
            lines.append("")
            nearby_section = "\n".join(lines)

        # Route fields — support both config Route and db Route
        origin = getattr(route, "origin", "")
        destination = getattr(route, "destination", "")
        trip_type = getattr(route, "trip_type", "round_trip")
        date_flex = getattr(route, "date_flex_days", None) or getattr(route, "date_flexibility_days", 3)

        # Days until departure
        earliest_dep = getattr(route, "earliest_departure", None)
        today = datetime.now(UTC).date()
        if earliest_dep:
            dep_date = earliest_dep if isinstance(earliest_dep, date) else today
            days_until_departure = max(0, (dep_date - today).days)
        else:
            days_until_departure = "unknown"

        # Data confidence
        sample_count = price_history.get("count", 0)
        first_seen = price_history.get("first_seen")
        if first_seen and sample_count > 0:
            if isinstance(first_seen, str):
                first_date = datetime.fromisoformat(first_seen).date()
            elif isinstance(first_seen, datetime):
                first_date = first_seen.date()
            elif isinstance(first_seen, date):
                first_date = first_seen
            else:
                first_date = today
            days_observed = max(1, (today - first_date).days)
        elif sample_count > 0:
            days_observed = "unknown"
        else:
            days_observed = 0

        # Pre-computed analysis — deterministic facts Claude must use
        price_pp = float(snapshot.lowest_price or 0) / passengers
        analysis_lines = []
        if price_history.get("count", 0) > 0:
            avg_pp = float(price_history["avg_price"]) / passengers
            min_pp = float(price_history["min_price"]) / passengers
            diff_from_avg = price_pp - avg_pp
            pct_from_avg = (diff_from_avg / avg_pp * 100) if avg_pp > 0 else 0
            if diff_from_avg < 0:
                analysis_lines.append(f"- Price is €{abs(diff_from_avg):,.0f} BELOW your 90-day average ({abs(pct_from_avg):.0f}% cheaper)")
            else:
                analysis_lines.append(f"- Price is €{diff_from_avg:,.0f} ABOVE your 90-day average ({pct_from_avg:.0f}% more expensive)")
            if price_pp <= min_pp:
                analysis_lines.append("- This is a NEW 90-day LOW")
            else:
                analysis_lines.append(f"- Your 90-day minimum is €{min_pp:,.0f}/pp (current is €{price_pp - min_pp:,.0f} above it)")
        if snapshot.typical_low and snapshot.typical_high:
            typ_low_pp = float(snapshot.typical_low) / passengers
            typ_high_pp = float(snapshot.typical_high) / passengers
            if price_pp <= typ_low_pp:
                analysis_lines.append("- Price is AT or BELOW Google's typical low — this is cheap")
            elif price_pp >= typ_high_pp:
                analysis_lines.append("- Price is AT or ABOVE Google's typical high — this is expensive")
            else:
                position = (price_pp - typ_low_pp) / (typ_high_pp - typ_low_pp) * 100
                analysis_lines.append(f"- Price sits at the {position:.0f}th percentile of Google's typical range")
        pre_analysis = "\n".join(analysis_lines) if analysis_lines else "- Insufficient data for analysis"

        return _SCORE_PROMPT.format(
            today=today.strftime("%Y-%m-%d"),
            origin=origin,
            destination=destination,
            trip_type=trip_type,
            outbound_date=snapshot.outbound_date or "flexible",
            return_date=snapshot.return_date or "flexible",
            date_flex=date_flex,
            passengers=passengers,
            best_flight_json=best_flight_json,
            lowest_price=price_pp,
            source=snapshot.source,
            price_history_section=price_history_section,
            serpapi_section=serpapi_section,
            pre_analysis=pre_analysis,
            days_until_departure=days_until_departure,
            sample_count=sample_count,
            days_observed=days_observed,
            traveller_name=traveller_name,
            home_airport=home_airport,
            traveller_preferences_section=traveller_preferences_section,
            preferred_airlines_section=preferred_airlines_section,
            past_decisions_section=past_decisions_section,
            nearby_section=nearby_section,
        )

    def _parse_response(self, raw: str) -> DealScore:
        try:
            # Strip markdown code fences if present
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()
            data = json.loads(cleaned)
            urgency = data["urgency"]
            if urgency not in ("book_now", "watch", "skip"):
                urgency = "watch"
            reasoning_raw = data.get("reasoning")
            reasoning = self._coerce_reasoning(reasoning_raw)
            return DealScore(
                score=float(data["score"]),
                urgency=urgency,
                reasoning=reasoning,
                booking_window_hours=int(data["booking_window_hours"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            logger.warning("Malformed Claude response, using conservative defaults: %s", exc)
            return DealScore(
                score=0.3,
                urgency="watch",
                reasoning=self._fallback_reasoning(f"Could not parse Claude response: {raw[:200]}"),
                booking_window_hours=48,
            )

    @staticmethod
    def _coerce_reasoning(raw) -> dict:
        """Validate Claude's `reasoning` field and synthesise missing keys (§6.5).

        Accepts the new dict shape OR a legacy free-text string (older response capture
        replays). On any malformed input, returns a synthetic 3-field fallback.
        """
        if isinstance(raw, dict):
            return {
                "vs_dates": str(raw.get("vs_dates") or "Not evaluated this run"),
                "vs_range": str(raw.get("vs_range") or "Not evaluated this run"),
                "vs_nearby": str(raw.get("vs_nearby") or "No nearby airports configured"),
            }
        if isinstance(raw, str) and raw.strip():
            return {
                "vs_dates": raw[:120],
                "vs_range": "Static fallback — Claude returned legacy string",
                "vs_nearby": "Not evaluated this run",
            }
        return DealScorer._fallback_reasoning("No reasoning returned")

    @staticmethod
    def _fallback_reasoning(detail: str) -> dict:
        return {
            "vs_dates": detail[:120],
            "vs_range": "Static fallback — Claude unavailable",
            "vs_nearby": "Not evaluated this run",
        }
