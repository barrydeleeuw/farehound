from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.alerts.telegram import TelegramNotifier
from src.analysis.nearby_airports import compare_airports
from src.analysis.scorer import DealScorer
from src.apis.community import CommunityListener, RSSListener
from src.apis.community import CommunityFeedConfig as CommunityFeed
from src.apis.serpapi import SerpAPIClient, SerpAPIError, VerificationResult, generate_date_windows
from src.bot.commands import TripBot
from src.config import AppConfig, Route as ConfigRoute, load_config
from src.storage.db import Database
from src.storage.models import Deal, PollWindow, PriceSnapshot, Route as DBRoute
from src.utils.airlines import airline_name

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


def _generate_weekend_windows(
    earliest_departure: date,
    latest_return: date,
    trip_duration_days: int,
    preferred_departure_days: list[int],
    preferred_return_days: list[int],
    max_windows: int = 4,
) -> list[tuple[date, date]]:
    """Generate weekend-specific date windows within a travel range.

    Finds departure dates that fall on preferred_departure_days (e.g. Thu/Fri)
    and return dates on preferred_return_days (e.g. Sun/Mon), spaced throughout
    the date range.
    """
    all_candidates: list[tuple[date, date]] = []
    dep_set = set(preferred_departure_days)

    current = earliest_departure
    while current <= latest_return - timedelta(days=trip_duration_days):
        if current.weekday() in dep_set:
            ret = current + timedelta(days=trip_duration_days)
            if ret <= latest_return:
                all_candidates.append((current, ret))
        current += timedelta(days=1)

    if not all_candidates:
        raise ValueError(
            f"No weekend windows fit in {earliest_departure} to {latest_return} "
            f"with {trip_duration_days}-day duration"
        )

    if len(all_candidates) <= max_windows:
        return all_candidates

    # Evenly space across candidates
    step = (len(all_candidates) - 1) / (max_windows - 1)
    return [all_candidates[round(step * i)] for i in range(max_windows)]


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
        self.scorer = DealScorer(
            api_key=config.anthropic.api_key,
            model=config.anthropic.model,
        )
        # Telegram notifier (default chat_id from config, per-call chat_id for multi-user)
        self.telegram_notifier: TelegramNotifier | None = None
        if config.telegram_alerts is not None and config.telegram_alerts.enabled:
            self.telegram_notifier = TelegramNotifier(
                bot_token=config.telegram_alerts.bot_token,
            )

        self.scheduler = AsyncIOScheduler()
        self._first_run = True
        self._last_full_rescan: datetime | None = None
        self._secondary_poll_counter: int = 0
        self._latest_nearby_comparison: dict[str, list[dict]] = {}

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
            try:
                self.community_listener = CommunityListener(
                    api_id=config.telegram.api_id,
                    api_hash=config.telegram.api_hash,
                    feeds=feeds,
                )
            except Exception as e:
                logger.warning("Could not init Telegram community listener: %s", e)

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

    async def _ensure_default_user(self) -> None:
        """Create Barry as default user if no users exist, seeding from config."""
        loop = asyncio.get_running_loop()
        users = await loop.run_in_executor(None, self.db.get_all_active_users)

        if not users:
            # Fresh DB — create default user from config
            chat_id = self.config.telegram_alerts.chat_id if self.config.telegram_alerts else "default"
            user_id = await loop.run_in_executor(
                None, self.db.create_user, chat_id, self.config.traveller.name
            )
            await loop.run_in_executor(
                None, lambda: self.db.update_user(
                    user_id,
                    home_airport=self.config.traveller.home_airport,
                    onboarded=1,
                )
            )
            # Seed airports
            if self.config.airports:
                await loop.run_in_executor(
                    None, self.db.seed_airport_transport, self.config.airports, user_id
                )
            # Migrate config routes to this user
            for route_cfg in self.config.routes:
                db_route = _config_route_to_db(route_cfg)
                await loop.run_in_executor(None, self.db.upsert_route, db_route, user_id)
            logger.info(
                "Created default user '%s' with %d routes",
                self.config.traveller.name, len(self.config.routes),
            )
            return

        # Fix migration-created user with placeholder chat_id="default"
        default_user = await loop.run_in_executor(None, self.db.get_user_by_chat_id, "default")
        if default_user and self.config.telegram_alerts:
            user_id = default_user["user_id"]
            await loop.run_in_executor(
                None, lambda: self.db.update_user(
                    user_id,
                    name=self.config.traveller.name,
                    home_airport=self.config.traveller.home_airport,
                )
            )
            # Update chat_id directly (update_user doesn't allow chat_id changes)
            real_chat_id = self.config.telegram_alerts.chat_id

            def _fix_chat_id():
                self.db._conn.execute(
                    "UPDATE users SET telegram_chat_id = ? WHERE user_id = ?",
                    [real_chat_id, user_id],
                )
                self.db._conn.commit()

            await loop.run_in_executor(None, _fix_chat_id)
            # Seed airports if config has them
            if self.config.airports:
                await loop.run_in_executor(
                    None, self.db.seed_airport_transport, self.config.airports, user_id
                )
            logger.info("Updated default user '%s' with config data", self.config.traveller.name)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        # Init DB schema (sync)
        await loop.run_in_executor(None, self.db.init_schema)
        logger.info("Database schema initialized")

        # Bootstrap default user from config (if no users exist)
        await self._ensure_default_user()

        # Schedule polling job
        interval_hours = self.config.scoring.poll_interval_hours
        self.scheduler.add_job(
            self.poll_routes,
            "interval",
            hours=interval_hours,
            id="poll_routes",
            misfire_grace_time=60,
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

        # Schedule pending feedback follow-ups (hourly)
        self.scheduler.add_job(
            self._check_pending_feedback,
            "interval",
            hours=1,
            id="check_pending_feedback",
            misfire_grace_time=60,
        )
        logger.info("Scheduled pending feedback check every hour")

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

    async def _check_pending_feedback(self) -> None:
        """Send follow-up messages for deals alerted 3+ days ago with no feedback."""
        if self.telegram_notifier is None:
            return
        loop = asyncio.get_running_loop()
        pending = await loop.run_in_executor(None, self.db.get_deals_pending_feedback)
        if not pending:
            return

        # Build route_id -> user mapping for per-user follow-ups
        users = await loop.run_in_executor(None, self.db.get_all_active_users)
        route_user_map: dict[str, dict] = {}
        for user in users:
            routes = await loop.run_in_executor(None, self.db.get_active_routes, user["user_id"])
            for route in routes:
                route_user_map[route.route_id] = user

        logger.info("Sending follow-up for %d deals with no feedback", len(pending))
        for deal in pending:
            user = route_user_map.get(deal.get("route_id"))
            chat_id = user["telegram_chat_id"] if user else None
            try:
                await self.telegram_notifier.send_follow_up(deal, chat_id=chat_id)
            except Exception:
                logger.exception("Failed to send follow-up for deal %s", deal.get("deal_id"))

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

    # --- Window generation ---

    def _generate_windows_for_route(self, route: DBRoute) -> list[tuple[date, date]]:
        """Generate date windows for a route. Returns empty list on error."""
        if not route.earliest_departure or not route.latest_return:
            logger.warning("Route %s missing date range, skipping", route.route_id)
            return []

        trip_duration = route.trip_duration_days or DEFAULT_TRIP_DURATION_DAYS
        try:
            if route.trip_duration_type == "weekend":
                return _generate_weekend_windows(
                    earliest_departure=route.earliest_departure,
                    latest_return=route.latest_return,
                    trip_duration_days=trip_duration,
                    preferred_departure_days=route.preferred_departure_days or [3, 4],
                    preferred_return_days=route.preferred_return_days or [0, 6],
                    max_windows=DEFAULT_MAX_WINDOWS,
                )
            else:
                return generate_date_windows(
                    earliest_departure=route.earliest_departure,
                    latest_return=route.latest_return,
                    trip_duration_days=trip_duration,
                    max_windows=DEFAULT_MAX_WINDOWS,
                )
        except ValueError as e:
            logger.error("Cannot generate windows for route %s: %s", route.route_id, e)
            return []

    # --- Polling ---

    async def poll_routes(self) -> None:
        """Poll all active users' routes with shared SerpAPI calls."""
        logger.info("Starting poll cycle")
        self._cycle_best_prices: dict[str, float] = {}
        loop = asyncio.get_running_loop()

        users = await loop.run_in_executor(None, self.db.get_all_active_users)
        if not users:
            logger.warning("No active users to poll")
            return

        # Phase 1: Collect all search requests, dedup by search key
        # Key: (origin, dest, outbound, return_dt, passengers, trip_type)
        search_requests: dict[tuple, list[tuple[dict, DBRoute]]] = {}
        user_route_windows: list[tuple[dict, DBRoute, list[tuple[date, date]]]] = []

        for user in users:
            routes = await loop.run_in_executor(None, self.db.get_active_routes, user["user_id"])
            for route in routes:
                windows = self._generate_windows_for_route(route)
                if not windows:
                    continue
                windows_to_poll = await self._select_windows(route, windows)
                user_route_windows.append((user, route, windows_to_poll))
                for outbound, return_dt in windows_to_poll:
                    key = (route.origin, route.destination, outbound, return_dt,
                           route.passengers, route.trip_type)
                    search_requests.setdefault(key, []).append((user, route))

        logger.info(
            "Polling %d unique searches across %d users",
            len(search_requests), len(users),
        )

        # Phase 2: Execute each unique search once, distribute results
        for key, user_routes in search_requests.items():
            origin, dest, outbound, return_dt, passengers, trip_type = key
            try:
                result = await self.serpapi.search_flights(
                    origin=origin,
                    destination=dest,
                    outbound_date=outbound,
                    return_date=return_dt,
                    passengers=passengers,
                    trip_type=trip_type,
                )
            except SerpAPIError as e:
                logger.error(
                    "SerpAPI error for %s→%s on %s: %s", origin, dest, outbound, e,
                )
                continue
            except Exception as e:
                logger.error(
                    "Error polling %s→%s on %s: %s", origin, dest, outbound, e,
                    exc_info=True,
                )
                continue

            # Store result for each user watching this search
            for user, route in user_routes:
                try:
                    await self._store_result_for_user(route, result, outbound, return_dt, user)
                except Exception as e:
                    logger.error(
                        "Failed storing result for route %s: %s",
                        route.route_id, e, exc_info=True,
                    )

        self._first_run = False
        self._secondary_poll_counter += 1
        logger.info("Poll cycle complete (secondary counter: %d)", self._secondary_poll_counter)

        # Phase 3: Secondary airports per user (every other cycle)
        if self._secondary_poll_counter % 2 == 0:
            for user, route, windows in user_route_windows:
                try:
                    await self._poll_secondary_airports(route, windows, user)
                except Exception as e:
                    logger.error(
                        "Secondary polling failed for %s: %s",
                        route.route_id, e, exc_info=True,
                    )

        # Update HA sensors
        all_routes = [r for _, r, _ in user_route_windows]
        if all_routes:
            await self._update_ha_sensors(all_routes)

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
                logger.info("Updated %d HA sensors", len(summaries))
            except Exception:
                logger.exception("Failed to update HA sensors")

    async def _store_result_for_user(
        self,
        route: DBRoute,
        result,
        outbound: date,
        return_dt: date,
        user: dict,
    ) -> None:
        """Store a SerpAPI result as snapshot for a specific user's route and check alerts."""
        loop = asyncio.get_running_loop()
        user_id = user["user_id"]

        now = datetime.now(UTC)
        insights = result.price_insights
        lowest_price = insights.get("lowest_price")
        typical_range = insights.get("typical_price_range", [])
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
            search_params={
                **(result.search_params or {}),
                "google_flights_url": result.raw_response.get("search_metadata", {}).get("google_flights_url", ""),
            },
        )

        await loop.run_in_executor(None, self.db.insert_snapshot, snapshot, user_id)
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
            await self._check_alerts(route, snapshot, float(lowest_price), best_flight, user)

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

    async def _poll_secondary_airports(
        self, route: DBRoute, windows: list[tuple[date, date]], user: dict
    ) -> None:
        loop = asyncio.get_running_loop()
        user_id = user["user_id"]
        secondary_airports = await loop.run_in_executor(
            None, self.db.get_secondary_airports, user_id
        )
        if not secondary_airports:
            return

        primary_transport = await loop.run_in_executor(
            None, self.db.get_airport_transport, route.origin, user_id
        )
        if not primary_transport:
            logger.warning("No transport data for primary airport %s", route.origin)
            return

        logger.info(
            "Polling %d secondary airports for route %s",
            len(secondary_airports), route.route_id,
        )

        for outbound, return_dt in windows:
            # Get primary snapshot for this window (just stored above)
            primary_snapshot = await loop.run_in_executor(
                None, self.db.get_latest_snapshot, route.route_id, user_id
            )
            if not primary_snapshot or primary_snapshot.lowest_price is None:
                continue

            primary_result = {
                "airport_code": route.origin,
                "fare_pp": float(primary_snapshot.lowest_price) / route.passengers,
                "transport_cost": primary_transport["transport_cost_eur"] or 0,
                "parking_cost": primary_transport.get("parking_cost_eur"),
                "transport_mode": primary_transport.get("transport_mode", ""),
                "transport_time_min": primary_transport.get("transport_time_min", 0),
            }

            secondary_results = []
            for airport in secondary_airports:
                try:
                    result = await self.serpapi.search_flights(
                        origin=airport["airport_code"],
                        destination=route.destination,
                        outbound_date=outbound,
                        return_date=return_dt,
                        passengers=route.passengers,
                        trip_type=route.trip_type,
                    )

                    insights = result.price_insights
                    lowest = insights.get("lowest_price")
                    if lowest is None:
                        continue

                    # Store snapshot with different origin marker in search_params
                    now = datetime.now(UTC)
                    best_flight = result.best_flights[0] if result.best_flights else None
                    typical_range = insights.get("typical_price_range", [])

                    snapshot = PriceSnapshot(
                        snapshot_id=uuid4().hex,
                        route_id=route.route_id,
                        observed_at=now,
                        source="serpapi_poll",
                        passengers=route.passengers,
                        outbound_date=outbound,
                        return_date=return_dt,
                        lowest_price=Decimal(str(lowest)),
                        currency=self.config.serpapi.currency,
                        best_flight=best_flight,
                        all_flights=result.best_flights + result.other_flights,
                        price_level=insights.get("price_level"),
                        typical_low=Decimal(str(typical_range[0])) if len(typical_range) > 0 else None,
                        typical_high=Decimal(str(typical_range[1])) if len(typical_range) > 1 else None,
                        price_history=insights.get("price_history"),
                        search_params={
                            **(result.search_params or {}),
                            "origin": airport["airport_code"],
                        },
                    )
                    await loop.run_in_executor(None, self.db.insert_snapshot, snapshot, user_id)
                    logger.info(
                        "Stored secondary snapshot %s→%s: €%s",
                        airport["airport_code"], route.destination, lowest,
                    )

                    secondary_results.append({
                        "airport_code": airport["airport_code"],
                        "fare_pp": float(lowest) / route.passengers,
                        "transport_cost": airport.get("transport_cost_eur") or 0,
                        "parking_cost": airport.get("parking_cost_eur"),
                        "transport_mode": airport.get("transport_mode", ""),
                        "transport_time_min": airport.get("transport_time_min", 0),
                    })

                except SerpAPIError as e:
                    logger.error(
                        "SerpAPI error for secondary %s→%s: %s",
                        airport["airport_code"], route.destination, e,
                    )
                except Exception as e:
                    logger.error(
                        "Error polling secondary %s→%s: %s",
                        airport["airport_code"], route.destination, e,
                        exc_info=True,
                    )

            # Store comparison for use in alerts (attached to route context)
            if secondary_results:
                comparison = compare_airports(primary_result, secondary_results, route.passengers)
                if comparison:
                    self._latest_nearby_comparison[route.route_id] = comparison
                    logger.info(
                        "Route %s: best nearby saving €%.0f via %s",
                        route.route_id,
                        comparison[0]["savings"],
                        comparison[0]["airport_name"],
                    )

    async def _check_alerts(
        self,
        route: DBRoute,
        snapshot: PriceSnapshot,
        price: float,
        best_flight: dict | None,
        user: dict,
    ) -> None:
        loop = asyncio.get_running_loop()
        user_id = user["user_id"]
        chat_id = user["telegram_chat_id"]

        # Get price history for pre-filter and scoring context
        history = await loop.run_in_executor(
            None, self.db.get_price_history, route.route_id, 90, user_id
        )
        avg_price = history.get("avg_price")
        sample_count = history.get("count", 0)

        # Pre-filter: only score with Claude if price looks interesting
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

        # Score with Claude using per-user traveller info
        score_result = None
        try:
            score_result = await self.scorer.score_deal(
                snapshot=snapshot,
                route=route,
                price_history=history,
                traveller_name=user.get("name") or self.config.traveller.name,
                home_airport=user.get("home_airport") or self.config.traveller.home_airport,
                traveller_preferences=user.get("preferences") or self.config.traveller.preferences or None,
                past_feedback=feedback or None,
                nearby_comparison=self._latest_nearby_comparison.get(route.route_id),
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
                None, self.db.get_last_alerted_price, route.route_id, user_id
            )

            # Also check in-cycle tracker
            cycle_best = self._cycle_best_prices.get(route.route_id)
            effective_last = last_alerted
            if cycle_best is not None:
                if effective_last is None or cycle_best < effective_last:
                    effective_last = cycle_best

            # Rule 1: New low
            is_new_low = effective_last is None or price < effective_last

            # Rule 2: Book now + low
            is_book_now_and_low = (
                score_result.urgency == "book_now"
                and snapshot.price_level == "low"
                and (effective_last is None or price <= effective_last)
            )

            # Rule 3: Inflection detection
            inflection, bottom_price = await loop.run_in_executor(
                None, self.db.detect_price_inflection, route.route_id, user_id
            )
            if inflection and bottom_price is not None:
                inflection_msg = (
                    f"Price bottomed out at €{bottom_price:,.0f}"
                    " — book now before it rises further."
                )

            should_alert = is_new_low or is_book_now_and_low or inflection

            if should_alert:
                deal.alert_sent = True
                deal.alert_sent_at = now
                # Track best alerted price this cycle
                prev = self._cycle_best_prices.get(route.route_id)
                if prev is None or price < prev:
                    self._cycle_best_prices[route.route_id] = price
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

        await loop.run_in_executor(None, self.db.insert_deal, deal, user_id)

        # Send alert for deals that passed dedup
        if deal.alert_sent:
            airline_code = ""
            stops = 0
            flight_data = best_flight or (snapshot.all_flights[0] if snapshot.all_flights else None)
            if flight_data:
                legs = flight_data.get("flights", [])
                if legs:
                    airline_code = legs[0].get("airline", "")
                    stops = max(0, len(legs) - 1)
                elif flight_data.get("airline"):
                    airline_code = flight_data["airline"]
            airline = airline_name(airline_code) if airline_code else "Unknown"

            # Extract Google Flights URL from snapshot search_params
            gf_url = ""
            if snapshot.search_params and isinstance(snapshot.search_params, dict):
                gf_url = snapshot.search_params.get("google_flights_url", "")

            # Get primary airport transport for cost breakdown
            primary_transport = await loop.run_in_executor(
                None, self.db.get_airport_transport, route.origin, user_id
            )
            primary_t_cost = primary_transport.get("transport_cost_eur", 0) if primary_transport else 0
            primary_parking = primary_transport.get("parking_cost_eur") if primary_transport else None
            primary_mode = primary_transport.get("transport_mode", "") if primary_transport else ""

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
                "nearby_comparison": self._latest_nearby_comparison.get(route.route_id, []),
                "google_flights_url": gf_url,
                "primary_transport_cost": primary_t_cost,
                "primary_parking_cost": primary_parking or 0,
                "primary_transport_mode": primary_mode,
            }

            try:
                if self.telegram_notifier:
                    await self.telegram_notifier.send_deal_alert(deal_info, chat_id=chat_id)
            except Exception:
                logger.exception("Failed to send alert for route %s", route.route_id)

    async def send_daily_digest(self) -> None:
        """Send daily digest per user with their routes."""
        logger.info("Preparing daily digest")
        loop = asyncio.get_running_loop()

        users = await loop.run_in_executor(None, self.db.get_all_active_users)
        if not users:
            logger.info("No active users, skipping digest")
            return

        for user in users:
            user_id = user["user_id"]
            chat_id = user["telegram_chat_id"]

            routes: list[DBRoute] = await loop.run_in_executor(
                None, self.db.get_active_routes, user_id
            )
            if not routes:
                continue

            since = datetime.now(UTC) - timedelta(days=1)
            summaries: list[dict] = []

            for route in routes:
                latest = await loop.run_in_executor(
                    None, self.db.get_latest_snapshot, route.route_id, user_id
                )
                if latest is None:
                    continue

                # 7-day trend
                history_7d = await loop.run_in_executor(
                    None, self.db.get_price_history, route.route_id, 7, user_id
                )
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
                recent_deals = await loop.run_in_executor(
                    None, self.db.get_deals_since, route.route_id, since, user_id
                )
                watch_deals = [d for d in recent_deals if d.urgency == "watch"]

                # Use cheapest recent snapshot for dates
                cheapest = await loop.run_in_executor(
                    None, self.db.get_cheapest_recent_snapshot, route.route_id, 7, user_id
                )
                best = cheapest or latest

                summary: dict = {
                    "origin": route.origin,
                    "destination": route.destination,
                    "lowest_price": float(best.lowest_price) if best and best.lowest_price else None,
                    "trend": trend,
                    "passengers": route.passengers,
                    "dates": f"{best.outbound_date} to {best.return_date}" if best and best.outbound_date else "",
                    "outbound_date": str(best.outbound_date) if best and best.outbound_date else "",
                    "return_date": str(best.return_date) if best and best.return_date else "",
                }

                if watch_deals:
                    summary["watch_deals"] = len(watch_deals)

                # Add nearby airport comparison for digest
                nearby = self._latest_nearby_comparison.get(route.route_id)
                if nearby:
                    summary["nearby_prices"] = nearby

                summaries.append(summary)

            if not summaries:
                continue

            try:
                if self.telegram_notifier:
                    await self.telegram_notifier.send_daily_digest(summaries, chat_id=chat_id)
            except Exception:
                logger.exception("Failed to send daily digest to user %s", user_id)

    async def on_community_deal(self, deal_info: dict) -> None:
        """Handle a deal detected from community channels (Layer 2).

        1. Match against active routes across all users
        2. Verify fare via SerpAPI (once)
        3. Score with Claude per user (community_flagged=True)
        4. Send error fare alert to each matching user
        """
        loop = asyncio.get_running_loop()
        origin = (deal_info.get("origin") or "").upper()
        destination = (deal_info.get("destination") or "").upper()
        community_price = deal_info.get("price")

        if not origin or not destination:
            logger.debug("Community deal missing origin/destination, skipping")
            return

        # Find matching user/route pairs across all users
        users = await loop.run_in_executor(None, self.db.get_all_active_users)
        matches: list[tuple[dict, DBRoute]] = []
        for user in users:
            routes = await loop.run_in_executor(None, self.db.get_active_routes, user["user_id"])
            for r in routes:
                if r.origin.upper() == origin and r.destination.upper() == destination:
                    matches.append((user, r))
                    break  # One route per user per origin/dest

        if not matches:
            logger.debug(
                "Community deal %s → %s does not match any active route", origin, destination,
            )
            return

        # Use first match as reference for verification
        _, ref_route = matches[0]
        logger.info(
            "Community deal matches %d user(s) for %s → %s (community price: %s)",
            len(matches), origin, destination, community_price,
        )

        # --- Pre-filter 1: Date window check ---
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

        if outbound_date is not None and ref_route.earliest_departure and ref_route.latest_return:
            if outbound_date < ref_route.earliest_departure or outbound_date > ref_route.latest_return:
                logger.debug(
                    "Community deal date %s outside route window %s–%s, skipping",
                    outbound_date, ref_route.earliest_departure, ref_route.latest_return,
                )
                return

        # Fall back to route dates if community deal didn't specify
        if outbound_date is None and ref_route.earliest_departure:
            outbound_date = ref_route.earliest_departure
            return_date = ref_route.latest_return

        if outbound_date is None:
            logger.warning("No dates available for verification of community deal, skipping")
            return

        # --- Pre-filter 2: Price sanity check ---
        if community_price is not None:
            history = await loop.run_in_executor(
                None, self.db.get_price_history, ref_route.route_id, 90
            )
            avg_price_precheck = history.get("avg_price")
            if avg_price_precheck is not None and community_price > float(avg_price_precheck):
                logger.debug(
                    "Community price €%.0f above 90-day avg €%.0f for %s, skipping",
                    community_price, float(avg_price_precheck), ref_route.route_id,
                )
                return

        # Verify fare via SerpAPI (once for all users)
        try:
            verification = await self.serpapi.verify_fare(
                origin=origin,
                destination=destination,
                outbound_date=outbound_date,
                return_date=return_date,
                expected_price=community_price or 0,
                passengers=ref_route.passengers,
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

        # Process for each matching user
        for user, route in matches:
            await self._process_community_deal_for_user(
                user, route, verification, actual_price,
                outbound_date, return_date, community_price,
            )

    async def _process_community_deal_for_user(
        self,
        user: dict,
        route: DBRoute,
        verification: VerificationResult,
        actual_price: float,
        outbound_date: date,
        return_date: date | None,
        community_price: float | None,
    ) -> None:
        """Store and score a verified community deal for a specific user."""
        loop = asyncio.get_running_loop()
        user_id = user["user_id"]
        chat_id = user["telegram_chat_id"]

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

        await loop.run_in_executor(None, self.db.insert_snapshot, snapshot, user_id)
        logger.info(
            "Stored verification snapshot for %s (user %s): €%s",
            route.route_id, user_id, actual_price,
        )

        # --- Pre-filter 3: Only score if price looks genuinely good ---
        history = await loop.run_in_executor(
            None, self.db.get_price_history, route.route_id, 90, user_id
        )
        avg_price = history.get("avg_price")
        snapshot_count = history.get("count", 0)

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
                traveller_name=user.get("name") or self.config.traveller.name,
                home_airport=user.get("home_airport") or self.config.traveller.home_airport,
                traveller_preferences=user.get("preferences") or self.config.traveller.preferences or None,
                past_feedback=community_feedback or None,
            )
            logger.info(
                "Community deal scored: %.2f (%s) — %s",
                score_result.score, score_result.urgency, score_result.reasoning,
            )
        except Exception:
            logger.exception("Claude scoring failed for community deal, sending price-only alert")
            # Fallback: send price-only alert
            fallback_deal_id = uuid4().hex
            fallback_airline_code = ""
            if verification.flights:
                fb_legs = verification.flights[0].get("flights", [])
                if fb_legs:
                    fallback_airline_code = fb_legs[0].get("airline", "")
            fallback_info = {
                "deal_id": fallback_deal_id,
                "origin": route.origin,
                "destination": route.destination,
                "price": actual_price,
                "airline": airline_name(fallback_airline_code) if fallback_airline_code else "Unknown",
                "dates": f"{outbound_date} to {return_date}" if return_date else str(outbound_date),
                "outbound_date": str(outbound_date),
                "return_date": str(return_date) if return_date else "",
                "passengers": route.passengers,
                "booking_url": verification.booking_url,
                "reasoning": f"Community error fare (scoring unavailable). Verified at €{float(actual_price):,.0f}.",
            }
            try:
                if self.telegram_notifier:
                    await self.telegram_notifier.send_error_fare_alert(fallback_info, chat_id=chat_id)
            except Exception:
                logger.exception("Failed to send fallback error fare alert")
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
            await loop.run_in_executor(None, self.db.insert_deal, deal, user_id)
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
            airline_code = ""
            stops = 0
            best_flight = verification.flights[0] if verification.flights else None
            if best_flight:
                flights_list = best_flight.get("flights", [])
                if flights_list:
                    airline_code = flights_list[0].get("airline", "")
                stops = max(0, len(flights_list) - 1) if flights_list else 0
            airline = airline_name(airline_code) if airline_code else "Unknown"

            alert_info = {
                "deal_id": deal.deal_id,
                "origin": route.origin,
                "destination": route.destination,
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
                if self.telegram_notifier:
                    await self.telegram_notifier.send_error_fare_alert(alert_info, chat_id=chat_id)
            except Exception:
                logger.exception("Failed to send error fare alert for %s", route.route_id)
        else:
            logger.info(
                "Community deal scored %.2f (below threshold %.2f), not alerting",
                score_result.score, alert_threshold,
            )

        await loop.run_in_executor(None, self.db.insert_deal, deal, user_id)

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
