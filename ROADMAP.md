# FareHound Roadmap

> Last updated: 2026-03-24

## Mission

**Making travel accessible for everyone by finding the lowest real cost to fly.**

Most flight search tools show you the ticket price. FareHound shows you the true cost — including how you get to the airport, what parking costs, and whether a "cheaper" flight from a farther airport actually saves you money. Everyone deserves to know when a genuinely great deal exists, not just people who spend hours checking multiple airports and deal sites.

Every feature we build serves this mission: reduce the gap between what people pay and what they could pay, with zero effort on their part.

## In Progress

### [ITEM-001] Multi-user stabilization
- **Status:** In Progress
- **Priority:** P0 (Critical)
- **Effort:** M
- **Dependencies:** None
- **Summary:** Fix remaining bugs from v2.0 multi-user launch — onboarding flow, cache consistency, SerpAPI budget management.
- **Acceptance Criteria:**
  - [ ] Onboarding flow works end-to-end (name → location → airports → first trip)
  - [ ] Cached SerpAPI responses used for local testing
  - [ ] Poll interval at 24h keeps within Starter plan budget
  - [ ] Barry's routes work correctly after fresh DB migration
  - [ ] Second user can onboard and see separate trips

## Ready

### [ITEM-030] Fix: max_stops not enforced in SerpAPI searches
- **Status:** Ready
- **Priority:** P0 (Critical)
- **Effort:** S
- **Dependencies:** None
- **Summary:** `max_stops` is stored on the route model but never passed to SerpAPI or used to filter results — in primary polling, secondary polling, or on-demand secondary polls. SerpAPI's `stops` param supports this (0=any, 1=nonstop, 2=up to 1 stop, 3=up to 2 stops). This caused a false "€1,151 savings from Düsseldorf" claim for Amsterdam→Mexico City: the DUS cheapest flight (€1,410) had 2 stops (LHR+DFW), but the route was configured for max 1 stop. The price comparison should have excluded that flight entirely.
- **Acceptance Criteria:**
  - [ ] Pass `stops` parameter to SerpAPI `search_flights()` derived from `route.max_stops` (map: max_stops=0→stops=1 nonstop, max_stops=1→stops=2 up to 1 stop, etc.)
  - [ ] Apply to all three call sites: primary poll, secondary poll, on-demand secondary poll
  - [ ] Post-filter: if `price_insights.lowest_price` is used as fallback (ITEM-028), also filter `best_flights`/`other_flights` by stop count before taking min price
  - [ ] Verify with cached SerpAPI responses that filtered results differ from unfiltered
- **Fix location:** `src/apis/serpapi.py` (add `max_stops` param to `search_flights`), `src/orchestrator.py` (pass `route.max_stops` at all call sites)

### [ITEM-031] Factor flight duration into deal scoring and nearby comparisons
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** S
- **Dependencies:** None
- **Summary:** Currently deals and nearby airport comparisons only consider price and transport cost/time to the airport. Flight duration (total travel time including layovers) is not factored in. A "cheaper" flight from a secondary airport with a 21h journey vs a 12h direct route from the primary airport may not be a genuine saving. Flight duration data is already available in SerpAPI results (`total_duration` on each flight). This should be surfaced in alerts and used by the scorer to qualify recommendations.
- **Acceptance Criteria:**
  - [ ] Extract `total_duration` from flight results and store alongside price data in secondary comparisons
  - [ ] Include flight duration in the nearby comparison data passed to the Claude scorer and Telegram alerts
  - [ ] Alert message shows flight duration for both primary and secondary alternatives (e.g. "DUS saves €200 but adds 9h travel time")
  - [ ] Scorer prompt updated to weigh duration — a large duration penalty should reduce the recommendation strength
- **Fix location:** `src/analysis/nearby_airports.py` (add duration to comparison dict), `src/orchestrator.py` (extract duration from results), `src/alerts/telegram.py` (display duration), `src/analysis/scorer.py` (update prompt)

### [ITEM-028] Fix: secondary airport savings uses unreliable price source
- **Status:** Ready
- **Priority:** P0 (Critical)
- **Effort:** S
- **Dependencies:** None
- **Summary:** `_poll_secondary_airports` and `_poll_secondary_airports_for_snapshot` in orchestrator.py use `price_insights.lowest_price` from SerpAPI, which can be a historical low or per-person value inconsistent with actual bookable flights. This caused a false "€555 savings from Düsseldorf" claim for Amsterdam→Tokyo Narita when DUS flights were actually more expensive (€1,034/pp vs €866/pp from AMS). The same pattern is already done correctly in `verify_fare()`.
- **Acceptance Criteria:**
  - [ ] Use `min(f["price"] for f in best_flights + other_flights)` as primary price source in `_poll_secondary_airports` and `_poll_secondary_airports_for_snapshot`
  - [ ] Fall back to `price_insights.lowest_price` only when no flight results have prices
  - [ ] Apply same fix to `_store_snapshot` for primary airport consistency
  - [ ] Existing `test_nearby_airports.py` tests still pass
- **Fix location:** `src/orchestrator.py` lines ~700 and ~815, `src/apis/serpapi.py` (extract helper)

### [ITEM-029] Enhanced debug logging for SerpAPI polling and airport comparisons
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** S
- **Dependencies:** None
- **Summary:** Currently the Docker logs only show HTTP request traces (httpx INFO) with no application-level detail about prices, comparisons, or savings calculations. The Düsseldorf bug (ITEM-028) was undiagnosable from logs alone. Add structured debug logging so future price discrepancies can be identified immediately from `ha apps logs`.
- **Acceptance Criteria:**
  - [ ] Each SerpAPI search logs: `price_insights.lowest_price` vs actual min flight price from results, with a warning if they diverge by >20%
  - [ ] Secondary airport comparison logs per-airport breakdown: airport code, fare_pp, transport cost, parking, net total
  - [ ] Final `compare_airports` result logs: primary net vs best secondary net, savings amount, whether threshold was met
  - [ ] Log level is DEBUG (not INFO) so it doesn't flood normal logs — enable via HA add-on config option or env var

### [ITEM-027] Non-blocking bot: run price checks as background tasks
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** S
- **Dependencies:** None
- **Summary:** The bot processes updates sequentially — while `_immediate_price_check` runs (5-15s of SerpAPI calls), no new messages are polled. If the user sends another message while a price check is running, it sits in the Telegram queue unprocessed. Fix: run `_immediate_price_check` as `asyncio.create_task()` instead of `await`. The bot loop continues polling immediately after "Route added". The price check sends its results when done. Same pattern for any long-running operation in the bot handler.
- **Fix location:** `src/bot/commands.py` — replace `await self._immediate_price_check(...)` with `asyncio.create_task(self._immediate_price_check(...))`. Consider the same for `_interpret_message` Claude API calls.

### [ITEM-011] Weekend/short trip date windowing
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** S
- **Dependencies:** None
- **Summary:** "Long weekend in May" should generate Thu/Fri→Sun/Mon windows, not May 1-31. Trip duration model exists but needs proper orchestrator integration.

### [ITEM-002] Savings tracker
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** S
- **Dependencies:** None
- **Summary:** Track cumulative savings per user. Every time FareHound finds a cheaper alternative, log it. This is the proof that our mission works — "FareHound has found €12,400 in savings for our users."
- **Acceptance Criteria:**
  - [ ] New `savings_log` table: user_id, deal_id, route_id, primary_cost, alternative_cost, savings_amount, airport_code, timestamp
  - [ ] Logged every time a nearby alternative with savings > €75 is found
  - [ ] `/savings` command shows total: "FareHound has found €2,400 in potential savings across your trips"
  - [ ] Data available for future contribution/billing features


## Proposed

### [ITEM-004] Google Maps one-time transport lookup per city
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** S
- **Dependencies:** [ITEM-001]
- **Summary:** Cache Google Maps transport data by city. If two users live in Amsterdam, reuse the same lookup.

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
- **Acceptance Criteria:**
  - [ ] Gmail API client polls for new emails from JFC/SF senders every 5 minutes
  - [ ] JFC parser handles single-destination and multi-destination formats (with IATA codes, price ranges, travel months, cabin class, baggage tags)
  - [ ] SF parser extracts origin/destination cities (resolve to IATA), price, example dates, stops, airline
  - [ ] Deals matched against all users' routes (destination + origin/nearby + date overlap)
  - [ ] Telegram notification on match with deal details and booking link
  - [ ] Dedup by email_id and route+price+date range (24h window)
  - [ ] Deals stored in `email_deals` table for history
  - [ ] Parsing errors logged, never crash the pipeline

### [ITEM-019] Airline promo email ingestion
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** L
- **Dependencies:** [ITEM-006]
- **Summary:** Create a dedicated FareHound email address and subscribe it to promotional mailing lists from airlines worldwide. This inbox becomes a continuous source of fare sales, flash deals, and route launches — directly from the airlines themselves. Reuses the email parsing + matching pipeline from ITEM-006 (Secret Flying / JFC), but with per-airline parsers since each airline's email format differs.
- **Design considerations:**
  - Uses the shared FareHound Gmail account (`farehound2203@gmail.com`) — same inbox as ITEM-006 (Secret Flying / JFC)
  - Each airline has its own email template — need a parser-per-airline or a generic LLM-based extractor that pulls origin, destination, price, dates, cabin class, and booking link from any promo email
  - Volume will be high (dozens of airlines × multiple emails/week) — needs dedup, rate limiting, and relevance filtering before hitting the matcher
  - Matcher reuses the same logic as ITEM-006: check parsed routes against user-configured trips (including nearby airports), notify via Telegram on match
  - Some airline promos are region-targeted (e.g. "from Amsterdam" or "from Europe") — parser must handle both specific-origin and regional deals
  - Gmail API (via service account or OAuth) to programmatically read incoming mail — shared infra with ITEM-006
- **Acceptance Criteria:**
  - [ ] `farehound2203@gmail.com` subscribed to major airline promo lists
  - [ ] Inbound email pipeline processes incoming promos automatically
  - [ ] Parser extracts structured deal data (routes, prices, dates, airline, cabin class) from airline emails
  - [ ] Deals matched against user trips and Telegram notifications sent on match
  - [ ] Deduplication prevents re-notifying the same promo deal
  - [ ] At least 10 major airlines subscribed at launch (KLM, Transavia, Ryanair, easyJet, Vueling, Turkish, Etihad, Emirates, Lufthansa, TAP)
- **Subscription onboarding tool:** Small CLI/script that walks through the airline list one by one. For each airline: opens the newsletter signup URL in the browser → you subscribe with `farehound2203@gmail.com` → the tool polls the Gmail inbox for a confirmation email from that airline → marks it as subscribed → moves to the next. Turns a tedious afternoon into a guided checklist session. Requires maintaining a `newsletter_url` per airline in `airlines.py`.
- **Subscription health monitoring:** The email pipeline automatically stamps each airline with a `last_received` date when it processes an incoming promo. Airlines with no email in X weeks get flagged — could mean unsubscribed, spam-filtered, or inactive list.
- **Review notes:** High volume source — this could generate a lot of noise. The LLM-based parser approach (vs. regex per airline) is probably the only scalable option given the variety of email formats. Depends on ITEM-006's pipeline being in place first.

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
- **Dependencies:** [ITEM-001]
- **Summary:** "Japan" monitors NRT + KIX + NGO as separate routes under one trip. User says "all of them" → creates routes for each.

### [ITEM-015] Adaptive polling frequency
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** M
- **Dependencies:** [ITEM-001]
- **Summary:** Poll more when departure < 6 weeks (4h), less when > 4 months (48h). Currently fixed at 24h.

### [ITEM-018] E2E Telegram bot test harness
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** M
- **Dependencies:** None
- **Summary:** Telethon-based integration tests that connect as a test user and walk through bot flows (onboarding, trip creation, deal alerts, booking follow-up). Verifies message content and conversation state against expectations. Requires a test Telegram account and running bot instance.

## Done

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

## Parked

### [ITEM-P01] Creative routing (virtual interlining)
- **Parked:** No affordable API. Revisit if budget increases.

### [ITEM-P02] HA Lovelace dashboard
- **Parked:** Removed from consideration. Telegram is the sole interface. HA is the deployment platform only.

### [ITEM-P03] 1Password / passport checks
- **Parked:** Over-engineered for current stage.
