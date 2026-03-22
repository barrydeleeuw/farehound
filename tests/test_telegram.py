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
        assert "Amsterdam" in payload["text"]
        assert "Tokyo Narita" in payload["text"]
        assert "€485" in payload["text"]
        assert "Good Deal" in payload["text"]
        assert payload["parse_mode"] == "Markdown"
        assert payload["disable_web_page_preview"] is True


# --- send_deal_alert with nearby ---

@pytest.mark.asyncio
async def test_send_deal_alert_with_nearby(notifier):
    deal_info = {
        "origin": "AMS",
        "destination": "NRT",
        "price": 1940,
        "score": 0.85,
        "reasoning": "Good deal",
        "airline": "KLM",
        "dates": "2026-10-01 to 2026-10-15",
        "nearby_comparison": [
            {
                "airport_code": "BRU",
                "airport_name": "Brussels",
                "fare_pp": 1600.0,
                "net_cost": 3270.0,
                "savings": 610.0,
                "transport_mode": "Thalys",
                "transport_cost": 70.0,
                "transport_time_min": 150,
            },
            {
                "airport_code": "DUS",
                "airport_name": "Dusseldorf",
                "fare_pp": 1750.0,
                "net_cost": 3560.0,
                "savings": 320.0,
                "transport_mode": "train",
                "transport_cost": 60.0,
                "transport_time_min": 168,
            },
        ],
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

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        text = payload["text"]
        assert "🟢 Brussels" in text
        assert "save €610" in text
        assert "Thalys" in text
        assert "🟡 Dusseldorf" in text
        assert "save €320" in text


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
        assert "Search Flights" in text or "Book Now" in text


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
        assert "Amsterdam → Tokyo Narita" in text
        assert "€485" in text
        assert "📉" in text
        assert "Amsterdam → Istanbul" in text
        assert "➡️" in text  # stable trend


@pytest.mark.asyncio
async def test_send_daily_digest_with_nearby(notifier):
    routes = [
        {
            "origin": "AMS",
            "destination": "NRT",
            "lowest_price": 1940,
            "trend": "down",
            "passengers": 2,
            "nearby_prices": [
                {
                    "airport_code": "BRU",
                    "airport_name": "Brussels",
                    "fare_pp": 1600.0,
                    "net_cost": 3270.0,
                    "savings": 610.0,
                },
            ],
        },
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
        assert "Amsterdam" in text
        assert "Brussels" in text
        assert "€1,600/pp" in text
        assert "save €610" in text


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


# --- send_deal_alert inline keyboard ---

@pytest.mark.asyncio
async def test_send_deal_alert_buttons(notifier):
    """Deal alert buttons: Search Flights (URL) + Wait (callback)."""
    deal_info = {
        "origin": "AMS",
        "destination": "NRT",
        "price": 485,
        "score": 0.88,
        "airline": "KLM",
        "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_123",
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

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        keyboard = payload["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 1
        row = keyboard[0]
        assert row[0]["text"] == "Search Flights ✈️"
        assert "url" in row[0]
        assert row[0]["url"].startswith("https://www.google.com/travel/flights")
        assert row[1]["text"] == "Wait 🕐"
        assert row[1]["callback_data"] == "wait:deal_123"


# --- send_error_fare_alert inline keyboard ---

@pytest.mark.asyncio
async def test_send_error_fare_alert_buttons(notifier):
    """Error fare alert buttons: Search Flights (URL) + Wait (callback)."""
    deal_info = {
        "origin": "AMS",
        "destination": "NRT",
        "price": 200,
        "score": 0.95,
        "airline": "QR",
        "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_456",
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
        keyboard = payload["reply_markup"]["inline_keyboard"]
        row = keyboard[0]
        assert row[0]["text"] == "Search Flights ✈️"
        assert "url" in row[0]
        assert row[1]["text"] == "Wait 🕐"
        assert row[1]["callback_data"] == "wait:deal_456"


# --- send_follow_up ---

@pytest.mark.asyncio
async def test_send_follow_up(notifier):
    """Follow-up message has booked/watching buttons."""
    deal_info = {
        "origin": "AMS",
        "destination": "NRT",
        "price": 485,
        "deal_id": "deal_789",
    }

    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await notifier.send_follow_up(deal_info)

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        text = payload["text"]
        assert "Amsterdam" in text
        assert "Tokyo Narita" in text
        assert "€485" in text
        assert "three days ago" in text
        keyboard = payload["reply_markup"]["inline_keyboard"]
        row = keyboard[0]
        assert row[0]["text"] == "Yes, booked ✅"
        assert row[0]["callback_data"] == "booked:deal_789"
        assert row[1]["text"] == "Still watching 👀"
        assert row[1]["callback_data"] == "watching:deal_789"
