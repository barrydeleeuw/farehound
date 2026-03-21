from __future__ import annotations

from src.apis.community import parse_deal_message


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
