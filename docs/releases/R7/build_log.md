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
