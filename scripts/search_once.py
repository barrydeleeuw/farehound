#!/usr/bin/env python3
"""One-off flight search for testing the SerpAPI → DuckDB pipeline.

Usage:
    python scripts/search_once.py
    python scripts/search_once.py --origin AMS --destination NRT --outbound 2026-10-08 --return 2026-10-22 --passengers 2
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.apis.serpapi import SerpAPIClient, SerpAPIError, FlightSearchResult
from src.config import load_config
from src.storage.db import Database
from src.storage.models import PriceSnapshot, Route

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


def _print_plain(result: FlightSearchResult, origin: str, destination: str,
                 outbound: str, return_dt: str | None, passengers: int,
                 snapshot_count: int) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {origin} -> {destination}")
    print(f"  {outbound}" + (f" to {return_dt}" if return_dt else " (one-way)"))
    print(f"  {passengers} passenger(s)")
    print(f"{'=' * 60}")

    insights = result.price_insights
    lowest = insights.get("lowest_price")
    if lowest is not None:
        print(f"\n  Lowest price: EUR {lowest}")
    else:
        print("\n  No price data returned.")

    level = insights.get("price_level")
    typical = insights.get("typical_price_range")
    if level:
        print(f"  Price level:  {level}")
    if typical and len(typical) == 2:
        print(f"  Typical range: EUR {typical[0]} - {typical[1]}")

    all_flights = result.best_flights + result.other_flights
    if all_flights:
        print(f"\n  Top flights (showing up to 3 of {len(all_flights)}):")
        print(f"  {'Airline':<20} {'Depart':>8} {'Arrive':>8} {'Stops':>5} {'Price':>8}")
        print(f"  {'-' * 52}")
        for flight_group in all_flights[:3]:
            legs = flight_group.get("flights", [])
            price = flight_group.get("price")
            airline = legs[0].get("airline", "?") if legs else "?"
            dep_time = legs[0].get("departure_airport", {}).get("time", "?") if legs else "?"
            arr_time = legs[-1].get("arrival_airport", {}).get("time", "?") if legs else "?"
            stops = len(legs) - 1
            stop_str = "Direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"
            price_str = f"EUR {price}" if price else "N/A"
            print(f"  {airline:<20} {dep_time:>8} {arr_time:>8} {stop_str:>5} {price_str:>8}")
    else:
        print("\n  No flights found.")

    gf_url = result.raw_response.get("search_metadata", {}).get("google_flights_url")
    if gf_url:
        print(f"\n  Google Flights: {gf_url}")

    booking_urls = _extract_booking_urls(result)
    if booking_urls:
        print(f"\n  Booking links:")
        for url in booking_urls[:3]:
            print(f"    {url}")

    print(f"\n  Snapshots stored for this route: {snapshot_count}")
    print()


def _print_rich(result: FlightSearchResult, origin: str, destination: str,
                outbound: str, return_dt: str | None, passengers: int,
                snapshot_count: int) -> None:
    header = f"[bold]{origin} -> {destination}[/bold]\n"
    header += f"{outbound}" + (f" to {return_dt}" if return_dt else " (one-way)")
    header += f"  |  {passengers} passenger(s)"
    console.print(Panel(header, title="FareHound Search", border_style="blue"))

    insights = result.price_insights
    lowest = insights.get("lowest_price")
    level = insights.get("price_level")
    typical = insights.get("typical_price_range")

    if lowest is not None:
        level_color = {"low": "green", "typical": "yellow", "high": "red"}.get(level or "", "white")
        console.print(f"\n  Lowest price: [bold green]EUR {lowest}[/bold green]")
        if level:
            console.print(f"  Price level:  [{level_color}]{level}[/{level_color}]")
        if typical and len(typical) == 2:
            console.print(f"  Typical range: EUR {typical[0]} - {typical[1]}")
    else:
        console.print("\n  [yellow]No price data returned.[/yellow]")

    all_flights = result.best_flights + result.other_flights
    if all_flights:
        table = Table(title=f"Top Flights ({len(all_flights)} total)")
        table.add_column("Airline", style="cyan")
        table.add_column("Depart", justify="right")
        table.add_column("Arrive", justify="right")
        table.add_column("Stops", justify="center")
        table.add_column("Price", justify="right", style="green")

        for flight_group in all_flights[:3]:
            legs = flight_group.get("flights", [])
            price = flight_group.get("price")
            airline = legs[0].get("airline", "?") if legs else "?"
            dep_time = legs[0].get("departure_airport", {}).get("time", "?") if legs else "?"
            arr_time = legs[-1].get("arrival_airport", {}).get("time", "?") if legs else "?"
            stops = len(legs) - 1
            stop_str = "Direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"
            price_str = f"EUR {price}" if price else "N/A"
            table.add_row(airline, dep_time, arr_time, stop_str, price_str)

        console.print()
        console.print(table)

    gf_url = result.raw_response.get("search_metadata", {}).get("google_flights_url")
    if gf_url:
        console.print(f"\n  [dim]Google Flights:[/dim] {gf_url}")

    booking_urls = _extract_booking_urls(result)
    if booking_urls:
        console.print(f"\n  [dim]Booking links:[/dim]")
        for url in booking_urls[:3]:
            console.print(f"    {url}")

    console.print(f"\n  Snapshots stored for this route: [bold]{snapshot_count}[/bold]\n")


def _extract_booking_urls(result: FlightSearchResult) -> list[str]:
    urls = []
    for opt in result.booking_options:
        together = opt.get("together", {})
        url = together.get("booking_request", {}).get("url")
        if url:
            urls.append(url)
    return urls


async def search(origin: str, destination: str, outbound: str,
                 return_dt: str | None, passengers: int,
                 route_id: str, api_key: str, currency: str) -> None:
    client = SerpAPIClient(api_key=api_key, currency=currency)
    trip_type = "round_trip" if return_dt else "one_way"

    result = await client.search_flights(
        origin=origin,
        destination=destination,
        outbound_date=outbound,
        return_date=return_dt,
        passengers=passengers,
        trip_type=trip_type,
    )

    db = Database()
    try:
        db.init_schema()

        # Ensure route exists for FK constraint
        db.upsert_route(Route(
            route_id=route_id,
            origin=origin,
            destination=destination,
            passengers=passengers,
        ))

        insights = result.price_insights
        lowest = insights.get("lowest_price")
        typical_range = insights.get("typical_price_range")

        snapshot = PriceSnapshot(
            snapshot_id=uuid4().hex,
            route_id=route_id,
            observed_at=datetime.now(UTC),
            source="serpapi_poll",
            passengers=passengers,
            outbound_date=date.fromisoformat(outbound),
            return_date=date.fromisoformat(return_dt) if return_dt else None,
            lowest_price=lowest,
            currency=currency,
            best_flight=result.best_flights[0] if result.best_flights else None,
            all_flights=result.best_flights + result.other_flights,
            price_level=insights.get("price_level"),
            typical_low=typical_range[0] if typical_range and len(typical_range) == 2 else None,
            typical_high=typical_range[1] if typical_range and len(typical_range) == 2 else None,
            price_history=insights.get("price_history"),
            search_params=result.search_params,
        )
        db.insert_snapshot(snapshot)

        history = db.get_price_history(route_id)
        snapshot_count = history["count"]
    finally:
        db.close()

    printer = _print_rich if HAS_RICH else _print_plain
    printer(result, origin, destination, outbound, return_dt, passengers, snapshot_count)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a single flight search via SerpAPI and store the result."
    )
    parser.add_argument("--origin", help="Origin IATA code (e.g. AMS)")
    parser.add_argument("--destination", help="Destination IATA code (e.g. NRT)")
    parser.add_argument("--outbound", help="Outbound date (YYYY-MM-DD)")
    parser.add_argument("--return", dest="return_date", help="Return date (YYYY-MM-DD)")
    parser.add_argument("--passengers", type=int, help="Number of passengers")
    args = parser.parse_args()

    # Load config for API key and defaults
    try:
        config = load_config()
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        api_key = config.serpapi.api_key
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Set the SERPAPI_API_KEY environment variable and try again.", file=sys.stderr)
        sys.exit(1)

    currency = config.serpapi.currency

    if args.origin and args.destination and args.outbound:
        origin = args.origin
        destination = args.destination
        outbound = args.outbound
        return_dt = args.return_date
        passengers = args.passengers or 2
        route_id = f"{origin.lower()}-{destination.lower()}-manual"
    elif not args.origin and not args.destination and not args.outbound:
        # Use first route from config
        if not config.routes:
            print("Error: no routes defined in config.yaml", file=sys.stderr)
            sys.exit(1)

        route = config.routes[0]
        origin = route.origin
        destination = route.destination
        passengers = route.passengers

        if route.earliest_departure:
            outbound = str(route.earliest_departure)
        else:
            print("Error: first route has no earliest_departure date.", file=sys.stderr)
            sys.exit(1)

        if route.latest_return and route.trip_type == "round_trip":
            return_dt = str(route.latest_return)
        else:
            return_dt = None

        route_id = route.id
    else:
        print("Error: provide all of --origin, --destination, --outbound, or none to use config.",
              file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(search(origin, destination, outbound, return_dt, passengers,
                           route_id, api_key, currency))
    except SerpAPIError as e:
        print(f"API error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
