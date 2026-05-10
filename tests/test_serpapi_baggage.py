"""T15 — Baggage parsing + airline fallback table (R7 §8).

Architect-Lead's Finding #1: real cached SerpAPI responses contain ZERO baggage data.
We therefore mock at the unit-test level using synthetic fixtures
(`tests/fixtures/serpapi_with_baggage/`) plus inline test inputs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.apis.serpapi import FlightSearchResult
from src.utils.baggage import (
    FALLBACK,
    LONG_HAUL_KM,
    estimate,
    parse_baggage_extensions,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "serpapi_with_baggage"


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


def _result_from_fixture(name: str) -> FlightSearchResult:
    """Build a FlightSearchResult from a synthetic fixture file."""
    data = _load(name)
    return FlightSearchResult(
        best_flights=data.get("best_flights", []),
        other_flights=data.get("other_flights", []),
        price_insights=data.get("price_insights", {}),
        booking_options=data.get("booking_options", []),
        search_params=data.get("search_parameters", {}),
        raw_response=data,
    )


# =============================================================================
# parse_baggage_extensions — string scanning
# =============================================================================

class TestParseBaggageExtensions:

    def test_extracts_checked_eur_format(self):
        result = parse_baggage_extensions(["Checked baggage: 1st bag 40 €"])
        assert result == {"carry_on": 0, "checked": 40}

    def test_extracts_checked_eur_prefix(self):
        result = parse_baggage_extensions(["Checked baggage €50"])
        assert result == {"carry_on": 0, "checked": 50}

    def test_extracts_carry_on_eur(self):
        result = parse_baggage_extensions(["Carry-on baggage: 25 €"])
        assert result == {"carry_on": 25, "checked": 0}

    def test_extracts_both_in_one_list(self):
        result = parse_baggage_extensions([
            "Carry-on baggage: 8 €",
            "Checked baggage: 1st bag 35 €",
        ])
        assert result == {"carry_on": 8, "checked": 35}

    def test_dollar_format_recognised(self):
        result = parse_baggage_extensions(["Checked baggage: $45"])
        assert result == {"carry_on": 0, "checked": 45}

    def test_returns_none_when_no_baggage_terms(self):
        """Non-baggage extensions like legroom/wifi must NOT match."""
        result = parse_baggage_extensions([
            "Average legroom (31 in)",
            "Wi-Fi for a fee",
            "In-seat power & USB outlets",
            "Carbon emissions: 850 kg",
        ])
        assert result is None

    def test_returns_none_for_empty_input(self):
        assert parse_baggage_extensions([]) is None
        assert parse_baggage_extensions(None) is None

    def test_handles_non_list_input_gracefully(self):
        """Defensive — must NEVER raise (Condition C4)."""
        assert parse_baggage_extensions("not a list") is None
        assert parse_baggage_extensions({"key": "val"}) is None
        assert parse_baggage_extensions(42) is None

    def test_skips_non_string_items_in_list(self):
        result = parse_baggage_extensions([
            None,
            42,
            {"nested": "dict"},
            "Checked baggage: 30 €",
        ])
        assert result == {"carry_on": 0, "checked": 30}

    def test_does_not_match_bare_numeric_phrases(self):
        """'1st bag' or '2 bags' alone (no currency) must NOT be picked up as a fee."""
        result = parse_baggage_extensions([
            "Checked baggage: 1st bag included",
            "2 bags allowed",
        ])
        assert result is None


# =============================================================================
# estimate — fallback table per airline + user preference
# =============================================================================

class TestEstimateFallback:

    # --- Airline policy lookups ---

    def test_kl_long_haul_free_one_checked(self):
        """KL on long-haul: carry_on=0, checked=0 (legacy carrier free bag)."""
        result = estimate("KL", leg_distance_km=10000, baggage_needs="one_checked")
        assert result == {"carry_on": 0, "checked": 0}

    def test_kl_short_haul_paid_checked(self):
        """KL on short-haul: carry_on=0, checked=25 (short-haul fee per FALLBACK table)."""
        result = estimate("KL", leg_distance_km=500, baggage_needs="one_checked")
        assert result == {"carry_on": 0, "checked": 25}

    def test_fr_short_haul_one_checked_40(self):
        """Per plan §8.6: one_checked × FR short-haul = €40 each direction."""
        result = estimate("FR", leg_distance_km=1500, baggage_needs="one_checked")
        assert result == {"carry_on": 25, "checked": 40}

    def test_hv_long_haul_lcc_fees(self):
        """Transavia (HV) on long-haul has LCC fees."""
        result = estimate("HV", leg_distance_km=5000, baggage_needs="one_checked")
        assert result == {"carry_on": 12, "checked": 35}

    def test_unknown_airline_uses_default_table(self):
        """Unknown airline code falls back to `_DEFAULT` policy."""
        result = estimate("ZZ", leg_distance_km=1000, baggage_needs="one_checked")
        assert result == {
            "carry_on": FALLBACK["_DEFAULT"]["carry_on"],
            "checked": FALLBACK["_DEFAULT"]["checked_short_haul"],
        }

    def test_lowercase_airline_code_matched(self):
        """Airline lookup is case-insensitive."""
        upper = estimate("KL", leg_distance_km=100, baggage_needs="one_checked")
        lower = estimate("kl", leg_distance_km=100, baggage_needs="one_checked")
        assert upper == lower

    def test_none_airline_falls_back_to_default(self):
        result = estimate(None, leg_distance_km=500, baggage_needs="one_checked")
        assert result == {"carry_on": 0, "checked": 30}  # _DEFAULT short-haul

    # --- User preference matrix ---

    def test_carry_on_only_kl_zero_total(self):
        """Per plan §8.6: carry_on_only × KL = €0 (KL has zero carry-on fee)."""
        result = estimate("KL", leg_distance_km=10000, baggage_needs="carry_on_only")
        assert result == {"carry_on": 0, "checked": 0}

    def test_carry_on_only_fr_keeps_carry_fee(self):
        """carry_on_only on Ryanair still pays the €25 carry fee but no checked."""
        result = estimate("FR", leg_distance_km=1500, baggage_needs="carry_on_only")
        assert result == {"carry_on": 25, "checked": 0}

    def test_two_checked_doubles_checked_fee(self):
        """two_checked = carry_on fee (if any) + 2x checked fee."""
        result = estimate("FR", leg_distance_km=1500, baggage_needs="two_checked")
        assert result == {"carry_on": 25, "checked": 80}  # 40 × 2

    def test_default_baggage_needs_is_one_checked(self):
        """Per plan §8.4: default behaviour when baggage_needs is None or unrecognised = one_checked."""
        result_default = estimate("KL", leg_distance_km=500, baggage_needs=None)
        result_one = estimate("KL", leg_distance_km=500, baggage_needs="one_checked")
        assert result_default == result_one

    # --- Long-haul threshold ---

    def test_long_haul_threshold_at_4000km(self):
        """Per plan §8.3 / FALLBACK constant: ≥4000km is long-haul."""
        below = estimate("KL", leg_distance_km=LONG_HAUL_KM - 100, baggage_needs="one_checked")
        at = estimate("KL", leg_distance_km=LONG_HAUL_KM, baggage_needs="one_checked")
        above = estimate("KL", leg_distance_km=LONG_HAUL_KM + 100, baggage_needs="one_checked")
        # KL short-haul=25, long-haul=0
        assert below["checked"] == 25
        assert at["checked"] == 0
        assert above["checked"] == 0

    def test_unknown_distance_treated_as_short_haul(self):
        """When leg_distance_km is None, fee defaults to short-haul (conservative)."""
        result = estimate("KL", leg_distance_km=None, baggage_needs="one_checked")
        assert result == {"carry_on": 0, "checked": 25}

    # --- Defensive ---

    def test_estimate_never_raises(self):
        """Even with garbage inputs the estimate falls back to zeros."""
        # Passes a non-numeric string for leg_distance_km — float() will fail downstream.
        result = estimate("KL", leg_distance_km="not-a-number", baggage_needs="one_checked")
        assert result == {"carry_on": 0, "checked": 0}


# =============================================================================
# FlightSearchResult.parse_baggage — full pipeline (Section 8.2)
# =============================================================================

class TestFlightSearchResultParseBaggage:

    def test_full_baggage_both_ways_fixture_extracts_serpapi(self):
        """Fixture A: baggage in booking_options[].together.extensions → source='serpapi'."""
        result = _result_from_fixture("full_baggage_both_ways.json")
        baggage = result.parse_baggage("KL", leg_distance_km=9300, baggage_needs="one_checked")

        assert baggage["source"] == "serpapi"
        assert baggage["currency"] == "EUR"
        # booking_options had "Checked baggage: 1st bag 40 €" — both directions
        assert baggage["outbound"]["checked"] == 40
        assert baggage["return"]["checked"] == 40

    def test_outbound_only_fixture_falls_back_to_flight_extensions(self):
        """Fixture B: empty booking_options, baggage in flight-leg extensions."""
        result = _result_from_fixture("outbound_only_baggage.json")
        baggage = result.parse_baggage("FR", leg_distance_km=8800, baggage_needs="one_checked")

        assert baggage["source"] == "serpapi"
        # Outbound has both carry-on 25€ and checked 50€; return leg has neither.
        assert baggage["outbound"]["carry_on"] == 25
        assert baggage["outbound"]["checked"] == 50
        # Return leg had no baggage strings — parser falls back to outbound per §8.2 (or None)
        # Builder's pipeline: ret_parsed or out_parsed → outbound used as ret when ret_parsed is None.
        assert baggage["return"]["carry_on"] == 25
        assert baggage["return"]["checked"] == 50

    def test_no_baggage_fixture_uses_fallback_table(self):
        """Fixture C: no baggage strings anywhere → source='fallback_table' (HV short-haul)."""
        result = _result_from_fixture("no_baggage_data.json")
        baggage = result.parse_baggage("HV", leg_distance_km=1900, baggage_needs="one_checked")

        # HV short-haul: carry_on=12, checked=30 — both non-zero so source stays 'fallback_table'
        assert baggage["source"] == "fallback_table"
        assert baggage["outbound"] == {"carry_on": 12, "checked": 30}
        assert baggage["return"] == {"carry_on": 12, "checked": 30}

    def test_no_baggage_with_zero_fallback_marks_unknown(self):
        """No SerpAPI data + KL long-haul (0 fees) → source='unknown' so renderer suppresses."""
        result = _result_from_fixture("no_baggage_data.json")
        baggage = result.parse_baggage("KL", leg_distance_km=10000, baggage_needs="one_checked")

        # KL long-haul fees = (0, 0) → source flips to 'unknown' per Condition C5.
        assert baggage["source"] == "unknown"

    def test_empty_result_does_not_raise(self):
        """Defensive: missing booking_options + empty flights → fallback path runs."""
        empty = FlightSearchResult()
        baggage = empty.parse_baggage("KL", leg_distance_km=500, baggage_needs="one_checked")
        # Falls all the way through to estimate(); KL short-haul has €25 checked fee
        assert baggage["source"] == "fallback_table"
        assert baggage["outbound"]["checked"] == 25

    def test_malformed_booking_options_dont_crash(self):
        """Booking_options that aren't dicts or have missing 'together' must be skipped silently."""
        result = FlightSearchResult(
            booking_options=[
                "not a dict",  # bad type
                {"no_together_key": True},
                {"together": "not a dict"},
                {"together": {}},  # no extensions
            ],
            best_flights=[
                {"flights": [{"extensions": ["Checked baggage: €30"]}]},
            ],
        )
        baggage = result.parse_baggage("KL", leg_distance_km=500, baggage_needs="one_checked")
        # Skips garbage booking_options, picks up flight-leg extension
        assert baggage["source"] == "serpapi"
        assert baggage["outbound"]["checked"] == 30

    def test_dict_shape_matches_section_8_1(self):
        """Returned dict has exactly the keys documented in §8.1."""
        result = _result_from_fixture("full_baggage_both_ways.json")
        baggage = result.parse_baggage("KL", leg_distance_km=9300, baggage_needs="one_checked")
        assert set(baggage.keys()) == {"outbound", "return", "source", "currency"}
        for direction in ("outbound", "return"):
            assert set(baggage[direction].keys()) >= {"carry_on", "checked"}
        assert baggage["source"] in ("serpapi", "fallback_table", "unknown")