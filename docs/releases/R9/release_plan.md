# R9 — Real Door-to-Door Cost (ITEM-053)

## Theme

Make the deal page show **real** door-to-door cost without making the user fill in airport transport by hand. Today, `airport_transport` is empty for most users → transport renders at €0 in the breakdown → the hero `total /pp` lies. ITEM-053 closes the gap by auto-discovering nearby airports, auto-filling multiple modes per airport via Google Maps Distance Matrix, picking the cheapest at render time, and giving the user editable preferences as the source of truth.

## Scope

**One item:** [ITEM-053] Auto-discover & enrich nearby airports (Google Maps + multi-mode + cheapest-mode selection). Subsumes the previously-parked ITEM-045 (manual conversational onboarding) and ITEM-004 (Google Maps cache-by-city).

**Build mode:** Solo (Barry's choice). `/code-review` runs at end as independent perspective.

## Locked decisions

1. **API: Google Maps Distance Matrix** — $200/mo free credit (40k free element-pairs/mo); ITEM-053 burn is ~6 elements per onboarded user. Effectively $0 forever.
2. **Train fares: option (b) — curated `data/train_fares_eu.json`.** No NS API integration. User confirms the curated estimate at onboarding via inline `[Yes / Adjust]` button.
3. **Settings page is the source of truth post-onboarding.** Every value editable per-airport per-mode. User can ADD modes ("ride from family — €0"), DISABLE modes ("never take taxis to AMS"), set per-airport "always use [mode]" override.
4. **Build path: graceful-skip-when-key-missing first.** Onboarding falls back to ITEM-045 conversational flow if `google_maps_api_key` is unset. Barry provisions key in parallel; auto-fill activates on next onboarding.
5. **No NS API. No cloud migration. No multi-user expansion.**

## Pre-build architectural context (Phase 3 inline)

**Trigger met:** ≥3 modules touched (storage, web, apis, bot, analysis), new external dependency (Google Maps), new architectural primitive (multi-mode-per-airport schema).

**Verdict: Proceed with conditions.**

### Conditions (non-negotiable)

1. **Schema migration must preserve existing `airport_transport` rows.** Read forward into `airport_transport_option` at first boot as `source='user_override', confidence='high', enabled=1`. Old table preserved as compat shim for one release before drop in R10. Tested with a roundtrip migration test.

2. **Cheapest-mode selection is a pure function in `src/analysis/transport.py`.** No DB calls, no I/O. Takes options list + passengers + trip_days, returns chosen option. Unit-tested with explicit fixtures (1 pax / 4 pax / 1 day / 14 days / parking-included / parking-excluded).

3. **Render path must be single-write.** Every place that currently calls `db.get_airport_transport(code)` (3 sites: `src/web/data.py:299`, `src/alerts/telegram.py` cost-breakdown helper, `src/analysis/nearby_airports.py:transport_total`) must switch to `db.get_transport_options() + pick_cheapest_mode()`. No half-migration where some surfaces use new schema and others use old.

4. **Graceful-skip is observable.** When `google_maps_api_key` is absent, `directions()` raises a typed exception (`GoogleMapsKeyMissing`); onboarding catches it, logs a `WARN` line, and falls back to the conversational flow. No silent failures — user always knows why auto-fill didn't run.

5. **Curated dataset format is documented.** `data/airport_parking.json` and `data/train_fares_eu.json` get a top-of-file comment block describing schema + `last_verified` semantics. Future-Barry needs to know how to update them.

### Refactoring opportunities

- `transport_total()` in `nearby_airports.py:25` is currently used by the scorer and by the breakdown. Refactor to call `pick_cheapest_mode()` so scorer + display agree on cost.
- `seed_airport_transport()` in `db.py:895` is only used by `tests/conftest.py` fixtures — no production callers. Deprecate (don't delete yet) and replace test usage with new `add_transport_option()`.

### Architecture debt impact

- **Debt introduced:** old `airport_transport` table kept for one release as compat shim. Documented in TECHNICAL.md with R10 cleanup task.
- **Debt resolved:** silent-omission of nearby airports below €75 savings threshold is replaced with deterministic cheapest-mode comparison. Removes the `nearby_airports.py:42` magic threshold.

---

## Acceptance criteria (source of truth — verified at Phase 5)

- [ ] **AC-1** New user with home airport gets 3 nearby airports auto-proposed in onboarding (one bot message, not N).
- [ ] **AC-2** Each confirmed airport has ≥2 modes auto-populated (drive + transit minimum where Distance Matrix returns transit) when `google_maps_api_key` is set.
- [ ] **AC-3** `data/airport_parking.json` ships with ~20 EU airports (AMS, EIN, RTM, BRU, DUS, CRL, FRA, MUC, LHR, LGW, CDG, ORY, MAD, BCN, FCO, MXP, ZRH, GVA, ARN, CPH).
- [ ] **AC-4** `data/train_fares_eu.json` ships with curated estimates for ~10 common airport-pairs from common European home cities; user confirms at onboarding via inline `[Yes / Adjust]`.
- [ ] **AC-5** Deal page picks the cheapest mode per route at render time, accounting for party size + trip duration.
- [ ] **AC-6** Breakdown row shows the chosen mode label ("via train") so the user has transparency.
- [ ] **AC-7** Settings page renders all modes per airport, editable inline (cost, time, parking).
- [ ] **AC-8** User can ADD a new mode to an airport ("ride from family") via Settings → "+ add mode".
- [ ] **AC-9** User can DISABLE a mode per-airport (sets `enabled=0`); disabled modes excluded from cheapest-mode selection.
- [ ] **AC-10** Per-airport "always use [mode]" override stored and respected at render time.
- [ ] **AC-11** New `google_maps_api_key` add-on config option; onboarding auto-fill skipped gracefully (with conversational fallback) if unset.
- [ ] **AC-12** Migration runs cleanly: existing single-mode rows in `airport_transport` are read forward into `airport_transport_option` at first boot, no data loss. Tested with roundtrip test.
- [ ] **AC-13** Tests: `tests/test_transport_options.py` for cheapest-mode selection (1 pax / 4 pax / various trip durations / parking-included).
- [ ] **AC-14** Integration test: poll → snapshot → deal page render → assert chosen mode appears in breakdown row + total includes its cost.

## Build phases

Sequential. Each phase complete before next.

### Phase A — Schema, models, migration
- New table `airport_transport_option` (multi-mode, with `enabled`, `source`, `confidence`).
- New column `airport_override_mode` on `users` table (per-airport override stored as JSON `{AMS: "drive", EIN: null, ...}`).
- Migration: forward `airport_transport` rows → `airport_transport_option` as `source='user_override'`.
- Dataclass `AirportTransportOption` in `src/storage/models.py`.
- DB methods: `get_transport_options(code, user_id)`, `add_transport_option(...)`, `update_transport_option(...)`, `delete_transport_option(...)`, `set_airport_override_mode(...)`, `get_airport_override_mode(...)`.
- Test: `tests/test_db_migration_r9.py` — roundtrip migration test.

### Phase B — Google Maps client + curated datasets
- `src/apis/google_maps.py`: `directions(origin_coords, dest_coords, mode)` returning `{distance_km, duration_min}`. Raises `GoogleMapsKeyMissing` when key absent.
- Cache layer: store responses in DB to avoid re-calling for the same `(origin, dest, mode)` tuple.
- `data/airport_parking.json`: ~20 EU airports with `daily_eur` and `last_verified`.
- `data/train_fares_eu.json`: ~10 common airport-pairs with `rt_per_pp_eur` and `last_verified`.
- `data/viable_airports_eu.json`: allow-list of airports with long-haul service to filter regional/seasonal-only ones from auto-discovery.
- Tests: `tests/test_google_maps.py` (mock httpx), `tests/test_curated_datasets.py` (schema + key uniqueness).

### Phase C — Cheapest-mode selection
- `src/analysis/transport.py`: pure functions
  - `compute_mode_total(opt, passengers, trip_days) -> float` (party total RT)
  - `pick_cheapest_mode(options, passengers, trip_days, override_mode=None) -> AirportTransportOption | None`
- Refactor `transport_total()` in `nearby_airports.py` to delegate to `compute_mode_total()`.
- Tests: `tests/test_transport_options.py` covering 1pax/4pax × short/long trip × with/without parking × override behavior.

### Phase D — Render-path swap
- `src/web/data.py:299` — `db.get_airport_transport()` → `db.get_transport_options() + pick_cheapest_mode()`.
- `src/alerts/telegram.py` — same swap in `_format_cost_breakdown` helper.
- `src/analysis/scorer.py` — same swap (uses `transport_total()`, refactor cascades).
- Breakdown row label: `"transport (train, cheapest)"` instead of bare `"transport"`.
- Test: `tests/test_data_assembler.py` — assert chosen mode appears in breakdown rows + total.

### Phase E — Onboarding flow
- Update `src/bot/commands.py` ITEM-043 onboarding: after airport confirmation, propose 3 nearby airports from `viable_airports_eu.json` within 200km radius.
- For each airport, call `google_maps.directions()` for drive + transit; pull parking from `airport_parking.json`; pull train fare estimate from `train_fares_eu.json`.
- Inline confirm: `"[Adjust]"` button per value, `[All looks right]` to accept all.
- Fallback: if `google_maps_api_key` unset OR airport not in viable list → conversational flow ("How do you get to EIN? car/train/uber/bus + cost").

### Phase F — Settings page made editable
- `src/web/templates/settings.html.j2` — each airport row expands to show all modes with inline edit fields.
- New endpoints in `src/web/app.py`:
  - `PUT /api/airports/{code}/options/{mode}` — update value
  - `POST /api/airports/{code}/options` — add new mode
  - `DELETE /api/airports/{code}/options/{mode}` — disable / hard-delete (user_added only)
  - `PUT /api/airports/{code}/override` — set / clear "always use [mode]"
- `src/web/static/app.js` — handlers wired up (same `api()` helper as routes page).
- Mobile-first UX (Telegram WebApp).

### Phase G — Tests + code review
- Run full test suite. Target: 0 regressions.
- Phase 4 step 8 trigger: `/code-review` IS triggered (effort L + persistence schema migration). Run after all builds complete.
- Address ≥80-confidence findings before deploy.

### Phase H — Verify, deploy
- Self-audit per Phase 5 (post-build).
- Sync `farehound/src/` and `farehound/pyproject.toml` (per CLAUDE.md recipe — pyproject sync is **mandatory** if any new dependencies).
- Bump `farehound/config.yaml` to `0.11.0` (MINOR bump per SemVer 0.x rules — new feature, schema change).
- Commit, push, PR, merge to main, deploy via HA Supervisor.
- Manual verification cases written in `verification_report.md`.

## Out of scope

- ITEM-011 (weekend/short-trip date windowing) — R10
- Multi-user UX (waitlist UX, admin features) — already shipped, not touched
- Cloud migration (Railway, Postgres) — Phase A architecture work
- Discovery scanning (ITEM-038) — independent
- Removing the legacy `airport_transport` table — R10 cleanup

## Risks (accepted)

| Risk | Severity | Mitigation |
|---|---|---|
| User has no `google_maps_api_key` at deploy time | High | Graceful skip → conversational fallback. Auto-fill activates on next onboarding once key is provisioned. |
| Cheapest-mode picks counter-intuitive option | Medium | Per-airport "always use [mode]" override gives user the escape hatch. Mode label visible in breakdown row. |
| Curated `train_fares_eu.json` becomes stale | Low | `last_verified` field on each row; user confirms at onboarding; documented annual review in TECHNICAL.md. |
| Migration fails on existing data | High | Roundtrip test in `tests/test_db_migration_r9.py`. Old table preserved one release as compat shim. |
| Google Maps API quota exhaustion | Low | $200/mo free credit covers ~6,666 onboardings; FareHound is single-tenant; cache-on-write means same `(origin, dest, mode)` never re-called. |

## Definition of done

- All 14 acceptance criteria met (Phase 5 audit verifies).
- Test suite ≥465 → ≥500 (target +35 tests).
- `/code-review` clean of ≥80-confidence findings.
- Deployed to HA Pi as v0.11.0, observed in logs:
  - `Database schema initialized`
  - `Migrated N airport_transport rows → airport_transport_option`
  - `Scheduled polling every X hours`
  - No `ERROR` lines.
- Manual verification by Barry per `verification_report.md`.
