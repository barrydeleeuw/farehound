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

### [ITEM-035] Immediate fare feedback after trip creation
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** S
- **Dependencies:** None
- **Summary:** After adding a route, the user should immediately see that FareHound is looking up fares (typing indicator + "Checking prices..." message), followed by actual results seconds later. Currently, `_immediate_price_check()` exists and sends a typing indicator, but when it fails the user only sees "I'll check prices on the next poll cycle" — which feels broken. The happy path also lacks an explicit "Checking prices now..." message before the typing indicator, so the user doesn't know what's happening.
- **Fix approach:**
  - Send "Checking prices now..." message immediately after "Route added" confirmation
  - Ensure typing indicator stays active during the SerpAPI call
  - On success: show fares for home airport + nearby airports with price comparison
  - On failure: show specific error ("Price check timed out, will retry on next poll cycle") instead of generic fallback
  - On partial success (home airport OK, some nearby airports failed): show what we have, note which airports couldn't be checked
- **Acceptance Criteria:**
  - [ ] User sees "Checking prices now..." immediately after route confirmation
  - [ ] Typing indicator visible during SerpAPI lookup
  - [ ] Fares displayed inline for home + nearby airports on success
  - [ ] Partial failures show available results + note about missing airports
  - [ ] Full failure shows specific error message, not generic fallback

### [ITEM-034] Bug: Region/archipelago destinations should ask for clarification
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** S
- **Dependencies:** None
- **Summary:** "Add a trip to the Canary Islands" auto-picked Las Palmas (LPA) without asking which island. The `_PARSE_PROMPT` only triggers `needs_clarification` for country-level destinations (e.g. "Japan" → which city?), but not for regions or archipelagos with multiple airports. "Canary Islands" should offer: Gran Canaria (LPA), Tenerife (TFS), Fuerteventura (FUE), Lanzarote (ACE). Related to ITEM-012 (multi-destination) but this is a simpler prompt fix — treat regions/archipelagos like countries in the disambiguation logic.
- **Fix approach:**
  - Extend `_PARSE_PROMPT` disambiguation rule to cover regions, archipelagos, and island groups — not just countries
  - Add examples: "Canary Islands", "Balearic Islands", "Greek Islands", "Hawaii"
  - When user says "all of them", create routes for each (ties into ITEM-012)
- **Acceptance Criteria:**
  - [ ] "Canary Islands" triggers clarification with LPA, TFS, FUE, ACE options
  - [ ] Other archipelago/region destinations also trigger clarification
  - [ ] User can pick one or say "all" (ITEM-012 dependency for "all")



### [ITEM-036] Redesign trip recommendation message layout
- **Status:** Proposed
- **Priority:** P1 (High)
- **Effort:** M
- **Dependencies:** None (but should land before or alongside ITEM-022, ITEM-005)
- **Summary:** Current trip recommendation messages mix Google Flights data, FareHound-calculated costs, and price context in an inconsistent order. The message starts with price/pp, then total cost, then dates, and ends with typical range — making it hard to scan. Alternative airports are inconsistently shown (sometimes present, sometimes missing). With upcoming features adding more information (luggage costs via ITEM-037, preferred airline via ITEM-005, discovery deals via ITEM-038), the layout needs a clear, consistent structure before it gets worse.
- **Current problems:**
  - Information order isn't logical — price comes before dates, context comes last
  - No visual separation between "flight facts" (from Google Flights) and "FareHound value-add" (transport, parking, total cost)
  - Alternative airports inconsistently shown — sometimes present, sometimes absent, no explanation why
  - No room for upcoming data: luggage costs, preferred airline comparison, deal source
  - Error fare alerts use a completely different layout from regular alerts
  - Daily digest uses yet another layout variation
- **Acceptance Criteria:**
  - [ ] All three message types (deal alert, error fare, digest) use consistent section ordering
  - [ ] Flight info (airline, stops, dates) shown before pricing
  - [ ] True cost breakdown clearly separated from flight price with FareHound label
  - [ ] Price context (level + range + trend) grouped together near cost
  - [ ] Alternatives section always present — shows alternatives or "none cheaper"
  - [ ] Error fare alerts include cost breakdown and alternatives
  - [ ] Daily digest includes airline info
  - [ ] Message renders well on mobile Telegram (tested on iOS + Android)

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