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
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.bot.commands import _PARSE_PROMPT
from src.storage.db import Database
from src.storage.models import Route
from src.web import data as data_assembler
from src.web.auth import require_user

logger = logging.getLogger("farehound.web")

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))


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


def _register_html_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request, tg_user: dict = Depends(require_user)) -> HTMLResponse:
        # Default landing → routes
        return await routes(request, tg_user)

    @app.get("/deal/{deal_id}", response_class=HTMLResponse)
    async def deal_page(
        request: Request, deal_id: str, tg_user: dict = Depends(require_user)
    ) -> HTMLResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        deal = await asyncio.to_thread(data_assembler.assemble_deal, db, deal_id, user_id)
        if deal is None:
            raise HTTPException(status_code=404, detail="deal not found")
        return _TEMPLATES.TemplateResponse(request, "deal.html.j2", {"deal": deal})

    @app.get("/routes", response_class=HTMLResponse)
    async def routes(request: Request, tg_user: dict = Depends(require_user)) -> HTMLResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered — open the bot first")
        ctx = await asyncio.to_thread(data_assembler.assemble_routes, db, user_id)
        return _TEMPLATES.TemplateResponse(request, "routes.html.j2", ctx)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(
        request: Request, tg_user: dict = Depends(require_user)
    ) -> HTMLResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered — open the bot first")
        handle = "@" + str(tg_user.get("username") or tg_user.get("first_name") or "")
        ctx = await asyncio.to_thread(data_assembler.assemble_settings, db, user_id, handle)
        return _TEMPLATES.TemplateResponse(request, "settings.html.j2", ctx)


# ---------- API routes ----------


def _register_api_routes(app: FastAPI) -> None:
    @app.post("/api/routes/parse")
    async def parse_trip(
        body: dict = Body(...), tg_user: dict = Depends(require_user)
    ) -> JSONResponse:
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")

        api_key = app.state.anthropic_key
        model = app.state.anthropic_model
        if not api_key:
            raise HTTPException(status_code=503, detail="parser unavailable (no anthropic key)")

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        client = anthropic.AsyncAnthropic(api_key=api_key)
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=256,
                messages=[
                    {"role": "user", "content": _PARSE_PROMPT.format(today=today, user_text=text)}
                ],
            )
        except Exception as e:
            logger.exception("parse: anthropic call failed")
            raise HTTPException(status_code=502, detail=f"parser error: {e}") from None

        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=502, detail="parser returned non-JSON") from None

        return JSONResponse(parsed)

    @app.post("/api/routes")
    async def create_route(
        body: dict = Body(...), tg_user: dict = Depends(require_user)
    ) -> JSONResponse:
        db: Database = app.state.db
        user_id = _resolve_user_id(db, tg_user)
        if user_id is None:
            raise HTTPException(status_code=403, detail="user not registered")

        origin = (body.get("origin") or "").strip().upper()
        dest = (body.get("destination") or "").strip().upper()
        if not origin or not dest or len(origin) != 3 or len(dest) != 3:
            raise HTTPException(status_code=400, detail="origin and destination IATA codes required")

        route = Route(
            route_id=f"{origin.lower()}_{dest.lower()}_{uuid.uuid4().hex[:6]}",
            origin=origin,
            destination=dest,
            earliest_departure=body.get("earliest_departure"),
            latest_return=body.get("latest_return"),
            passengers=int(body.get("passengers") or 2),
            max_stops=int(body.get("max_stops") or 1),
            trip_duration_type=body.get("trip_duration_type"),
            trip_duration_days=body.get("trip_duration_days"),
            preferred_departure_days=body.get("preferred_departure_days"),
            preferred_return_days=body.get("preferred_return_days"),
            notes=body.get("notes"),
            active=True,
        )
        await asyncio.to_thread(db.upsert_route, route, user_id)
        return JSONResponse({
            "route_id": route.route_id,
            "first_poll_eta_seconds": None,  # follow-up: trigger immediate poll
        })

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
            await asyncio.to_thread(_update_user_safe, db, user_id, updates)

        return JSONResponse({"updated": list(updates.keys())})


# ---------- helpers ----------


def _route_belongs_to(db: Database, route_id: str, user_id: str) -> bool:
    row = db._conn.execute(
        "SELECT 1 FROM routes WHERE route_id = ? AND user_id = ?",
        [route_id, user_id],
    ).fetchone()
    return row is not None


def _update_user_safe(db: Database, user_id: str, updates: dict) -> None:
    """Wrapper around `db.update_user` that tolerates fields not yet in the allowlist.

    The `users.update_user` allowlist includes `preferences`; `baggage_needs` may not be
    a top-level column, so when not allowed, we fold it into preferences instead.
    """
    try:
        db.update_user(user_id, **updates)
    except Exception:
        # Fall back: stuff anything unknown into preferences JSON
        prefs = updates.pop("preferences", None)
        existing = db.get_user(user_id) or {}
        existing_prefs = existing.get("preferences") or {}
        if not isinstance(existing_prefs, dict):
            try:
                existing_prefs = json.loads(existing_prefs)
            except Exception:
                existing_prefs = {}
        if isinstance(prefs, str):
            try:
                prefs = json.loads(prefs)
            except Exception:
                prefs = {}
        existing_prefs.update(prefs or {})
        existing_prefs.update(updates)
        db.update_user(user_id, preferences=json.dumps(existing_prefs))


__all__ = ["create_app"]
