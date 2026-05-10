"""Tests for src/web/app.py endpoints — HTML rendering + JSON action contracts."""

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.storage.db import Database
from src.storage.models import Deal, PriceSnapshot, Route
from src.web.app import create_app


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh on-disk SQLite per test (in-memory doesn't survive cross-thread executors)."""
    monkeypatch.setenv("FAREHOUND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FAREHOUND_WEB_DEV_BYPASS_AUTH", "1")
    monkeypatch.setenv("FAREHOUND_WEB_DEV_USER_ID", "42")
    d = Database(db_path=tmp_path / "test.db")
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
def seeded(db):
    """Seed a user, two routes, a snapshot, and one deal."""
    user_id = db.create_user(telegram_chat_id="42", name="Barry")
    db.upsert_route(
        Route(
            route_id="ams_nrt_test",
            origin="AMS",
            destination="NRT",
            earliest_departure="2026-10-15",
            latest_return="2026-11-05",
            passengers=2,
            max_stops=1,
            active=True,
        ),
        user_id,
    )
    db.upsert_route(
        Route(
            route_id="ams_bcn_test",
            origin="AMS",
            destination="BCN",
            earliest_departure="2026-09-01",
            latest_return="2026-09-08",
            passengers=2,
            max_stops=1,
            active=True,
        ),
        user_id,
    )
    snap = PriceSnapshot(
        snapshot_id="snap1",
        route_id="ams_nrt_test",
        observed_at=datetime.now(UTC),
        source="serpapi_poll",
        passengers=2,
        lowest_price=Decimal("3640"),
        outbound_date="2026-10-15",
        return_date="2026-11-05",
    )
    db.insert_snapshot(snap, user_id=user_id)
    deal = Deal(
        deal_id="d_ams_nrt",
        snapshot_id="snap1",
        route_id="ams_nrt_test",
        score=Decimal("0.85"),
        urgency="watch",
        reasoning="Good deal — €80 below typical low.",
        alert_sent=True,
        alert_sent_at=datetime.now(UTC),
    )
    db.insert_deal(deal, user_id=user_id)
    return {"user_id": user_id, "deal_id": "d_ams_nrt", "route_id": "ams_nrt_test"}


@pytest.fixture
def client(db, seeded):
    app = create_app(db=db, anthropic_key=None, anthropic_model="test-model")
    return TestClient(app)


# ---------- HTML routes ----------


class TestHtmlEndpoints:
    def test_get_routes_renders(self, client):
        resp = client.get("/routes")
        assert resp.status_code == 200
        assert "AMS → NRT" in resp.text
        assert "AMS → BCN" in resp.text
        assert "monitored" in resp.text

    def test_get_deal_renders(self, client, seeded):
        resp = client.get(f"/deal/{seeded['deal_id']}")
        assert resp.status_code == 200
        assert "AMS → NRT" in resp.text
        assert "Cost breakdown" in resp.text
        assert "Why this is the best" in resp.text

    def test_get_deal_404(self, client):
        resp = client.get("/deal/nonexistent")
        assert resp.status_code == 404

    def test_get_settings_renders(self, client):
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert "Baggage needs" in resp.text
        assert "Quiet hours" in resp.text

    def test_root_redirects_to_routes(self, client):
        # root() falls through to routes() — should render the routes page.
        resp = client.get("/")
        assert resp.status_code == 200
        assert "monitored" in resp.text


# ---------- JSON action endpoints ----------


class TestSnoozeEndpoint:
    def test_snooze_route_clamps_days(self, client, seeded):
        # 999 days → clamped to 365
        resp = client.post(f"/api/routes/{seeded['route_id']}/snooze", json={"days": 999})
        assert resp.status_code == 200
        assert resp.json() == {"snoozed_for_days": 365}

    def test_snooze_negative_days_clamped(self, client, seeded):
        resp = client.post(f"/api/routes/{seeded['route_id']}/snooze", json={"days": -5})
        assert resp.status_code == 200
        assert resp.json() == {"snoozed_for_days": 1}

    def test_snooze_unknown_route_404(self, client):
        resp = client.post("/api/routes/nonexistent/snooze", json={"days": 7})
        assert resp.status_code == 404

    def test_unsnooze_route(self, client, seeded):
        client.post(f"/api/routes/{seeded['route_id']}/snooze", json={"days": 7})
        resp = client.post(f"/api/routes/{seeded['route_id']}/unsnooze")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


class TestFeedbackEndpoint:
    def test_feedback_booked(self, client, db, seeded):
        resp = client.post(
            f"/api/deals/{seeded['deal_id']}/feedback", json={"feedback": "booked"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"feedback": "booked"}

        # Verify it landed in the DB
        row = db._conn.execute(
            "SELECT feedback FROM deals WHERE deal_id = ?", [seeded["deal_id"]]
        ).fetchone()
        assert row[0] == "booked"

    def test_feedback_invalid_value_rejected(self, client, seeded):
        resp = client.post(
            f"/api/deals/{seeded['deal_id']}/feedback", json={"feedback": "garbage"}
        )
        assert resp.status_code == 400


class TestSettingsEndpoint:
    def test_get_settings_json(self, client):
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert "baggage_needs" in body
        assert "transports" in body
        assert "version" in body

    def test_patch_baggage_needs(self, client):
        resp = client.patch("/api/settings", json={"baggage_needs": "two_checked"})
        assert resp.status_code == 200
        assert "baggage_needs" in resp.json()["updated"]

    def test_patch_invalid_baggage_needs_rejected(self, client):
        resp = client.patch("/api/settings", json={"baggage_needs": "garbage"})
        assert resp.status_code == 400


class TestCreateRoute:
    def test_create_route(self, client, db, seeded):
        resp = client.post(
            "/api/routes",
            json={
                "origin": "AMS",
                "destination": "ICN",
                "earliest_departure": "2026-10-01",
                "latest_return": "2026-10-15",
                "passengers": 2,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "route_id" in body
        # And it shows up in the list
        list_resp = client.get("/routes")
        assert "AMS → ICN" in list_resp.text

    def test_create_rejects_short_iata(self, client):
        resp = client.post(
            "/api/routes",
            json={"origin": "AM", "destination": "NRT"},  # AM is invalid
        )
        assert resp.status_code == 400


class TestParseEndpoint:
    def test_parse_returns_400_on_empty_text(self, client):
        resp = client.post("/api/routes/parse", json={"text": ""})
        assert resp.status_code == 400

    def test_parse_503_when_no_anthropic_key(self, client):
        # Fixture sets anthropic_key=None; should return 503
        resp = client.post("/api/routes/parse", json={"text": "Tokyo for 2 weeks"})
        assert resp.status_code == 503


# ---------- Auth gate (verify the bypass actually works in this test setup) ----------


class TestAuthGate:
    def test_html_routes_require_auth_when_bypass_off(self, db, seeded, monkeypatch):
        monkeypatch.delenv("FAREHOUND_WEB_DEV_BYPASS_AUTH", raising=False)
        app = create_app(db=db, anthropic_key=None, anthropic_model="test-model")
        c = TestClient(app)
        resp = c.get("/routes")
        assert resp.status_code == 401
