from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.apis.community import CommunityFeedConfig, RSSListener, parse_deal_message


# --- Route patterns ---

def test_parse_arrow_route():
    result = parse_deal_message("AMS → NRT from €485")
    assert result is not None
    assert result["origin"] == "AMS"
    assert result["destination"] == "NRT"
    assert result["price"] == 485.0


def test_parse_dash_arrow_route():
    result = parse_deal_message("AMS -> NRT from €300")
    assert result is not None
    assert result["origin"] == "AMS"
    assert result["destination"] == "NRT"


def test_parse_to_route():
    result = parse_deal_message("AMS to NRT €485")
    assert result is not None
    assert result["origin"] == "AMS"
    assert result["destination"] == "NRT"


def test_parse_plane_emoji_route():
    result = parse_deal_message("AMS ✈ NRT €500")
    assert result is not None
    assert result["origin"] == "AMS"
    assert result["destination"] == "NRT"
    assert result["price"] == 500.0


def test_parse_hyphen_route():
    result = parse_deal_message("AMS-NRT from €400")
    assert result is not None
    assert result["origin"] == "AMS"
    assert result["destination"] == "NRT"


# --- Price patterns ---

def test_parse_euro_prefix():
    result = parse_deal_message("AMS → NRT from €485")
    assert result["price"] == 485.0


def test_parse_dollar_prefix():
    result = parse_deal_message("AMS → NRT from $299")
    assert result["price"] == 299.0


def test_parse_price_suffix_eur():
    result = parse_deal_message("AMS → NRT 312 EUR")
    assert result["price"] == 312.0


def test_parse_price_prefix_eur():
    result = parse_deal_message("AMS → NRT EUR 450")
    assert result["price"] == 450.0


def test_parse_price_with_comma():
    result = parse_deal_message("AMS → NRT from €1,485")
    assert result["price"] == 1485.0


def test_parse_reversed_format():
    """Test: '€312 London to Tokyo' — price before route."""
    result = parse_deal_message("€312 LHR to NRT")
    assert result is not None
    assert result["price"] == 312.0
    assert result["origin"] == "LHR"
    assert result["destination"] == "NRT"


# --- Date patterns ---

def test_parse_iso_date():
    result = parse_deal_message("AMS → NRT €485 on 2026-10-08")
    assert "dates" in result
    assert "2026-10-08" in result["dates"]


def test_parse_month_day_date():
    result = parse_deal_message("AMS → NRT €485 departing Oct 8")
    assert "dates" in result
    # Dates are normalized to ISO format
    assert any("-10-08" in d for d in result["dates"])


def test_parse_day_month_date():
    result = parse_deal_message("AMS → NRT €485 departing 8 October")
    assert "dates" in result


def test_parse_slash_date():
    result = parse_deal_message("AMS → NRT €485 on 08/10")
    assert "dates" in result


# --- Edge cases ---

def test_parse_empty_string():
    assert parse_deal_message("") is None


def test_parse_none_like():
    assert parse_deal_message("") is None


def test_parse_no_deal_info():
    result = parse_deal_message("Hello world, no flight deals here")
    assert result is None


def test_parse_price_only():
    """A message with price but no route should still return data."""
    result = parse_deal_message("Amazing deal €199!")
    assert result is not None
    assert result["price"] == 199.0


def test_parse_iata_fallback():
    """When no arrow pattern, fall back to extracting IATA codes."""
    result = parse_deal_message("Flight from AMS arriving NRT for €485")
    assert result is not None
    # Should pick up IATA codes via fallback
    assert result.get("origin") == "AMS"
    assert result.get("destination") == "NRT"


# --- Origin filtering (CommunityListener level, tested via parse_deal_message) ---

def test_parse_deal_preserves_case():
    """Origin/destination should be uppercased."""
    result = parse_deal_message("ams → nrt from €485")
    assert result is not None
    assert result["origin"] == "AMS"
    assert result["destination"] == "NRT"


# --- RSSListener ---

def test_rss_listener_init():
    feeds = [
        CommunityFeedConfig(channel="reddit", filter_origins=["AMS", "LHR"], url="https://example.com/rss"),
        CommunityFeedConfig(channel="secret", filter_origins=[], url="https://example.com/rss2"),
    ]
    listener = RSSListener(feeds=feeds, poll_interval_seconds=600)
    assert listener.poll_interval == 600
    assert len(listener.feeds) == 2
    assert "AMS" in listener._filter_origins
    assert "LHR" in listener._filter_origins
    assert listener._running is False


def test_rss_listener_init_default_interval():
    listener = RSSListener(feeds=[])
    assert listener.poll_interval == 300


@pytest.mark.asyncio
async def test_rss_listener_start():
    listener = RSSListener(feeds=[])
    callback = AsyncMock()
    await listener.start(callback)
    assert listener._running is True
    assert listener._callback is callback


def test_rss_listener_stop():
    listener = RSSListener(feeds=[])
    listener._running = True
    listener.stop()
    assert listener._running is False


def _make_mock_response(text):
    resp = MagicMock()
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


def _patch_httpx(mock_response):
    """Context manager that patches httpx.AsyncClient for RSS polling."""
    mock_async_client = AsyncMock()
    mock_async_client.get = AsyncMock(return_value=mock_response)
    mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
    mock_async_client.__aexit__ = AsyncMock(return_value=False)
    return patch("src.apis.community.httpx.AsyncClient", return_value=mock_async_client), mock_async_client


@pytest.mark.asyncio
async def test_rss_poll_seeds_seen_ids():
    feed_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Test</title>
      <item><title>AMS to NRT €450</title><guid>deal1</guid></item>
      <item><title>LHR to JFK $299</title><guid>deal2</guid></item>
    </channel></rss>"""

    feeds = [CommunityFeedConfig(channel="test", filter_origins=[], url="https://example.com/rss")]
    listener = RSSListener(feeds=feeds)
    callback = AsyncMock()
    await listener.start(callback)

    patcher, _ = _patch_httpx(_make_mock_response(feed_xml))
    with patcher:
        await listener._poll(seed=True)
        callback.assert_not_called()
        assert "deal1" in listener._seen_ids
        assert "deal2" in listener._seen_ids


@pytest.mark.asyncio
async def test_rss_poll_new_entries_trigger_callback():
    feed_seed = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>T</title>
      <item><title>Old</title><guid>old1</guid></item>
    </channel></rss>"""

    feed_new = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>T</title>
      <item><title>Old</title><guid>old1</guid></item>
      <item><title>AMS → NRT €399 error fare</title><guid>new1</guid><summary>Great deal</summary></item>
    </channel></rss>"""

    feeds = [CommunityFeedConfig(channel="test", filter_origins=[], url="https://example.com/rss")]
    listener = RSSListener(feeds=feeds)
    callback = AsyncMock()
    await listener.start(callback)

    with patch("src.apis.community.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        mock_client.get = AsyncMock(return_value=_make_mock_response(feed_seed))
        await listener._poll(seed=True)
        callback.assert_not_called()

        mock_client.get = AsyncMock(return_value=_make_mock_response(feed_new))
        await listener._poll(seed=False)
        assert callback.call_count == 1
        deal_info = callback.call_args[0][0]
        assert deal_info["origin"] == "AMS"
        assert deal_info["destination"] == "NRT"
        assert deal_info["community_flagged"] is True


@pytest.mark.asyncio
async def test_rss_seen_ids_dedup():
    feed_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>T</title>
      <item><title>AMS to NRT €450</title><guid>deal1</guid><summary>Deal</summary></item>
    </channel></rss>"""

    feeds = [CommunityFeedConfig(channel="test", filter_origins=[], url="https://example.com/rss")]
    listener = RSSListener(feeds=feeds)
    callback = AsyncMock()
    await listener.start(callback)

    patcher, _ = _patch_httpx(_make_mock_response(feed_xml))
    with patcher:
        await listener._poll(seed=False)
        assert callback.call_count == 1
        await listener._poll(seed=False)
        assert callback.call_count == 1  # deduped


@pytest.mark.asyncio
async def test_rss_origin_filtering():
    feed_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>T</title>
      <item><title>LHR to JFK $299</title><guid>d1</guid><summary>London deal</summary></item>
      <item><title>AMS to NRT €450</title><guid>d2</guid><summary>Amsterdam deal</summary></item>
    </channel></rss>"""

    feeds = [CommunityFeedConfig(channel="test", filter_origins=["AMS"], url="https://example.com/rss")]
    listener = RSSListener(feeds=feeds)
    callback = AsyncMock()
    await listener.start(callback)

    patcher, _ = _patch_httpx(_make_mock_response(feed_xml))
    with patcher:
        await listener._poll(seed=False)
        assert callback.call_count == 1
        deal_info = callback.call_args[0][0]
        assert deal_info["origin"] == "AMS"
