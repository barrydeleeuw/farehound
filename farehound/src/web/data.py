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


def _build_breakdown_rows(
    *,
    flights_total: float,
    baggage: float,
    transport: float,
    parking: float,
    transport_mode: str,
    passengers: int,
    explanations: dict | None = None,
) -> list[dict]:
    explanations = explanations or {}
    rows = []
    rows.append({
        "label": "flights",
        "op": "",
        "amount_display": _fmt_eur(flights_total),
        "explanation": explanations.get("flights"),
    })
    if baggage:
        rows.append({
            "label": "baggage",
            "op": "+",
            "amount_display": _fmt_eur(baggage),
            "explanation": explanations.get("baggage"),
        })
    if transport:
        rows.append({
            "label": f"transport ({transport_mode})" if transport_mode else "transport",
            "op": "+",
            "amount_display": _fmt_eur(transport),
            "explanation": explanations.get("transport"),
        })
    if parking:
        rows.append({
            "label": "parking",
            "op": "+",
            "amount_display": _fmt_eur(parking),
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

    # Transport for primary airport
    primary = db.get_airport_transport(route.origin, user_id=user_id) or {}
    p_cost = float(primary.get("transport_cost_eur") or 0)
    p_mode = primary.get("transport_mode") or ""
    p_park = float(primary.get("parking_cost_eur") or 0)
    p_total_transport = transport_total(p_cost, p_mode, passengers)

    # Baggage estimate from snapshot (set by R7's parser)
    baggage = 0.0
    if snapshot and getattr(snapshot, "baggage_estimate", None):
        try:
            be = snapshot.baggage_estimate if isinstance(snapshot.baggage_estimate, dict) else json.loads(snapshot.baggage_estimate)
            outbound = be.get("outbound", {}) if isinstance(be, dict) else {}
            ret = be.get("return", {}) if isinstance(be, dict) else {}
            for leg in (outbound, ret):
                for k in ("checked", "carry_on"):
                    v = leg.get(k) if isinstance(leg, dict) else None
                    if v is not None:
                        baggage += float(v) * passengers
        except Exception:
            pass

    breakdown_total = lowest + baggage + p_total_transport + p_park
    breakdown_rows = _build_breakdown_rows(
        flights_total=lowest,
        baggage=baggage,
        transport=p_total_transport,
        parking=p_park,
        transport_mode=p_mode,
        passengers=passengers,
    )

    # Reasoning — prefer structured 3-field JSON, fall back to free-text
    reasoning = _parse_reasoning_json(deal_row.get("reasoning_json"))
    if reasoning is None and deal_row.get("reasoning"):
        # Convert the legacy single string into one bullet so the template still renders
        reasoning = [{"headline": deal_row["reasoning"], "detail": ""}]

    # Price history (90 days) for the sparkline
    price_history = {"series_json": "[]", "first_label": "", "last_label": "",
                     "window_days": 90, "typical_low": None, "typical_high": None}
    try:
        ph = db.get_price_history(route.route_id, days=90, user_id=user_id)
        series = ph.get("series") if isinstance(ph, dict) else None
        if series:
            # Ensure shape [iso, price]
            normalised = [(str(d), float(p)) for d, p in series if p is not None]
            if normalised:
                price_history["series_json"] = json.dumps(normalised)
                price_history["first_label"] = _fmt_date(normalised[0][0])
                price_history["last_label"] = _fmt_date(normalised[-1][0])
        if snapshot:
            price_history["typical_low"] = snapshot.typical_low
            price_history["typical_high"] = snapshot.typical_high
    except Exception:
        logger.debug("price history unavailable for %s", route.route_id, exc_info=True)

    # Alternatives — leave empty for now; R7's nearby comparison cache lives on the
    # Orchestrator, not in the DB. The web app will get a richer list once the
    # nearby-evaluated data is persisted (follow-up).
    alternatives = {"airports": [], "dates": []}

    # Baggage policy items derived from utils/baggage FALLBACK
    baggage_items, airline_label = _baggage_policy_items(snapshot)

    # Last-alerted price for the delta
    last_alerted = db.get_last_alerted_price(route.route_id, user_id=user_id)
    delta = None
    delta_abs = ""
    if last_alerted is not None and lowest:
        delta = lowest - float(last_alerted)
        delta_abs = _fmt_eur(abs(delta))

    book_url = deal_row.get("booking_url") or _google_flights_url(route, snapshot)

    airline_display = airline_label or (snapshot.best_flight.get("airline") if snapshot and isinstance(getattr(snapshot, "best_flight", None), dict) else None)

    return {
        "deal_id": deal_row["deal_id"],
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
        "price_pp_display": _fmt_eur(price_pp),
        "delta_since_alert": delta,
        "delta_since_alert_abs": delta_abs,
        "breakdown": {
            "rows": breakdown_rows,
            "total_display": _fmt_eur(breakdown_total),
            "total_pp_display": _fmt_eur(breakdown_total / passengers if passengers > 1 else breakdown_total),
        },
        "reasoning": reasoning or [],
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

    transports = db.get_all_airport_transports(user_id=user_id)
    transport_rows = []
    for t in transports:
        time_min = t.get("transport_time_min")
        time_label = _format_time_min(time_min) if time_min is not None else None
        transport_rows.append({
            "airport_code": t.get("airport_code"),
            "transport_mode": t.get("transport_mode"),
            "transport_cost_eur": t.get("transport_cost_eur"),
            "time_label": time_label,
            "parking_cost_eur": t.get("parking_cost_eur"),
        })

    return {
        "settings": {
            "baggage_needs": user.get("baggage_needs") or prefs.get("baggage_needs") or "one_checked",
            "transports": transport_rows,
            "quiet_from": prefs.get("quiet_from"),
            "quiet_to": prefs.get("quiet_to"),
            "digest_time": prefs.get("digest_time"),
            "digest_skip_count_7d": user.get("digest_skip_count_7d") or 0,
            "telegram_label": telegram_handle or user.get("name") or "—",
            "version": "0.10.0",
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
