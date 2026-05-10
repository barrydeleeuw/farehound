"""R9 ITEM-053: onboarding auto-fill helper — populates airport_transport_option
from Google Maps + curated datasets. Graceful skip when key missing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.bot.commands import TripBot
from src.storage.db import Database


@pytest.fixture
def db_with_user(tmp_path):
    database = Database(db_path=tmp_path / "test.db")
    database.init_schema()
    database._conn.execute(
        "INSERT INTO users (user_id, telegram_chat_id, name, onboarded, active) "
        "VALUES (?, ?, ?, 1, 1)",
        ["u1", "123", "Test"],
    )
    database._conn.commit()
    yield database
    database.close()


def _make_bot(db, *, gm_key=None):
    return TripBot(
        bot_token="test-token",
        db=db,
        anthropic_api_key="anthropic-test",
        anthropic_model="claude-test",
        google_maps_api_key=gm_key,
    )


@pytest.mark.asyncio
async def test_autofill_without_gm_key_seeds_curated_data(db_with_user):
    """No GM key: train fares + (no drive/taxi) populated from curated dataset only."""
    bot = _make_bot(db_with_user, gm_key=None)
    result = await bot._auto_fill_transport_options(
        user_id="u1", origin_city="amsterdam", airport_codes=["AMS", "EIN"],
    )
    # Train fares are curated for amsterdam|AMS and amsterdam|EIN.
    ams_opts = db_with_user.get_transport_options("AMS", "u1")
    ams_modes = {o["mode"] for o in ams_opts}
    assert "train" in ams_modes
    assert "drive" not in ams_modes  # no GM key → no drive distance
    assert "taxi" not in ams_modes
    ein_opts = db_with_user.get_transport_options("EIN", "u1")
    assert "train" in {o["mode"] for o in ein_opts}
    # Skip reason flagged.
    assert result["AMS"]["skipped_reason"] == "google_maps_key_missing"


@pytest.mark.asyncio
async def test_autofill_with_gm_key_populates_drive_and_taxi(db_with_user):
    """GM key present: drive + taxi populated alongside curated train."""
    bot = _make_bot(db_with_user, gm_key="test-key")

    from src.apis.google_maps import DirectionsResult

    fake_directions = AsyncMock(side_effect=[
        DirectionsResult(distance_km=20.0, duration_min=25, mode="drive"),    # AMS drive
        DirectionsResult(distance_km=20.0, duration_min=45, mode="transit"),  # AMS transit
        DirectionsResult(distance_km=120.0, duration_min=80, mode="drive"),   # EIN drive
        DirectionsResult(distance_km=120.0, duration_min=120, mode="transit"),# EIN transit
    ])
    with patch.object(
        __import__("src.apis.google_maps", fromlist=["GoogleMapsClient"]).GoogleMapsClient,
        "directions",
        fake_directions,
    ):
        result = await bot._auto_fill_transport_options(
            user_id="u1", origin_city="amsterdam", airport_codes=["AMS", "EIN"],
        )

    ams_opts = db_with_user.get_transport_options("AMS", "u1")
    ams_modes = {o["mode"] for o in ams_opts}
    assert {"train", "drive", "taxi"}.issubset(ams_modes)
    drive = next(o for o in ams_opts if o["mode"] == "drive")
    # 20km × €0.25 = €5
    assert drive["cost_eur"] == 5.0
    assert drive["time_min"] == 25
    assert drive["parking_cost_per_day_eur"] is not None  # AMS is in curated parking
    taxi = next(o for o in ams_opts if o["mode"] == "taxi")
    # 20km × €2.50 = €50
    assert taxi["cost_eur"] == 50.0
    train = next(o for o in ams_opts if o["mode"] == "train")
    # AMS train transit time should be updated from Google Maps (45 min).
    assert train["time_min"] == 45


@pytest.mark.asyncio
async def test_autofill_skips_unknown_airport_gracefully(db_with_user):
    """Airport not in viable_airports_eu list → skipped without crash."""
    bot = _make_bot(db_with_user, gm_key="test-key")

    with patch.object(
        __import__("src.apis.google_maps", fromlist=["GoogleMapsClient"]).GoogleMapsClient,
        "directions",
        AsyncMock(),
    ):
        result = await bot._auto_fill_transport_options(
            user_id="u1", origin_city="amsterdam", airport_codes=["ZZZ"],
        )
    # No options created.
    assert db_with_user.get_transport_options("ZZZ", "u1") == []
    assert result["ZZZ"]["modes"] == []


@pytest.mark.asyncio
async def test_autofill_continues_when_one_airport_fails(db_with_user):
    """If Google Maps fails for one airport, the others still get populated."""
    bot = _make_bot(db_with_user, gm_key="test-key")

    from src.apis.google_maps import GoogleMapsError, DirectionsResult

    call_count = {"n": 0}

    async def maybe_fail(self, *, origin, destination, mode):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            # First airport (AMS) — fails.
            raise GoogleMapsError("simulated network error")
        # Second airport (EIN) — succeeds.
        return DirectionsResult(distance_km=50.0, duration_min=45, mode=mode)

    with patch.object(
        __import__("src.apis.google_maps", fromlist=["GoogleMapsClient"]).GoogleMapsClient,
        "directions",
        maybe_fail,
    ):
        await bot._auto_fill_transport_options(
            user_id="u1", origin_city="amsterdam", airport_codes=["AMS", "EIN"],
        )
    # AMS: only train (curated) — no drive/taxi because GM failed.
    ams_modes = {o["mode"] for o in db_with_user.get_transport_options("AMS", "u1")}
    assert "train" in ams_modes
    assert "drive" not in ams_modes
    # EIN: drive + taxi + train succeeded.
    ein_modes = {o["mode"] for o in db_with_user.get_transport_options("EIN", "u1")}
    assert {"train", "drive", "taxi"}.issubset(ein_modes)
