from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.alerts.homeassistant import HomeAssistantNotifier
from src.alerts.telegram import TelegramNotifier
from src.analysis.scorer import DealScorer
from src.apis.community import CommunityListener, RSSListener
from src.apis.community import CommunityFeedConfig as CommunityFeed
from src.apis.serpapi import SerpAPIClient, SerpAPIError, VerificationResult, generate_date_windows
from src.bot.commands import TripBot
from src.config import AppConfig, Route as ConfigRoute, load_config
from src.storage.db import Database
from src.storage.models import Deal, PollWindow, PriceSnapshot, Route as DBRoute

logger = logging.getLogger("farehound.orchestrator")

# Default trip duration when computing date windows
DEFAULT_TRIP_DURATION_DAYS = 14
# How often to do a full rescan of all windows (days)
FULL_RESCAN_INTERVAL_DAYS = 7
# Max windows per route for initial scan
DEFAULT_MAX_WINDOWS = 4
# Percentage drop below average that triggers an alert (used as pre-filter and static fallback)
DROP_PERCENT_THRESHOLD = 0.15
# Minimum snapshots before we have enough history to skip Claude scoring
COLD_START_THRESHOLD = 5


def _config_route_to_db(r: ConfigRoute) -> DBRoute:
    return DBRoute(
        route_id=r.id,
        origin=r.origin,
        destination=r.destination,
        trip_type=r.trip_type,
        earliest_departure=date.fromisoformat(r.earliest_departure) if r.earliest_departure else None,
        latest_return=date.fromisoformat(r.latest_return) if r.latest_return else None,
        date_flex_days=r.date_flexibility_days,
        max_stops=r.max_stops,
        passengers=r.passengers,
        preferred_airlines=r.preferred_airlines,
        notes=r.notes,
        active=True,
    )


class Orchestrator:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.db = Database()
        self.serpapi = SerpAPIClient(
            api_key=config.serpapi.api_key,
            currency=config.serpapi.currency,
        )
        self.notifier = HomeAssistantNotifier(
            notify_service=config.alerts.notify_service,
            base_url=config.alerts.base_url,
            token=config.alerts.token,
        )
        self.scorer = DealScorer(
            api_key=config.anthropic.api_key,
            model=config.anthropic.model,
        )
        # Telegram alert channel (optional)
        self.telegram_notifier: TelegramNotifier | None = None
        if config.telegram_alerts is not None and config.telegram_alerts.enabled:
            self.telegram_notifier = TelegramNotifier(
                bot_token=config.telegram_alerts.bot_token,
                chat_id=config.telegram_alerts.chat_id,
            )

        self.scheduler = AsyncIOScheduler()
        self._first_run = True
        self._last_full_rescan: datetime | None = None

        # Community listeners (Layer 2)
        self.community_listener: CommunityListener | None = None
        self._community_task: asyncio.Task | None = None
        self.rss_listener: RSSListener | None = None
        self._rss_task: asyncio.Task | None = None

        # Split feeds by type
        telegram_feeds = [f for f in config.community_feeds if f.type == "telegram_channel"]
        rss_feeds = [f for f in config.community_feeds if f.type == "rss"]

        if config.telegram is not None and telegram_feeds:
            feeds = [
                CommunityFeed(channel=f.channel, filter_origins=f.filter_origins)
                for f in telegram_feeds
            ]
            self.community_listener = CommunityListener(
                api_id=config.telegram.api_id,
                api_hash=config.telegram.api_hash,
                feeds=feeds,
            )

        if rss_feeds:
            feeds = [
                CommunityFeed(channel=f.channel, filter_origins=f.filter_origins, url=f.url)
                for f in rss_feeds
            ]
            self.rss_listener = RSSListener(feeds=feeds)

        # Telegram bot for /trip commands (reuses alert bot token)
        self.trip_bot: TripBot | None = None
        self._trip_bot_task: asyncio.Task | None = None
        if config.telegram_alerts is not None and config.telegram_alerts.enabled:
            self.trip_bot = TripBot(
                bot_token=config.telegram_alerts.bot_token,
                chat_id=config.telegram_alerts.chat_id,
                db=self.db,
                anthropic_api_key=config.anthropic.api_key,
                anthropic_model=config.anthropic.model,
                home_airport=config.traveller.home_airport,
                reload_callback=self.reload_routes,
            )

    async def reload_routes(self) -> None:
        """Re-sync routes from DB so polling picks up bot-added/removed routes."""
        logger.info("Reloading routes from database")
        self._first_run = True

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        # Init DB schema (sync)
        await loop.run_in_executor(None, self.db.init_schema)
        logger.info("Database schema initialized")

        # Sync routes from config into DB
        for route_cfg in self.config.routes:
            db_route = _config_route_to_db(route_cfg)
            await loop.run_in_executor(None, self.db.upsert_route, db_route)
            logger.info("Synced route: %s (%s -> %s)", route_cfg.id, route_cfg.origin, route_cfg.destination)

        # Schedule polling job
        interval_hours = self.config.scoring.poll_interval_hours
        self.scheduler.add_job(
            self.poll_routes,
            "interval",
            hours=interval_hours,
            id="poll_routes",
            misfire_grace_time=60,  # allow 60s grace for startup delays
        )
        logger.info("Scheduled polling every %d hours", interval_hours)

        # Schedule daily digest
        digest_hour, digest_minute = self.config.scoring.digest_time
        self.scheduler.add_job(
            self.send_daily_digest,
            "cron",
            hour=digest_hour,
            minute=digest_minute,
            id="daily_digest",
        )
        logger.info("Scheduled daily digest at %02d:%02d", digest_hour, digest_minute)

        # Register signal handlers
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.shutdown(s)))

        # Start community listeners as concurrent tasks
        if self.community_listener is not None:
            try:
                await self.community_listener.start(callback=self.on_community_deal)
                self._community_task = asyncio.create_task(
                    self.community_listener.run_until_disconnected()
                )
                self._community_task.add_done_callback(self._on_community_task_done)
                logger.info("Telegram community listener started")
            except RuntimeError:
                logger.warning("Telegram channel listener skipped (not authorized). RSS feeds still active.")

        if self.rss_listener is not None:
            await self.rss_listener.start(callback=self.on_community_deal)
            self._rss_task = asyncio.create_task(self.rss_listener.run_forever())
            self._rss_task.add_done_callback(self._on_community_task_done)
            logger.info("RSS community listener started")

        # Start TripBot for /trip commands
        if self.trip_bot is not None:
            self._trip_bot_task = asyncio.create_task(self.trip_bot.run())
            self._trip_bot_task.add_done_callback(self._on_community_task_done)
            logger.info("TripBot command handler started")

        self.scheduler.start()
        logger.info("Orchestrator started")

        # Run first poll immediately (scheduler interval starts after this)
        asyncio.create_task(self.poll_routes())

        # Keep running until shutdown
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    def _on_community_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("Community listener crashed: %s", exc, exc_info=exc)

    async def shutdown(self, sig: signal.Signals | None = None) -> None:
        if sig:
            logger.info("Received signal %s, shutting down", sig.name)
        else:
            logger.info("Shutting down")

        if self.community_listener is not None:
            await self.community_listener.disconnect()
        if self.rss_listener is not None:
            self.rss_listener.stop()
        if self.trip_bot is not None:
            self.trip_bot.stop()

        self.scheduler.shutdown(wait=False)
        await self.serpapi.close()
        self.db.close()
        logger.info("Shutdown complete")

        # Cancel all running tasks
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()

    async def poll_routes(self) -> None:
        logger.info("Starting poll cycle")
        loop = asyncio.get_running_loop()

        routes: list[DBRoute] = await loop.run_in_executor(None, self.db.get_active_routes)
        if not routes:
            logger.warning("No active routes to poll")
            return

        logger.info("Polling %d active routes", len(routes))

        # Process routes in parallel, but searches within a route are sequential
        tasks = [self._poll_single_route(route) for route in routes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for route, result in zip(routes, results):
            if isinstance(result, Exception):
                logger.error("Route %s failed: %s", route.route_id, result, exc_info=result)

        self._first_run = False
        logger.info("Poll cycle complete")

        # Update HA sensors with latest route data
        await self._update_ha_sensors(routes)

    async def _update_ha_sensors(self, routes: list[DBRoute]) -> None:
        """Build routes_summary from latest snapshots and push to HA sensors."""
        loop = asyncio.get_running_loop()
        summaries: list[dict] = []

        for route in routes:
            latest = await loop.run_in_executor(None, self.db.get_latest_snapshot, route.route_id)
            if latest is None:
                continue

            # 7-day trend
            history_7d = await loop.run_in_executor(None, self.db.get_price_history, route.route_id, 7)
            price_now = float(latest.lowest_price) if latest.lowest_price else None
            avg_7d = float(history_7d["avg_price"]) if history_7d.get("avg_price") else None

            if price_now is not None and avg_7d is not None and avg_7d > 0:
                diff = (price_now - avg_7d) / avg_7d
                trend = "down" if diff < -0.03 else ("up" if diff > 0.03 else "stable")
            else:
                trend = ""

            # Latest deal score
            since = datetime.now(UTC) - timedelta(days=1)
            recent_deals = await loop.run_in_executor(None, self.db.get_deals_since, route.route_id, since)
            deal_score = float(recent_deals[0].score) if recent_deals else None

            summaries.append({
                "route_id": route.route_id,
                "origin": route.origin,
                "destination": route.destination,
                "lowest_price": price_now,
                "currency": latest.currency or "EUR",
                "trend": trend,
                "last_checked": latest.observed_at.isoformat() if latest.observed_at else "",
                "deal_score": deal_score,
            })

        if summaries:
            try:
                await self.notifier.update_sensors(summaries)
                logger.info("Updated %d HA sensors", len(summaries))
            except Exception:
                logger.exception("Failed to update HA sensors")

    async def send_daily_digest(self) -> None:
        logger.info("Preparing daily digest")
        loop = asyncio.get_running_loop()

        routes: list[DBRoute] = await loop.run_in_executor(None, self.db.get_active_routes)
        if not routes:
            logger.info("No active routes, skipping digest")
            return

        since = datetime.now(UTC) - timedelta(days=1)
        summaries: list[dict] = []

        for route in routes:
            latest = await loop.run_in_executor(None, self.db.get_latest_snapshot, route.route_id)
            if latest is None:
                continue

            # 7-day trend
            history_7d = await loop.run_in_executor(None, self.db.get_price_history, route.route_id, 7)
            history_now = float(latest.lowest_price) if latest.lowest_price else None
            avg_7d = float(history_7d["avg_price"]) if history_7d.get("avg_price") else None

            if history_now is not None and avg_7d is not None and avg_7d > 0:
                diff = (history_now - avg_7d) / avg_7d
                if diff < -0.03:
                    trend = "down"
                elif diff > 0.03:
                    trend = "up"
                else:
                    trend = "stable"
            else:
                trend = ""

            # Recent watch-level deals
            recent_deals = await loop.run_in_executor(None, self.db.get_deals_since, route.route_id, since)
            watch_deals = [d for d in recent_deals if d.urgency == "watch"]

            summary: dict = {
                "origin": route.origin,
                "destination": route.destination,
                "lowest_price": float(latest.lowest_price) if latest.lowest_price else None,
                "trend": trend,
            }

            if watch_deals:
                summary["watch_deals"] = len(watch_deals)

            summaries.append(summary)

        if not summaries:
            logger.info("No route data for digest, skipping")
            return

        try:
            await self.notifier.send_daily_digest(summaries)
            if self.telegram_notifier:
                await self.telegram_notifier.send_daily_digest(summaries)
        except Exception:
            logger.exception("Failed to send daily digest")

    async def _poll_single_route(self, route: DBRoute) -> None:
        loop = asyncio.get_running_loop()
        logger.info("Polling route %s: %s -> %s", route.route_id, route.origin, route.destination)

        if not route.earliest_departure or not route.latest_return:
            logger.warning("Route %s missing date range, skipping", route.route_id)
            return

        # Generate date windows
        trip_duration = DEFAULT_TRIP_DURATION_DAYS
        try:
            windows = generate_date_windows(
                earliest_departure=route.earliest_departure,
                latest_return=route.latest_return,
                trip_duration_days=trip_duration,
                max_windows=DEFAULT_MAX_WINDOWS,
            )
        except ValueError as e:
            logger.error("Cannot generate windows for route %s: %s", route.route_id, e)
            return

        # Decide which windows to poll
        windows_to_poll = await self._select_windows(route, windows)

        for outbound, return_dt in windows_to_poll:
            try:
                await self._search_and_store(route, outbound, return_dt)
            except SerpAPIError as e:
                logger.error(
                    "SerpAPI error for %s (%s to %s): %s",
                    route.route_id, outbound, return_dt, e,
                )
            except Exception as e:
                logger.error(
                    "Unexpected error polling %s (%s to %s): %s",
                    route.route_id, outbound, return_dt, e,
                    exc_info=True,
                )

    async def _select_windows(
        self, route: DBRoute, all_windows: list[tuple[date, date]]
    ) -> list[tuple[date, date]]:
        loop = asyncio.get_running_loop()

        # First run or weekly rescan: poll all windows
        needs_full_rescan = (
            self._first_run
            or self._last_full_rescan is None
            or (datetime.now(UTC) - self._last_full_rescan).days >= FULL_RESCAN_INTERVAL_DAYS
        )

        if needs_full_rescan:
            self._last_full_rescan = datetime.now(UTC)
            logger.info("Full rescan for route %s (%d windows)", route.route_id, len(all_windows))
            return all_windows

        # Subsequent runs: focus on windows with lowest prices
        existing_windows: list[PollWindow] = await loop.run_in_executor(
            None, self.db.get_poll_windows, route.route_id
        )

        if not existing_windows:
            return all_windows

        # Find focus windows (lowest price) and always include them
        focus = [w for w in existing_windows if w.priority == "focus"]
        if focus:
            focus_dates = {(w.outbound_date, w.return_date) for w in focus}
            selected = [(o, r) for o, r in all_windows if (o, r) in focus_dates]
            if selected:
                logger.info(
                    "Focus polling %d windows for route %s",
                    len(selected), route.route_id,
                )
                return selected

        # Fallback: poll the window with the overall lowest seen price
        sorted_windows = sorted(
            existing_windows,
            key=lambda w: float(w.lowest_seen_price) if w.lowest_seen_price is not None else float("inf"),
        )
        best = sorted_windows[0]
        selected = [(o, r) for o, r in all_windows if o == best.outbound_date]
        return selected or [all_windows[0]]

    async def _search_and_store(self, route: DBRoute, outbound: date, return_dt: date) -> None:
        loop = asyncio.get_running_loop()

        result = await self.serpapi.search_flights(
            origin=route.origin,
            destination=route.destination,
            outbound_date=outbound,
            return_date=return_dt,
            passengers=route.passengers,
            trip_type=route.trip_type,
        )

        now = datetime.now(UTC)
        insights = result.price_insights
        lowest_price = insights.get("lowest_price")
        typical_range = insights.get("typical_price_range", [])

        # Extract best flight info
        best_flight = result.best_flights[0] if result.best_flights else None

        snapshot = PriceSnapshot(
            snapshot_id=uuid4().hex,
            route_id=route.route_id,
            observed_at=now,
            source="serpapi_poll",
            passengers=route.passengers,
            outbound_date=outbound,
            return_date=return_dt,
            lowest_price=Decimal(str(lowest_price)) if lowest_price is not None else None,
            currency=self.config.serpapi.currency,
            best_flight=best_flight,
            all_flights=result.best_flights + result.other_flights,
            price_level=insights.get("price_level"),
            typical_low=Decimal(str(typical_range[0])) if len(typical_range) > 0 else None,
            typical_high=Decimal(str(typical_range[1])) if len(typical_range) > 1 else None,
            price_history=insights.get("price_history"),
            search_params=result.search_params,
        )

        await loop.run_in_executor(None, self.db.insert_snapshot, snapshot)
        logger.info(
            "Stored snapshot for %s: %s->%s on %s, price=%s",
            route.route_id, route.origin, route.destination, outbound, lowest_price,
        )

        # Update poll window tracking
        await loop.run_in_executor(
            None, self.db.update_poll_window,
            route.route_id, outbound, return_dt,
            float(lowest_price) if lowest_price is not None else None,
        )

        # Check alert rules
        if lowest_price is not None:
            await self._check_alerts(route, snapshot, float(lowest_price), best_flight)

    async def _check_alerts(
        self,
        route: DBRoute,
        snapshot: PriceSnapshot,
        price: float,
        best_flight: dict | None,
    ) -> None:
        loop = asyncio.get_running_loop()

        # Get price history for pre-filter and scoring context
        history = await loop.run_in_executor(None, self.db.get_price_history, route.route_id, 90)
        avg_price = history.get("avg_price")
        sample_count = history.get("count", 0)

        # Pre-filter: only score with Claude if price looks interesting
        # (below 90-day avg, or cold start with < 5 snapshots)
        is_cold_start = sample_count < COLD_START_THRESHOLD
        is_below_avg = (
            avg_price is not None
            and float(avg_price) > 0
            and price < float(avg_price)
        )

        if not is_cold_start and not is_below_avg:
            logger.debug(
                "Route %s price %s not below avg %s, skipping scoring",
                route.route_id, price, avg_price,
            )
            return

        # Fetch past feedback for scoring calibration
        feedback = await loop.run_in_executor(None, self.db.get_recent_feedback)

        # Score with Claude, fall back to static threshold on error
        score_result = None
        try:
            score_result = await self.scorer.score_deal(
                snapshot=snapshot,
                route=route,
                price_history=history,
                traveller_name=self.config.traveller.name,
                home_airport=self.config.traveller.home_airport,
                traveller_preferences=self.config.traveller.preferences or None,
                past_feedback=feedback or None,
            )
            logger.info(
                "Route %s scored: %.2f (%s) — %s",
                route.route_id, score_result.score, score_result.urgency, score_result.reasoning,
            )
        except Exception:
            logger.exception("Claude scoring failed for route %s, falling back to static threshold", route.route_id)
            score_result = self._static_fallback(price, avg_price)

        # Store deal record
        now = datetime.now(UTC)
        deal = Deal(
            deal_id=uuid4().hex,
            snapshot_id=snapshot.snapshot_id,
            route_id=route.route_id,
            score=Decimal(str(round(score_result.score, 2))),
            urgency=score_result.urgency,
            reasoning=score_result.reasoning,
        )

        # Determine action based on thresholds
        alert_threshold = self.config.scoring.alert_threshold
        watch_threshold = self.config.scoring.watch_threshold

        should_alert = False
        inflection_msg: str | None = None

        if score_result.score >= alert_threshold:
            # Smart dedup: decide whether this alert is meaningful
            last_alerted = await loop.run_in_executor(
                None, self.db.get_last_alerted_price, route.route_id
            )

            # Rule 1: New low — price is lower than last alerted price
            is_new_low = last_alerted is None or price < last_alerted

            # Rule 2: Book now + low — Claude says book_now AND Google says price_level is low
            is_book_now_and_low = (
                score_result.urgency == "book_now"
                and snapshot.price_level == "low"
            )

            # Rule 3: Inflection detection — price was dropping then ticked up
            inflection, bottom_price = await loop.run_in_executor(
                None, self.db.detect_price_inflection, route.route_id
            )
            if inflection and bottom_price is not None:
                inflection_msg = (
                    f"Price bottomed out at €{bottom_price:.0f}"
                    " — book now before it rises further."
                )

            should_alert = is_new_low or is_book_now_and_low or inflection

            if should_alert:
                deal.alert_sent = True
                deal.alert_sent_at = now
                if inflection_msg and not is_new_low:
                    deal.reasoning = inflection_msg
            else:
                logger.info(
                    "Route %s scored %.2f but deduped (last alerted at €%.0f, current €%.0f)",
                    route.route_id, score_result.score,
                    last_alerted or 0, price,
                )
        elif score_result.score >= watch_threshold:
            logger.info("Route %s is watch-level (%.2f), will include in digest", route.route_id, score_result.score)
        else:
            logger.info("Route %s scored below watch threshold (%.2f), skipping", route.route_id, score_result.score)

        await loop.run_in_executor(None, self.db.insert_deal, deal)

        # Send alert for deals that passed dedup
        if deal.alert_sent:
            airline = "Unknown"
            stops = 0
            if best_flight:
                flights = best_flight.get("flights", [])
                if flights:
                    airline = flights[0].get("airline", "Unknown")
                stops = max(0, len(flights) - 1) if flights else 0

            deal_info = {
                "deal_id": deal.deal_id,
                "origin": route.origin,
                "destination": route.destination,
                "price": price,
                "avg_price": f"{float(avg_price):.0f}" if avg_price else "?",
                "airline": airline,
                "stops": stops,
                "dates": f"{snapshot.outbound_date} to {snapshot.return_date}",
                "outbound_date": str(snapshot.outbound_date),
                "return_date": str(snapshot.return_date),
                "passengers": route.passengers,
                "score": score_result.score,
                "urgency": score_result.urgency,
                "reasoning": inflection_msg or score_result.reasoning,
            }

            try:
                await self.notifier.send_deal_alert(deal_info)
                if self.telegram_notifier:
                    await self.telegram_notifier.send_deal_alert(deal_info)
            except Exception:
                logger.exception("Failed to send alert for route %s", route.route_id)

    async def on_community_deal(self, deal_info: dict) -> None:
        """Handle a deal detected from community channels (Layer 2).

        1. Match against active routes
        2. Verify fare via SerpAPI
        3. Score with Claude (community_flagged=True)
        4. Send error fare alert if score meets threshold
        """
        loop = asyncio.get_running_loop()
        origin = (deal_info.get("origin") or "").upper()
        destination = (deal_info.get("destination") or "").upper()
        community_price = deal_info.get("price")

        if not origin or not destination:
            logger.debug("Community deal missing origin/destination, skipping")
            return

        # Match against active routes
        routes: list[DBRoute] = await loop.run_in_executor(None, self.db.get_active_routes)
        matched = [
            r for r in routes
            if r.origin.upper() == origin and r.destination.upper() == destination
        ]

        if not matched:
            logger.debug(
                "Community deal %s → %s does not match any active route", origin, destination,
            )
            return

        route = matched[0]
        logger.info(
            "Community deal matches route %s: %s → %s (community price: %s)",
            route.route_id, origin, destination, community_price,
        )

        # --- Pre-filter 1: Date window check ---
        # If the deal specifies dates, check they fall within the route's travel window
        dates = deal_info.get("dates", [])
        outbound_date: date | None = None
        return_date: date | None = None
        if dates:
            try:
                outbound_date = date.fromisoformat(dates[0])
                if len(dates) > 1:
                    return_date = date.fromisoformat(dates[1])
            except (ValueError, TypeError):
                pass

        if outbound_date is not None and route.earliest_departure and route.latest_return:
            if outbound_date < route.earliest_departure or outbound_date > route.latest_return:
                logger.debug(
                    "Community deal date %s outside route window %s–%s, skipping",
                    outbound_date, route.earliest_departure, route.latest_return,
                )
                return

        # Fall back to route dates if community deal didn't specify
        if outbound_date is None and route.earliest_departure:
            outbound_date = route.earliest_departure
            return_date = route.latest_return

        if outbound_date is None:
            logger.warning("No dates available for verification of community deal, skipping")
            return

        # --- Pre-filter 2: Price sanity check ---
        # Skip if community price is higher than our historical average (not a deal)
        if community_price is not None:
            history = await loop.run_in_executor(None, self.db.get_price_history, route.route_id, 90)
            avg_price_precheck = history.get("avg_price")
            if avg_price_precheck is not None and community_price > float(avg_price_precheck):
                logger.debug(
                    "Community price €%.0f above 90-day avg €%.0f for %s, skipping",
                    community_price, float(avg_price_precheck), route.route_id,
                )
                return

        # Verify fare via SerpAPI
        try:
            verification = await self.serpapi.verify_fare(
                origin=origin,
                destination=destination,
                outbound_date=outbound_date,
                return_date=return_date,
                expected_price=community_price or 0,
                passengers=route.passengers,
            )
        except SerpAPIError as e:
            logger.error("SerpAPI verification failed for community deal: %s", e)
            return

        if not verification.verified:
            logger.info(
                "Community deal %s → %s not verified (actual: €%s, expected: €%s)",
                origin, destination, verification.actual_price, community_price,
            )
            return

        actual_price = verification.actual_price
        if actual_price is None:
            logger.warning("Verification returned no price for %s → %s", origin, destination)
            return

        # Store snapshot from verification
        now = datetime.now(UTC)
        best_flight = verification.flights[0] if verification.flights else None
        insights = verification.price_insights
        typical_range = insights.get("typical_price_range", [])

        snapshot = PriceSnapshot(
            snapshot_id=uuid4().hex,
            route_id=route.route_id,
            observed_at=now,
            source="serpapi_verify",
            passengers=route.passengers,
            outbound_date=outbound_date,
            return_date=return_date,
            lowest_price=Decimal(str(actual_price)),
            currency=self.config.serpapi.currency,
            best_flight=best_flight,
            all_flights=verification.flights,
            price_level=insights.get("price_level"),
            typical_low=Decimal(str(typical_range[0])) if len(typical_range) > 0 else None,
            typical_high=Decimal(str(typical_range[1])) if len(typical_range) > 1 else None,
            price_history=insights.get("price_history"),
        )

        await loop.run_in_executor(None, self.db.insert_snapshot, snapshot)
        logger.info(
            "Stored verification snapshot for %s: €%s", route.route_id, actual_price,
        )

        # --- Pre-filter 3: Only score with Claude if price looks genuinely good ---
        history = await loop.run_in_executor(None, self.db.get_price_history, route.route_id, 90)
        avg_price = history.get("avg_price")
        min_price = history.get("min_price")
        snapshot_count = history.get("count", 0)

        # If we have enough history, skip Claude if price isn't at least 10% below average
        if snapshot_count >= COLD_START_THRESHOLD and avg_price is not None:
            if actual_price > float(avg_price) * 0.90:
                logger.info(
                    "Verified price €%.0f not significantly below avg €%.0f for %s, skipping Claude",
                    actual_price, float(avg_price), route.route_id,
                )
                return

        # Fetch past feedback for scoring calibration
        community_feedback = await loop.run_in_executor(None, self.db.get_recent_feedback)

        score_result = None
        try:
            score_result = await self.scorer.score_deal(
                snapshot=snapshot,
                route=route,
                price_history=history,
                community_flagged=True,
                traveller_name=self.config.traveller.name,
                home_airport=self.config.traveller.home_airport,
                traveller_preferences=self.config.traveller.preferences or None,
                past_feedback=community_feedback or None,
            )
            logger.info(
                "Community deal scored: %.2f (%s) — %s",
                score_result.score, score_result.urgency, score_result.reasoning,
            )
        except Exception:
            logger.exception("Claude scoring failed for community deal, sending price-only alert")
            # Fallback: send price-only alert without scoring
            fallback_deal_id = uuid4().hex
            fallback_info = {
                "deal_id": fallback_deal_id,
                "origin": origin,
                "destination": destination,
                "price": actual_price,
                "airline": "Unknown",
                "dates": f"{outbound_date} to {return_date}" if return_date else str(outbound_date),
                "outbound_date": str(outbound_date),
                "return_date": str(return_date) if return_date else "",
                "passengers": route.passengers,
                "booking_url": verification.booking_url,
                "reasoning": f"Community error fare (scoring unavailable). Verified at €{actual_price}.",
            }
            try:
                await self.notifier.send_error_fare_alert(fallback_info)
                if self.telegram_notifier:
                    await self.telegram_notifier.send_error_fare_alert(fallback_info)
            except Exception:
                logger.exception("Failed to send fallback error fare alert")
            # Still store the deal
            deal = Deal(
                deal_id=fallback_deal_id,
                snapshot_id=snapshot.snapshot_id,
                route_id=route.route_id,
                score=Decimal("0.70"),
                urgency="book_now",
                reasoning="Community error fare (scoring unavailable)",
                booking_url=verification.booking_url,
                alert_sent=True,
                alert_sent_at=now,
            )
            await loop.run_in_executor(None, self.db.insert_deal, deal)
            return

        # Store deal record
        booking_url = verification.booking_url
        deal = Deal(
            deal_id=uuid4().hex,
            snapshot_id=snapshot.snapshot_id,
            route_id=route.route_id,
            score=Decimal(str(round(score_result.score, 2))),
            urgency=score_result.urgency,
            reasoning=score_result.reasoning,
            booking_url=booking_url,
        )

        alert_threshold = self.config.scoring.alert_threshold
        if score_result.score >= alert_threshold:
            deal.alert_sent = True
            deal.alert_sent_at = now

            # Extract airline info from best flight
            airline = "Unknown"
            stops = 0
            if best_flight:
                flights_list = best_flight.get("flights", [])
                if flights_list:
                    airline = flights_list[0].get("airline", "Unknown")
                stops = max(0, len(flights_list) - 1) if flights_list else 0

            alert_info = {
                "deal_id": deal.deal_id,
                "origin": origin,
                "destination": destination,
                "price": actual_price,
                "avg_price": f"{float(avg_price):.0f}" if avg_price else "?",
                "airline": airline,
                "stops": stops,
                "dates": f"{outbound_date} to {return_date}" if return_date else str(outbound_date),
                "outbound_date": str(outbound_date),
                "return_date": str(return_date) if return_date else "",
                "passengers": route.passengers,
                "score": score_result.score,
                "urgency": score_result.urgency,
                "reasoning": score_result.reasoning,
                "booking_url": booking_url,
            }

            try:
                await self.notifier.send_error_fare_alert(alert_info)
                if self.telegram_notifier:
                    await self.telegram_notifier.send_error_fare_alert(alert_info)
            except Exception:
                logger.exception("Failed to send error fare alert for %s", route.route_id)
        else:
            logger.info(
                "Community deal scored %.2f (below threshold %.2f), not alerting",
                score_result.score, alert_threshold,
            )

        await loop.run_in_executor(None, self.db.insert_deal, deal)

    @staticmethod
    def _static_fallback(price: float, avg_price: Decimal | None):
        """Fallback scoring when Claude API is unavailable."""
        from src.analysis.scorer import DealScore

        if avg_price is not None and float(avg_price) > 0:
            drop_pct = (float(avg_price) - price) / float(avg_price)
            if drop_pct > DROP_PERCENT_THRESHOLD:
                return DealScore(
                    score=0.80,
                    urgency="book_now",
                    reasoning=f"Static fallback: price is {drop_pct:.0%} below 90-day average (Claude unavailable)",
                    booking_window_hours=48,
                )
        return DealScore(
            score=0.40,
            urgency="watch",
            reasoning="Static fallback: price below average but not exceptional (Claude unavailable)",
            booking_window_hours=72,
        )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config()
    orchestrator = Orchestrator(config)
    await orchestrator.start()


if __name__ == "__main__":
    asyncio.run(main())
