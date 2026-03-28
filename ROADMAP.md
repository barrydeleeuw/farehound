# FareHound Roadmap

> Last updated: 2026-03-28

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

### [ITEM-046] Bug: date windows ignore user's specified departure date
- **Status:** Proposed
- **Priority:** P0 (Critical)
- **Effort:** S
- **Dependencies:** None
- **Summary:** "Japan for 3 weeks departure around October 18" returns flights departing Nov 9 — three weeks off from the requested date. The parse prompt treats "around October 18" as a wide Oct-Nov search window, then `generate_date_windows` picks the cheapest window regardless of proximity to the user's intended date. The system should respect the departure date the user gave.
- **Two fixes needed:**
  1. **Parse prompt**: When the user gives a specific departure date (e.g. "departure October 18"), set `earliest_departure` to Oct 16 and `latest_return` based on trip duration from Oct 20 (±2 days flex). Don't expand to the full month.
  2. **Date flexibility step**: After parsing dates, ask the user: "Is October 18 a fixed departure, or are you flexible within a few weeks?" If fixed → ±2 days. If flexible → ask how many weeks of flexibility, then use that as the window.
- **Current behavior:** "departure around Oct 18, 3 weeks" → `earliest_departure=Oct 1, latest_return=Nov 30` → window generator picks Nov 9 → user gets flights 3 weeks late
- **Expected behavior:** "departure around Oct 18, 3 weeks" → `earliest_departure=Oct 16, latest_return=Nov 10` → window generator stays near Oct 18
- **Acceptance Criteria:**
  - [ ] Specific departure date ("October 18") generates ±2 day window, not full month
  - [ ] "Around October" (vague) generates full month window as today
  - [ ] User asked about date flexibility during trip creation
  - [ ] Flexible users get wider search, fixed users get tight window
  - [ ] Date shown in deal alerts matches what user asked for

### [ITEM-037] Luggage-aware total cost calculation
- **Status:** Proposed
- **Priority:** P1 (High)
- **Effort:** M
- **Dependencies:** None
- **Summary:** Factor baggage costs into deal scoring and alerts. A €200 fare with €60 return baggage fees isn't really €200 — it's €260. Currently FareHound only considers ticket price. This item adds luggage cost awareness so users see the true cost of flying, directly serving the mission of showing real cost.
- **Design considerations:**
  - **SerpAPI data:** Google Flights results via SerpAPI include `carry_on_bag`, `checked_bag` fields in booking options with costs per bag. Extract and use these.
  - **User preferences:** Users already specify baggage preferences ("values included checked baggage"). Extend this to a structured setting:
    - `baggage_needs`: `carry_on_only` | `one_checked` | `two_checked` (default: `one_checked`)
    - Used to calculate total cost: ticket price + applicable baggage fees (outbound + return)
  - **Airline baggage defaults:** Some airlines include bags (KLM long-haul includes 1 checked), others don't (Transavia, Ryanair). Maintain a simple lookup table for common airlines as fallback when SerpAPI doesn't return bag prices.
  - **Integration points:**
    - Scorer: pass total cost (ticket + bags) instead of ticket-only price
    - Alerts: show breakdown — "€240 ticket + €50 bags = €290 total"
    - Discovery (ITEM-038): include baggage estimate in anomaly alerts
    - Nearby airport comparison: compare total costs including bags, not just fares
- **Acceptance Criteria:**
  - [ ] Baggage cost extracted from SerpAPI response when available
  - [ ] User baggage preference configurable (`carry_on_only`, `one_checked`, `two_checked`)
  - [ ] Total cost (ticket + baggage) used in deal scoring instead of ticket-only price
  - [ ] Alert messages show cost breakdown: fare + baggage = total
  - [ ] Fallback airline baggage lookup for when SerpAPI doesn't provide bag prices
  - [ ] Nearby airport comparison uses total cost including baggage

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

### [ITEM-045] Onboarding: ask transport mode per airport
- **Status:** Proposed
- **Priority:** P1 (High)
- **Effort:** S
- **Dependencies:** [ITEM-043]
- **Summary:** After airport confirmation, ask the user how they get to each airport. Without this, transport costs are blank and FareHound can't calculate true door-to-door cost — the core value proposition. A simple per-airport question ("How do you get to Schiphol? Car / Train / Uber / Bus") plus estimated cost fills the `airport_transport` table properly. Could be a single Claude-powered conversational step: "How do you usually get to the airport? e.g. 'I drive to Schiphol, take the train to Rotterdam, and Uber to Eindhoven'" → Claude parses into structured transport data.
- **Acceptance Criteria:**
  - [ ] User is asked about transport to each airport during onboarding
  - [ ] Transport mode and estimated cost stored in `airport_transport`
  - [ ] Works conversationally (one natural language message, not N separate questions)
  - [ ] Fallback: if user skips, transport costs remain NULL (already handled gracefully)

### [ITEM-004] Google Maps one-time transport lookup per city
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** S
- **Dependencies:** None
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
- **Parked:** Removed from consideration. Telegram is the sole interface. HA is the deployment platform only.

### [ITEM-P03] 1Password / passport checks
- **Parked:** Over-engineered for current stage.

### [ITEM-P04] Tikkie (iDEAL) payment integration
- **Parked:** Use Tikkie Business API to send payment requests directly in the Telegram chat — trusted by the Dutch demographic (especially older users who distrust credit card forms). Replaces ITEM-007/ITEM-008's payment mechanism. Netherlands-only (iDEAL). Revisit when: (1) ITEM-002 savings tracker proves value, (2) 3-5 active users, (3) contribution vs subscription model decided.