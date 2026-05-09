# [ITEM-051] Value-Prop Restoration & UX Overhaul

## Context

FareHound's stated mission: **"Making travel accessible by finding the lowest REAL cost to fly"** — total door-to-door cost (ticket + transport + parking + bags), with multi-airport comparison, filtered noise, and zero effort.

A user audit (May 2026) found the live experience falls short across all three pillars:

### 1. Value-prop delivery is inconsistent

| Message type | Cost breakdown | Baggage | Nearby alts | Why-best reasoning |
|---|---|---|---|---|
| Deal alert | ✓ | ✗ | ✓ (if savings ≥ €75) | partial |
| Error fare alert | ✗ price only | ✗ | ✗ | partial |
| Follow-up | ✗ minimal text | ✗ | ✗ | ✗ |
| Daily digest | ✓ | ✗ | ✓ (if savings ≥ €75) | ✗ |

- Baggage is never parsed from SerpAPI even though the data is in the response. The "real cost" promise is wrong by €40–€100/trip.
- Nearby airport comparison silently drops alternatives saving <€75 ([nearby_airports.py:42](src/analysis/nearby_airports.py:42)). User cannot tell *"we checked and yours is best"* from *"we never checked"*.
- No structured reasoning — scorer reasoning is 2–3 free-text sentences ([scorer.py:14–86](src/analysis/scorer.py:14)) with no "checked X dates × Y airports → this is cheapest by €Z".
- Error fare alerts ([telegram.py:288–327](src/alerts/telegram.py:288)) and follow-ups ([telegram.py:329–347](src/alerts/telegram.py:329)) skip the breakdown and nearby section entirely.

### 2. Action UX is broken

- No "Watching 👀" button on deal alerts ([telegram.py:280–284](src/alerts/telegram.py:280)) or daily digest ([telegram.py:471–477](src/alerts/telegram.py:471)). User can only "Book Now" or "Wait/Not interested". The "Watching" status only appears on the **3-day follow-up**.
- No route-level snooze. Marking a deal "Booked" only suppresses that one `deal_id` — new deals on the same route alert immediately.
- Inconsistent callback prefixes across the codebase: `book:`, `dismiss:`, `wait:`, `watching:`, `digest_booked:`, `digest_dismiss:`, `booked:` ([commands.py:674–836](src/bot/commands.py:674)).

### 3. Daily digest feels like spam

- Digest fires every day at 08:00 ([orchestrator.py:228–236](src/orchestrator.py:228)). Skips when no pending deals, but does **not** skip when prices haven't moved — user sees the same content repeated.
- "Watch"-level deals (score 0.50–0.74) silently land in the digest with no prior heads-up.
- No "we checked your routes today" trust signal — user can't tell the system is even working when nothing is alerted.

## Specification

One coherent release covering all three buckets. The fixes are tightly coupled — shipping piecemeal would leave the user with half-broken alerts (e.g. baggage in deal alerts but not in digest, or `Watching` on alerts but no snooze to back it up).

### Sub-item 1 — Unified cost-breakdown helper

**Files:** [src/alerts/telegram.py](src/alerts/telegram.py)

Extract the cost-breakdown logic (currently inline at lines 195–206 and 385–396) into one helper:

```python
def _format_cost_breakdown(
    price: float, transport: float, parking: float,
    mode: str, baggage: float, passengers: int,
) -> tuple[str, float]:  # (line, total)
```

Call from all four message types: `send_deal_alert`, `send_error_fare_alert`, `send_follow_up`, `send_daily_digest`.

### Sub-item 2 — Baggage parsing + display (subsumes ITEM-037)

**Files:** [src/apis/serpapi.py](src/apis/serpapi.py), [src/storage/models.py](src/storage/models.py), [src/storage/db.py](src/storage/db.py), [src/config.py](src/config.py), [src/alerts/telegram.py](src/alerts/telegram.py)

- Parse `booking_options[].extensions` and `together` blocks for `carry_on_bag` / `checked_bag` fees per direction.
- Fallback table for common airlines (KLM long-haul includes 1×checked, Transavia/Ryanair don't) when SerpAPI omits the field.
- New `PriceSnapshot.baggage_estimate` JSON field (`{outbound: {carry_on, checked}, return: {...}}`) + DB column + migration.
- New user preference `baggage_needs`: `carry_on_only | one_checked | two_checked` (default `one_checked`). Stored on `users` table.
- Cost breakdown becomes: `€480 flights + €80 bags + €30 transport + €0 parking = €590 total`.
- Scorer (sub-item 6) gets the bag-inclusive total instead of ticket-only.

### Sub-item 3 — "We checked X" transparency

**Files:** [src/orchestrator.py](src/orchestrator.py), [src/analysis/nearby_airports.py](src/analysis/nearby_airports.py), [src/alerts/telegram.py](src/alerts/telegram.py)

Replace silent omission. `nearby_airports.compare_airports()` returns **two lists**: `competitive` (savings ≥ €75) and `evaluated` (everything checked, with computed totals).

Telegram footer rules:
- All alts saved enough → existing "Nearby alternatives" block (unchanged).
- None saved enough → `✓ Checked EIN, BRU, DUS — your airport is best by €40–€60`.
- Mixed → existing block + footer `…also checked DUS (€60 more, skipped)`.

Same transparency for dates: `✓ Polled Mar 8 / 12 / 15 / 22 — Mar 12 is cheapest`.

### Sub-item 4 — "Watching 👀" button on alerts and digest

**Files:** [src/alerts/telegram.py](src/alerts/telegram.py), [src/bot/commands.py](src/bot/commands.py)

Add a third button to deal alerts and digest:

```
[Book Now ✈️]  [Watching 👀]  [Skip route 🔕]
```

- `Watching` callback marks `deal.feedback='watching'`, **stops follow-up nags** for that deal but keeps the route polling.
- `Skip route` callback snoozes the entire route 7d (sub-item 5) and dismisses all pending deals on it.

### Sub-item 5 — Per-route snooze

**Files:** [src/storage/db.py](src/storage/db.py), [src/storage/models.py](src/storage/models.py), [src/orchestrator.py](src/orchestrator.py), [src/bot/commands.py](src/bot/commands.py)

- Add `routes.snoozed_until TIMESTAMP NULL` (use the existing `_run_migrations` pattern).
- Orchestrator `poll_routes()` and `send_daily_digest()` skip routes where `snoozed_until > now()`.
- Bot commands: `/snooze <route> <days>`, `/unsnooze <route>`.
- Auto-snooze 30 days when a deal is marked `booked` (the trip is set; stop polling).

### Sub-item 6 — Structured scorer reasoning

**File:** [src/analysis/scorer.py](src/analysis/scorer.py)

Rewrite the system prompt (lines 14–47) so `reasoning` always returns three structured lines:

```
✓ Cheapest of {N} dates polled (Mar 12 saves €{X}/pp vs others)
✓ {Vs Google range OR vs your 90-day average}
✓ {Vs nearby OR "yours is best by €{Y}"}
```

Inject the orchestrator's already-computed comparison data into the scorer prompt and constrain the output format. Use `response_format={"type": "json_object"}` so we get structured fields back, then render them.

### Sub-item 7 — "📊 Details" button placeholder

**File:** [src/alerts/telegram.py](src/alerts/telegram.py)

Add a fourth button row pointing to a placeholder URL (Google Flights deep link for now). In [ITEM-049] this becomes the Mini Web App entry point. Adding the button now means [ITEM-049] only needs to swap the URL — the message layout doesn't change again.

### Sub-item 8 — Smarter daily digest

**Files:** [src/orchestrator.py](src/orchestrator.py), [src/storage/db.py](src/storage/db.py)

Add `users.last_digest_fingerprint TEXT` (a hash of `{route_id: lowest_price}` per user).

`send_daily_digest()` skips entirely when ALL of these are true:
- No new deals since last digest
- No price moved more than €10 on any route
- Less than 3 days since last digest sent

When skipped, log it. The scheduler still runs daily; it just doesn't message.

When NOT skipped, the digest header changes from `"You haven't decided on these yet"` to a concrete summary:

```
📊 FareHound Daily — 3 routes, 2 prices moved
• AMS→NRT dropped €40 (€1820/pp)
• AMS→BKK new low (€620/pp)
• AMS→LIS unchanged
```

### Sub-item 9 — Callback prefix consolidation

**File:** [src/bot/commands.py](src/bot/commands.py)

Standardize on `deal:{action}:{deal_id}` and `route:{action}:{route_id}`. Keep legacy prefixes as aliases so in-flight messages still work. New prefixes:

- `deal:book` (deep link, not callback)
- `deal:watch`
- `deal:dismiss`
- `route:snooze:{days}`
- `route:unsnooze`

### Sub-item 10 — `/status` command

**File:** [src/bot/commands.py](src/bot/commands.py)

```
🐕 FareHound status
• Monitoring 3 routes (1 snoozed)
• Last poll: 2h ago (next in 22h)
• Alerts this week: 5 (1 booked, 2 watching, 2 dismissed)
• SerpAPI: 247/1000 calls used this month
• Saved you €840 across 2 trips (/savings for detail)
```

Wires together `/savings`, route list, and SerpAPI usage tracker (already exists).

## Acceptance Criteria

- [ ] All 4 message types include cost breakdown and baggage line when data available
- [ ] Watching button on deal alerts AND digest, not just follow-up
- [ ] Skip route button snoozes route 7d and dismisses pending deals
- [ ] Per-route snooze respected in `poll_routes()` and `send_daily_digest()`
- [ ] Auto-snooze fires on `booked` feedback (30 days)
- [ ] `/snooze` and `/unsnooze` commands work
- [ ] `we checked X airports` line appears whenever a comparison ran, regardless of savings threshold
- [ ] Scorer reasoning returns structured JSON with 3 bullet fields, rendered as bullet list in alerts
- [ ] Daily digest skipped when no route price moved >€10 since last digest AND <3 days since last sent
- [ ] Daily digest header shows concrete "what moved" summary when not skipped
- [ ] `/status` command works and shows: routes (with snooze count), last poll, alerts this week with feedback breakdown, SerpAPI usage, savings link
- [ ] Callback prefix consolidation: new `deal:*` / `route:*` handlers added; legacy prefixes still handled (aliases)
- [ ] Tests added/updated:
  - [ ] `tests/test_telegram.py` — each of 4 message types includes breakdown + baggage line + we-checked footer + 3 buttons
  - [ ] `tests/test_serpapi_baggage.py` (new) — parse the 17 cached responses, assert at least one has `baggage_estimate.outbound.checked > 0`
  - [ ] `tests/test_db.py` — migration roundtrip for `routes.snoozed_until`, `users.last_digest_fingerprint`, `users.baggage_needs`, `price_snapshots.baggage_estimate`
  - [ ] `tests/test_orchestrator.py` — digest skip logic when fingerprint unchanged; snooze respected in poll loop; auto-snooze on booked
  - [ ] `tests/test_scorer.py` — reasoning JSON has 3 structured lines
- [ ] All existing tests still pass
- [ ] `farehound/src/` synced and version bumped per [CLAUDE.md](CLAUDE.md)
- [ ] Deployed to HA via `sudo docker exec hassio_cli ha apps update 30bba4a3_farehound`
- [ ] Post-deploy logs show clean migrations and successful poll cycle

## Out of Scope

- Cloud migration to Railway/Postgres ([ARCHITECTURE.md](ARCHITECTURE.md) Phase A)
- Multi-user expansion beyond what already exists
- Telegram Mini Web App ([ITEM-049])
- Discovery scanning ([ITEM-038])
- Full custom web dashboard ([ITEM-050])

## Open Questions

- **Baggage fallback table location:** new module `src/utils/baggage.py` or extend `src/utils/airlines.py`? Lean toward `airlines.py` since baggage policy is airline-keyed.
- **`Skip route` semantics:** does it snooze the route only, or also un-watch any related routes (e.g. route to a city served by multiple airports)? Default to snoozing the single route only — user can re-snooze siblings if needed.
- **Daily digest skip telemetry:** log only, or surface in `/status` ("digest skipped 3 days this week — no price moves")? Lean toward `/status`-only when it's not noisy.

## Reuse, Don't Rebuild

- `transport_total()` in [nearby_airports.py:25](src/analysis/nearby_airports.py:25)
- `find_cheapest_date()` in [telegram.py:13](src/alerts/telegram.py:13)
- `_deal_emoji()` / `_deal_label()` in [telegram.py:67–90](src/alerts/telegram.py:67)
- DB migration pattern already used in `db.py` for previous schema changes
- `SERPAPI_CACHE_DIR` for offline test runs (17 cached responses)

## Reference

- Original audit + plan: `~/.claude/plans/i-s-been-a-while-cozy-mochi.md` (auto-saved during the planning session)
- [TECHNICAL.md](TECHNICAL.md) — current architecture overview
- [ARCHITECTURE.md](ARCHITECTURE.md) — multi-user evolution analysis (out of scope here)
