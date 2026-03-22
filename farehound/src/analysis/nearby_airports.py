from __future__ import annotations

from src.utils.airports import airport_name

# Transport modes where cost is per person (each passenger needs a ticket)
_PER_PERSON_MODES = {"train", "thalys", "bus", "metro", "public transport", "ferry", "tram"}


def is_per_person_transport(mode: str) -> bool:
    return mode.lower().strip() in _PER_PERSON_MODES


def transport_total(transport_cost: float, mode: str, passengers: int) -> float:
    """Total round-trip transport cost accounting for per-person vs per-vehicle modes."""
    one_way = transport_cost * passengers if is_per_person_transport(mode) else transport_cost
    return one_way * 2  # round trip


def calculate_net_cost(
    fare_pp: float,
    passengers: int,
    transport_cost: float,
    parking_cost: float | None,
    transport_mode: str = "",
) -> float:
    """Total trip cost: fare x passengers + transport (round-trip, per-person if applicable) + parking."""
    return (fare_pp * passengers) + transport_total(transport_cost, transport_mode, passengers) + (parking_cost or 0)


def compare_airports(
    primary_result: dict,
    secondary_results: list[dict],
    passengers: int,
    savings_threshold: float = 75.0,
) -> list[dict]:
    """Compare secondary airports against primary.

    primary_result: {airport_code, fare_pp, transport_cost, parking_cost, transport_mode, transport_time_min}
    secondary_results: list of same shape

    Returns list sorted by savings (best first), only including airports with > threshold net savings.
    Each entry: {airport_code, airport_name, fare_pp, net_cost, savings, transport_mode, transport_cost, transport_time_min}
    """
    primary_net = calculate_net_cost(
        fare_pp=primary_result["fare_pp"],
        passengers=passengers,
        transport_cost=primary_result["transport_cost"],
        parking_cost=primary_result.get("parking_cost"),
        transport_mode=primary_result.get("transport_mode", ""),
    )

    comparisons = []
    for sec in secondary_results:
        sec_net = calculate_net_cost(
            fare_pp=sec["fare_pp"],
            passengers=passengers,
            transport_cost=sec["transport_cost"],
            parking_cost=sec.get("parking_cost"),
            transport_mode=sec.get("transport_mode", ""),
        )
        savings = primary_net - sec_net
        if savings > savings_threshold:
            comparisons.append({
                "airport_code": sec["airport_code"],
                "airport_name": airport_name(sec["airport_code"]),
                "fare_pp": sec["fare_pp"],
                "net_cost": sec_net,
                "savings": savings,
                "transport_mode": sec.get("transport_mode", ""),
                "transport_cost": sec["transport_cost"],
                "parking_cost": sec.get("parking_cost") or 0,
                "transport_time_min": sec.get("transport_time_min", 0),
            })

    comparisons.sort(key=lambda x: x["savings"], reverse=True)
    return comparisons
