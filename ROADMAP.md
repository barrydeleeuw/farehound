# FareHound Roadmap

> Last updated: 2026-05-09 (v0.9.0)

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

### [ITEM-045] Onboarding: ask transport mode per airport
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** S
- **Dependencies:** [ITEM-043] (Done)
- **Summary:** After airport confirmation, ask the user how they get to each airport. Without this, transport costs are blank and FareHound can't calculate true door-to-door cost — the core value proposition. A simple per-airport question ("How do you get to Schiphol? Car / Train / Uber / Bus") plus estimated cost fills the `airport_transport` table properly. Could be a single Claude-powered conversational step: "How do you usually get to the airport? e.g. 'I drive to Schiphol, take the train to Rotterdam, and Uber to Eindhoven'" → Claude parses into structured transport data.
- **Acceptance Criteria:**
  - [ ] User is asked about transport to each airport during onboarding
  - [ ] Transport mode and estimated cost stored in `airport_transport`
  - [ ] Works conversationally (one natural language message, not N separate questions)
  - [ ] Fallback: if user skips, transport costs remain NULL (already handled gracefully)

### [ITEM-049] Telegram Mini Web App
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** L
- **Dependencies:** [ITEM-051] must ship first — its "📊 Details" button is the entry point
- **Summary:** Rich detail views launched from a button on each Telegram alert. Telegram WebApp `web_app` button hands off signed `initData` (no separate auth). Replaces parts of the parked [ITEM-P02] (Lovelace dashboard) — Telegram WebApp is a better fit than HA Lovelace for a multi-user direction.
- **Pages:**
  - `/deal/{id}` — price-history sparkline (90d), full alternatives table including <€75-savings ones, baggage policy by airline, structured why-best, booking deep links
  - `/routes` — list, snooze toggles, edit dates
  - `/settings` — baggage preference, transport overrides, notification quiet hours
- **Stack suggestion:** FastAPI + HTMX deployed alongside the bot on the Pi (or on Railway later when [ARCHITECTURE.md](ARCHITECTURE.md) Phase A migration happens). Matches the codebase's Python skill set, no JS build step.
- **Why this matters:** [ITEM-051] fixes the message-fits-in-chat parts of the value prop. Anything richer (price charts, side-by-side comparisons, full alternatives view) needs a real surface — and Telegram Mini Apps give us that without giving up Telegram's auth/push benefits.
- **Acceptance Criteria:**
  - [ ] WebApp button added to deal alerts pointing to `/deal/{id}` page
  - [ ] `initData` validation server-side (HMAC against bot token)
  - [ ] Deal detail page shows: price history chart, full alternatives table, baggage by airline, structured why-best, booking deep link
  - [ ] Routes page shows snooze toggles wired to existing snooze logic from [ITEM-051]
  - [ ] Settings page persists preferences to DB
  - [ ] HTTPS configured (Cloudflare Tunnel or Caddy on the Pi)
  - [ ] Tests for `initData` validation and route handlers

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