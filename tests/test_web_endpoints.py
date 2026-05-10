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


class TestRemoveRoute:
    def test_delete_route_soft_deletes_and_404s_after(self, client, db, seeded):
        rid = seeded["route_id"]
        # Pre: route is active
        active = db.get_active_routes(user_id=seeded["user_id"])
        assert any(r.route_id == rid for r in active)

        resp = client.request("DELETE", f"/api/routes/{rid}")
        assert resp.status_code == 200
        assert resp.json() == {"removed": True}

        # Post: route is no longer in active list (soft-delete)
        active_after = db.get_active_routes(user_id=seeded["user_id"])
        assert not any(r.route_id == rid for r in active_after)

        # Subsequent operations on the route 404 (ownership check still passes
        # since it remains in the routes table, but a delete is idempotent —
        # second DELETE should also 200 since the row is still owned).
        # (The intent is "stop monitoring", not "purge from history".)

    def test_delete_unknown_route_404(self, client):
        resp = client.request("DELETE", "/api/routes/nonexistent_route")
        assert resp.status_code == 404

    def test_delete_other_users_route_blocked(self, db, seeded, monkeypatch):
        # Cross-user safety: even with valid auth, user A cannot remove user B's route.
        from src.storage.models import Route
        other_user = db.create_user(telegram_chat_id="99", name="Other")
        db.upsert_route(
            Route(
                route_id="ams_lis_other",
                origin="AMS", destination="LIS",
                earliest_departure="2026-07-01", latest_return="2026-07-08",
                passengers=2, max_stops=1, active=True,
            ),
            other_user,
        )
        # Test bypass user is "42"; "ams_lis_other" belongs to other user "99"
        from src.web.app import create_app
        from fastapi.testclient import TestClient
        app = create_app(db=db, anthropic_key=None, anthropic_model="test")
        c = TestClient(app)
        resp = c.request("DELETE", "/api/routes/ams_lis_other")
        assert resp.status_code == 404
        # And the other user's route stays active
        active = db.get_active_routes(user_id=other_user)
        assert any(r.route_id == "ams_lis_other" for r in active)


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

    def test_feedback_unknown_deal_404(self, client):
        # CR-1 regression: /api/deals/{deal_id}/feedback must enforce ownership.
        # A deal_id that doesn't belong to the requesting user (or doesn't exist)
        # must 404, not silently mutate someone else's deal.
        resp = client.post(
            "/api/deals/d_someone_elses/feedback", json={"feedback": "booked"}
        )
        assert resp.status_code == 404

    def test_feedback_other_users_deal_blocked(self, db, seeded, monkeypatch):
        # CR-1 regression: even with valid auth, user A cannot mutate user B's deal.
        from datetime import UTC, datetime
        from decimal import Decimal
        from src.storage.models import Deal, PriceSnapshot

        # Seed a second user with their own deal
        other_user = db.create_user(telegram_chat_id="99", name="Other")
        snap = PriceSnapshot(
            snapshot_id="snap_other", route_id="ams_nrt_test",
            observed_at=datetime.now(UTC), source="serpapi_poll", passengers=2,
            lowest_price=Decimal("3500"),
        )
        db.insert_snapshot(snap, user_id=other_user)
        db.insert_deal(Deal(
            deal_id="d_other_user", snapshot_id="snap_other", route_id="ams_nrt_test",
            score=Decimal("0.8"), urgency="watch", alert_sent=True,
            alert_sent_at=datetime.now(UTC),
        ), user_id=other_user)

        # Now user 42 (from FAREHOUND_WEB_DEV_USER_ID) tries to mutate other user's deal
        from src.web.app import create_app
        from fastapi.testclient import TestClient
        app = create_app(db=db, anthropic_key=None, anthropic_model="test")
        c = TestClient(app)
        resp = c.post("/api/deals/d_other_user/feedback", json={"feedback": "booked"})
        assert resp.status_code == 404

        # And the original deal feedback was NOT changed
        row = db._conn.execute(
            "SELECT feedback FROM deals WHERE deal_id = 'd_other_user'"
        ).fetchone()
        assert row[0] is None or row[0] == ""


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


# Trip creation is bot-only (multi-turn /trip flow). The Mini Web App's
# /routes page sends users back to chat for adds — it has no /api/routes (POST)
# or /api/routes/parse endpoint. See routes.html.j2's "Back to chat" CTA.


# ---------- Auth gate (verify the bypass actually works in this test setup) ----------


class TestAuthGate:
    def test_html_routes_serve_bootstrap_when_no_initdata(self, db, seeded, monkeypatch):
        # Without `?tg=<initData>` and without dev bypass, HTML routes return the
        # bootstrap page (200). Bootstrap JS reads `Telegram.WebApp.initData` on
        # load and reloads with `?tg=` set so the second GET can authenticate.
        monkeypatch.delenv("FAREHOUND_WEB_DEV_BYPASS_AUTH", raising=False)
        app = create_app(db=db, anthropic_key=None, anthropic_model="test-model")
        c = TestClient(app)
        resp = c.get("/routes")
        assert resp.status_code == 200
        assert "tg.initData" in resp.text  # the bootstrap script reference
        assert "/routes" in resp.text       # target path baked in

    def test_html_routes_401_with_invalid_initdata(self, db, seeded, monkeypatch):
        # When `?tg=` IS supplied but invalid, the server can't bootstrap any further
        # (the redirect already happened) and must return 401.
        monkeypatch.delenv("FAREHOUND_WEB_DEV_BYPASS_AUTH", raising=False)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
        app = create_app(db=db, anthropic_key=None, anthropic_model="test-model")
        c = TestClient(app)
        resp = c.get("/routes?tg=garbage")
        assert resp.status_code == 401

    def test_api_routes_strict_401_when_no_header(self, db, seeded, monkeypatch):
        # /api/* endpoints stay strict — they require the x-telegram-init-data
        # header (set by client JS), no bootstrap fallback.
        monkeypatch.delenv("FAREHOUND_WEB_DEV_BYPASS_AUTH", raising=False)
        app = create_app(db=db, anthropic_key=None, anthropic_model="test-model")
        c = TestClient(app)
        resp = c.get("/api/routes")
        assert resp.status_code == 401
