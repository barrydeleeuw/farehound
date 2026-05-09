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

> **How to claim a task.** Each task header carries a checkbox: `[ ]` available, `[~]` in_progress, `[x]` done. To claim: change `[ ]` → `[~]` in the **header** line and add your handle to `Owner:` if not already set. To complete: change `[~]` → `[x]` and post a one-line "T{N} done" message to team-lead. **Only one task `[~]` per agent at a time.** The legacy `Status:` line at the bottom of each task is being phased out — the header checkbox is authoritative.
>
> **Reading order.** Each task is self-contained: header (owner / depends-on / blocks / files with line refs) → one-line acceptance → detail bullets. Builder reads T1 → T13 in dependency order; Tester reads T14 → T19 and starts the test scaffold for any task whose Builder dependency has reached `[~]`.
>
> **Line refs** are anchor points to navigate the codebase — exact numbers may drift as edits land; use them to locate, not to assume code there is unchanged.

### Builder tasks

#### `[x]` T1 — db_migrations
- **Owner:** builder
- **Depends on:** —
- **Blocks:** T2, T9, T10, T11, T12, T16
- **Files:** `src/storage/db.py:189` (extend `init_schema`)
- **Acceptance (one-line):** All 5 ALTER blocks (A1–A5 from §5) added with idempotent `_has_column` guard; `init_schema()` re-run produces no errors.
- **Detail:**
  - Insert the 5 ALTER blocks from §5 after the existing `_USER_ID_TABLES` migration loop and the follow-up / approved column migrations, BEFORE `_migrate_default_user()`.
  - Tester verifies in T16.
  - Builder MAY commit standalone before anything else lands — de-risks downstream work and lets Tester start T16 immediately.

#### `[x]` T2 — models_update
- **Owner:** builder
- **Depends on:** T1
- **Blocks:** T3, T9, T12
- **Files:** `src/storage/models.py:44` (`Route`), `:159` (`PriceSnapshot`), `:230` (`Deal`)
- **Acceptance (one-line):** Three new dataclass fields added, `from_row` deserializes them via existing `_parse_json` helper, existing tests still pass.
- **Detail:**
  - `Route.snoozed_until: datetime | None = None` (added to dataclass, `to_dict`, `from_row`).
  - `PriceSnapshot.baggage_estimate: dict | None = None` (parse JSON in `from_row`).
  - `Deal.reasoning_json: dict | None = None` (parse JSON in `from_row`).

#### `[x]` T3 — serpapi_baggage_parsing
- **Owner:** builder
- **Depends on:** T2
- **Blocks:** T7, T15
- **Files:**
  - `src/apis/serpapi.py:184` (extend `FlightSearchResult` construction in `search_flights`)
  - `src/utils/baggage.py` (NEW; FALLBACK table + `estimate()` per §8.3)
  - `src/orchestrator.py:547` and `:733` (snapshot creation sites — populate `baggage_estimate`)
- **Acceptance (one-line):** Every new `PriceSnapshot` carries a `baggage_estimate` dict with `source` ∈ `{serpapi, fallback_table, unknown}`; parser never raises (Condition C4).
- **Detail:**
  - Add module-level helper `parse_baggage_extensions(booking_options, best_flights, other_flights) -> dict | None` in `serpapi.py` per §8.2.
  - Create `src/utils/baggage.py` with `FALLBACK` table + `estimate(airline_code, leg_distance_km, baggage_needs) -> dict` per §8.3.
  - In orchestrator snapshot-creation sites, derive `airline_code` from `best_flight.flights[0].airline`, `leg_distance_km` proxy from `duration × 800` (default 2000 km if unavailable), look up user's `baggage_needs` (default `"one_checked"`).
  - Cached SerpAPI fixtures lack baggage data (Finding #1) — Tester writes synthetic fixtures in T15.

#### `[x]` T4 — cost_breakdown_helper
- **Owner:** builder
- **Depends on:** —
- **Blocks:** T7
- **Files:** `src/alerts/telegram.py:195–206` (deal alert inline), `:385–396` (digest inline). Helper sits at module level near `_format_flight_line` (`:93`).
- **Acceptance (one-line):** Single `_format_cost_breakdown(price, transport, parking, mode, baggage, passengers) -> tuple[str, float]` consumed by all 4 message types; zero-baggage output identical to current behaviour.
- **Detail:**
  - Replace inline duplicates at `:195–206` and `:385–396`.
  - Error-fare and follow-up paths get the helper added in T7 (they have no breakdown today).
  - Land BEFORE T7 (Condition C3) so Builder doesn't fork 4 inconsistent variants.

#### `[x]` T5 — nearby_airports_two_lists
- **Owner:** builder
- **Depends on:** —
- **Blocks:** T6
- **Files:** `src/analysis/nearby_airports.py:38` (`compare_airports`)
- **Acceptance (one-line):** `compare_airports(...) -> dict` returns `{"competitive": [...], "evaluated": [...]}` per §9.1; existing nearby tests still green.
- **Detail:**
  - Today returns `list[dict]` filtered by `savings_threshold` (`:71`). Change to dict shape.
  - `evaluated` includes ALL secondaries that were costed (incl. negative-savings) with new `delta_vs_primary` field; `competitive` is the existing >€75 list.
  - Keep `savings_threshold` parameter — used to split the two lists, not to hide entries.
  - Existing `tests/test_nearby_airports.py` updated in this task.

#### `[x]` T6 — transparency_data_assembly
- **Owner:** builder
- **Depends on:** T5
- **Blocks:** T7
- **Files:** `src/orchestrator.py:799–820` (in `_poll_secondary_airports`), `:917–936` (in `_poll_secondary_airports_for_snapshot`), `:1136` (`deal_info` assembly), `:1261–1264` (digest summary nearby block); type annotation at `:122`
- **Acceptance (one-line):** `_latest_nearby_comparison[route_id]` becomes `{"competitive": [...], "evaluated": [...]}`; both lists flow into `deal_info` and digest summaries; never `pop()` on empty `evaluated` (Condition C6).
- **Detail:**
  - `_latest_nearby_comparison: dict[str, dict]`.
  - At both secondary-poll sites, store the dict returned by T5's new `compare_airports` shape — keep the entry even when `competitive` is empty so the renderer can show "we checked but found nothing competitive".
  - In `_send_deferred_alert`, pass `nearby_comparison=self._latest_nearby_comparison.get(route.route_id, {"competitive": [], "evaluated": []})` to `deal_info`.
  - In `send_daily_digest`, populate `summary["nearby"]` with the same dict shape (rename existing `"nearby_prices"` → `"nearby"` so telegram.py reads consistently).

#### `[ ]` T7 — telegram_4_messages_unified
- **Owner:** builder
- **Depends on:** T3, T4, T6, T12
- **Blocks:** T8, T14
- **Files:** `src/alerts/telegram.py:165` (`send_deal_alert`), `:288` (`send_error_fare_alert`), `:329` (`send_follow_up`), `:349` (`send_daily_digest`)
- **Acceptance (one-line):** All 4 message types render: cost-breakdown helper output (incl. baggage line when total>0), 3-bullet structured reasoning, "we checked X" footer per §9.2, date-transparency line per §9.3, "📊 Details" button.
- **Detail:**
  - Wire `_format_cost_breakdown` (T4) into all 4 paths — error-fare and follow-up get a breakdown for the first time.
  - Render `reasoning_json` (T12) as 3 bullet lines (`✓ ...`); fall back to single-line `_reasoning_` only when `reasoning_json` is missing (legacy deal records).
  - Implement footer cases from §9.2 using the `competitive` / `evaluated` lists from T6.
  - Add date-transparency line under the deal alert when `price_history` is present (§9.3).
  - Add "📊 Details" button as second keyboard row, URL = `_google_flights_url(deal_info)` (Condition C10 — placeholder only).
  - Keyboard row 1 modifications (3-button row) come in T8.

#### `[ ]` T8 — watching_skip_buttons
- **Owner:** builder
- **Depends on:** T7, T13
- **Blocks:** T19
- **Files:**
  - `src/alerts/telegram.py:276–284` (deal alert keyboard), `:471–477` (digest keyboard)
  - `src/bot/commands.py:674` (callback dispatcher — extend the `deal:*` / `route:*` branches added by T13)
- **Acceptance (one-line):** Three-button row (Book / Watching / Skip route) on deal alerts AND digest; `route:snooze:7:{route_id}` snoozes route 7d AND bulk-dismisses pending deals.
- **Detail:**
  - Deal alert keyboard row 1: `[Book Now ✈️ (URL)] [Watching 👀 (callback deal:watch:{deal_id})] [Skip route 🔕 (callback route:snooze:7:{route_id})]`. Row 2: `[📊 Details (URL)]` (added by T7).
  - Digest per-route keyboard mirrors row 1 plus row 2 with Details.
  - The `route:snooze:7:*` handler MUST: (a) call `db.snooze_route(route_id, 7)`, (b) call `db.bulk_dismiss_route_deals(route_id, user_id)` so pending deals on that route disappear from the next digest.

#### `[x]` T9 — snooze_infra
- **Owner:** builder
- **Depends on:** T1, T2
- **Blocks:** T11, T17
- **Files:**
  - `src/storage/db.py:305` (extend `get_active_routes`), `:548` (extend `get_routes_with_pending_deals`), append `snooze_route` / `unsnooze_route` helpers
  - `src/orchestrator.py:392` and `:1170` (callers — should be no-op once db helpers filter)
  - `src/bot/commands.py:377` (slash dispatch — add `/snooze`, `/unsnooze`), `:793–814` (extend `book` / `booked` / `digest_booked` callback paths to call auto-snooze helper)
- **Acceptance (one-line):** Snooze respected in poll loop and digest; `/snooze` / `/unsnooze` slash commands work; auto-snooze 30d fires on `feedback='booked'` from BOTH new and legacy callback paths (Condition C9).
- **Detail:**
  - `db.snooze_route(route_id, days)` and `db.unsnooze_route(route_id)` (§10.2).
  - Extend `get_active_routes(user_id, include_snoozed=False)`: SQL filter `WHERE snoozed_until IS NULL OR snoozed_until <= datetime('now')` unless flag set.
  - Extend `get_routes_with_pending_deals` SQL to JOIN routes and apply same filter.
  - Wire auto-snooze: introduce `_apply_booked_feedback(deal_id)` helper in `commands.py` that does `update_deal_feedback + UPDATE deals SET booked=1 + db.snooze_route(route_id, 30)`. Every callback path that marks `feedback='booked'` calls this helper.
  - `/snooze {route_id_or_destination_substring} [days=7]` resolves via case-insensitive match against `origin/destination/route_id`; reply includes sibling-route note (§1.4 OQ#2).

#### `[x]` T10 — digest_fingerprint_gating
- **Owner:** builder
- **Depends on:** T1
- **Blocks:** T11, T17
- **Files:**
  - `src/storage/db.py` (append helpers `get_user_digest_state`, `update_user_digest_state`)
  - `src/orchestrator.py:1155` (`send_daily_digest` — predicate + concrete header)
- **Acceptance (one-line):** Skip predicate (§11.2) implemented; concrete "what moved" header rendered when sending (§11.4); fingerprint + last_digest_sent_at + skip count updated.
- **Detail:**
  - Compute fingerprint per §11.1.
  - All four conditions in §11.2 must hold to skip. On skip: log INFO, increment `digest_skip_count_7d`, do NOT update `last_digest_sent_at`.
  - On send: build delta lines from `db.get_recent_snapshots(route_id, limit=2)`; render `"Daily — N routes, M prices moved"` header per §11.4.
  - Reset `digest_skip_count_7d` to 0 when last_digest_sent_at is older than 7 days (rolling window approximation — sufficient for `/status` display).

#### `[ ]` T11 — status_command
- **Owner:** builder
- **Depends on:** T9, T10
- **Blocks:** —
- **Files:** `src/bot/commands.py:401` (extend slash dispatch — add `/status` branch); append `_handle_status` near `_handle_savings:1246`
- **Acceptance (one-line):** `/status` produces the formatted block from §1's spec — snoozed count, last poll, alert breakdown, SerpAPI usage, savings link, digest skip telemetry.
- **Detail:**
  - Pull data: `db.get_active_routes(include_snoozed=True)` then count snoozed; last poll time from most recent snapshot; alerts-this-week from `db.get_deals_since(since=now-7d)` grouped by feedback; SerpAPI usage from `orchestrator.serpapi._calls_this_month` (need to expose via TripBot — pass at construction OR query DB count of snapshots in current month as proxy if injection is too invasive); savings via `db.get_total_savings(user_id)`; digest skip via `users.digest_skip_count_7d`.
  - If injecting the SerpAPI counter is too invasive, use snapshot-count-this-month as a documented proxy and add a TODO comment referencing Architect-Lead.

#### `[x]` T12 — scorer_json_contract
- **Owner:** builder
- **Depends on:** T1, T2
- **Blocks:** T7, T18
- **Files:**
  - `src/analysis/scorer.py:14–47` (rewrite `_SYSTEM_PROMPT`), `:49–86` (rewrite `_SCORE_PROMPT`'s response section), `:96` (`DealScore` dataclass), `:332` (`_parse_response`)
  - `src/orchestrator.py:1002` (Deal construction — also write `reasoning_json`), `:1577` (`_static_fallback`)
- **Acceptance (one-line):** Scorer returns structured 3-field `reasoning` object per §6.1; `DealScore.reasoning` is `dict[str, str]`; falls back to synthetic 3-field on malformed (§6.5); orchestrator persists both `reasoning_json` and a flattened bullet-string in `reasoning` (Condition C7).
- **Detail:**
  - Update prompt to instruct Claude to return JSON shape from §6.1 with all 3 reasoning sub-fields.
  - `DealScore.reasoning: dict[str, str]` (was `str`).
  - `_parse_response` validates all 3 keys present; on missing/malformed, return synthetic 3-field per §6.5.
  - Where orchestrator constructs `Deal(...)`: `Deal.reasoning_json = score_result.reasoning` (the dict), `Deal.reasoning = "\n".join(f"✓ {v}" for v in score_result.reasoning.values())` (flattened).
  - Update `_static_fallback` to also produce synthetic 3-field reasoning.

#### `[x]` T13 — callback_prefix_consolidation
- **Owner:** builder
- **Depends on:** —
- **Blocks:** T8
- **Files:** `src/bot/commands.py:674` (`_handle_callback`), `:679` (the split)
- **Acceptance (one-line):** `data.split(":", 2)` + `_LEGACY_ALIAS` map; all new prefixes (§7.1) work; all legacy prefixes still work as aliases (Condition C2).
- **Detail:**
  - Add `_LEGACY_ALIAS` dict at module level per §7.2.
  - Refactor dispatcher to split on `":"` with maxsplit=2, then dispatch via `(domain, action)` tuple. Legacy single-segment prefixes route through the alias map.
  - For `route:snooze:{days}:{route_id}` (4 segments), use a sub-split inside the `route:` branch: `payload.split(":", 1)` → `(days, route_id)`.
  - Confirm callback flows for: `confirm_route`, `confirm_modify`, `confirm_remove`, `edit_route`, `cancel_*`, `approve_user`, `reject_user`, `confirm_airports`, `change_airports` — all of these are non-deal/non-route prefixes and pass through unchanged.

### Tester tasks

#### `[ ]` T14 — tests_telegram (4 message types)
- **Owner:** tester
- **Depends on (Builder):** T7
- **Blocks:** —
- **Files:** `tests/test_telegram.py`
- **Acceptance (one-line):** For each of `send_deal_alert`, `send_error_fare_alert`, `send_follow_up`, `send_daily_digest`: cost breakdown present, baggage line correctly suppressed-when-zero, transparency footer in all 3 cases, 3-button row + Details button on deal+digest, reasoning rendered as 3 bullets.
- **Detail:**
  - Build a parametrized fixture matrix: (msg_type) × (baggage zero / nonzero / unknown) × (competitive empty / non-empty / mixed).
  - Use `respx` or stdlib `unittest.mock.AsyncMock` to capture the JSON payload sent to `_send_message`.
  - Assert the Markdown body contains the expected substrings; assert keyboard structure is exactly the documented row layout.
  - For follow-up: cost breakdown is the new addition — verify it's there.

#### `[ ]` T15 — tests_serpapi_baggage *(highest-risk test — Finding #1)*
- **Owner:** tester
- **Depends on (Builder):** T3
- **Blocks:** —
- **Files:** `tests/test_serpapi_baggage.py` (NEW)
- **Acceptance (one-line):** Parser extracts baggage from synthetic fixtures (booking_options + flight-leg extensions), falls back to airline table when no data, applies user preference correctly, and tolerates the 25 cached responses without raising.
- **Detail:**
  - **Synthetic fixtures** (Tester writes inline as Python dicts; do NOT add to `data/serpapi_cache/` — that directory is for live captures):
    - (a) `booking_options[].together.extensions` containing strings like `"Carry-on bag €25"`, `"Checked bag €40"`.
    - (b) `best_flights[].flights[].extensions` containing same baggage strings.
    - (c) Empty / no-baggage extensions → forces fallback path.
  - Assert parser returns `source="serpapi"` for (a) and (b), `source="fallback_table"` for (c) with known airline, `source="unknown"` for (c) with airline not in table AND no SerpAPI data.
  - Fallback table: KL × `one_checked` × long-haul = €0; FR × `one_checked` × short-haul = €(carry_on + checked_short_haul) per direction; HV × `carry_on_only` = €(carry_on) per direction; unknown airline → `_DEFAULT`.
  - Smoke: load all 25 cached responses from `data/serpapi_cache/` and assert parser doesn't raise on any of them; document that all 25 produce `source="unknown"` per Finding #1.

#### `[x]` T16 — tests_db_migrations
- **Owner:** tester
- **Depends on (Builder):** T1
- **Blocks:** —
- **Files:** `tests/test_db.py`
- **Acceptance (one-line):** All 5 new columns present after `init_schema`; second call is a no-op; round-trip helpers (snooze, fingerprint, baggage_estimate, reasoning_json, baggage_needs) work.
- **Detail:**
  - Use a tmp-path SQLite DB.
  - `init_schema()` × 2 — second call must not raise.
  - For each new column: insert / read / update / read pattern.
  - Helper-based snooze round-trip deferred to T17 (`get_active_routes` filtering); column-level round-trip via raw SQL is sufficient here.

#### `[x]` T17 — tests_orchestrator (digest skip + snooze + auto-snooze)
- **Owner:** tester
- **Depends on (Builder):** T9, T10
- **Blocks:** T19
- **Files:** `tests/test_orchestrator.py`
- **Acceptance (one-line):** Digest skip predicate fires when fingerprint unchanged + <3d; snooze respected in `poll_routes` AND `send_daily_digest`; auto-snooze 30d fires on `feedback='booked'` from new AND legacy callback paths.
- **Detail:**
  - Skip predicate matrix: (fingerprint same/different) × (days_since_last <3 / >=3) × (new_deals 0 / >0). Verify all 8 cells produce documented behaviour.
  - Snooze in `poll_routes`: pre-populate a snoozed route, call `poll_routes`, assert that route was NOT in `search_requests`.
  - Snooze in `send_daily_digest`: pre-populate a snoozed route with pending deals, call `send_daily_digest`, assert no message sent for that route.
  - Auto-snooze: invoke each callback path (`deal:book`, `book:`, `booked:`, `digest_booked:`) and assert `routes.snoozed_until` ≈ now+30d.

#### `[x]` T18 — tests_scorer (structured reasoning)
- **Owner:** tester
- **Depends on (Builder):** T12
- **Blocks:** T19
- **Files:** `tests/test_scorer.py`
- **Acceptance (one-line):** Valid 3-field response parsed correctly; malformed JSON → fallback synthetic 3-field; missing field → fallback fires; bullet-string flattening produces 3 lines.
- **Detail:**
  - Mock `_client.messages.create` to return a fixed text body.
  - Cases: (a) valid full 3-field — assert `DealScore.reasoning` is dict with 3 keys; (b) malformed JSON — assert synthetic fallback object; (c) missing `vs_nearby` — assert synthetic fallback fires; (d) `urgency` outside enum — assert defaults safely.
  - Helper test: given a 3-field `reasoning` dict, the flatten-to-bullet logic produces exactly 3 lines starting with `✓ `.

#### `[ ]` T19 — integration_test_end_to_end *(NON-NEGOTIABLE — /release Phase 4 step 6b)*
- **Owner:** tester
- **Depends on (Builder):** T7, T8, T17, T18
- **Blocks:** —
- **Files:** `tests/test_v07_release.py` (NEW; follows v0.7/v0.8 fixture style)
- **Acceptance (one-line):** End-to-end deal-alert path with mocked HTTP only — alert renders all R7 surfaces, `deal:book` callback marks booked + auto-snoozes, next digest skips that user.
- **Detail:**
  - Single user, 1 route AMS→TYO, primary AMS + secondaries EIN/BRU configured in `airport_transport`.
  - Mock HTTP at `httpx.AsyncClient` boundary: SerpAPI returns synthetic response with baggage in extensions, secondary searches return higher prices (so EIN/BRU don't beat €75 threshold — exercises "we checked" footer).
  - Mock `anthropic.AsyncAnthropic` to return structured 3-field reasoning JSON.
  - Run `await orchestrator.poll_routes()`.
  - Assert Telegram payload contains: cost breakdown line including `+ €N bags`, 3 reasoning bullets (`✓ ...`), `✓ Checked 2 airports — your airport is best by €...` footer, 3-button row (Book / Watching / Skip route), `📊 Details` button, `deal:watch:` and `route:snooze:7:` callback data.
  - Simulate user clicking `deal:book:{deal_id}` → assert `deal.feedback='booked'`, `deal.booked=1`, `route.snoozed_until` ≈ now+30d.
  - Run `await orchestrator.send_daily_digest()` immediately after → assert NO Telegram message sent (route snoozed).

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
