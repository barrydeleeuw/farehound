from __future__ import annotations

import logging

import httpx

from src.analysis.nearby_airports import transport_total
from src.utils.airports import route_name

logger = logging.getLogger(__name__)


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
        origin = deal.get("origin", "")
        dest = deal.get("destination", "")
        outbound = deal.get("outbound_date", "")
        return_date = deal.get("return_date", "")
        passengers = deal.get("passengers", 2)
        # Use Google Flights direct URL format
        url = (
            f"https://www.google.com/travel/flights"
            f"?q=Flights+from+{origin}+to+{dest}"
        )
        if outbound:
            url += f"+on+{outbound}"
        if return_date:
            url += f"+return+{return_date}"
        if passengers and passengers > 1:
            url += f"+{passengers}+passengers"
        return url

    async def send_deal_alert(self, deal_info: dict, chat_id: str | None = None) -> None:
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

        if reasoning:
            lines.append(f"_{reasoning}_")

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
                t_min = alt.get("transport_time_min", 0)
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

        deal_id = deal_info.get("deal_id")
        reply_markup = None
        if deal_id:
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "Book Now ✈️", "url": search_url},
                    {"text": "Wait 🕐", "callback_data": f"wait:{deal_id}"},
                ]]
            }

        await self._send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)

    async def send_error_fare_alert(self, deal_info: dict, chat_id: str | None = None) -> None:
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
        lines.append("⚡ BOOK NOW — these usually disappear fast!")
        if reasoning:
            lines.append(f"_{reasoning}_")
        lines.append(f"[Book Now]({booking_url})")

        deal_id = deal_info.get("deal_id")
        reply_markup = None
        if deal_id:
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "Book Now ✈️", "url": booking_url},
                    {"text": "Wait 🕐", "callback_data": f"wait:{deal_id}"},
                ]]
            }

        await self._send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)

    async def send_follow_up(self, deal_info: dict, chat_id: str | None = None) -> None:
        """Send a follow-up message for deals with no feedback after 3+ days."""
        route = route_name(deal_info.get("origin", "???"), deal_info.get("destination", "???"))
        price = deal_info.get("price", "?")
        deal_id = deal_info.get("deal_id")

        text = (
            f"You saw the {route} deal at €{float(price):,.0f} three days ago. "
            "Did you book it?"
        )
        reply_markup = None
        if deal_id:
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "Yes, booked ✅", "callback_data": f"booked:{deal_id}"},
                    {"text": "Still watching 👀", "callback_data": f"watching:{deal_id}"},
                ]]
            }
        await self._send_message(chat_id, text, reply_markup=reply_markup)

    async def send_daily_digest(self, routes_summary: list[dict], chat_id: str | None = None) -> None:
        if not routes_summary:
            return

        from src.utils.airports import airport_name

        # Send header
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
                    t_min = alt.get("transport_time_min", 0)
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

            search_url = self._google_flights_url({
                "origin": origin, "destination": dest, "passengers": passengers,
                "outbound_date": route_data.get("outbound_date", ""),
                "return_date": route_data.get("return_date", ""),
            })
            deal_ids = route_data.get("deal_ids", [])
            route_id = route_data.get("route_id", "")
            user_id = route_data.get("user_id", "")
            keyboard = [[{"text": "Book Now ✈️", "url": search_url}]]
            if deal_ids and route_id:
                keyboard.append([
                    {"text": "Booked ✅", "callback_data": f"digest_booked:{deal_ids[0]}"},
                    {"text": "Not interested", "callback_data": f"digest_dismiss:{route_id}:{user_id}"},
                ])
            reply_markup = {"inline_keyboard": keyboard}

            await self._send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)
