"""Tests for thin-Telegram format (Option B) — gated on MINIAPP_URL env var.

Verifies:
  - When MINIAPP_URL is set, alert messages are short and use `web_app` buttons.
  - When MINIAPP_URL is unset, falls through to the v0.9.0 rich format (existing
    test_telegram.py covers that path; we just sanity-check the dispatch here).
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from src.alerts.telegram import TelegramNotifier


@pytest.fixture
def notifier():
    """A TelegramNotifier with the network call patched out so we can capture payloads."""
    n = TelegramNotifier(bot_token="test:token")
    sent: list[tuple[str | None, str, dict | None]] = []

    async def fake_send_message(chat_id, text, reply_markup=None, parse_mode="Markdown"):
        sent.append((chat_id, text, reply_markup))

    n._send_message = fake_send_message  # type: ignore[assignment]
    n._sent = sent  # type: ignore[attr-defined]
    return n


@pytest.fixture
def miniapp(monkeypatch):
    """Enable the Mini Web App feature flag for this test."""
    monkeypatch.setenv("MINIAPP_URL", "https://farehound.example.com")
    yield "https://farehound.example.com"


@pytest.fixture
def no_miniapp(monkeypatch):
    monkeypatch.delenv("MINIAPP_URL", raising=False)


# ---------- Thin format ----------


class TestThinDealAlert:
    @pytest.mark.asyncio
    async def test_thin_message_is_short(self, notifier, miniapp):
        deal = {
            "origin": "AMS", "destination": "NRT", "price": 3640, "passengers": 2,
            "score": 0.85, "deal_id": "d_test", "route_id": "r_test",
        }
        await notifier.send_deal_alert(deal, chat_id="42")
        assert len(notifier._sent) == 1
        _, text, _ = notifier._sent[0]
        # Must be short: 2-3 lines.
        line_count = text.count("\n") + 1
        assert line_count <= 3, f"Thin message has {line_count} lines: {text!r}"
        # No rich-format markers
        assert "Cost breakdown" not in text
        assert "Why this is the best" not in text

    @pytest.mark.asyncio
    async def test_thin_uses_web_app_button(self, notifier, miniapp):
        deal = {
            "origin": "AMS", "destination": "NRT", "price": 3640, "passengers": 2,
            "deal_id": "d_test", "route_id": "r_test",
        }
        await notifier.send_deal_alert(deal, chat_id="42")
        _, _, markup = notifier._sent[0]
        assert markup is not None
        kbd = markup["inline_keyboard"]
        # First button on first row should be the web_app launcher
        first = kbd[0][0]
        assert first["text"].startswith("📊")
        assert "web_app" in first
        assert first["web_app"]["url"].startswith("https://farehound.example.com")
        assert first["web_app"]["url"].endswith("/deal/d_test")

    @pytest.mark.asyncio
    async def test_thin_includes_inline_actions(self, notifier, miniapp):
        deal = {
            "origin": "AMS", "destination": "NRT", "price": 3640, "passengers": 2,
            "deal_id": "d_test", "route_id": "r_test",
        }
        await notifier.send_deal_alert(deal, chat_id="42")
        _, _, markup = notifier._sent[0]
        first_row = markup["inline_keyboard"][0]
        labels = [b["text"] for b in first_row]
        # Open + Watching + Skip route are all one-tap and stay inline
        assert any("Watching" in l for l in labels)
        assert any("Skip route" in l for l in labels)


class TestThinErrorFare:
    @pytest.mark.asyncio
    async def test_thin_error_fare_short_with_web_app_button(self, notifier, miniapp):
        deal = {
            "origin": "AMS", "destination": "NRT", "price": 620,
            "deal_id": "d_err", "route_id": "r_err",
        }
        await notifier.send_error_fare_alert(deal, chat_id="42")
        _, text, markup = notifier._sent[0]
        assert "Error fare" in text
        # 2-3 lines max
        assert text.count("\n") <= 2
        # web_app button is present
        assert "web_app" in markup["inline_keyboard"][0][0]


class TestThinFollowUp:
    @pytest.mark.asyncio
    async def test_thin_follow_up_keeps_inline_book_watching(self, notifier, miniapp):
        deal = {
            "origin": "AMS", "destination": "NRT", "price": 3640, "deal_id": "d_fu",
        }
        await notifier.send_follow_up(deal, chat_id="42")
        _, text, markup = notifier._sent[0]
        assert "three days ago" in text
        # First row should have Booked / Watching as one-tap callbacks
        first_row = markup["inline_keyboard"][0]
        labels = [b.get("text", "") for b in first_row]
        assert any("booked" in l.lower() for l in labels)
        assert any("watching" in l.lower() for l in labels)
        # Second row: web_app open
        second_row = markup["inline_keyboard"][1]
        assert "web_app" in second_row[0]


class TestThinDigest:
    @pytest.mark.asyncio
    async def test_thin_digest_is_one_message(self, notifier, miniapp):
        routes = [
            {"origin": "AMS", "destination": "NRT", "lowest_price": 3600, "alert_price": 3700, "passengers": 2},
            {"origin": "AMS", "destination": "BCN", "lowest_price": 200, "alert_price": 220, "passengers": 2},
            {"origin": "AMS", "destination": "LIS", "lowest_price": 410, "alert_price": 410, "passengers": 2},
        ]
        await notifier.send_daily_digest(routes, chat_id="42")
        # Exactly one message — not one-per-route like the rich format
        assert len(notifier._sent) == 1
        _, text, markup = notifier._sent[0]
        assert "FareHound Daily" in text
        assert "3 routes" in text
        # Two prices moved >€10 vs alert
        assert "2 prices moved" in text
        # Single button → /routes
        kbd = markup["inline_keyboard"]
        assert len(kbd) == 1
        assert kbd[0][0]["web_app"]["url"].endswith("/routes")


# ---------- Fallback to rich format ----------


class TestFallbackToRich:
    @pytest.mark.asyncio
    async def test_no_miniapp_url_uses_rich_format(self, notifier, no_miniapp):
        # Same deal — but without MINIAPP_URL, message should be rich (long)
        deal = {
            "origin": "AMS", "destination": "NRT", "price": 3640, "passengers": 2,
            "score": 0.85, "deal_id": "d_test", "route_id": "r_test",
            "primary_transport_cost": 45, "primary_parking_cost": 0,
            "primary_transport_mode": "uber",
        }
        await notifier.send_deal_alert(deal, chat_id="42")
        _, text, _ = notifier._sent[0]
        # Rich format includes the cost breakdown line ("flights" + "total")
        assert "flights" in text.lower()
        assert "total" in text.lower()


# ---------- Empty MINIAPP_URL is treated as unset ----------


class TestEmptyMiniappUrl:
    @pytest.mark.asyncio
    async def test_empty_string_falls_back_to_rich(self, notifier, monkeypatch):
        monkeypatch.setenv("MINIAPP_URL", "   ")  # whitespace-only
        deal = {
            "origin": "AMS", "destination": "NRT", "price": 3640, "passengers": 2,
            "score": 0.85, "deal_id": "d_test", "route_id": "r_test",
            "primary_transport_cost": 45, "primary_parking_cost": 0,
            "primary_transport_mode": "uber",
        }
        await notifier.send_deal_alert(deal, chat_id="42")
        _, text, _ = notifier._sent[0]
        assert "flights" in text.lower()  # rich format
