from __future__ import annotations

import logging

import httpx

from src.analysis.nearby_airports import transport_total
from src.utils.airports import route_name

logger = logging.getLogger(__name__)

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
        primary_t = deal_info.get("primary_transport_cost", 0)
        primary_p = deal_info.get("primary_parking_cost", 0)
        primary_mode = deal_info.get("primary_transport_mode", "transport")
        primary_t_total = transport_total(primary_t, primary_mode, passengers)
        primary_total = float(price) + primary_t_total + primary_p
        cost_parts = [f"€{float(price):,.0f} flights"]
        if primary_t_total:
            cost_parts.append(f"€{primary_t_total:,.0f} {primary_mode.lower()}")
        if primary_p:
            cost_parts.append(f"€{primary_p:,.0f} parking")
        lines.append(f"{' + '.join(cost_parts)} = *€{primary_total:,.0f} total*")

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
                t_total = transport_total(t_cost, mode, alt_passengers)
                alt_parts = [f"€{fare_total:,.0f} flights"]
                if t_total:
                    alt_parts.append(f"€{t_total:,.0f} {mode.lower()}")
                if parking:
                    alt_parts.append(f"€{parking:,.0f} parking")
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
                lines.append(
                    f"    {' + '.join(alt_parts)} = *€{net:,.0f} total*"
                )
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
                d_transport = route_data.get("primary_transport_cost", 0)
                d_parking = route_data.get("primary_parking_cost", 0)
                d_mode = route_data.get("primary_transport_mode", "transport")
                d_t_total = transport_total(d_transport, d_mode, passengers)
                d_total = float(lowest) + d_t_total + d_parking
                d_parts = [f"€{float(lowest):,.0f} flights"]
                if d_t_total:
                    d_parts.append(f"€{d_t_total:,.0f} {d_mode.lower()}")
                if d_parking:
                    d_parts.append(f"€{d_parking:,.0f} parking")
                lines.append(f"{' + '.join(d_parts)} = *€{d_total:,.0f} total*")
                # Show price change since alert
                alert_price = route_data.get("alert_price")
                if alert_price is not None:
                    diff = float(lowest) - float(alert_price)
                    if abs(diff) >= 1:
                        direction = "📉" if diff < 0 else "📈"
                        lines.append(f"{direction} {'Dropped' if diff < 0 else 'Rose'} €{abs(diff):,.0f} since alert")
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
                    alt_pax_count = route_data.get("passengers", 2)
                    t_total_alt = transport_total(t_cost, mode, alt_pax_count)
                    alt_parts = [f"€{alt_fare_total:,.0f} flights"]
                    if t_total_alt:
                        alt_parts.append(f"€{t_total_alt:,.0f} {mode.lower()}")
                    if parking:
                        alt_parts.append(f"€{parking:,.0f} parking")
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
                    lines.append(
                        f"    {' + '.join(alt_parts)} = *€{net:,.0f} total*"
                    )
                    lines.append(
                        f"    {mode} {hours:.1f}h to airport"
                    )

            search_url = self._google_flights_url({
                "origin": origin, "destination": dest, "passengers": passengers,
                "outbound_date": route_data.get("outbound_date", ""),
                "return_date": route_data.get("return_date", ""),
            })
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "Book Now ✈️", "url": search_url},
                ]]
            }

            await self._send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)
