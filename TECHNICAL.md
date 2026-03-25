# FareHound — Technical Reference

FareHound is a personal flight fare monitoring service deployed as a Home Assistant add-on. It combines scheduled price polling via SerpAPI Google Flights with real-time community error fare detection (Telegram channels + RSS feeds), AI-powered deal scoring with behavioral learning (Claude), and multi-channel notifications. Designed to run 24/7 on existing HAOS hardware with near-zero cost.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                       DATA SOURCES                            │
│                                                               │
│  ┌─────────────────────┐  ┌────────────────┐  ┌───────────┐ │
│  │  SerpAPI             │  │  Telegram      │  │  RSS      │ │
│  │  (Google Flights)    │  │  Channels      │  │  Feeds    │ │
│  │                      │  │  (Telethon)    │  │           │ │
│  │  LAYER 1: scheduled  │  │                │  │  Reddit   │ │
│  │  polling (2-4h)      │  │  @trip4world   │  │  Secret   │ │
│  │                      │  │  @holidaypirat │  │  Flying   │ │
│  │  LAYER 2: on-demand  │  │               │  │  etc.     │ │
│  │  verification        │  │  LAYER 2       │  │  LAYER 2  │ │
│  └──────────┬───────────┘  └───────┬────────┘  └─────┬─────┘ │
└─────────────┼──────────────────────┼─────────────────┼───────┘
              │                      │                 │
              ▼                      ▼                 ▼
┌─────────────────────────┐   ┌──────────────────────────────┐
│  Scheduled Poller       │   │  Community Listeners         │
│                         │   │                              │
│  • Smart date polling   │   │  • Telegram (real-time)      │
│    (spread windows,     │   │  • RSS (every 5 min)         │
│     focus on cheapest)  │   │  • Pre-filters:              │
│  • Store snapshots      │   │    - Route match             │
│  • ~150-300 calls/mo    │   │    - Date window check       │
│    per route            │   │    - Price sanity (< avg)    │
│                         │   │  • Verify via SerpAPI        │
└────────────┬────────────┘   └────────────┬─────────────────┘
             │                             │
             ▼                             ▼
┌──────────────────────────────────────────────────────────────┐
│  SQLite (/data/flights.db)                                    │
│  routes, price_snapshots, deals, alert_rules, poll_windows   │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  Analysis Engine (Claude API)                                │
│  • Score against SQLite history + SerpAPI price_insights      │
│  • Behavioral feedback: learns from booked/dismissed deals   │
│  • Urgency classification: book_now / watch / skip           │
│  • Pre-filter: only scores deals 10%+ below avg              │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  Alerting                                                    │
│  • Telegram bot (primary) — @BotFather bot                   │
│  • Daily digest — scheduled summary of all routes            │
│  • Feedback loop — "Book Now" / "Not Interested" actions     │
└──────────────────────────────────────────────────────────────┘
```

## Component List

| Component | File Path | Responsibility |
|-----------|-----------|----------------|
| Config loader | `src/config.py` | Load/validate YAML config, resolve env vars, HA options translation |
| SerpAPI client | `src/apis/serpapi.py` | Google Flights search + verify_fare + date windowing + rate tracking |
| Community listener | `src/apis/community.py` | Telegram (Telethon) + RSS (feedparser) listeners, deal message parsing |
| Database layer | `src/storage/db.py` | SQLite queries, schema, feedback tracking |
| Data models | `src/storage/models.py` | Dataclasses: Route, PriceSnapshot, Deal, PollWindow, AlertRule |
| Deal scorer | `src/analysis/scorer.py` | Claude API scoring with behavioral feedback enrichment |
| Telegram notifier | `src/alerts/telegram.py` | Telegram Bot API alerts (primary channel) |
| Orchestrator | `src/orchestrator.py` | Event loop, scheduling, pipeline coordination, community callback |
| Entrypoint | `ha-addon/run.sh` | Container startup, env var export, graceful shutdown |
| HA add-on config | `ha-addon/config.yaml` | Add-on metadata, options schema |
| Lovelace card | `ha-addon/lovelace-card.yaml` | Dashboard card configs (entities, markdown, history-graph) |
| App config | `config.yaml` | Routes, preferences, API key refs, alert channels, community feeds |
| Manual search | `scripts/search_once.py` | One-off SerpAPI search for testing |

## Data Flow

### Layer 1 — Scheduled Polling

```
APScheduler timer (every 2-4h)
  → orchestrator.poll_routes()
    → For each active route:
      → Select poll windows (smart date strategy)
      → serpapi.search_flights(route, dates, passengers)
      → db.insert_snapshot(result)
      → Pre-filter: price < 90-day avg or cold start?
        → Yes: scorer.score_deal(snapshot, history, feedback)
        → If score >= alert_threshold:
          → notifier.send_deal_alert(deal_info)
          → telegram.send_deal_alert(deal_info)  [if enabled]
          → db.insert_deal(deal)
    → notifier.update_sensors(routes_summary)
```

### Layer 2 — Community-Triggered

```
Telegram listener (real-time) + RSS poller (every 5 min)
  → community.parse_deal_message(text)
    → Pre-filter 1: route matches active watchlist?
    → Pre-filter 2: dates within route travel window?
    → Pre-filter 3: community price < 90-day average?
    → serpapi.verify_fare(route, dates, expected_price)
    → Pre-filter 4: verified price 10%+ below average?
    → scorer.score_deal(snapshot, history, community_flagged=True, feedback)
    → If score >= threshold:
      → notifier.send_error_fare_alert(deal_info, booking_url)
      → telegram.send_error_fare_alert(deal_info)  [if enabled]
      → db.insert_deal(deal)
```

### Feedback Loop

```
User receives notification
  → Taps "Book Now" → deal.feedback = 'booked'
  → Taps "Not Interested" → deal.feedback = 'dismissed'
  → Ignores → deal.feedback = NULL

Next scoring call:
  → db.get_recent_feedback(limit=20)
  → Injected as PAST DECISIONS in Claude prompt
  → Claude calibrates scores based on revealed preferences
```

### Smart Date Polling Strategy

Goal: stay within ~150-300 SerpAPI searches/month per route.

1. **Initial scan**: 3-4 spread date windows across the travel period
2. **Focus polling**: concentrate on the window with lowest prices
3. **Weekly rescan**: re-check all windows for price shifts
4. **Expand on drops**: if a price drop detected, poll adjacent dates

## SQLite Schema

```sql
CREATE TABLE users (
    user_id           TEXT PRIMARY KEY,
    telegram_chat_id  TEXT UNIQUE NOT NULL,
    name              TEXT,
    home_location     TEXT,
    home_airport      TEXT DEFAULT 'AMS',
    preferences       TEXT,
    onboarded         INTEGER DEFAULT 0,
    active            INTEGER DEFAULT 1,
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE routes (
    route_id          TEXT PRIMARY KEY,
    origin            TEXT NOT NULL,
    destination       TEXT NOT NULL,
    trip_type         TEXT DEFAULT 'round_trip',
    earliest_departure TEXT,
    latest_return     TEXT,
    date_flex_days    INTEGER DEFAULT 3,
    max_stops         INTEGER DEFAULT 1,
    passengers        INTEGER DEFAULT 2,
    preferred_airlines TEXT,
    notes             TEXT,
    active            INTEGER DEFAULT 1,
    created_at        TEXT DEFAULT (datetime('now')),
    trip_duration_type TEXT,
    trip_duration_days INTEGER,
    preferred_departure_days TEXT,
    preferred_return_days TEXT
);

CREATE TABLE poll_windows (
    window_id         TEXT PRIMARY KEY,
    route_id          TEXT REFERENCES routes(route_id),
    outbound_date     TEXT NOT NULL,
    return_date       TEXT,
    priority          TEXT DEFAULT 'normal',
    last_polled_at    TEXT,
    lowest_seen_price REAL,
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE price_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    route_id          TEXT REFERENCES routes(route_id),
    window_id         TEXT REFERENCES poll_windows(window_id),
    observed_at       TEXT NOT NULL,
    source            TEXT NOT NULL,          -- 'serpapi_poll' | 'serpapi_verify'
    outbound_date     TEXT,
    return_date       TEXT,
    passengers        INTEGER NOT NULL,
    lowest_price      REAL,
    currency          TEXT DEFAULT 'EUR',
    best_flight       TEXT,
    all_flights       TEXT,
    price_level       TEXT,
    typical_low       REAL,
    typical_high      REAL,
    price_history     TEXT,
    search_params     TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE deals (
    deal_id           TEXT PRIMARY KEY,
    snapshot_id       TEXT REFERENCES price_snapshots(snapshot_id),
    route_id          TEXT REFERENCES routes(route_id),
    score             REAL,
    urgency           TEXT,                   -- 'book_now' | 'watch' | 'skip'
    reasoning         TEXT,
    booking_url       TEXT,
    alert_sent        INTEGER DEFAULT 0,
    alert_sent_at     TEXT,
    booked            INTEGER DEFAULT 0,
    feedback          TEXT,                   -- 'booked' | 'dismissed' | NULL
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE alert_rules (
    rule_id           TEXT PRIMARY KEY,
    route_id          TEXT REFERENCES routes(route_id),
    rule_type         TEXT NOT NULL,
    threshold         REAL,
    channel           TEXT NOT NULL,
    active            INTEGER DEFAULT 1
);

CREATE TABLE airport_transport (
    airport_code      TEXT PRIMARY KEY,
    airport_name      TEXT,
    transport_mode    TEXT,
    transport_cost_eur REAL,
    transport_time_min INTEGER,
    parking_cost_eur  REAL,
    is_primary        INTEGER DEFAULT 0
);
```

## Key Patterns

### Async Everywhere

```python
# All I/O through httpx.AsyncClient
async with httpx.AsyncClient() as client:
    response = await client.get(url, params=params)

# SQLite via run_in_executor (no native async)
loop = asyncio.get_running_loop()
result = await loop.run_in_executor(None, db.method, *args)

# Telegram listener, RSS poller, and APScheduler share one event loop
```

### Config Loading

```python
# HA add-on mode: flat options in /data/options.json
#   → _translate_ha_options() → nested AppConfig structure
# Local dev: config.yaml with nested structure directly
# Secrets: env var names in config, resolved at runtime
```

### Error Handling

- API failures: log + skip, don't crash. Retry on next poll cycle.
- Claude scoring failure: fall back to static 15%-drop threshold.
- Telegram disconnect: Telethon auto-reconnects. Task crash logged via done_callback.
- Community feed errors: per-feed isolation, one failing feed doesn't stop others.
- SerpAPI rate limits: in-memory monthly counter, warnings at 80%/90%.

## Dependencies

```
# Core
httpx              # Async HTTP (all API calls)
anthropic          # Claude API client
pyyaml             # Config parsing
# sqlite3 is built-in (Python stdlib)

# Community feeds
telethon           # Telegram channel monitoring
feedparser         # RSS feed parsing

# Scheduling
apscheduler        # Job scheduling (AsyncIOScheduler)

# Dev
pytest             # Testing
pytest-asyncio     # Async test support
rich               # CLI output formatting (scripts)
```

## SerpAPI Google Flights — API Reference

**Endpoint:** `https://serpapi.com/search`

| Param | Value | Notes |
|-------|-------|-------|
| `engine` | `google_flights` | Required |
| `departure_id` | `AMS` | IATA airport code |
| `arrival_id` | `NRT` | IATA airport code |
| `outbound_date` | `2026-10-08` | YYYY-MM-DD |
| `return_date` | `2026-10-22` | Omit for one-way |
| `type` | `1` | 1=round trip, 2=one way |
| `adults` | `2` | Variable per route |
| `currency` | `EUR` | |
| `hl` | `en` | Language |
| `deep_search` | `true` | Browser-identical results |
| `sort_by` | `2` | Price sorted |

**Key response paths:**
- `best_flights[].price` — cheapest options
- `price_insights.lowest_price` — lowest available
- `price_insights.price_level` — "low" / "typical" / "high"
- `price_insights.typical_price_range` — [low, high]
- `booking_options[].together.booking_request.url` — booking deep link
- `search_metadata.google_flights_url` — Google Flights fallback URL

**Pricing:** Free=250/mo, Starter($25)=1,000/mo, Developer($75)=5,000/mo

## Build Phases (all complete)

### Phase 1 — Core Monitoring Loop
SerpAPI client, SQLite storage, config system, smart date polling, static threshold alerts, Telegram notifications, add-on scaffolding, search_once.py test script.

### Phase 2 — Claude Scoring
Deal scorer with Claude API, replaced static thresholds with AI scoring, daily digest, Google Flights URLs in alerts, traveller preferences in config.

### Phase 3 — Community Feed Integration
Telegram channel listener (Telethon), RSS feed listener (feedparser) for Reddit + Secret Flying, deal message parsing with date normalization, SerpAPI error fare verification, pre-filters (route match, date window, price sanity, Claude gate), urgent alert path.

### Phase 4 — Polish
Feedback loop (booked/dismissed tracking, behavioral prompt enrichment), Lovelace dashboard card with HA sensors, Telegram bot alerts (optional secondary), full test suite (139 tests).
