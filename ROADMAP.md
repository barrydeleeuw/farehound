# FareHound Roadmap

> Last updated: 2026-05-10 (v0.10.0)

## Mission

**Making travel accessible for everyone by finding the lowest real cost to fly.**

Most flight search tools show you the ticket price. FareHound shows you the true cost — including how you get to the airport, what parking costs, and whether a "cheaper" flight from a farther airport actually saves you money. Everyone deserves to know when a genuinely great deal exists, not just people who spend hours checking multiple airports and deal sites.

No more subscribing to airline newsletters full of irrelevant promotions — FareHound monitors the routes you actually care about and alerts you only when the price is genuinely good. The promotional prices airlines email about already show up on Google Flights; the difference is FareHound filters the noise for you.

Every feature we build serves this mission: reduce the gap between what people pay and what they could pay, with zero effort on their part.

## In Progress

<!-- No items currently in progress -->

## Ready

### [ITEM-011] Weekend/short trip date windowing
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** S
- **Dependencies:** None
- **Summary:** "Long weekend in May" should generate Thu/Fri→Sun/Mon windows, not May 1-31. Trip duration model exists but needs proper orchestrator integration.

### [ITEM-053] Auto-discover & enrich nearby airports (Google Maps + multi-mode + cheapest-mode selection)
- **Status:** Ready — **next-up.** Subsumes [ITEM-045] (manual onboarding) and [ITEM-004] (Google Maps lookup); both re-parked as superseded.
- **Priority:** P1 (High)
- **Effort:** M-L
- **Dependencies:** [ITEM-043] (Done). Requires Google Maps Platform API key (free tier covers expected usage; new add-on config option).
- **Why now:** During v0.10.0–v0.10.16 dogfooding the deal page consistently rendered transport at €0 and the alternatives table empty because `airport_transport` had no rows for the user's home airport or nearby candidates. The current schema also forces a single mode per airport, so the user can't see "drive vs train vs taxi — which is cheapest for this trip." This item closes both gaps in one coherent release.

#### Three problems solved together
1. **No data:** `airport_transport` is empty until the user manually fills it via conversation. Users churn before getting through onboarding because transport costs are tedious to research.
2. **One mode per airport:** schema only stores the user's chosen mode; can't compare drive vs train vs taxi at deal-render time.
3. **No "cheapest" recommendation:** even if multiple modes were stored, there's no logic to pick the cheapest for the current party size + trip duration.

#### Approach

**A. Auto-discover nearby airports.** When a user sets a home airport, propose the 3 closest viable airports within ~200 km radius (excluding tiny regional/seasonal-only ones). Reuse geographic filtering from `_resolve_nearby_airports` where applicable; viable-airport curation may need a small `data/viable_airports_eu.json` allow-list to filter out airports with no long-haul service.

**B. Auto-fill all viable modes per airport** (instead of one):
- **Drive:** Google Maps Distance Matrix API (`mode=driving`) → distance + duration. Cost = `distance_km × €0.25/km` (heuristic; user can adjust per-km rate during onboarding).
- **Train:** Google Maps Distance Matrix API (`mode=transit`) → duration. Cost via NS API for NL routes (free, scoped to Netherlands); for international routes (BRU/DUS/CRL) use a curated estimate-by-route table seeded for the EU airports we expect, with an "estimate — confirm or override" prompt to the user.
- **Taxi:** heuristic only — `distance_km × €2.50/km` (rough EU taxi rate). Duration ≈ drive duration. No public API.
- **Parking:** seeded from a curated `data/airport_parking.json` covering ~20 EU airports (AMS, EIN, RTM, BRU, DUS, CRL, FRA, LHR, etc.) with daily P+R / economy-lot rates. User can override per-airport.

**C. Schema migration: multiple modes per airport.**
```sql
CREATE TABLE airport_transport_option (
    user_id           TEXT NOT NULL,
    airport_code      TEXT NOT NULL,
    mode              TEXT NOT NULL,    -- 'drive' | 'train' | 'taxi' | 'uber' | 'bus'
    cost_eur          REAL,             -- one-way; doubled at render time for round-trip
    cost_scales_with_pax  INTEGER,      -- 1 for train/taxi/uber/bus, 0 for drive
    time_min          INTEGER,
    parking_cost_per_day_eur REAL,      -- only for drive; null otherwise
    source            TEXT,             -- 'google_maps' | 'ns_api' | 'curated' | 'user_override'
    confidence        TEXT,             -- 'high' | 'medium' | 'low' (for "estimate — confirm" UI)
    PRIMARY KEY (user_id, airport_code, mode)
);
```
Old `airport_transport` table preserved short-term as a compatibility shim (read at boot, migrated forward into options), then dropped after one release of clean operation.

**D. Render-time cheapest-mode selection.** In `src/web/data.py` and `src/alerts/telegram.py`, replace the single `db.get_airport_transport(origin)` call with:
```python
options = db.get_transport_options(origin, user_id)
trip_days = (route.return_date - route.depart_date).days
totals = [_compute_mode_total(opt, passengers, trip_days) for opt in options]
chosen = min(totals, key=lambda t: t.cost)
```
Breakdown row becomes: `transport (train, cheapest)  €15/pp` with the chosen mode visible. If the user has an explicit per-airport override flag set ("always use [mode] regardless of cost"), respect that instead.

**E. Settings page made editable.** Currently read-only. Each airport row expands to show all stored modes with inline edit (cost, time, parking, confidence flag). Per-airport "always use [mode]" boolean override.

#### Acceptance criteria
- [ ] New user with a home airport gets 3 nearby airports auto-proposed and confirmed inline (one bot message, not N).
- [ ] Each confirmed airport has ≥2 modes auto-populated (drive + train minimum where transit exists; taxi as fallback).
- [ ] `data/airport_parking.json` ships with ~20 EU airports curated.
- [ ] NS API integration for NL train fares; international routes show "estimate — confirm" with a one-tap "looks right" / "let me adjust" inline button.
- [ ] Deal page picks the cheapest mode per route at render time, accounting for party size + trip duration.
- [ ] Breakdown row shows the chosen mode label ("via train") so the user has transparency.
- [ ] Settings page shows all modes per airport, editable inline; per-airport "always use X" override stored and respected.
- [ ] Google Maps API key is a new add-on config option (`google_maps_api_key`), and the auto-fill flow is skipped gracefully (with "tap to add manually" fallback) if the key is unset.
- [ ] Migration runs cleanly: existing single-mode rows in `airport_transport` are read forward into `airport_transport_option` at first boot, no data loss.
- [ ] Old conversational-onboarding flow ([ITEM-045]) survives as fallback when auto-fill confidence is low or for airports outside the curated set.
- [ ] Tests: `tests/test_transport_options.py` for cheapest-mode selection (1 pax / 4 pax / various trip durations / parking-included).

#### Cost / rate-limit notes
- **Google Maps Distance Matrix:** $5/1000 elements after free tier. Per onboarding: 4 airports × 2 modes (drive + transit) = 8 elements ≈ $0.04 per user. Free tier covers ~25k elements/mo at zero cost — comfortable.
- **NS API:** free, no key needed for fare lookups. Rate-limit lenient.
- **Cache:** all auto-fill results are stored in `airport_transport_option` with `source` + `confidence` so we never re-call Google Maps for the same airport pair.

### [ITEM-054] Cheapest-TOTAL fare selection across airlines (not just cheapest FARE)
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** M
- **Dependencies:** [ITEM-053] shipped through v0.11.8 (airline-code capture from `other_flights` fallback) — Done.
- **Why this matters:** FareHound's mission is "lowest REAL cost door-to-door." Today the orchestrator picks the cheapest **fare** out of SerpAPI's results, then adds baggage based on that airline's policy. If Transavia offers €129 (no bag included → +€30 = €159 total) and KLM offers €145 (bag included → €145 total), we currently surface Transavia at €159 — when KLM is actually €14 cheaper door-to-door. The "real cost" promise breaks down across airline classes.
- **Surfaced in v0.11.x dogfooding:** Barry's AMS→MEX deal showed €30/checked baggage for an Air France long-haul (where the bag is included in the fare) because the airline code was being lost; v0.11.8 fixed the airline-capture half. The remaining half — picking the cheapest TOTAL not the cheapest FARE — is this item.
- **Approach:**
  - Walk through `best_flights + other_flights` from each SerpAPI response.
  - For each flight option, compute `total = price + baggage_party_total` where `baggage_party_total = parse_baggage_extensions(opt.extensions) or fallback_table(opt.airline, leg_distance, baggage_needs) × passengers × 2 directions`.
  - Pick the option with the lowest total — `min(options, key=lambda o: total(o))`.
  - Store the chosen option as `snapshot.best_flight` along with its `airline_code`, `airline_name`, `price`, and `baggage_estimate`.
  - Deal page shows the chosen airline prominently + an expandable "see other fare options" list ranked by total cost so the user can see the trade-off.
- **Acceptance criteria:**
  - [ ] For each snapshot, all flights in `best_flights + other_flights` are evaluated for total cost (not just the cheapest fare).
  - [ ] `snapshot.best_flight` reflects the lowest-TOTAL option, including its airline code.
  - [ ] Deal page hero shows airline name (e.g. "AMS → NRT · KLM").
  - [ ] Deal page has a "fare options" disclosure that lists each priced option with `airline · €fare · €total` so the user can see the trade-off.
  - [ ] Synthetic SerpAPI fixture test: 2 options — Transavia €129 (no bag) + KLM €145 (long-haul bag included) — KLM picked when `baggage_needs='one_checked'`; Transavia picked when `baggage_needs='carry_on_only'`.
  - [ ] Existing scoring still works with the chosen option (scorer prompts unchanged).
  - [ ] Backwards-compat: snapshots stored pre-ITEM-054 keep rendering correctly (their `best_flight` already has whatever was picked at poll time).
- **Out of scope:**
  - Booking-class differentiation within a single airline (KLM Light vs Standard vs Flex). SerpAPI doesn't reliably surface fare-class metadata; would need a separate effort.
  - Code-share resolution (operating carrier vs marketing carrier). Same data-availability constraint.
- **Surfaces affected:**
  - Orchestrator: `_compute_baggage_for_result` becomes `_pick_cheapest_total_option` returning `(flight, baggage)` together.
  - Telegram alerts: `airline` field on deal_info uses the chosen option's airline (already the case post-v0.11.8 once the flow is fixed).
  - Deal page: hero shows airline, breakdown row label can include airline ("flights — KLM"), new "Fare options" section.

## Proposed

### [ITEM-050] Full custom web dashboard
- **Status:** Proposed
- **Priority:** P3 (Low)
- **Effort:** XL
- **Dependencies:** [ARCHITECTURE.md](ARCHITECTURE.md) Phase A (cloud migration to Railway+Postgres), validated multi-user demand, [ITEM-049] deployed
- **Summary:** A proper standalone web app for account management, billing, route management, history, analytics. Telegram becomes notification-only. Revisits the decision in [ARCHITECTURE.md §4](ARCHITECTURE.md) ("Why not a web app") once there's a paying user base.
- **Why this is on the roadmap (not parked):** Kept visible as the long-term answer for a real product. [ITEM-049] (Telegram Mini Web App) covers ~90% of the rich-detail use cases at <10% the effort, so this stays Proposed/P3 until a paying user base validates the investment.
- **Review notes:** Premature today. Revisit once: (1) ARCHITECTURE.md Phase A is complete (Railway + Postgres), (2) [ITEM-049] is shipped and we know what users actually use it for, (3) there's evidence of demand for features that don't fit in a Mini Web App (e.g. multi-user team accounts, complex analytics, B2B integrations).

### [ITEM-052] R7 cleanup batch
- **Status:** Proposed
- **Priority:** P3 (Low)
- **Effort:** S
- **Dependencies:** [ITEM-051] shipped (v0.9.0)
- **Summary:** Bundle of 6 P3 cleanup items surfaced during R7 build + post-build audit + code review. None are bugs; all are stylistic / debt management items worth doing in one batch ~30 days after R7 settles.
- **Items:**
  - **FU-1:** Reset `digest_skip_count_7d` rolling-window (currently monotonic).
  - **FU-2:** ~~Bound `/snooze` days argument upper limit~~ (already shipped in R7 code-review fix CR-2).
  - **FU-3:** Reconcile T15 25-cached-response smoke loop (cached fixtures in `data/serpapi_cache/` contain zero baggage data, so the test was substituted with synthetic fixtures — clean up the substitution).
  - **FU-4:** Drop legacy `deals.reasoning` column after 60 days of structured `reasoning_json` operation.
  - **FU-5:** Delete the legacy callback-alias if-ladder in `src/bot/commands.py` after the 2026-06-08 alias deadline.
  - **FU-6:** Strip task-reference comments (`T7 §X`, `A1`–`A5`, `R7`, `Condition C9`, `T12`) from `src/bot/commands.py`, `src/storage/db.py`, `src/alerts/telegram.py`, `src/orchestrator.py`. CLAUDE.md style violation, low impact.
- **Acceptance Criteria:**
  - [ ] FU-1 implemented or explicitly de-prioritized with reasoning
  - [ ] FU-3, FU-4, FU-5 each addressed or formally re-deferred with date
  - [ ] FU-6: zero `T<N>`, `§<N>`, `Condition C<N>` references remain in source files

### [ITEM-038] Discovery: broad destination scanning from home airports
- **Status:** Proposed
- **Priority:** P1 (High)
- **Effort:** L
- **Dependencies:** [ITEM-037]
- **Summary:** Replace paid deal subscriptions (Jack's Flight Club, Secret Flying premium) with automated discovery. Use SerpAPI's Google Travel Explore API to scan cheap flights from the user's home airports and nearby airports (e.g. AMS, EIN, BRU, DUS, CRL), detect anomalous price drops, and alert users about deals on routes they haven't explicitly configured — including destinations they hadn't considered.
- **Why this matters:** JFC and Secret Flying charge monthly fees to do essentially the same thing — monitor fares broadly and flag cheap ones. FareHound can automate this with the same Google Flights data, personalized to the user's home airports, and with total cost awareness (see ITEM-037).
- **Design considerations:**
  - **Multi-airport scanning:** Scan from all user home airports + configured nearby airports. One Explore API call per origin covers all destinations — very efficient.
    - Example: AMS, EIN, BRU, DUS, CRL = 5 origins
    - Frequency: 2x/week per origin = ~40 calls/month (~4% of 1,000 call budget)
    - Weekly is sufficient — error fares typically survive several hours to a day
  - **Anomaly detection logic:**
    - Maintain a rolling price baseline per origin→destination pair from explore history
    - Flag destinations where price drops >40-50% below baseline
    - On anomaly: trigger a targeted route search (regular SerpAPI flights call) to confirm price and get flight details + baggage info (see ITEM-037)
    - Different alert priority/tone: "Discovery deal: AMS → Tokyo €310 (normally ~€650) — possible error fare, book fast!"
  - **Total cost enrichment:** Discovery alerts should include estimated total cost (ticket + baggage) via ITEM-037, so users see the real price, not just the headline fare.
  - **Explore API specifics:**
    - Endpoint: `engine=google_travel_explore`, `departure_id=AMS`
    - Returns: destination, price, dates, duration, airline per destination
    - One call covers all destinations from an origin — very efficient for broad scanning
  - **Budget estimation (current plan: 1,000 calls/month):**
    - Explore scans: 5 airports × 2x/week = ~40 calls/month
    - Confirmation checks on anomalies: ~5-10/month (rare events)
    - Total overhead: ~50 calls/month (~5% of budget)
  - **Replaces:** Paid JFC/Secret Flying subscriptions for deal discovery. Community RSS feeds (Layer 2) remain active as a complementary free source.
- **Acceptance Criteria:**
  - [ ] Explore scans run from all user home + nearby airports (deduplicated across users)
  - [ ] Price baselines tracked per origin→destination from explore history
  - [ ] Anomalous price drops (>40-50% below baseline) flagged as discovery deals
  - [ ] Urgent Telegram alert with destination, price, typical price, airline, dates
  - [ ] Total cost estimate included in alert (ticket + baggage via ITEM-037)
  - [ ] API call budget stays within SerpAPI plan limits — monitoring and safeguards
  - [ ] Explore results cached and shared across users with the same/nearby home airport

### [ITEM-005] Preferred airline comparison
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** M
- **Dependencies:** None
- **Summary:** Show both cheapest and preferred airline: "Cheapest: €240 (Transavia) | KLM: €289 (+€49)". Helps users make informed choices.

### [ITEM-006] Deal email pipeline (Jack's Flight Club + Secret Flying)
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** L
- **Dependencies:** None
- **Summary:** Ingest deal emails from JFC and Secret Flying via Gmail API polling. Parse with Claude (Haiku), match against user routes (including nearby airports), notify via Telegram. These are the highest-value deals — error fares and flash sales that periodic SerpAPI polling can never catch.
- **Detail:** [roadmap/deal-email-pipeline.md](roadmap/deal-email-pipeline.md)
- **Source value tracking:**
  - Apply the same evaluation model to every deal source (JFC, Secret Flying, and any future source) to measure whether each adds value beyond what FareHound catches via SerpAPI polling (ITEM-021) and other sources.
  - Track per deal email (regardless of source): source name, route, price, booking platform recommended, whether FareHound already had this deal from another source, and whether FareHound's own price for the same route was comparable.
  - Key questions to answer per source after 3-6 months of data:
    1. How many deals match our configured routes? (relevance rate)
    2. Of those, how many did FareHound already flag independently? (overlap rate)
    3. Does the source recommend booking on platforms other than Google Flights — and are those prices actually cheaper? (unique booking value)
    4. How many source-exclusive deals did the user act on? (incremental value)
  - **JFC (€40/year):** If overlap with FareHound's own discovery is high, cancel and save the fee. If JFC consistently surfaces unique deals (e.g. via airline-direct booking platforms), keep it.
  - **Secret Flying (free RSS + email):** RSS is already ingested in Layer 2. Email may catch deals faster than RSS. Track whether email-only deals exist and whether they arrive before the RSS feed. If email adds no speed or coverage advantage over RSS, skip the email integration and save the complexity.
- **Acceptance Criteria:**
  - [ ] Gmail API client polls for new emails from JFC/SF senders every 5 minutes
  - [ ] JFC parser handles single-destination and multi-destination formats (with IATA codes, price ranges, travel months, cabin class, baggage tags)
  - [ ] SF parser extracts origin/destination cities (resolve to IATA), price, example dates, stops, airline
  - [ ] Deals matched against all users' routes (destination + origin/nearby + date overlap)
  - [ ] Telegram notification on match with deal details and booking link
  - [ ] Dedup by email_id and route+price+date range (24h window)
  - [ ] Deals stored in `email_deals` table for history
  - [ ] Parsing errors logged, never crash the pipeline

### [ITEM-007] Voluntary contribution model ("Pay what it saved you")
- **Status:** Proposed
- **Priority:** P3 (Low)
- **Effort:** L
- **Dependencies:** [ITEM-002], validated user base
- **Summary:** After booking with savings, suggest a voluntary contribution. Aligns with accessibility mission: those who save more contribute more.
- **Review notes:** Low conversion (~2-3%). Needs proven savings data first.

### [ITEM-008] Subscription + pay-per-trip tiers
- **Status:** Proposed
- **Priority:** P3 (Low)
- **Effort:** XL
- **Dependencies:** Validated demand
- **Summary:** Free tier (1 route) vs subscription for continuous monitoring. Free tier keeps travel accessible; subscription funds infrastructure.
- **Review notes:** Premature until Phase A validates demand.

### [ITEM-009] Deploy to Railway (cloud)
- **Status:** Proposed
- **Priority:** P3 (Low)
- **Effort:** M
- **Dependencies:** Phase A validation
- **Summary:** Move from HA to cloud. Only if Phase A proves demand.

### [ITEM-012] Multi-destination per trip
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** M
- **Dependencies:** None
- **Summary:** "Japan" monitors NRT + KIX + NGO as separate routes under one trip. User says "all of them" → creates routes for each.

### [ITEM-015] Adaptive polling frequency
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** M
- **Dependencies:** None
- **Summary:** Poll more when departure < 6 weeks (4h), less when > 4 months (48h). Currently fixed at 24h.

### [ITEM-018] E2E Telegram bot test harness
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** M
- **Dependencies:** None
- **Summary:** Telethon-based integration tests that connect as a test user and walk through bot flows (onboarding, trip creation, deal alerts, booking follow-up). Verifies message content and conversation state against expectations. Requires a test Telegram account and running bot instance.

## Done

### [ITEM-D16] Mini Web App + thin Telegram (v0.10.0 → v0.10.16)
- **Status:** Done — [ITEM-049] shipped 2026-05-10 as v0.10.0 and hardened across 16 patch releases through v0.10.16 over ~24h of dogfooding. Frontend (`e4dc24a`): 3 Jinja-portable HTML pages with editorial-ledger aesthetic — Fraunces/Hanken Grotesk/JetBrains Mono, no rounded corners. Backend (`d2da895`): FastAPI under `src/web/` boots alongside bot via `asyncio.gather` from Orchestrator.start; 12 endpoints (4 HTML, 8 JSON); `initData` HMAC validation rejects forged/expired/tampered/future requests. Telegram flipped to **Option B**: alerts thin to 2-line pings with single `📊 Open in FareHound` button; daily digest collapses to one summary message; v0.9.0 rich format remains as fallback when `MINIAPP_URL` is unset. HTTPS via existing Pi Cloudflare Tunnel (added `farehound.bdl-ha.net` ingress to `/share/cloudflared/config.yml`).
- **Patch trajectory** (every fix backed by a regression test or deterministic check):
  - `v0.10.1` — synced `farehound/pyproject.toml` so HA Supervisor build picks up FastAPI/uvicorn/jinja deps; updated `CLAUDE.md` deploy recipe.
  - `v0.10.2–4` — Supervisor rebuild plumbing (version bumps to force rebuild, then real fix).
  - `v0.10.5` — bootstrap pattern: Telegram passes `initData` as URL hash fragment which servers can't read; added JS shim that reads `Telegram.WebApp.initData` and reloads with `?tg=` query param so the server can validate.
  - `v0.10.6–8` — replaced web-form add-trip with "Back to chat" CTA after Barry hit parsing dead-ends; trip creation stays in Telegram bot conversation only. "Route added" success message now embeds Mini Web App button.
  - `v0.10.9–10` — added Remove (soft-delete via `db.deactivate_route`) endpoint + button per route; settings page now surfaces "message me to add airports" pointer (no inline form).
  - `v0.10.11–12` — diagnosed Telegram WebView caching old `app.js` via on-screen toasts; fixed with `?v={cache_buster}` query param injected by `_TEMPLATES.env.globals["cache_buster"]`.
  - `v0.10.13` — fixed latent v1.0.0 crash: `transport_time_min` could be NULL, divisions blew up scorer + nearby alts. Three call sites guarded with `or 0` + regression test.
  - `v0.10.14` — deal page unit consistency: per-person primary everywhere, party total relegated to annotation. Reasoning rendered as bulleted list (was inline sentence with mid-text checkmarks).
  - `v0.10.15` — **architectural shift:** removed LLM-generated numbers from displayed reasoning. New `_build_deterministic_reasoning(snapshot, last_alerted, passengers, price_history_dict, nearby_count)` returns up to 4 deterministic bullets (Google range position, 90-day history position, delta since alert, nearby footprint). Hero now shows total-cost /pp instead of fare-only /pp. Resolves Barry's "where does €234 come from" mystery.
  - `v0.10.16` — labelled fare-only bullets explicitly ("Flight fare €X/pp") with disclaimer "(ticket only — bags & transport not included)" since SerpAPI's `typical_price_range` excludes bags/transport.
- **Code-review fixes** (`a91edb8`): critical cross-user feedback bug fixed (ownership check on `/api/deals/:id/feedback`), `/api/routes/parse` capped at 500 chars to prevent Anthropic-budget abuse, over-defensive `_update_user_safe` wrapper removed.
- **Detail:** [docs/releases/R8/release_plan.md](docs/releases/R8/release_plan.md), [docs/deployment/cloudflare-tunnel.md](docs/deployment/cloudflare-tunnel.md)
- **Built solo** (not a team) — single new module + 4 additive template thin-outs didn't justify 3-agent coordination overhead. `/code-review` ran at the end as the independent perspective. Decision documented in release plan.
- **Deferred follow-ups** for next release: (1) immediate poll on `POST /api/routes` (currently waits for next cron tick — surfaces as empty state for newly added routes), (2) persist nearby-airport "evaluated" list to DB so `/deal/{id}` alternatives table renders for routes that haven't yet hit a savings threshold, (3) update R7 verification report MV-5/6/7/8 to "superseded by web app", (4) **promoted: [ITEM-053] (auto-discover & enrich nearby airports)** — Barry hit the "no airports configured" gap on the deal alternatives section AND the "transport at €0 in the breakdown" gap. The underlying issue is that `airport_transport` is empty AND single-mode-per-airport. ITEM-053 fixes both by auto-filling multiple modes per airport via Google Maps + NS API + curated parking dataset, then picking the cheapest mode per route at render time. Subsumes the previous [ITEM-045] (manual conversational onboarding).

### [ITEM-D15] Real Cost Restoration (v0.9.0)
- **Status:** Done — ITEM-051 shipped 2026-05-09 as one coherent release covering all 4 Telegram message types. Includes: unified `_format_cost_breakdown` helper across deal alert / error fare / follow-up / daily digest, SerpAPI baggage parsing + display via new `src/utils/baggage.py` module (subsumes [ITEM-037]), "we checked X airports/dates" transparency footer (kills silent omission below €75 nearby savings threshold), `Watching 👀` button on alerts and digest, per-route snooze (`routes.snoozed_until`) with auto-snooze on `booked` feedback + `/snooze` `/unsnooze` commands, structured 3-bullet scorer reasoning JSON contract with backward compat, fingerprint-gated daily digest skip with concrete "what moved" header, callback prefix consolidation (`deal:*` / `route:*`) with legacy aliases, `/status` command, and `📊 Details` button placeholder for [ITEM-049] Mini Web App. Suite: 311 → 420 (+109 R7 tests). T19 integration test caught a real ship-blocker (`deal_info["route_id"]` missing) that 416 unit tests had passed. Code-review fixes: clamped `route:snooze` callback `days` to `[1, 365]` (security-relevant; negative values would un-snooze), removed unnecessary defensive shims (`getattr`/`hasattr` for methods defined in same release).
- **Detail:** [docs/releases/R7/](docs/releases/R7/) — release_plan.md, build_log.md, verification_report.md
- **5 P3 follow-ups** captured as [ITEM-052].

### [ITEM-037] Luggage-aware total cost calculation
- **Status:** Done — Subsumed by [ITEM-051] / shipped in v0.9.0. Baggage parsing implemented in `src/utils/baggage.py` with airline fallback table; `baggage_estimate` field on `PriceSnapshot`; `baggage_needs` user preference (`carry_on_only` / `one_checked` / `two_checked`); cost breakdown now shows `€X flights + €Y bags + €Z transport + €W parking = €N total` across all 4 message types. Total cost (incl. bags) used by scorer.

### [ITEM-D01] Core monitoring loop (v0.1-v0.3)
- **Status:** Done — SerpAPI polling, SQLite, Claude scoring, smart alerting, RSS feeds.

### [ITEM-D02] Nearby airport engine (v1.0)
- **Status:** Done — Multi-airport polling with door-to-door cost comparison. The core differentiator.

### [ITEM-D03] Conversational Telegram bot (v1.1)
- **Status:** Done — Claude-powered natural language trip management.

### [ITEM-D04] Scoring honesty + UX polish (v1.2-v1.3)
- **Status:** Done — Fact-based scoring, Book Now/Wait, transparent cost breakdown.

### [ITEM-D05] Multi-user support (v2.0)
- **Status:** Done — Users table, shared polling, Telegram onboarding, SerpAPI cache.

### [ITEM-D06] Alert quality & cleanup (v2.1)
- **Status:** Done — Smart daily digest (only undecided trips), transparent cost breakdown in alerts, booking follow-up verified, Reddit RSS 403s fixed (JSON API), HA dead code removed (homeassistant.py, Lovelace card).

### [ITEM-D07] Fix the alert pipeline (v2.2)
- **Status:** Done — Price-drop alerting replaces score-gated alerts (ITEM-024), nearby airports always included in deal alerts via on-demand polling (ITEM-021), scorer reasoning uses per-person pricing (ITEM-020).

### [ITEM-D08] Bot UX overhaul (v2.3)
- **Status:** Done — Inline buttons replace /yes /no (ITEM-026), immediate price check after adding trip with typing indicator (ITEM-025), natural language during pending proposals (ITEM-022), enriched price query with cost breakdown and nearby alternatives (ITEM-023).

### [ITEM-017] SerpAPI response cache for local testing
- **Status:** Done — SERPAPI_CACHE_DIR env var enables cached responses locally. 17 responses recorded. Zero API calls during dev.

### [ITEM-D14] Take Action (v0.8.0)
- **Status:** Done — ITEM-048 (daily digest actionable buttons: "Booked ✅" and "Not interested" callbacks, bulk route dismiss), ITEM-047 (cheapest departure date hint from existing price_history in deal alerts, digest, and price checks), ITEM-002 (savings tracker: savings_log table, automatic logging at nearby airport comparison, /savings command). 311 tests pass (38 new).

### [ITEM-046] Date windows respect user's departure date
- **Status:** Done — Parse prompt generates tight ±2 day window for specific departure dates. Confirmation message shows actual search window.

### [ITEM-D13] First Impressions (v0.7.0)
- **Status:** Done — ITEM-035 (immediate fare feedback: "Checking prices now..." message, specific error messages on failure), ITEM-034 (region/archipelago disambiguation in parse prompt), ITEM-036 (message layout redesign: flight info line with airline/stops/duration across all message types, price context with level and range). 271 tests pass.

### [ITEM-D12] Know Your Airports + Approval Gate (v0.6.0)
- **Status:** Done — ITEM-043 (onboarding resolves airports via Claude, confirmation with inline buttons, fallback to manual IATA), ITEM-044 (user approval gate: approved column, first user auto-approved as admin, waitlist message, admin Telegram notification with Approve/Reject buttons, orchestrator skips unapproved users). 271 tests pass (9 new).

### [ITEM-043] Know Your Airports (v0.6.0)
- **Status:** Done — Onboarding resolves city → airports via Claude. Confirmation with inline buttons. Fallback to manual IATA entry. Deleted unused SerpAPI-based `_resolve_airports()`. 267 tests pass (5 new).

### [ITEM-D11] Clean Signals (v0.5.0)
- **Status:** Done — ITEM-042 (daily digest crash: table-qualified user_id in JOIN), ITEM-041 (removed RSS/Telethon community listeners, community.py, config). Version reset to SemVer 0.x (pre-release). 264 tests pass (1 new regression test).

### [ITEM-001] Multi-user stabilization
- **Status:** Done — Onboarding flow, SerpAPI cache (25 responses), 24h poll interval with hard cap at 950, DB migration with user scoping, multi-user isolation tested. Shipped across v2.0–v2.4.

### [ITEM-D10] Trust the Numbers (v2.4)
- **Status:** Done — ITEM-028 (actual flight prices), ITEM-030 (max_stops enforced), ITEM-040 (API budget: windows 4→2, secondary every 3rd cycle, hard cap at 950), ITEM-031 (flight duration in comparisons), ITEM-029 (debug logging), ITEM-032 (follow-up spam fix), ITEM-033 (NL silent failures fix), ITEM-027 (non-blocking bot). 297 tests pass (19 new).

### [ITEM-D09] NL add-trip prompt fix (v2.3.x)
- **Status:** Done
- **Summary:** Fixed NL (natural language) trip creation via Telegram. The `_INTERPRET_SYSTEM` prompt was missing IATA airport code requirements (destinations returned as city names like "Seoul" instead of "ICN", breaking flight searches), trip duration fields (`trip_duration_type`, `trip_duration_days`, `preferred_departure_days`, `preferred_return_days`), and instructions for cross-trip references ("same dates as Japan"). Added duration rules matching the `/trip` flow and explicit IATA code instructions.

## Parked

### [ITEM-P01] Creative routing (virtual interlining)
- **Parked:** No affordable API. Revisit if budget increases.

### [ITEM-P02] HA Lovelace dashboard
- **Parked:** Superseded by [ITEM-049] (Telegram Mini Web App). Lovelace was the wrong surface for a multi-user product — Telegram WebApp gives us rich detail views with built-in auth and push, without a second platform to maintain. HA remains the deployment platform only.

### [ITEM-P03] 1Password / passport checks
- **Parked:** Over-engineered for current stage.

### [ITEM-P04] Tikkie (iDEAL) payment integration
- **Parked:** Use Tikkie Business API to send payment requests directly in the Telegram chat — trusted by the Dutch demographic (especially older users who distrust credit card forms). Replaces ITEM-007/ITEM-008's payment mechanism. Netherlands-only (iDEAL). Revisit when: (1) ITEM-002 savings tracker proves value, (2) 3-5 active users, (3) contribution vs subscription model decided.

### [ITEM-045] Onboarding: ask transport mode per airport
- **Parked:** Superseded by [ITEM-053]. The conversational-onboarding flow survives as the **low-confidence fallback** inside ITEM-053 (when auto-fill can't resolve a route, e.g. no NS coverage and no curated parking row), but it's no longer the primary mechanism. Original framing — "ask the user how they get to each airport" — was a UX dead-end because users churn before completing N per-airport conversations. Auto-fill with confirm-or-override is the right primary path.

### [ITEM-004] Google Maps one-time transport lookup per city
- **Parked:** Subsumed by [ITEM-053]. ITEM-004's "cache by city, reuse across users" idea is preserved as an implementation detail inside ITEM-053 — the `airport_transport_option` table stores per-user rows but Google Maps API responses can be cached by `(origin_city, airport_code, mode)` tuple in a separate cache table to avoid re-calling for users in the same city. Out of scope until multi-user demand exists.