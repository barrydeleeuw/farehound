from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.alerts.telegram import TelegramNotifier, TELEGRAM_API


@pytest.fixture
def notifier():
    return TelegramNotifier(bot_token="123:ABC")


CHAT_ID = "-100999"


# --- init ---

def test_telegram_notifier_init():
    n = TelegramNotifier(bot_token="tok")
    assert n._bot_token == "tok"


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

        await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        payload = call_args.kwargs.get("json") or call_args[1]["json"]

        assert "123:ABC" in url
        assert payload["chat_id"] == CHAT_ID
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

        await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        text = payload["text"]
        assert "Brussels" in text
        assert "save €610" in text
        assert "Thalys" in text
        assert "Dusseldorf" in text
        assert "save €320" in text
        assert "total" in text


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

        await notifier.send_error_fare_alert(deal_info, chat_id=CHAT_ID)

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

        await notifier.send_daily_digest(routes, chat_id=CHAT_ID)

        # 3 messages: header + 2 routes
        assert mock_client.post.call_count == 3
        calls = [c.kwargs.get("json") or c[1]["json"] for c in mock_client.post.call_args_list]
        header = calls[0]["text"]
        assert "FareHound Daily" in header
        assert "2 route(s)" in header
        route1 = calls[1]["text"]
        assert "Amsterdam → Tokyo Narita" in route1
        assert "€485" in route1
        assert "📉" in route1
        route2 = calls[2]["text"]
        assert "Amsterdam → Istanbul" in route2
        assert "➡️" in route2


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

        await notifier.send_daily_digest(routes, chat_id=CHAT_ID)

        # 2 messages: header + 1 route
        assert mock_client.post.call_count == 2
        route_payload = mock_client.post.call_args_list[1].kwargs.get("json") or mock_client.post.call_args_list[1][1]["json"]
        text = route_payload["text"]
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

        await notifier.send_daily_digest([], chat_id=CHAT_ID)
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
    """Deal alert R7 keyboard: Book Now (URL) + Watching (callback) + Skip route + Details row."""
    deal_info = {
        "origin": "AMS",
        "destination": "NRT",
        "price": 485,
        "score": 0.88,
        "airline": "KLM",
        "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_123",
        "route_id": "route_abc",
    }

    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        keyboard = payload["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 2
        row = keyboard[0]
        assert row[0]["text"] == "Book Now ✈️"
        assert row[0]["url"].startswith("https://www.google.com/travel/flights")
        assert row[1]["text"] == "Watching 👀"
        assert row[1]["callback_data"] == "deal:watch:deal_123"
        assert row[2]["text"] == "Skip route 🔕"
        assert row[2]["callback_data"] == "route:snooze:7:route_abc"
        # Row 2: Details placeholder.
        assert keyboard[1][0]["text"] == "📊 Details"


# --- send_error_fare_alert inline keyboard ---

@pytest.mark.asyncio
async def test_send_error_fare_alert_buttons(notifier):
    """Error fare R7 keyboard: Book Now (URL) + Watching (callback) + Skip route."""
    deal_info = {
        "origin": "AMS",
        "destination": "NRT",
        "price": 200,
        "score": 0.95,
        "airline": "QR",
        "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_456",
        "route_id": "route_xyz",
    }

    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await notifier.send_error_fare_alert(deal_info, chat_id=CHAT_ID)

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        keyboard = payload["reply_markup"]["inline_keyboard"]
        row = keyboard[0]
        assert row[0]["text"] == "Book Now ✈️"
        assert row[1]["text"] == "Watching 👀"
        assert row[1]["callback_data"] == "deal:watch:deal_456"
        assert row[2]["callback_data"] == "route:snooze:7:route_xyz"


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

        await notifier.send_follow_up(deal_info, chat_id=CHAT_ID)

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        text = payload["text"]
        assert "Amsterdam" in text
        assert "Tokyo Narita" in text
        assert "€485" in text
        assert "three days ago" in text
        keyboard = payload["reply_markup"]["inline_keyboard"]
        row = keyboard[0]
        assert row[0]["text"] == "Yes, booked ✅"
        assert row[0]["callback_data"] == "deal:book:deal_789"
        assert row[1]["text"] == "Still watching 👀"
        assert row[1]["callback_data"] == "deal:watch:deal_789"


# =============================================================================
# T14 — R7 (ITEM-051): all 4 message types unified
# =============================================================================

from src.alerts.telegram import (
    _render_reasoning_bullets,
    _render_transparency_footer,
    _render_date_transparency,
    _format_cost_breakdown,
    _baggage_total,
)


def _mock_http():
    """Build a context-manager mock for httpx.AsyncClient."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _payload_of(call):
    """Extract the JSON payload from a httpx.post mock call."""
    return call.kwargs.get("json") or call[1]["json"]


# --- Render helpers (unit) ---

class TestRenderReasoningBullets:

    def test_three_field_dict_renders_three_lines(self):
        reasoning = {
            "vs_dates": "Cheapest of 4 dates polled",
            "vs_range": "€80 below typical low",
            "vs_nearby": "AMS is best",
        }
        lines = _render_reasoning_bullets(reasoning, "ignored legacy text")
        assert len(lines) == 3
        for line in lines:
            assert line.startswith("✓ ")

    def test_legacy_string_with_newlines_returned_as_lines(self):
        legacy = "✓ bullet1\n✓ bullet2\n✓ bullet3"
        lines = _render_reasoning_bullets(None, legacy)
        assert len(lines) == 3
        assert "bullet2" in lines[1]

    def test_legacy_free_text_wrapped_in_italics(self):
        lines = _render_reasoning_bullets(None, "Single-line legacy reasoning")
        assert lines == ["_Single-line legacy reasoning_"]

    def test_no_reasoning_returns_empty(self):
        assert _render_reasoning_bullets(None, "") == []
        assert _render_reasoning_bullets(None, None) == []

    def test_dict_takes_precedence_over_legacy(self):
        """When both reasoning_json AND legacy string are present, structured wins."""
        reasoning = {"vs_dates": "x", "vs_range": "y", "vs_nearby": "z"}
        lines = _render_reasoning_bullets(reasoning, "legacy fallback")
        assert "_legacy fallback_" not in lines
        assert any("x" in l for l in lines)


class TestRenderTransparencyFooter:

    def test_competitive_only_returns_none(self):
        """Existing nearby block already shows competitive — no extra footer."""
        competitive = [{"airport_code": "BRU", "savings": 120}]
        assert _render_transparency_footer(competitive, [{"airport_code": "BRU"}]) is None

    def test_both_empty_returns_none(self):
        """No polling happened → no footer."""
        assert _render_transparency_footer([], []) is None

    def test_all_saved_none_competitive(self):
        """All evaluated, none competitive → '✓ Checked X — your airport is best'."""
        evaluated = [
            {"airport_code": "EIN", "delta_vs_primary": 30},
            {"airport_code": "BRU", "delta_vs_primary": 70},
        ]
        footer = _render_transparency_footer([], evaluated)
        assert footer is not None
        assert "Checked 2 airports" in footer
        assert "best by €30–€70" in footer

    def test_all_saved_singular_count(self):
        evaluated = [{"airport_code": "EIN", "delta_vs_primary": 40}]
        footer = _render_transparency_footer([], evaluated)
        assert "Checked 1 airport" in footer  # singular
        assert "best by €40" in footer

    def test_mixed_some_competitive_some_not(self):
        """Some saved, some too expensive → '…also checked X (€Y+ more, skipped)'."""
        competitive = [{"airport_code": "BRU", "savings": 120}]
        evaluated = [
            {"airport_code": "BRU", "delta_vs_primary": -120},  # competitive (negative = cheaper)
            {"airport_code": "EIN", "airport_name": "Eindhoven", "delta_vs_primary": 30},
        ]
        footer = _render_transparency_footer(competitive, evaluated)
        assert footer is not None
        assert "Eindhoven" in footer
        assert "skipped" in footer


class TestRenderDateTransparency:

    def test_polled_n_dates_line(self):
        history = [
            ("2026-10-01", 500),
            ("2026-10-08", 470),
            ("2026-10-15", 520),
        ]
        line = _render_date_transparency(history)
        assert line is not None
        assert "Polled 3 dates" in line
        assert "Oct 08" in line  # cheapest

    def test_empty_history_returns_none(self):
        assert _render_date_transparency([]) is None
        assert _render_date_transparency(None) is None

    def test_dict_format_supported(self):
        history = [
            {"date": "2026-10-01", "price": 500},
            {"date": "2026-10-08", "price": 470},
        ]
        line = _render_date_transparency(history)
        assert "Polled 2 dates" in line


class TestBaggageTotal:

    def test_unknown_source_returns_zero(self):
        baggage = {
            "outbound": {"carry_on": 25, "checked": 40},
            "return": {"carry_on": 25, "checked": 40},
            "source": "unknown",
        }
        assert _baggage_total(baggage, passengers=2) == 0.0

    def test_serpapi_source_sums_both_directions_times_passengers(self):
        baggage = {
            "outbound": {"carry_on": 0, "checked": 40},
            "return": {"carry_on": 0, "checked": 40},
            "source": "serpapi",
        }
        # 40 + 40 = 80 per pax × 2 = 160
        assert _baggage_total(baggage, passengers=2) == 160.0

    def test_none_baggage_returns_zero(self):
        assert _baggage_total(None, passengers=2) == 0.0

    def test_zero_passengers_treated_as_one(self):
        baggage = {
            "outbound": {"carry_on": 0, "checked": 40},
            "return": {"carry_on": 0, "checked": 40},
            "source": "serpapi",
        }
        assert _baggage_total(baggage, passengers=0) == 80.0


class TestFormatCostBreakdownBaggage:

    def test_baggage_line_appended_when_nonzero(self):
        baggage = {
            "outbound": {"carry_on": 0, "checked": 40},
            "return": {"carry_on": 0, "checked": 40},
            "source": "serpapi",
        }
        line, total = _format_cost_breakdown(
            price=500, transport=0, parking=0, mode="train",
            baggage=baggage, passengers=2,
        )
        assert "€160 bags" in line
        assert total == 660.0  # 500 + 160

    def test_baggage_line_suppressed_when_zero(self):
        line, total = _format_cost_breakdown(
            price=500, transport=0, parking=0, mode="train",
            baggage={"source": "fallback_table",
                     "outbound": {"carry_on": 0, "checked": 0},
                     "return": {"carry_on": 0, "checked": 0}},
            passengers=2,
        )
        assert "bags" not in line
        assert total == 500.0

    def test_baggage_line_suppressed_when_unknown(self):
        """Per Condition C5, source='unknown' must suppress the bags line."""
        baggage = {
            "outbound": {"carry_on": 12, "checked": 30},
            "return": {"carry_on": 12, "checked": 30},
            "source": "unknown",
        }
        line, total = _format_cost_breakdown(
            price=500, transport=0, parking=0, mode="train",
            baggage=baggage, passengers=2,
        )
        assert "bags" not in line
        assert total == 500.0


# --- send_deal_alert: structured 3-bullet reasoning + 3-button keyboard + Details ---

@pytest.mark.asyncio
async def test_deal_alert_renders_3_reasoning_bullets(notifier):
    deal_info = {
        "origin": "AMS", "destination": "NRT",
        "price": 970, "score": 0.85,
        "reasoning_json": {
            "vs_dates": "Cheapest of 4 dates polled",
            "vs_range": "€80 below typical low",
            "vs_nearby": "AMS is best",
        },
        "airline": "KLM", "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_001", "route_id": "ams-nrt",
    }
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)
        text = _payload_of(mock_cls.return_value.post.call_args)["text"]

    assert "✓ Cheapest of 4 dates polled" in text
    assert "✓ €80 below typical low" in text
    assert "✓ AMS is best" in text


@pytest.mark.asyncio
async def test_deal_alert_baggage_line_appears_when_nonzero(notifier):
    deal_info = {
        "origin": "AMS", "destination": "NRT", "price": 970,
        "score": 0.85, "airline": "KL", "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_002", "route_id": "ams-nrt",
        "passengers": 2,
        "baggage_estimate": {
            "outbound": {"carry_on": 0, "checked": 40},
            "return": {"carry_on": 0, "checked": 40},
            "source": "serpapi",
        },
    }
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)
        text = _payload_of(mock_cls.return_value.post.call_args)["text"]

    assert "€160 bags" in text


@pytest.mark.asyncio
async def test_deal_alert_baggage_line_suppressed_when_unknown(notifier):
    deal_info = {
        "origin": "AMS", "destination": "NRT", "price": 970,
        "score": 0.85, "airline": "KL", "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_003", "route_id": "ams-nrt",
        "passengers": 2,
        "baggage_estimate": {
            "outbound": {"carry_on": 0, "checked": 0},
            "return": {"carry_on": 0, "checked": 0},
            "source": "unknown",
        },
    }
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)
        text = _payload_of(mock_cls.return_value.post.call_args)["text"]

    assert "bags" not in text


@pytest.mark.asyncio
async def test_deal_alert_keyboard_three_button_row_plus_details(notifier):
    deal_info = {
        "origin": "AMS", "destination": "NRT", "price": 970,
        "score": 0.85, "airline": "KL", "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_004", "route_id": "ams-nrt",
    }
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)
        keyboard = _payload_of(mock_cls.return_value.post.call_args)["reply_markup"]["inline_keyboard"]

    # Row 1: Book Now (URL) + Watching 👀 (deal:watch) + Skip route 🔕 (route:snooze:7)
    assert len(keyboard) == 2
    row1 = keyboard[0]
    assert row1[0]["text"] == "Book Now ✈️"
    assert "url" in row1[0]
    assert row1[1]["text"] == "Watching 👀"
    assert row1[1]["callback_data"] == "deal:watch:deal_004"
    assert row1[2]["text"] == "Skip route 🔕"
    assert row1[2]["callback_data"] == "route:snooze:7:ams-nrt"
    # Row 2: Details
    row2 = keyboard[1]
    assert row2[0]["text"] == "📊 Details"
    assert "url" in row2[0]


@pytest.mark.asyncio
async def test_deal_alert_transparency_footer_all_saved(notifier):
    deal_info = {
        "origin": "AMS", "destination": "NRT", "price": 970,
        "score": 0.85, "airline": "KL", "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_005", "route_id": "ams-nrt",
        # No competitive, but two evaluated
        "nearby_comparison": [],
        "nearby_evaluated": [
            {"airport_code": "EIN", "delta_vs_primary": 30},
            {"airport_code": "BRU", "delta_vs_primary": 70},
        ],
    }
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)
        text = _payload_of(mock_cls.return_value.post.call_args)["text"]

    assert "Checked 2 airports" in text
    assert "your airport is best" in text


@pytest.mark.asyncio
async def test_deal_alert_transparency_footer_mixed(notifier):
    deal_info = {
        "origin": "AMS", "destination": "NRT", "price": 1940,
        "score": 0.85, "airline": "KL", "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_006", "route_id": "ams-nrt",
        "nearby_comparison": [
            {"airport_code": "BRU", "airport_name": "Brussels", "fare_pp": 1600,
             "net_cost": 3270, "savings": 610, "transport_mode": "Thalys",
             "transport_cost": 70, "transport_time_min": 150},
        ],
        "nearby_evaluated": [
            {"airport_code": "BRU", "delta_vs_primary": -610},
            {"airport_code": "EIN", "airport_name": "Eindhoven", "delta_vs_primary": 30},
        ],
    }
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)
        text = _payload_of(mock_cls.return_value.post.call_args)["text"]

    # Mixed: competitive Brussels shown, EIN noted in footer.
    assert "Brussels" in text
    assert "Eindhoven" in text
    assert "skipped" in text


@pytest.mark.asyncio
async def test_deal_alert_legacy_reasoning_string_fallback(notifier):
    """When reasoning_json absent, legacy string still renders (back-compat)."""
    deal_info = {
        "origin": "AMS", "destination": "NRT", "price": 485,
        "score": 0.85, "reasoning": "Legacy single-line reasoning",
        "airline": "KL", "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_007", "route_id": "ams-nrt",
    }
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_deal_alert(deal_info, chat_id=CHAT_ID)
        text = _payload_of(mock_cls.return_value.post.call_args)["text"]

    assert "_Legacy single-line reasoning_" in text


# --- send_error_fare_alert: cost breakdown + baggage + reasoning bullets ---

@pytest.mark.asyncio
async def test_error_fare_alert_includes_baggage_and_reasoning(notifier):
    deal_info = {
        "origin": "AMS", "destination": "NRT", "price": 200,
        "score": 0.95, "airline": "QR", "dates": "2026-10-01 to 2026-10-15",
        "deal_id": "deal_ef_001", "route_id": "ams-nrt",
        "passengers": 2,
        "reasoning_json": {
            "vs_dates": "Outlier — 75% below normal",
            "vs_range": "Way below Google range",
            "vs_nearby": "AMS exclusive",
        },
        "baggage_estimate": {
            "outbound": {"carry_on": 0, "checked": 30},
            "return": {"carry_on": 0, "checked": 30},
            "source": "serpapi",
        },
    }
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_error_fare_alert(deal_info, chat_id=CHAT_ID)
        text = _payload_of(mock_cls.return_value.post.call_args)["text"]

    assert "€200 flights" in text
    assert "€120 bags" in text  # 30+30 = 60 per pax × 2 = 120
    assert "✓ Outlier" in text
    assert "✓ Way below" in text
    assert "✓ AMS exclusive" in text


# --- send_follow_up: cost breakdown + new deal:* callbacks ---

@pytest.mark.asyncio
async def test_follow_up_includes_cost_breakdown_with_baggage(notifier):
    deal_info = {
        "origin": "AMS", "destination": "NRT", "price": 485,
        "deal_id": "deal_fu_001",
        "passengers": 2,
        "primary_transport_cost": 0,
        "primary_transport_mode": "train",
        "baggage_estimate": {
            "outbound": {"carry_on": 0, "checked": 40},
            "return": {"carry_on": 0, "checked": 40},
            "source": "serpapi",
        },
    }
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_follow_up(deal_info, chat_id=CHAT_ID)
        text = _payload_of(mock_cls.return_value.post.call_args)["text"]

    assert "€485 flights" in text
    assert "€160 bags" in text


@pytest.mark.asyncio
async def test_follow_up_callbacks_use_new_deal_prefixes(notifier):
    deal_info = {
        "origin": "AMS", "destination": "NRT", "price": 485,
        "deal_id": "deal_fu_002",
    }
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_follow_up(deal_info, chat_id=CHAT_ID)
        keyboard = _payload_of(mock_cls.return_value.post.call_args)["reply_markup"]["inline_keyboard"]

    row = keyboard[0]
    assert row[0]["callback_data"] == "deal:book:deal_fu_002"
    assert row[1]["callback_data"] == "deal:watch:deal_fu_002"


# --- send_daily_digest: 3-button row + Details + footer + concrete header ---

@pytest.mark.asyncio
async def test_digest_per_route_keyboard_three_button_row_plus_details(notifier):
    routes = [
        {
            "origin": "AMS", "destination": "NRT", "lowest_price": 485,
            "trend": "down", "passengers": 2,
            "deal_ids": ["deal_dg_001"],
            "route_id": "ams-nrt",
            "user_id": "u1",
        },
    ]
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_daily_digest(routes, chat_id=CHAT_ID)
        # 2 messages: header + 1 route
        assert mock_cls.return_value.post.call_count == 2
        route_payload = _payload_of(mock_cls.return_value.post.call_args_list[1])
        keyboard = route_payload["reply_markup"]["inline_keyboard"]

    assert len(keyboard) == 2
    row1 = keyboard[0]
    assert row1[0]["text"] == "Book Now ✈️"
    assert row1[1]["text"] == "Watching 👀"
    assert row1[1]["callback_data"] == "deal:watch:deal_dg_001"
    assert row1[2]["text"] == "Skip route 🔕"
    assert row1[2]["callback_data"] == "route:snooze:7:ams-nrt"
    row2 = keyboard[1]
    assert row2[0]["text"] == "📊 Details"


@pytest.mark.asyncio
async def test_digest_renders_concrete_header_override(notifier):
    """When orchestrator stuffs digest_header_override into the first summary, telegram uses it."""
    routes = [
        {
            "origin": "AMS", "destination": "NRT", "lowest_price": 485,
            "trend": "down", "passengers": 2,
            "deal_ids": ["d1"], "route_id": "ams-nrt", "user_id": "u1",
            "digest_header_override": "📊 *FareHound Daily* — 1 route, 1 price moved\n• AMS→NRT dropped €40 (€485/pp)",
        },
    ]
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_daily_digest(routes, chat_id=CHAT_ID)
        header_text = _payload_of(mock_cls.return_value.post.call_args_list[0])["text"]

    assert "1 price moved" in header_text
    assert "dropped €40" in header_text
    # Generic placeholder NOT present
    assert "haven't decided" not in header_text


@pytest.mark.asyncio
async def test_digest_route_baggage_appears_when_nonzero(notifier):
    routes = [
        {
            "origin": "AMS", "destination": "NRT", "lowest_price": 1940,
            "trend": "down", "passengers": 2,
            "deal_ids": ["d1"], "route_id": "ams-nrt", "user_id": "u1",
            "baggage_estimate": {
                "outbound": {"carry_on": 0, "checked": 40},
                "return": {"carry_on": 0, "checked": 40},
                "source": "serpapi",
            },
        },
    ]
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_daily_digest(routes, chat_id=CHAT_ID)
        text = _payload_of(mock_cls.return_value.post.call_args_list[1])["text"]

    assert "€160 bags" in text


@pytest.mark.asyncio
async def test_digest_transparency_footer_renders(notifier):
    routes = [
        {
            "origin": "AMS", "destination": "NRT", "lowest_price": 485,
            "trend": "down", "passengers": 2,
            "deal_ids": ["d1"], "route_id": "ams-nrt", "user_id": "u1",
            "nearby_prices": [],
            "nearby_evaluated": [
                {"airport_code": "EIN", "delta_vs_primary": 25},
                {"airport_code": "BRU", "delta_vs_primary": 60},
            ],
        },
    ]
    with patch("src.alerts.telegram.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_http()
        await notifier.send_daily_digest(routes, chat_id=CHAT_ID)
        text = _payload_of(mock_cls.return_value.post.call_args_list[1])["text"]

    assert "Checked 2 airports" in text
    assert "best by €25–€60" in text
