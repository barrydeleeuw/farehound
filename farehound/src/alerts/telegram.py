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

    async def _send_message(self, text: str, parse_mode: str = "Markdown") -> None:
        url = f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
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
        url = f"https://www.google.com/travel/flights?q=Flights+from+{origin}+to+{dest}"
        if outbound:
            url += f"+on+{outbound}"
        if return_date:
            url += f"+return+{return_date}"
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

        emoji = _deal_emoji(score, urgency)
        label = _deal_label(score, urgency)
        route = route_name(origin, dest)
        lines = [
            f"{emoji} *{label}* — {route}",
            f"*€{price}* | {airline} | {dates}",
        ]
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
                t_min = alt.get("transport_time_min", 0)
                hours = t_min / 60
                lines.append(
                    f"{icon} {name}: €{fare:,.0f}/pp → €{net:,.0f} net (save €{savings:,.0f})"
                )
                lines.append(
                    f"    {mode} €{t_cost:.0f} return | {hours:.1f}h to airport"
                )

        lines.append(f"[Search Flights]({search_url})")

        await self._send_message("\n".join(lines))

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
            f"*€{price}* | {airline} | {dates}",
            "BOOK NOW — these usually disappear fast!",
        ]
        if reasoning:
            lines.append(f"_{reasoning}_")
        lines.append(f"[Book Now]({booking_url})")

        await self._send_message("\n".join(lines))

    async def send_daily_digest(self, routes_summary: list[dict]) -> None:
        if not routes_summary:
            return

        lines = [f"📊 *FareHound Daily* — {len(routes_summary)} route(s)\n"]
        for route in routes_summary:
            origin = route.get("origin", "?")
            dest = route.get("destination", "?")
            lowest = route.get("lowest_price", "—")
            trend = route.get("trend", "")
            trend_icon = {"down": "📉", "up": "📈", "stable": "➡️"}.get(trend, "")
            score = route.get("deal_score")
            emoji = _deal_emoji(score)
            lines.append(f"{emoji} {route_name(origin, dest)}: *€{lowest}* {trend_icon}")

            nearby_prices = route.get("nearby_prices") or []
            if nearby_prices:
                from src.utils.airports import airport_name
                origin_name = airport_name(origin)
                passengers = route.get("passengers", 2)
                lines.append(f"  {origin_name}: €{lowest}/pp")
                for i, alt in enumerate(nearby_prices):
                    icon = "🟢" if i == 0 else ""
                    name = alt.get("airport_name") or alt.get("airport_code", "?")
                    fare = alt.get("fare_pp", 0)
                    net = alt.get("net_cost", 0)
                    savings = alt.get("savings", 0)
                    savings_str = f", save €{savings:,.0f}" if savings else ""
                    lines.append(
                        f"  {icon} {name}: €{fare:,.0f}/pp (€{net:,.0f} net{savings_str})".rstrip()
                    )

        await self._send_message("\n".join(lines))
