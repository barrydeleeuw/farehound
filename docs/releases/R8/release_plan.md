# R8 — Mini Web App backend + thin Telegram

**Shipped:** 2026-05-10 as v0.10.0
**Built by:** team-lead solo (no agent team — see notes below)
**Branch:** `claude/focused-kapitsa-56740e`

## Solo build rationale

`/release` initially spun up a 3-agent team (Architect-Lead + Builder + Tester). After ~30s on Architect-Lead's Phase A, Barry called the size out as overkill for this scope, and the team-lead agreed: 1 new module + 4 message-template thin-outs is mostly contiguous code, hard to parallelize meaningfully, and re-deriving the parent conversation's full context (frontend choices, Option-B decision, endpoint contract from `frontend/README.md`) for 3 fresh agents would have cost ~80k tokens of duplicate pre-work each. Architect-Lead was shut down via `shutdown_request`. Solo build, with `/code-review` as the independent perspective at the end.

## Scope

Single-roadmap-item release wrapping up [ITEM-049](../../../ROADMAP.md). Frontend pass shipped earlier today at commit `e4dc24a`; this release adds the FastAPI backend that serves it, plus the Telegram-side change that flips the bot from "rich messages with optional Mini App" to "thin pings + Mini App is the primary surface" (Option B, locked 2026-05-10).

### What ships

1. **`src/web/`** — FastAPI app boots in the same process as the bot via `asyncio.gather` from `Orchestrator.start`. 12 endpoints (4 HTML, 8 JSON) listed below.
2. **`initData` HMAC validation** — `src/web/auth.py` validates Telegram WebApp signed payloads. Wrong-token rejected, expired (>24h) rejected, replay-from-the-future rejected, tampered fields rejected.
3. **Jinja templates** in `src/web/templates/` — port of the v0.9.0 frontend HTML with real data baked in. Static assets (`style.css`, `app.js`) copied to `src/web/static/`.
4. **Thin Telegram format** — `src/alerts/telegram.py` adds `_send_*_thin` variants for all 4 message types. Gated on `MINIAPP_URL` env var; when unset, falls back to the v0.9.0 rich format (no test breakage).
5. **HA add-on config** — new `miniapp_url` option in `ha-addon/config.yaml` and `farehound/config.yaml`. `run.sh` exports it + `TELEGRAM_BOT_TOKEN` for HMAC validation.
6. **Cloudflare Tunnel docs** at `docs/deployment/cloudflare-tunnel.md` — Barry runs the deploy himself.
7. **Tests** — `tests/test_web_auth.py` (14), `tests/test_web_endpoints.py` (19), `tests/test_telegram_thin.py` (8). Suite: 420 → **461** (+41).

## Endpoints

| Method | Path | Owner | Notes |
|---|---|---|---|
| `GET` | `/` | HTML | Falls through to `/routes` |
| `GET` | `/deal/{deal_id}` | HTML | Full deal detail, sparkline, alternatives, baggage |
| `GET` | `/routes` | HTML | Route list, summary, add-trip form |
| `GET` | `/settings` | HTML | Baggage / transport / quiet-hours / digest-time |
| `POST` | `/api/routes/parse` | JSON | Claude NL parse → structured trip |
| `POST` | `/api/routes` | JSON | Create route (first poll on next cycle) |
| `POST` | `/api/routes/{route_id}/snooze` | JSON | Wraps `db.snooze_route`, clamps days to [1, 365] |
| `POST` | `/api/routes/{route_id}/unsnooze` | JSON | Wraps `db.unsnooze_route` |
| `POST` | `/api/deals/{deal_id}/feedback` | JSON | Wraps `db.update_deal_feedback` |
| `GET` | `/api/settings` | JSON | Read-only settings dump |
| `PATCH` | `/api/settings` | JSON | Updates `users.preferences` JSON column |

All routes require valid `initData` via `x-telegram-init-data` header or `?tg=` query param. Local-dev bypass: `FAREHOUND_WEB_DEV_BYPASS_AUTH=1`.

## Architectural decisions (locked solo)

| Question | Decision | Rationale |
|---|---|---|
| Run-loop integration | `asyncio.gather(orchestrator, uvicorn.serve)` in same event loop | Shared SQLite handle, no IPC, simpler than s6-overlay two-service |
| HTML vs JSON routing | HTML at `/`, JSON at `/api/` | Convention matches user mental model |
| Static assets | `src/web/static/` (copies from `frontend/`) | Stays inside existing `src/` Docker COPY; no new sync step |
| NL parse prompt | Import `_PARSE_PROMPT` from `src/bot/commands.py` | Minimal change; if bot+web diverge, easy to extract later |
| Deps | `fastapi`, `uvicorn[standard]`, `jinja2` in `pyproject.toml` | Standard stack, no build pipeline |
| Web app port | `localhost:8081`, configurable via `FAREHOUND_WEB_PORT` | Cloudflare Tunnel proxies to it; never exposed directly |
| Auth bypass for dev | `FAREHOUND_WEB_DEV_BYPASS_AUTH=1` env var | Lets local previews work without manufacturing initData |

## What did NOT ship in this release

- **Immediate poll on route create** — `POST /api/routes` stores the route and returns. First poll happens on the next scheduled cron tick (up to 24h). The frontend's "checking prices…" placeholder stays until then. Follow-up if snappier UX is wanted.
- **Alternatives data on `/deal/{id}`** — the page renders correctly but the alternatives table is empty because R7's nearby-airport comparison cache lives in the orchestrator instance, not in the DB. Persisting the evaluated/competitive lists is a follow-up (small change to `orchestrator._latest_nearby_comparison`).
- **Real-data integration tests** — Tests use `FAREHOUND_WEB_DEV_BYPASS_AUTH` to skip HMAC. HMAC tests cover validation logic; endpoint tests cover contracts. End-to-end against real Telegram is out of scope.

## Pre-build conditions met

- ✅ All new code under `src/web/` — bot/orchestrator changes are minimal: 1 new method (`_run_web`), 1 lazy uvicorn import, 1 new task in `start()`.
- ✅ `src/alerts/telegram.py` changes are additive: feature-flag dispatch at the top of each `send_*` method + 4 new private `_send_*_thin` methods. No existing function refactored.
- ✅ No build pipeline. Pip install + uvicorn.
- ✅ Submit-only Claude parse (no debounce in the frontend, single endpoint server-side).
- ✅ `MINIAPP_URL` feature flag — backend can deploy before tunnel is live; rich format remains as fallback.
- ✅ Cloudflare Tunnel: doc only, Barry runs the actual deploy.
- ✅ Suite stays green: 420 → 461 (+41 new).

## Latent bug noted (not in scope)

`src/analysis/scorer.py:223` `hours = t_min / 60` crashes when `airport_transport.transport_time_min` is NULL. Pre-existing from v1.0.0 (`d635a64`, March 2026). Currently dormant in production. Add to [ITEM-052] cleanup or a dedicated hotfix.
