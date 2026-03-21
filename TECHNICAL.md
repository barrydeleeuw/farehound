# FareHound — Technical Reference

FareHound is a personal flight fare monitoring service deployed as a Home Assistant add-on. It combines scheduled price polling via SerpAPI Google Flights with real-time community error fare detection (Telegram channels), AI-powered deal scoring (Claude), and HA native notifications. Designed to run 24/7 on existing HAOS hardware with near-zero cost.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                       DATA SOURCES                            │
│                                                               │
│  ┌─────────────────────┐       ┌──────────────────────────┐  │
│  │  SerpAPI             │       │  Telegram Channels       │  │
│  │  (Google Flights)    │       │  (error fare communities)│  │
│  │                      │       │                          │  │
│  │  LAYER 1: scheduled  │       │  LAYER 2: detection      │  │
│  │  polling (2-4h)      │       │  (real-time listener)    │  │
│  │                      │       │                          │  │
│  │  LAYER 2: on-demand  │       │  Via Telethon            │  │
│  │  verification        │       │                          │  │
│  └──────────┬───────────┘       └────────────┬─────────────┘  │
└─────────────┼────────────────────────────────┼────────────────┘
              │                                │
              ▼                                ▼
┌─────────────────────────┐   ┌──────────────────────────────────┐
│  Scheduled Poller       │   │  Community Listener              │
│                         │   │                                  │
│  • Smart date polling   │   │  • Match deal against watchlist  │
│    (spread windows,     │   │  • Verify via SerpAPI ───────┐   │
│     focus on cheapest)  │   │  • Urgent alert path         │   │
│  • Store snapshots      │   │                              │   │
│  • ~150-300 calls/mo    │   └──────────┬───────────────────┘   │
│    per route            │              │                       │
└────────────┬────────────┘              │                       │
             │                           │                       │
             ▼                           ▼                       │
┌──────────────────────────────────────────────────────────────┐ │
│  DuckDB (/data/flights.duckdb)                               │ │
│  price_snapshots, routes, deals, alert_rules, poll_windows   │ │
└──────────────────────────┬───────────────────────────────────┘ │
                           │                                     │
                           ▼                                     │
┌──────────────────────────────────────────────────────────────┐ │
│  Analysis Engine (Claude API)                                │ │
│  • Score against DuckDB history + SerpAPI price_insights     │ │
│  • Urgency classification: book_now / watch / skip           │ │
└──────────────────────────┬───────────────────────────────────┘ │
                           │                                     │
                           ▼                                     │
┌──────────────────────────────────────────────────────────────┐ │
│  Alerting                                                    │ │
│  • HA native notifications (primary) — Companion App push    │ │
│  • Lovelace dashboard card (Phase 4)                         │ │
│  • No web UI                                                 │ │
└──────────────────────────────────────────────────────────────┘
```

## Component List

| Component | File Path | Responsibility |
|-----------|-----------|----------------|
| Config loader | `src/config.py` | Load/validate YAML config, resolve env vars for secrets |
| SerpAPI client | `src/apis/serpapi.py` | All Google Flights queries (Layer 1 polling + Layer 2 verification) |
| Community listener | `src/apis/community.py` | Telegram channel listener via Telethon, route matching |
| Database layer | `src/storage/db.py` | DuckDB connection pool, all queries, schema migrations |
| Data models | `src/storage/models.py` | Dataclasses for routes, snapshots, deals, poll windows |
| Deal scorer | `src/analysis/scorer.py` | Claude API scoring prompt, response parsing |
| HA notifier | `src/alerts/homeassistant.py` | HA REST API notifications with action buttons |
| Orchestrator | `src/orchestrator.py` | Event loop, scheduling (APScheduler), pipeline coordination |
| Entrypoint | `ha-addon/run.sh` | Container startup script |
| HA add-on config | `ha-addon/config.yaml` | Add-on metadata, options schema, ports |
| App config | `config.yaml` | Routes, preferences, API key refs, alert channels |
| Manual search | `scripts/search_once.py` | One-off SerpAPI search for testing |

## Data Flow

### Layer 1 — Scheduled Polling

```
APScheduler timer (every 2-4h)
  → orchestrator.run_scheduled_poll()
    → For each active route:
      → Determine poll windows (smart date strategy)
      → serpapi.search_flights(route, dates, passengers)
      → db.store_snapshot(result)
      → scorer.score_deal(snapshot, history_from_db)
      → If score >= threshold:
        → homeassistant.send_alert(deal, google_flights_url)
        → db.record_alert(deal)
```

### Layer 2 — Community-Triggered

```
Telethon listener (always-on)
  → community.on_message(message)
    → Parse origin/destination/price from message
    → Match against active routes in DB
    → If match:
      → serpapi.verify_fare(route, dates, passengers)
      → db.store_snapshot(verification_result)
      → scorer.score_deal(snapshot, history, price_insights)
      → homeassistant.send_urgent_alert(deal, booking_url)
      → db.record_alert(deal)
```

### Smart Date Polling Strategy

Goal: stay within ~150-300 SerpAPI searches/month per route.

1. **Initial scan**: 3-4 spread date windows across the travel period
2. **Focus polling**: concentrate on the window with lowest prices (poll every 2-4h)
3. **Weekly rescan**: re-check all windows for price shifts
4. **Expand on drops**: if a price drop detected, poll adjacent dates to find the optimum

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
    priority          VARCHAR DEFAULT 'normal',  -- 'focus' | 'normal' | 'rescan'
    last_polled_at    TIMESTAMP,
    lowest_seen_price DECIMAL(10,2),
    created_at        TIMESTAMP DEFAULT now()
);

CREATE TABLE price_snapshots (
    snapshot_id       VARCHAR PRIMARY KEY,
    route_id          VARCHAR REFERENCES routes(route_id),
    window_id         VARCHAR REFERENCES poll_windows(window_id),
    observed_at       TIMESTAMP NOT NULL,
    source            VARCHAR NOT NULL,          -- 'serpapi_poll' | 'serpapi_verify'
    outbound_date     DATE,
    return_date       DATE,
    passengers        INTEGER NOT NULL,
    lowest_price      DECIMAL(10,2),
    currency          VARCHAR DEFAULT 'EUR',
    best_flight       JSON,
    all_flights       JSON,
    price_level       VARCHAR,                   -- from price_insights
    typical_low       DECIMAL(10,2),
    typical_high      DECIMAL(10,2),
    price_history     JSON,
    search_params     JSON,                      -- SerpAPI request params for debugging
    created_at        TIMESTAMP DEFAULT now()
);

CREATE TABLE deals (
    deal_id           VARCHAR PRIMARY KEY,
    snapshot_id       VARCHAR REFERENCES price_snapshots(snapshot_id),
    route_id          VARCHAR REFERENCES routes(route_id),
    score             DECIMAL(3,2),
    urgency           VARCHAR,                   -- 'book_now' | 'watch' | 'skip'
    reasoning         VARCHAR,
    booking_url       VARCHAR,
    alert_sent        BOOLEAN DEFAULT false,
    alert_sent_at     TIMESTAMP,
    booked            BOOLEAN DEFAULT false,
    created_at        TIMESTAMP DEFAULT now()
);

CREATE TABLE alert_rules (
    rule_id           VARCHAR PRIMARY KEY,
    route_id          VARCHAR REFERENCES routes(route_id),
    rule_type         VARCHAR NOT NULL,          -- 'price_below' | 'score_above'
    threshold         DECIMAL(10,2),
    channel           VARCHAR NOT NULL,          -- 'ha_notify'
    active            BOOLEAN DEFAULT true
);
```

Key differences from spec schema:
- `source` values are `serpapi_poll` / `serpapi_verify` (no Amadeus)
- Added `poll_windows` table for smart date strategy
- Added `passengers` to `price_snapshots` (variable traveller count)
- Added `window_id` FK in snapshots
- Added `search_params` for debugging
- Routes have `passengers` column (configurable, not hardcoded to 2)

## Key Patterns

### Async Everywhere

```python
# All I/O through httpx.AsyncClient
async with httpx.AsyncClient() as client:
    response = await client.get(url, params=params)

# DuckDB via run_in_executor (no native async)
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(None, db.execute_query, sql, params)

# Telethon listener and APScheduler share one event loop
loop = asyncio.get_event_loop()
scheduler.start()
await client.run_until_disconnected()  # Telethon blocks the loop
```

### Config Loading

```python
# Secrets resolved from environment variables
# config.yaml references env var names, not values:
#   serpapi:
#     api_key_env: SERPAPI_API_KEY
# At load time: os.environ[config["serpapi"]["api_key_env"]]
```

### Error Handling

- API failures: log + skip, don't crash the loop. Retry on next poll cycle.
- Telegram disconnect: Telethon auto-reconnects. Log reconnection events.
- DuckDB write failures: log + continue. Alerting should not depend on successful DB write.
- SerpAPI rate limits: track monthly usage in memory, pause polling when approaching limit.

### ID Generation

UUIDs for all primary keys (`uuid.uuid4().hex`).

## Dependencies

```
# Core
httpx              # Async HTTP client (all API calls)
duckdb             # Embedded analytical database
anthropic          # Claude API client
pyyaml             # Config parsing

# Community feeds
telethon           # Telegram channel monitoring

# Scheduling
apscheduler        # Job scheduling (AsyncIOScheduler)

# Dev
pytest             # Testing
pytest-asyncio     # Async test support
rich               # CLI output formatting (scripts)
```

Not used (dropped from spec):
- `amadeus` — Amadeus self-service API decommissioned July 2026
- `serpapi` Python package — using httpx directly against SerpAPI REST endpoint
- `requests` — replaced by httpx
- `feedparser` — RSS dropped, Telegram-first
- `python-telegram-bot` — alerts via HA only, not Telegram bot
- `aiosmtplib` — email alerts dropped

## SerpAPI Google Flights — API Reference

**Endpoint:** `https://serpapi.com/search`

**Request params:**

| Param | Value | Notes |
|-------|-------|-------|
| `engine` | `google_flights` | Required |
| `api_key` | `{key}` | From env |
| `departure_id` | `AMS` | IATA airport code |
| `arrival_id` | `NRT` | IATA airport code |
| `outbound_date` | `2026-10-08` | YYYY-MM-DD |
| `return_date` | `2026-10-22` | YYYY-MM-DD, omit for one-way |
| `type` | `1` | 1=round trip, 2=one way |
| `adults` | `2` | Variable per route |
| `currency` | `EUR` | |
| `hl` | `en` | Language |
| `deep_search` | `true` | Browser-identical results |
| `sort_by` | `2` | 2=price sorted |

**Response structure (key fields):**

```json
{
  "best_flights": [
    {
      "flights": [
        {
          "airline": "KLM",
          "flight_number": "KL861",
          "departure_airport": { "id": "AMS", "time": "10:25" },
          "arrival_airport": { "id": "NRT", "time": "06:15+1" },
          "duration": 660,
          "airplane": "Boeing 787-9",
          "legroom": "31 in",
          "extensions": ["Carbon emissions estimate: 500 kg"]
        }
      ],
      "total_duration": 660,
      "price": 485,
      "type": "Round trip"
    }
  ],
  "other_flights": [ "..." ],
  "price_insights": {
    "lowest_price": 485,
    "price_level": "low",
    "typical_price_range": [650, 900],
    "price_history": [[1711929600, 720], [1712016000, 715]]
  },
  "search_metadata": {
    "google_flights_url": "https://www.google.com/travel/flights?..."
  }
}
```

**Booking URLs:** Found in `booking_options[].together.booking_request.url` when available. Fall back to `search_metadata.google_flights_url` for Layer 1 alerts.

**Rate limits:** Free tier = 250/month. Starter ($25/month) = 1,000/month. Track usage via monthly call counter in memory.

## Build Phases

### Phase 1 — Core Monitoring Loop (current)
- SerpAPI client (`src/apis/serpapi.py`) — search + parse responses
- DuckDB storage — schema init, snapshot writes, history queries
- Config-driven route watchlist with variable passengers
- Smart date polling logic (spread windows, focus on cheapest)
- Basic HA notifications on static price thresholds
- `scripts/search_once.py` for manual testing
- HA add-on packaging (Dockerfile, config.yaml, run.sh)

### Phase 2 — Claude Scoring
- Scoring prompt with DuckDB price history context
- Replace static thresholds with AI-scored alerts
- Daily digest via HA notification
- Google Flights URLs in all alerts

### Phase 3 — Community Feed Integration
- Telegram channel listener (Telethon) for error fare channels
- Message parsing — extract origin/dest/price from deal posts
- Route matching against watchlist
- SerpAPI verification on match (confirm price + booking link)
- Urgent alert path with "Book Now" action button

### Phase 4 — Polish
- Lovelace dashboard card for price trends
- Historical trend visualisation
- HA automations (lights, speaker announcements for error fares)
- Data retention policy (archive to Parquet after 90 days)
