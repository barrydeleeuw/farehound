"""R9 ITEM-053: web endpoints for editable transport options + override mode."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from src.storage.db import Database
from src.web.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FAREHOUND_WEB_DEV_BYPASS_AUTH", "1")
    monkeypatch.setenv("FAREHOUND_WEB_DEV_USER_ID", "111111")
    db = Database(db_path=tmp_path / "test.db")
    db.init_schema()
    db._conn.execute(
        "INSERT INTO users (user_id, telegram_chat_id, name, onboarded, active, approved) "
        "VALUES (?, ?, ?, 1, 1, 1)",
        ["dev-bypass-user", "111111", "Dev User"],
    )
    db._conn.commit()
    app = create_app(db, anthropic_key=None, anthropic_model=None)
    yield TestClient(app)
    db.close()


def test_get_options_empty_initially(client):
    r = client.get("/api/airports/AMS/options")
    assert r.status_code == 200
    body = r.json()
    assert body["airport_code"] == "AMS"
    assert body["options"] == []
    assert body["override_mode"] is None


def test_post_option_then_get(client):
    r = client.post("/api/airports/AMS/options", json={
        "mode": "drive", "cost_eur": 30, "time_min": 25,
        "parking_cost_per_day_eur": 8,
    })
    assert r.status_code == 200
    assert r.json()["added"] is True

    r = client.get("/api/airports/AMS/options")
    body = r.json()
    assert len(body["options"]) == 1
    assert body["options"][0]["mode"] == "drive"
    assert body["options"][0]["cost_eur"] == 30
    assert body["options"][0]["source"] == "user_added"


def test_post_option_invalid_mode_rejected(client):
    r = client.post("/api/airports/AMS/options", json={
        "mode": "teleport", "cost_eur": 0,
    })
    assert r.status_code == 400


def test_post_option_invalid_airport_code_rejected(client):
    r = client.post("/api/airports/A/options", json={"mode": "drive", "cost_eur": 0})
    assert r.status_code == 400


def test_put_option_updates_fields(client):
    client.post("/api/airports/AMS/options", json={
        "mode": "train", "cost_eur": 15,
    })
    r = client.put("/api/airports/AMS/options/train", json={
        "cost_eur": 18, "time_min": 35,
    })
    assert r.status_code == 200
    r = client.get("/api/airports/AMS/options")
    train = next(o for o in r.json()["options"] if o["mode"] == "train")
    assert train["cost_eur"] == 18
    assert train["time_min"] == 35
    assert train["source"] == "user_override"  # promoted on edit


def test_put_option_disable(client):
    client.post("/api/airports/AMS/options", json={"mode": "taxi", "cost_eur": 50})
    r = client.put("/api/airports/AMS/options/taxi", json={"enabled": False})
    assert r.status_code == 200
    body = client.get("/api/airports/AMS/options").json()
    taxi = next(o for o in body["options"] if o["mode"] == "taxi")
    assert taxi["enabled"] is False


def test_put_nonexistent_option_404(client):
    r = client.put("/api/airports/AMS/options/drive", json={"cost_eur": 10})
    assert r.status_code == 404


def test_delete_option(client):
    client.post("/api/airports/AMS/options", json={"mode": "bus", "cost_eur": 8})
    r = client.delete("/api/airports/AMS/options/bus")
    assert r.status_code == 200
    body = client.get("/api/airports/AMS/options").json()
    assert all(o["mode"] != "bus" for o in body["options"])


def test_post_uber_mode_rejected(client):
    """v0.11.2: uber removed from allowed modes (redundant with taxi)."""
    r = client.post("/api/airports/AMS/options", json={"mode": "uber", "cost_eur": 40})
    assert r.status_code == 400


def test_delete_nonexistent_404(client):
    r = client.delete("/api/airports/AMS/options/drive")
    assert r.status_code == 404


def test_set_and_clear_override_mode(client):
    client.post("/api/airports/AMS/options", json={"mode": "drive", "cost_eur": 30})
    client.post("/api/airports/AMS/options", json={"mode": "train", "cost_eur": 15})
    r = client.put("/api/airports/AMS/override", json={"mode": "drive"})
    assert r.status_code == 200
    body = client.get("/api/airports/AMS/options").json()
    assert body["override_mode"] == "drive"
    # Clear via empty mode.
    r = client.put("/api/airports/AMS/override", json={"mode": None})
    assert r.status_code == 200
    body = client.get("/api/airports/AMS/options").json()
    assert body["override_mode"] is None


def test_set_override_with_invalid_mode_rejected(client):
    r = client.put("/api/airports/AMS/override", json={"mode": "teleport"})
    assert r.status_code == 400


def test_set_override_for_nonexistent_mode_rejected(client):
    """Review #3: cannot set override='drive' if airport has no enabled drive option."""
    client.post("/api/airports/AMS/options", json={"mode": "train", "cost_eur": 15})
    r = client.put("/api/airports/AMS/override", json={"mode": "drive"})
    assert r.status_code == 400
    body = r.json()
    assert "not an enabled option" in body["detail"]


def test_set_override_for_disabled_mode_rejected(client):
    """A disabled mode cannot become the override (would silently fall through to cheapest)."""
    client.post("/api/airports/AMS/options", json={"mode": "train", "cost_eur": 15})
    client.post("/api/airports/AMS/options", json={"mode": "drive", "cost_eur": 30})
    client.put("/api/airports/AMS/options/drive", json={"enabled": False})
    r = client.put("/api/airports/AMS/override", json={"mode": "drive"})
    assert r.status_code == 400


def test_post_negative_cost_clamped(client):
    """Review #7: POST cost_eur is clamped to 0 (was previously asymmetric with PUT)."""
    r = client.post("/api/airports/AMS/options", json={"mode": "drive", "cost_eur": -100})
    assert r.status_code == 200
    body = client.get("/api/airports/AMS/options").json()
    drive = next(o for o in body["options"] if o["mode"] == "drive")
    assert drive["cost_eur"] == 0.0


def test_options_per_airport_isolated(client):
    client.post("/api/airports/AMS/options", json={"mode": "drive", "cost_eur": 30})
    client.post("/api/airports/EIN/options", json={"mode": "train", "cost_eur": 10})
    ams = client.get("/api/airports/AMS/options").json()
    ein = client.get("/api/airports/EIN/options").json()
    assert {o["mode"] for o in ams["options"]} == {"drive"}
    assert {o["mode"] for o in ein["options"]} == {"train"}


# ----- v0.11.3: airport CRUD (suggest, add, delete) -----


def _seed_legacy_airport(client, code, name=None, is_primary=False):
    """Helper: seed a legacy airport_transport row (the test client doesn't go
    through onboarding). The `client` fixture's underlying DB is accessed via
    the FastAPI app state."""
    db = client.app.state.db
    db.seed_airport_transport(
        [{"code": code, "name": name or code, "is_primary": is_primary}],
        user_id="dev-bypass-user",
    )


def test_suggest_returns_closest_airports(client):
    # Set the user's home_airport to AMS so suggest can geocode against it.
    db = client.app.state.db
    db._conn.execute(
        "UPDATE users SET home_airport = 'AMS', home_location = 'amsterdam' WHERE user_id = ?",
        ["dev-bypass-user"],
    )
    db._conn.commit()
    _seed_legacy_airport(client, "AMS", "Schiphol", is_primary=True)
    r = client.get("/api/airports/suggest?limit=4")
    assert r.status_code == 200
    body = r.json()
    assert body["home_code"] == "AMS"
    iatas = [c["iata"] for c in body["candidates"]]
    assert "AMS" not in iatas  # home is excluded
    assert len(iatas) <= 4
    # Closest known: Rotterdam (RTM), Eindhoven (EIN), Brussels (BRU).
    assert any(c in iatas for c in ("RTM", "EIN", "BRU"))


def test_suggest_excludes_already_configured(client):
    db = client.app.state.db
    db._conn.execute(
        "UPDATE users SET home_airport = 'AMS', home_location = 'amsterdam' WHERE user_id = ?",
        ["dev-bypass-user"],
    )
    db._conn.commit()
    _seed_legacy_airport(client, "AMS", "Schiphol", is_primary=True)
    _seed_legacy_airport(client, "EIN", "Eindhoven")
    r = client.get("/api/airports/suggest?limit=4")
    iatas = [c["iata"] for c in r.json()["candidates"]]
    assert "EIN" not in iatas  # already configured


def test_suggest_rejects_unknown_home_airport(client):
    db = client.app.state.db
    db._conn.execute(
        "UPDATE users SET home_airport = 'XYZ' WHERE user_id = ?",
        ["dev-bypass-user"],
    )
    db._conn.commit()
    r = client.get("/api/airports/suggest")
    assert r.status_code == 400


def test_post_airport_validates_iata_format(client):
    r = client.post("/api/airports", json={"code": "ABCD"})
    assert r.status_code == 400


def test_post_airport_rejects_unknown_iata(client):
    r = client.post("/api/airports", json={"code": "ZZZ"})
    assert r.status_code == 400
    assert "viable-airports" in r.json()["detail"]


def test_post_airport_rejects_already_configured(client):
    _seed_legacy_airport(client, "AMS", "Schiphol", is_primary=True)
    r = client.post("/api/airports", json={"code": "AMS"})
    assert r.status_code == 409


def test_post_airport_adds_legacy_row_without_bot(client):
    """No trip_bot in app.state → autofill_skipped='bot_unavailable' but legacy row added."""
    r = client.post("/api/airports", json={"code": "EIN"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "EIN"
    assert body["modes_added"] == []
    assert body["autofill_skipped"] == "bot_unavailable"
    # Legacy row exists.
    db = client.app.state.db
    legacy = db.get_airport_transport("EIN", user_id="dev-bypass-user")
    assert legacy is not None
    assert legacy["is_primary"] is False


def test_delete_airport(client):
    _seed_legacy_airport(client, "EIN", "Eindhoven")
    client.post("/api/airports/EIN/options", json={"mode": "drive", "cost_eur": 30})
    client.post("/api/airports/EIN/options", json={"mode": "train", "cost_eur": 15})
    r = client.delete("/api/airports/EIN")
    assert r.status_code == 200
    db = client.app.state.db
    assert db.get_airport_transport("EIN", user_id="dev-bypass-user") is None
    assert db.get_transport_options("EIN", "dev-bypass-user", include_disabled=True) == []


def test_delete_airport_refuses_primary(client):
    _seed_legacy_airport(client, "AMS", "Schiphol", is_primary=True)
    r = client.delete("/api/airports/AMS")
    assert r.status_code == 400
    assert "primary" in r.json()["detail"].lower()


def test_delete_airport_404_if_missing(client):
    r = client.delete("/api/airports/EIN")
    assert r.status_code == 404
