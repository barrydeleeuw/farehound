from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import anthropic
import httpx

from src.storage.db import Database
from src.storage.models import Route

logger = logging.getLogger("farehound.bot")

TELEGRAM_API = "https://api.telegram.org"

_PARSE_PROMPT = """\
Extract flight route from this text. Return ONLY valid JSON with these fields:
- origin: IATA airport code or null if not specified
- destination: IATA airport code (use city codes like TYO for Tokyo when multiple airports)
- earliest_departure: YYYY-MM-DD
- latest_return: YYYY-MM-DD
- passengers: int (default 2)
- max_stops: int (default 1, 0 if user says "direct only" or "nonstop")
- notes: string or null
- needs_clarification: boolean (true if destination is a country or ambiguous region)
- clarification_question: string or null (ask which city if ambiguous)
- options: list of strings or null (suggested cities/airports to choose from)

If the destination is a country (e.g. "Japan", "Mexico", "Spain"), set needs_clarification=true
and suggest the top 2-3 cities with their airport codes.

If dates have no year, assume the next occurrence of that date.
Today is {today}.

Text: {user_text}"""


class TripBot:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        db: Database,
        anthropic_api_key: str,
        anthropic_model: str,
        home_airport: str,
        reload_callback=None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._db = db
        self._anthropic_key = anthropic_api_key
        self._anthropic_model = anthropic_model
        self._home_airport = home_airport
        self._reload_callback = reload_callback
        self._offset: int = 0
        self._pending: dict[str, dict] = {}  # chat_id -> pending confirmation
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info("TripBot polling started")
        async with httpx.AsyncClient(timeout=40.0) as client:
            while self._running:
                try:
                    updates = await self._get_updates(client)
                    for update in updates:
                        await self._handle_update(update, client)
                except httpx.TimeoutException:
                    continue
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("TripBot polling error")
                    await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False

    async def _get_updates(self, client: httpx.AsyncClient) -> list[dict]:
        url = f"{TELEGRAM_API}/bot{self._bot_token}/getUpdates"
        params = {"offset": self._offset, "timeout": 30, "allowed_updates": '["message"]'}
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        updates = data.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    async def _handle_update(self, update: dict, client: httpx.AsyncClient) -> None:
        msg = update.get("message")
        if not msg:
            return
        text = (msg.get("text") or "").strip()
        chat_id = str(msg["chat"]["id"])

        # Handle replies to clarification questions (not commands)
        if not text.startswith("/"):
            pending = self._pending.get(chat_id)
            if pending and pending.get("action") == "clarify":
                await self._handle_clarification_reply(text, chat_id, client)
            return

        cmd = text.split()[0].lower()
        if cmd in ("/start", "/help"):
            await self._handle_help(chat_id, client)
        elif cmd == "/trips":
            await self._handle_trips(chat_id, client)
        elif cmd == "/trip":
            await self._handle_trip(text[5:].strip(), chat_id, client)
        elif cmd == "/remove":
            await self._handle_remove(text[7:].strip(), chat_id, client)
        elif cmd == "/yes":
            await self._handle_yes(chat_id, client)
        elif cmd == "/no":
            await self._handle_no(chat_id, client)

    async def _handle_help(self, chat_id: str, client: httpx.AsyncClient) -> None:
        await self._send(client, chat_id, (
            "🐕 *FareHound Bot*\n\n"
            "I monitor flight prices and alert you when deals are good.\n\n"
            "*Commands:*\n"
            "`/trip Tokyo, Oct 18 - Nov 8, 2 people`\n"
            "  Add a route to monitor. I'll figure out the airports and dates.\n\n"
            "`/trip Alicante, end of June, 2 weeks, direct only`\n"
            "  You can say 'direct only' or 'max 2 stops'.\n\n"
            "`/trips`\n"
            "  List your active routes with latest prices.\n\n"
            "`/remove Tokyo` or `/remove ams-nrt`\n"
            "  Stop monitoring a route. Just `/remove` to see options.\n\n"
            "I'll send you alerts when I find genuinely good deals — "
            "not every price check, only new lows or confirmed bargains."
        ), parse_mode="Markdown")

    async def _handle_trip(self, user_text: str, chat_id: str, client: httpx.AsyncClient) -> None:
        if not user_text:
            await self._send(client, chat_id, (
                "Tell me where you want to go! Examples:\n\n"
                "`/trip Tokyo, Oct 18 - Nov 8, 2 people`\n"
                "`/trip Mexico City, end of December, 3 weeks`\n"
                "`/trip Alicante, late June, 2 weeks, direct only`"
            ), parse_mode="Markdown")
            return

        parsed = await self._parse_route(user_text)
        if parsed is None:
            await self._send(client, chat_id, "Sorry, I couldn't parse that. Try: /trip Tokyo, Oct 18 - Nov 8, 2 people")
            return

        # Handle ambiguous destinations (countries, regions)
        if parsed.get("needs_clarification"):
            question = parsed.get("clarification_question", "Which city did you mean?")
            options = parsed.get("options", [])
            if options:
                question += "\n\n" + "\n".join(f"• {opt}" for opt in options)
            question += "\n\nJust reply with the city name."
            self._pending[chat_id] = {
                "action": "clarify",
                "original_text": user_text,
                "parsed": parsed,
            }
            await self._send(client, chat_id, question)
            return

        origin = parsed.get("origin") or self._home_airport
        destination = parsed.get("destination")
        if not destination:
            await self._send(client, chat_id, "I couldn't determine the destination. Please try again.")
            return

        from src.utils.airports import route_name

        earliest = parsed.get("earliest_departure", "")
        latest = parsed.get("latest_return", "")
        passengers = parsed.get("passengers", 2)
        max_stops = parsed.get("max_stops", 1)
        notes = parsed.get("notes") or ""

        self._pending[chat_id] = {
            "action": "add",
            "origin": origin,
            "destination": destination,
            "earliest_departure": earliest,
            "latest_return": latest,
            "passengers": passengers,
            "max_stops": max_stops,
            "notes": notes,
        }

        stops_str = "direct only" if max_stops == 0 else f"max {max_stops} stop{'s' if max_stops > 1 else ''}"
        msg = (
            f"Add route: *{route_name(origin, destination)}*\n"
            f"📅 {earliest} → {latest}\n"
            f"👥 {passengers} pax | {stops_str}"
        )
        if notes:
            msg += f"\n📝 {notes}"
        msg += "\n\nReply /yes or /no"
        await self._send(client, chat_id, msg)

    async def _handle_clarification_reply(self, reply: str, chat_id: str, client: httpx.AsyncClient) -> None:
        """User replied to a clarification question — re-parse with the specific city."""
        pending = self._pending.pop(chat_id, None)
        if not pending:
            return
        original = pending.get("original_text", "")
        # Replace the ambiguous part with the user's specific answer
        new_text = f"{reply}, {original}"
        await self._handle_trip(new_text, chat_id, client)

    async def _handle_trips(self, chat_id: str, client: httpx.AsyncClient) -> None:
        from src.utils.airports import airport_name, route_name

        loop = asyncio.get_running_loop()
        routes = await loop.run_in_executor(None, self._db.get_active_routes)

        if not routes:
            await self._send(client, chat_id, "No active routes. Add one with /trip")
            return

        lines = ["✈️ *Your Routes*\n"]
        for r in routes:
            cheapest = await loop.run_in_executor(None, self._db.get_cheapest_recent_snapshot, r.route_id)

            name = route_name(r.origin, r.destination)
            dates = ""
            if r.earliest_departure:
                dates = f"{r.earliest_departure}"
                if r.latest_return:
                    dates += f" → {r.latest_return}"

            price_line = ""
            if cheapest and cheapest.lowest_price:
                price_line = f"💰 €{float(cheapest.lowest_price):,.0f}"
                if cheapest.outbound_date and cheapest.return_date:
                    out = cheapest.outbound_date
                    ret = cheapest.return_date
                    if hasattr(out, 'strftime'):
                        out = out.strftime("%b %d")
                    if hasattr(ret, 'strftime'):
                        ret = ret.strftime("%b %d")
                    price_line += f" ({out} → {ret})"
                since = datetime.now(UTC) - timedelta(days=1)
                deals = await loop.run_in_executor(None, self._db.get_deals_since, r.route_id, since)
                if deals and deals[0].score:
                    price_line += f" (score: {float(deals[0].score):.2f})"
            else:
                price_line = "⏳ Waiting for first price check"

            stops = "direct" if r.max_stops == 0 else f"max {r.max_stops} stop{'s' if r.max_stops > 1 else ''}"
            route_block = (
                f"*{name}*\n"
                f"  📅 {dates}\n"
                f"  👥 {r.passengers} pax | {stops}\n"
                f"  {price_line}"
            )

            # Show cheapest nearby airport if available
            best_alt = None
            if cheapest and cheapest.lowest_price and hasattr(self._db, "get_nearby_snapshots"):
                nearby_snaps = await loop.run_in_executor(
                    None, self._db.get_nearby_snapshots, r.route_id, r.origin
                )
                primary_price = float(cheapest.lowest_price)
                for alt in nearby_snaps:
                    alt_price = float(alt["lowest_price"])
                    if alt_price < primary_price:
                        origin_code = alt["airport_code"].upper()
                        if best_alt is None or alt_price < best_alt[1]:
                            best_alt = (airport_name(origin_code), alt_price)
            if best_alt:
                route_block += f"\n  🟢 Cheaper from {best_alt[0]}: €{best_alt[1]:,.0f}/pp"

            route_block += f"\n  ID: `{r.route_id}`"
            lines.append(route_block)

        await self._send(client, chat_id, "\n\n".join(lines))

    async def _handle_remove(self, query: str, chat_id: str, client: httpx.AsyncClient) -> None:
        from src.utils.airports import route_name, AIRPORTS

        if not query:
            # Show routes with IDs for easy removal
            loop = asyncio.get_running_loop()
            routes = await loop.run_in_executor(None, self._db.get_active_routes)
            if not routes:
                await self._send(client, chat_id, "No active routes to remove.")
                return
            lines = ["Which route do you want to remove?\n"]
            for r in routes:
                lines.append(f"  `/remove {r.route_id}` — {route_name(r.origin, r.destination)}")
            await self._send(client, chat_id, "\n".join(lines))
            return

        loop = asyncio.get_running_loop()
        routes = await loop.run_in_executor(None, self._db.get_active_routes)

        # Match by route_id, destination IATA, or destination city name
        query_upper = query.upper().strip()
        query_lower = query.lower().strip()
        match = [r for r in routes if (
            r.route_id == query_lower
            or r.route_id == query_upper
            or r.destination == query_upper
            or AIRPORTS.get(r.destination, "").lower() == query_lower
        )]

        if not match:
            await self._send(client, chat_id, f"No route matching '{query}'. Use /remove to see options.")
            return

        if len(match) > 1:
            lines = ["Multiple matches — be more specific:\n"]
            for r in match:
                lines.append(f"  `/remove {r.route_id}` — {route_name(r.origin, r.destination)}")
            await self._send(client, chat_id, "\n".join(lines))
            return

        r = match[0]
        self._pending[chat_id] = {"action": "remove", "route_id": r.route_id}
        await self._send(client, chat_id, f"Remove *{route_name(r.origin, r.destination)}*? Reply /yes or /no")

    async def _handle_yes(self, chat_id: str, client: httpx.AsyncClient) -> None:
        pending = self._pending.pop(chat_id, None)
        if not pending:
            await self._send(client, chat_id, "Nothing pending to confirm.")
            return

        loop = asyncio.get_running_loop()

        if pending["action"] == "add":
            route_id = f"{pending['origin'].lower()}_{pending['destination'].lower()}"
            route = Route(
                route_id=route_id,
                origin=pending["origin"],
                destination=pending["destination"],
                earliest_departure=pending["earliest_departure"] or None,
                latest_return=pending["latest_return"] or None,
                passengers=pending["passengers"],
                notes=pending["notes"],
                active=True,
            )
            await loop.run_in_executor(None, self._db.upsert_route, route)

            if self._reload_callback:
                await self._reload_callback()

            await self._send(client, chat_id, f"Route added: {route.origin} → {route.destination}")

        elif pending["action"] == "remove":
            await loop.run_in_executor(None, self._db.deactivate_route, pending["route_id"])

            if self._reload_callback:
                await self._reload_callback()

            await self._send(client, chat_id, f"Route `{pending['route_id']}` removed.")

    async def _handle_no(self, chat_id: str, client: httpx.AsyncClient) -> None:
        self._pending.pop(chat_id, None)
        await self._send(client, chat_id, "Cancelled.")

    async def _parse_route(self, user_text: str) -> dict | None:
        try:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            client = anthropic.AsyncAnthropic(api_key=self._anthropic_key)
            resp = await client.messages.create(
                model=self._anthropic_model,
                max_tokens=256,
                messages=[{"role": "user", "content": _PARSE_PROMPT.format(today=today, user_text=user_text)}],
            )
            text = resp.content[0].text.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            return json.loads(text)
        except Exception:
            logger.exception("Failed to parse route text")
            return None

    async def _send(self, client: httpx.AsyncClient, chat_id: str, text: str, parse_mode: str = "Markdown") -> None:
        url = f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        except Exception:
            logger.exception("Failed to send Telegram message")
