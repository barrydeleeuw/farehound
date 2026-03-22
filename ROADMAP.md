# FareHound Roadmap

> Last updated: 2026-03-22

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

### [ITEM-006] Gmail deal pipeline (Secret Flying + Jack's Flight Club)
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** L
- **Dependencies:** [ITEM-001]
- **Summary:** Ingest deal emails from Secret Flying (and later Jack's Flight Club) via Google Apps Script → webhook. Parse structured deal data, match against user-configured trips, and notify on relevant cheap fares. Error fares and flash sales are where the biggest savings happen — directly serving our mission.
- **Email analysis (Secret Flying):**
  - Sender: `my@deals.secretflying.com` — filter by this address
  - Two deal formats observed:
    1. **Single-origin** — e.g. "Non-stop from Amsterdam to Curacao for €191 one-way". One departure city, one destination, list of available dates.
    2. **Multi-origin** — e.g. "Business Class from European cities to Johannesburg from €1419 roundtrip". Multiple departure cities, each with its own price range and date pairs.
  - Structured fields in every email: DEPART (city/country), ARRIVE (city/country), DATES (month + ordinal day lists), STOPS (non-stop or layover city), AIRLINES (carrier name)
  - Price embedded in subject line and body; can be one-way or roundtrip with ranges per origin
  - Cabin class sometimes present (Business Class, lie-flat seats)
  - Dates are human-readable ordinals ("3rd, 5th, 6th May") — need parsing to actual dates
- **Pipeline design:**
  1. Google Apps Script filters inbox for sender `deals.secretflying.com`, extracts HTML body
  2. POST to FareHound webhook endpoint with raw email content
  3. Parser extracts: origins (city + country → IATA code), destination (→ IATA code), price (amount + currency + one-way/roundtrip), dates, airline, stops, cabin class
  4. Matcher checks parsed origins/destinations against all users' configured trips (including nearby airports)
  5. On match: send Telegram alert with deal details and "GO TO DEAL" link
- **Acceptance Criteria:**
  - [ ] Google Apps Script deployed that forwards Secret Flying emails to FareHound webhook
  - [ ] Parser handles both single-origin and multi-origin email formats
  - [ ] City names resolved to IATA airport codes (Amsterdam → AMS, Brussels → BRU, etc.)
  - [ ] Deals matched against user trips — origin airport (or nearby) + destination match
  - [ ] Telegram notification sent with: route, price, dates, airline, cabin class, deal link
  - [ ] Duplicate deals not re-notified (dedup by route + price + date range)
  - [ ] Architecture extensible for Jack's Flight Club emails (different format, same pipeline)

### [ITEM-019] Airline promo email ingestion
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** L
- **Dependencies:** [ITEM-006]
- **Summary:** Create a dedicated FareHound email address and subscribe it to promotional mailing lists from airlines worldwide. This inbox becomes a continuous source of fare sales, flash deals, and route launches — directly from the airlines themselves. Reuses the email parsing + matching pipeline from ITEM-006 (Secret Flying / JFC), but with per-airline parsers since each airline's email format differs.
- **Design considerations:**
  - Dedicated address (e.g. `deals@farehound.app`) subscribed to airline promo lists globally
  - Each airline has its own email template — need a parser-per-airline or a generic LLM-based extractor that pulls origin, destination, price, dates, cabin class, and booking link from any promo email
  - Volume will be high (dozens of airlines × multiple emails/week) — needs dedup, rate limiting, and relevance filtering before hitting the matcher
  - Matcher reuses the same logic as ITEM-006: check parsed routes against user-configured trips (including nearby airports), notify via Telegram on match
  - Some airline promos are region-targeted (e.g. "from Amsterdam" or "from Europe") — parser must handle both specific-origin and regional deals
  - Consider a managed email service (e.g. Mailgun inbound routing, Google Workspace) to programmatically process incoming mail rather than polling IMAP
- **Acceptance Criteria:**
  - [ ] Dedicated email address created and subscribed to major airline promo lists
  - [ ] Inbound email pipeline processes incoming promos automatically
  - [ ] Parser extracts structured deal data (routes, prices, dates, airline, cabin class) from airline emails
  - [ ] Deals matched against user trips and Telegram notifications sent on match
  - [ ] Deduplication prevents re-notifying the same promo deal
  - [ ] At least 10 major airlines subscribed at launch (KLM, Transavia, Ryanair, easyJet, Vueling, Turkish, Etihad, Emirates, Lufthansa, TAP)
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

### [ITEM-018] E2E Telegram bot test harness
- **Status:** Proposed
- **Priority:** P2 (Medium)
- **Effort:** M
- **Dependencies:** None
- **Summary:** Telethon-based integration tests that connect as a test user and walk through bot flows (onboarding, trip creation, deal alerts, booking follow-up). Verifies message content and conversation state against expectations. Requires a test Telegram account and running bot instance.


### [ITEM-011] Weekend/short trip date windowing
- **Status:** Ready
- **Priority:** P1 (High)
- **Effort:** S
- **Dependencies:** None
- **Summary:** "Long weekend in May" should generate Thu/Fri→Sun/Mon windows, not May 1-31. Trip duration model exists but needs proper orchestrator integration.

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


### [ITEM-017] SerpAPI response cache for local testing
- **Status:** Done
- **Summary:** SERPAPI_CACHE_DIR env var enables cached responses locally. 17 responses recorded. Zero API calls during dev.

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
- **Status:** Done — Smart daily digest (only undecided trips), transparent cost breakdown in alerts, booking follow-up verified, Reddit RSS 403s fixed (JSON API), HA dead code removed (homeassistant.py, farehound/src/, Lovelace card).

## Parked

### [ITEM-P01] Creative routing (virtual interlining)
- **Parked:** No affordable API. Revisit if budget increases.

### [ITEM-P02] HA Lovelace dashboard
- **Parked:** Removed from consideration. Telegram is the sole interface. HA is the deployment platform only.

### [ITEM-P03] 1Password / passport checks
- **Parked:** Over-engineered for current stage.
