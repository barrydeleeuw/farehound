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
