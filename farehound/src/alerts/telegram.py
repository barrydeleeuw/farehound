from __future__ import annotations

import logging

import httpx

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


class TelegramNotifier:
    """Send flight deal alerts via Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id

    async def _send_message(
        self, text: str, parse_mode: str = "Markdown", reply_markup: dict | None = None
    ) -> None:
        url = f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
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
                logger.info("Telegram message sent to chat %s", self._chat_id)
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

    async def send_deal_alert(self, deal_info: dict) -> None:
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
            f"*€{price_pp:,.0f}/pp* | {airline} | {dates}",
        ]
        # Show primary airport total cost
        primary_t = deal_info.get("primary_transport_cost", 0)
        primary_p = deal_info.get("primary_parking_cost", 0)
        primary_mode = deal_info.get("primary_transport_mode", "")
        primary_total = float(price) + primary_t + primary_p
        if primary_t or primary_p:
            cost_parts = [f"€{float(price):,.0f} flights"]
            if primary_t:
                cost_parts.append(f"€{primary_t:,.0f} {primary_mode.lower()}")
            if primary_p:
                cost_parts.append(f"€{primary_p:,.0f} parking")
            lines.append(f"{' + '.join(cost_parts)} = *€{primary_total:,.0f} total*")
        elif passengers > 1:
            lines.append(f"Total: €{float(price):,.0f} for {passengers} passengers")
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
                passengers = deal_info.get("passengers", 2) if hasattr(deal_info, 'get') else 2
                fare_total = fare * passengers
                transport_parts = [f"€{t_cost:,.0f} transport"]
                if parking:
                    transport_parts.append(f"€{parking:,.0f} parking")
                transport_total = t_cost + parking
                lines.append(
                    f"{icon} *{name}*: €{fare:,.0f}/pp (save €{savings:,.0f})"
                )
                lines.append(
                    f"    Flights: €{fare_total:,.0f} + {' + '.join(transport_parts)} = *€{net:,.0f} total*"
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

        await self._send_message("\n".join(lines), reply_markup=reply_markup)

    async def send_error_fare_alert(self, deal_info: dict) -> None:
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
            f"*€{float(price):,.0f}* | {airline} | {dates}",
            "BOOK NOW — these usually disappear fast!",
        ]
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

        await self._send_message("\n".join(lines), reply_markup=reply_markup)

    async def send_follow_up(self, deal_info: dict) -> None:
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
        await self._send_message(text, reply_markup=reply_markup)

    async def send_daily_digest(self, routes_summary: list[dict]) -> None:
        if not routes_summary:
            return

        from src.utils.airports import airport_name

        # Send header
        await self._send_message(
            f"📊 *FareHound Daily* — {len(routes_summary)} route(s)"
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
            if dates:
                lines.append(f"📅 {dates}")
            if lowest is not None:
                price_pp = float(lowest) / passengers if passengers > 1 else float(lowest)
                lines.append(f"💰 *€{price_pp:,.0f}/pp* (€{float(lowest):,.0f} total for {passengers} pax)")
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
                    lines.append(
                        f"{icon} {name}: €{fare:,.0f}/pp → €{net:,.0f} net (save €{savings:,.0f})"
                    )
                    cost_parts = [f"€{t_cost:,.0f} fuel/transport"]
                    if parking:
                        cost_parts.append(f"€{parking:,.0f} parking")
                    lines.append(
                        f"    {mode} {hours:.1f}h | {' + '.join(cost_parts)}"
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

            await self._send_message("\n".join(lines), reply_markup=reply_markup)
