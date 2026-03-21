# FareHound — v2 Gap Analysis & Roadmap

**Date:** March 21, 2026
**Current version:** v0.3.6 (running on HAOS)
**Target:** v2 spec (farehound-v2-spec.md)

---

## Executive Assessment

The v2 spec represents a significant vision shift: from "flight price monitor" to "personal trip operations assistant." The current codebase (v0.3.6) has strong foundations that align well with v2's architecture, but there are three major gaps and several medium ones. Here's what we have, what we need, and the recommended path.

---

## What We Have (v0.3.6) vs What v2 Needs

### Already Aligned with v2

| v2 Requirement | Current Status | Gap |
|---------------|---------------|-----|
| Telegram as primary interface | **Done** — bot with /trip, /trips, /remove, /help | Need richer conversational flow |
| Claude-powered trip parsing | **Done** — /trip parses natural language | Need clarification follow-ups (partially done), multi-destination |
| Claude-powered deal scoring | **Done** — advisory scoring with pattern knowledge | Need nearby airport data in prompt |
| Smart alerting (dedup) | **Done** — new low, book_now+low, inflection detection | Aligned |
| Deal quality indicators | **Done** — 🔥💰👀😴 based on score | Aligned |
| Airport name lookup | **Done** — 110+ airports, route_name() helper | Aligned |
| Nearby airport filter origins | **Done** — AMS, BRU, DUS, RTM, EIN, CGN, CRL, LGG, NRN | Need transport cost modeling |
| RSS community feeds | **Done** — SecretFlying, TheFlightDeal, Reddit | v2 replaces with Gmail+WhatsApp (see below) |
| SerpAPI verification | **Done** — verify community deals | Aligned |
| SQLite storage | **Done** — migrated from DuckDB | v2 says DuckDB but SQLite is better for HAOS |
| HA add-on packaging | **Done** — s6-overlay, auto-deploy via SSH | Aligned |
| Daily digest | **Done** — scheduled at 08:00 | Need nearby airport comparison in digest |
| Feedback loop | **Done** — booked/dismissed tracking | Aligned |
| Adaptive date windowing | **Done** — focus polling on cheapest windows | Aligned |

### Major Gaps (v2 core differentiators we don't have)

#### Gap 1: Nearby Airport Intelligence with Door-to-Door Cost
**v2 says:** "This is the primary unique differentiator. No existing tool does this."
**We have:** Nearby airports in RSS filter list, but zero transport cost modeling.
**What's needed:**
- `airport_transport` table (transport mode, cost, time, parking)
- `nearby_airports.py` — net cost engine: fare × pax + transport × 2 + parking
- Multi-airport polling per trip (AMS, BRU, DUS, EIN, CGN all queried)
- Scoring prompt enriched with nearby airport comparison
- Alert format: "Save €340 by departing from Brussels" with net cost breakdown
**Effort:** Medium — mostly new code, but schema/models exist to extend
**Priority:** **P0** — this is what makes FareHound worth existing

#### Gap 2: Deal Service Ingestion (JFC + Secret Flying via Gmail)
**v2 says:** Replace RSS feeds with Gmail API (JFC emails) + WhatsApp bridge (Secret Flying).
**We have:** RSS feeds (partially working — some Reddit 403s), no Gmail integration.
**What's needed:**
- `apis/gmail.py` — Gmail API client, OAuth, label polling
- `analysis/email_parser.py` — Claude-powered JFC/SF email parsing
- Gmail OAuth setup script
- Deduplication (same deal from multiple sources)
- watgbridge for WhatsApp→Telegram (Secret Flying fast path)
**Effort:** High — Gmail OAuth is fiddly, watgbridge adds operational complexity
**Priority:** **P1** — but RSS feeds work as a bridge until this is built

#### Gap 3: Amadeus API (Replace SerpAPI for Layer 1)
**v2 says:** Use Amadeus Self-Service API for scheduled polling (free tier: 2,000 calls/mo).
**We have:** SerpAPI for everything (polling + verification).
**Assessment:** This is a "nice to have" optimization, not a blocker. SerpAPI works fine and gives us Google Flights data (price_insights, typical ranges) that Amadeus doesn't provide. The v2 spec's cost analysis for Amadeus (3,000-6,500 calls/month for 1-2 trips) actually exceeds the free tier.
**Recommendation:** **Keep SerpAPI.** Add Amadeus only if SerpAPI costs become a problem. The Google price_insights data is more valuable than Amadeus's raw fares for scoring.
**Priority:** **P3** — defer unless SerpAPI budget becomes tight

### Medium Gaps

| Gap | v2 Requirement | Effort | Priority |
|-----|---------------|--------|----------|
| **Multi-destination per trip** | Trip to "Japan" = NRT + KIX | Small — extend trip model, multiple search calls | P1 |
| **Adaptive polling frequency** | Primary 4h, secondary 8h, escalate near departure | Medium — scheduler logic | P1 |
| **Trip model (replaces routes)** | Richer model: duration_days, preferred_connections, constraints | Small — extend existing Route model | P1 |
| **Conversational flow** | Multi-turn trip setup with clarification | Medium — extend /trip bot (partially done) | P2 |
| **Trip modification** | "Push Japan to November" via Telegram | Small — Claude parses, update route | P2 |
| **Cost gate** | Parse cheaply, verify sparingly pipeline | Small — mostly exists in orchestrator | P2 |
| **Shared Telegram group** | Add Paola to alerts | Trivial — send to group_id instead of chat_id | P3 |
| **Trip completeness checks** | Passport, calendar, 1Password | Medium — HA calendar API, 1Password CLI | P3 |
| **Data retention** | Archive old JSON, keep aggregates | Small — periodic cleanup job | P3 |

### Things to Drop from v2 Spec

| v2 Spec Item | Recommendation | Why |
|-------------|----------------|-----|
| **DuckDB** | Keep SQLite | DuckDB doesn't have ARM musllinux wheels. SQLite works perfectly. |
| **HA notifications** | Already dropped | Telegram is the sole channel. HA sensors still useful for dashboard. |
| **watgbridge (WhatsApp bridge)** | Defer to Phase 2 | Adds significant operational complexity. RSS feeds cover the fast path for now. |
| **1Password integration** | Defer indefinitely | Nice idea but overkill. A manual reminder suffices. |
| **Amadeus as primary API** | Keep SerpAPI | Google price_insights are more valuable than raw Amadeus fares for scoring. |
| **python-telegram-bot dependency** | Keep httpx approach | We already have a working bot with httpx. No need to add a dependency. |

---

## Recommended Build Phases

### Phase 1: Nearby Airport Engine (the killer feature)
**Goal:** Multi-airport monitoring with door-to-door cost comparison in alerts.

1. Create `airport_transport` table + seed with Barry's airports (AMS, BRU, DUS, EIN, CGN)
2. Build `nearby_airports.py` — net cost calculation engine
3. Extend orchestrator to poll secondary airports per trip
4. Add nearby airport comparison to Claude scoring prompt
5. Update Telegram alert format to show comparison (v2 spec format)
6. Update daily digest with per-airport breakdown

**This is the feature that justifies FareHound's existence.**

### Phase 2: Deal Pipeline Upgrade (Gmail + better parsing)
**Goal:** JFC + Secret Flying emails → parsed, matched, verified, alerted.

1. Gmail API client + OAuth setup
2. Claude-powered email parser (JFC format, SF format)
3. Deduplication by route+price+airline hash
4. Replace RSS feeds with Gmail polling (keep RSS as fallback)
5. WhatsApp bridge (if RSS fallback proves insufficient)

### Phase 3: Richer Trip Management
**Goal:** Full conversational trip management via Telegram.

1. Multi-destination support ("Japan" = NRT + KIX)
2. Trip modification ("push to November")
3. Preferred airline comparison in alerts
4. Adaptive polling (escalate near departure, de-escalate far out)
5. Shared Telegram group for Paola

### Phase 4: Intelligence & Polish
**Goal:** Trip readiness and refinements.

1. Trip completeness checks (passport expiry, calendar conflicts)
2. HA calendar integration
3. Data archival (90-day rolling window for raw JSON)
4. Optional HA Lovelace dashboard via REST API

---

## Key Architectural Decisions

1. **Keep SQLite over DuckDB** — ARM compatibility, zero compile time, built into Python
2. **Keep SerpAPI over Amadeus** — Google price_insights are uniquely valuable for scoring
3. **Keep httpx over python-telegram-bot** — already working, one less dependency
4. **RSS feeds as bridge** — keep until Gmail integration is proven
5. **Telegram as sole interface** — drop HA notifications (already done), keep HA sensors for dashboard
