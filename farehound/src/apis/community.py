from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from time import monotonic
from pathlib import Path
from typing import Callable, Awaitable

import feedparser
import httpx
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
    url: str | None = None


def _normalize_date(date_str: str) -> str:
    """Try to convert a date string to ISO format (YYYY-MM-DD). Returns original if unparseable."""
    # Already ISO
    if re.match(r"\d{4}-\d{2}-\d{2}$", date_str):
        return date_str

    # Try common formats: "Oct 8", "8 Oct", "October 8", "8 October"
    now = date.today()
    for fmt in ("%b %d", "%d %b", "%B %d", "%d %B", "%d/%m", "%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            # Inject current year for formats without year to avoid deprecation warning
            if "%Y" not in fmt:
                parsed = datetime.strptime(f"{date_str.strip()} {now.year}", f"{fmt} %Y")
            else:
                parsed = datetime.strptime(date_str.strip(), fmt)
            if parsed.date() < now:
                parsed = parsed.replace(year=now.year + 1)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


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

    # Extract dates and normalize to ISO format
    date_matches = DATE_RE.findall(text)
    dates = []
    for match_groups in date_matches:
        date_str = next((g for g in match_groups if g), None)
        if date_str:
            normalized = _normalize_date(date_str)
            dates.append(normalized)
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


def _parse_reddit_json(raw: str) -> list[dict]:
    """Parse Reddit's JSON listing format into a list of entry dicts."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    children = data.get("data", {}).get("children", [])
    entries = []
    for child in children:
        post = child.get("data", {})
        entries.append({
            "id": post.get("id", ""),
            "title": post.get("title", ""),
            "summary": post.get("selftext", ""),
            "link": f"https://www.reddit.com{post['permalink']}" if post.get("permalink") else "",
        })
    return entries


class RSSListener:
    """Polls RSS feeds for flight deals at a configurable interval."""

    def __init__(
        self,
        feeds: list[CommunityFeedConfig],
        poll_interval_seconds: int = 300,
    ) -> None:
        self.feeds = feeds
        self.poll_interval = poll_interval_seconds
        self._filter_origins: set[str] = set()
        for feed in feeds:
            self._filter_origins.update(o.upper() for o in feed.filter_origins)
        self._seen_ids: dict[str, float] = {}  # entry_id -> timestamp (monotonic)
        self._callback: Callable[[dict], Awaitable[None]] | None = None
        self._running = False

    async def start(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        self._callback = callback
        self._running = True
        logger.info(
            "RSS listener started, polling %d feeds every %ds: %s",
            len(self.feeds),
            self.poll_interval,
            [f.url for f in self.feeds],
        )

    async def run_forever(self) -> None:
        """Poll RSS feeds in a loop. Run as an asyncio task."""
        # Seed seen IDs on first poll to avoid alerting on old posts
        await self._poll(seed=True)

        while self._running:
            await asyncio.sleep(self.poll_interval)
            if not self._running:
                break
            try:
                await self._poll(seed=False)
            except Exception:
                logger.exception("RSS poll cycle failed")

    async def _poll(self, seed: bool = False) -> None:
        # Prune seen IDs older than 7 days to prevent unbounded growth
        _MAX_AGE = 7 * 24 * 3600  # 7 days in seconds
        now = monotonic()
        self._seen_ids = {k: v for k, v in self._seen_ids.items() if now - v < _MAX_AGE}

        async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}) as client:
            for feed_config in self.feeds:
                if not feed_config.url:
                    continue
                try:
                    resp = await client.get(feed_config.url)
                    resp.raise_for_status()

                    # Reddit .json endpoints return their own format
                    if feed_config.url and feed_config.url.endswith(".json"):
                        entries = _parse_reddit_json(resp.text)
                    else:
                        parsed = feedparser.parse(resp.text)
                        entries = [
                            {"id": e.get("id") or e.get("link") or e.get("title", ""),
                             "title": e.get("title", ""), "summary": e.get("summary", ""),
                             "link": e.get("link")}
                            for e in parsed.entries
                        ]

                    for entry in entries:
                        entry_id = entry.get("id") or entry.get("link") or entry.get("title", "")
                        if entry_id in self._seen_ids:
                            continue
                        self._seen_ids[entry_id] = monotonic()

                        if seed:
                            continue

                        # Combine title and summary for parsing
                        text = f"{entry.get('title', '')} {entry.get('summary', '')}"
                        deal = parse_deal_message(text)
                        if deal is None:
                            continue

                        origin = deal.get("origin", "").upper()
                        if self._filter_origins and origin and origin not in self._filter_origins:
                            logger.debug("RSS: skipping deal from %s (not in origins)", origin)
                            continue

                        deal_info = {
                            "origin": deal.get("origin"),
                            "destination": deal.get("destination"),
                            "price": deal.get("price"),
                            "dates": deal.get("dates", []),
                            "source_channel": feed_config.channel,
                            "raw_message": text.strip(),
                            "community_flagged": True,
                            "link": entry.get("link"),
                        }

                        logger.info(
                            "RSS deal detected: %s → %s at %s from %s",
                            deal_info.get("origin"),
                            deal_info.get("destination"),
                            deal_info.get("price"),
                            feed_config.channel,
                        )

                        if self._callback:
                            try:
                                await self._callback(deal_info)
                            except Exception:
                                logger.exception("Error in RSS deal callback")

                except httpx.HTTPError as e:
                    logger.warning("Failed to fetch RSS feed %s: %s", feed_config.url, e)

    def stop(self) -> None:
        self._running = False
        logger.info("RSS listener stopped")
