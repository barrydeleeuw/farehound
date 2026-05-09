from __future__ import annotations

from src.analysis.nearby_airports import calculate_net_cost, compare_airports, transport_total, is_per_person_transport


# --- transport helpers ---

def test_per_person_transport_modes():
    assert is_per_person_transport("train")
    assert is_per_person_transport("Thalys")
    assert is_per_person_transport("bus")
    assert not is_per_person_transport("car")
    assert not is_per_person_transport("uber")
    assert not is_per_person_transport("taxi")


def test_transport_total_car():
    # Car: €45 one-way, 2 pax → €45 * 2 trips = €90 (per vehicle)
    assert transport_total(45, "car", 2) == 90.0


def test_transport_total_train():
    # Train: €35 one-way, 2 pax → €35 * 2 pax * 2 trips = €140
    assert transport_total(35, "train", 2) == 140.0


def test_transport_total_train_single():
    # Train: €35 one-way, 1 pax → €35 * 1 * 2 = €70
    assert transport_total(35, "train", 1) == 70.0


# --- calculate_net_cost ---

def test_calculate_net_cost_car():
    # 400pp * 2 + car €45 * 2 trips + 0 = 890
    assert calculate_net_cost(400, 2, 45, None, "car") == 890.0


def test_calculate_net_cost_train():
    # 400pp * 2 + train €12 * 2 pax * 2 trips + 0 = 848
    assert calculate_net_cost(400, 2, 12, None, "train") == 848.0


def test_calculate_net_cost_with_parking():
    # 350pp * 2 + car €30 * 2 trips + €50 parking = 810
    assert calculate_net_cost(350, 2, 30, 50, "car") == 810.0


def test_calculate_net_cost_zero_transport():
    assert calculate_net_cost(300, 2, 0, None) == 600.0


# --- compare_airports ---

def _make_primary():
    return {
        "airport_code": "AMS",
        "fare_pp": 500,
        "transport_cost": 45,
        "parking_cost": None,
        "transport_mode": "uber",
        "transport_time_min": 30,
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


# primary net: 500*2 + uber €45*2 = 1090

def test_compare_airports_finds_savings():
    primary = _make_primary()  # net = 1090
    secondaries = [
        _make_secondary("BRU", 350, 35),  # net = 350*2 + train €35*2pax*2trips = 840, savings = 250
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    competitive = result["competitive"]
    assert len(competitive) == 1
    assert competitive[0]["airport_code"] == "BRU"
    assert competitive[0]["savings"] == 250.0
    assert competitive[0]["net_cost"] == 840.0
    # evaluated includes the same secondary regardless of threshold
    assert len(result["evaluated"]) == 1


def test_compare_airports_excludes_below_threshold():
    primary = _make_primary()  # net = 1090
    secondaries = [
        _make_secondary("DUS", 480, 35),  # net = 480*2 + train €35*2*2 = 1100, savings = -10
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    assert len(result["competitive"]) == 0
    # evaluated still records it with delta_vs_primary positive (more expensive)
    assert len(result["evaluated"]) == 1
    assert result["evaluated"][0]["airport_code"] == "DUS"
    assert result["evaluated"][0]["delta_vs_primary"] == 10.0


def test_compare_airports_sorted_by_savings():
    primary = _make_primary()  # net = 1090
    secondaries = [
        _make_secondary("BRU", 400, 35),   # net = 800 + 140 = 940, savings = 150
        _make_secondary("CGN", 350, 35),   # net = 700 + 140 = 840, savings = 250
        _make_secondary("EIN", 380, 15, 50, "car", 50),  # net = 760 + 30 + 50 = 840, savings = 250
    ]
    result = compare_airports(primary, secondaries, passengers=2)
    competitive = result["competitive"]
    assert len(competitive) == 3
    # CGN and EIN tied at 250, BRU at 150
    assert competitive[2]["airport_code"] == "BRU"


def test_compare_airports_custom_threshold():
    primary = _make_primary()  # net = 1090
    secondaries = [
        _make_secondary("BRU", 470, 35),  # net = 940 + 140 = 1080, savings = 10
    ]
    assert len(compare_airports(primary, secondaries, passengers=2)["competitive"]) == 0
    assert (
        len(compare_airports(primary, secondaries, passengers=2, savings_threshold=5)["competitive"])
        == 1
    )


def test_compare_airports_empty_secondaries():
    primary = _make_primary()
    result = compare_airports(primary, [], passengers=2)
    assert result["competitive"] == []
    assert result["evaluated"] == []


def test_compare_airports_with_parking():
    primary = _make_primary()  # net = 1090
    secondaries = [
        _make_secondary("EIN", 350, 25, 50, "car", 50),  # net = 700 + 50 + 50 = 800, savings = 290
    ]
    competitive = compare_airports(primary, secondaries, passengers=2)["competitive"]
    assert len(competitive) == 1
    assert competitive[0]["net_cost"] == 800.0
    assert competitive[0]["transport_mode"] == "car"


def test_compare_airports_includes_airport_name():
    primary = _make_primary()
    secondaries = [
        _make_secondary("CGN", 350, 35, transport_mode="train", time_min=180),
    ]
    competitive = compare_airports(primary, secondaries, passengers=2)["competitive"]
    assert competitive[0]["airport_name"] == "Cologne"


# --- flight_duration_min passthrough ---

def test_compare_airports_passes_through_flight_duration():
    primary = _make_primary()
    primary["flight_duration_min"] = 720
    secondaries = [{
        "airport_code": "BRU",
        "fare_pp": 350,
        "transport_cost": 35,
        "parking_cost": None,
        "transport_mode": "train",
        "transport_time_min": 120,
        "flight_duration_min": 680,
    }]
    competitive = compare_airports(primary, secondaries, passengers=2)["competitive"]
    assert len(competitive) == 1
    assert competitive[0]["flight_duration_min"] == 680
    assert competitive[0]["primary_flight_duration_min"] == 720


def test_compare_airports_flight_duration_none():
    """When flight_duration_min is not provided, it should be None."""
    primary = _make_primary()  # no flight_duration_min
    secondaries = [
        _make_secondary("BRU", 350, 35),
    ]
    competitive = compare_airports(primary, secondaries, passengers=2)["competitive"]
    assert competitive[0]["flight_duration_min"] is None
    assert competitive[0]["primary_flight_duration_min"] is None
