"""Record SerpAPI responses for offline testing.

Run once to capture real responses, then use them in tests and local dev.
"""
import json
import hashlib
from pathlib import Path
import httpx

API_KEY = "1322ec102330476e6d5e5b69d7abe2f1bdaa4c05fa84643e44f0623d31eb57c5"
CACHE_DIR = Path(__file__).parent.parent / "data" / "serpapi_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def cache_key(params: dict) -> str:
    """Deterministic hash from query params (excluding api_key)."""
    clean = {k: v for k, v in sorted(params.items()) if k != "api_key"}
    return hashlib.md5(json.dumps(clean).encode()).hexdigest()


def record(params: dict) -> dict:
    params["api_key"] = API_KEY
    r = httpx.get("https://serpapi.com/search", params=params, timeout=30)
    data = r.json()
    key = cache_key(params)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(data, indent=2))
    print(f"Recorded {key}: {params.get('engine')} {params.get('departure_id', params.get('start_addr', '?'))} → {params.get('arrival_id', params.get('end_addr', '?'))}")
    return data


# --- Record flight searches for our routes ---
routes = [
    ("AMS", "NRT", "2026-10-20", "2026-11-03"),
    ("AMS", "ALC", "2026-06-20", "2026-07-04"),
    ("AMS", "MEX", "2026-12-24", "2027-01-07"),
    ("BRU", "NRT", "2026-10-20", "2026-11-03"),
    ("DUS", "NRT", "2026-10-20", "2026-11-03"),
]

print("=== Recording flight searches ===")
for origin, dest, out, ret in routes:
    record({
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": dest,
        "outbound_date": out,
        "return_date": ret,
        "type": "1",
        "adults": "2",
        "currency": "EUR",
        "hl": "en",
        "deep_search": "true",
        "sort_by": "2",
    })

# --- Record Google Maps directions ---
airports = [
    ("AMS", "Amsterdam Schiphol Airport"),
    ("BRU", "Brussels Airport"),
    ("DUS", "Dusseldorf Airport"),
    ("EIN", "Eindhoven Airport"),
    ("CGN", "Cologne Bonn Airport"),
    ("RTM", "Rotterdam The Hague Airport"),
]

print("\n=== Recording Google Maps directions ===")
for code, name in airports:
    for mode_name, mode_id in [("driving", "0"), ("transit", "3")]:
        record({
            "engine": "google_maps_directions",
            "start_addr": "The Hague, Netherlands",
            "end_addr": name,
            "travel_mode": mode_id,
        })

print(f"\nDone! {len(list(CACHE_DIR.glob('*.json')))} responses cached in {CACHE_DIR}")
