from __future__ import annotations

from src.analysis.nearby_airports import calculate_net_cost, compare_airports


# --- calculate_net_cost ---

def test_calculate_net_cost_basic():
    # 400 * 2 + 12 * 2 + 0 = 824
    assert calculate_net_cost(400, 2, 12, None) == 824.0


def test_calculate_net_cost_with_parking():
    # 350 * 2 + 30 * 2 + 50 = 810
    assert calculate_net_cost(350, 2, 30, 50) == 810.0


def test_calculate_net_cost_single_passenger():
    # 500 * 1 + 70 * 2 + 0 = 640
    assert calculate_net_cost(500, 1, 70, None) == 640.0


def test_calculate_net_cost_zero_transport():
    assert calculate_net_cost(300, 2, 0, None) == 600.0


# --- compare_airports ---

def _make_primary():
    return {
        "airport_code": "AMS",
        "fare_pp": 500,
        "transport_cost": 12,
        "parking_cost": None,
        "transport_mode": "train",
        "transport_time_min": 45,
    }


def _make_secondary(code, fare_pp, transport_cost, parking_cost=None, transport_mode="train", time_min=120):
    return {
        "airport_code": code,
        "fare_pp": fare_pp,
        "transport_cost": transport_cost,
        "parking_cost": parking_cost,
        "transport_mode": transport_mode,
        "transport_time_min": time_min,
    }


# primary net = 500*2 + 12*2 + 0 = 1024

def test_compare_airports_finds_savings():
    primary = _make_primary()  # net = 1024
    secondaries = [
        _make_secondary("BRU", 350, 70),  # net = 350*2 + 70*2 = 840, savings = 184
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    assert len(result) == 1
    assert result[0]["airport_code"] == "BRU"
    assert result[0]["airport_name"] == "Brussels"
    assert result[0]["savings"] == 184.0
    assert result[0]["net_cost"] == 840.0


def test_compare_airports_excludes_below_threshold():
    primary = _make_primary()  # net = 1024
    secondaries = [
        _make_secondary("DUS", 480, 60),  # net = 480*2 + 60*2 = 1080, savings = -56
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    assert len(result) == 0


def test_compare_airports_sorted_by_savings():
    primary = _make_primary()  # net = 1024
    secondaries = [
        _make_secondary("BRU", 400, 70),   # net = 400*2 + 70*2 = 940, savings = 84
        _make_secondary("CGN", 350, 70),   # net = 350*2 + 70*2 = 840, savings = 184
        _make_secondary("EIN", 380, 30, 50),  # net = 380*2 + 30*2 + 50 = 870, savings = 154
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    assert len(result) == 3
    assert result[0]["airport_code"] == "CGN"
    assert result[1]["airport_code"] == "EIN"
    assert result[2]["airport_code"] == "BRU"


def test_compare_airports_custom_threshold():
    primary = _make_primary()  # net = 1024
    secondaries = [
        _make_secondary("BRU", 450, 70),  # net = 450*2 + 70*2 = 1040, savings = -16
    ]
    # Both default (75) and lower (30) threshold exclude negative savings
    assert len(compare_airports(primary, secondaries, passengers=2)) == 0
    assert len(compare_airports(primary, secondaries, passengers=2, savings_threshold=30)) == 0


def test_compare_airports_custom_threshold_positive_savings():
    primary = _make_primary()  # net = 1024
    secondaries = [
        _make_secondary("BRU", 440, 30),  # net = 440*2 + 30*2 = 940, savings = 84
    ]
    # Default threshold (75) includes it
    assert len(compare_airports(primary, secondaries, passengers=2)) == 1
    # Higher threshold excludes it
    assert len(compare_airports(primary, secondaries, passengers=2, savings_threshold=100)) == 0


def test_compare_airports_empty_secondaries():
    primary = _make_primary()
    result = compare_airports(primary, [], passengers=2)
    assert result == []


def test_compare_airports_with_parking():
    primary = _make_primary()  # net = 1024
    secondaries = [
        _make_secondary("EIN", 350, 30, 50, "car", 50),  # net = 350*2 + 30*2 + 50 = 810, savings = 214
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    assert len(result) == 1
    assert result[0]["net_cost"] == 810.0
    assert result[0]["transport_mode"] == "car"
    assert result[0]["transport_time_min"] == 50


def test_compare_airports_includes_airport_name():
    primary = _make_primary()
    secondaries = [
        _make_secondary("CGN", 350, 70, transport_mode="train", time_min=180),
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    assert result[0]["airport_name"] == "Cologne"
