# FareHound Roadmap

> Last updated: 2026-03-25

## Mission

**Making travel accessible for everyone by finding the lowest real cost to fly.**

Most flight search tools show you the ticket price. FareHound shows you the true cost — including how you get to the airport, what parking costs, and whether a "cheaper" flight from a farther airport actually saves you money. Everyone deserves to know when a genuinely great deal exists, not just people who spend hours checking multiple airports and deal sites.

No more subscribing to airline newsletters full of irrelevant promotions — FareHound monitors the routes you actually care about and alerts you only when the price is genuinely good. The promotional prices airlines email about already show up on Google Flights; the difference is FareHound filters the noise for you.

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

### [ITEM-032] Bug: Booking follow-up spam — duplicates and no send-once guard
- **Status:** Proposed
- **Priority:** P0 (Critical)
- **Effort:** S
- **Dependencies:** None
- **Summary:** Users receive duplicate "Did you book it?" follow-up messages — the same route+price asked multiple times per hour, indefinitely. Root cause: the hourly `_check_pending_feedback()` scheduler has no "follow-up already sent" tracking, no deduplication per route, and no rate limiting.
- **Root causes identified:**
  1. **No follow-up-sent tracking:** The `deals` table has `alert_sent` and `feedback` columns but no `follow_up_sent_at`. Once a deal is 3+ days old with `feedback IS NULL`, it matches `get_deals_pending_feedback()` every hour — forever — until the user responds.
  2. **No dedup per route:** Multiple deal records can exist for the same route+price (different date windows, community deal sources). Each record triggers its own follow-up message, so the user gets "Did you book AMS→NRT at €1,937?" three times if three deal records match.
  3. **No rate limiting or batching:** `_check_pending_feedback()` loops through all matching deals and fires Telegram messages immediately with no delay, batch grouping, or per-user throttle.
  4. **Hourly re-firing:** The scheduler runs every hour, so an unanswered follow-up produces a new message every hour until feedback is given — 24 messages/day per deal.
- **Fix approach:**
  1. **Add `follow_up_sent_at` column** to `deals` table. Set it when a follow-up is sent. `get_deals_pending_feedback()` excludes deals where `follow_up_sent_at IS NOT NULL`.
  2. **Deduplicate by route:** Group pending deals by `route_id` and send one follow-up per route (using the most recent/cheapest deal), not one per deal record.
  3. **Batch follow-ups per user:** Instead of sending N individual messages, send one consolidated message per user: "You saw these deals recently — did you book any?" with a list of routes. Reduces notification noise from N messages to 1.
  4. **Cap follow-up attempts:** Maximum 2 follow-ups per deal (e.g., at 3 days and 7 days). After that, mark the deal as `feedback = 'expired'` and stop asking. The user clearly isn't interested.
  5. **Respect quiet hours:** Don't send follow-ups between 22:00–08:00 user local time (ties into future user preferences).
- **Acceptance Criteria:**
  - [ ] `follow_up_sent_at` column added to deals table; migration handles existing data
  - [ ] Each deal receives at most 2 follow-up messages (at ~3 days and ~7 days)
  - [ ] Only one follow-up per route per user — deduplicated across deal records
  - [ ] Follow-ups batched into a single message per user when multiple routes are pending
  - [ ] Unanswered deals auto-expire after second follow-up (`feedback = 'expired'`)
  - [ ] No duplicate follow-up messages observed in testing

### [ITEM-033] Bug: NL trip creation silent failures
- **Status:** Proposed
- **Priority:** P0 (Critical)
- **Effort:** S
- **Dependencies:** None
- **Summary:** NL trip creation via Telegram silently fails on certain inputs. Observed: "Add a trip to Seoul on the same dates as Japan" produced no response at all. The `_interpret_message()` method in `commands.py` has a broad try-except that swallows Claude interpretation errors. If Claude fails to resolve a cross-trip reference or returns malformed JSON, the user gets either a generic "I didn't understand" or nothing. Additionally, the `add_trip` handler silently returns if `destination` is missing and `response_text` is empty.
- **Fix approach:**
  - Add specific error handling for cross-trip reference resolution failures — tell the user "I couldn't find a trip called 'Japan' in your active routes"
  - Ensure every code path in `_interpret_message()` sends visible feedback to the user
  - Log the raw Claude response on failure for debugging
- **Acceptance Criteria:**
  - [ ] Every NL message gets a visible response — no silent failures
  - [ ] Cross-trip reference failures produce a helpful error ("I couldn't find trip X")
  - [ ] Failed Claude interpretations logged with raw response for debugging

### [ITEM-034] Bug: Region/archipelago destinations should ask for clarification
- **Status:** Proposed
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

### [ITEM-035] Immediate fare feedback after trip creation
- **Status:** Proposed
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
- **Dependencies:** [ITEM-001]
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
- **Dependencies:** [ITEM-001], [ITEM-037]
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

### [ITEM-039] JFC flight-hacking knowledge base
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** M
- **Dependencies:** [ITEM-006]
- **Summary:** Ingest Jack's Flight Club flight-hacking articles (`https://members.jacksflightclub.com/articles/flight-hacking`, behind login) into a local knowledge base. These articles teach techniques like mistake fares, hidden city ticketing, positioning flights, and points hacking — knowledge FareHound could use to give smarter advice and eventually apply hacking strategies to user trips automatically.
- **Design considerations:**
  - Content is behind JFC member login — need authenticated scraping (session cookie or headless browser login)
  - Scrape article list, then fetch each article's full text + metadata (title, category, date)
  - Store in a `knowledge_articles` table: source, title, url, content, category, scraped_at
  - Generate embeddings for RAG retrieval — when a user asks travel questions, FareHound can reference relevant flight-hacking techniques
  - Respect rate limits and scraping ethics — cache aggressively, don't hammer their server
  - Could also enrich deal alerts: "This fare from AMS→TYO looks like a mistake fare — book fast!"
  - Re-scrape periodically (weekly?) to pick up new articles
- **Acceptance Criteria:**
  - [ ] Authenticated scraper fetches all flight-hacking articles from JFC members area
  - [ ] Articles stored in local DB with full text, metadata, and embeddings
  - [ ] RAG retrieval integrated into `_INTERPRET_SYSTEM` for general_chat responses
  - [ ] FareHound can answer "how do mistake fares work?" or "tips for booking cheap flights" using JFC knowledge
  - [ ] Periodic re-scrape picks up new articles without duplicating existing ones

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
