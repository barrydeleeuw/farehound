"""Debug script: run one poll cycle locally and trace nearby airport data flow."""
import asyncio
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

# Load env
load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
logger = logging.getLogger("debug")

async def main():
    from src.config import load_config
    from src.storage.db import Database
    from src.apis.serpapi import SerpAPIClient
    from src.analysis.nearby_airports import compare_airports

    config = load_config()
    db = Database()
    db.init_schema()
    db.seed_airport_transport(config.airports)

    serpapi = SerpAPIClient(
        api_key=os.environ["SERPAPI_API_KEY"],
        currency="EUR",
    )

    # Sync routes
    from src.storage.models import Route as DBRoute
    for r in config.routes:
        db_route = DBRoute(
            route_id=r.id, origin=r.origin, destination=r.destination,
            trip_type=r.trip_type, earliest_departure=r.earliest_departure,
            latest_return=r.latest_return, date_flex_days=r.date_flexibility_days,
            max_stops=r.max_stops, passengers=r.passengers, notes=r.notes,
        )
        db.upsert_route(db_route)

    route = db.get_active_routes()[0]  # First route
    logger.info("Testing route: %s → %s", route.origin, route.destination)

    # Get secondary airports
    secondary_airports = db.get_secondary_airports()
    primary_transport = db.get_airport_transport(route.origin)
    logger.info("Primary: %s (transport: %s)", route.origin, primary_transport)
    logger.info("Secondary airports: %s", [a["airport_code"] for a in secondary_airports])

    # Poll primary
    from datetime import date
    outbound = route.earliest_departure or date(2026, 10, 20)
    return_dt = date(outbound.year, outbound.month + 1, outbound.day) if outbound.month < 12 else date(outbound.year + 1, 1, outbound.day)

    logger.info("Searching primary: %s → %s (%s to %s)", route.origin, route.destination, outbound, return_dt)
    primary_result_raw = await serpapi.search_flights(
        origin=route.origin, destination=route.destination,
        outbound_date=outbound, return_date=return_dt,
        passengers=route.passengers, trip_type=route.trip_type,
    )
    primary_price = primary_result_raw.price_insights.get("lowest_price")
    logger.info("Primary price: €%s", primary_price)

    if not primary_price:
        logger.error("No primary price, can't compare")
        return

    primary_result = {
        "airport_code": route.origin,
        "fare_pp": float(primary_price) / route.passengers,
        "transport_cost": primary_transport.get("transport_cost_eur") or 0,
        "parking_cost": primary_transport.get("parking_cost_eur"),
        "transport_mode": primary_transport.get("transport_mode", ""),
        "transport_time_min": primary_transport.get("transport_time_min", 0),
    }
    logger.info("Primary result: %s", primary_result)

    # Poll secondary airports
    secondary_results = []
    for airport in secondary_airports:
        try:
            logger.info("Searching secondary: %s → %s", airport["airport_code"], route.destination)
            result = await serpapi.search_flights(
                origin=airport["airport_code"], destination=route.destination,
                outbound_date=outbound, return_date=return_dt,
                passengers=route.passengers, trip_type=route.trip_type,
            )
            lowest = result.price_insights.get("lowest_price")
            if lowest is None:
                logger.info("  No price for %s", airport["airport_code"])
                continue
            logger.info("  %s price: €%s", airport["airport_code"], lowest)
            secondary_results.append({
                "airport_code": airport["airport_code"],
                "fare_pp": float(lowest) / route.passengers,
                "transport_cost": airport.get("transport_cost_eur") or 0,
                "parking_cost": airport.get("parking_cost_eur"),
                "transport_mode": airport.get("transport_mode", ""),
                "transport_time_min": airport.get("transport_time_min", 0),
            })
        except Exception as e:
            logger.error("  Error for %s: %s", airport["airport_code"], e)

    logger.info("\n=== COMPARISON ===")
    logger.info("Primary: %s", primary_result)
    logger.info("Secondary results: %d airports", len(secondary_results))

    comparison = compare_airports(primary_result, secondary_results, route.passengers)
    logger.info("Comparison result: %s", comparison)

    if comparison:
        for alt in comparison:
            logger.info(
                "  🟢 %s: €%,.0f/pp → €%,.0f net (save €%,.0f) via %s",
                alt["airport_name"], alt["fare_pp"], alt["net_cost"],
                alt["savings"], alt["transport_mode"],
            )
    else:
        logger.warning("  NO nearby airports with >€75 savings!")

    await serpapi.close()
    db.close()

asyncio.run(main())
