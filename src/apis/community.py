from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

from telethon import TelegramClient, events

logger = logging.getLogger(__name__)

# Common airport codes for regex matching
IATA_CODE_RE = re.compile(r"\b([A-Z]{3})\b")
# Price patterns: €485, $299, 485 EUR, EUR 485, etc.
PRICE_RE = re.compile(
    r"(?:[\$€£])\s*(\d[\d,]*(?:\.\d{2})?)"
    r"|(\d[\d,]*(?:\.\d{2})?)\s*(?:EUR|USD|GBP|€|\$|£)"
    r"|(?:EUR|USD|GBP)\s*(\d[\d,]*(?:\.\d{2})?)",
    re.IGNORECASE,
)
# Date patterns: 2026-10-08, Oct 8, 8 Oct, 08/10, 10/08
DATE_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\b"
    r"|(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}\b)"
    r"|(\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\b)"
    r"|(\b\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b",
    re.IGNORECASE,
)
# Route arrow patterns: AMS → NRT, AMS-NRT, AMS to NRT, AMS ✈ NRT
ROUTE_RE = re.compile(
    r"\b([A-Z]{3})\s*(?:→|->|–|—|-|to|✈️?)\s*([A-Z]{3})\b",
    re.IGNORECASE,
)


@dataclass
class CommunityFeedConfig:
    channel: str
    filter_origins: list[str] = field(default_factory=list)


def parse_deal_message(text: str) -> dict | None:
    """Extract deal info from a community message.

    Returns a dict with origin, destination, price, dates if parseable,
    or None if the message doesn't look like a deal.
    """
    if not text:
        return None

    result: dict = {}

    # Try route pattern first (AMS → NRT)
    route_match = ROUTE_RE.search(text)
    if route_match:
        result["origin"] = route_match.group(1).upper()
        result["destination"] = route_match.group(2).upper()
    else:
        # Fall back to extracting all IATA codes
        codes = IATA_CODE_RE.findall(text)
        # Filter out common false positives
        codes = [c for c in codes if len(c) == 3 and c.isalpha()]
        if len(codes) >= 2:
            result["origin"] = codes[0]
            result["destination"] = codes[1]

    # Extract price
    price_match = PRICE_RE.search(text)
    if price_match:
        raw_price = next(g for g in price_match.groups() if g is not None)
        raw_price = raw_price.replace(",", "")
        try:
            result["price"] = float(raw_price)
        except ValueError:
            pass

    # Extract dates
    date_matches = DATE_RE.findall(text)
    dates = []
    for match_groups in date_matches:
        date_str = next((g for g in match_groups if g), None)
        if date_str:
            dates.append(date_str)
    if dates:
        result["dates"] = dates

    # Need at least origin+destination or price to consider it a deal
    if "origin" not in result and "price" not in result:
        return None

    return result


class CommunityListener:
    """Monitors Telegram channels for error fare deals via Telethon."""

    def __init__(
        self,
        api_id: str,
        api_hash: str,
        feeds: list[CommunityFeedConfig],
        session_path: str | Path = "/data/farehound_telegram",
    ) -> None:
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.feeds = feeds
        self._filter_origins: set[str] = set()
        for feed in feeds:
            self._filter_origins.update(
                o.upper() for o in feed.filter_origins
            )
        self._channels = [f.channel for f in feeds]
        self._client = TelegramClient(
            str(session_path), self.api_id, self.api_hash
        )
        self._callback: Callable[[dict], Awaitable[None]] | None = None

    async def start(
        self, callback: Callable[[dict], Awaitable[None]]
    ) -> None:
        """Connect to Telegram and start monitoring channels.

        Args:
            callback: Async function called with deal_info dict when a
                matching deal is detected. Keys: origin, destination,
                price, dates, source_channel, raw_message, community_flagged.
        """
        self._callback = callback

        await self._client.connect()
        if not await self._client.is_user_authorized():
            logger.error(
                "Telegram client not authorized. Run an interactive session "
                "first to complete auth and generate the session file."
            )
            raise RuntimeError("Telegram client not authorized")

        logger.info(
            "Telegram connected, monitoring %d channels: %s",
            len(self._channels),
            self._channels,
        )

        @self._client.on(events.NewMessage(chats=self._channels))
        async def _on_message(event: events.NewMessage.Event) -> None:
            await self._handle_message(event)

    async def _handle_message(
        self, event: events.NewMessage.Event
    ) -> None:
        text = event.message.text or ""
        if not text.strip():
            return

        deal = parse_deal_message(text)
        if deal is None:
            return

        # Filter by watched origin airports if configured
        origin = deal.get("origin", "").upper()
        if self._filter_origins and origin and origin not in self._filter_origins:
            logger.debug(
                "Skipping deal from %s (not in watched origins)", origin
            )
            return

        # Build callback payload
        chat = await event.get_chat()
        channel_name = getattr(chat, "username", None) or str(chat.id)

        deal_info = {
            "origin": deal.get("origin"),
            "destination": deal.get("destination"),
            "price": deal.get("price"),
            "dates": deal.get("dates", []),
            "source_channel": channel_name,
            "raw_message": text,
            "community_flagged": True,
        }

        logger.info(
            "Community deal detected: %s → %s at %s from %s",
            deal_info.get("origin"),
            deal_info.get("destination"),
            deal_info.get("price"),
            channel_name,
        )

        if self._callback:
            try:
                await self._callback(deal_info)
            except Exception:
                logger.exception("Error in community deal callback")

    async def disconnect(self) -> None:
        """Gracefully disconnect from Telegram."""
        if self._client.is_connected():
            await self._client.disconnect()
            logger.info("Telegram client disconnected")

    async def run_until_disconnected(self) -> None:
        """Block until the client disconnects. Use this in the main loop."""
        await self._client.run_until_disconnected()
