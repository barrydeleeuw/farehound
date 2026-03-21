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
│  DuckDB (/data/flights.duckdb)                               │
│  routes, price_snapshots, deals, alert_rules, poll_windows   │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  Analysis Engine (Claude API)                                │
│  • Score against DuckDB history + SerpAPI price_insights     │
│  • Behavioral feedback: learns from booked/dismissed deals   │
│  • Urgency classification: book_now / watch / skip           │
│  • Pre-filter: only scores deals 10%+ below avg              │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  Alerting                                                    │
│  • HA notifications (primary) — push + action buttons        │
│  • Telegram bot (optional) — @BotFather bot                  │
│  • Lovelace sensors — sensor.farehound_{route}_price         │
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
| Database layer | `src/storage/db.py` | DuckDB queries, schema, feedback tracking |
| Data models | `src/storage/models.py` | Dataclasses: Route, PriceSnapshot, Deal, PollWindow, AlertRule |
| Deal scorer | `src/analysis/scorer.py` | Claude API scoring with behavioral feedback enrichment |
| HA notifier | `src/alerts/homeassistant.py` | HA REST API notifications, action buttons, sensor updates |
| Telegram notifier | `src/alerts/telegram.py` | Telegram Bot API alerts (optional secondary channel) |
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

## DuckDB Schema

```sql
CREATE TABLE routes (
    route_id          VARCHAR PRIMARY KEY,
    origin            VARCHAR NOT NULL,
    destination       VARCHAR NOT NULL,
    trip_type         VARCHAR DEFAULT 'round_trip',
    earliest_departure DATE,
    latest_return     DATE,
    date_flex_days    INTEGER DEFAULT 3,
    max_stops         INTEGER DEFAULT 1,
    passengers        INTEGER DEFAULT 2,
    preferred_airlines VARCHAR[],
    notes             VARCHAR,
    active            BOOLEAN DEFAULT true,
    created_at        TIMESTAMP DEFAULT now()
);

CREATE TABLE poll_windows (
    window_id         VARCHAR PRIMARY KEY,
    route_id          VARCHAR REFERENCES routes(route_id),
    outbound_date     DATE NOT NULL,
    return_date       DATE,
    priority          VARCHAR DEFAULT 'normal',
    last_polled_at    TIMESTAMP,
    lowest_seen_price DECIMAL(10,2),
    created_at        TIMESTAMP DEFAULT now()
);

CREATE TABLE price_snapshots (
    snapshot_id       VARCHAR PRIMARY KEY,
    route_id          VARCHAR REFERENCES routes(route_id),
    observed_at       TIMESTAMP NOT NULL,
    source            VARCHAR NOT NULL,       -- 'serpapi_poll' | 'serpapi_verify'
    passengers        INTEGER NOT NULL,
    outbound_date     DATE,
    return_date       DATE,
    lowest_price      DECIMAL(10,2),
    currency          VARCHAR DEFAULT 'EUR',
    best_flight       JSON,
    all_flights       JSON,
    price_level       VARCHAR,
    typical_low       DECIMAL(10,2),
    typical_high      DECIMAL(10,2),
    price_history     JSON,
    search_params     JSON,
    created_at        TIMESTAMP DEFAULT now()
);

CREATE TABLE deals (
    deal_id           VARCHAR PRIMARY KEY,
    snapshot_id       VARCHAR REFERENCES price_snapshots(snapshot_id),
    route_id          VARCHAR REFERENCES routes(route_id),
    score             DECIMAL(3,2),
    urgency           VARCHAR,                -- 'book_now' | 'watch' | 'skip'
    reasoning         VARCHAR,
    booking_url       VARCHAR,
    alert_sent        BOOLEAN DEFAULT false,
    alert_sent_at     TIMESTAMP,
    booked            BOOLEAN DEFAULT false,
    feedback          VARCHAR,                -- 'booked' | 'dismissed' | NULL
    created_at        TIMESTAMP DEFAULT now()
);

CREATE TABLE alert_rules (
    rule_id           VARCHAR PRIMARY KEY,
    route_id          VARCHAR REFERENCES routes(route_id),
    rule_type         VARCHAR NOT NULL,
    threshold         DECIMAL(10,2),
    channel           VARCHAR NOT NULL,
    active            BOOLEAN DEFAULT true
);
```

## Key Patterns

### Async Everywhere

```python
# All I/O through httpx.AsyncClient
async with httpx.AsyncClient() as client:
    response = await client.get(url, params=params)

# DuckDB via run_in_executor (no native async)
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
duckdb             # Embedded analytical database
anthropic          # Claude API client
pyyaml             # Config parsing

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
SerpAPI client, DuckDB storage, config system, smart date polling, static threshold alerts, HA notifications, add-on scaffolding, search_once.py test script.

### Phase 2 — Claude Scoring
Deal scorer with Claude API, replaced static thresholds with AI scoring, daily digest, Google Flights URLs in alerts, traveller preferences in config.

### Phase 3 — Community Feed Integration
Telegram channel listener (Telethon), RSS feed listener (feedparser) for Reddit + Secret Flying, deal message parsing with date normalization, SerpAPI error fare verification, pre-filters (route match, date window, price sanity, Claude gate), urgent alert path.

### Phase 4 — Polish
Feedback loop (booked/dismissed tracking, behavioral prompt enrichment), Lovelace dashboard card with HA sensors, Telegram bot alerts (optional secondary), full test suite (139 tests).
