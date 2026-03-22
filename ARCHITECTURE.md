# FareHound — Multi-User Architecture Analysis

**Date:** March 22, 2026
**Author:** Architecture review for product evolution
**Context:** Barry wants to evolve FareHound from a personal HA add-on into a monetizable multi-user product.

---

## 1. Deployment Model

**Recommendation: Railway (or Fly.io as backup)**

The current HA add-on model is a dead end for multi-user. A Raspberry Pi can't serve other people's polling schedules, and HA's add-on system assumes single-tenant. But Barry is one person, not a DevOps team — so the deployment model needs to be "deploy and forget."

**Railway** is the right choice:
- **$5/mo** for a small persistent service (512MB RAM, shared CPU) — more than enough for 10-50 users
- **Managed Postgres** included in the plan (see section 2)
- **Deploy from GitHub** — push to main, it deploys. No Docker registry, no CI/CD pipeline to maintain
- **Cron jobs** supported natively — no need for APScheduler in-process (though keeping APScheduler is fine too)
- **Persistent volumes** available if needed
- **Scales to $20-30/mo** at 50+ users without architecture changes

**Why not the alternatives:**
- **Serverless (Lambda/Cloud Functions):** FareHound runs long-lived polling loops, community listeners, and a Telegram bot. These are persistent processes, not request-response handlers. Serverless would require rearchitecting everything into event-driven triggers with external schedulers, which adds complexity for no benefit at this scale.
- **Fly.io:** Good option, similar to Railway but slightly more DevOps-heavy (Dockerfiles, fly.toml). Fine as a backup if Railway pricing changes.
- **Render:** Similar to Railway but their free tier spins down on inactivity, which kills the polling loop. Paid tier works but Railway's DX is better.
- **VPS (Hetzner/DigitalOcean):** €4-5/mo is cheap, but then Barry owns the OS, security updates, monitoring, and disaster recovery. Not worth it for a solo developer.
- **Keep HA as an option:** Don't. The complexity of supporting two deployment models (HA add-on for self-hosters + cloud for multi-user) doubles the maintenance burden for a tiny self-hoster audience. If someone wants to self-host, they can run the Docker image directly — no need for HA-specific packaging.

**Migration path:** Strip the `ha-addon/` directory. Replace `run.sh` with a simple `Dockerfile` + `Procfile` for Railway. Environment variables already work (config.yaml reads from env vars). This is a one-day change.

---

## 2. Database

**Recommendation: Postgres via Railway (or Neon as a standalone alternative)**

SQLite doesn't work for multi-user in a cloud deployment. The reasons are practical, not theoretical:
- Railway's filesystem is ephemeral — SQLite data would be lost on redeploy unless using a persistent volume, which adds complexity
- Concurrent writes from multiple users' polling jobs will hit SQLite's write lock under load
- No remote access for debugging or analytics

**Postgres on Railway:**
- Included with Railway's plan, no separate billing
- FareHound's query patterns (INSERT, SELECT with WHERE, simple aggregations) are trivially portable from SQLite
- The schema changes needed for multi-user (adding `user_id` columns) are the same regardless of database choice

**Schema changes for multi-user:**

```sql
-- New: users table
CREATE TABLE users (
    user_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_chat_id TEXT UNIQUE NOT NULL,
    name            TEXT,
    home_location   TEXT,           -- "The Hague, Netherlands"
    home_airport    TEXT,           -- IATA code of primary airport
    preferences     JSONB,          -- traveller preferences (free-form)
    serpapi_budget   INTEGER DEFAULT 500,  -- monthly call limit per user
    active          BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Existing tables get user_id
ALTER TABLE routes ADD COLUMN user_id UUID REFERENCES users(user_id);
ALTER TABLE price_snapshots ADD COLUMN user_id UUID REFERENCES users(user_id);
ALTER TABLE deals ADD COLUMN user_id UUID REFERENCES users(user_id);
ALTER TABLE poll_windows ADD COLUMN user_id UUID REFERENCES users(user_id);
ALTER TABLE alert_rules ADD COLUMN user_id UUID REFERENCES users(user_id);

-- airport_transport becomes per-user (each user has different nearby airports)
ALTER TABLE airport_transport ADD COLUMN user_id UUID REFERENCES users(user_id);
```

**Why not the alternatives:**
- **SQLite per user:** Clever idea but operationally painful. Can't query across users for analytics. Can't do cross-user deduplication (e.g., two users watching AMS→NRT could share polling results). Backup/migration is per-file.
- **Supabase/Neon free tier:** These work and are free, but adding a separate managed DB when Railway includes Postgres just adds another service to manage. Use Neon only if deploying somewhere that doesn't bundle a database.
- **DuckDB:** Already rejected in v0.3.6 for ARM compatibility. Still wrong for multi-user — it's an OLAP engine, not a transactional database.

**Migration effort:** Medium. Replace `sqlite3` calls with `asyncpg` (or `psycopg` with connection pooling). The actual SQL is 95% compatible — main changes are `?` → `$1` for parameters and `INTEGER` booleans → native `BOOLEAN`. The `Database` class interface stays the same.

---

## 3. User Model & Onboarding

**Onboarding flow via Telegram bot:**

```
User finds @FareHoundBot → taps Start
    │
    ▼
Bot: "Welcome to FareHound! I find cheap flights from airports near you.
      Where do you live? (city or address)"
    │
    User: "The Hague" or "Amsterdam" or "near Düsseldorf"
    │
    ▼
Bot uses Google Maps Geocoding API to resolve location
    → Finds airports within 250km radius
    → Calculates drive/train times via Google Maps Distance Matrix API
    → Presents: "I found these airports near you:
        ✈️ AMS — Amsterdam Schiphol (30 min, primary)
        ✈️ RTM — Rotterdam (20 min)
        ✈️ EIN — Eindhoven (50 min)
        ✈️ BRU — Brussels (2.5h by train)
        ✈️ DUS — Düsseldorf (2h by car)
      I'll monitor all of these for your trips.
      Want to adjust this list?"
    │
    ▼
Bot: "Great! Tell me about a trip you're planning.
      Example: 'Japan for 2 weeks in October, 2 passengers'"
    │
    User describes trip → existing Claude NL parsing handles this
    │
    ▼
Bot confirms route, starts monitoring
```

**What can reuse current code:**
- `TripBot` class (`src/bot/commands.py`) — the entire conversational flow, Claude parsing prompts, trip confirmation, /trips, /remove commands. The only change is replacing the hardcoded `self._chat_id` check with a DB lookup by `chat_id`.
- Airport transport data model (`airport_transport` table) — already exists, just needs `user_id`.
- `_PARSE_PROMPT` and `_INTERPRET_SYSTEM` — work as-is, just inject the user's specific `home_airport` instead of reading from config.

**What's new:**
- Google Maps API integration for location → airport resolution (Geocoding + Distance Matrix). ~$2-5/mo for the API calls at 50 users (each user onboards once).
- Transport cost estimation: Google Maps gives drive time. Train costs need a simple lookup table (major European routes have fixed prices). Parking costs are manual input or a reasonable default.
- User preferences storage: the current `traveller.preferences` list in config.yaml becomes a JSONB column per user.

**Cost of Google Maps APIs:**
- Geocoding: $5/1000 requests. At onboarding only = negligible.
- Distance Matrix: $5/1000 elements. 1 user × 10 airports = 10 elements. 50 users = 500 elements = $2.50 total, once.
- Monthly cost: effectively $0 — these are one-time onboarding calls.

---

## 4. Telegram Bot Architecture

**Recommendation: Single bot, multi-user via chat_id. No web app needed.**

The current architecture is almost right. The `TripBot` already:
- Receives messages via long polling (`getUpdates`)
- Routes updates through `_handle_update`
- Maintains per-chat conversation history (`self._conversation_history: dict[str, list]`)
- Tracks pending confirmations per chat (`self._pending: dict[str, dict]`)

**What changes:**
1. **Remove the `chat_id` guard.** Currently, `TripBot.__init__` takes a single `chat_id` from config. For multi-user, every incoming message's `chat_id` is looked up in the `users` table. Unknown chat_id = new user → trigger onboarding flow.
2. **User context injection.** Each message handler currently reads routes from a shared DB. Multi-user: `db.get_active_routes()` becomes `db.get_active_routes(user_id=user_id)`. Same for airports, preferences, etc.
3. **Notification routing.** `TelegramNotifier.send_deal_alert()` currently sends to one `chat_id`. Multi-user: the orchestrator iterates over users who have matching routes and sends to each user's `chat_id`.

**Why not a web app:**
- FareHound's entire interaction model is conversational. A web app would duplicate what the Telegram bot already does, but with a worse mobile experience and more maintenance burden.
- Telegram handles auth (each user has a unique chat_id), notifications (push to mobile), and rich formatting (inline buttons, Markdown) for free.
- A web app makes sense later if FareHound needs a dashboard, settings page, or payment integration. But for MVP multi-user, Telegram is sufficient.
- If a web app is ever needed, it should be a thin layer for account management and billing only — not a parallel interface for trip management.

**Scaling concern:** Telegram's `getUpdates` long polling is fine for hundreds of users. If FareHound ever reaches thousands, switch to webhooks (Telegram pushes updates to a URL). This is a one-line config change, not an architecture change.

---

## 5. Cost Model

### Per-user costs

| Component | Monthly cost per user | Assumptions |
|-----------|----------------------|-------------|
| **SerpAPI** | **€8-25** | 3 routes × ~100-170 calls/route (primary 4h + secondary 8h). Starter plan = 1,000 calls for $25. 1 user with 3 routes ≈ 300-500 calls. |
| **Claude API** | **€1-3** | Scoring, parsing, digests. ~50-100 calls/mo at ~$0.01-0.03/call. |
| **Hosting** | **€0.10-0.50** | Railway $5/mo shared across users. At 10 users = €0.50/user. At 50 users = €0.10/user. |
| **Database** | **€0** | Included with Railway. |
| **Google Maps** | **€0** | One-time onboarding cost, negligible. |
| **Total** | **€10-28/user/mo** | SerpAPI is the dominant cost. |

### SerpAPI is the bottleneck

SerpAPI pricing drives everything:
- **Free:** 100 calls/mo — not viable for even 1 user with nearby airports
- **Starter ($25/mo):** 1,000 calls/mo — supports ~2-3 active users
- **Developer ($75/mo):** 5,000 calls/mo — supports ~10-15 active users
- **Business ($150/mo):** 15,000 calls/mo — supports ~30-50 active users

**Cost optimization strategies:**
1. **Share polling results.** If two users both watch AMS→NRT in October, poll once and share the snapshot. This alone could cut calls by 30-50% as the user base grows.
2. **Reduce secondary airport frequency.** Poll secondary airports daily instead of every 8h unless primary price is high.
3. **Cache aggressively.** SerpAPI results for the same route+date are valid for 2-4 hours. Don't re-poll if a recent snapshot exists.
4. **Consider Amadeus for Layer 1.** The free tier (2,000 calls/mo) could supplement SerpAPI for basic price checks, reserving SerpAPI for verification and price_insights data. This was deferred in the roadmap but becomes more attractive at multi-user scale.

### Pricing recommendation

**€9.99/month** per user.

Reasoning:
- At 10 users: revenue = €100/mo. SerpAPI Developer plan = €70/mo. Hosting = €5/mo. Claude = €15/mo. Margin = €10/mo. Tight but viable.
- At 30 users: revenue = €300/mo. SerpAPI Business = €140/mo. Hosting = €10/mo. Claude = €50/mo. Margin = €100/mo. Healthy.
- At 50 users: revenue = €500/mo. SerpAPI = €140-200/mo. Hosting = €20/mo. Claude = €80/mo. Margin = €200/mo.

**Alternative: freemium.** 1 route free (use shared polling to keep costs near zero), €9.99/mo for unlimited routes. This lets people try FareHound before paying and reduces churn risk.

**Payment integration:** Stripe via Telegram bot inline payment (Telegram supports this natively) or a simple landing page with Stripe Checkout. Don't build a billing system — use Stripe's customer portal for subscription management.

---

## 6. Migration Path

### Phase A: "Barry + 5 friends" (2-4 weeks of work)

**Goal:** Multi-user on a single Railway deployment. No payment, no onboarding automation. Barry manually adds users.

**Minimum changes:**
1. **Add `users` table.** Seed Barry's data. Add friends manually via SQL or a `/admin adduser` bot command.
2. **Add `user_id` to all tables.** Migrate existing data under Barry's user_id.
3. **Remove hardcoded `chat_id`.** TripBot looks up user by chat_id from incoming message. TelegramNotifier sends to the right user's chat_id.
4. **Deploy to Railway.** Dockerfile + Procfile. Environment variables for API keys (same as today). Railway Postgres replaces SQLite.
5. **Friends configure airports manually.** Barry adds their airports via SQL or a simple `/airports` command. No Google Maps integration yet.

**What stays the same:** All polling logic, scoring, alerts, community feeds, daily digest. These just get scoped to `user_id`.

**What this validates:** Whether other people actually find FareHound useful. Whether the cost model works. Whether the Telegram bot UX is clear enough for non-Barry users.

### Phase B: "50 beta users" (1-2 months after Phase A)

**Goal:** Self-service onboarding, automated airport setup, cost controls.

**Infrastructure:**
1. **Google Maps onboarding flow.** User says where they live → auto-populate airports.
2. **Per-user SerpAPI budget.** Track API calls per user, enforce monthly limits. Free tier users get 1 route, paid users get unlimited.
3. **Shared polling.** Deduplicate SerpAPI calls when multiple users watch the same route+dates.
4. **Stripe integration.** Simple subscription: free (1 route) or €9.99/mo (unlimited).
5. **User dashboard.** Minimal web page (or just Telegram commands) showing: active routes, monthly API usage, subscription status.
6. **Monitoring.** Basic alerting on Railway: is the bot up? Are polls running? API error rates.

### Phase C: "Paying product" (2-3 months after Phase B)

**Goal:** Product-market fit, growth, operational maturity.

**Additions:**
1. **Landing page.** Simple site explaining FareHound, with "Start on Telegram" CTA. Can be a single static page on Vercel.
2. **Referral system.** "Invite a friend, get a free month." Viral growth via Telegram sharing.
3. **Amadeus as Layer 1.** Switch scheduled polling to Amadeus (free tier), keep SerpAPI for verification only. Dramatically reduces per-user cost.
4. **Multi-region support.** Users outside Netherlands — different airport clusters, different community feeds.
5. **Operational tooling.** Admin dashboard, user analytics, cost tracking per user, automated alerting on budget overruns.
6. **Data privacy.** GDPR compliance: data export, account deletion, privacy policy. Required for EU product.

---

## 7. What to Keep vs Rewrite

### Keep and extend (scope to user_id)

| Component | File | Change needed |
|-----------|------|---------------|
| **SerpAPI client** | `src/apis/serpapi.py` | No changes. Stateless — takes route params, returns results. |
| **Nearby airport engine** | `src/analysis/nearby_airports.py` | No changes. Takes airport data + fares, returns comparison. |
| **Claude scorer** | `src/analysis/scorer.py` | No changes. Takes snapshot + history + feedback, returns score. |
| **Telegram notifier** | `src/alerts/telegram.py` | Replace `self._chat_id` with per-call `chat_id` parameter. Small change. |
| **Trip bot (commands)** | `src/bot/commands.py` | Replace hardcoded `chat_id` with DB lookup. Add user context to Claude prompts. Medium change — mostly adding `user_id` params to DB calls. |
| **Data models** | `src/storage/models.py` | Add `user_id` field to all models. Small change. |
| **Config loader** | `src/config.py` | Simplify — API keys come from env vars, user-specific config comes from DB. Remove traveller/routes from config.yaml. |
| **Community listener (RSS)** | `src/apis/community.py` | Extend to check deals against all users' routes, not just one config. Medium change. |
| **Airport utilities** | `src/utils/airports.py` | No changes. |
| **Airline utilities** | `src/utils/airlines.py` | No changes. |

### Rewrite

| Component | File | Why |
|-----------|------|-----|
| **Database layer** | `src/storage/db.py` | Replace sqlite3 with asyncpg. All queries get `user_id` WHERE clauses. Every method signature changes. This is the biggest single piece of work. |
| **Orchestrator** | `src/orchestrator.py` | Currently loads routes from config + DB for one user. Needs to iterate over all active users, load their routes, poll per-user. Scheduling logic needs per-user awareness. Major rewrite of `poll_routes()` and `send_daily_digest()`. |
| **Config.yaml** | `config.yaml` | Becomes infrastructure-only: API keys, polling intervals, feature flags. User-specific data (routes, airports, preferences) moves to DB. |

### Remove

| Component | File | Why |
|-----------|------|-----|
| **HA add-on packaging** | `ha-addon/` | No longer deploying as HA add-on. |
| **HA notifier** | `src/alerts/homeassistant.py` | Already unused (Telegram is primary). |
| **HA-specific config translation** | `_translate_ha_options()` in config.py | No longer needed. |
| **Lovelace card** | `ha-addon/lovelace-card.yaml` | No longer needed. |

### Keep but defer changes

| Component | File | Notes |
|-----------|------|-------|
| **Telegram channel listener** | `src/apis/community.py` (Telethon) | Works for community deals. Multi-user: check against all users' routes. Defer until Phase B. |
| **Feedback loop** | In `db.py` + `scorer.py` | Currently per-single-user. Multi-user: feedback is naturally per-user (each user's chat_id → their deals). Works without changes. |

---

## 8. First Steps

**Build Phase A: "Barry + 5 friends."** This is the minimum viable experiment to validate the multi-user idea.

### Concrete first steps (in order):

1. **Deploy current FareHound to Railway.** Don't change any code — just get it running in the cloud with Railway Postgres. This proves the deployment model works and decouples from HA.

2. **Add the `users` table and `user_id` columns.** Write a migration script. Seed Barry as user #1. Update `Database` class to accept `user_id` in all methods.

3. **Make TripBot multi-user.** Remove the `chat_id` guard. Look up user by `chat_id` on every message. Unknown `chat_id` → create user with a simple "What's your name? What's your home airport?" flow (no Google Maps yet — just ask for the IATA code).

4. **Make the orchestrator multi-user.** `poll_routes()` iterates over all active users. `send_daily_digest()` sends per-user digests. Each user's routes are polled independently.

5. **Make the notifier multi-user.** `TelegramNotifier` methods take `chat_id` as a parameter instead of using `self._chat_id`.

6. **Invite 3-5 friends.** Give them the bot link. Watch what happens. Collect feedback.

### What NOT to build first:

- Google Maps onboarding (manual airport setup is fine for 5 friends)
- Payment/Stripe (it's free for friends)
- Shared polling optimization (not needed at 5 users)
- Landing page (word of mouth is fine)
- Amadeus API (SerpAPI works)
- Web dashboard (Telegram is enough)

**The goal of Phase A is learning, not building.** Does anyone besides Barry actually want this? What do they find confusing? What features do they ask for? Build the minimum to find out.

---

## Summary

| Decision | Recommendation |
|----------|---------------|
| **Hosting** | Railway ($5/mo) |
| **Database** | Postgres on Railway |
| **User model** | Telegram chat_id as identity, user table in Postgres |
| **Bot architecture** | Single bot, multi-user via chat_id lookup |
| **Pricing** | €9.99/mo (freemium: 1 route free) |
| **First milestone** | Barry + 5 friends on Railway, no payment |
| **Biggest rewrite** | `db.py` (sqlite→asyncpg) and `orchestrator.py` (single→multi-user polling) |
| **Biggest cost risk** | SerpAPI — shared polling is critical for scale |
| **Kill switch** | If friends don't use it after 2 weeks, the product idea isn't validated. Don't build Phase B. |
