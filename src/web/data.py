"""Data assembly for Mini Web App template rendering.

Each `assemble_*` function takes a Database handle (and what it needs from the
user/route context) and returns a plain dict ready for Jinja. Pre-formatted
strings (`price_pp_display`, `dates.label`, etc.) live in the dict so templates
stay logic-free.

These functions never raise on missing data — when a field can't be computed,
the dict key is set to None and the template falls back via Jinja's default
filters.
"""

from __future__ import annotations

import json
import logging
from datetime import date as date_type
from datetime import UTC, datetime
from typing import Any

from src.analysis.nearby_airports import transport_total
from src.storage.db import Database
from src.storage.models import PriceSnapshot, Route
from src.utils.airports import airport_name

logger = logging.getLogger("farehound.web.data")


# ---------- formatting helpers ----------


def _fmt_eur(amount: float | int | None) -> str:
    if amount is None:
        return "—"
    try:
        return f"{float(amount):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_date(d: str | date_type | None, fmt: str = "%-d %b") -> str:
    if d is None or d == "":
        return ""
    if isinstance(d, str):
        try:
            d = date_type.fromisoformat(d[:10])
        except ValueError:
            return d
    return d.strftime(fmt)


def _date_range_label(outbound: str | None, return_date: str | None) -> str:
    o = _fmt_date(outbound)
    r = _fmt_date(return_date)
    if o and r:
        return f"{o} → {r}"
    return o or r or ""


# ---------- deal page ----------


def _get_deal_by_id(db: Database, deal_id: str, user_id: str | None = None) -> dict | None:
    """Fetch a single deal row + its snapshot."""
    sql = (
        "SELECT d.deal_id, d.snapshot_id, d.route_id, d.score, d.urgency, "
        "d.reasoning, d.reasoning_json, d.feedback, d.alert_sent_at, d.booking_url "
        "FROM deals d WHERE d.deal_id = ?"
    )
    params: list = [deal_id]
    if user_id:
        sql += " AND d.user_id = ?"
        params.append(user_id)
    row = db._conn.execute(sql, params).fetchone()
    if not row:
        return None
    cols = ["deal_id", "snapshot_id", "route_id", "score", "urgency",
            "reasoning", "reasoning_json", "feedback", "alert_sent_at", "booking_url"]
    return dict(zip(cols, row))


def _get_route(db: Database, route_id: str, user_id: str | None = None) -> Route | None:
    sql = "SELECT * FROM routes WHERE route_id = ?"
    params: list = [route_id]
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    cursor = db._conn.execute(sql, params)
    row = cursor.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cursor.description]
    return Route.from_row(row, cols)


def _parse_reasoning_json(raw: str | None) -> list[dict] | None:
    """Return the structured 3-bullet reasoning, or None for legacy free-text."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    bullets = []
    for key in ("dates", "range", "nearby"):
        v = data.get(key)
        if isinstance(v, dict) and v.get("headline"):
            bullets.append({"headline": v["headline"], "detail": v.get("detail") or ""})
        elif isinstance(v, str) and v:
            bullets.append({"headline": v, "detail": ""})
    return bullets or None


def _build_deterministic_reasoning(
    snapshot,
    last_alerted: float | None,
    passengers: int,
    price_history_dict: dict | None,
    nearby_count: int = 0,
) -> list[dict]:
    """Build the 'why this is the best' bullets from snapshot data — deterministic only.

    Replaces the LLM-generated reasoning. Each bullet's numbers come from code,
    not from a free-text Claude response, so they always match the page's other
    numbers. Returns a list of `{headline, detail}` dicts (max 4).
    """
    bullets: list[dict] = []
    pax = max(int(passengers or 1), 1)
    if not snapshot or snapshot.lowest_price is None:
        return [{"headline": "No price data yet for this trip.", "detail": ""}]

    current_pp = float(snapshot.lowest_price) / pax if pax > 1 else float(snapshot.lowest_price)
    typical_low = snapshot.typical_low
    typical_high = snapshot.typical_high

    # Bullet 1 — position vs. Google's typical range.
    # IMPORTANT: Google's typical_price_range is fare-only (no baggage, no
    # transport). The hero is TOTAL /pp. So we compare the FARE component of
    # the user's price against Google's range, and label it "flight fare"
    # explicitly so users don't try to reconcile it against the hero number.
    if typical_low is not None and typical_high is not None:
        low_pp = float(typical_low) / pax if pax > 1 else float(typical_low)
        high_pp = float(typical_high) / pax if pax > 1 else float(typical_high)
        range_str = f"€{low_pp:,.0f}–€{high_pp:,.0f}/pp"
        fare_str = f"€{current_pp:,.0f}/pp"
        if current_pp < low_pp:
            bullets.append({
                "headline": f"Flight fare {fare_str} is €{low_pp - current_pp:,.0f}/pp below Google's typical low.",
                "detail": f"Google's typical range for this route is {range_str} (ticket only — bags & transport not included).",
            })
        elif current_pp > high_pp:
            bullets.append({
                "headline": f"Flight fare {fare_str} is €{current_pp - high_pp:,.0f}/pp above Google's typical high.",
                "detail": f"Google's typical range is {range_str} (ticket only — bags & transport not included). Above-range fares often drop; watch this one.",
            })
        else:
            bullets.append({
                "headline": f"Flight fare {fare_str} is within Google's typical range.",
                "detail": f"Google's typical range is {range_str} (ticket only — bags & transport not included). Not unusually cheap or expensive yet.",
            })

    # Bullet 2 — position in the user's own price history (deduped 90-day series)
    series = []
    if isinstance(price_history_dict, dict):
        raw = price_history_dict.get("series") or []
        for d, p in raw:
            try:
                if p is not None:
                    series.append(float(p))
            except (TypeError, ValueError):
                continue
    if len(series) >= 2:
        series_pp = [s / pax if pax > 1 else s for s in series]
        history_min = min(series_pp)
        history_max = max(series_pp)
        if current_pp <= history_min + 0.5:
            bullets.append({
                "headline": f"New 90-day low.",
                "detail": f"Previous low {len(series)} snapshots ago was €{history_min:,.0f}/pp; high €{history_max:,.0f}/pp.",
            })
        elif current_pp <= (history_min + history_max) / 2:
            bullets.append({
                "headline": f"Below the 90-day midpoint.",
                "detail": f"Range over {len(series)} snapshots: €{history_min:,.0f}–€{history_max:,.0f}/pp.",
            })
        else:
            bullets.append({
                "headline": f"Above the 90-day midpoint.",
                "detail": f"Range over {len(series)} snapshots: €{history_min:,.0f}–€{history_max:,.0f}/pp.",
            })
    elif len(series) == 1:
        bullets.append({
            "headline": "Only 1 price observed so far for these dates.",
            "detail": "Limited history — context will improve over the next polling cycles.",
        })

    # Bullet 3 — change since last alert
    if last_alerted is not None and last_alerted > 0:
        alerted_pp = float(last_alerted) / pax if pax > 1 else float(last_alerted)
        diff_pp = current_pp - alerted_pp
        if abs(diff_pp) >= 1:
            if diff_pp < 0:
                bullets.append({
                    "headline": f"Dropped €{abs(diff_pp):,.0f}/pp since the last alert.",
                    "detail": f"Was €{alerted_pp:,.0f}/pp. Now €{current_pp:,.0f}/pp.",
                })
            else:
                bullets.append({
                    "headline": f"Up €{diff_pp:,.0f}/pp since the last alert.",
                    "detail": f"Was €{alerted_pp:,.0f}/pp. Now €{current_pp:,.0f}/pp.",
                })

    # Bullet 4 — nearby airports footprint (informational only when not available).
    # The /settings page is read-only for the airport list — adding new airports
    # goes via chat, so point users there instead of Preferences.
    if nearby_count == 0:
        bullets.append({
            "headline": "No nearby airports configured for this destination.",
            "detail": "Message me in chat to add airports near your destination — then FareHound can compare door-to-door cost across them.",
        })

    return bullets[:4]


def _build_breakdown_rows(
    *,
    flights_total: float,
    baggage: float,
    transport: float,
    parking: float,
    transport_mode: str,
    passengers: int,
    baggage_label: str = "",
    explanations: dict | None = None,
) -> list[dict]:
    """Build the per-person cost breakdown rows for the deal page.

    All amounts are presented per-person (party-total is shown as a smaller
    annotation on the total line). Pre-R8 this returned party totals in the
    rows + a per-pp annotation on total — that mix was confusing.
    """
    explanations = explanations or {}
    pax = max(int(passengers or 1), 1)

    def per_pp(amount: float) -> float:
        return float(amount) / pax if pax > 1 else float(amount)

    rows = []
    rows.append({
        "label": "flights",
        "op": "",
        "amount_display": _fmt_eur(per_pp(flights_total)),
        "explanation": explanations.get("flights"),
    })
    if baggage:
        # Annotate which baggage assumption we're using so the user can sanity-check.
        rows.append({
            "label": f"baggage{(' ' + baggage_label) if baggage_label else ''}",
            "op": "+",
            "amount_display": _fmt_eur(per_pp(baggage)),
            "explanation": explanations.get("baggage"),
        })
    if transport:
        rows.append({
            "label": f"transport ({transport_mode})" if transport_mode else "transport",
            "op": "+",
            "amount_display": _fmt_eur(per_pp(transport)),
            "explanation": explanations.get("transport"),
        })
    if parking:
        rows.append({
            "label": "parking",
            "op": "+",
            "amount_display": _fmt_eur(per_pp(parking)),
            "explanation": explanations.get("parking"),
        })
    return rows


def assemble_deal(db: Database, deal_id: str, user_id: str | None = None) -> dict | None:
    """Build the full deal-page context. Returns None if the deal doesn't exist."""
    deal_row = _get_deal_by_id(db, deal_id, user_id)
    if not deal_row:
        return None

    route = _get_route(db, deal_row["route_id"], user_id)
    if not route:
        return None

    snapshot = db.get_latest_snapshot(route.route_id, user_id=user_id)

    passengers = int(route.passengers or 2)
    lowest = float(snapshot.lowest_price) if snapshot and snapshot.lowest_price is not None else 0.0
    price_pp = lowest / passengers if passengers > 1 else lowest

    # Transport for primary airport — R9 ITEM-053 picks the cheapest enabled
    # mode for this party + trip duration, honouring any per-airport override.
    trip_days = 0
    if route.earliest_departure and route.latest_return:
        trip_days = max((route.latest_return - route.earliest_departure).days, 0)
    primary = db.get_resolved_transport(
        route.origin, user_id=user_id,
        passengers=passengers, trip_days=trip_days,
    ) or {}
    p_cost = float(primary.get("transport_cost_eur") or 0)
    p_mode = primary.get("transport_mode") or ""
    p_mode_label = primary.get("mode_label") or p_mode
    p_park = float(primary.get("parking_cost_eur") or 0)
    p_total_transport = transport_total(p_cost, p_mode, passengers)

    # User's baggage preference — drives both the assumption label shown to the
    # user and (where SerpAPI doesn't have data) the fallback estimate.
    user_row = db.get_user(user_id) if user_id else {}
    if not isinstance(user_row, dict):
        user_row = {}
    user_prefs = user_row.get("preferences") or {}
    if not isinstance(user_prefs, dict):
        try:
            user_prefs = json.loads(user_prefs)
        except Exception:
            user_prefs = {}
    baggage_needs = (
        user_row.get("baggage_needs")
        or user_prefs.get("baggage_needs")
        or "one_checked"
    )
    baggage_label = {
        "carry_on_only": "(carry-on only)",
        "one_checked": "(1× checked, both ways)",
        "two_checked": "(2× checked, both ways)",
    }.get(baggage_needs, "")

    # Baggage estimate — sum SerpAPI fees per direction, scaled by user preference.
    # Pre-v0.10.14 we always summed carry_on + checked regardless of user prefs;
    # now we honour `baggage_needs`.
    baggage = 0.0
    if snapshot and getattr(snapshot, "baggage_estimate", None):
        try:
            be = snapshot.baggage_estimate if isinstance(snapshot.baggage_estimate, dict) else json.loads(snapshot.baggage_estimate)
            outbound = be.get("outbound", {}) if isinstance(be, dict) else {}
            ret = be.get("return", {}) if isinstance(be, dict) else {}
            for leg in (outbound, ret):
                if not isinstance(leg, dict):
                    continue
                carry = float(leg.get("carry_on") or 0)
                checked = float(leg.get("checked") or 0)
                # Apply user preference. carry_on always counts (it's a fee even
                # for carry-on-only travellers). `checked` is per-bag.
                if baggage_needs == "carry_on_only":
                    leg_total = carry
                elif baggage_needs == "two_checked":
                    leg_total = carry + 2 * checked
                else:  # one_checked (default)
                    leg_total = carry + checked
                baggage += leg_total * passengers
        except Exception:
            pass

    breakdown_total = lowest + baggage + p_total_transport + p_park
    breakdown_rows = _build_breakdown_rows(
        flights_total=lowest,
        baggage=baggage,
        transport=p_total_transport,
        parking=p_park,
        transport_mode=p_mode_label,
        passengers=passengers,
        baggage_label=baggage_label,
    )

    # Last-alerted price (party total) — used for both the since-alert delta
    # AND the deterministic reasoning bullets, so resolved up front.
    last_alerted = db.get_last_alerted_price(route.route_id, user_id=user_id)

    # Price history series — fetched once, used by both the sparkline and the
    # reasoning bullets that compute 90-day min/max/median position.
    history_dict = None
    try:
        history_dict = db.get_price_history(route.route_id, days=90, user_id=user_id)
    except Exception:
        logger.debug("price history unavailable for %s", route.route_id, exc_info=True)

    # Reasoning — built from snapshot data deterministically. Numbers always
    # match the rest of the page. (Pre-v0.10.15, this was a free-text string
    # generated by Claude at scoring time; numbers in the prose drifted from
    # the live page state and confused users.)
    reasoning = _build_deterministic_reasoning(
        snapshot=snapshot,
        last_alerted=last_alerted,
        passengers=passengers,
        price_history_dict=history_dict,
        nearby_count=0,  # follow-up: pass actual count from orchestrator's nearby cache
    )

    # Price history for the sparkline (reuses the series fetched above)
    price_history = {"series_json": "[]", "first_label": "", "last_label": "",
                     "window_days": 90, "typical_low": None, "typical_high": None}
    if isinstance(history_dict, dict):
        series = history_dict.get("series") or []
        normalised = [(str(d), float(p)) for d, p in series if p is not None]
        if normalised:
            price_history["series_json"] = json.dumps(normalised)
            price_history["first_label"] = _fmt_date(normalised[0][0])
            price_history["last_label"] = _fmt_date(normalised[-1][0])
    if snapshot:
        price_history["typical_low"] = snapshot.typical_low
        price_history["typical_high"] = snapshot.typical_high

    # Alternatives — leave empty for now; R7's nearby comparison cache lives on the
    # Orchestrator, not in the DB. The web app will get a richer list once the
    # nearby-evaluated data is persisted (follow-up).
    alternatives = {"airports": [], "dates": []}

    # Baggage policy items derived from utils/baggage FALLBACK
    baggage_items, airline_label = _baggage_policy_items(snapshot)

    # Since-alert delta. Both alerted and current totals are computed in /pp
    # for display consistency. Bag/transport/parking are constants, so the
    # /pp delta of TOTAL equals the /pp delta of FLIGHTS (we display the
    # flights delta as a stand-in for the user-visible delta).
    delta = None
    delta_abs = ""
    alerted_total_pp_display = ""
    if last_alerted is not None and lowest:
        delta = lowest - float(last_alerted)
        delta_pp = delta / passengers if passengers > 1 else delta
        delta_abs = _fmt_eur(abs(delta_pp))
        # Recompute the total at alert-time using the same baggage/transport assumption.
        alerted_total = float(last_alerted) + baggage + p_total_transport + p_park
        alerted_total_pp = alerted_total / passengers if passengers > 1 else alerted_total
        alerted_total_pp_display = _fmt_eur(alerted_total_pp)

    book_url = deal_row.get("booking_url") or _google_flights_url(route, snapshot)

    airline_display = airline_label or (snapshot.best_flight.get("airline") if snapshot and isinstance(getattr(snapshot, "best_flight", None), dict) else None)

    # HERO — total cost per person (incl. baggage + transport + parking).
    # Pre-v0.10.15 the hero was flights-only; that hid the "real cost" the
    # mission promises and made the breakdown total feel disconnected.
    total_pp = breakdown_total / passengers if passengers > 1 else breakdown_total

    return {
        "deal_id": deal_row["deal_id"],
        "route_id": route.route_id,  # exposed on body data attr for the Skip route action
        "route": {
            "origin": route.origin,
            "destination": route.destination,
            "name": airport_name(route.destination) or route.destination,
        },
        "dates": {
            "label": _date_range_label(snapshot.outbound_date if snapshot else None,
                                        snapshot.return_date if snapshot else None) or "—",
        },
        "passengers": passengers,
        "airline": airline_display,
        # Hero number — total /pp (real cost). Flights-only is shown in the breakdown.
        "price_pp_display": _fmt_eur(total_pp),
        "flights_pp_display": _fmt_eur(price_pp),  # available in case the template wants both
        "delta_since_alert": delta,
        "delta_since_alert_abs": delta_abs,
        "alerted_total_pp_display": alerted_total_pp_display,
        "breakdown": {
            "rows": breakdown_rows,
            "total_pp_display": _fmt_eur(total_pp),
            "total_party_display": _fmt_eur(breakdown_total),
            "passengers": passengers,
        },
        "reasoning": reasoning,  # built deterministically; numbers always match the page
        "price_history": price_history,
        "alternatives": alternatives,
        "baggage_policy": {"airline_label": airline_label, "entries": baggage_items},
        "book_url": book_url,
    }


def _baggage_policy_items(snapshot: PriceSnapshot | None) -> tuple[list[dict], str]:
    """Build the baggage-policy items list from the airline of the snapshot's best flight.

    Returns (items, airline_label). Empty if no recognisable airline.
    """
    if not snapshot or not getattr(snapshot, "best_flight", None):
        return [], ""
    try:
        best = snapshot.best_flight if isinstance(snapshot.best_flight, dict) else json.loads(snapshot.best_flight or "{}")
    except (TypeError, ValueError):
        return [], ""
    airline_code = (best.get("airline") if isinstance(best, dict) else None) or ""
    if not airline_code:
        return [], ""

    from src.utils.baggage import FALLBACK

    code = airline_code.upper().strip()
    policy = FALLBACK.get(code) or FALLBACK["_DEFAULT"]
    long_haul_checked = policy["checked_long_haul"]
    short_haul_checked = policy["checked_short_haul"]
    carry_fee = policy.get("carry_on", 0)
    label = code

    def fmt(cost: int) -> dict:
        if cost <= 0:
            return {"cost_display": "included", "cost_class": "included"}
        return {"cost_display": f"+€{cost} each way", "cost_class": "surcharge"}

    entries = [
        {"item": "carry-on", **fmt(carry_fee)},
        {"item": "1× checked (23kg) — long-haul", **fmt(long_haul_checked)},
        {"item": "1× checked (23kg) — short-haul", **fmt(short_haul_checked)},
    ]
    return entries, label


def _google_flights_url(route: Route, snapshot: PriceSnapshot | None) -> str:
    base = f"https://www.google.com/travel/flights?q=Flights+from+{route.origin}+to+{route.destination}"
    if snapshot and snapshot.outbound_date:
        base += f"+on+{snapshot.outbound_date}"
    if snapshot and snapshot.return_date:
        base += f"+return+{snapshot.return_date}"
    if route.passengers and route.passengers > 1:
        base += f"+{route.passengers}+passengers"
    return base


# ---------- routes page ----------


def assemble_routes(db: Database, user_id: str) -> dict:
    routes = db.get_active_routes(user_id=user_id, include_snoozed=True)
    now_iso = datetime.now(UTC).replace(tzinfo=None)
    snoozed_count = 0
    out_routes = []
    for i, route in enumerate(routes, 1):
        is_snoozed = bool(route.snoozed_until)
        if is_snoozed:
            try:
                until = datetime.fromisoformat(str(route.snoozed_until).replace("Z", "+00:00").replace("+00:00", ""))
                if until > now_iso:
                    snoozed_count += 1
                else:
                    is_snoozed = False
            except Exception:
                is_snoozed = False

        snapshot = db.get_latest_snapshot(route.route_id, user_id=user_id)
        passengers = int(route.passengers or 2)
        is_pending = snapshot is None
        current_pp = None
        if snapshot and snapshot.lowest_price is not None:
            current_pp = float(snapshot.lowest_price) / passengers if passengers > 1 else float(snapshot.lowest_price)

        last_alerted = db.get_last_alerted_price(route.route_id, user_id=user_id)
        delta_label = None
        is_new_low = False
        if current_pp is not None and last_alerted is not None:
            diff = current_pp - (float(last_alerted) / passengers if passengers > 1 else float(last_alerted))
            if abs(diff) >= 1:
                arrow = "▼" if diff < 0 else "▲"
                delta_label = f"{arrow} €{_fmt_eur(abs(diff))}"

        out_routes.append({
            "route_id": route.route_id,
            "ordinal": f"{i:02d}",
            "origin": route.origin,
            "destination": route.destination,
            "city": airport_name(route.destination) or route.destination,
            "dates_label": _date_range_label(route.earliest_departure, route.latest_return) or "dates flexible",
            "passengers": passengers,
            "current_price_pp_display": _fmt_eur(current_pp) if current_pp else "—",
            "delta_label": delta_label,
            "is_new_low": is_new_low,
            "is_snoozed": is_snoozed,
            "is_pending": is_pending,
            "footnote": _route_footnote(route, snapshot),
            "latest_deal_id": _latest_deal_id(db, route.route_id, user_id),
        })

    last_poll_label = _last_poll_label(db, user_id)
    return {
        "summary": {
            "monitored": len(routes),
            "snoozed": snoozed_count,
            "last_poll_label": last_poll_label,
            "serpapi_used": _serpapi_used_this_month(),
            "serpapi_cap": 950,
            "savings_total": None,
            "savings_trip_count": None,
        },
        "routes": out_routes,
    }


def _route_footnote(route: Route, snapshot: PriceSnapshot | None) -> str:
    if snapshot is None:
        return "no price data yet"
    parts = []
    try:
        observed = datetime.fromisoformat(str(snapshot.observed_at).replace("Z", "+00:00").split("+")[0])
        delta = datetime.now(UTC).replace(tzinfo=None) - observed
        hours = int(delta.total_seconds() // 3600)
        parts.append(f"last poll {hours}h ago" if hours < 48 else f"last poll {hours // 24}d ago")
    except Exception:
        pass
    return " · ".join(parts) if parts else ""


def _latest_deal_id(db: Database, route_id: str, user_id: str | None = None) -> str:
    sql = (
        "SELECT deal_id FROM deals WHERE route_id = ?"
    )
    params: list = [route_id]
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    sql += " ORDER BY alert_sent_at DESC, created_at DESC LIMIT 1"
    row = db._conn.execute(sql, params).fetchone()
    return row[0] if row else ""


def _last_poll_label(db: Database, user_id: str) -> str:
    sql = "SELECT MAX(observed_at) FROM price_snapshots WHERE user_id = ?"
    row = db._conn.execute(sql, [user_id]).fetchone()
    if not row or not row[0]:
        return ""
    try:
        observed = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00").split("+")[0])
        delta = datetime.now(UTC).replace(tzinfo=None) - observed
        hours = int(delta.total_seconds() // 3600)
        return f"{hours}h ago" if hours < 48 else f"{hours // 24}d ago"
    except Exception:
        return ""


def _serpapi_used_this_month() -> int:
    """Best-effort read from the SerpAPI usage tracker. Returns 0 if unavailable."""
    try:
        from src.apis.serpapi import _api_call_count
        return int(_api_call_count)
    except Exception:
        return 0


# ---------- settings page ----------


_BAGGAGE_OPTIONS = [
    {"value": "carry_on_only", "label": "carry-on only", "desc": "no checked bags"},
    {"value": "one_checked", "label": "one checked bag", "desc": "23kg, both directions"},
    {"value": "two_checked", "label": "two checked bags", "desc": "23kg each, both directions"},
]


def assemble_settings(db: Database, user_id: str, telegram_handle: str = "") -> dict:
    user = db.get_user(user_id) or {}
    prefs_raw = user.get("preferences")
    prefs: dict = {}
    if prefs_raw:
        try:
            prefs = json.loads(prefs_raw) if isinstance(prefs_raw, str) else dict(prefs_raw)
        except Exception:
            prefs = {}

    # R9 ITEM-053: render the editable multi-mode airport list. Each airport
    # gets a card with all options (drive/train/taxi/...) and the user's per-
    # airport override mode if set. The legacy airport_transport table is
    # consulted only for airport_name + is_primary metadata; cost data lives
    # in airport_transport_option after the migration.
    options_all = db.get_all_transport_options(user_id) if user_id else []
    legacy_meta = {
        t["airport_code"]: t for t in db.get_all_airport_transports(user_id=user_id)
    }
    by_airport: dict[str, list[dict]] = {}
    for opt in options_all:
        by_airport.setdefault(opt["airport_code"], []).append(opt)

    airport_cards: list[dict] = []
    seen_codes: set[str] = set()
    # Order: primary airport first, then alphabetical.
    primary_code = next(
        (m["airport_code"] for m in legacy_meta.values() if m.get("is_primary")),
        None,
    )
    ordered_codes: list[str] = []
    if primary_code:
        ordered_codes.append(primary_code)
    for code in sorted(by_airport.keys()):
        if code != primary_code:
            ordered_codes.append(code)
    for code in legacy_meta.keys():
        if code not in ordered_codes:
            ordered_codes.append(code)

    user_overrides_raw = user.get("airport_override_mode")
    user_overrides = {}
    if user_overrides_raw:
        try:
            user_overrides = json.loads(user_overrides_raw)
            if not isinstance(user_overrides, dict):
                user_overrides = {}
        except Exception:
            user_overrides = {}

    for code in ordered_codes:
        if code in seen_codes:
            continue
        seen_codes.add(code)
        meta = legacy_meta.get(code, {})
        opts = by_airport.get(code, [])
        modes_view = []
        for o in opts:
            time_min = o.get("time_min")
            modes_view.append({
                "mode": o.get("mode"),
                "cost_eur": o.get("cost_eur"),
                "cost_scales_with_pax": o.get("cost_scales_with_pax"),
                "time_min": time_min,
                "time_label": _format_time_min(time_min) if time_min is not None else None,
                "parking_cost_per_day_eur": o.get("parking_cost_per_day_eur"),
                "enabled": o.get("enabled"),
                "source": o.get("source"),
                "confidence": o.get("confidence"),
                "label": o.get("label"),
            })
        airport_cards.append({
            "code": code,
            "name": meta.get("airport_name") or code,
            "is_primary": bool(meta.get("is_primary")),
            "modes": modes_view,
            "override_mode": user_overrides.get(code),
        })

    return {
        "settings": {
            "baggage_needs": user.get("baggage_needs") or prefs.get("baggage_needs") or "one_checked",
            "airports": airport_cards,
            "transports": [],  # legacy field — kept for template back-compat; templates read `airports` now
            "quiet_from": prefs.get("quiet_from"),
            "quiet_to": prefs.get("quiet_to"),
            "digest_time": prefs.get("digest_time"),
            "digest_skip_count_7d": user.get("digest_skip_count_7d") or 0,
            "telegram_label": telegram_handle or user.get("name") or "—",
            "version": "0.11.1",
        },
        "baggage_options": _BAGGAGE_OPTIONS,
    }


def _format_time_min(minutes: int | float | None) -> str:
    if minutes is None:
        return "—"
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if m else f"{h}h"
