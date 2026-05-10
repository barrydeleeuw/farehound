"""FareHound Mini Web App — FastAPI factory + endpoint handlers.

HTML routes return Jinja-rendered pages; `/api/*` routes return JSON.
All routes (HTML and JSON) require a valid Telegram `initData` payload — see `auth.py`.

The web app boots in the same process as the bot (`src.orchestrator.main` runs
both via `asyncio.gather`), so they share the same SQLite handle and event loop.
DB calls are sync — the FastAPI handlers run them via `asyncio.to_thread` to
avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.storage.db import Database
from src.web import data as data_assembler
from src.web.auth import require_user

logger = logging.getLogger("farehound.web")

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))

# Cache-buster appended to static asset URLs (?v=...) so Telegram's WebApp
# WebView picks up new JS/CSS after a restart instead of serving stale files.
# Boot-time epoch — bumps on every container restart.
import time as _time
_TEMPLATES.env.globals["cache_buster"] = str(int(_time.time()))


def create_app(db: Database, anthropic_key: str | None, anthropic_model: str | None) -> FastAPI:
    """Build the FastAPI app, wired to the existing Database + Claude client."""
    app = FastAPI(title="FareHound Mini Web App", docs_url=None, redoc_url=None)

    # Static files: /static/style.css, /static/app.js
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    # Convenience: bot token must exist for HMAC validation. Validated at request time
    # (not boot) so missing tokens just produce 401s rather than crashing the process.
    app.state.db = db
    app.state.anthropic_key = anthropic_key
    app.state.anthropic_model = anthropic_model or "claude-sonnet-4-20250514"

    _register_html_routes(app)
    _register_api_routes(app)
    return app


# ---------- HTML routes ----------


def _resolve_user_id(db: Database, tg_user: dict) -> str | None:
    """Map Telegram user.id → users.user_id row, or None if unknown."""
    chat_id = str(tg_user.get("id"))
    user = db.get_user_by_chat_id(chat_id)
    if not user:
        return None
    return user.get("user_id")


def _bootstrap_response(request: Request, target: str) -> HTMLResponse:
    """Return the tiny bootstrap page that re-loads `target` with initData attached."""
    return _TEMPLATES.TemplateResponse(request, "_bootstrap.html.j2", {"target": target})


def _html_user_for_request(db: Database, request: Request) -> tuple[dict | None, str | None]:
    """Resolve (tg_user_dict, user_id) for an HTML route. Returns (None, None) when:
    - no initData supplied (caller should bootstrap)
    - initData invalid (caller should 401)
    - user not registered (caller should 401)

    Honours the `FAREHOUND_WEB_DEV_BYPASS_AUTH=1` env var for local testing.
    """
    # Dev bypass — short-circuits initData validation entirely.
    if os.environ.get("FAREHOUND_WEB_DEV_BYPASS_AUTH") == "1":
        chat_id = os.environ.get("FAREHOUND_WEB_DEV_USER_ID", "0")
        stub_user = {"id": int(chat_id) if chat_id.lstrip("-").isdigit() else 0,
                     "first_name": "DevUser"}
        user = db.get_user_by_chat_id(chat_id)
        return (stub_user, user.get("user_id") if user else None)

    init_data = request.query_params.get("tg") or ""
    if not init_data:
        return (None, None)
    try:
        from src.web.auth import validate_init_data
        tg_user = validate_init_data(init_data)
    except Exception:
        return ({}, None)  # initData was supplied but invalid → 401
    user = db.get_user_by_chat_id(str(tg_user.get("id")))
    return (tg_user, user.get("user_id") if user else None)


def _needs_bootstrap(request: Request) -> bool:
    """True when the caller hit an HTML route without initData (and no dev bypass).
    Used to decide between rendering the bootstrap page vs a 401."""
    if os.environ.get("FAREHOUND_WEB_DEV_BYPASS_AUTH") == "1":
        return False
    return not request.query_params.get("tg")


def _register_html_routes(app: FastAPI) -> None:
    # HTML pages: Telegram passes initData via the URL hash (#tgWebAppData=...),
    # which the server can't see. On the first GET we return a tiny bootstrap
    # page that reads `Telegram.WebApp.initData` and reloads with `?tg=...`.
    # On the second GET (with `?tg=` set), we validate, render with real data.
    # Same pattern for /, /deal/{id}, /routes, /settings.

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request) -> HTMLResponse:
        if _needs_bootstrap(request):
            return _bootstrap_response(request, "/routes")
        return await routes(request)

    @app.get("/deal/{deal_id}", response_class=HTMLResponse)
    async def deal_page(request: Request, deal_id: str) -> HTMLResponse:
        if _needs_bootstrap(request):
            return _bootstrap_response(request, f"/deal/{deal_id}")
        db: Database = app.state.db
        _, user_id = _html_user_for_request(db, request)
        if user_id is None:
            raise HTTPException(status_code=401, detail="initData invalid or user not registered")
        deal = await asyncio.to_thread(data_assembler.assemble_deal, db, deal_id, user_id)
        if deal is None:
            raise HTTPException(status_code=404, detail="deal not found")
        return _TEMPLATES.TemplateResponse(request, "deal.html.j2", {"deal": deal})

    @app.get("/routes", response_class=HTMLResponse)
    async def routes(request: Request) -> HTMLResponse:
        if _needs_bootstrap(request):
            return _bootstrap_response(request, "/routes")
        db: Database = app.state.db
        _, user_id = _html_user_for_request(db, request)
        if user_id is None:
            raise HTTPException(status_code=401, detail="initData invalid or user not registered")
        ctx = await asyncio.to_thread(data_assembler.assemble_routes, db, user_id)
        return _TEMPLATES.TemplateResponse(request, "routes.html.j2", ctx)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        if _needs_bootstrap(request):
            return _bootstrap_response(request, "/settings")
        db: Database = app.state.db
        tg_user, user_id = _html_user_for_request(db, request)
        if user_id is None:
            raise HTTPException(status_code=401, detail="initData invalid or user not registered")
        handle = "@" + str((tg_user or {}).get("username") or (tg_user or {}).get("first_name") or "")
        ctx = await asyncio.to_thread(data_assembler.assemble_settings, db, user_id, handle)
        return _TEMPLATES.TemplateResponse(request, "settings.html.j2", ctx)


# ---------- API routes ----------


def _register_api_routes(app: FastAPI) -> None:
    # ---- GET endpoints used by the HTML page JS to populate shells ----

    @app.get("/api/routes")
    async def get_routes(tg_user: dict = Depends(require_user)) -> JSONResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered — open the bot first")
        ctx = await asyncio.to_thread(data_assembler.assemble_routes, db, user_id)
        return JSONResponse(ctx)

    @app.get("/api/deals/{deal_id}")
    async def get_deal(deal_id: str, tg_user: dict = Depends(require_user)) -> JSONResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        deal = await asyncio.to_thread(data_assembler.assemble_deal, db, deal_id, user_id)
        if deal is None:
            raise HTTPException(status_code=404, detail="deal not found")
        return JSONResponse(deal)

    # ---- Action endpoints (mutations) ----
    # Note: trip creation is handled exclusively by the bot's /trip flow in
    # src/bot/commands.py — it has multi-turn disambiguation (clarifying
    # questions, IATA option pickers) that a one-shot HTTP endpoint can't match.
    # The Mini Web App's /routes page sends users back to chat for adds.

    @app.post("/api/routes/{route_id}/snooze")
    async def snooze_route(
        route_id: str, body: dict = Body(default={}), tg_user: dict = Depends(require_user)
    ) -> JSONResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")

        days_raw = body.get("days", 7)
        try:
            days = max(1, min(int(days_raw), 365))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="days must be an integer") from None

        # Verify the route belongs to this user before snoozing
        owned = await asyncio.to_thread(_route_belongs_to, db, route_id, user_id)
        if not owned:
            raise HTTPException(status_code=404, detail="route not found")

        await asyncio.to_thread(db.snooze_route, route_id, days)
        return JSONResponse({"snoozed_for_days": days})

    @app.post("/api/routes/{route_id}/unsnooze")
    async def unsnooze_route(
        route_id: str, tg_user: dict = Depends(require_user)
    ) -> JSONResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")
        owned = await asyncio.to_thread(_route_belongs_to, db, route_id, user_id)
        if not owned:
            raise HTTPException(status_code=404, detail="route not found")
        await asyncio.to_thread(db.unsnooze_route, route_id)
        return JSONResponse({"ok": True})

    @app.delete("/api/routes/{route_id}")
    async def delete_route(
        route_id: str, tg_user: dict = Depends(require_user)
    ) -> JSONResponse:
        """Soft-delete a route — sets active=0, stops polling, keeps history.
        Same semantic as the bot's /remove command."""
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")
        owned = await asyncio.to_thread(_route_belongs_to, db, route_id, user_id)
        if not owned:
            raise HTTPException(status_code=404, detail="route not found")
        await asyncio.to_thread(db.deactivate_route, route_id)
        return JSONResponse({"removed": True})

    @app.post("/api/deals/{deal_id}/feedback")
    async def deal_feedback(
        deal_id: str, body: dict = Body(...), tg_user: dict = Depends(require_user)
    ) -> JSONResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")

        feedback = (body.get("feedback") or "").strip().lower()
        if feedback not in ("booked", "watching", "dismissed"):
            raise HTTPException(status_code=400, detail="feedback must be booked|watching|dismissed")

        # Ownership check — users may only mutate their own deals.
        owned = await asyncio.to_thread(_deal_belongs_to, db, deal_id, user_id)
        if not owned:
            raise HTTPException(status_code=404, detail="deal not found")

        await asyncio.to_thread(db.update_deal_feedback, deal_id, feedback)
        return JSONResponse({"feedback": feedback})

    @app.get("/api/settings")
    async def get_settings(
        tg_user: dict = Depends(require_user),
    ) -> JSONResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")
        handle = "@" + str(tg_user.get("username") or tg_user.get("first_name") or "")
        ctx = await asyncio.to_thread(data_assembler.assemble_settings, db, user_id, handle)
        return JSONResponse(ctx["settings"])

    @app.patch("/api/settings")
    async def patch_settings(
        body: dict = Body(...), tg_user: dict = Depends(require_user)
    ) -> JSONResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")

        # Normalise into preferences JSON; baggage_needs is its own column on users.
        user = await asyncio.to_thread(db.get_user, user_id) or {}
        prefs = user.get("preferences") or {}
        if not isinstance(prefs, dict):
            try:
                prefs = json.loads(prefs)
            except Exception:
                prefs = {}

        updates: dict = {}
        if "baggage_needs" in body:
            v = str(body["baggage_needs"])
            if v not in ("carry_on_only", "one_checked", "two_checked"):
                raise HTTPException(status_code=400, detail="invalid baggage_needs")
            updates["baggage_needs"] = v
        for key in ("quiet_from", "quiet_to", "digest_time"):
            if key in body:
                prefs[key] = str(body[key])
        if prefs:
            updates["preferences"] = json.dumps(prefs)

        if updates:
            await asyncio.to_thread(db.update_user, user_id, **updates)

        return JSONResponse({"updated": list(updates.keys())})

    # ---- R9 ITEM-053: airport transport options (multi-mode editable) ----

    _VALID_MODES = {"drive", "train", "taxi", "uber", "bus", "metro", "ferry", "tram", "other"}
    _PER_PERSON_MODE_DEFAULTS = {"train", "bus", "metro", "ferry", "tram"}

    def _validate_airport_code(code: str) -> str:
        c = (code or "").strip().upper()
        if not c.isalpha() or len(c) != 3:
            raise HTTPException(status_code=400, detail="airport_code must be a 3-letter IATA code")
        return c

    def _validate_mode(mode: str) -> str:
        m = (mode or "").strip().lower()
        if m not in _VALID_MODES:
            raise HTTPException(status_code=400, detail=f"mode must be one of: {sorted(_VALID_MODES)}")
        return m

    @app.get("/api/airports/{code}/options")
    async def get_airport_options(
        code: str, tg_user: dict = Depends(require_user)
    ) -> JSONResponse:
        """List all transport options (incl. disabled) for an airport."""
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")
        airport = _validate_airport_code(code)
        opts = await asyncio.to_thread(
            db.get_transport_options, airport, user_id, include_disabled=True
        )
        override = await asyncio.to_thread(db.get_airport_override_mode, airport, user_id)
        return JSONResponse({
            "airport_code": airport,
            "options": opts,
            "override_mode": override,
        })

    @app.post("/api/airports/{code}/options")
    async def add_airport_option(
        code: str, body: dict = Body(...), tg_user: dict = Depends(require_user)
    ) -> JSONResponse:
        """Add (or replace) a transport option for an airport. User-driven."""
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")
        airport = _validate_airport_code(code)
        mode = _validate_mode(body.get("mode", ""))
        try:
            cost_eur = float(body["cost_eur"]) if body.get("cost_eur") is not None else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="cost_eur must be a number") from None
        time_min = body.get("time_min")
        if time_min is not None:
            try:
                time_min = max(0, int(time_min))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="time_min must be an integer") from None
        parking = body.get("parking_cost_per_day_eur")
        if parking is not None:
            try:
                parking = max(0.0, float(parking))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="parking_cost_per_day_eur must be a number") from None
        # Sensible default for cost_scales_with_pax based on mode; user can flip.
        scales_default = mode in _PER_PERSON_MODE_DEFAULTS
        scales = bool(body.get("cost_scales_with_pax", scales_default))
        label = (body.get("label") or "").strip() or None

        await asyncio.to_thread(
            db.add_transport_option,
            user_id=user_id, airport_code=airport, mode=mode,
            cost_eur=cost_eur, cost_scales_with_pax=scales,
            time_min=time_min, parking_cost_per_day_eur=parking,
            source="user_added", confidence="high", label=label, enabled=True,
        )
        return JSONResponse({"added": True, "airport_code": airport, "mode": mode})

    @app.put("/api/airports/{code}/options/{mode}")
    async def update_airport_option(
        code: str, mode: str, body: dict = Body(...),
        tg_user: dict = Depends(require_user),
    ) -> JSONResponse:
        """Patch any subset of {cost_eur, time_min, parking_cost_per_day_eur, enabled, label}."""
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")
        airport = _validate_airport_code(code)
        canonical_mode = _validate_mode(mode)
        kwargs: dict = {}
        if "cost_eur" in body and body["cost_eur"] is not None:
            try:
                kwargs["cost_eur"] = max(0.0, float(body["cost_eur"]))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="cost_eur must be a number") from None
        if "time_min" in body and body["time_min"] is not None:
            try:
                kwargs["time_min"] = max(0, int(body["time_min"]))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="time_min must be an integer") from None
        if "parking_cost_per_day_eur" in body:
            v = body["parking_cost_per_day_eur"]
            if v is None:
                kwargs["parking_cost_per_day_eur"] = 0.0
            else:
                try:
                    kwargs["parking_cost_per_day_eur"] = max(0.0, float(v))
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="parking must be a number") from None
        if "enabled" in body:
            kwargs["enabled"] = bool(body["enabled"])
        if "label" in body:
            kwargs["label"] = (body["label"] or "").strip() or None

        updated = await asyncio.to_thread(
            db.update_transport_option,
            user_id=user_id, airport_code=airport, mode=canonical_mode, **kwargs,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="option not found")
        return JSONResponse({"updated": True})

    @app.delete("/api/airports/{code}/options/{mode}")
    async def delete_airport_option(
        code: str, mode: str, tg_user: dict = Depends(require_user)
    ) -> JSONResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")
        airport = _validate_airport_code(code)
        canonical_mode = _validate_mode(mode)
        deleted = await asyncio.to_thread(
            db.delete_transport_option,
            user_id=user_id, airport_code=airport, mode=canonical_mode,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="option not found")
        return JSONResponse({"deleted": True})

    @app.put("/api/airports/{code}/override")
    async def set_airport_override(
        code: str, body: dict = Body(default={}),
        tg_user: dict = Depends(require_user),
    ) -> JSONResponse:
        """Set or clear the per-airport 'always use [mode]' preference. Pass mode=null to clear."""
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")
        airport = _validate_airport_code(code)
        raw_mode = body.get("mode")
        if raw_mode is None or raw_mode == "":
            mode_val = None
        else:
            mode_val = _validate_mode(str(raw_mode))
        await asyncio.to_thread(
            db.set_airport_override_mode,
            user_id=user_id, airport_code=airport, mode=mode_val,
        )
        return JSONResponse({"override_mode": mode_val})


# ---------- helpers ----------


def _route_belongs_to(db: Database, route_id: str, user_id: str) -> bool:
    row = db._conn.execute(
        "SELECT 1 FROM routes WHERE route_id = ? AND user_id = ?",
        [route_id, user_id],
    ).fetchone()
    return row is not None


def _deal_belongs_to(db: Database, deal_id: str, user_id: str) -> bool:
    row = db._conn.execute(
        "SELECT 1 FROM deals WHERE deal_id = ? AND user_id = ?",
        [deal_id, user_id],
    ).fetchone()
    return row is not None


__all__ = ["create_app"]
