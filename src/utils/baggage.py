"""Baggage policy fallback table and parser helpers (R7 ITEM-051 sub-item 2).

Public:
- `parse_baggage_extensions(extensions)` — extract per-direction `(carry_on, checked)` totals from
  a list of SerpAPI extension strings. Defensive: never raises on malformed input.
- `estimate(airline_code, leg_distance_km, baggage_needs)` — fallback per-direction baggage cost
  when SerpAPI is silent. Honors user preference (`carry_on_only` | `one_checked` | `two_checked`).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

LONG_HAUL_KM = 4000

# IATA-keyed airline policies. Approximate per-direction fees in EUR.
# Long-haul includes 1× checked free for legacy carriers; LCCs charge.
FALLBACK = {
    "KL": {"carry_on": 0, "checked_long_haul": 0,  "checked_short_haul": 25},
    "AF": {"carry_on": 0, "checked_long_haul": 0,  "checked_short_haul": 30},
    "LH": {"carry_on": 0, "checked_long_haul": 0,  "checked_short_haul": 30},
    "BA": {"carry_on": 0, "checked_long_haul": 0,  "checked_short_haul": 35},
    "HV": {"carry_on": 12, "checked_long_haul": 35, "checked_short_haul": 30},  # Transavia
    "FR": {"carry_on": 25, "checked_long_haul": 50, "checked_short_haul": 40},  # Ryanair
    "U2": {"carry_on": 8,  "checked_long_haul": 35, "checked_short_haul": 30},  # easyJet
    "W6": {"carry_on": 10, "checked_long_haul": 40, "checked_short_haul": 30},  # Wizz
    "_DEFAULT": {"carry_on": 0, "checked_long_haul": 30, "checked_short_haul": 30},
}

# Recognised baggage terms.
_BAGGAGE_TERMS = r"(carry[\s-]?on|checked|hand|cabin)"
# Numeric fee — must be adjacent to a currency symbol/code so we don't grab "1st" or "2 bags".
_FEE = r"(?:(?:€|EUR)\s*(\d+)|(\d+)\s*(?:€|EUR)|(?:\$|USD)\s*(\d+)|(\d+)\s*(?:\$|USD))"
_BAGGAGE_RE = re.compile(_FEE + r".{0,40}?\b" + _BAGGAGE_TERMS + r"\b", re.IGNORECASE)
_BAGGAGE_RE_REVERSE = re.compile(r"\b" + _BAGGAGE_TERMS + r"\b.{0,40}?" + _FEE, re.IGNORECASE)

_CARRY_KEYS = ("carry", "hand", "cabin")
_CHECKED_KEYS = ("checked",)


def parse_baggage_extensions(extensions) -> dict | None:
    """Scan a list of strings for baggage fee mentions.

    Returns `{"carry_on": int, "checked": int}` if any matched. Returns None if no extensions
    contained recognisable baggage info — caller should fall back to airline table.
    """
    if not extensions:
        return None
    if not isinstance(extensions, (list, tuple)):
        return None
    found_carry = 0
    found_checked = 0
    matched_any = False
    for raw in extensions:
        if not isinstance(raw, str):
            continue
        for match in list(_BAGGAGE_RE.finditer(raw)) + list(_BAGGAGE_RE_REVERSE.finditer(raw)):
            groups = match.groups()
            amount = next((int(g) for g in groups if g and g.isdigit()), None)
            kind = next((g.lower() for g in groups if g and not g.isdigit()), "")
            if amount is None or not kind:
                continue
            matched_any = True
            if any(k in kind for k in _CARRY_KEYS):
                found_carry = max(found_carry, amount)
            elif any(k in kind for k in _CHECKED_KEYS):
                found_checked = max(found_checked, amount)
    if not matched_any:
        return None
    return {"carry_on": found_carry, "checked": found_checked}


def _airline_policy(airline_code: str | None) -> dict:
    if not airline_code:
        return FALLBACK["_DEFAULT"]
    code = airline_code.upper().strip()
    return FALLBACK.get(code, FALLBACK["_DEFAULT"])


def estimate(
    airline_code: str | None,
    leg_distance_km: float | None,
    baggage_needs: str | None,
) -> dict:
    """Per-direction baggage cost based on user preference and airline policy.

    Returns `{"carry_on": int, "checked": int}`. Always succeeds — never raises.
    """
    try:
        policy = _airline_policy(airline_code)
        is_long_haul = (
            leg_distance_km is not None and float(leg_distance_km) >= LONG_HAUL_KM
        )
        checked_fee = (
            policy["checked_long_haul"] if is_long_haul else policy["checked_short_haul"]
        )
        carry_fee = policy.get("carry_on", 0)
        if baggage_needs == "carry_on_only":
            return {"carry_on": carry_fee, "checked": 0}
        if baggage_needs == "two_checked":
            return {"carry_on": carry_fee, "checked": checked_fee * 2}
        # Default: one_checked
        return {"carry_on": carry_fee, "checked": checked_fee}
    except Exception:
        logger.exception("baggage.estimate failed for airline=%r", airline_code)
        return {"carry_on": 0, "checked": 0}
