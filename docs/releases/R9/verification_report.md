# R9 — Verification Report

## Post-Build Audit

### Verdict: **Ship**

### Pre-Build Conditions Met: Yes

| Condition (from release_plan.md) | Met? | Notes |
|---|---|---|
| Migration preserves existing `airport_transport` rows | ✅ | Idempotent forward-migration tested via `test_legacy_airport_transport_migrated_forward` + `test_migration_is_idempotent` + `test_migration_preserves_user_edits`. |
| Cheapest-mode selection is a pure function in `src/analysis/transport.py` | ✅ | No DB / I/O imports. 17 unit tests cover math + selection + override + disable + resolve_breakdown. |
| Render path is single-write | ✅ | All 9 production callers (`web/data.py:303`, `orchestrator.py` × 5 incl. secondary loop, `bot/commands.py` × 3) use `db.get_resolved_transport()`. Legacy `get_airport_transport()` survives only as the in-method fallback inside the resolver. |
| Graceful-skip is observable when `google_maps_api_key` is absent | ✅ | `GoogleMapsKeyMissing` exception raised; onboarding catches and logs `WARN`. Tested in `test_autofill_without_gm_key_seeds_curated_data`. |
| Curated dataset format is documented | ✅ | Top-of-file `_meta` blocks in all three JSON files. |

### Drift Detected
None. The `_PER_PERSON_MODES` set is duplicated between `analysis/transport.py:15` and `analysis/nearby_airports.py:10` — flagged in code review as minor #14, not fixed because removing the duplicate would require touching unrelated code (CLAUDE.md "don't refactor surrounding code"). Captured as follow-up.

### Coherence Assessment
The new module set (`src/analysis/transport.py`, `src/apis/google_maps.py`, `src/utils/airport_data.py`) sits cleanly alongside existing peers (`nearby_airports.py`, `serpapi.py`, `airports.py`). No new architectural primitive was bolted on — the multi-mode shift is a deeper schema decision that the existing call patterns absorbed without reshaping (every consumer kept its dict-access style; only the data source changed).

### Mandatory Follow-ups (≥80 confidence findings)
All 8 fixed in this release. See commit `5825f8e`.

### Backlog (<80 confidence findings — captured for next release)

- **R9-FU1 (#5)** — Cap `label` field length server-side (currently relies on client `maxlength=40`).
- **R9-FU2 (#6)** — Cap upper bounds on `cost_eur` (≤5000), `time_min` (≤600), `parking` (≤500) to catch typos.
- **R9-FU3 (#8)** — `add_transport_option` should preserve `source='user_override'` on conflict (currently downgrades to `user_added`).
- **R9-FU4 (#10)** — `get_resolved_transport` makes a second SQL roundtrip for legacy metadata; combine.
- **R9-FU5 (#13)** — Hoist `is_per_person_mode` import in `db.py` (no real circular dep).
- **R9-FU6 (#14)** — De-dup `_PER_PERSON_MODES` between `transport.py` and `nearby_airports.py`.
- **R9-FU7 (#15)** — Drop legacy `airport_transport` table after one release of clean operation (R10).
- **R9-FU8 (#16)** — Sort fallback codes alphabetically in `assemble_settings`.

### Architecture Debt Ledger (Final)
- **Debt introduced:** Legacy `airport_transport` table still readable as fallback inside `get_resolved_transport()`. Documented R10 cleanup.
- **Debt resolved:** None directly, but `pick_cheapest_mode` removes the implicit-comparison path that lived in three places (orchestrator + scorer + bot) by centralizing it.
- **Net change:** Better. New schema is normalized + extensible (multi-mode, per-airport overrides, sources/confidence labels).

---

## Acceptance Criteria — Verified

| ID | Criterion | Met? | Evidence |
|---|---|---|---|
| AC-1 | New user gets 3 nearby auto-proposed | ✅ | `find_nearby_airports()` returns up to 3 within 200km; called from `_finish_onboarding` → `_auto_fill_transport_options`. |
| AC-2 | Each airport ≥2 modes auto-populated when key set | ✅ | `test_autofill_with_gm_key_populates_drive_and_taxi`: AMS gets train+drive+taxi. |
| AC-3 | `data/airport_parking.json` ≥20 EU airports | ✅ | 27 airports. `test_parking_dataset_schema_sane`. |
| AC-4 | `data/train_fares_eu.json` ≥10 pairs | ✅ | 19 pairs. `test_train_fares_dataset_schema_sane`. |
| AC-5 | Cheapest mode picked at render time per party + duration | ✅ | `test_assemble_deal_picks_cheapest_mode_for_2pax_7days`, `test_assemble_deal_disabled_mode_excluded`. |
| AC-6 | Breakdown row shows chosen mode label | ✅ | `test_assemble_deal_picks_cheapest_mode_for_2pax_7days` asserts "train" + "cheapest" in label. |
| AC-7 | Settings page renders editable modes | ✅ | Manual render test against `/settings?tg=test` confirmed cards + dropdown. |
| AC-8 | User can ADD a new mode | ✅ | `test_post_option_then_get`. |
| AC-9 | User can DISABLE a mode (excluded from cheapest) | ✅ | `test_assemble_deal_disabled_mode_excluded`. |
| AC-10 | Per-airport override stored + respected | ✅ | `test_assemble_deal_respects_override` + `test_set_and_clear_override_mode`. |
| AC-11 | New `google_maps_api_key` add-on option; graceful skip | ✅ | `farehound/config.yaml` + `farehound/rootfs/.../run`; `test_autofill_without_gm_key_seeds_curated_data`. |
| AC-12 | Migration roundtrip; no data loss | ✅ | `test_legacy_airport_transport_migrated_forward`, `test_migration_is_idempotent`, `test_migration_preserves_user_edits`. |
| AC-13 | Cheapest-mode unit tests | ✅ | 17 tests in `test_transport_options.py`. |
| AC-14 | Integration test: poll → snapshot → deal page → assert mode in breakdown | ✅ | `test_r9_render_integration.py` (5 tests). |

**14/14 met.**

---

## Test Results

- **Total tests:** 465 (pre-R9) → **546** (post-R9 fixes). +81 R9 tests.
- **Pass rate:** 546/546 (100%).
- **Test runtime:** ~4.3s on dev machine.
- **New test files:**
  - `tests/test_transport_options.py` — 28 tests (math + DB CRUD + migration)
  - `tests/test_google_maps.py` — 26 tests (client + datasets + heuristics)
  - `tests/test_r9_render_integration.py` — 5 tests (end-to-end render)
  - `tests/test_r9_onboarding_autofill.py` — 4 tests (auto-fill flow)
  - `tests/test_r9_web_endpoints.py` — 15 tests (5 endpoints + edge cases)

---

## Code Review Summary

Independent review found **16 findings**:
- **8 ≥80 confidence:** all fixed in commit `5825f8e`. Three were Important (correctness + render-path + override validation), five were Minor (clamping + comments + private-attr access + dialog pre-fill).
- **8 <80 confidence:** captured as R9-FU1 through R9-FU8 above. Backlogged for R10.

No Critical findings. Net architectural impact assessed as **Better**.

---

## Manual Verification Cases

After deploy to HA Pi, Barry should run through these to confirm v0.11.0 works end-to-end:

### MV-1: Migration ran cleanly on first boot

1. **DO:** Tail the add-on logs immediately after deploy:
   ```
   sudo docker exec hassio_cli ha apps logs 30bba4a3_farehound | tail -40
   ```
2. **EXPECT:**
   - `Database schema initialized`
   - `Migrated N airport_transport rows → airport_transport_option` (N = number of airports you had configured pre-R9, likely 1–3)
   - No `ERROR` lines.
3. **WRONG IF:** No `Migrated` line at all (migration didn't run), OR errors mention `airport_transport_option does not exist`, OR multiple `Migrated N` lines on subsequent boots (migration not idempotent).

### MV-2: Existing transport data preserved on the deal page

1. **DO:** Open the FareHound Mini Web App → tap any deal → look at "Cost breakdown."
2. **EXPECT:** Transport row shows the airport's mode + a non-zero cost (legacy AMS row migrated forward); breakdown sums correctly. Mode label is the bare mode name (e.g. `"transport (drive)"`) since you only have one mode pre-R9.
3. **WRONG IF:** Transport row shows €0 (migration didn't carry data over) or "transport ()" with no mode.

### MV-3: Settings page renders editable cards

1. **DO:** Open Mini Web App → Preferences.
2. **EXPECT:**
   - "Airports & transport" section lists each of your configured airports as a card.
   - Each card shows the legacy mode you had pre-R9.
   - Tap a row → edit dialog opens with cost / time / parking fields populated.
   - Save → page reloads and shows the new value.
3. **WRONG IF:** Cards don't render, dialog doesn't open, or save fails with a 4xx/5xx error.

### MV-4: Add a new mode

1. **DO:** On Preferences, find your AMS card → tap "+ add mode" → fill in `mode=train`, `cost=15`, `time=30`, `enabled=on` → Save.
2. **EXPECT:** New train row appears alongside your existing legacy mode. Re-open a deal → breakdown shows "train (cheapest)" if train is genuinely cheaper for the party + duration, else your legacy mode with "cheapest" suffix.
3. **WRONG IF:** Train row doesn't appear, or appears but breakdown still shows the old mode without the "(cheapest)" suffix when train should win.

### MV-5: Set "always use [mode]" override

1. **DO:** On a card with ≥2 modes, change "Always use" dropdown from "cheapest (auto)" to "drive" (or whichever mode you prefer).
2. **EXPECT:** Selection persists on reload. Open a deal → breakdown row says "drive (your choice)".
3. **WRONG IF:** Dropdown reverts on reload, or breakdown row still shows the cheapest mode.

### MV-6: Disable a mode

1. **DO:** Tap a mode row → uncheck "Enabled" → Save.
2. **EXPECT:** Row gets strike-through styling. Open a deal → breakdown does NOT pick that mode even if it would be cheapest.
3. **WRONG IF:** Disabled mode still appears in breakdown.

### MV-7: Onboarding for a NEW user (only relevant once Barry adds a second user, or for a fresh install)

1. **DO:** Trigger fresh onboarding via the bot (start fresh chat from a new Telegram account if available, OR simulate by clearing the user row in the DB and re-running `/start`).
2. **EXPECT (no `google_maps_api_key` set):**
   - After airport confirmation, message: "(Google Maps API key not configured — transport costs are blank. Add them in Preferences.)"
   - Settings page shows the airport card with the legacy single-mode row only.
3. **EXPECT (with `google_maps_api_key` set):**
   - Message: "I've estimated transport costs (drive / train / taxi + parking). Review and adjust the numbers in Preferences."
   - Settings page shows multiple modes per airport, marked as `(estimate)` for medium/low confidence.
4. **WRONG IF:** Either path errors out, or the success message references the wrong key state.

### MV-8: Reverify previously broken case — "no nearby airports configured"

1. **DO:** Open the deal page.
2. **EXPECT:** If your nearby airports have data in `airport_transport_option` (post-migration), the alternatives section shows them. If not, the "No nearby airports configured for this destination" message persists — which is correct, but going to Preferences and adding modes for those airports should resolve it.
3. **WRONG IF:** Alternatives section is empty even when nearby airports DO have transport options configured.

---

## Deploy Steps (per CLAUDE.md recipe)

```bash
# In the worktree, BEFORE pushing:
rm -rf farehound/src && cp -r src/ farehound/src/
cp pyproject.toml farehound/pyproject.toml

# Bump farehound/config.yaml: 0.10.16 → 0.11.0
# (already in this commit — verify before merging)

# Commit + push + PR + merge to main.

# On HA Pi:
sudo docker exec hassio_cli ha apps stop 30bba4a3_farehound
sudo docker exec hassio_cli ha store reload
sudo docker exec hassio_cli ha apps update 30bba4a3_farehound
sudo docker exec hassio_cli ha apps start 30bba4a3_farehound
sleep 15
sudo docker exec hassio_cli ha apps logs 30bba4a3_farehound | tail -40
```

**Verify the Migrated log line appears.** If yes, run MV-1 through MV-8.

If you want auto-fill for new airports, add `google_maps_api_key` to the add-on Configuration tab. Without it, the rest of R9 still works — you just enter transport values manually in Preferences.
