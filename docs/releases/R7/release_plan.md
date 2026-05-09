# R7 — Real Cost Restoration

**Release scope:** ITEM-051 only.
**Architect:** Architect-Lead.
**Builder + Tester** are joining the team after this plan lands. This is the canonical document; both should read it top-to-bottom once, then reference Sections 6–10 during work.

> **One-file plan.** Phase A advisory, Phase B design, Phase C atomic task list — all here. No companion docs.

---

## 1. Phase A — Pre-Build Advisory

### 1.1 Verdict
**Proceed with Conditions.** Alignment Score: **High** for sub-items 1, 3, 4, 5, 7, 8, 9, 10. **Medium** for sub-items 2 (baggage) and 6 (structured scorer reasoning) — both have a real-world data gap that must be handled defensively, not assumed away.

### 1.2 Findings
1. **Baggage data is NOT reliably present in SerpAPI responses.** I inspected all 25 cached responses in `data/serpapi_cache/`. Result: `booking_options` is empty in every cached file; flight-leg `extensions` arrays contain 137 distinct strings, **zero of which mention `bag`, `carry`, `checked`, or `luggage`** — only legroom, power, Wi-Fi, carbon emissions. The spec's claim "the data is in the response, we just don't parse it" is partially false against `deep_search=true` (our default). Baggage parsing must therefore (a) attempt SerpAPI extraction, (b) fall back to airline-level defaults, (c) honour user `baggage_needs` preference, and (d) the `test_serpapi_baggage` fixture-based test must be reformulated — the existing fixtures cannot satisfy "at least one has `baggage_estimate.outbound.checked > 0`" without new captures or synthetic fixtures. **Tester must mock SerpAPI baggage extensions at unit-test level**, not rely solely on cached fixtures. This is the single biggest risk in R7.
2. **`telegram.py` cost-breakdown logic is duplicated across 4 sites** (lines 195–206, 385–396, plus error fare and follow-up have NO breakdown). Sub-item 1 (helper extraction) is a clear net-positive refactor — about 30 lines deleted. Do this BEFORE sub-items 2/3/4 land or they will land inconsistent variants.
3. **Migration ordering is straightforward** — `db.py:_run_migrations` already follows an idempotent `_has_column → ALTER TABLE` pattern (db.py:189–209). Four new columns slot cleanly in. No rollback needed; SQLite + idempotent ALTERs.
4. **Callback prefix consolidation collides with in-flight messages.** Today's callback parser (`commands.py:679`) splits on the FIRST `:`. New `deal:book:{id}` / `route:snooze:7:{id}` prefixes use `:` as a multi-segment separator. Splitting twice (`split(":", 2)`) keeps backwards compatibility, but legacy single-segment prefixes (`book:{id}`, `wait:{id}`, `digest_booked:{id}`, etc.) must continue to work as aliases for at least 30 days so any unread Telegram messages don't break when users click them.
5. **Multi-user code paths exist but are barely exercised.** All new tables/columns must continue scoping by `user_id` (e.g. `routes.snoozed_until` is already user-scoped because routes are; `users.last_digest_fingerprint` and `users.baggage_needs` are user-keyed; `price_snapshots.baggage_estimate` is user-snapshot scoped). No multi-user expansion needed.
6. **Scorer JSON contract change has small but real client-impact.** Today `reasoning` is a free-text string saved to `deals.reasoning`. Switching to a structured object means the renderer (telegram.py) must reformat, AND a backwards-compat path is needed for old deal records read during follow-up rendering. Strategy: write structured `reasoning_json` column to `deals`, keep `reasoning` as a flattened bullet-string for legacy reads.
7. **No "what moved" digest data is computed today.** Digest currently fetches latest snapshot per route. The "concrete summary" header (sub-item 8) needs the *previous* snapshot per route to compute deltas. This should be derived from `price_snapshots` directly (no new column) — the fingerprint column is for the SKIP decision, not the header text.
8. **Data flow for Sub-item 3 (transparency)** has a subtle ordering bug today: `_latest_nearby_comparison` is mutated in `_poll_secondary_airports_for_snapshot` (orchestrator:917–936). When secondary results return zero entries, the entry is *popped* — losing the "we checked but found nothing competitive" signal. The fix is to store **two** lists per route: `competitive` and `evaluated`, and never pop. Builder must update both call sites.

### 1.3 Conditions (Builder MUST follow)
| # | Condition | Reason |
|---|-----------|--------|
| C1 | All DB migrations are idempotent (`if not _has_column → ALTER TABLE`). Order: A1 → A2 → A3 → A4 (see §5). | Existing pattern; no rollback story for SQLite. |
| C2 | Callback dispatcher must `data.split(":", 2)` and accept BOTH `deal:book:{id}` AND legacy `book:{id}`. Legacy aliases stay for 30 days minimum (until 2026-06-08). | In-flight messages from before R7 must keep working. |
| C3 | Cost-breakdown helper lands FIRST (Task 4) before any other telegram.py edit. All 4 message types switch to it in a single PR-segment. | Avoids 4 inconsistent variants drifting. |
| C4 | Baggage parsing must NEVER throw on missing/malformed booking_options or extensions — log + return zero. | Real responses sometimes lack the data entirely. |
| C5 | Baggage display rule: only show `+ €N bags` if `baggage_estimate.total > 0`. Suppress the line on zero-cost airlines (KLM long-haul, etc.) — don't print "+ €0 bags". | Avoids visual noise on routes where bags are included. |
| C6 | `_latest_nearby_comparison[route_id]` becomes a dict `{"competitive": [...], "evaluated": [...]}`, NOT a list. Both call sites updated. Never `pop()` on empty `evaluated`. | Sub-item 3 transparency depends on this. |
| C7 | Scorer outputs structured `reasoning` (object, 3 fields). DB stores it under new column `deals.reasoning_json` (TEXT, JSON). The legacy `deals.reasoning` column gets a flattened bullet-string for back-compat. | Old code paths still read `reasoning`. |
| C8 | Snooze enforcement happens in TWO places: `poll_routes()` filters out snoozed routes from per-user route lists; `send_daily_digest()` filters likewise. Neither place deletes data — snoozes are time-based. | Symmetry; user shouldn't see digest about a route they just snoozed. |
| C9 | Auto-snooze on `booked` feedback fires via the EXISTING feedback callback paths (sub-item 9 callbacks AND legacy `book:` / `digest_booked:` aliases). Lookup deal → route, then `UPDATE routes SET snoozed_until = now + 30d`. | Both code paths must call the same helper. |
| C10 | "📊 Details" placeholder URL = Google Flights deep link (already computed via `_google_flights_url()`). Do NOT add a new endpoint or stub server. | Sub-item 7 is layout-only; ITEM-049 will replace the URL later. |

### 1.4 Resolved Open Questions
1. **Baggage fallback table location:** **NEW MODULE `src/utils/baggage.py`.** Reasoning: `airlines.py` is a 53-line flat code-name dict. Adding baggage policy keyed by `(airline_code, route_class, leg_distance)` is structurally different — it's policy data with logic (defaults, overrides, "long-haul" detection), not a name lookup. New module keeps `airlines.py` simple and lets `baggage.py` evolve (e.g. region-specific rules) without polluting it.
2. **`Skip route` semantics:** **Snoozes the single route ONLY.** Reasoning: a multi-airport "destination city" is modeled as multiple routes (e.g. AMS→TYO and AMS→NRT may both exist). Snoozing all siblings would surprise the user — they may want to keep one airport active. Show in confirmation toast: `"Snoozed AMS→NRT for 7 days. Other routes to TYO unchanged."`.
3. **Daily digest skip telemetry:** **Surface in `/status` only.** Reasoning: log-only is invisible to Barry; surfacing makes the "we're working in the background" signal explicit. Format in `/status`: `"Digest skipped 3 of last 7 days (no significant price moves)"`. Logged at INFO level too, but `/status` is the user-facing surface.

### 1.5 Architecture Debt Ledger Update
| Δ | Item | Notes |
|---|------|-------|
| **Resolved** | Cost breakdown duplicated 4× in telegram.py | Helper extraction (sub-item 1). |
| **Resolved** | Inconsistent callback prefixes | Consolidation (sub-item 9). |
| **Resolved** | Free-text scorer reasoning (hard to render, hard to test) | Structured contract (sub-item 6). |
| **Resolved** | Silent omission of nearby airports below €75 (UX debt) | Two-list contract (sub-item 3). |
| **Introduced** | New `src/utils/baggage.py` module — fallback table to maintain | Tracked here. Bounded — 1 file, ~80 LOC. |
| **Introduced** | Two reasoning columns on `deals` (`reasoning`, `reasoning_json`) | Back-compat tax. Plan: drop `reasoning` after 60 days of clean reads. |
| **Net change** | **Debt down.** R7 removes more than it adds. | |

---

## 2. Release scope (ITEM-051's 10 sub-items)

| # | Title | Files | Acceptance Criterion (one-line) |
|---|-------|-------|---------------------------------|
| 1 | Unified cost-breakdown helper | `src/alerts/telegram.py` | All 4 message types call `_format_cost_breakdown(...)` — no inline duplication. |
| 2 | Baggage parsing + display | `src/apis/serpapi.py`, `src/utils/baggage.py` (new), `src/storage/{db,models}.py`, `src/alerts/telegram.py`, `src/config.py`, `src/orchestrator.py` | Parse SerpAPI when present; fallback to airline table; render `+ €N bags` only when total > 0. |
| 3 | "We checked X" transparency | `src/analysis/nearby_airports.py`, `src/orchestrator.py`, `src/alerts/telegram.py` | Footer present in all 3 cases (all-saved / none-saved / mixed) per spec rules. |
| 4 | "Watching 👀" button on alerts and digest | `src/alerts/telegram.py`, `src/bot/commands.py` | Three-button row on deal alerts AND digest (Book / Watching / Skip route). |
| 5 | Per-route snooze | `src/storage/{db,models}.py`, `src/orchestrator.py`, `src/bot/commands.py` | `routes.snoozed_until` respected in `poll_routes()` and `send_daily_digest()`; auto-snooze 30d on `booked`. |
| 6 | Structured scorer reasoning | `src/analysis/scorer.py`, `src/storage/{db,models}.py`, `src/alerts/telegram.py` | `DealScore.reasoning` returns structured 3-field object; rendered as 3 bullet lines. |
| 7 | "📊 Details" button placeholder | `src/alerts/telegram.py` | Fourth row button on deal alerts; URL = Google Flights deep link. |
| 8 | Smarter daily digest | `src/orchestrator.py`, `src/storage/db.py` | Skip when no price moved >€10 AND no new deals AND <3 days; concrete header otherwise. |
| 9 | Callback prefix consolidation | `src/bot/commands.py` | New `deal:*` / `route:*` handlers; legacy aliases retained. |
| 10 | `/status` command | `src/bot/commands.py` | Outputs routes / snoozed / last-poll / alerts-this-week / SerpAPI usage / savings link. |

**Out of scope (do NOT touch):** ARCHITECTURE.md Phase A migration; ITEM-049 Mini Web App; ITEM-038 discovery; ITEM-045 onboarding transport; ITEM-011 weekend windowing; multi-user expansion beyond what exists.

---

## 3. Architectural context (~200 words)

FareHound is single-process Python asyncio. `Orchestrator` (orchestrator.py) holds the event loop and owns the SerpAPI client, scorer, Telegram notifier, TripBot, scheduler, and DB. APScheduler runs `poll_routes` (every 24h), `send_daily_digest` (cron), `_check_pending_feedback` (hourly). All DB I/O is sync sqlite3 wrapped in `loop.run_in_executor`.

**Multi-user model:** Every domain table (`routes`, `price_snapshots`, `deals`, `poll_windows`, `airport_transport`, `savings_log`) carries a `user_id` column added by migration. New columns added in R7 follow the same convention: `users.baggage_needs`, `users.last_digest_fingerprint` are user-keyed; `routes.snoozed_until` inherits scoping from `routes.user_id`; `price_snapshots.baggage_estimate` is per-snapshot.

**Alert flow (deal alert path):** `poll_routes()` → per route per window → `_store_result_for_user` → `_check_alerts` (pre-filter, score with Claude, dedup, defer) → at end of cycle `_send_deferred_alert` (one alert per route). For each deferred alert, `_poll_secondary_airports_for_snapshot` fires once to fill `_latest_nearby_comparison[route_id]`, then `telegram_notifier.send_deal_alert(deal_info, chat_id)` renders.

**Digest flow:** APScheduler cron → `send_daily_digest()` → per approved user → routes filtered by `get_routes_with_pending_deals` → per route, latest snapshot + 7-day trend + recent watch deals + nearby comparison → `telegram.send_daily_digest(summaries)`.

**Test infrastructure:** pytest + pytest-asyncio. `SERPAPI_CACHE_DIR` env var enables offline replay (25 cached responses currently — none with baggage data, see Finding #1).

---

## 4. Build dependency DAG

```
T1 db_migrations  ──┬──>  T9 snooze_infra  ──┬──>  T11 status_command
                    │                          │
                    ├──>  T10 digest_skip ─────┘
                    │
                    └──>  T2 models_update  ──>  T3 serpapi_baggage_parsing  ──┐
                                                                                │
T4 cost_breakdown_helper  ────────────────────────────────────────────────────┐ │
                                                                              │ │
T5 nearby_airports_two_lists  ──>  T6 transparency_data_assembly  ────────────┤ │
                                                                              ▼ ▼
                                                                     T7 telegram_unified
                                                                              │
                                                                              ▼
                                                                     T8 watching_skip_buttons
                                                                              │
                                                  T12 scorer_json (independent) ──┐
                                                                                  ▼
                                                                     [Builder DONE]
                                                                              │
                                                                              ▼
                                                  Tester picks up T13–T19 in parallel
```

T13 callback consolidation has NO blockers and can land at any point Builder chooses.

---

## 5. Migration plan (`db.py:init_schema` additions)

Append to `db.py:init_schema`, after the existing `_USER_ID_TABLES` block, BEFORE `_migrate_default_user()`. All idempotent.

```python
# A1: routes.snoozed_until — per-route snooze (sub-item 5)
if not _has_column(self._conn, "routes", "snoozed_until"):
    self._conn.execute("ALTER TABLE routes ADD COLUMN snoozed_until TEXT")

# A2: users.baggage_needs — preference (sub-item 2)
if not _has_column(self._conn, "users", "baggage_needs"):
    self._conn.execute(
        "ALTER TABLE users ADD COLUMN baggage_needs TEXT DEFAULT 'one_checked'"
    )

# A3: users.last_digest_fingerprint — digest skip gating (sub-item 8)
if not _has_column(self._conn, "users", "last_digest_fingerprint"):
    self._conn.execute("ALTER TABLE users ADD COLUMN last_digest_fingerprint TEXT")
if not _has_column(self._conn, "users", "last_digest_sent_at"):
    self._conn.execute("ALTER TABLE users ADD COLUMN last_digest_sent_at TEXT")
if not _has_column(self._conn, "users", "digest_skip_count_7d"):
    self._conn.execute("ALTER TABLE users ADD COLUMN digest_skip_count_7d INTEGER DEFAULT 0")

# A4: price_snapshots.baggage_estimate — JSON blob (sub-item 2)
if not _has_column(self._conn, "price_snapshots", "baggage_estimate"):
    self._conn.execute("ALTER TABLE price_snapshots ADD COLUMN baggage_estimate TEXT")

# A5: deals.reasoning_json — structured scorer output (sub-item 6)
if not _has_column(self._conn, "deals", "reasoning_json"):
    self._conn.execute("ALTER TABLE deals ADD COLUMN reasoning_json TEXT")

self._conn.commit()
```

**Rollback:** SQLite doesn't support `DROP COLUMN` cleanly; new columns are NULL-tolerated. To roll back R7, redeploy the old image — the columns become dead weight (acceptable). Don't write any migration that drops columns.

**Ordering:** A1–A5 are independent; the order above is just for predictable diff review. None depend on each other.

---

## 6. Scorer JSON contract (sub-item 6)

### 6.1 Contract
The scorer returns:

```json
{
  "score": 0.78,
  "urgency": "watch",
  "reasoning": {
    "vs_dates": "Cheapest of 4 dates polled — Mar 12 saves €60/pp vs others",
    "vs_range": "€80 below Google's typical low (€620–€780) and 12% below your 90-day average",
    "vs_nearby": "AMS is best — €40 cheaper door-to-door than EIN, €70 cheaper than BRU"
  },
  "booking_window_hours": 48
}
```

### 6.2 Field rules
| Field | Required | Format | Example |
|-------|----------|--------|---------|
| `score` | yes | float 0.0–1.0 | `0.78` |
| `urgency` | yes | enum `book_now` \| `watch` \| `skip` | `"watch"` |
| `reasoning.vs_dates` | yes | string, ≤120 chars, references concrete dates | `"Cheapest of 4 dates polled..."` |
| `reasoning.vs_range` | yes | string, ≤120 chars, references Google range OR 90-day avg | `"€80 below Google's typical low..."` |
| `reasoning.vs_nearby` | yes (string, but may be `"No nearby airports configured"` or `"Yours is best"` when N/A) | string, ≤120 chars | `"AMS is best — €40 cheaper..."` |
| `booking_window_hours` | yes | int | `48` |

### 6.3 Rendering rule (telegram.py)
Replace the current italic single-line `_{reasoning}_` with three bullet lines:

```
✓ Cheapest of 4 dates polled — Mar 12 saves €60/pp vs others
✓ €80 below Google's typical low (€620–€780)
✓ AMS is best — €40 cheaper door-to-door than EIN
```

### 6.4 Storage
- **`deals.reasoning_json`** (new): full JSON object as TEXT.
- **`deals.reasoning`** (existing): newline-separated bullet-string of the 3 fields. Kept for back-compat with old reads (e.g. follow-up rendering pulls `reasoning` from old deal records).

### 6.5 Fallback
If Claude returns malformed JSON or any required field is missing, scorer falls back to the existing `_static_fallback` and produces a synthetic 3-field object: `{vs_dates: "…", vs_range: "Static fallback — Claude unavailable", vs_nearby: "Not evaluated this run"}`.

---

## 7. Callback prefix migration (sub-item 9)

### 7.1 Mapping table
| Legacy prefix | New prefix | Action | Alias retained until |
|---------------|------------|--------|----------------------|
| `book:{deal_id}` | `deal:book:{deal_id}` | book button (URL deep-link, not callback) | 2026-06-08 |
| `wait:{deal_id}` | `deal:watch:{deal_id}` | mark `feedback='watching'` | 2026-06-08 |
| `dismiss:{deal_id}` | `deal:dismiss:{deal_id}` | mark `feedback='dismissed'` | 2026-06-08 |
| `watching:{deal_id}` | `deal:watch:{deal_id}` (merged) | mark `feedback='watching'` | 2026-06-08 |
| `booked:{deal_id}` | `deal:book:{deal_id}` (merged with auto-snooze) | mark `feedback='booked'` + auto-snooze route 30d | 2026-06-08 |
| `digest_booked:{deal_id}` | `deal:book:{deal_id}` (alias) | same as above | 2026-06-08 |
| `digest_dismiss:{route_id}:{user_id}` | `route:dismiss:{route_id}:{user_id}` | bulk-dismiss + (sub-item 4) snooze 7d | 2026-06-08 |
| _new_ | `route:snooze:{days}:{route_id}` | set `snoozed_until = now + days` | n/a |
| _new_ | `route:unsnooze:{route_id}` | clear `snoozed_until` | n/a |

### 7.2 Implementation
```python
parts = data.split(":", 2)  # CHANGED from split(":", 1)
if len(parts) == 3:
    domain, action, payload = parts          # deal:watch:abc, route:snooze:7:xyz...
elif len(parts) == 2:
    # Legacy single-segment — alias map
    legacy, payload = parts
    domain, action = _LEGACY_ALIAS.get(legacy, (None, legacy))
else:
    return
```

`_LEGACY_ALIAS = {"book": ("deal", "book"), "wait": ("deal", "watch"), "dismiss": ("deal", "dismiss"), "watching": ("deal", "watch"), "booked": ("deal", "book"), "digest_booked": ("deal", "book"), "digest_dismiss": ("route", "dismiss")}`

Note: `route:snooze:{days}:{route_id}` has 4 segments → use `data.split(":", 3)` only inside the `route:` branch, OR encode days into payload as `{days}:{route_id}`.

### 7.3 Tests required
- New-prefix happy path (T13)
- Legacy-prefix backwards compatibility for every entry in §7.1 (T13)
- Auto-snooze fires on both `deal:book:*` AND legacy `book:*` / `booked:*` / `digest_booked:*` (T17)

---

## 8. Baggage subsystem design (sub-item 2 — most fragile area)

### 8.1 Data shape
`PriceSnapshot.baggage_estimate: dict | None` with shape:
```json
{
  "outbound": {"carry_on": 0, "checked": 40},
  "return":   {"carry_on": 0, "checked": 40},
  "source": "serpapi" | "fallback_table" | "unknown",
  "currency": "EUR"
}
```
Stored as JSON in `price_snapshots.baggage_estimate` TEXT column.

### 8.2 Parsing pipeline (in `serpapi.py`)
1. **Primary:** scan `booking_options[].together.extensions` for strings matching `/(\d+)\s*€.*?(carry|checked)\b/i`.
2. **Secondary:** scan `best_flights[].flights[].extensions` for the same patterns (less common, but present on some carriers).
3. **Fallback:** if no data found, call `src.utils.baggage.estimate(airline_code, leg_distance_km, travel_class)` → returns dict with `source: "fallback_table"`.
4. **Fully unknown:** return `{outbound: {carry_on: 0, checked: 0}, return: {…}, source: "unknown"}`. Renderer suppresses the line on `source == "unknown"`.

### 8.3 Fallback table (in `src/utils/baggage.py`)
Keyed by airline IATA code. Approximate per-direction fees. Long-haul includes 1× checked free for legacy carriers; LCCs charge:

```python
FALLBACK = {
    "KL": {"carry_on": 0, "checked_long_haul": 0, "checked_short_haul": 25},
    "AF": {"carry_on": 0, "checked_long_haul": 0, "checked_short_haul": 30},
    "LH": {"carry_on": 0, "checked_long_haul": 0, "checked_short_haul": 30},
    "BA": {"carry_on": 0, "checked_long_haul": 0, "checked_short_haul": 35},
    "HV": {"carry_on": 12, "checked_long_haul": 35, "checked_short_haul": 30},  # Transavia
    "FR": {"carry_on": 25, "checked_long_haul": 50, "checked_short_haul": 40},  # Ryanair
    "U2": {"carry_on": 8,  "checked_long_haul": 35, "checked_short_haul": 30},  # easyJet
    "W6": {"carry_on": 10, "checked_long_haul": 40, "checked_short_haul": 30},  # Wizz
    # default for unknown airline code
    "_DEFAULT": {"carry_on": 0, "checked_long_haul": 30, "checked_short_haul": 30},
}
LONG_HAUL_KM = 4000
```

Public function:
```python
def estimate(airline_code: str, leg_distance_km: float | None, baggage_needs: str) -> dict:
    """Return per-direction baggage cost based on user preference and airline policy."""
```

### 8.4 User preference (`users.baggage_needs`)
- `carry_on_only` → cost = airline `carry_on` × 1 direction (or 0 if free)
- `one_checked` (default) → cost = `carry_on` + `checked_long_haul`-or-`short_haul` per direction
- `two_checked` → cost = `carry_on` + 2× checked

### 8.5 Display rule
Append `" + €{baggage_total:,.0f} bags"` to cost-breakdown line ONLY if `baggage_total > 0` AND `source != "unknown"`. Otherwise omit silently — don't print "+ €0 bags".

### 8.6 Test approach (Tester)
- Unit test the parser against synthetic SerpAPI fixtures (NOT only the 25 cached responses, which lack baggage data — Finding #1).
- Tester writes ≥3 synthetic fixtures: (a) booking_options with baggage strings, (b) flight-leg extensions with baggage strings, (c) zero data → fallback path.
- Test the fallback table against known airlines (KL, FR, HV).
- Test the user-preference matrix (`carry_on_only` × no-fee airline = €0).

---

## 9. "We checked X" transparency design (sub-item 3)

### 9.1 `compare_airports()` becomes a 2-list contract
```python
def compare_airports(...) -> dict:
    return {
        "competitive": [...],    # savings > €75, sorted desc by savings
        "evaluated":   [...],    # ALL evaluated airports incl. primary, with computed totals
    }
```

`evaluated` always contains every secondary that was queried, even if savings ≤ €75 or negative. Each entry gets `airport_code`, `airport_name`, `fare_pp`, `net_cost`, `delta_vs_primary` (signed; positive means MORE expensive than primary).

### 9.2 Footer rules (telegram.py)
After the existing nearby block, render footer based on `(competitive, evaluated)` shape:

| Case | Render |
|------|--------|
| `competitive` non-empty, no other `evaluated` | (existing nearby block, no footer change) |
| `competitive` empty, `evaluated` non-empty | `✓ Checked {N} airports — your airport is best by €{min_delta}–€{max_delta}` |
| `competitive` non-empty, additional non-competitive in `evaluated` | (existing block) + `…also checked {names} (€{delta} more, skipped)` |
| Both empty | NO footer (didn't poll secondaries this cycle) |

### 9.3 Date transparency (lighter scope)
Add a single line under the deal alert when `price_history` is present:
`✓ Polled {N} dates — {best_date} is cheapest`
Reuse `find_cheapest_date()` (telegram.py:13) — extract the date list, render as `Mar 8 / 12 / 15 / 22`.

### 9.4 Storage change in orchestrator
`self._latest_nearby_comparison[route_id]` becomes `dict[str, dict]` with keys `competitive` / `evaluated`. Both call sites (`_poll_secondary_airports`, `_poll_secondary_airports_for_snapshot`) updated. **Never `pop()` on empty `evaluated`** — keep the entry so transparency can render "we checked but found nothing".

---

## 10. Snooze design (sub-item 5)

### 10.1 Data
- New column `routes.snoozed_until TEXT` (ISO datetime UTC, NULL = not snoozed).
- New `Route` dataclass field `snoozed_until: datetime | None = None`.

### 10.2 Helpers (db.py)
```python
def snooze_route(self, route_id: str, days: int) -> None:
    until = (datetime.now(UTC) + timedelta(days=days)).isoformat()
    self._conn.execute("UPDATE routes SET snoozed_until = ? WHERE route_id = ?", [until, route_id])
    self._conn.commit()

def unsnooze_route(self, route_id: str) -> None:
    self._conn.execute("UPDATE routes SET snoozed_until = NULL WHERE route_id = ?", [route_id])
    self._conn.commit()

def get_active_routes(self, user_id, include_snoozed: bool = False) -> list[Route]:
    # extend existing — filter where snoozed_until IS NULL OR snoozed_until <= now
```

### 10.3 Enforcement
- `orchestrator.poll_routes`: routes are pulled via `get_active_routes(user_id)` which now filters snoozed.
- `orchestrator.send_daily_digest`: same filter via `get_routes_with_pending_deals` (extend SQL to join routes and check `snoozed_until`).

### 10.4 Auto-snooze
On any `feedback='booked'` write (callback handler in `commands.py`), call `db.snooze_route(route_id_for_deal, 30)`. Wire this into the **shared** post-feedback helper so both new (`deal:book:*`) and legacy (`book:*` / `booked:*` / `digest_booked:*`) paths trigger it.

### 10.5 Bot commands
- `/snooze {route_id} {days}` (default 7) → calls `db.snooze_route`, replies `"Snoozed AMS→TYO for 7 days. /unsnooze AMS→TYO to resume."`
- `/unsnooze {route_id}` → calls `db.unsnooze_route`.
- Free-form NL ("snooze Tokyo for 2 weeks") routes through `_INTERPRET_SYSTEM` — out of scope for R7. Slash-only.

---

## 11. Digest fingerprint design (sub-item 8)

### 11.1 Fingerprint
```python
fingerprint = hashlib.sha256(
    json.dumps(
        sorted(
            {route_id: round(lowest_price, 0) for route_id, lowest_price in pairs},
            key=lambda kv: kv[0],
        ),
        sort_keys=True,
    ).encode()
).hexdigest()[:16]
```
Stored in `users.last_digest_fingerprint`.

### 11.2 Skip predicate (ALL must hold)
- `users.last_digest_fingerprint == new_fingerprint`
- No new deals inserted since `last_digest_sent_at`
- No price moved more than €10 since previous digest snapshot
- Less than 3 days since `last_digest_sent_at`

### 11.3 When skipping
- Log INFO: `"Digest skipped for user {user_id} — fingerprint unchanged, last digest 1d ago"`
- Increment `users.digest_skip_count_7d` (rolled over by `/status` view — derive from `last_digest_sent_at` timestamps if you want true 7d window; simple counter is fine).
- DO NOT update `last_digest_sent_at` (so a real change tomorrow re-evaluates).

### 11.4 When NOT skipping (concrete header)
Replace `"You haven't decided on these yet:"` with:
```
📊 FareHound Daily — {N} routes, {M} prices moved
• AMS→NRT dropped €40 (€1820/pp)        # if change >= €10
• AMS→BKK new low (€620/pp)             # if a new deal landed
• AMS→LIS unchanged                      # otherwise
```
Compute deltas inline in `orchestrator.send_daily_digest` from `latest` snapshot vs. previous (use `get_recent_snapshots(route_id, limit=2)`).

---

## 12. Atomic Task List (Phase C)

> Builder works tasks T1–T13 sequentially within their dependency chain (§4). Tester picks up T14–T19 as the corresponding Builder tasks land. Both should set their current task as `in_progress` and only one task `in_progress` per agent at a time. Use the Builder/Tester `# Status` field below to coordinate (Builder updates Status when starting/completing; Tester monitors).

### Builder tasks

#### **T1 — db_migrations**
- **Owner:** builder
- **Files:** `src/storage/db.py:189` (extend `init_schema`)
- **Blocks:** T2, T9, T10, T11, T12 (everything that touches DB)
- **Acceptance:** all 5 ALTER blocks (A1–A5 from §5) added; idempotent re-run produces no errors; pyproject test `pytest tests/test_db.py -k migration` (Tester writes T16) passes.
- **Notes:** Builder MAY commit T1 standalone before any other code change; this de-risks downstream work.
- **Status:** _done_ [x]

#### **T2 — models_update**
- **Owner:** builder
- **Files:** `src/storage/models.py`
- **Depends on:** T1
- **Blocks:** T3, T9
- **Acceptance:** `Route.snoozed_until: datetime | None = None`; `PriceSnapshot.baggage_estimate: dict | None = None`; `Deal.reasoning_json: dict | None = None`. All `from_row` methods deserialize the new columns (parse JSON for baggage_estimate / reasoning_json). All existing tests pass.
- **Status:** _done_ [x]

#### **T3 — serpapi_baggage_parsing**
- **Owner:** builder
- **Files:** `src/apis/serpapi.py` (parse), `src/utils/baggage.py` (NEW), `src/orchestrator.py` (wire snapshot creation)
- **Depends on:** T2
- **Blocks:** T7
- **Acceptance:** `FlightSearchResult` exposes `parse_baggage(airline_code, leg_distance_km, baggage_needs) → dict`; orchestrator stores result on `PriceSnapshot.baggage_estimate`; new `src/utils/baggage.py` module with FALLBACK table + `estimate()` function (per §8.3). Unit test in T14 (Tester) green.
- **Notes:** Defensive parsing only — no exceptions on missing data. See Condition C4.
- **Status:** _pending_

#### **T4 — cost_breakdown_helper**
- **Owner:** builder
- **Files:** `src/alerts/telegram.py` (extract helper, replace inline duplicates)
- **Depends on:** T1 (optional — DB not actually touched, but order helps)
- **Blocks:** T7
- **Acceptance:** new module-level `_format_cost_breakdown(price, transport, parking, mode, baggage, passengers) -> tuple[str, float]` returns (display_string, total_eur). All 4 inline cost-breakdown sites in `telegram.py` replaced. Unit test (T14) green. Output identical to current behaviour for zero-baggage case.
- **Notes:** Land THIS before T7 to avoid 4 inconsistent variants (Condition C3).
- **Status:** _done_ [x]

#### **T5 — nearby_airports_two_lists**
- **Owner:** builder
- **Files:** `src/analysis/nearby_airports.py`
- **Depends on:** none
- **Blocks:** T6
- **Acceptance:** `compare_airports(...) -> dict` returns `{"competitive": [...], "evaluated": [...]}` per §9.1. All callers in orchestrator updated. Existing nearby tests still green.
- **Status:** _pending_

#### **T6 — transparency_data_assembly**
- **Owner:** builder
- **Files:** `src/orchestrator.py` (`_poll_secondary_airports`, `_poll_secondary_airports_for_snapshot`, `_send_deferred_alert`, `send_daily_digest`)
- **Depends on:** T5
- **Blocks:** T7
- **Acceptance:** `_latest_nearby_comparison[route_id]` is now a dict with `competitive` and `evaluated` keys. **Never `pop()` on empty evaluated.** `deal_info` and digest summaries pass both lists to telegram.py. Existing alert-flow tests still pass.
- **Notes:** Condition C6.
- **Status:** _pending_

#### **T7 — telegram_4_messages_unified**
- **Owner:** builder
- **Files:** `src/alerts/telegram.py`
- **Depends on:** T3, T4, T6, T12
- **Blocks:** T8
- **Acceptance:**
  - `send_deal_alert`, `send_error_fare_alert`, `send_follow_up`, `send_daily_digest` all call `_format_cost_breakdown(...)` including `baggage` arg.
  - 3 reasoning bullets rendered (from new structured `reasoning_json` if present, else fall back to `reasoning` string).
  - Transparency footer rendered per §9.2.
  - Date-transparency line per §9.3.
  - "📊 Details" placeholder button (sub-item 7) added to deal alert keyboard, pointing to `_google_flights_url(deal_info)`.
- **Status:** _pending_

#### **T8 — watching_skip_buttons**
- **Owner:** builder
- **Files:** `src/alerts/telegram.py` (3-button row), `src/bot/commands.py` (handlers)
- **Depends on:** T7, T13
- **Blocks:** none
- **Acceptance:**
  - Deal alert keyboard has 3-button row: `[Book Now ✈️ (URL)] [Watching 👀 (callback deal:watch:{id})] [Skip route 🔕 (callback route:snooze:7:{route_id})]`. "📊 Details" sits on row 2.
  - Daily digest per-route keyboard has the same 3-button row plus "📊 Details" row 2.
  - `route:snooze:7` callback snoozes the route 7d AND bulk-dismisses pending deals on that route.
- **Status:** _pending_

#### **T9 — snooze_infra**
- **Owner:** builder
- **Files:** `src/storage/db.py` (helpers + extend `get_active_routes`/`get_routes_with_pending_deals`), `src/orchestrator.py` (no-op if helpers do the filtering), `src/bot/commands.py` (`/snooze`, `/unsnooze`, auto-snooze hook)
- **Depends on:** T1, T2
- **Blocks:** T11
- **Acceptance:**
  - `db.snooze_route(route_id, days)` and `db.unsnooze_route(route_id)` exist.
  - `db.get_active_routes(user_id)` filters snoozed routes (with optional `include_snoozed=True`).
  - `db.get_routes_with_pending_deals(user_id)` filters snoozed.
  - `/snooze {route_substring} [days]` and `/unsnooze {route_substring}` commands work; auto-snooze fires 30d on `feedback='booked'` from BOTH new and legacy callback paths (Condition C9).
- **Status:** _pending_

#### **T10 — digest_fingerprint_gating**
- **Owner:** builder
- **Files:** `src/storage/db.py` (helpers for fingerprint read/write + `last_digest_sent_at`), `src/orchestrator.py` (`send_daily_digest`)
- **Depends on:** T1
- **Blocks:** T11
- **Acceptance:**
  - Skip predicate (§11.2) implemented; logs `"Digest skipped for user {} — fingerprint unchanged"`.
  - When NOT skipping: header replaced with concrete `"FareHound Daily — N routes, M prices moved"` + per-route delta lines (§11.4).
  - `users.last_digest_fingerprint`, `last_digest_sent_at`, `digest_skip_count_7d` updated on send.
- **Status:** _pending_

#### **T11 — status_command**
- **Owner:** builder
- **Files:** `src/bot/commands.py` (new `_handle_status` + dispatcher entry)
- **Depends on:** T9, T10
- **Blocks:** none
- **Acceptance:** `/status` produces a message containing: monitoring count + snoozed count, last poll time, alerts this week with feedback breakdown, SerpAPI usage `{N}/{HARD_CAP}`, savings line `"Saved you €{X} across {N} trips (/savings)"`, and digest skip count `"Digest skipped {N} of last 7 days"`.
- **Status:** _pending_

#### **T12 — scorer_json_contract**
- **Owner:** builder
- **Files:** `src/analysis/scorer.py`
- **Depends on:** T1, T2
- **Blocks:** T7
- **Acceptance:** `_SCORE_PROMPT` rewritten to require structured `reasoning` object (§6.1). `DealScore.reasoning` is now `dict[str, str]` (3 fields). `_parse_response` validates all 3 sub-fields; falls back to synthetic 3-field object on malformed (§6.5). Orchestrator stores `reasoning_json` AND a flattened bullet-string in `reasoning` (Condition C7).
- **Status:** _pending_

#### **T13 — callback_prefix_consolidation**
- **Owner:** builder
- **Files:** `src/bot/commands.py` (`_handle_callback`)
- **Depends on:** none (independent)
- **Blocks:** T8 (consumer)
- **Acceptance:**
  - `data.split(":", 2)` plus `_LEGACY_ALIAS` map per §7.2.
  - All new prefixes work: `deal:book`, `deal:watch`, `deal:dismiss`, `route:snooze:{days}:{id}`, `route:unsnooze:{id}`, `route:dismiss:{id}:{user_id}`.
  - All legacy prefixes still work (Condition C2): `book:`, `wait:`, `dismiss:`, `watching:`, `booked:`, `digest_booked:`, `digest_dismiss:`.
- **Status:** _pending_

### Tester tasks

#### **T14 — tests_telegram (each of 4 message types)**
- **Owner:** tester
- **Files:** `tests/test_telegram.py`
- **Picks up after:** T7
- **Acceptance:** for each of `send_deal_alert`, `send_error_fare_alert`, `send_follow_up`, `send_daily_digest`:
  - Assert cost breakdown line present.
  - Assert baggage line included when `baggage_estimate.total > 0`; absent when total = 0 OR `source == 'unknown'`.
  - Assert "we checked" footer in all 3 cases (all-saved / none-saved / mixed).
  - Assert 3-button row (Book / Watching / Skip route) present on deal alert AND daily digest.
  - Assert "📊 Details" button present.
  - Assert reasoning rendered as 3 bullet lines from `reasoning_json`, with fallback to single-line `reasoning` string.

#### **T15 — tests_serpapi_baggage**
- **Owner:** tester
- **Files:** `tests/test_serpapi_baggage.py` (NEW)
- **Picks up after:** T3
- **Acceptance:**
  - **Synthetic fixtures** for: (a) booking_options with baggage strings, (b) flight-leg extensions with baggage strings, (c) zero data.
  - Assert parser extracts baggage from (a) and (b), and falls back from (c).
  - Assert fallback table behaviour for KL (long-haul free), FR (LCC fees), HV (LCC fees), unknown airline (`_DEFAULT`).
  - Assert preference matrix: `carry_on_only` × KL = €0; `one_checked` × FR short-haul = €40 each direction.
  - The 25 cached SerpAPI responses (which lack baggage data) are used as integration smoke — assert they go through the parser without throwing.
- **Notes:** This is the **highest-risk test** in R7 — Finding #1.

#### **T16 — tests_db_migrations**
- **Owner:** tester
- **Files:** `tests/test_db.py`
- **Picks up after:** T1
- **Acceptance:**
  - Assert all 5 columns exist after `init_schema()`.
  - Assert idempotency: second `init_schema()` call doesn't error.
  - Round-trip test: `snooze_route` + `get_active_routes` excludes; `unsnooze_route` re-includes.
  - Round-trip test: `users.last_digest_fingerprint`, `users.baggage_needs`, `price_snapshots.baggage_estimate`, `deals.reasoning_json`.

#### **T17 — tests_orchestrator (digest skip + snooze + auto-snooze)**
- **Owner:** tester
- **Files:** `tests/test_orchestrator.py`
- **Picks up after:** T9, T10
- **Acceptance:**
  - Digest skip predicate test — fingerprint unchanged + <3d → skipped; fingerprint changed → sent; >3d since last digest → sent regardless.
  - Snooze respected in `poll_routes` — snoozed route NOT polled.
  - Snooze respected in `send_daily_digest` — snoozed route NOT in summaries.
  - Auto-snooze on `booked` feedback fires for both new (`deal:book:*`) AND legacy (`book:*`, `booked:*`, `digest_booked:*`) callback paths.

#### **T18 — tests_scorer (structured reasoning)**
- **Owner:** tester
- **Files:** `tests/test_scorer.py`
- **Picks up after:** T12
- **Acceptance:**
  - Mock Claude returns valid 3-field `reasoning` object → asserted on returned `DealScore`.
  - Mock Claude returns malformed JSON → fallback to synthetic 3-field object (per §6.5).
  - Mock Claude returns missing `vs_nearby` → fallback fires.
  - Renderer test (in test_telegram covered by T14, but unit-level here) — flattening to bullet-string for `deals.reasoning` legacy column produces 3 lines.

#### **T19 — integration_test_end_to_end** (NON-NEGOTIABLE per /release Phase 4 step 6b)
- **Owner:** tester
- **Files:** `tests/test_v07_release.py` (NEW; follows v0.7/v0.8 pattern)
- **Picks up after:** T7, T8, T17
- **Acceptance:** end-to-end flow with mocked HTTP only:
  - 1 user, 1 route AMS→TYO, primary AMS + secondary EIN/BRU configured.
  - Mock SerpAPI primary response (with synthetic baggage in extensions), 2 secondaries.
  - Mock Claude scorer to return structured 3-field reasoning.
  - Run `orchestrator.poll_routes()`.
  - Assert: deal alert sent with cost breakdown including baggage line, 3 reasoning bullets, "we checked 2 airports" footer (because EIN/BRU don't beat €75 threshold), 3-button keyboard, "📊 Details" button, callback data uses NEW `deal:*` / `route:*` prefixes.
  - Assert: clicking `deal:book:{id}` callback marks deal booked AND auto-snoozes route 30d.
  - Assert: subsequent `send_daily_digest` skips that user (route is snoozed).

---

## 13. Definition of Done

A release is shippable when ALL of:

- [ ] All 13 Builder tasks complete with their named acceptance criterion satisfied.
- [ ] All 6 Tester tasks have green tests (`pytest tests/`).
- [ ] Existing 311 tests still pass.
- [ ] `pytest tests/test_v07_release.py` (T19) passes.
- [ ] `farehound/src/` synced from root `src/` per CLAUDE.md.
- [ ] `farehound/config.yaml` version bumped (current 0.8.x → 0.9.0 — R7 is a Take-Action-quality release).
- [ ] Architect-Lead returns for post-build audit:
  - Read-through of every changed file.
  - Confirms every Condition (C1–C10) is met in code.
  - Confirms backwards-compat for callbacks (Condition C2).
  - Confirms baggage display rule (Condition C5) — no "+ €0 bags" in test fixtures.
  - Confirms two-list nearby footer renders in all 3 cases.
- [ ] Deployed to HA per CLAUDE.md flow:
  - `ssh barry@homeassistant.local`
  - `sudo docker exec hassio_cli ha apps stop 30bba4a3_farehound`
  - `sudo docker exec hassio_cli ha store reload`
  - `sudo docker exec hassio_cli ha apps update 30bba4a3_farehound`
  - `sudo docker exec hassio_cli ha apps start 30bba4a3_farehound`
- [ ] Post-deploy log verification:
  - "Database schema initialized" present.
  - All 5 ALTER columns present in DB (Tester or Architect spot-checks).
  - First poll cycle runs without ERROR lines (except pre-existing Telegram channel auth).

---

## 14. Communication protocol

- **Builder** sends Architect-Lead a one-line message when each task transitions Pending → InProgress and InProgress → Done. Use TodoWrite for own granular tracking.
- **Tester** does the same.
- **Either** pings Architect-Lead immediately if they hit an architectural blocker (not a question — a blocker). For questions, attempt the documented decision in §1.4 / Conditions §1.3 and proceed.
- **Architect-Lead** stays idle until Builder + Tester both report Done. Then runs the post-build audit.

---

_End of plan. Builder + Tester: claim T1 / T15 next._
