from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.alerts.homeassistant import HomeAssistantNotifier


@pytest.fixture
def notifier():
    return HomeAssistantNotifier(
        notify_service="notify.mobile_app_phone",
        base_url="http://ha.local:8123",
        token="test-token",
    )


# --- send_deal_alert ---

@pytest.mark.asyncio
async def test_send_deal_alert_includes_deal_id(notifier):
    deal_info = {
        "deal_id": "abc123",
        "origin": "AMS",
        "destination": "NRT",
        "price": 485,
        "score": 0.88,
        "reasoning": "Great deal",
        "airline": "KLM",
        "dates": "2026-10-01 to 2026-10-15",
    }

    with patch("src.alerts.homeassistant.httpx.AsyncClient") as mock_cls:
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
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["data"]["tag"] == "farehound-deal-abc123"
        assert "abc123" in payload["title"] or "AMS" in payload["title"]


@pytest.mark.asyncio
async def test_send_deal_alert_dismiss_action(notifier):
    deal_info = {
        "deal_id": "xyz789",
        "origin": "AMS",
        "destination": "NRT",
        "price": 485,
        "score": 0.88,
        "reasoning": "Good price",
        "airline": "KLM",
        "dates": "2026-10-01 to 2026-10-15",
    }

    with patch("src.alerts.homeassistant.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await notifier.send_deal_alert(deal_info)

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        actions = payload["data"]["actions"]
        action_names = [a["action"] for a in actions]
        assert "URI" in action_names
        assert "DISMISS_DEAL_xyz789" in action_names
        # Verify dismiss button title
        dismiss_action = next(a for a in actions if a["action"].startswith("DISMISS_DEAL_"))
        assert dismiss_action["title"] == "Not Interested"


# --- send_error_fare_alert ---

@pytest.mark.asyncio
async def test_send_error_fare_alert_has_dismiss(notifier):
    deal_info = {
        "deal_id": "err001",
        "origin": "AMS",
        "destination": "NRT",
        "price": 200,
        "score": 0.95,
        "reasoning": "Error fare!",
        "airline": "QR",
        "dates": "2026-10-01 to 2026-10-15",
    }

    with patch("src.alerts.homeassistant.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await notifier.send_error_fare_alert(deal_info)

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        assert "Error Fare" in payload["title"]
        assert payload["data"]["priority"] == "high"
        actions = payload["data"]["actions"]
        assert any(a["action"] == "DISMISS_DEAL_err001" for a in actions)
        assert any(a["action"] == "URI" for a in actions)


# --- update_sensors ---

@pytest.mark.asyncio
async def test_update_sensors_payload(notifier):
    routes_summary = [
        {
            "route_id": "ams-nrt",
            "origin": "AMS",
            "destination": "NRT",
            "lowest_price": 485.0,
            "currency": "EUR",
            "trend": "down",
            "last_checked": "2026-03-21T10:00:00",
            "deal_score": 0.88,
        },
    ]

    with patch("src.alerts.homeassistant.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await notifier.update_sensors(routes_summary)

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        payload = call_args.kwargs.get("json") or call_args[1]["json"]

        assert "sensor.farehound_ams_nrt_price" in url
        assert payload["state"] == "485.0"
        attrs = payload["attributes"]
        assert attrs["route_name"] == "AMS → NRT"
        assert attrs["trend"] == "↓ dropping"
        assert attrs["icon"] == "mdi:airplane"
        assert attrs["unit_of_measurement"] == "EUR"


@pytest.mark.asyncio
async def test_update_sensors_unknown_price(notifier):
    routes_summary = [
        {
            "route_id": "ams-ist",
            "origin": "AMS",
            "destination": "IST",
            "lowest_price": None,
            "currency": "EUR",
            "trend": "",
            "last_checked": "",
            "deal_score": None,
        },
    ]

    with patch("src.alerts.homeassistant.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await notifier.update_sensors(routes_summary)

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        assert payload["state"] == "unknown"


# --- handle_notification_action ---

@pytest.mark.asyncio
async def test_handle_notification_action_dismiss(notifier):
    result = await notifier.handle_notification_action("DISMISS_DEAL_abc123", "abc123")
    assert result == "dismissed"


@pytest.mark.asyncio
async def test_handle_notification_action_book(notifier):
    result = await notifier.handle_notification_action("BOOK_NOW", "abc123")
    assert result == "booked"


@pytest.mark.asyncio
async def test_handle_notification_action_unknown(notifier):
    result = await notifier.handle_notification_action("SOMETHING_ELSE", "abc123")
    assert result is None
