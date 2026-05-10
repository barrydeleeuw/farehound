"""R9 ITEM-053: cheapest-mode selection, multi-mode storage, migration roundtrip."""

from __future__ import annotations

import pytest

from src.analysis.transport import (
    compute_mode_total,
    is_per_person_mode,
    pick_cheapest_mode,
    resolve_breakdown_inputs,
)
from src.storage.db import Database


# ---------- Math layer ----------


def test_is_per_person_mode_classifies_modes():
    assert is_per_person_mode("train") is True
    assert is_per_person_mode("Train") is True  # case-insensitive
    assert is_per_person_mode("bus") is True
    assert is_per_person_mode("metro") is True
    assert is_per_person_mode("drive") is False
    assert is_per_person_mode("taxi") is False
    assert is_per_person_mode("uber") is False
    assert is_per_person_mode(None) is False
    assert is_per_person_mode("") is False


def test_compute_mode_total_drive_with_parking():
    drive = {
        "mode": "drive", "cost_eur": 30, "cost_scales_with_pax": False,
        "parking_cost_per_day_eur": 25,
    }
    # 1-way 30 → RT 60; parking 25*7 = 175; total 235.
    assert compute_mode_total(drive, passengers=2, trip_days=7) == 235.0
    # No parking when trip_days=0.
    assert compute_mode_total(drive, passengers=2, trip_days=0) == 60.0


def test_compute_mode_total_train_scales_with_pax():
    train = {
        "mode": "train", "cost_eur": 15, "cost_scales_with_pax": True,
        "parking_cost_per_day_eur": None,
    }
    # 15 * 2 (RT) * 1 = 30; trip_days irrelevant.
    assert compute_mode_total(train, passengers=1, trip_days=7) == 30.0
    # 15 * 2 * 4 = 120.
    assert compute_mode_total(train, passengers=4, trip_days=7) == 120.0


def test_compute_mode_total_taxi_per_vehicle():
    taxi = {
        "mode": "taxi", "cost_eur": 50, "cost_scales_with_pax": False,
        "parking_cost_per_day_eur": None,
    }
    # 50 * 2 (RT) — same regardless of pax.
    assert compute_mode_total(taxi, passengers=1, trip_days=14) == 100.0
    assert compute_mode_total(taxi, passengers=4, trip_days=14) == 100.0


def test_compute_mode_total_handles_none_cost():
    opt = {"mode": "drive", "cost_eur": None, "cost_scales_with_pax": False}
    assert compute_mode_total(opt, passengers=2, trip_days=7) == 0.0


# ---------- Cheapest-mode selection ----------


@pytest.fixture
def options_set():
    return [
        {"mode": "drive", "cost_eur": 30, "cost_scales_with_pax": False,
         "parking_cost_per_day_eur": 25, "enabled": True},
        {"mode": "train", "cost_eur": 15, "cost_scales_with_pax": True,
         "parking_cost_per_day_eur": None, "enabled": True},
        {"mode": "taxi", "cost_eur": 50, "cost_scales_with_pax": False,
         "parking_cost_per_day_eur": None, "enabled": True},
    ]


def test_pick_cheapest_2pax_long_trip_picks_train(options_set):
    # 2 pax, 7 days: drive=235, train=60, taxi=100 → train.
    chosen = pick_cheapest_mode(options_set, passengers=2, trip_days=7)
    assert chosen["mode"] == "train"


def test_pick_cheapest_4pax_short_trip_picks_taxi(options_set):
    # 4 pax, 3 days: drive=30*2+25*3=135, train=15*2*4=120, taxi=100 → taxi.
    chosen = pick_cheapest_mode(options_set, passengers=4, trip_days=3)
    assert chosen["mode"] == "taxi"


def test_pick_cheapest_4pax_long_trip_picks_train(options_set):
    # 4 pax, 14 days: drive=30*2+25*14=410, train=15*2*4=120, taxi=100 → taxi still.
    chosen = pick_cheapest_mode(options_set, passengers=4, trip_days=14)
    assert chosen["mode"] == "taxi"


def test_pick_cheapest_solo_traveler_picks_train(options_set):
    # 1 pax, 7 days: drive=60+175=235, train=30, taxi=100 → train.
    chosen = pick_cheapest_mode(options_set, passengers=1, trip_days=7)
    assert chosen["mode"] == "train"


def test_override_mode_wins_when_set(options_set):
    chosen = pick_cheapest_mode(
        options_set, passengers=2, trip_days=7, override_mode="drive"
    )
    assert chosen["mode"] == "drive"


def test_override_falls_back_to_cheapest_if_mode_missing(options_set):
    # User said "always use uber" but uber isn't in their options — fall through.
    chosen = pick_cheapest_mode(
        options_set, passengers=2, trip_days=7, override_mode="uber"
    )
    assert chosen["mode"] == "train"


def test_disabled_modes_excluded(options_set):
    # Disable train; cheapest of remaining (drive=235, taxi=100) → taxi.
    options_set[1]["enabled"] = False
    chosen = pick_cheapest_mode(options_set, passengers=2, trip_days=7)
    assert chosen["mode"] == "taxi"


def test_pick_cheapest_returns_none_when_no_options():
    assert pick_cheapest_mode([], passengers=2, trip_days=7) is None


def test_pick_cheapest_returns_none_when_all_disabled(options_set):
    for o in options_set:
        o["enabled"] = False
    assert pick_cheapest_mode(options_set, passengers=2, trip_days=7) is None


# ---------- resolve_breakdown_inputs ----------


def test_resolve_breakdown_inputs_returns_legacy_shape(options_set):
    res = resolve_breakdown_inputs(options_set, passengers=2, trip_days=7)
    assert res["mode"] == "train"
    assert res["transport_cost_eur"] == 15.0  # one-way
    assert res["parking_cost_eur"] == 0.0     # train has no parking
    assert res["is_cheapest"] is True
    assert res["override_used"] is False
    assert res["no_options"] is False


def test_resolve_breakdown_inputs_drive_resolves_parking(options_set):
    # Pick drive via override; parking should resolve per-day × trip_days.
    res = resolve_breakdown_inputs(
        options_set, passengers=2, trip_days=7, override_mode="drive"
    )
    assert res["mode"] == "drive"
    assert res["transport_cost_eur"] == 30.0
    assert res["parking_cost_eur"] == 25 * 7  # 175
    assert res["override_used"] is True
    assert res["is_cheapest"] is False  # user chose, not cheapest


def test_resolve_breakdown_inputs_no_options_flag():
    res = resolve_breakdown_inputs([], passengers=2, trip_days=7)
    assert res["no_options"] is True
    assert res["transport_cost_eur"] == 0.0


# ---------- DB layer: multi-mode CRUD + migration ----------


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "test.db")
    database.init_schema()
    database._conn.execute(
        "INSERT INTO users (user_id, telegram_chat_id, name) VALUES (?, ?, ?)",
        ["u1", "123", "Test"],
    )
    database._conn.commit()
    yield database
    database.close()


def test_add_and_get_transport_option(db):
    db.add_transport_option(
        user_id="u1", airport_code="AMS", mode="train",
        cost_eur=15, cost_scales_with_pax=True, time_min=30,
        source="curated",
    )
    opts = db.get_transport_options("AMS", "u1")
    assert len(opts) == 1
    assert opts[0]["mode"] == "train"
    assert opts[0]["cost_eur"] == 15
    assert opts[0]["cost_scales_with_pax"] is True
    assert opts[0]["enabled"] is True
    assert opts[0]["source"] == "curated"


def test_add_transport_option_replaces_existing(db):
    db.add_transport_option(
        user_id="u1", airport_code="AMS", mode="train",
        cost_eur=15, cost_scales_with_pax=True,
    )
    db.add_transport_option(
        user_id="u1", airport_code="AMS", mode="train",
        cost_eur=20, cost_scales_with_pax=True,  # update via re-insert
    )
    opts = db.get_transport_options("AMS", "u1")
    assert len(opts) == 1
    assert opts[0]["cost_eur"] == 20


def test_update_transport_option_promotes_to_user_override(db):
    db.add_transport_option(
        user_id="u1", airport_code="AMS", mode="train",
        cost_eur=15, cost_scales_with_pax=True, source="curated",
    )
    updated = db.update_transport_option(
        user_id="u1", airport_code="AMS", mode="train", cost_eur=18
    )
    assert updated is True
    opts = db.get_transport_options("AMS", "u1")
    assert opts[0]["cost_eur"] == 18
    assert opts[0]["source"] == "user_override"


def test_disabled_options_excluded_by_default(db):
    db.add_transport_option(
        user_id="u1", airport_code="AMS", mode="train",
        cost_eur=15, cost_scales_with_pax=True,
    )
    db.add_transport_option(
        user_id="u1", airport_code="AMS", mode="taxi",
        cost_eur=50, cost_scales_with_pax=False, enabled=False,
    )
    enabled = db.get_transport_options("AMS", "u1")
    assert len(enabled) == 1
    assert enabled[0]["mode"] == "train"
    all_opts = db.get_transport_options("AMS", "u1", include_disabled=True)
    assert len(all_opts) == 2


def test_delete_transport_option(db):
    db.add_transport_option(
        user_id="u1", airport_code="AMS", mode="train",
        cost_eur=15, cost_scales_with_pax=True,
    )
    deleted = db.delete_transport_option(
        user_id="u1", airport_code="AMS", mode="train"
    )
    assert deleted is True
    assert db.get_transport_options("AMS", "u1", include_disabled=True) == []
    # Deleting again returns False (no row).
    assert db.delete_transport_option(
        user_id="u1", airport_code="AMS", mode="train"
    ) is False


def test_airport_override_mode_set_get_clear(db):
    assert db.get_airport_override_mode("AMS", "u1") is None
    db.set_airport_override_mode(user_id="u1", airport_code="AMS", mode="drive")
    assert db.get_airport_override_mode("AMS", "u1") == "drive"
    # Different airport unaffected.
    db.set_airport_override_mode(user_id="u1", airport_code="EIN", mode="train")
    assert db.get_airport_override_mode("AMS", "u1") == "drive"
    assert db.get_airport_override_mode("EIN", "u1") == "train"
    # Clear one.
    db.set_airport_override_mode(user_id="u1", airport_code="AMS", mode=None)
    assert db.get_airport_override_mode("AMS", "u1") is None
    assert db.get_airport_override_mode("EIN", "u1") == "train"


def test_legacy_airport_transport_migrated_forward(db):
    # Seed legacy table directly.
    db.seed_airport_transport(
        [{"code": "AMS", "name": "Schiphol", "transport_mode": "train",
          "transport_cost_eur": 15, "transport_time_min": 30, "parking_cost_eur": None,
          "is_primary": True}],
        user_id="u1",
    )
    # Re-init triggers idempotent forward migration.
    db.init_schema()
    opts = db.get_transport_options("AMS", "u1")
    assert len(opts) == 1
    assert opts[0]["mode"] == "train"
    assert opts[0]["cost_eur"] == 15
    assert opts[0]["source"] == "legacy"
    assert opts[0]["cost_scales_with_pax"] is True  # train → per-person


def test_migration_is_idempotent(db):
    db.seed_airport_transport(
        [{"code": "AMS", "name": "Schiphol", "transport_mode": "drive",
          "transport_cost_eur": 30, "transport_time_min": 25, "parking_cost_eur": 8,
          "is_primary": True}],
        user_id="u1",
    )
    for _ in range(3):
        db.init_schema()
    opts = db.get_transport_options("AMS", "u1")
    assert len(opts) == 1, "migration should not duplicate rows on re-run"


def test_migration_preserves_user_edits(db):
    """If a user has already edited a forward-migrated row, re-running the
    migration must not overwrite their edit (e.g. promoted source='user_override')."""
    db.seed_airport_transport(
        [{"code": "AMS", "name": "Schiphol", "transport_mode": "train",
          "transport_cost_eur": 15, "transport_time_min": 30, "parking_cost_eur": None,
          "is_primary": True}],
        user_id="u1",
    )
    db.init_schema()  # forward migrate
    db.update_transport_option(
        user_id="u1", airport_code="AMS", mode="train", cost_eur=22
    )
    db.init_schema()  # re-run migration
    opts = db.get_transport_options("AMS", "u1")
    assert opts[0]["cost_eur"] == 22  # user edit preserved
    assert opts[0]["source"] == "user_override"


def test_get_all_transport_options_sorted(db):
    db.add_transport_option(user_id="u1", airport_code="EIN", mode="train",
                            cost_eur=10, cost_scales_with_pax=True)
    db.add_transport_option(user_id="u1", airport_code="AMS", mode="drive",
                            cost_eur=30, cost_scales_with_pax=False)
    db.add_transport_option(user_id="u1", airport_code="AMS", mode="train",
                            cost_eur=15, cost_scales_with_pax=True)
    all_opts = db.get_all_transport_options("u1")
    codes = [o["airport_code"] for o in all_opts]
    assert codes == ["AMS", "AMS", "EIN"]  # AMS comes first, then EIN
