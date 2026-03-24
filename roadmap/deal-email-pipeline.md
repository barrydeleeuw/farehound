# [ITEM-006] Deal Email Pipeline (Jack's Flight Club + Secret Flying)

## Context

FareHound currently discovers deals via SerpAPI polling and RSS community feeds. The highest-value deals — error fares, flash sales, and curated cheap flights — arrive via email from Jack's Flight Club (JFC) and Secret Flying (SF). These are time-sensitive (hours, not days) and represent the biggest savings opportunities. Ingesting them would let FareHound alert users about deals it could never find through periodic polling alone.

## Email Sources

### Jack's Flight Club
- **Sender:** `jack@content.jacksflightclub.com`
- **Reply-to:** `jack@jacksflightclub.com`
- **Frequency:** 3-5 emails/day
- **Subscription:** Barry's personal Gmail (`bdeleeuw1987@gmail.com`) — forward to FareHound inbox, or use Gmail API directly

### Secret Flying
- **Sender:** `my@deals.secretflying.com`
- **Reply-to:** `no-reply@deals.secretflying.com`
- **Frequency:** 10-20 emails/day (higher volume, more noise)
- **Subscription:** Same Gmail, subscribed to BRU/AMS/nearby airports

## Email Format Analysis (from real examples)

### JFC Format

**Subject line pattern:**
```
[*tags*] Destination in €Xs-€Ys return [in months] [(airlines)]
```

Subject line tags indicate special attributes:
- `*non-stop*` — direct flights only
- `*prem eco*` — premium economy cabin
- `*free bag*` — checked bag included
- No tag — standard economy with layovers

**Examples:**
- `*non-stop* Canary Islands in €120s-€190s return`
- `*prem eco* Punta Cana in €1120s-€1290s return in May-June and August-February (Lufthansa, Condor)`
- `Beijing in €540s-€590s return in June & September-January (SkyTeam & Star Alliance)`
- `*free bag* Dar es Salaam in €580s-€660s return in April-November (Turkish Airlines)`

**Body structure — "Taking off" section (structured data):**

Two layout variants observed:

**Variant A — Single destination, multiple origins:**
```
To
Punta Cana (PUJ)

From
Amsterdam (AMS) - €1190
Brussels (BRU) - €1120 (xmas)
Dusseldorf (DUS) - €1188
Nuremberg (NUE) - €1146 (some routes have a bus to Munich included)
```

**Variant B — Multiple destinations, multiple origins:**
```
Return trips to Fuerteventura (FUE):
Hanover (HAJ) - €145 (cabin bag, up to €160)

Return trips to Gran Canaria (LPA):
Amsterdam (AMS) - €134 (up to €164)

Return trips to Tenerife (TFS):
Amsterdam (AMS) - €127 (up to €154)
Dusseldorf (DUS) - €180 (up to €185)
```

**Price format:**
- Base price: `€559`, `€1190`
- With range: `€134 (up to €164)`, `€585 (up to €633)`
- With annotation: `€1120 (xmas)`, `€145 (cabin bag, up to €160)`
- Always **per person, return/roundtrip**

**Details sidebar:**
```
Travel Dates: April-June & November-February (varies by route, excl. peak dates)
Airline: Condor, Transavia, Corendon & easyJet
Standard fare: €200 - €250 (non-stop)
Bags & fees: Cabin bag starts at €30 rtn...
Likely to last: At least a few days, some may go sooner.
```

**Per-route date restrictions (sometimes present):**
```
Month coverage for each route:
- From Brussels: September-October
- From Stockholm: April-May, August-November
```

**IATA codes:** Always present in parentheses next to city names — both origins and destinations.

### Secret Flying Format

**Subject line pattern:**
```
Origin, Country to Destination for only €X roundtrip
```

**Example:**
- `Brussels, Belgium to Hong Kong for only €489 roundtrip`

**Body structure — labeled fields (red headers):**
```
DEPART:
Brussels, Belgium

ARRIVE:
Hong Kong

RETURN:
Brussels, Belgium

DATES:
Limited availability from November to December 2026
Example dates:
18th Nov – 1st Dec
22nd Nov – 5th Dec
25th Nov – 8th Dec
...possibly more…

STOPS:
Shanghai

AIRLINES:
Juneyao Airlines
```

**Key differences from JFC:**
- Single origin, single destination per email (always)
- City names without IATA codes — must resolve to IATA (Brussels → BRU, Hong Kong → HKG)
- Specific example date pairs (outbound – return), not just month ranges
- Price is a single fixed amount, not a range
- Layover city named in STOPS (not just "1 stop")
- "GO TO DEAL" link goes to a booking page (not Google Flights)

## Parsed Deal Schema

Every email (JFC or SF) produces one or more `EmailDeal` records:

```python
@dataclass
class EmailDeal:
    source: str                    # "jfc" | "secretflying"
    destination_city: str          # "Hong Kong", "Punta Cana"
    destination_iata: str          # "HKG", "PUJ"
    origin_city: str               # "Amsterdam", "Brussels"
    origin_iata: str               # "AMS", "BRU"
    price_eur: float               # lowest price per person, return
    price_high_eur: float | None   # upper range (JFC "up to €X"), None for SF
    standard_fare_eur: float | None # JFC "standard fare" for context
    travel_months: list[str]       # ["April", "May", "June", "November", ...]
    example_dates: list[tuple[str, str]]  # [("18th Nov", "1st Dec"), ...] — SF specific
    airlines: list[str]            # ["Condor", "Transavia"]
    stops: str | None              # "non-stop", "Shanghai", "1 stop"
    cabin_class: str               # "economy", "premium_economy"
    bags_included: bool            # True if *free bag* or checked bag mentioned
    deal_url: str | None           # "GO TO DEAL" link (SF) or "Find This Fare" (JFC)
    raw_subject: str               # original subject line
    received_at: datetime          # email timestamp
    email_id: str                  # Gmail message ID for dedup
```

A single JFC email can produce **many** EmailDeal records (one per origin-destination pair). The Canary Islands email produces 8 deals (4 destinations x varying origins).

## Pipeline Architecture

### Phase 1: Email Ingestion

**TBD — decide during build.** Two candidate approaches:

**Option A — Gmail API polling:**
- Poll Gmail API every 5 minutes for new emails from JFC/SF senders
- Use `users.messages.list` with `from:` filter, track `historyId`
- Runs inside the existing FareHound asyncio loop
- Risk: OAuth refresh tokens can silently expire (especially in "testing" mode)

**Option B — Google Apps Script + buffer:**
- Apps Script triggers on new email, extracts body/subject/sender
- Writes to a Google Sheet (or other buffer) that FareHound polls
- More reliable auth (no OAuth token management), real-time trigger
- Risk: adds a middleman (Sheet) and a second system to maintain

**What doesn't change:** Everything downstream of ingestion (parsing, matching, notification) is identical regardless of how emails arrive. The parser receives a subject + body + sender + timestamp.

### Phase 2: Parsing

**Approach: Claude-based extraction (not regex)**

These emails have enough variation (annotations, per-route date restrictions, multi-destination layouts) that regex parsing would be brittle. Use Claude to extract structured data from the email body.

**Parser prompt template:**
```
Extract flight deals from this email. Return JSON array of deals.
For each origin-destination pair, extract:
- origin_city, origin_iata
- destination_city, destination_iata
- price_eur (lowest), price_high_eur (if "up to" present)
- travel_months, airlines, stops, cabin_class, bags_included

Email source: {jfc|secretflying}
Subject: {subject}
Body: {body_text}
```

Use Haiku for cost efficiency — these are structured extractions, not creative tasks.

**IATA resolution:** JFC emails include IATA codes inline. SF emails use city names only — maintain a city→IATA lookup table (start with existing `airports.yaml` cities + common destinations). Fall back to Claude if lookup misses.

### Phase 3: Matching

For each parsed `EmailDeal`, check against all users' configured routes:

```python
def matches_user_route(deal: EmailDeal, route: DBRoute, user_airports: list[str]) -> bool:
    # Destination match: deal destination matches route destination
    dest_match = deal.destination_iata == route.destination

    # Origin match: deal origin is user's primary OR any secondary airport
    origin_match = deal.origin_iata in user_airports

    # Date overlap: deal travel months overlap with route's date window
    date_match = has_date_overlap(deal.travel_months, route.earliest_departure, route.latest_return)

    return dest_match and origin_match and date_match
```

**Nearby airport matching is critical:** If a JFC email lists DUS at €180 and the user flies from AMS, but DUS is in their secondary airports, this should still match — and FareHound can calculate the door-to-door cost including transport to DUS.

### Phase 4: Notification

On match, send Telegram alert with deal-specific formatting:

```
✈️ Deal Alert — Amsterdam → Tenerife
💰 €127/pp return (normally €200-€250)
📅 April-June & November-February
✈️ Condor, Transavia (non-stop)
🧳 Cabin bag from €30 rtn

Source: Jack's Flight Club
⏳ Likely to last a few days

[Find This Fare]
```

For nearby airport matches, include transport cost:
```
Also available from Dusseldorf (DUS) at €180/pp
  + €112 car + €120 parking = €592 total vs €307 from AMS
```

## Acceptance Criteria

- [ ] Gmail API client authenticates and polls for new emails from JFC and SF senders
- [ ] JFC parser handles both single-destination and multi-destination email formats
- [ ] JFC parser extracts: destinations (with IATA), origins (with IATA), prices (low + high), travel months, airlines, stops, cabin class, baggage tags
- [ ] SF parser extracts: origin city→IATA, destination city→IATA, price, example dates, stops, airline
- [ ] Parsed deals matched against all users' routes (destination + origin/nearby + date overlap)
- [ ] Telegram notification sent on match with deal details, source attribution, and booking link
- [ ] Deduplication: same email_id never processed twice; same route+price+date range not re-notified within 24h
- [ ] Deals stored in `email_deals` table for history and debugging
- [ ] Parsing errors logged but don't crash the pipeline — skip malformed emails gracefully

## Out of Scope (v1)

- Gmail forwarding / webhook (use polling)
- Airline promo email parsing (ITEM-019 — different beast)
- Automatic trip creation from deals (just notify on existing routes)
- Price verification via SerpAPI for email deals (trust the source for v1)

## Open Questions

- **Ingestion method:** Gmail API polling vs Google Apps Script + buffer. Decide during build based on what's simplest to get working reliably on the Pi. See Phase 1 above.
