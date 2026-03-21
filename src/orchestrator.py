from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.alerts.homeassistant import HomeAssistantNotifier
from src.apis.serpapi import SerpAPIClient, SerpAPIError, generate_date_windows
from src.config import AppConfig, Route as ConfigRoute, load_config
from src.storage.db import Database
from src.storage.models import PollWindow, PriceSnapshot, Route as DBRoute

logger = logging.getLogger("farehound.orchestrator")

# Default trip duration when computing date windows
DEFAULT_TRIP_DURATION_DAYS = 14
# How often to do a full rescan of all windows (days)
FULL_RESCAN_INTERVAL_DAYS = 7
# Max windows per route for initial scan
DEFAULT_MAX_WINDOWS = 4
# Percentage drop below average that triggers an alert
DROP_PERCENT_THRESHOLD = 0.15


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
        )
        self.scheduler = AsyncIOScheduler()
        self._first_run = True
        self._last_full_rescan: datetime | None = None

    async def start(self) -> None:
        loop = asyncio.get_event_loop()

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
            next_run_time=datetime.now(UTC),  # run immediately on start
        )
        logger.info("Scheduled polling every %d hours", interval_hours)

        # Register signal handlers
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.shutdown(s)))

        self.scheduler.start()
        logger.info("Orchestrator started")

        # Keep running until shutdown
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    async def shutdown(self, sig: signal.Signals | None = None) -> None:
        if sig:
            logger.info("Received signal %s, shutting down", sig.name)
        else:
            logger.info("Shutting down")

        self.scheduler.shutdown(wait=False)
        self.db.close()
        logger.info("Shutdown complete")

        # Cancel all running tasks
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()

    async def poll_routes(self) -> None:
        logger.info("Starting poll cycle")
        loop = asyncio.get_event_loop()

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

    async def _poll_single_route(self, route: DBRoute) -> None:
        loop = asyncio.get_event_loop()
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
        loop = asyncio.get_event_loop()

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
        loop = asyncio.get_event_loop()

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
        loop = asyncio.get_event_loop()

        # Get alert rules for this route
        rules = await loop.run_in_executor(None, self.db.get_alert_rules, route.route_id)

        # Get price history for percentage drop check
        history = await loop.run_in_executor(None, self.db.get_price_history, route.route_id, 90)
        avg_price = history.get("avg_price")

        should_alert = False
        reasons: list[str] = []

        # Check static threshold rules
        for rule in rules:
            if rule.rule_type == "price_below" and rule.threshold is not None:
                if price < float(rule.threshold):
                    should_alert = True
                    reasons.append(f"Price {price} below threshold {rule.threshold}")

        # Check percentage drop below 90-day average
        if avg_price is not None and float(avg_price) > 0:
            drop_pct = (float(avg_price) - price) / float(avg_price)
            if drop_pct > DROP_PERCENT_THRESHOLD:
                should_alert = True
                reasons.append(
                    f"Price {price} is {drop_pct:.0%} below 90-day avg {float(avg_price):.0f}"
                )

        if not should_alert:
            return

        logger.info("Alert triggered for %s: %s", route.route_id, "; ".join(reasons))

        # Build alert info
        airline = "Unknown"
        stops = 0
        if best_flight:
            flights = best_flight.get("flights", [])
            if flights:
                airline = flights[0].get("airline", "Unknown")
            stops = max(0, len(flights) - 1) if flights else 0

        deal_info = {
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
        }

        try:
            await self.notifier.send_deal_alert(deal_info)
        except Exception:
            logger.exception("Failed to send alert for route %s", route.route_id)


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
