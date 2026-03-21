from __future__ import annotations

from src.analysis.nearby_airports import calculate_net_cost, compare_airports


# --- calculate_net_cost ---

def test_calculate_net_cost_basic():
    # 400 * 2 + 12 + 0 = 812
    assert calculate_net_cost(400, 2, 12, None) == 812.0


def test_calculate_net_cost_with_parking():
    # 350 * 2 + 30 + 50 = 780
    assert calculate_net_cost(350, 2, 30, 50) == 780.0


def test_calculate_net_cost_single_passenger():
    # 500 * 1 + 70 + 0 = 570
    assert calculate_net_cost(500, 1, 70, None) == 570.0


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


def test_compare_airports_finds_savings():
    primary = _make_primary()  # net = 500*2 + 12 = 1012
    secondaries = [
        _make_secondary("BRU", 350, 70),  # net = 350*2 + 70 = 770, savings = 242
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    assert len(result) == 1
    assert result[0]["airport_code"] == "BRU"
    assert result[0]["airport_name"] == "Brussels"
    assert result[0]["savings"] == 242.0
    assert result[0]["net_cost"] == 770.0


def test_compare_airports_excludes_below_threshold():
    primary = _make_primary()  # net = 1012
    secondaries = [
        _make_secondary("DUS", 480, 60),  # net = 480*2 + 60 = 1020, savings = -8
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    assert len(result) == 0


def test_compare_airports_sorted_by_savings():
    primary = _make_primary()  # net = 1012
    secondaries = [
        _make_secondary("BRU", 400, 70),   # net = 870, savings = 142
        _make_secondary("CGN", 350, 70),   # net = 770, savings = 242
        _make_secondary("EIN", 380, 30, 50),  # net = 840, savings = 172
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    assert len(result) == 3
    assert result[0]["airport_code"] == "CGN"
    assert result[1]["airport_code"] == "EIN"
    assert result[2]["airport_code"] == "BRU"


def test_compare_airports_custom_threshold():
    primary = _make_primary()  # net = 1012
    secondaries = [
        _make_secondary("BRU", 450, 70),  # net = 970, savings = 42
    ]
    # Default threshold (75) would exclude this
    assert len(compare_airports(primary, secondaries, passengers=2)) == 0
    # Lower threshold includes it
    assert len(compare_airports(primary, secondaries, passengers=2, savings_threshold=30)) == 1


def test_compare_airports_empty_secondaries():
    primary = _make_primary()
    result = compare_airports(primary, [], passengers=2)
    assert result == []


def test_compare_airports_with_parking():
    primary = _make_primary()  # net = 1012
    secondaries = [
        _make_secondary("EIN", 350, 30, 50, "car", 50),  # net = 350*2 + 30 + 50 = 780, savings = 232
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    assert len(result) == 1
    assert result[0]["net_cost"] == 780.0
    assert result[0]["transport_mode"] == "car"
    assert result[0]["transport_time_min"] == 50


def test_compare_airports_includes_airport_name():
    primary = _make_primary()
    secondaries = [
        _make_secondary("CGN", 350, 70, transport_mode="train", time_min=180),
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    assert result[0]["airport_name"] == "Cologne"
