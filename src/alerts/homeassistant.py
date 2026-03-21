from __future__ import annotations

import logging
import os
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class HomeAssistantNotifier:
    """Send notifications via the Home Assistant Supervisor REST API."""

    def __init__(
        self,
        notify_service: str,
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        # notify_service e.g. "notify.mobile_app_barry_phone"
        # We need just the part after "notify." for the API path
        self._full_service = notify_service
        self._service_name = notify_service.removeprefix("notify.")
        self._base_url = (
            base_url
            or os.environ.get("SUPERVISOR_URL", "http://supervisor/core")
        )
        self._token = (
            token
            or os.environ.get("SUPERVISOR_TOKEN", "")
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _google_flights_url(self, deal: dict) -> str:
        """Build a Google Flights search URL from deal info."""
        origin = deal.get("origin", "")
        destination = deal.get("destination", "")
        outbound = deal.get("outbound_date", "")
        return_date = deal.get("return_date", "")
        passengers = deal.get("passengers", 1)

        url = (
            f"https://www.google.com/travel/flights?q=Flights"
            f"%20from%20{quote(origin)}%20to%20{quote(destination)}"
        )
        if outbound:
            url += f"%20on%20{outbound}"
        if return_date:
            url += f"%20return%20{return_date}"
        if passengers and passengers > 1:
            url += f"&px={passengers}"
        return url

    async def _call_service(self, payload: dict) -> None:
        """POST to the HA notify service endpoint."""
        url = f"{self._base_url}/api/services/notify/{self._service_name}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
                resp.raise_for_status()
                logger.info("Notification sent via %s", self._full_service)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "HA API error %s: %s", exc.response.status_code, exc.response.text
            )
        except httpx.ConnectError:
            logger.error("Cannot reach Home Assistant at %s", self._base_url)
        except httpx.TimeoutException:
            logger.error("Timeout calling Home Assistant notify service")
        except Exception:
            logger.exception("Unexpected error sending HA notification")

    async def send_deal_alert(self, deal_info: dict) -> None:
        """Layer 1 — scheduled poll deal alert."""
        origin = deal_info.get("origin", "???")
        dest = deal_info.get("destination", "???")
        price = deal_info.get("price", "?")
        score = deal_info.get("score")
        reasoning = deal_info.get("reasoning", "")
        airline = deal_info.get("airline", "Unknown")
        dates = deal_info.get("dates", "")
        search_url = deal_info.get("google_flights_url") or self._google_flights_url(
            deal_info
        )

        score_str = f" ({score:.2f})" if score is not None else ""
        title = f"✈️ Deal{score_str} — {origin} → {dest} | €{price}"
        message = f"{airline} | {dates} | €{price}"
        if reasoning:
            message += f"\n{reasoning}"

        payload = {
            "title": title,
            "message": message,
            "data": {
                "actions": [
                    {
                        "action": "URI",
                        "title": "Search Flights",
                        "uri": search_url,
                    }
                ],
            },
        }

        logger.info("Sending deal alert: %s → %s @ €%s", origin, dest, price)
        await self._call_service(payload)

    async def send_error_fare_alert(self, deal_info: dict) -> None:
        """Layer 2 — community-triggered error fare alert."""
        origin = deal_info.get("origin", "???")
        dest = deal_info.get("destination", "???")
        price = deal_info.get("price", "?")
        score = deal_info.get("score")
        reasoning = deal_info.get("reasoning", "")
        airline = deal_info.get("airline", "Unknown")
        dates = deal_info.get("dates", "")
        booking_url = deal_info.get("booking_url") or deal_info.get(
            "google_flights_url"
        ) or self._google_flights_url(deal_info)

        score_str = f" ({score:.2f})" if score is not None else ""
        title = f"🔥 Error Fare{score_str} — {origin} → {dest} | €{price}"
        message = f"BOOK NOW — {airline} | {dates} | €{price}"
        if reasoning:
            message += f"\n{reasoning}"

        payload = {
            "title": title,
            "message": message,
            "data": {
                "ttl": 0,
                "priority": "high",
                "actions": [
                    {
                        "action": "URI",
                        "title": "Book Now",
                        "uri": booking_url,
                    }
                ],
            },
        }

        logger.info("Sending error fare alert: %s → %s @ €%s", origin, dest, price)
        await self._call_service(payload)

    async def send_daily_digest(self, routes_summary: list[dict]) -> None:
        """Daily summary of all monitored routes."""
        if not routes_summary:
            logger.info("No routes to summarise, skipping daily digest")
            return

        lines: list[str] = []
        for route in routes_summary:
            origin = route.get("origin", "?")
            dest = route.get("destination", "?")
            lowest = route.get("lowest_price", "—")
            trend = route.get("trend", "")
            trend_icon = {"down": "↓", "up": "↑", "stable": "→"}.get(trend, "")
            lines.append(f"{origin}→{dest}: €{lowest} {trend_icon}")

        title = f"✈️ FareHound Daily — {len(routes_summary)} route(s)"
        message = "\n".join(lines)

        payload = {
            "title": title,
            "message": message,
        }

        logger.info("Sending daily digest for %d routes", len(routes_summary))
        await self._call_service(payload)
