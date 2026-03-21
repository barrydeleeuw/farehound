from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.alerts.telegram import TelegramNotifier, TELEGRAM_API


@pytest.fixture
def notifier():
    return TelegramNotifier(bot_token="123:ABC", chat_id="-100999")


# --- init ---

def test_telegram_notifier_init():
    n = TelegramNotifier(bot_token="tok", chat_id="cid")
    assert n._bot_token == "tok"
    assert n._chat_id == "cid"


# --- send_deal_alert ---

@pytest.mark.asyncio
async def test_send_deal_alert_format(notifier):
    deal_info = {
        "origin": "AMS",
        "destination": "NRT",
        "price": 485,
        "score": 0.88,
        "reasoning": "Great deal",
        "airline": "KLM",
        "dates": "2026-10-01 to 2026-10-15",
    }

    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await notifier.send_deal_alert(deal_info)

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        payload = call_args.kwargs.get("json") or call_args[1]["json"]

        assert "123:ABC" in url
        assert payload["chat_id"] == "-100999"
        assert "AMS" in payload["text"]
        assert "NRT" in payload["text"]
        assert "€485" in payload["text"]
        assert "0.88" in payload["text"]
        assert "Search Flights" in payload["text"]
        assert payload["parse_mode"] == "Markdown"
        assert payload["disable_web_page_preview"] is True


# --- send_error_fare_alert ---

@pytest.mark.asyncio
async def test_send_error_fare_alert_format(notifier):
    deal_info = {
        "origin": "AMS",
        "destination": "NRT",
        "price": 200,
        "score": 0.95,
        "reasoning": "Error fare confirmed",
        "airline": "QR",
        "dates": "2026-10-01 to 2026-10-15",
    }

    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await notifier.send_error_fare_alert(deal_info)

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        text = payload["text"]
        assert "Error Fare" in text
        assert "BOOK NOW" in text
        assert "€200" in text
        assert "Book Now" in text


# --- send_daily_digest ---

@pytest.mark.asyncio
async def test_send_daily_digest_format(notifier):
    routes = [
        {"origin": "AMS", "destination": "NRT", "lowest_price": 485, "trend": "down"},
        {"origin": "AMS", "destination": "IST", "lowest_price": 200, "trend": "stable"},
    ]

    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await notifier.send_daily_digest(routes)

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        text = payload["text"]
        assert "FareHound Daily" in text
        assert "2 route(s)" in text
        assert "AMS→NRT" in text
        assert "€485" in text
        assert "↓" in text
        assert "AMS→IST" in text
        assert "→" in text  # stable trend


@pytest.mark.asyncio
async def test_send_daily_digest_empty(notifier):
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await notifier.send_daily_digest([])
        mock_client.post.assert_not_called()


# --- _google_flights_url ---

def test_google_flights_url(notifier):
    url = notifier._google_flights_url({
        "origin": "AMS",
        "destination": "NRT",
        "outbound_date": "2026-10-01",
        "return_date": "2026-10-15",
    })
    assert "AMS" in url
    assert "NRT" in url
    assert "2026-10-01" in url
    assert "2026-10-15" in url
    assert url.startswith("https://www.google.com/travel/flights")
