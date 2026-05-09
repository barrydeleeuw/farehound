# R7 Build Log

## T1 db_migrations
- Added 5 idempotent ALTER blocks to `src/storage/db.py:init_schema` after the existing approved-column migration, before `_migrate_default_user()`.
- Columns added: `routes.snoozed_until`, `users.baggage_needs DEFAULT 'one_checked'`, `users.last_digest_fingerprint`, `users.last_digest_sent_at`, `users.digest_skip_count_7d DEFAULT 0`, `price_snapshots.baggage_estimate`, `deals.reasoning_json`.
- All ALTERs guarded by `_has_column` per existing pattern.
- Verified idempotent re-run (calling `init_schema()` twice) and confirmed all 7 new columns present.
- Tests: `pytest tests/test_db.py` 54/54 passing; Tester writes T16 against this.

## T2 models_update
- Added `Route.snoozed_until: datetime | None`, `PriceSnapshot.baggage_estimate: dict | None`, `Deal.reasoning_json: dict | None`.
- All three deserialized via `_parse_datetime` / `_parse_json` in `from_row`; included in `to_dict` outputs.
- Updated `tests/test_models.py:54` (test_route_to_dict_roundtrip key set) to include `snoozed_until`.
- Tests: full suite 311/311 passing.

## T4 cost_breakdown_helper
- New helpers `_format_cost_breakdown(price, transport, parking, mode, baggage, passengers) -> (str, float)` and `_baggage_total(baggage, passengers)` in `src/alerts/telegram.py`.
- Replaced 4 inline duplications: primary breakdown in `send_deal_alert`, primary in `send_daily_digest`, nearby alt in `send_deal_alert`, nearby alt in `send_daily_digest`.
- Baggage handled defensively per Condition C5: zero suppressed, `source == 'unknown'` suppressed, missing fields treated as 0.
- Output identical to current behaviour when `baggage_estimate` absent (existing snapshots have no baggage). Matches `calculate_net_cost` formula in `nearby_airports.py:35` for the alt path.
- Tests: full suite 311/311 passing; T14 (Tester) will assert per-message-type baggage rendering.

## T5 nearby_airports_two_lists
- `compare_airports(...)` in `src/analysis/nearby_airports.py:38` now returns `{"competitive": [...], "evaluated": [...]}` per §9.1.
- `evaluated` always includes every secondary that was costed (regardless of threshold) with new `delta_vs_primary` (signed: positive = more expensive).
- `competitive` keeps the existing >€75 filter and is sorted desc.
- Updated `src/bot/commands.py:1754` (immediate price check) caller to use `["competitive"]`.
- Updated all assertions in `tests/test_nearby_airports.py` to use `result["competitive"]` / `result["evaluated"]`; added negative-delta assertion in `test_compare_airports_excludes_below_threshold`.
- Tests: 17/17 nearby + full suite 325/325 passing.

## T6 transparency_data_assembly
- `_latest_nearby_comparison: dict[str, dict]` (was `dict[str, list]`).
- Both writer call sites updated (`_poll_secondary_airports` ~ orchestrator:799, `_poll_secondary_airports_for_snapshot` ~ orchestrator:917).
- **Per Condition C6, never `pop()` on empty `evaluated`.** Entry is preserved when secondaries were polled even if `competitive` is empty — so renderer can show "we checked X, yours is best". Only popped when no secondaries were polled at all.
- Savings logging now iterates `comparison["competitive"]` only (correct behaviour — non-competitive alts shouldn't generate savings rows).
- Readers updated:
  - Scorer call (`orchestrator:986`) passes `["competitive"]` (existing list-shape API).
  - `deal_info` (`orchestrator:1136`) populates both `nearby_comparison` (competitive list, back-compat for telegram.py) AND new `nearby_evaluated` (full list, for T7's transparency footer).
  - Digest summary (`orchestrator:1261`) populates `nearby_prices` (competitive) and `nearby_evaluated` similarly.
- Tests: full suite 325/325 passing.

## T13 callback_prefix_consolidation
- `_handle_callback` (`src/bot/commands.py:687`) now first attempts `data.split(":", 2)` and dispatches `deal:*` / `route:*` via new `_handle_new_callback` helper. Falls back to legacy `split(":", 1)` for all other callbacks (`approve_user`, `confirm_airports`, `confirm_route`, `digest_booked`, `digest_dismiss`, etc.) — Condition C2.
- New domain handlers:
  - `deal:book:{deal_id}` → `feedback='booked'` + `booked=1` + auto-snooze 30d.
  - `deal:watch:{deal_id}` → `feedback='watching'`.
  - `deal:dismiss:{deal_id}` → `feedback='dismissed'`.
  - `route:snooze:{days}:{route_id}` → calls `db.snooze_route` (T9 helper) AND bulk-dismisses pending deals on that route.
  - `route:unsnooze:{route_id}` → calls `db.unsnooze_route`.
  - `route:dismiss:{route_id}:{user_id}` → bulk-dismiss (mirrors legacy `digest_dismiss`).
- Auto-snooze helper `_auto_snooze_route_for_deal(deal_id, days=30)` looks up `deal.route_id` and snoozes — used by new `deal:book` AND legacy `book:` / `booked:` / `digest_booked:` paths (Condition C9).
- Defensive `getattr(self._db, 'snooze_route', None)` guards so this doesn't crash if T9 hasn't landed; T9 fills the helper.
- Confirmed callback flows for non-deal/non-route prefixes pass through unchanged.
- Tests: full suite 325/325 passing.

## T9 snooze_infra
- DB helpers in `src/storage/db.py`:
  - `snooze_route(route_id, days)` — sets `snoozed_until = now + days` (UTC ISO).
  - `unsnooze_route(route_id)` — clears `snoozed_until`.
  - `get_active_routes(user_id, include_snoozed=False)` — new kw filters routes whose `snoozed_until` is in the future. Default behaviour is to hide snoozed routes; pass `include_snoozed=True` for `/snooze` resolution and admin views.
  - `get_routes_with_pending_deals(user_id)` — JOINs routes and excludes snoozed (Condition C8).
- Orchestrator gets snooze enforcement for free: `poll_routes()` and `send_daily_digest()` already call `get_active_routes(user_id)` (orchestrator:392, :1177, :1308). No orchestrator-side change needed.
- Slash commands in `src/bot/commands.py`:
  - `/snooze {route|substring} [days]` — defaults to 7 days. `_resolve_route_for_user` matches by route_id, prefix, origin/dest IATA, or city name.
  - `/unsnooze {route|substring}` — symmetric.
- Auto-snooze hook (Condition C9) was already wired in T13's `_auto_snooze_route_for_deal` — it now resolves `db.snooze_route` successfully (was no-op before T9).
- Smoke test: snooze→get_active→empty; unsnooze→back to one.
- Tests: full suite 325/325 passing; Tester writes T17 against this.

## T10 digest_fingerprint_gating
- Per-user fingerprint (sha256 of sorted route_id→rounded-price pairs, truncated to 16 chars) computed in `Orchestrator._compute_digest_fingerprint` (§11.1).
- Skip predicate (§11.2 — all 4 must hold): same fingerprint AND no new deals AND biggest price move ≤ €10 AND <3 days since last digest.
- When skipping: increment `users.digest_skip_count_7d`, log `Digest skipped for user {} — fingerprint unchanged, last digest {}d ago`. Do NOT touch `last_digest_sent_at` (so a real change tomorrow re-evaluates).
- When NOT skipping: `_format_digest_header` produces the concrete header (§11.4) with one line per route ("dropped €40", "new low (€620/pp)", or "unchanged"). Stashed in `summaries[0]["digest_header_override"]`.
- `telegram.py:send_daily_digest` reads the override; falls back to legacy "FareHound Daily — N route(s)" when absent.
- Persists `users.last_digest_fingerprint` + `last_digest_sent_at` after successful send. Extended `db.update_user` allowlist with `baggage_needs`, `last_digest_fingerprint`, `last_digest_sent_at`, `digest_skip_count_7d`.
- Per-route delta uses `db.get_recent_snapshots(route_id, limit=2)` (no schema change, per Finding #7).
- Tests: full suite 325/325 passing; Tester's T17 (orchestrator digest skip / snooze / auto-snooze) is now fully unblocked — depends on T9+T10 both done.

## T12 scorer_json_contract
- `_SCORE_PROMPT` JSON template (`src/analysis/scorer.py`) now requires structured `reasoning: {vs_dates, vs_range, vs_nearby}` per §6.1.
- `DealScore.reasoning` typed as `dict` (was `str`). New module-level `reasoning_to_bullets(dict|str|None) -> str` flattens to `\n`-joined bullets.
- `_parse_response` validates urgency enum and runs `_coerce_reasoning` — accepts dict, falls back gracefully on string (legacy capture replays) or missing fields. Synthetic 3-field fallback in `_fallback_reasoning` per §6.5.
- Orchestrator stores both columns (Condition C7):
  - `Deal.reasoning_json = score_result.reasoning` (the dict).
  - `Deal.reasoning = reasoning_to_bullets(score_result.reasoning)` (`✓ {v}\n…` for legacy reads).
- `db.insert_deal` extended to write `reasoning_json` column.
- `_static_fallback` produces synthetic 3-field reasoning so the dataclass is always consistent.
- `Route.snoozed_until` parser now tags datetimes as UTC (new `_parse_datetime_utc` in models.py) so timezone-aware comparisons work.
- New `deal_info["reasoning_json"]` and updated `reasoning` (now bullet-string) on both alert paths and follow-up.
- Tests: full suite 340/340 passing (Tester added 15 tests covering snooze/auto-snooze/digest skip/header).


## Pre-staging
- Created `tests/fixtures/serpapi_with_baggage/` with 3 synthetic fixtures matching SerpAPI response shape per release_plan.md §8:
  - `full_baggage_both_ways.json` — baggage in BOTH `booking_options[].together.extensions` AND `best_flights[].flights[].extensions` (KLM AMS→NRT, "Checked baggage: 1st bag 40 €").
  - `outbound_only_baggage.json` — baggage only in flight-leg extensions on outbound flight; return leg has no baggage strings (Ryanair AMS→BKK, carry-on 25 €, checked 50 €).
  - `no_baggage_data.json` — zero baggage strings anywhere; parser must hit fallback table (Transavia AMS→LIS).
- These back T15 (parser unit tests) and T19 (integration). Architect-Lead's Finding #1 confirmed real cached fixtures lack baggage data — these are manufactured to exercise the parsing pipeline.
- Created `tests/test_integration_r7.py` skeleton with 3 skipped tests that will activate as Builder T7/T8/T17 land. Skeleton imports cleanly under pytest.

## T16 db_migrations roundtrip
- Added `TestR7Migrations` class to `tests/test_db.py` — 14 tests:
  - Column-existence checks for all 5 R7 ALTER blocks (A1–A5: routes.snoozed_until, users.baggage_needs, users.last_digest_fingerprint + last_digest_sent_at + digest_skip_count_7d, price_snapshots.baggage_estimate, deals.reasoning_json).
  - Idempotency: `init_schema()` called twice — column set unchanged.
  - Default-value checks: `users.baggage_needs DEFAULT 'one_checked'`, `users.digest_skip_count_7d DEFAULT 0`.
  - Round-trip writes for each new column via raw SQL (T9 helpers don't exist yet — T17 will exercise them).
  - JSON round-trip for `price_snapshots.baggage_estimate` and `deals.reasoning_json`.
  - NULL-tolerant assertion for pre-R7 rows.
- Tests: 325/325 passing (311 baseline + 14 new). No regressions.

## T17 orchestrator: snooze + digest fingerprint + auto-snooze
- Added 15 new tests to `tests/test_orchestrator.py`, organized into 3 classes + 4 standalone async tests, using a real `Database` fixture (`real_db`) for end-to-end behaviour:
  - `TestSnoozeFiltering` (4 tests): `get_active_routes` excludes snoozed routes by default; `include_snoozed=True` overrides; `unsnooze_route` re-includes; expired snooze (past timestamp) treated as active.
  - `TestAutoSnoozeOnBooked` (3 tests): `TripBot._auto_snooze_route_for_deal` sets `snoozed_until` ~30d in future; route disappears from default `get_active_routes`; silent on missing deal_id.
  - `TestDigestFingerprintHelpers` (4 tests): `_compute_digest_fingerprint` is order-independent (sorted by route_id), changes when price changes, rounds to whole euro (sub-€1 movement is invisible); `_format_digest_header` produces concrete "N routes, M prices moved" line plus per-route "dropped €X / new low / unchanged" bullets.
  - 4 standalone async tests covering full `send_daily_digest` flow:
    - Skip predicate fires when fingerprint matches AND <3d AND price moved <€10 AND no new deals → notifier NOT called, `digest_skip_count_7d` incremented.
    - Digest sent when fingerprint changes (€50 price move) → notifier called once, new fingerprint persisted.
    - Digest sent regardless of fingerprint when last digest >3d ago.
    - Snoozed route excluded from per-user digest summary entirely.
- Test approach: real Database, real Orchestrator (with `_make_orchestrator_with_mocks`'s mock-DB swapped for the real one), AsyncMock telegram_notifier. Mocks only the HTTP boundary, not the helpers under test.
- Notes: caught two NameErrors in Builder's in-flight T12 working-tree (`reasoning_dict`, `deals_since_last_digest`) — flagged via DM, fixed before commit.
- Tests: 340/340 passing (325 prior + 15 new). No regressions.
- Code landed in commit b663d67 (bundled with Builder's T12 — ack from Builder; future Tester commits will be standalone).

## T18 scorer: structured 3-field reasoning contract
- Added 11 new tests to `tests/test_scorer.py` covering §6.1 + §6.5:
  - `_parse_response` returns 3-field reasoning dict for valid Claude response.
  - Malformed JSON → `_fallback_reasoning` produces synthetic 3-field dict (`Static fallback — Claude unavailable`).
  - Missing `vs_nearby` sub-field is replaced with documented placeholder `"No nearby airports configured"` per `_coerce_reasoning`.
  - Legacy free-text string reasoning is gracefully coerced into a 3-field dict (older response replays don't break).
  - Invalid urgency value (not in enum `book_now|watch|skip`) coerces to `"watch"`.
  - `reasoning_to_bullets` renderer: 3-field dict → 3 lines prefixed with `✓ `; string passes through; None → empty; empty fields skipped.
  - End-to-end `score_deal` (mocked Anthropic): structured response → `DealScore.reasoning` is dict; malformed response → conservative defaults `(score=0.3, urgency='watch', reasoning=fallback_dict)`.
- All 14 pre-existing scorer tests still pass thanks to back-compat coercion in `_coerce_reasoning`.
- Tests: 351/351 passing (340 prior + 11 new). No regressions.

## T3 serpapi_baggage_parsing
- New module `src/utils/baggage.py`:
  - `FALLBACK` table keyed by IATA airline code (KL, AF, LH, BA, HV, FR, U2, W6, _DEFAULT) with `carry_on` / `checked_long_haul` / `checked_short_haul` per direction (§8.3).
  - `LONG_HAUL_KM = 4000` cutoff.
  - `parse_baggage_extensions(extensions)` — defensive scan via two regexes, picks max amounts when multiple matches. Returns None when nothing recognisable; never raises (Condition C4).
  - `estimate(airline_code, leg_distance_km, baggage_needs)` — honors user preference matrix (`carry_on_only` / `one_checked` / `two_checked`). Always succeeds.
- `FlightSearchResult.parse_baggage(...)` in `src/apis/serpapi.py` (§8.2 pipeline):
  1. Primary scan: `booking_options[].together.extensions`.
  2. Secondary scan: `flights[].extensions` (outbound + return legs separately).
  3. Fallback: `baggage.estimate(...)`.
  4. Mark `source = "unknown"` when both primary path AND fallback yield zero — renderer suppresses the line per Condition C5.
- Orchestrator wiring (`src/orchestrator.py`):
  - New `_compute_baggage_for_result(result, best_flight, user)` — derives `airline_code` from `best_flight.flights[0].airline`, distance from `total_duration` (~800 km/h proxy), `baggage_needs` from `user`.
  - Primary snapshot construction (orchestrator:549) and secondary on-demand snapshot (orchestrator:888) both store `baggage_estimate`.
  - `secondary_results.append(...)` includes `baggage_estimate` so it propagates through `compare_airports` into `nearby_comparison[i]["baggage_estimate"]`, exposing baggage on alt cost breakdown lines.
  - `deal_info` and digest `summary` now expose `baggage_estimate` so `telegram._format_cost_breakdown` reads it.
- `db.insert_snapshot` extended to write `baggage_estimate` column.
- `nearby_airports.compare_airports` propagates `baggage_estimate` from input to output `entry`.
- Smoke tests pass parser against SerpAPI shapes (booking_options, flight extensions, dollar format, malformed input).
- Tests: full suite 351/351 passing; Tester's T15 unblocked.

## T15 tests: serpapi_baggage parser + airline fallback table
- New `tests/test_serpapi_baggage.py` — 31 tests organized into 3 classes:
  - `TestParseBaggageExtensions` (10 tests): EUR/USD format, prefix vs suffix, multiple-line lists, negative cases (legroom/wifi don't match), defensive (None/non-list/non-string items), refuses bare numeric phrases like "1st bag" or "2 bags allowed".
  - `TestEstimateFallback` (14 tests): per-airline lookups (KL long/short-haul, FR LCC, HV, unknown→`_DEFAULT`), case-insensitive code, None airline → default; user-preference matrix (`carry_on_only`/`one_checked`/`two_checked`), default to one_checked when None; long-haul threshold at 4000km exact boundary; unknown distance treated as short-haul; never raises on garbage `leg_distance_km`.
  - `TestFlightSearchResultParseBaggage` (7 tests): the 3 synthetic fixtures from pre-staging exercise the §8.2 pipeline:
    - `full_baggage_both_ways.json` → `source='serpapi'`, both directions €40 checked.
    - `outbound_only_baggage.json` → `source='serpapi'`, outbound parsed, return falls back to outbound (per Builder's `ret_parsed or out_parsed` rule).
    - `no_baggage_data.json` × HV short-haul → `source='fallback_table'` with non-zero fees.
    - `no_baggage_data.json` × KL long-haul → `source='unknown'` (zero fallback fees → suppressed per Condition C5).
    - Empty `FlightSearchResult` doesn't raise; malformed `booking_options` items silently skipped; result dict shape matches §8.1 (`{outbound, return, source, currency}`).
- Plan §12 acceptance for "25 cached responses smoke test" not exercised here — those fixtures don't exist in the worktree (only synthetic fixtures Tester created); covered by `test_empty_result_does_not_raise` and the malformed-input cases instead. Defensive guarantee (Condition C4: never raises) is verified.
- Tests: 31/31 baggage tests passing. The 2 telegram-button test failures observed in the full suite are from Builder's in-flight T7 (telegram unified) — not from T15. Will resolve when T7 commits.

## T7 telegram_4_messages_unified
- New telegram-side helpers in `src/alerts/telegram.py`:
  - `_render_reasoning_bullets(reasoning_json, reasoning_legacy)` — 3-bullet structured reasoning per §6.3; gracefully falls back to legacy single-line italic when only the string column is present.
  - `_render_transparency_footer(competitive, evaluated)` — §9.2 cases: all-saved (no footer), none-saved ("Checked N airports — yours best by €X–€Y"), mixed ("…also checked N (€X+ more, skipped)"), or both empty (None).
  - `_render_date_transparency(price_history)` — §9.3: "Polled N dates — Mar 12 is cheapest".
  - `_build_deal_keyboard(deal_id, route_id, search_url, deal_info)` — 3-button row + Details row used by both deal alerts and error fares.
- All 4 message types now share the `_format_cost_breakdown` helper (T4) AND the new helpers above:
  - `send_deal_alert`: cost line, 3 reasoning bullets, date transparency, nearby block, "we checked X" footer, R7 keyboard with Details (sub-item 7) — Condition C10 satisfied (Google Flights deep link).
  - `send_error_fare_alert`: cost line + breakdown, 3 reasoning bullets, R7 keyboard.
  - `send_follow_up`: cost line + breakdown, switched callbacks to new prefixes.
  - `send_daily_digest`: per-route 3-button row + Details, transparency footer, date transparency.
- Updated 3 existing v0.7/v0.8/v2.1 release tests to assert the new R7 keyboard shape (test_send_deal_alert_buttons, test_send_error_fare_alert_buttons, test_send_follow_up, test_digest_route_has_book_and_action_buttons, test_digest_route_without_deals_has_no_action_buttons, test_send_follow_up_format).
- Tests: full suite 382/382 passing; Tester's T14 (per-message-type assertions) unblocked.

## T8 watching_skip_buttons
- Keyboard rendering already landed in T7 via `_build_deal_keyboard`; handlers already landed in T13 via `_handle_new_callback`.
  - Deal alert + Daily digest both render `[Book Now] [Watching 👀] [Skip route 🔕]` on row 1, `[📊 Details]` on row 2.
  - `route:snooze:7:{route_id}` callback snoozes the route AND bulk-dismisses pending deals on that route (Condition C9-style symmetry).
- No additional code needed for T8 acceptance — completed by T7 + T13.
- Tests: full suite 382/382 passing.

## T11 status_command
- New `db.get_status_stats(user_id)` aggregates monitoring count + snoozed, last poll, alerts this week with feedback breakdown, snapshots in last 30d (SerpAPI usage proxy), savings, digest skip count.
- New `TripBot._handle_status` handler renders the §1.4.3 format. Sample output:
  ```
  📊 FareHound Status
  • Monitoring: 1 route (1 snoozed)
  • Last poll: 6h ago
  • Alerts this week: 3 (2 booked, 1 no-response)
  • SerpAPI usage: 184/950 (last 30d)
  • Saved you €450 across 2 trips (/savings)
  • Digest skipped 3 of last 7 days (no significant price moves)
  ```
- `/status` slash command wired into the message dispatcher.
- Smoke-tested: 2 active routes, 1 snoozed → stats correctly show 1 monitoring + 1 snoozed.
- Tests: full suite 382/382 passing.
