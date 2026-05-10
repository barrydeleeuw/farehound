"""Multi-mode transport cost computation and cheapest-mode selection.

R9 ITEM-053: each airport can have multiple transport options (drive, train,
taxi, etc.). At render time we pick the cheapest option for the current party
size and trip duration, unless the user has set a per-airport "always use [mode]"
override.

Pure functions — no I/O, no DB calls. Easy to unit-test.
"""

from __future__ import annotations

# Modes where the cost is per-person (each passenger needs a ticket).
# Drive/taxi/uber are per-vehicle: one cost regardless of how many people fit.
_PER_PERSON_MODES = {
    "train", "thalys", "bus", "metro", "public transport", "ferry", "tram",
}


def is_per_person_mode(mode: str | None) -> bool:
    """True if the cost of this mode scales with passenger count."""
    if not mode:
        return False
    return mode.lower().strip() in _PER_PERSON_MODES


# Fallback assumption when the caller doesn't know trip duration. Used only
# for cheapest-mode COMPARISON (so parking gets fairly considered) — NOT for
# the displayed parking number, which the renderer computes from the actual
# trip_days. R9 review #1: a trip_days=0 fallback would make drive look free
# even when parking would dominate the real cost.
DEFAULT_TRIP_DAYS_FOR_COMPARISON = 7


def compute_mode_total(opt: dict, passengers: int, trip_days: int = 0) -> float:
    """Total round-trip cost for the party using this transport option.

    `opt` is the dict shape returned by Database.get_transport_options():
        {
          "mode": "train",
          "cost_eur": 15.0,                 # one-way
          "cost_scales_with_pax": True,
          "parking_cost_per_day_eur": None,
          ...
        }

    Two cost models, controlled by `cost_scales_with_pax`:

    - **Per-vehicle** (drive, taxi): cost is for the whole party in one
      vehicle. Total = cost × 2 (RT) + parking_per_day × trip_days.
      Assumes party ≤ 4 pax (one car). Above 4 the user should adjust.
    - **Per-person** (train, bus, metro, ferry, tram): cost is one ticket.
      Total = cost × 2 (RT) × passengers. No parking.

    When trip_days == 0 we substitute DEFAULT_TRIP_DAYS_FOR_COMPARISON for the
    parking term — otherwise drive options without an explicit trip duration
    would beat train by ignoring the dominant cost.
    """
    effective_days = trip_days if trip_days > 0 else DEFAULT_TRIP_DAYS_FOR_COMPARISON
    cost = float(opt.get("cost_eur") or 0.0)
    scales = bool(opt.get("cost_scales_with_pax"))
    if scales:
        round_trip = cost * 2 * max(passengers, 1)
    else:
        round_trip = cost * 2
    parking_per_day = float(opt.get("parking_cost_per_day_eur") or 0.0)
    parking = parking_per_day * effective_days
    return round_trip + parking


def pick_cheapest_mode(
    options: list[dict],
    passengers: int,
    trip_days: int = 0,
    *,
    override_mode: str | None = None,
) -> dict | None:
    """Return the cheapest enabled option for the given party + duration.

    If `override_mode` is set and matches an enabled option, that one wins
    regardless of cost — the user explicitly chose convenience over price.

    Returns None if no enabled options exist.
    """
    enabled = [o for o in options if o.get("enabled", True)]
    if not enabled:
        return None
    if override_mode:
        for o in enabled:
            if (o.get("mode") or "").lower() == override_mode.lower():
                return o
        # Override set but the named mode is disabled / missing — fall through
        # to cheapest rather than failing silently.
    return min(enabled, key=lambda o: compute_mode_total(o, passengers, trip_days))


def resolve_breakdown_inputs(
    options: list[dict],
    passengers: int,
    trip_days: int,
    *,
    override_mode: str | None = None,
) -> dict:
    """Resolve the multi-mode option list to a single set of values for the
    cost-breakdown row.

    Returns a dict matching the legacy single-mode shape so the breakdown
    renderer doesn't need to know about multiple modes:
        {
          "mode": "train",
          "transport_cost_eur": 15.0,        # one-way (so transport_total still works)
          "parking_cost_eur": 30.0,          # resolved: per-day × trip_days
          "transport_time_min": 45,
          "is_cheapest": True,
          "override_used": False,
          "no_options": False,
        }
    Returns the no_options shape when the user has no options configured for
    this airport — the renderer shows €0 transport with the existing data gap UX.
    """
    chosen = pick_cheapest_mode(
        options, passengers, trip_days, override_mode=override_mode
    )
    if chosen is None:
        return {
            "mode": "",
            "transport_cost_eur": 0.0,
            "parking_cost_eur": 0.0,
            "transport_time_min": 0,
            "is_cheapest": False,
            "override_used": False,
            "no_options": True,
        }
    parking_per_day = float(chosen.get("parking_cost_per_day_eur") or 0.0)
    parking_total = parking_per_day * max(trip_days, 0)
    override_used = bool(
        override_mode
        and (chosen.get("mode") or "").lower() == override_mode.lower()
    )
    return {
        "mode": chosen.get("mode") or "",
        "transport_cost_eur": float(chosen.get("cost_eur") or 0.0),
        "parking_cost_eur": parking_total,
        "transport_time_min": int(chosen.get("time_min") or 0),
        "is_cheapest": not override_used,
        "override_used": override_used,
        "no_options": False,
    }
