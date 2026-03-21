from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


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
        reasoning = deal_info.get("reasoning", "")
        airline = deal_info.get("airline", "Unknown")
        dates = deal_info.get("dates", "")
        search_url = deal_info.get("google_flights_url") or self._google_flights_url(deal_info)

        score_str = f" ({score:.2f})" if score is not None else ""
        lines = [
            f"✈️ *Deal{score_str}* — {origin} → {dest}",
            f"*€{price}* | {airline} | {dates}",
        ]
        if reasoning:
            lines.append(f"_{reasoning}_")
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

        score_str = f" ({score:.2f})" if score is not None else ""
        lines = [
            f"🔥 *Error Fare{score_str}* — {origin} → {dest}",
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

        lines = [f"✈️ *FareHound Daily* — {len(routes_summary)} route(s)\n"]
        for route in routes_summary:
            origin = route.get("origin", "?")
            dest = route.get("destination", "?")
            lowest = route.get("lowest_price", "—")
            trend = route.get("trend", "")
            trend_icon = {"down": "↓", "up": "↑", "stable": "→"}.get(trend, "")
            lines.append(f"{origin}→{dest}: *€{lowest}* {trend_icon}")

        await self._send_message("\n".join(lines))
