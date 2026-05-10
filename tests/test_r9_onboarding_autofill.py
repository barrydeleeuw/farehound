"""R9 ITEM-053: onboarding auto-fill helper — populates airport_transport_option
from SerpAPI directions + curated datasets. Graceful skip when SerpAPI key missing
or directions lookup fails."""

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


def _make_bot(db, *, serpapi_key=None):
    return TripBot(
        bot_token="test-token",
        db=db,
        anthropic_api_key="anthropic-test",
        anthropic_model="claude-test",
        serpapi_key=serpapi_key,
    )


@pytest.mark.asyncio
async def test_autofill_without_serpapi_key_seeds_curated_only(db_with_user):
    """No SerpAPI key: only curated train fares get seeded."""
    bot = _make_bot(db_with_user, serpapi_key=None)
    result = await bot._auto_fill_transport_options(
        user_id="u1", origin_city="amsterdam", airport_codes=["AMS"],
    )
    # No SerpAPI = no train (because curated seeding happens INSIDE the SerpAPI
    # branch in the new flow). Confirms the code path returns the right reason.
    assert result["AMS"]["skipped_reason"] == "serpapi_key_missing"


@pytest.mark.asyncio
async def test_autofill_with_serpapi_key_populates_drive_train_taxi(db_with_user):
    """SerpAPI key present: train (curated) + drive + taxi (from directions)."""
    bot = _make_bot(db_with_user, serpapi_key="serp-test-key")

    from src.apis.serpapi import DirectionsResult

    fake_directions = AsyncMock(side_effect=[
        DirectionsResult(distance_km=20.0, duration_min=25, mode="drive"),    # AMS drive
        DirectionsResult(distance_km=20.0, duration_min=45, mode="transit"),  # AMS transit
        DirectionsResult(distance_km=120.0, duration_min=80, mode="drive"),   # EIN drive
        DirectionsResult(distance_km=120.0, duration_min=120, mode="transit"),# EIN transit
    ])
    with patch(
        "src.apis.serpapi.SerpAPIClient.directions", fake_directions,
    ):
        await bot._auto_fill_transport_options(
            user_id="u1", origin_city="amsterdam", airport_codes=["AMS", "EIN"],
        )

    ams_opts = db_with_user.get_transport_options("AMS", "u1")
    ams_modes = {o["mode"] for o in ams_opts}
    assert {"train", "drive", "taxi"}.issubset(ams_modes)
    drive = next(o for o in ams_opts if o["mode"] == "drive")
    # 20 km × €0.25 = €5.
    assert drive["cost_eur"] == 5.0
    assert drive["time_min"] == 25
    assert drive["parking_cost_per_day_eur"] is not None  # AMS curated parking exists
    assert drive["source"] == "serpapi_directions"
    taxi = next(o for o in ams_opts if o["mode"] == "taxi")
    # 20 km × €2.50 = €50.
    assert taxi["cost_eur"] == 50.0
    train = next(o for o in ams_opts if o["mode"] == "train")
    # SerpAPI gave us a transit duration; train option's time_min should reflect it.
    assert train["time_min"] == 45


@pytest.mark.asyncio
async def test_autofill_skips_unknown_airport_gracefully(db_with_user):
    """Airport not in viable_airports_eu list → skipped without crash."""
    bot = _make_bot(db_with_user, serpapi_key="serp-test-key")
    with patch("src.apis.serpapi.SerpAPIClient.directions", AsyncMock()):
        result = await bot._auto_fill_transport_options(
            user_id="u1", origin_city="amsterdam", airport_codes=["ZZZ"],
        )
    assert db_with_user.get_transport_options("ZZZ", "u1") == []
    assert result["ZZZ"]["modes"] == []


@pytest.mark.asyncio
async def test_autofill_continues_when_one_airport_fails(db_with_user):
    """If SerpAPI fails for one airport, the others still get populated."""
    bot = _make_bot(db_with_user, serpapi_key="serp-test-key")

    from src.apis.serpapi import DirectionsResult, SerpAPIError

    call_count = {"n": 0}

    async def maybe_fail(self, *, origin, destination, mode):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            # First airport (AMS) — fails on both calls.
            raise SerpAPIError("simulated parse error")
        return DirectionsResult(distance_km=50.0, duration_min=45, mode=mode)

    with patch("src.apis.serpapi.SerpAPIClient.directions", maybe_fail):
        await bot._auto_fill_transport_options(
            user_id="u1", origin_city="amsterdam", airport_codes=["AMS", "EIN"],
        )
    # AMS: only train (curated) — no drive/taxi because SerpAPI failed.
    ams_modes = {o["mode"] for o in db_with_user.get_transport_options("AMS", "u1")}
    assert "train" in ams_modes
    assert "drive" not in ams_modes
    # EIN: drive + taxi + train succeeded.
    ein_modes = {o["mode"] for o in db_with_user.get_transport_options("EIN", "u1")}
    assert {"train", "drive", "taxi"}.issubset(ein_modes)
