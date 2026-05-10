from __future__ import annotations

import logging
import os

import httpx

from src.analysis.nearby_airports import transport_total
from src.utils.airports import route_name

logger = logging.getLogger(__name__)


def _miniapp_url(path: str = "") -> str | None:
    """Return the absolute Mini Web App URL for `path`, or None if MINIAPP_URL is unset."""
    base = os.environ.get("MINIAPP_URL", "").strip().rstrip("/")
    if not base:
        return None
    if not path:
        return base
    return f"{base}/{path.lstrip('/')}"


def _miniapp_enabled() -> bool:
    return _miniapp_url() is not None


def find_cheapest_date(
    price_history: list | None,
    earliest_dep: str,
    latest_ret: str,
    current_price: float,
    passengers: int,
) -> str | None:
    if not price_history:
        return None
    try:
        from datetime import date as date_type
        earliest = date_type.fromisoformat(earliest_dep) if earliest_dep else None
        latest = date_type.fromisoformat(latest_ret) if latest_ret else None
    except (ValueError, TypeError):
        return None
    if not earliest or not latest:
        return None

    cheapest_date = None
    cheapest_price = None
    for entry in price_history:
        try:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                d_str, price = entry[0], entry[1]
            elif isinstance(entry, dict):
                d_str = entry.get("date", "")
                price = entry.get("price")
            else:
                continue
            if price is None:
                continue
            price = float(price)
            d = date_type.fromisoformat(str(d_str)[:10])
            if d < earliest or d > latest:
                continue
            if cheapest_price is None or price < cheapest_price:
                cheapest_price = price
                cheapest_date = d
        except (ValueError, TypeError, IndexError):
            continue

    if cheapest_date is None or cheapest_price is None:
        return None

    current_pp = current_price / passengers if passengers > 1 else current_price
    cheapest_pp = cheapest_price / passengers if passengers > 1 else cheapest_price
    saving_pp = current_pp - cheapest_pp
    if saving_pp > 20:
        return f"💡 {cheapest_date.strftime('%b %d')} is €{saving_pp:,.0f}/pp cheaper for this route"
    return None

TELEGRAM_API = "https://api.telegram.org"

_REASONING_KEYS = ("vs_dates", "vs_range", "vs_nearby")


def _render_reasoning_bullets(reasoning_json: dict | None, reasoning_legacy: str | None) -> list[str]:
    """Render structured `reasoning_json` (3 fields) as 3 bullet lines.

    Falls back to the legacy single-line string when only `reasoning_legacy` is present —
    older deal records (pre-T12) still flow through this path.
    """
    if isinstance(reasoning_json, dict):
        return [f"✓ {reasoning_json[k]}" for k in _REASONING_KEYS if reasoning_json.get(k)]
    if reasoning_legacy:
        # Legacy already-flattened bullet-string (newline-separated), or free text.
        text = str(reasoning_legacy).strip()
        if "\n" in text:
            return [line for line in text.splitlines() if line.strip()]
        return [f"_{text}_"]
    return []


def _render_transparency_footer(competitive: list, evaluated: list) -> str | None:
    """Footer text per §9.2 — informs user which airports were checked.

    Returns None when no footer is needed (i.e. competitive non-empty with nothing else evaluated,
    or both lists empty).
    """
    competitive = competitive or []
    evaluated = evaluated or []
    competitive_codes = {a.get("airport_code") for a in competitive}
    non_competitive = [a for a in evaluated if a.get("airport_code") not in competitive_codes]

    if not competitive and not evaluated:
        return None
    if competitive and not non_competitive:
        return None  # Existing nearby block is sufficient.
    if not competitive and evaluated:
        deltas = [a.get("delta_vs_primary", 0) for a in evaluated if a.get("delta_vs_primary") is not None]
        if not deltas:
            return f"✓ Checked {len(evaluated)} airport{'s' if len(evaluated) != 1 else ''} — your airport is best"
        min_delta = min(deltas)
        max_delta = max(deltas)
        if min_delta == max_delta:
            return (
                f"✓ Checked {len(evaluated)} airport{'s' if len(evaluated) != 1 else ''} — "
                f"your airport is best by €{min_delta:,.0f}"
            )
        return (
            f"✓ Checked {len(evaluated)} airport{'s' if len(evaluated) != 1 else ''} — "
            f"your airport is best by €{min_delta:,.0f}–€{max_delta:,.0f}"
        )
    # Mixed: some competitive, some not.
    names = ", ".join(a.get("airport_name") or a.get("airport_code", "?") for a in non_competitive)
    deltas = [a.get("delta_vs_primary", 0) for a in non_competitive if a.get("delta_vs_primary") is not None]
    if deltas:
        return f"…also checked {names} (€{min(deltas):,.0f}+ more, skipped)"
    return f"…also checked {names}"


def _render_date_transparency(price_history) -> str | None:
    """One-liner showing date polling per §9.3: 'Polled N dates — Mar 12 is cheapest'."""
    if not price_history:
        return None
    from datetime import date as date_type
    dates_seen: list = []
    cheapest_date = None
    cheapest_price = None
    for entry in price_history:
        try:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                d_str, price = entry[0], entry[1]
            elif isinstance(entry, dict):
                d_str, price = entry.get("date", ""), entry.get("price")
            else:
                continue
            if price is None:
                continue
            d = date_type.fromisoformat(str(d_str)[:10])
            dates_seen.append(d)
            price = float(price)
            if cheapest_price is None or price < cheapest_price:
                cheapest_price = price
                cheapest_date = d
        except (ValueError, TypeError, IndexError):
            continue
    if not dates_seen or cheapest_date is None:
        return None
    return f"✓ Polled {len(dates_seen)} dates — {cheapest_date.strftime('%b %d')} is cheapest"


def _deal_emoji(score: float | None, urgency: str | None = None) -> str:
    """Return an emoji indicating deal quality."""
    if score is None:
        return "✈️"
    if score >= 0.9:
        return "🔥"  # Exceptional
    if score >= 0.75:
        return "💰"  # Good deal
    if score >= 0.50:
        return "👀"  # Worth watching
    return "😴"  # Skip


def _deal_label(score: float | None, urgency: str | None = None) -> str:
    """Return a human-readable deal quality label."""
    if score is None:
        return "Deal"
    if score >= 0.9:
        return "Exceptional Deal"
    if score >= 0.75:
        return "Good Deal"
    if score >= 0.50:
        return "Worth Watching"
    return "Not Great"


def _baggage_total(baggage: dict | None, passengers: int) -> float:
    """Sum baggage cost across both directions and passengers. Zero when missing or 'unknown'."""
    if not baggage or not isinstance(baggage, dict):
        return 0.0
    if baggage.get("source") == "unknown":
        return 0.0
    total = 0.0
    for direction in ("outbound", "return"):
        leg = baggage.get(direction) or {}
        try:
            total += float(leg.get("carry_on", 0) or 0)
            total += float(leg.get("checked", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total * max(int(passengers or 1), 1)


def _format_cost_breakdown(
    price: float,
    transport: float,
    parking: float,
    mode: str,
    baggage: dict | None,
    passengers: int,
) -> tuple[str, float]:
    """Return (display_string, total_eur) — single line, e.g. '€500 flights + €40 train + €30 bags = *€570 total*'.

    Suppresses zero-cost segments. Baggage line omitted when source == 'unknown' or total == 0.
    """
    transport_t = transport_total(transport, mode, passengers)
    bags = _baggage_total(baggage, passengers)
    total = float(price) + transport_t + (parking or 0) + bags
    parts = [f"€{float(price):,.0f} flights"]
    if transport_t:
        parts.append(f"€{transport_t:,.0f} {mode.lower()}")
    if parking:
        parts.append(f"€{parking:,.0f} parking")
    if bags > 0:
        parts.append(f"€{bags:,.0f} bags")
    return f"{' + '.join(parts)} = *€{total:,.0f} total*", total


def _format_flight_line(deal_info: dict) -> str:
    """Format a single line with airline, stops, and duration."""
    airline = deal_info.get("airline", "")
    stops = deal_info.get("stops")
    duration_min = deal_info.get("flight_duration_min")

    parts = []
    if airline:
        parts.append(airline)
    if stops is not None:
        parts.append("Direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}")
    if duration_min:
        hours = duration_min // 60
        mins = duration_min % 60
        parts.append(f"{hours}h{mins:02d}m")

    return " · ".join(parts) if parts else ""


class TelegramNotifier:
    """Send flight deal alerts via Telegram Bot API."""

    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token

    async def _send_message(
        self, chat_id: str, text: str, parse_mode: str = "Markdown", reply_markup: dict | None = None
    ) -> None:
        url = f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                logger.info("Telegram message sent to chat %s", chat_id)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Telegram API error %s: %s", exc.response.status_code, exc.response.text
            )
        except httpx.ConnectError:
            logger.error("Cannot reach Telegram API")
        except httpx.TimeoutException:
            logger.error("Timeout calling Telegram API")
        except Exception:
            logger.exception("Unexpected error sending Telegram message")

    def _google_flights_url(self, deal: dict) -> str:
        """v0.11.6: delegates to the canonical `#flt=` deep-link builder.
        The old `?q=Flights+from+X+to+Y` query-string form often dumped users
        on flights.google.com instead of a populated search."""
        from src.apis.serpapi import build_google_flights_url
        origin = deal.get("origin", "")
        dest = deal.get("destination", "")
        outbound = deal.get("outbound_date", "")
        if not origin or not dest or not outbound:
            return f"https://www.google.com/travel/flights?q=Flights+from+{origin}+to+{dest}"
        return build_google_flights_url(
            origin=origin,
            destination=dest,
            outbound_date=outbound,
            return_date=deal.get("return_date") or None,
            passengers=deal.get("passengers", 2),
        )

    async def send_deal_alert(self, deal_info: dict, chat_id: str | None = None) -> None:
        if _miniapp_enabled():
            return await self._send_deal_alert_thin(deal_info, chat_id)

        origin = deal_info.get("origin", "???")
        dest = deal_info.get("destination", "???")
        price = deal_info.get("price", "?")
        score = deal_info.get("score")
        urgency = deal_info.get("urgency")
        reasoning = deal_info.get("reasoning", "")
        airline = deal_info.get("airline", "Unknown")
        dates = deal_info.get("dates", "")
        search_url = deal_info.get("google_flights_url") or self._google_flights_url(deal_info)

        passengers = deal_info.get("passengers", 2)
        price_pp = float(price) / passengers if passengers > 1 else float(price)

        emoji = _deal_emoji(score, urgency)
        label = _deal_label(score, urgency)
        route = route_name(origin, dest)
        lines = [
            f"{emoji} *{label}* — {route}",
        ]
        # Flight info
        flight_line = _format_flight_line(deal_info)
        if flight_line:
            lines.append(f"✈️ {flight_line}")
        if dates:
            lines.append(f"📅 {dates}")

        # Pricing
        lines.append(f"💰 *€{price_pp:,.0f}/pp*")

        # Always show full cost breakdown for primary airport
        breakdown_line, _ = _format_cost_breakdown(
            float(price),
            deal_info.get("primary_transport_cost", 0) or 0,
            deal_info.get("primary_parking_cost", 0) or 0,
            deal_info.get("primary_transport_mode", "transport"),
            deal_info.get("baggage_estimate"),
            passengers,
        )
        lines.append(breakdown_line)

        # Price context
        price_level = deal_info.get("price_level", "")
        typical_low = deal_info.get("typical_low")
        typical_high = deal_info.get("typical_high")
        if price_level:
            level_icon = {"low": "📉", "typical": "➡️", "high": "📈"}.get(price_level, "")
            context = f"{level_icon} Price level: *{price_level}*"
            if typical_low is not None and typical_high is not None:
                context += f" (€{float(typical_low):,.0f}–€{float(typical_high):,.0f})"
            lines.append(context)

        cheapest_hint = find_cheapest_date(
            deal_info.get("price_history"),
            deal_info.get("earliest_departure", ""),
            deal_info.get("latest_return", ""),
            float(price),
            passengers,
        )
        if cheapest_hint:
            lines.append(cheapest_hint)

        # T7 §6.3: render structured 3-bullet reasoning if present, else fall back to legacy.
        reasoning_bullets = _render_reasoning_bullets(
            deal_info.get("reasoning_json"), reasoning,
        )
        if reasoning_bullets:
            lines.extend(reasoning_bullets)

        # T7 §9.3: date transparency (sits below cost / above nearby).
        date_line = _render_date_transparency(deal_info.get("price_history"))
        if date_line:
            lines.append(date_line)

        nearby = deal_info.get("nearby_comparison") or []
        if nearby:
            lines.append("")
            for i, alt in enumerate(nearby):
                icon = "🟢" if i == 0 else "🟡"
                name = alt.get("airport_name") or alt.get("airport_code", "?")
                fare = alt.get("fare_pp", 0)
                net = alt.get("net_cost", 0)
                savings = alt.get("savings", 0)
                mode = alt.get("transport_mode", "transport")
                t_cost = alt.get("transport_cost", 0)
                parking = alt.get("parking_cost") or 0
                t_min = alt.get("transport_time_min") or 0
                hours = t_min / 60
                alt_passengers = deal_info.get("passengers", 2)
                fare_total = fare * alt_passengers
                alt_breakdown, _ = _format_cost_breakdown(
                    fare_total, t_cost, parking, mode, alt.get("baggage_estimate"), alt_passengers,
                )
                duration_str = ""
                flight_dur = alt.get("flight_duration_min")
                primary_dur = alt.get("primary_flight_duration_min")
                if flight_dur:
                    dur_h = flight_dur / 60
                    if primary_dur and flight_dur != primary_dur:
                        diff_h = (flight_dur - primary_dur) / 60
                        sign = "+" if diff_h > 0 else ""
                        duration_str = f" | {dur_h:.0f}h flight ({sign}{diff_h:.0f}h)"
                    else:
                        duration_str = f" | {dur_h:.0f}h flight"

                lines.append(
                    f"{icon} *{name}*: €{fare:,.0f}/pp (save €{savings:,.0f}){duration_str}"
                )
                lines.append(f"    {alt_breakdown}")
                lines.append(
                    f"    {mode} {hours:.1f}h to airport"
                )

        # T7 §9.2: "we checked X" transparency footer.
        evaluated = deal_info.get("nearby_evaluated") or []
        footer = _render_transparency_footer(nearby, evaluated)
        if footer:
            lines.append(footer)

        deal_id = deal_info.get("deal_id")
        route_id = deal_info.get("route_id")
        reply_markup = self._build_deal_keyboard(deal_id, route_id, search_url, deal_info)

        await self._send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)

    def _build_deal_keyboard(
        self, deal_id: str | None, route_id: str | None, search_url: str, deal_info: dict,
    ) -> dict | None:
        """Three-button row + Details row per T7 / T8.

        Row 1: Book Now (URL) / Watching 👀 (deal:watch) / Skip route 🔕 (route:snooze:7).
        Row 2: 📊 Details (URL — Google Flights deep link, sub-item 7 placeholder per Condition C10).

        Falls back to two-button row if route_id is missing (older payloads).
        """
        if not deal_id:
            return None
        keyboard: list[list[dict]] = []
        primary_row: list[dict] = [{"text": "Book Now ✈️", "url": search_url}]
        primary_row.append({"text": "Watching 👀", "callback_data": f"deal:watch:{deal_id}"})
        if route_id:
            primary_row.append(
                {"text": "Skip route 🔕", "callback_data": f"route:snooze:7:{route_id}"}
            )
        keyboard.append(primary_row)
        # Row 2: Details (placeholder Google Flights deep link).
        details_url = self._google_flights_url(deal_info)
        keyboard.append([{"text": "📊 Details", "url": details_url}])
        return {"inline_keyboard": keyboard}

    async def send_error_fare_alert(self, deal_info: dict, chat_id: str | None = None) -> None:
        if _miniapp_enabled():
            return await self._send_error_fare_alert_thin(deal_info, chat_id)

        origin = deal_info.get("origin", "???")
        dest = deal_info.get("destination", "???")
        price = deal_info.get("price", "?")
        score = deal_info.get("score")
        reasoning = deal_info.get("reasoning", "")
        airline = deal_info.get("airline", "Unknown")
        dates = deal_info.get("dates", "")
        booking_url = (
            deal_info.get("booking_url")
            or deal_info.get("google_flights_url")
            or self._google_flights_url(deal_info)
        )

        route = route_name(origin, dest)
        lines = [
            f"🔥 *Error Fare* — {route}",
        ]
        flight_line = _format_flight_line(deal_info)
        if flight_line:
            lines.append(f"✈️ {flight_line}")
        if dates:
            lines.append(f"📅 {dates}")
        lines.append(f"💰 *€{float(price):,.0f}*")
        # Show full cost breakdown (with baggage if present) for error fares too.
        passengers = deal_info.get("passengers", 2)
        ef_breakdown, _ = _format_cost_breakdown(
            float(price),
            deal_info.get("primary_transport_cost", 0) or 0,
            deal_info.get("primary_parking_cost", 0) or 0,
            deal_info.get("primary_transport_mode", "transport"),
            deal_info.get("baggage_estimate"),
            passengers,
        )
        lines.append(ef_breakdown)
        lines.append("⚡ BOOK NOW — these usually disappear fast!")
        # Structured reasoning bullets (T7).
        reasoning_bullets = _render_reasoning_bullets(
            deal_info.get("reasoning_json"), reasoning,
        )
        if reasoning_bullets:
            lines.extend(reasoning_bullets)
        lines.append(f"[Book Now]({booking_url})")

        deal_id = deal_info.get("deal_id")
        route_id = deal_info.get("route_id")
        reply_markup = self._build_deal_keyboard(deal_id, route_id, booking_url, deal_info)

        await self._send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)

    async def send_follow_up(self, deal_info: dict, chat_id: str | None = None) -> None:
        """Send a follow-up message for deals with no feedback after 3+ days."""
        if _miniapp_enabled():
            return await self._send_follow_up_thin(deal_info, chat_id)

        route = route_name(deal_info.get("origin", "???"), deal_info.get("destination", "???"))
        price = deal_info.get("price", "?")
        deal_id = deal_info.get("deal_id")
        passengers = deal_info.get("passengers", 2)

        lines = [
            f"You saw the {route} deal at €{float(price):,.0f} three days ago. "
            "Did you book it?",
        ]
        # Re-show cost breakdown so the user sees the full picture in the follow-up too.
        breakdown, _ = _format_cost_breakdown(
            float(price),
            deal_info.get("primary_transport_cost", 0) or 0,
            deal_info.get("primary_parking_cost", 0) or 0,
            deal_info.get("primary_transport_mode", "transport"),
            deal_info.get("baggage_estimate"),
            passengers,
        )
        lines.append(breakdown)
        reply_markup = None
        if deal_id:
            # Use new prefixes; legacy aliases are still routed by the dispatcher (Condition C2).
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "Yes, booked ✅", "callback_data": f"deal:book:{deal_id}"},
                    {"text": "Still watching 👀", "callback_data": f"deal:watch:{deal_id}"},
                ]]
            }
        await self._send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)

    async def send_daily_digest(self, routes_summary: list[dict], chat_id: str | None = None) -> None:
        if not routes_summary:
            return

        if _miniapp_enabled():
            return await self._send_daily_digest_thin(routes_summary, chat_id)

        from src.utils.airports import airport_name

        # Concrete header (§11.4) when orchestrator computed deltas; otherwise fallback to generic.
        override = routes_summary[0].get("digest_header_override")
        if override:
            await self._send_message(chat_id, override)
        else:
            await self._send_message(
                chat_id,
                f"📊 *FareHound Daily* — {len(routes_summary)} route(s)\n"
                "You haven't decided on these yet:",
            )

        # Send one message per route with full details and Search button
        for route_data in routes_summary:
            origin = route_data.get("origin", "?")
            dest = route_data.get("destination", "?")
            lowest = route_data.get("lowest_price")
            trend = route_data.get("trend", "")
            trend_icon = {"down": "📉", "up": "📈", "stable": "➡️"}.get(trend, "")
            passengers = route_data.get("passengers", 2)
            dates = route_data.get("dates", "")
            score = route_data.get("deal_score")
            emoji = _deal_emoji(score)

            lines = [
                f"{emoji} *{route_name(origin, dest)}* {trend_icon}",
            ]
            flight_line = _format_flight_line(route_data)
            if flight_line:
                lines.append(f"✈️ {flight_line}")
            if dates:
                lines.append(f"📅 {dates}")
            if lowest is not None:
                price_pp = float(lowest) / passengers if passengers > 1 else float(lowest)
                lines.append(f"💰 *€{price_pp:,.0f}/pp*")
                # Always show full cost breakdown
                d_breakdown, _ = _format_cost_breakdown(
                    float(lowest),
                    route_data.get("primary_transport_cost", 0) or 0,
                    route_data.get("primary_parking_cost", 0) or 0,
                    route_data.get("primary_transport_mode", "transport"),
                    route_data.get("baggage_estimate"),
                    passengers,
                )
                lines.append(d_breakdown)
                # Show price change since alert
                alert_price = route_data.get("alert_price")
                if alert_price is not None:
                    diff = float(lowest) - float(alert_price)
                    if abs(diff) >= 1:
                        direction = "📉" if diff < 0 else "📈"
                        lines.append(f"{direction} {'Dropped' if diff < 0 else 'Rose'} €{abs(diff):,.0f} since alert")
                cheapest_hint = find_cheapest_date(
                    route_data.get("price_history"),
                    route_data.get("earliest_departure", ""),
                    route_data.get("latest_return", ""),
                    float(lowest),
                    passengers,
                )
                if cheapest_hint:
                    lines.append(cheapest_hint)
            else:
                lines.append("⏳ No price data yet")

            nearby_prices = route_data.get("nearby_prices") or []
            if nearby_prices:
                lines.append("")
                lines.append(f"*Nearby alternatives:*")
                origin_name = airport_name(origin)
                for i, alt in enumerate(nearby_prices):
                    icon = "🟢" if i == 0 else "🟡"
                    name = alt.get("airport_name") or alt.get("airport_code", "?")
                    fare = alt.get("fare_pp", 0)
                    net = alt.get("net_cost", 0)
                    savings = alt.get("savings", 0)
                    mode = alt.get("transport_mode", "transport")
                    t_cost = alt.get("transport_cost", 0)
                    parking = alt.get("parking_cost") or 0
                    t_min = alt.get("transport_time_min") or 0  # NULL-safe
                    hours = t_min / 60
                    alt_pax = route_data.get("passengers", 2)
                    alt_fare_total = fare * alt_pax
                    alt_breakdown, _ = _format_cost_breakdown(
                        alt_fare_total, t_cost, parking, mode, alt.get("baggage_estimate"), alt_pax,
                    )
                    duration_str = ""
                    flight_dur = alt.get("flight_duration_min")
                    primary_dur = alt.get("primary_flight_duration_min")
                    if flight_dur:
                        dur_h = flight_dur / 60
                        if primary_dur and flight_dur != primary_dur:
                            diff_h = (flight_dur - primary_dur) / 60
                            sign = "+" if diff_h > 0 else ""
                            duration_str = f" | {dur_h:.0f}h flight ({sign}{diff_h:.0f}h)"
                        else:
                            duration_str = f" | {dur_h:.0f}h flight"

                    lines.append(
                        f"{icon} *{name}*: €{fare:,.0f}/pp (save €{savings:,.0f}){duration_str}"
                    )
                    lines.append(f"    {alt_breakdown}")
                    lines.append(
                        f"    {mode} {hours:.1f}h to airport"
                    )

            # T7 §9.2: transparency footer for digest too.
            evaluated_d = route_data.get("nearby_evaluated") or []
            footer_d = _render_transparency_footer(nearby_prices, evaluated_d)
            if footer_d:
                lines.append(footer_d)
            # T7 §9.3: date transparency.
            date_line_d = _render_date_transparency(route_data.get("price_history"))
            if date_line_d:
                lines.append(date_line_d)

            search_url = self._google_flights_url({
                "origin": origin, "destination": dest, "passengers": passengers,
                "outbound_date": route_data.get("outbound_date", ""),
                "return_date": route_data.get("return_date", ""),
            })
            deal_ids = route_data.get("deal_ids", [])
            route_id = route_data.get("route_id", "")
            user_id = route_data.get("user_id", "")
            # New 3-button row + Details row (T7/T8). When no deal_ids/route_id (early state)
            # fall back to a single Book button.
            keyboard: list[list[dict]] = []
            primary_row: list[dict] = [{"text": "Book Now ✈️", "url": search_url}]
            if deal_ids:
                primary_row.append({"text": "Watching 👀", "callback_data": f"deal:watch:{deal_ids[0]}"})
            if route_id:
                primary_row.append(
                    {"text": "Skip route 🔕", "callback_data": f"route:snooze:7:{route_id}"}
                )
            keyboard.append(primary_row)
            keyboard.append([{"text": "📊 Details", "url": search_url}])
            reply_markup = {"inline_keyboard": keyboard}

            await self._send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)

    # ===========================================================================
    # Thin-Telegram variants — used when MINIAPP_URL is configured.
    # Web app is the primary surface; Telegram is a notification ping with a
    # single 📊 Open button + inline binary actions (Watching, Skip, Booked).
    # ===========================================================================

    def _miniapp_open_button(self, path: str = "") -> dict:
        """Telegram WebApp button — launches the Mini Web App inside Telegram."""
        url = _miniapp_url(path) or ""
        return {"text": "📊 Open in FareHound", "web_app": {"url": url}}

    async def _send_deal_alert_thin(self, deal_info: dict, chat_id: str | None = None) -> None:
        origin = deal_info.get("origin", "???")
        dest = deal_info.get("destination", "???")
        price = deal_info.get("price", "?")
        passengers = deal_info.get("passengers", 2)
        score = deal_info.get("score")
        deal_id = deal_info.get("deal_id")
        route_id = deal_info.get("route_id")

        try:
            price_pp = float(price) / passengers if passengers and passengers > 1 else float(price)
            price_label = f"€{price_pp:,.0f}/pp"
        except (TypeError, ValueError):
            price_label = f"€{price}"

        emoji = _deal_emoji(score)
        route = route_name(origin, dest)

        lines = [f"{emoji} *{route}* — {price_label}"]
        # Optional one-line context (delta vs alert price, or "new low")
        delta = deal_info.get("delta_since_alert")
        if delta is not None:
            try:
                delta_f = float(delta)
                if delta_f < 0:
                    lines.append(f"▼ €{abs(delta_f):,.0f} since alert")
                elif delta_f > 0:
                    lines.append(f"▲ €{delta_f:,.0f} since alert")
            except (TypeError, ValueError):
                pass
        lines.append("Tap to open.")

        keyboard: list[list[dict]] = []
        primary_row: list[dict] = [self._miniapp_open_button(f"deal/{deal_id}" if deal_id else "")]
        if deal_id:
            primary_row.append({"text": "Watching 👀", "callback_data": f"deal:watch:{deal_id}"})
        if route_id:
            primary_row.append({"text": "Skip route 🔕", "callback_data": f"route:snooze:7:{route_id}"})
        keyboard.append(primary_row)
        reply_markup = {"inline_keyboard": keyboard}

        await self._send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)

    async def _send_error_fare_alert_thin(self, deal_info: dict, chat_id: str | None = None) -> None:
        origin = deal_info.get("origin", "???")
        dest = deal_info.get("destination", "???")
        price = deal_info.get("price", "?")
        deal_id = deal_info.get("deal_id")
        route_id = deal_info.get("route_id")

        try:
            price_label = f"€{float(price):,.0f}"
        except (TypeError, ValueError):
            price_label = f"€{price}"

        route = route_name(origin, dest)
        lines = [
            f"🔥 *Error fare* — {route} — {price_label}",
            "Book fast — these usually disappear.",
        ]

        keyboard: list[list[dict]] = []
        primary_row: list[dict] = [self._miniapp_open_button(f"deal/{deal_id}" if deal_id else "")]
        if deal_id:
            primary_row.append({"text": "Watching 👀", "callback_data": f"deal:watch:{deal_id}"})
        if route_id:
            primary_row.append({"text": "Skip route 🔕", "callback_data": f"route:snooze:7:{route_id}"})
        keyboard.append(primary_row)
        reply_markup = {"inline_keyboard": keyboard}

        await self._send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)

    async def _send_follow_up_thin(self, deal_info: dict, chat_id: str | None = None) -> None:
        origin = deal_info.get("origin", "???")
        dest = deal_info.get("destination", "???")
        price = deal_info.get("price", "?")
        deal_id = deal_info.get("deal_id")

        try:
            price_label = f"€{float(price):,.0f}"
        except (TypeError, ValueError):
            price_label = f"€{price}"

        route = route_name(origin, dest)
        text = f"You saw the {route} deal at {price_label} three days ago. Did you book it?"

        keyboard: list[list[dict]] = []
        # Inline binary actions stay — they're one-tap and more convenient than opening the web app.
        if deal_id:
            keyboard.append([
                {"text": "Yes, booked ✅", "callback_data": f"deal:book:{deal_id}"},
                {"text": "Still watching 👀", "callback_data": f"deal:watch:{deal_id}"},
            ])
            keyboard.append([self._miniapp_open_button(f"deal/{deal_id}")])
        reply_markup = {"inline_keyboard": keyboard} if keyboard else None

        await self._send_message(chat_id, text, reply_markup=reply_markup)

    async def _send_daily_digest_thin(
        self, routes_summary: list[dict], chat_id: str | None = None
    ) -> None:
        n_routes = len(routes_summary)
        moved = sum(
            1 for r in routes_summary
            if r.get("alert_price") is not None
            and r.get("lowest_price") is not None
            and abs(float(r.get("lowest_price") or 0) - float(r.get("alert_price") or 0)) >= 10
        )

        if moved == 0:
            text = f"📊 *FareHound Daily* — {n_routes} route{'s' if n_routes != 1 else ''}. Tap to open."
        else:
            text = (
                f"📊 *FareHound Daily* — {n_routes} route{'s' if n_routes != 1 else ''}, "
                f"{moved} price{'s' if moved != 1 else ''} moved. Tap to open."
            )

        keyboard = {"inline_keyboard": [[self._miniapp_open_button("routes")]]}
        await self._send_message(chat_id, text, reply_markup=keyboard)
