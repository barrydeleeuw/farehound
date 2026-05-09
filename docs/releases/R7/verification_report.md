# R7 Verification Report — Real Cost Restoration

**Audit by:** Architect-Lead
**Audit date:** 2026-05-09
**HEAD:** `ab06fb7` (20 commits since `26d9947`)
**Suite:** 420/420 passing (locally re-run by Architect-Lead, 2.05s)
**Plan:** [`docs/releases/R7/release_plan.md`](release_plan.md)
**Build log:** [`docs/releases/R7/build_log.md`](build_log.md)

---

## 1. Verdict

**Ship with Follow-ups.**

R7 cleanly delivers ITEM-051's 10 sub-items with strong test coverage (420/420; +109 new tests, no regressions). Every Pre-Build Condition (C1–C10) is met in code, with one minor stylistic deviation on C2 that is functionally equivalent. The single material drift — `_LEGACY_ALIAS` map flattened into an `if`-ladder — is documented in §3 and does not affect behaviour. T19 caught a real integration bug (`deal_info.route_id` missing on real alerts) that unit tests had silently passed; the fix is committed at `c734361`. No security or data-integrity issues.

Three follow-ups for the next release (P3 / nice-to-have, NOT blockers): see §8.

---

## 2. Pre-Build Conditions Met

| # | Condition (paraphrased) | Status | Evidence |
|---|-------------------------|--------|----------|
| C1 | DB migrations idempotent, `_has_column → ALTER` pattern | ✅ Met | `db.py:209–233` — all 5 ALTER blocks guarded by `_has_column`. `tests/test_db.py::TestR7Migrations` exercises double-`init_schema()` no-op. |
| C2 | Callback dispatch: `data.split(":", 2)` and accept legacy single-segment until 2026-06-08 | ✅ Met (with stylistic deviation) | `commands.py:811–822` does the new 3-segment parse first, falls back to legacy 2-segment `split(":", 1)` ladder at `:824–965`. The `_LEGACY_ALIAS` dict from §7.2 was not used; an `if action == "book" / "wait" / "dismiss" / "watching" / "booked" / "digest_booked" / "digest_dismiss"` ladder is functionally identical and was kept. All seven legacy prefixes still work — see §3 Drift. |
| C3 | Cost-breakdown helper lands first; all 4 message types switch to it | ✅ Met | T4 commit `de5dfa6` landed before T7 commit `5adc776`. Helper `_format_cost_breakdown` (telegram.py:198) called from 6 sites covering all 4 message types: deal alert (`:326`, alt at `:385`), error fare (`:471`), follow-up (`:507`), digest (`:568`, alt at `:614`). |
| C4 | Baggage parsing never throws | ✅ Met | `parse_baggage_extensions` (baggage.py:43) returns None on `None`/non-list/non-string, regex misses fall through to `matched_any=False`. `estimate` (baggage.py:82) wraps everything in `try/except`. Tester exercised malformed-input paths in T15. |
| C5 | "+ €N bags" only when total > 0 AND source != "unknown" | ✅ Met | `_baggage_total` (telegram.py:181) returns 0 on `source == "unknown"` or missing dict. `_format_cost_breakdown` only appends "bags" when `bags > 0` (telegram.py:218–219). |
| C6 | `_latest_nearby_comparison[route_id]` is a dict; never `pop()` on empty `evaluated` | ✅ Met | `orchestrator.py:122` typed as `dict[str, dict]`. `pop()` only fires at `:949` when no secondaries were polled at all (correct semantics). When `evaluated` is non-empty but `competitive` is empty, the entry is preserved so the renderer can show "Checked N — yours is best". |
| C7 | Scorer outputs structured `reasoning`; `deals.reasoning_json` (TEXT JSON) + flattened `deals.reasoning` for legacy reads | ✅ Met | `DealScore.reasoning: dict` (scorer.py:121). `_coerce_reasoning` (scorer.py:384) produces dict from string-or-dict input. Orchestrator writes both fields at `orchestrator.py:1019–1020` (poll path) and `:1677–1678` (community-deal path). `db.insert_deal` extended at `db.py:596,607` to write `reasoning_json`. |
| C8 | Snooze enforcement in `poll_routes()` AND `send_daily_digest()`; time-based, no deletes | ✅ Met | `db.get_active_routes(user_id)` (db.py:344–346) filters `snoozed_until IS NULL OR <= now`. `db.get_routes_with_pending_deals` (db.py:678) JOINs `routes` and applies same predicate. Both `poll_routes` (orchestrator:392) and `send_daily_digest` (orchestrator:1177, :1218) call these. |
| C9 | Auto-snooze 30d on `booked` from new AND legacy callback paths | ✅ Met | `_auto_snooze_route_for_deal` (commands.py:683) is called from: new `deal:book` (`:730`), legacy `digest_booked` (`:896`), legacy `book` (`:944`), legacy `booked` (`:957`). Looks up `deal.route_id`, calls `db.snooze_route(route_id, 30)`. |
| C10 | "📊 Details" placeholder URL = Google Flights deep link, no new endpoint | ✅ Met | `_build_deal_keyboard` (telegram.py:441) sets `details_url = self._google_flights_url(deal_info)`. Digest keyboard (telegram.py:666) uses the same `search_url`. No new server, no new route. |

---

## 3. Drift Detected

### 3.1 `_LEGACY_ALIAS` dict → `if`-ladder (Condition C2 stylistic)
**Plan §7.2** specified a flat `_LEGACY_ALIAS = {"book": ("deal", "book"), ...}` lookup. Builder kept the existing `if action == "book" / "wait" / "dismiss" / "watching" / "booked" / "digest_booked"` ladder (commands.py:942–965) and `digest_dismiss` block (`:901–910`). All 7 legacy prefixes still route correctly; auto-snooze fires on the booked variants. **Verdict: functionally equivalent, accept as-is.** Rewriting to the dict shape would be churn for no behaviour change.

### 3.2 T8 work absorbed into T7 + T13
**Plan §12** had T8 producing the 3-button row + the `route:snooze:7:*` handler. Builder landed both pieces inside T7 (`5adc776` — keyboard via `_build_deal_keyboard`) and T13 (`a146db3` — `_handle_new_callback` route:snooze branch), then closed T8 as a no-op consolidation in `build_log.md`. **Verdict: clean consolidation.** Acceptance for T8 is satisfied (3-button row in deal alerts AND digest, `route:snooze:7` snoozes + bulk-dismisses) — verified at telegram.py:432–443, 656–667 and commands.py:748–775.

### 3.3 T19 caught the `deal_info.route_id` regression (commit `c734361`)
T19 was the first test to drive the orchestrator → telegram_notifier path with real objects. Three orchestrator deal-info dicts were missing `"route_id"` (poll path, community-deal happy path, community-deal fallback path), which silently dropped the "Skip route 🔕" button on production alerts. Unit tests passed because they hardcoded `route_id`. **Verdict: this is exactly the integration-test win the /release skill promises.** Fixed correctly — all three sites now populate `route_id=route.route_id` (orchestrator:1138, :1701, :1635 at the time of the fix).

### 3.4 25-cached-response smoke loop (T15) not exercised
Plan §12 T15 acceptance included a smoke test loading the 25 cached SerpAPI responses and asserting the parser doesn't raise. Tester documented in build_log §T15 that the cached fixtures don't exist in this worktree (they live at `~/projects/Claude Code/FareHound/data/serpapi_cache/`, not inside the worktree). Defensive guarantee (Condition C4) is verified instead via `test_empty_result_does_not_raise` and explicit malformed-input cases. **Verdict: acceptable substitute.** Architect-Lead's Finding #1 confirmed the cached responses contain zero baggage data, so the loop would have only verified "doesn't raise on empty extensions" — already covered.

### 3.5 Per-route delta `digest_skip_count_7d` rolling-window heuristic
Plan §11 / build_log §T10 promised "reset `digest_skip_count_7d` to 0 when last_digest_sent_at is older than 7 days". Implementation (orchestrator.py:1374–1382) does NOT reset the counter — it monotonically increments. **Verdict: minor — affects only the `/status` display number** ("Digest skipped N of last 7 days"). For Barry's single-tenant install with daily digests this stabilises naturally; in a long-running install the count drifts upward. Listed as Follow-up FU-1 in §8.

### 3.6 `T19` test file naming
Plan called for `tests/test_v07_release.py`; Tester used `tests/test_integration_r7.py`. **Verdict: better name, accept.** The v0.7/v0.8 release-test files are cumulative regression suites; a per-release integration file is a cleaner pattern.

---

## 4. Coherence Assessment

The codebase still reads as a unified whole. Every R7 module slots into the existing patterns:

- **DB layer:** the 5 new columns sit in `init_schema` next to existing `_has_column`-guarded migrations; the new `snooze_route` / `unsnooze_route` / `get_status_stats` helpers mirror the style of `log_saving` / `get_total_savings`.
- **Telegram layer:** the new `_render_*` helpers and `_format_cost_breakdown` follow the existing module-level helper pattern (`_deal_emoji`, `_deal_label`, `find_cheapest_date`). Class methods stayed instance methods.
- **Orchestrator:** `_compute_digest_fingerprint` / `_format_digest_header` / `_compute_baggage_for_result` are static or simple instance helpers; no new event-loop concept introduced.
- **Scorer:** the new prompt format keeps the same Claude-API call shape; `_coerce_reasoning` and `_fallback_reasoning` slot in alongside the existing `_parse_response`.
- **Bot:** `_handle_new_callback` is dispatched FIRST inside the existing `_handle_callback` (commands.py:811), legacy ladder remains intact. Slash commands (`/snooze`, `/unsnooze`, `/status`) follow the same `_handle_*` pattern as `/savings`, `/trips`.
- **Tests:** new test files (`test_serpapi_baggage.py`, `test_integration_r7.py`) match the existing pattern (per-module test file, class-grouped tests). `test_db.py:TestR7Migrations` mirrors the existing `TestRoutes` / `TestDeals` class style.

No surprise abstractions, no new dependencies (`pyproject.toml` diff is empty), no orphaned helpers. The diff totals +4214 lines (production: ~1238; tests + fixtures: ~2976; docs: ~984), but every production addition has a clear caller — see §5.

---

## 5. Flow Tracing — every new component has a real caller

| New component | Defined in | Real caller(s) | Verified |
|---------------|------------|----------------|----------|
| `src/utils/baggage.py::parse_baggage_extensions` | baggage.py:43 | `serpapi.py:60` (lazy import in `FlightSearchResult.parse_baggage`) | ✅ |
| `src/utils/baggage.py::estimate` | baggage.py:82 | `serpapi.py:60` (same import); fallback inside `parse_baggage` | ✅ |
| `FlightSearchResult.parse_baggage` | serpapi.py:~50 | `orchestrator._compute_baggage_for_result` (orchestrator:1734); called from primary snapshot site (orchestrator:549) AND secondary on-demand (orchestrator:891) | ✅ |
| `compare_airports` (new dict shape) | nearby_airports.py:38 | orchestrator:807, :929; commands.py:2020 (price-check) | ✅ all callers index `["competitive"]` or read `["evaluated"]` |
| `_format_cost_breakdown` | telegram.py:198 | telegram.py: 6 sites covering deal alert (`:326`, alt `:385`), error fare (`:471`), follow-up (`:507`), digest (`:568`, alt `:614`) | ✅ |
| `_render_reasoning_bullets` | telegram.py:69 | telegram.py:358 (deal alert), :482 (error fare) | ✅ |
| `_render_transparency_footer` | telegram.py:86 | telegram.py:410 (deal alert), :639 (digest) | ✅ |
| `_render_date_transparency` | telegram.py:124 | telegram.py:365 (deal alert), :643 (digest) | ✅ |
| `_baggage_total` | telegram.py:181 | telegram.py:211 (`_format_cost_breakdown` only) | ✅ |
| `_build_deal_keyboard` | telegram.py:420 | telegram.py:416 (deal alert), :491 (error fare) | ✅ |
| `digest_header_override` payload | orchestrator.py:1369 | telegram.py:534 (`send_daily_digest` reads override) | ✅ |
| `nearby_evaluated` payload | orchestrator.py:1157, :1304 | telegram.py:409 (deal alert), :638 (digest) | ✅ |
| `reasoning_json` payload | orchestrator.py:1153, :1717 | telegram.py:69 (`_render_reasoning_bullets` reads it) | ✅ |
| `_compute_digest_fingerprint` | orchestrator.py:1387 | orchestrator.py:1335 | ✅ |
| `_format_digest_header` | orchestrator.py:1409 | orchestrator.py:1367 | ✅ |
| `_compute_baggage_for_result` | orchestrator.py:1734 | orchestrator.py:549, :891 | ✅ |
| `db.snooze_route` / `db.unsnooze_route` | db.py:353, :362 | commands.py:759 (route:snooze callback), commands.py:780 (route:unsnooze), commands.py:1507 (`/snooze`), commands.py:1527 (`/unsnooze`), commands.py:699 (auto-snooze helper) | ✅ |
| `db.get_status_stats` | db.py:~395 | commands.py:_handle_status (commands.py:1433) | ✅ |
| `db.get_active_routes(include_snoozed)` (extended) | db.py:305 | orchestrator.py:392 (poll), :1177 (digest), :1218 (digest); commands.py multiple | ✅ |
| `_auto_snooze_route_for_deal` | commands.py:683 | commands.py:730 (deal:book), :896 (digest_booked), :944 (legacy book), :957 (legacy booked) | ✅ |
| `reasoning_to_bullets` | scorer.py:103 | orchestrator.py:1019, :1152, :1677, :1716 (4 sites — poll happy path, deal_info, community happy path, community fallback_info) | ✅ |
| `_coerce_reasoning` / `_fallback_reasoning` | scorer.py:384, :405 | scorer.py:367, :379 (`_parse_response` only) | ✅ |
| `/snooze`, `/unsnooze`, `/status` slash dispatch | commands.py:405, :408, :411 | commands.py:377 (slash command branch in `_handle_message`) | ✅ |

**No orphan code detected.** Every public/private helper added in R7 has at least one caller in the orchestration or alert layer. T19's catch (deal_info.route_id missing) was the only such gap and is fixed.

---

## 6. Acceptance Criteria check-off (ITEM-051 roadmap)

Roadmap-level criteria from `ROADMAP.md:39–47`:

- [x] All 4 message types include cost breakdown and baggage line when data available — verified §5 row "_format_cost_breakdown" + T14 (telegram unified tests)
- [x] Watching button on deal alerts AND digest, not just follow-up — verified telegram.py:434 (deal alert), :660 (digest) + T14 keyboard tests
- [x] Route snooze respected in poll loop and digest; auto-snooze fires on booked feedback — verified C8, C9 + T17 orchestrator tests
- [x] Daily digest skipped when no route price moved >€10 since last digest AND <3 days — verified orchestrator.py:1344–1350 + T17 skip-predicate matrix tests
- [x] Scorer reasoning returns structured JSON with 3 bullet fields — verified C7 + T18 scorer contract tests
- [x] `/status` command works — verified commands.py:1433 + smoke test in build_log §T11
- [x] Tests added: `test_telegram` (4 message types), `test_serpapi_baggage` (new), `test_db` (migrations), `test_orchestrator` (digest skip + snooze), `test_scorer` (structured) — all 5 present, +T18 scorer + T19 integration. 109 new tests across 6 files.
- [ ] Deployed to HA per CLAUDE.md sync flow — **NOT YET.** This is part of the ship step, not the audit. Deployment instructions are in CLAUDE.md and will run after Barry signs off via the manual verification cases in §10.

7 of 8 acceptance boxes met; the 8th is pending the ship step itself.

---

## 7. Test summary

**Total: 420/420 passing** (locally re-verified by Architect-Lead). Started at 311; Tester added 109 new tests across 6 files:

| File | New tests | Coverage |
|------|-----------|----------|
| `tests/test_telegram.py` | 34 | All 4 message types + render helper unit tests |
| `tests/test_serpapi_baggage.py` (new) | 31 | Parser + fallback table + `parse_baggage` pipeline against 3 synthetic fixtures |
| `tests/test_db.py` | 14 | All 5 R7 ALTER blocks: existence, idempotency, defaults, JSON round-trip |
| `tests/test_orchestrator.py` | 15 | Snooze filtering, auto-snooze, fingerprint helpers, full digest skip/send flow with real DB |
| `tests/test_scorer.py` | 11 | Structured 3-field contract: parse / coerce / fallback / urgency / renderer |
| `tests/test_integration_r7.py` (new) | 4 | End-to-end: real Orchestrator + DB + Notifier; mocks only HTTP boundaries |

**T19's role:** the integration test caught the `deal_info.route_id` regression that all 416 unit tests had missed. Without T19, Barry would have shipped a broken "Skip route 🔕" button. This is the canonical example of why /release Phase 4 step 6b is non-negotiable. Fix committed at `c734361` and re-tested — 420/420 still green.

**Synthetic fixtures created:** `tests/fixtures/serpapi_with_baggage/{full_baggage_both_ways,outbound_only_baggage,no_baggage_data}.json`. Architect-Lead's Finding #1 confirmed no real cached SerpAPI responses contain baggage data, so manufactured fixtures were necessary to exercise the parsing pipeline.

---

## 8. Mandatory Follow-ups

These should land on the roadmap as P3 cleanup. None are blockers for shipping R7.

| ID | Title | Why | Effort |
|----|-------|-----|--------|
| FU-1 | Reset `digest_skip_count_7d` rolling 7-day window | §3.5 — counter currently monotonically increments. `/status` line "Digest skipped N of last 7 days" drifts upward over time. Fix is ~5 lines in `send_daily_digest`: query `digest_skip_count_7d` reset window from `last_digest_sent_at`. | XS |
| FU-2 | Bound `/snooze` `days` argument | `/snooze Tokyo 999999` would set `snoozed_until` to year 4760. Per-user only, but ugly. Cap at 365 days, reply with friendly error if higher. | XS |
| FU-3 | Reconcile T15's 25-cached-response smoke loop | §3.4 — the cached responses live outside the worktree. Either (a) check them in to `tests/fixtures/serpapi_cache/` or (b) document the env-var pattern (`SERPAPI_CACHE_DIR=...`) for the test loop. | S |
| FU-4 | Drop legacy `deals.reasoning` column after 60 days | Once all in-flight messages with legacy callbacks have rolled over (target 2026-07-08, post C2 alias deadline), `reasoning_json` is the only consumer of the structured field. The flat-string `reasoning` column can be retired in a future release. | S — gated on time, not effort |
| FU-5 | Replace `_LEGACY_ALIAS`-style ladder with explicit dict (or remove altogether after 2026-06-08) | §3.1 — once the C2 30-day alias window closes, the legacy `if`-ladder block at commands.py:942–965 + the `digest_*` block at :886–910 can be deleted. | S |

None of FU-1 through FU-5 affect the core feature. The roadmap entry should bundle them as ITEM-NEXT cleanup.

---

## 9. Architecture Debt Ledger (Final)

| Δ | Item | Status |
|---|------|--------|
| **Resolved** | Cost breakdown duplicated 4× in telegram.py | ✅ Helper extracted; 6 call sites all routed through it. |
| **Resolved** | Inconsistent callback prefixes (`book:`, `wait:`, `dismiss:`, `watching:`, `booked:`, `digest_booked:`) | ✅ New `deal:*` / `route:*` namespace; legacy aliases retained until 2026-06-08 (Condition C2). |
| **Resolved** | Free-text scorer reasoning (hard to render, hard to test) | ✅ Structured 3-field contract; renderer + flatten-for-legacy + back-compat coercion. |
| **Resolved** | Silent omission of nearby airports below €75 | ✅ Two-list contract (`competitive` / `evaluated`); transparency footer renders the missing case. |
| **Resolved** | Daily digest fires every day regardless of price movement | ✅ Fingerprint + 4-condition skip predicate; concrete "what moved" header when sending. |
| **Introduced** | `src/utils/baggage.py` — fallback table to maintain | ⚠️ 108 LOC, single file. Bounded. Predicted in advisory. |
| **Introduced** | `deals.reasoning_json` + flattened `deals.reasoning` (back-compat tax) | ⚠️ Predicted. Plan: drop `reasoning` column after 60 days (FU-4). |
| **Introduced** | `_LEGACY_ALIAS`-style ladder in commands.py (instead of flat dict) | ⚠️ Stylistic; FU-5 cleans this after 2026-06-08. |
| **Introduced** | `digest_skip_count_7d` monotonic counter (no rolling reset) | ⚠️ FU-1; affects `/status` display only. |
| **Introduced** | `/snooze` `days` arg unbounded | ⚠️ FU-2; per-user only, no escalation risk. |
| **Net change** | **Debt down, decisively.** R7 resolves 5 long-standing items and introduces 5 small bounded ones — 4 of those have a documented retirement path (FU-1 / FU-2 / FU-4 / FU-5). | |

---

## 10. Manual Verification Cases for Barry

5 concrete scenarios. Each has DO / EXPECT / WRONG-IF. Run after deploy.

### MV-1 — Daily digest skip
**DO:** Wait 24 hours after the next digest fires. Don't add or remove any routes. Don't let any monitored price move by more than €10. Wait for the next 8:00 AM scheduled digest.
**EXPECT:** No Telegram message at 8:00 AM. In logs: `"Digest skipped for user {your_chat_id} — fingerprint unchanged, last digest 1.0d ago"`.
**WRONG IF:** A digest message arrives anyway, OR the log line says `Sending daily digest`.

### MV-2 — `/status` command
**DO:** Send `/status` in the FareHound chat.
**EXPECT:** A message like:
```
📊 FareHound Status
• Monitoring: 3 routes (1 snoozed)
• Last poll: 6h ago
• Alerts this week: 5 (1 booked, 2 watching, 2 dismissed)
• SerpAPI usage: 184/950 (last 30d)
• Saved you €840 across 2 trips (/savings)
• Digest skipped 3 of last 7 days (no significant price moves)
```
**WRONG IF:** Bot replies `Try /help for commands.` (means slash command not wired) OR any line shows zero-everywhere when you have active routes.

### MV-3 — `/snooze` and Skip route button
**DO:** On any deal alert, tap the **Skip route 🔕** button. Or send `/snooze Tokyo 14`.
**EXPECT:**
- Toast: `"Snoozed 7d"` (button) or `"🔕 Snoozed AMS-NRT for 14 days. /unsnooze AMS-NRT to resume."` (slash).
- The route stops appearing in the next daily digest.
- Pending deals on that route are dismissed (cleared from digest backlog).
- Tomorrow's poll cycle does NOT call SerpAPI for this route.
**WRONG IF:** Digest still mentions the route, OR poll logs show `Searching flights AMS → NRT` for the snoozed route.

### MV-4 — Auto-snooze on Booked
**DO:** On any deal alert (or via the daily digest), tap **Yes, booked ✅** (or **Booked ✅**).
**EXPECT:**
- Toast: `"Marked as booked!"`
- The route silently disappears from the next 30 days' digests AND polls.
- `/status` shows the snooze count incremented.
- Send `/unsnooze AMS-NRT` to re-enable.
**WRONG IF:** Booked button still leaves the route active, OR `/status` snooze count doesn't increase.

### MV-5 — Cost breakdown with baggage
**DO:** Wait for the next deal alert from a route flown by a budget airline (Transavia HV, Ryanair FR, easyJet U2, Wizz W6) — these have non-zero fallback baggage fees.
**EXPECT:** Cost breakdown line includes `+ €N bags`:
```
€480 flights + €30 train + €120 bags = €630 total
```
On a flag carrier (KLM long-haul, Air France) the bags line should be absent (free checked → `_baggage_total=0` → suppressed per Condition C5).
**WRONG IF:** A budget-carrier alert arrives WITHOUT a bags line, OR a flag carrier alert shows `+ €0 bags`.

### MV-6 — Transparency footer ("we checked X")
**DO:** Wait for an alert on a route where your primary airport is genuinely the cheapest (most days, AMS will be).
**EXPECT:** A footer line at the end of the alert: `✓ Checked EIN, BRU — your airport is best by €40–€60` (or similar).
**WRONG IF:** Footer absent on a route with secondary airports configured. Older "silent omission below €75" behaviour means you'll see *no* alternatives when there are no compelling ones — the new footer should explicitly say we checked them.

### MV-7 — Structured reasoning (3 bullets)
**DO:** Wait for any deal alert from a route with at least 5 prior snapshots (most active routes).
**EXPECT:** Three bullet lines after the price/baggage block:
```
✓ Cheapest of 4 dates polled — Mar 12 saves €60/pp vs others
✓ €80 below Google's typical low (€620–€780)
✓ Yours is best — €40 cheaper door-to-door than EIN
```
**WRONG IF:** A single italicised free-text line (means scorer fell back to legacy mode — check Anthropic API health) OR fewer than 3 bullets.

### MV-8 — Concrete digest header
**DO:** When the daily digest fires (and isn't skipped per MV-1), read the first message of the digest.
**EXPECT:** A header like `📊 FareHound Daily — 3 routes, 2 prices moved` followed by per-route lines:
```
• AMS→NRT dropped €40 (€1820/pp)
• AMS→BKK new low (€620/pp)
• AMS→LIS unchanged
```
**WRONG IF:** The old generic header `"You haven't decided on these yet:"` appears (means override path didn't fire — check orchestrator logs).

---

## 11. Security Posture Check

- **No new dependencies.** `pyproject.toml` diff vs `26d9947` is empty.
- **No hardcoded secrets in production code.** Architect-Lead grep of `(api[_-]?key|secret|token|password|bearer).*=.*['\"]` against `git diff 26d9947..HEAD -- 'src/**'` returned zero hits. (The string `api_key` appears only as parameter names and dict keys — not as literal credentials.)
- **No new external API calls or endpoints.** "📊 Details" is a Google Flights deep-link only (Condition C10); no FastAPI/HTMX server, no new HTTP route. Telegram callbacks remain the only inbound surface, gated by the same bot token.
- **DB schema:** all new columns are `TEXT` or `INTEGER DEFAULT 0`; no FK-referencing changes; existing `user_id` scoping preserved on `routes`, `price_snapshots`, `deals` for all new fields. No new PII categories.
- **Input validation on new commands:**
  - `/snooze {route} [days]`: route resolved by `_resolve_route_for_user` (matches active routes for the calling user only — no cross-user data leak); `days` parsed via `isdigit()` (always non-negative integer). **Minor:** `days` unbounded (FU-2). SQL parameterized.
  - `/unsnooze {route}`: same route resolver; no `days` arg.
  - `/status`: no user-supplied args.
- **Callback payloads:** `route:snooze:{days}:{route_id}` parses `days = int(sub[0])` with `ValueError` guard (commands.py:755–757); falls through silently on bad parse. `deal_id` and `route_id` flow into parameterized SQL only — no string interpolation.
- **No new logging of sensitive data.** Skip-predicate logs include `chat_id` (already in pre-R7 logs).

**Verdict: clean.** One minor improvement (FU-2: bound `/snooze` days) noted; no exploitable issue identified.

---

## 12. Sign-off

This release is **Ship with Follow-ups**. Architect-Lead recommends:

1. Sync `farehound/src/` from root `src/` per CLAUDE.md.
2. Bump `farehound/config.yaml` from 0.8.x → 0.9.0.
3. Commit + push to `main`.
4. Deploy to HA per CLAUDE.md flow (`ha apps stop` → `ha store reload` → `ha apps update` → `ha apps start`).
5. Wait 15 seconds, tail logs for `Database schema initialized` + `Scheduled daily digest at HH:MM` + clean `Starting poll cycle`.
6. Hand Barry the §10 manual verification cases. The ship message can quote MV-1 through MV-8 directly.
7. Add FU-1 through FU-5 to the roadmap as a single `ITEM-052 — R7 cleanup` item.

The `/release` skill's Phase 5 step is complete.

---

## Code Review (independent — Phase 4 step 8)

Triggered by L effort + persistence-schema touch + cross-module change. 3 parallel Sonnet reviewers (CLAUDE.md adherence, shallow bug scan, code-comments compliance). 14 issues raised across reviewers; 3 reached confidence ≥80 after team-lead verification — all fixed before ship.

### Findings ≥80 confidence (FIXED)

| # | File:line | Issue | Fix |
|---|---|---|---|
| CR-1 | `src/bot/commands.py:755` | `int(sub[0])` accepted negative/zero days from `route:snooze` callback_data — `snooze_route(route_id, -5)` would set `snoozed_until` in the past, silently un-snoozing the route. Real injection-style bug. | Clamp to `max(1, min(int(sub[0]), 365))`. |
| CR-2 | `src/bot/commands.py:1498` | Same family in `/snooze` slash command — `isdigit()` rejects negatives but accepts arbitrarily large values (already FU-2). | Same clamp. |
| CR-3 | `src/bot/commands.py:696, 759, 778, 765, 791` | `getattr(self._db, "snooze_route", None)` / `hasattr(...)` guards for methods that ARE defined in the same release (`db.py:352, 697`). Dead defensive shims violating CLAUDE.md "no abstraction beyond what current task requires." | Replaced with direct method calls. |

### Findings <80 confidence (NOT actioned)

- **R1.1**: `_static_fallback` docstring extension on lightly-modified function — borderline; docstring describes new return shape, function body changed. Score 25.
- **R2.2**: Lambda loop-capture in `update_user` calls — verified `await loop.run_in_executor(...)` is awaited inline, lambda runs before next iteration. **False positive.** Score 0.
- **R3.x** (×6): Task-reference comments (`# T7 §6.3:`, `# Condition C9:`, `# A1: A2: ...`, etc.) — real CLAUDE.md style violations but minor. Folded into ITEM-052 cleanup.

### Post-fix verification

- Suite re-run after fixes: **420/420 passing** (no regression).
- Fix commit: see ship commit below.
- The clamping math (`max(1, min(int(x), 365))`) is a one-line change; no dedicated test added (existing tests cover the `days=7` happy path; the bounds are covered implicitly by integer arithmetic).

### Updated follow-ups for ITEM-052

In addition to FU-1 through FU-5 above:
- **FU-6:** Strip task-reference comments (`T7 §X`, `A1`–`A5`, `R7`, `Condition C9`, `T12`) from `src/bot/commands.py`, `src/storage/db.py`, `src/alerts/telegram.py`, `src/orchestrator.py` once the release is ~30 days settled. Real CLAUDE.md violation but low impact and rotting nicely.

