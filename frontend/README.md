# FareHound — Mini Web App (frontend)

This is the **frontend-only** layer of [ITEM-049 (Telegram Mini Web App)](../ROADMAP.md). The backend (FastAPI endpoints, `initData` validation, deployment via Cloudflare Tunnel or Caddy) is a separate piece of work — see [_What the backend needs to expose_](#what-the-backend-needs-to-expose) below.

## Preview

```
cd frontend
python3 -m http.server 5173
```

Then open <http://localhost:5173/>.

The index page lists all three screens. Each is a self-contained HTML file with inline mock data, so you can iterate on the design without any backend.

## Stack — what was chosen and why

**Plain HTML + CSS + ~150 lines of vanilla JS.** No framework, no build step.

The roadmap suggested **FastAPI + HTMX**, and that's still the right call for the eventual production wiring (server-rendered, fits the Python codebase, no JS toolchain on the Pi). But for this design pass that would have meant designing around `hx-get` placeholders pointing at endpoints that don't exist yet — making the static preview less honest. The HTML structure here is intentionally thin so it ports trivially: roughly 30% becomes Jinja partials, the rest of the markup stays as-is.

**One JS dep at runtime:** the Telegram WebApp SDK loaded from `telegram.org/js/telegram-web-app.js`. Outside Telegram, that script no-ops gracefully — the page still renders, with a fallback `Book now on Google Flights` button standing in for the Telegram MainButton.

## Design intent

See [DESIGN.md](DESIGN.md) for the full rationale. Short version:

- **Concept:** "FareHound is a flight ledger." Editorial, hairline, monospace numerals. No rounded corners. Color used sparingly — green for savings, red for skip, everything else two-tone.
- **Type:** Fraunces (display serif), Hanken Grotesk (UI sans), JetBrains Mono (numerals).
- **Layout:** mobile-first, ≤480px column. Each section is a hairline rule + an all-caps tracked label + content.
- **Entry animation:** subtle staggered rise on page load, ~480ms, respects `prefers-reduced-motion`.
- **Theme:** dark default (Telegram users skew dark), light variant for `prefers-color-scheme: light`. Both palettes overridden at runtime by `--tg-theme-*` CSS variables when running inside Telegram.

## What's mocked vs real

| Element | Status | Source when real |
|---|---|---|
| Page structure & layout | **Real / final** | — |
| CSS / typography / palette | **Real / final** | — |
| Cost-breakdown row expansion | **Real interaction** | — |
| Sparkline rendering | **Real (SVG generation from any series)** | hits the same generator on real data |
| Telegram MainButton wiring | **Real (no-ops outside Telegram)** | — |
| Snooze / unsnooze chips | **Real local state**; toast is a `Telegram.WebApp.showAlert` call | will POST to `/api/routes/{id}/snooze` |
| Watching / Skip buttons | **Real local state**, confirmation flow via `showConfirm` | will POST to `/api/deals/{id}/feedback` |
| Deal data (price, breakdown, reasoning, alternatives, baggage) | **Inline mock** | reads from `deals` + `price_snapshots` + `airport_transport` + `src/utils/baggage.py` |
| Price-history series (30 sample points) | **Inline mock** | reads from `price_snapshots.price_history` |
| Routes list | **Inline mock** (3 cards) | reads from `routes` + most recent `deals` |
| Settings values | **Inline mock** | reads from `users.preferences` + `airport_transport` |

## What the backend needs to expose

Three thin JSON endpoints. The frontend does no auth itself — the backend validates the Telegram `initData` HMAC against the bot token and resolves the user from the included `user.id`.

### `GET /api/deals/:deal_id`

```jsonc
{
  "deal_id": "d_ams_nrt_2026-10-15",
  "route": { "origin": "AMS", "destination": "NRT", "name": "Tokyo" },
  "dates": { "outbound": "2026-10-15", "return": "2026-11-05" },
  "passengers": 2,
  "airline": "KLM longhaul",
  "price_pp": 1820,
  "price_total": 3640,
  "delta_since_alert": -40,
  "breakdown": {
    "flights": 3640,
    "baggage": 120,
    "transport": 90,
    "parking": 0,
    "explanations": {
      "flights": "€1,820/pp × 2 — KLM, 1 stop via DOH, 14h 35m total",
      "baggage": "2× checked 23kg, both directions. Carry-on and 1× checked included.",
      "transport": "Uber from The Hague to AMS, €45 each direction × 2 trips.",
      "parking": "No parking — Uber drop-off."
    }
  },
  "reasoning": [
    { "headline": "Cheapest of 4 dates polled.", "detail": "15 Oct saves €60/pp vs the next-best date." },
    { "headline": "€80 below Google's typical low.", "detail": "Range €620–€780, you're at €540." },
    { "headline": "Yours is best — door to door.", "detail": "€40 cheaper than EIN, €120 cheaper than BRU once transport is in." }
  ],
  "price_history": {
    "series": [["2026-02-09", 1980], ...],   // [iso_date, price] pairs
    "typical_low": 620,
    "typical_high": 780
  },
  "alternatives": {
    "airports": [
      { "code": "AMS", "desc": "Uber 34m", "total": 1925, "is_current": true, "is_best": true },
      { "code": "EIN", "desc": "Car 50m, parking €80", "total": 1965 },
      ...
    ],
    "dates": [
      { "label": "8 Oct", "desc": "Wed → Sun", "total": 1880 },
      { "label": "15 Oct", "desc": "Wed → Sun", "total": 1820, "is_current": true, "is_best": true },
      ...
    ]
  },
  "baggage_policy": {
    "airline_label": "KLM longhaul",
    "items": [
      { "item": "carry-on (12kg)", "cost": "included" },
      { "item": "1× checked (23kg)", "cost": "included" },
      { "item": "2× checked (23kg)", "cost": "+€60 each way" }
    ]
  },
  "book_url": "https://www.google.com/travel/flights?..."
}
```

### `GET /api/routes`

```jsonc
{
  "summary": {
    "monitored": 3,
    "snoozed": 1,
    "last_poll_at": "2026-05-10T10:42:08Z",
    "serpapi_usage": { "used": 184, "cap": 950 },
    "savings_total_eur": 840,
    "savings_trip_count": 2
  },
  "routes": [
    {
      "route_id": "ams_nrt",
      "ordinal": "01",
      "origin": "AMS", "destination": "NRT",
      "city": "Tokyo",
      "outbound": "2026-10-15", "return": "2026-11-05",
      "passengers": 2,
      "current_price_pp": 1820,
      "delta_since_alert": -40,
      "snoozed_until": null,
      "deals_this_week": 1,
      "alerted_price": 1860,
      "last_poll_iso": "2026-05-10T10:42:08Z",
      "latest_deal_id": "d_ams_nrt_2026-10-15"
    },
    ...
  ]
}
```

### `POST /api/routes/parse`

Splits the parse step from the create step so we can show a confirm card before committing anything to the DB. **Submit-only** — Claude is called once per add, on submit, not as the user types. (Decision: 2026-05-10. Live-as-you-type preview was rejected in favour of cost discipline matching the existing bot flow. Revisit if Claude per-call cost drops.)

```jsonc
// request
{ "text": "Seoul for 2 weeks in October" }

// response
{
  "origin": "AMS",            // inferred from user.home_airport
  "dest": "ICN",
  "city": "Seoul",
  "outbound": "2026-10-08",
  "return": "2026-10-22",
  "pax": 2,
  "trip_duration_days": 14,
  "ambiguities": []           // e.g. ["Could be Seoul (ICN) or Sokcho (SHO)"]
}
```

### `POST /api/routes`

Body is the structured parse from `/parse` (after user confirms). Backend creates the route, immediately fires a poll, returns `{ "route_id": "...", "first_poll_eta_seconds": 30 }`. Frontend prepends a placeholder card; in production it polls `GET /api/deals/latest?route_id=…` until the first snapshot lands.

### `POST /api/routes/:route_id/snooze` and `POST /api/deals/:deal_id/feedback`

Body: `{ "days": 7 }` for snooze, `{ "feedback": "watching" | "booked" | "dismissed" }` for feedback. Backend handles the auto-snooze-on-booked side effect that already exists in `src/bot/commands.py:_auto_snooze_route_for_deal`.

### `GET /api/settings` and `PATCH /api/settings`

Standard CRUD against `users.preferences` and `airport_transport` (per-airport rows). The fields are already documented in [TECHNICAL.md](../TECHNICAL.md).

## Telegram integration cheatsheet

The frontend already calls these — they no-op gracefully outside Telegram:

| Feature | Call | When it fires |
|---|---|---|
| Theme variables | CSS reads `--tg-theme-*` automatically | On page load |
| BackButton | `data-back="true"` on `<body>` | Pages that should show a back button (deal, settings) |
| MainButton | `Telegram.WebApp.MainButton.setText().show()` | Deal page sets it to "BOOK NOW" |
| Confirmation prompt | `Telegram.WebApp.showConfirm` | Skip-route action |
| Toasts | `Telegram.WebApp.showAlert` | All snooze / watch / skip confirmations |
| Haptics | `Telegram.WebApp.HapticFeedback.impactOccurred` | Any button tap |
| Open external link | `Telegram.WebApp.openLink` | Book-now button (keeps Telegram open) |

When the backend deploys, two more bits to wire on the Python side:

1. The bot's deal alert message gets a fourth button row: `web_app: { url: "https://farehound.example.com/deal/{deal_id}" }`. That's what flips the `📊 Details` button placeholder we shipped in v0.9.0 from a Google Flights deep link to the Mini Web App.
2. HTTPS is mandatory for Telegram WebApps. On the Pi, easiest is **Cloudflare Tunnel** (no port forwarding, free for personal use). Caddy with a real domain works too but wants a static public IP.

## Files

```
frontend/
├── DESIGN.md       — design rationale + page sketches (read this first)
├── README.md       — this file
├── index.html      — preview index, lists the 3 screens
├── deal.html       — /deal/{id} — the most important page
├── routes.html     — /routes — list + snooze
├── settings.html   — /settings — prefs + transport
├── style.css       — single stylesheet, dark+light, Telegram-themed
└── app.js          — ~150 lines: Telegram bootstrap, sparkline, interactions
```

## Known gaps in this pass

- **No reduced-data alternatives view.** If a route only has primary-airport data (no nearby comparisons yet), the alternatives table just shows one row. Worth a "we haven't checked secondaries yet" placeholder in the next iteration.
- **No empty states.** First-time user with zero routes sees a blank `/routes`. Should get a "Tell me where you want to go" hero.
- **No history page.** A user might want to see "all deals on AMS→NRT in the last 6 months" as a list. Out of scope for this pass; the sparkline on `/deal` covers ~90% of the curiosity.
- **Hardcoded language strings.** Once multi-user lands, copy needs i18n. Not now.
- **No edit-dates flow.** The `Edit dates` link on `/routes` is a stub.

These belong in a follow-up roadmap item (probably a child of ITEM-049 once it's in flight).
