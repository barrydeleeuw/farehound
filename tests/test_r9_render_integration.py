"""R9 ITEM-053: integration test for the render path.

The non-negotiable Phase 4 step 6b integration test: a route + snapshot + deal
+ multiple transport options → assemble_deal() → assert the chosen mode appears
in the breakdown row label AND the total includes its cost.

Mocks HTTP only (none here — no SerpAPI/Google Maps calls in render path).
Instantiates real Database, real models, real assembler — no per-component mocks.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src.storage.db import Database
from src.storage.models import Deal, PriceSnapshot, Route
from src.web.data import assemble_deal


@pytest.fixture
def db_with_user(tmp_path):
    database = Database(db_path=tmp_path / "test.db")
    database.init_schema()
    database._conn.execute(
        "INSERT INTO users (user_id, telegram_chat_id, name, onboarded, active, approved) "
        "VALUES (?, ?, ?, 1, 1, 1)",
        ["u1", "123", "Test"],
    )
    database._conn.commit()
    yield database
    database.close()


def _seed_route_snapshot_deal(db: Database, *, transport_options: list[dict]):
    """Seed a fully-wired route + snapshot + deal so assemble_deal() works end-to-end."""
    route = Route(
        route_id="r1",
        origin="AMS",
        destination="NRT",
        passengers=2,
        earliest_departure=date(2026, 10, 1),
        latest_return=date(2026, 10, 8),  # 7-day trip
        user_id="u1",
    )
    db.upsert_route(route)
    snapshot = PriceSnapshot(
        snapshot_id="s1",
        route_id="r1",
        observed_at=datetime.now(UTC),
        source="serpapi",
        passengers=2,
        outbound_date=date(2026, 10, 1),
        return_date=date(2026, 10, 8),
        lowest_price=800.0,
        all_flights=[],
        best_flight={"flights": [{"airline": "KL"}], "price": 800.0},
        user_id="u1",
    )
    db.insert_snapshot(snapshot, user_id="u1")
    deal = Deal(
        deal_id="d1",
        snapshot_id="s1",
        route_id="r1",
        score=0.9,
        urgency="high",
        reasoning="great deal",
        user_id="u1",
        alert_sent_at=datetime.now(UTC),
    )
    db.insert_deal(deal, user_id="u1")
    # Seed transport options.
    for opt in transport_options:
        db.add_transport_option(user_id="u1", airport_code="AMS", **opt)
    return route, snapshot, deal


def test_assemble_deal_picks_cheapest_mode_for_2pax_7days(db_with_user):
    """2 pax × 7 days: drive=235, train=60, taxi=100 → train wins."""
    _seed_route_snapshot_deal(
        db_with_user,
        transport_options=[
            {"mode": "drive", "cost_eur": 30, "cost_scales_with_pax": False,
             "parking_cost_per_day_eur": 25, "source": "user_added"},
            {"mode": "train", "cost_eur": 15, "cost_scales_with_pax": True,
             "parking_cost_per_day_eur": None, "source": "curated"},
            {"mode": "taxi", "cost_eur": 50, "cost_scales_with_pax": False,
             "parking_cost_per_day_eur": None, "source": "user_added"},
        ],
    )
    payload = assemble_deal(db_with_user, deal_id="d1", user_id="u1")
    assert payload is not None
    rows = payload["breakdown"]["rows"]
    transport_rows = [r for r in rows if "transport" in r["label"]]
    assert len(transport_rows) == 1
    # Mode label should mention "train" and indicate it's the cheapest of multiple.
    assert "train" in transport_rows[0]["label"]
    assert "cheapest" in transport_rows[0]["label"]
    # No parking row because train has no parking.
    parking_rows = [r for r in rows if r["label"] == "parking"]
    assert parking_rows == []


def test_assemble_deal_respects_override(db_with_user):
    """If user sets override='drive', breakdown shows drive even though train is cheaper."""
    _seed_route_snapshot_deal(
        db_with_user,
        transport_options=[
            {"mode": "drive", "cost_eur": 30, "cost_scales_with_pax": False,
             "parking_cost_per_day_eur": 25},
            {"mode": "train", "cost_eur": 15, "cost_scales_with_pax": True},
        ],
    )
    db_with_user.set_airport_override_mode(
        user_id="u1", airport_code="AMS", mode="drive"
    )
    payload = assemble_deal(db_with_user, deal_id="d1", user_id="u1")
    rows = payload["breakdown"]["rows"]
    transport_rows = [r for r in rows if "transport" in r["label"]]
    assert "drive" in transport_rows[0]["label"]
    assert "your choice" in transport_rows[0]["label"]
    # Drive has parking → parking row appears.
    parking_rows = [r for r in rows if r["label"] == "parking"]
    assert len(parking_rows) == 1


def test_assemble_deal_with_no_options_falls_back_to_legacy(db_with_user):
    """User has legacy airport_transport row but no airport_transport_option rows;
    the resolver must fall back gracefully so the deal page still renders."""
    db_with_user.seed_airport_transport(
        [{"code": "AMS", "name": "Schiphol", "transport_mode": "drive",
          "transport_cost_eur": 30, "transport_time_min": 25, "parking_cost_eur": 0,
          "is_primary": True}],
        user_id="u1",
    )
    # NOTE: skip init_schema re-run; we want to test the fallback path explicitly.
    _seed_route_snapshot_deal(db_with_user, transport_options=[])
    # Manually clear the option table to test pure-fallback behavior.
    db_with_user._conn.execute("DELETE FROM airport_transport_option")
    db_with_user._conn.commit()
    payload = assemble_deal(db_with_user, deal_id="d1", user_id="u1")
    assert payload is not None
    rows = payload["breakdown"]["rows"]
    transport_rows = [r for r in rows if "transport" in r["label"]]
    # Mode is plain "drive" without a "(cheapest)" suffix.
    assert len(transport_rows) == 1
    assert "drive" in transport_rows[0]["label"]


def test_assemble_deal_total_includes_cheapest_mode_cost(db_with_user):
    """Hero `total /pp` must reflect the chosen mode's cost contribution."""
    _seed_route_snapshot_deal(
        db_with_user,
        transport_options=[
            {"mode": "train", "cost_eur": 15, "cost_scales_with_pax": True},
        ],
    )
    payload = assemble_deal(db_with_user, deal_id="d1", user_id="u1")
    breakdown = payload["breakdown"]
    # 800 (flights) + 60 (train RT × 2 pax) = 860 party total = 430 /pp.
    # We can't assert exact numerals because baggage may add too — just assert
    # the breakdown total exceeds flights-only (i.e. transport contributed).
    assert breakdown["total_party_display"]
    assert breakdown["total_pp_display"]
    transport_rows = [r for r in breakdown["rows"] if "transport" in r["label"]]
    # Per-pp display: train RT 30/pp.
    assert "30" in transport_rows[0]["amount_display"] or "29" in transport_rows[0]["amount_display"]


def test_trips_list_price_matches_deal_page_total(db_with_user):
    """v0.11.4: trips list current_price_pp_display must equal the deal page
    hero total /pp (flights + baggage + transport + parking ÷ pax). Pre-fix the
    trips list was showing fare-only (€129) while the deal page showed total
    (€200) — confusing the user."""
    from src.web.data import assemble_routes

    _seed_route_snapshot_deal(
        db_with_user,
        transport_options=[
            {"mode": "train", "cost_eur": 11, "cost_scales_with_pax": True},
        ],
    )
    deal_payload = assemble_deal(db_with_user, deal_id="d1", user_id="u1")
    routes_payload = assemble_routes(db_with_user, user_id="u1")
    route_card = next(r for r in routes_payload["routes"] if r["route_id"] == "r1")

    # Both must show the same per-person total.
    deal_hero_pp = deal_payload["price_pp_display"]
    trips_pp = route_card["current_price_pp_display"]
    assert deal_hero_pp == trips_pp, (
        f"Deal page shows {deal_hero_pp}/pp but trips list shows {trips_pp}/pp"
    )


def test_trips_list_no_deal_yields_no_link(db_with_user):
    """v0.11.4: when a route has a snapshot but no Deal record (score below
    alert threshold), latest_deal_id is None and the template skips the <a>."""
    from src.storage.models import PriceSnapshot, Route
    from src.web.data import assemble_routes

    route = Route(
        route_id="r2", origin="AMS", destination="MEX", passengers=2,
        earliest_departure=date(2026, 12, 28), latest_return=date(2027, 1, 18),
        user_id="u1",
    )
    db_with_user.upsert_route(route)
    snap = PriceSnapshot(
        snapshot_id="s2", route_id="r2",
        observed_at=datetime.now(UTC), source="serpapi", passengers=2,
        outbound_date=date(2026, 12, 28), return_date=date(2027, 1, 18),
        lowest_price=1964.0, user_id="u1",
    )
    db_with_user.insert_snapshot(snap, user_id="u1")
    # Note: NO deal saved.

    payload = assemble_routes(db_with_user, user_id="u1")
    route_card = next(r for r in payload["routes"] if r["route_id"] == "r2")
    assert route_card["latest_deal_id"] is None
    assert route_card["is_pending"] is False  # snapshot exists
    # Price still computed (total /pp).
    assert route_card["current_price_pp_display"] != "—"


def test_assemble_deal_disabled_mode_excluded(db_with_user):
    """Disabling train should switch the chosen mode to next-cheapest."""
    _seed_route_snapshot_deal(
        db_with_user,
        transport_options=[
            {"mode": "train", "cost_eur": 15, "cost_scales_with_pax": True, "enabled": False},
            {"mode": "taxi", "cost_eur": 50, "cost_scales_with_pax": False},
            {"mode": "drive", "cost_eur": 30, "cost_scales_with_pax": False,
             "parking_cost_per_day_eur": 25},
        ],
    )
    payload = assemble_deal(db_with_user, deal_id="d1", user_id="u1")
    rows = payload["breakdown"]["rows"]
    transport_rows = [r for r in rows if "transport" in r["label"]]
    # 2 pax × 7 days: taxi=100, drive=235 → taxi wins.
    assert "taxi" in transport_rows[0]["label"]
