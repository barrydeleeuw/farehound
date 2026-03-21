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
- earliest_departure: YYYY-MM-DD (first day of the travel window)
- latest_return: YYYY-MM-DD (last day of the travel window)
- passengers: int (default 2)
- max_stops: int (default 1, 0 if user says "direct only" or "nonstop")
- notes: string or null
- needs_clarification: boolean (true if destination is a country or ambiguous region)
- clarification_question: string or null (ask which city if ambiguous)
- options: list of strings or null (suggested cities/airports to choose from)
- trip_duration_type: string or null — one of "weekend", "weeks", "days", "flexible", null
- trip_duration_days: int or null — length of trip in days
- preferred_departure_days: list of ints or null — preferred days of week to depart (0=Mon..6=Sun)
- preferred_return_days: list of ints or null — preferred days of week to return (0=Mon..6=Sun)

Duration type rules:
- "long weekend" or "weekend trip" → trip_duration_type="weekend", trip_duration_days=3, preferred_departure_days=[3,4], preferred_return_days=[0,6]
- "2 weeks" → trip_duration_type="weeks", trip_duration_days=14
- "10 days" → trip_duration_type="days", trip_duration_days=10
- "sometime in October" or "in May" with no specific dates → trip_duration_type="flexible"
- Specific dates like "Oct 18 - Nov 8" → trip_duration_type=null (exact dates given)

For "long weekend in May", set earliest_departure to May 1 and latest_return to May 31 (the search window), NOT the trip dates.

If the destination is a country (e.g. "Japan", "Mexico", "Spain"), set needs_clarification=true
and suggest the top 2-3 cities with their airport codes.

If dates have no year, assume the next occurrence of that date.
Today is {today}.

Text: {user_text}"""

_INTERPRET_SYSTEM = """\
You are FareHound, a friendly personal flight deal assistant. The user is messaging you via Telegram.

You help users:
- Add new flight routes to monitor
- Modify existing trips (change dates, destinations, passengers, stops)
- Remove routes they no longer need
- Check current prices on their monitored routes
- Answer general travel questions

Today is {today}.

The user's home airport is {home_airport}.

ACTIVE ROUTES:
{routes_summary}

CONVERSATION HISTORY:
{history}

Interpret the user's message and respond with JSON:
{{
  "intent": "add_trip" | "modify_trip" | "remove_trip" | "query_trips" | "query_prices" | "general_chat",
  "parameters": {{...}},
  "response_text": "your natural language response to the user"
}}

Intent-specific parameters:
- add_trip: {{"destination": "...", "origin": "..." or null, "earliest_departure": "YYYY-MM-DD", "latest_return": "YYYY-MM-DD", "passengers": int, "max_stops": int, "notes": "..."}}
- modify_trip: {{"route_id": "...", "changes": {{"earliest_departure": "...", "latest_return": "...", "passengers": int, ...}}}}
- remove_trip: {{"route_id": "...", "confirm": true/false}}
- query_trips: {{}}
- query_prices: {{"route_id": "..." or null}}
- general_chat: {{}}

For general_chat, just set response_text to your helpful answer. No parameters needed.

CRITICAL SAFETY RULES:
- NEVER return modify_trip or remove_trip intent for informational questions (e.g. "when's the best time to fly?", "how much does it cost?", "what's the weather like?").
- Only return add_trip, modify_trip, or remove_trip when the user EXPLICITLY asks to change, add, or remove something.
- A follow-up "yes", "ok", "sure", "yeah" after an informational response means "tell me more" or agreement with the information — it does NOT mean "change my data".
- Questions about travel (best time, prices, weather, tips) are ALWAYS general_chat or query_prices, never action intents.

When the user refers to a destination by name (e.g. "Japan", "Mexico"), match it to the active route if one exists.
When the user says "all of them" or similar, look at the conversation history to understand what they're referring to.
If modifying a trip, include all the changed fields in parameters.changes — use YYYY-MM-DD for dates."""


_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _format_date_display(route_or_pending: dict | Route) -> str:
    """Build a human-readable date/duration string for confirmation and /trips."""
    if isinstance(route_or_pending, Route):
        dur_type = route_or_pending.trip_duration_type
        dur_days = route_or_pending.trip_duration_days
        dep_days = route_or_pending.preferred_departure_days
        ret_days = route_or_pending.preferred_return_days
        earliest = route_or_pending.earliest_departure
        latest = route_or_pending.latest_return
    else:
        dur_type = route_or_pending.get("trip_duration_type")
        dur_days = route_or_pending.get("trip_duration_days")
        dep_days = route_or_pending.get("preferred_departure_days")
        ret_days = route_or_pending.get("preferred_return_days")
        earliest = route_or_pending.get("earliest_departure", "")
        latest = route_or_pending.get("latest_return", "")

    if dur_type == "weekend":
        dep_str = "/".join(_DAY_NAMES[d] for d in (dep_days or [3, 4]))
        ret_str = "/".join(_DAY_NAMES[d] for d in (ret_days or [0, 6]))
        period = _format_period(earliest, latest)
        return f"Long weekends ({dep_str}-{ret_str}) throughout {period}"
    elif dur_type == "weeks" and dur_days:
        weeks = dur_days // 7
        label = f"{weeks} week{'s' if weeks > 1 else ''}"
        period = _format_period(earliest, latest)
        return f"{label} trips throughout {period}"
    elif dur_type == "days" and dur_days:
        period = _format_period(earliest, latest)
        return f"{dur_days}-day trips throughout {period}"
    elif dur_type == "flexible":
        period = _format_period(earliest, latest)
        return f"Flexible dates in {period}"
    else:
        return f"{earliest} → {latest}"


def _format_period(earliest, latest) -> str:
    """Format a date range as 'May 2026' or 'May-Jun 2026'."""
    from datetime import date as date_type
    if not earliest:
        return str(latest) if latest else ""
    if isinstance(earliest, str):
        try:
            earliest = date_type.fromisoformat(earliest)
        except (ValueError, TypeError):
            return f"{earliest} → {latest}"
    if isinstance(latest, str) and latest:
        try:
            latest = date_type.fromisoformat(latest)
        except (ValueError, TypeError):
            return f"{earliest} → {latest}"
    if not latest:
        return earliest.strftime("%b %Y")
    if earliest.year == latest.year and earliest.month == latest.month:
        return earliest.strftime("%b %Y")
    if earliest.year == latest.year:
        return f"{earliest.strftime('%b')}-{latest.strftime('%b')} {earliest.year}"
    return f"{earliest.strftime('%b %Y')} - {latest.strftime('%b %Y')}"


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
        self._last_intent: dict[str, str] = {}  # chat_id -> last intent type
        self._conversation_history: dict[str, list[dict]] = {}  # chat_id -> [{role, text}]
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
        params = {"offset": self._offset, "timeout": 30, "allowed_updates": '["message","callback_query"]'}
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        updates = data.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    def _add_history(self, chat_id: str, role: str, text: str) -> None:
        history = self._conversation_history.setdefault(chat_id, [])
        history.append({"role": role, "text": text})
        # Keep last 5 messages
        if len(history) > 5:
            self._conversation_history[chat_id] = history[-5:]

    def _get_history_text(self, chat_id: str) -> str:
        history = self._conversation_history.get(chat_id, [])
        if not history:
            return "(no prior messages)"
        return "\n".join(f"{m['role']}: {m['text']}" for m in history)

    async def _handle_update(self, update: dict, client: httpx.AsyncClient) -> None:
        callback = update.get("callback_query")
        if callback:
            await self._handle_callback(callback, client)
            return

        msg = update.get("message")
        if not msg:
            return
        text = (msg.get("text") or "").strip()
        chat_id = str(msg["chat"]["id"])

        if not text:
            return

        # Slash commands as shortcuts
        if text.startswith("/"):
            cmd = text.split()[0].lower()
            if cmd in ("/start", "/help"):
                await self._handle_help(chat_id, client)
                return
            elif cmd == "/trips":
                await self._handle_trips(chat_id, client)
                return
            elif cmd == "/trip":
                user_text = text[5:].strip()
                if user_text:
                    await self._handle_trip(user_text, chat_id, client)
                else:
                    await self._handle_trip("", chat_id, client)
                return
            elif cmd == "/remove":
                await self._handle_remove(text[7:].strip(), chat_id, client)
                return
            elif cmd == "/yes":
                await self._handle_yes(chat_id, client)
                return
            elif cmd == "/no":
                await self._handle_no(chat_id, client)
                return

        # Guard: casual affirmatives after informational responses should NOT
        # trigger pending confirmations — re-interpret them instead.
        _CASUAL_AFFIRMATIVES = {"yes", "yeah", "yep", "ok", "okay", "sure", "yea", "right", "correct"}
        if text.lower().strip() in _CASUAL_AFFIRMATIVES:
            last = self._last_intent.get(chat_id)
            if last in ("general_chat", "query_prices", "query_trips", None):
                # Clear any stale pending state — this is not a confirmation
                self._pending.pop(chat_id, None)

        # Non-command text: route through Claude for interpretation
        self._add_history(chat_id, "user", text)
        await self._interpret_message(text, chat_id, client)

    async def _handle_callback(self, callback: dict, client: httpx.AsyncClient) -> None:
        callback_id = callback.get("id")
        data = callback.get("data", "")
        message = callback.get("message", {})

        parts = data.split(":", 1)
        if len(parts) != 2:
            return
        action, deal_id = parts

        if action == "book":
            self._db.update_deal_feedback(deal_id, "booked")
            answer_text = "Marked as booked!"
            suffix = "\n\n✅ Marked as booked!"
        elif action == "dismiss":
            self._db.update_deal_feedback(deal_id, "dismissed")
            answer_text = "Dismissed"
            suffix = "\n\n👎 Dismissed"
        else:
            return

        # Answer the callback to dismiss the loading spinner
        answer_url = f"{TELEGRAM_API}/bot{self._bot_token}/answerCallbackQuery"
        try:
            await client.post(answer_url, json={
                "callback_query_id": callback_id,
                "text": answer_text,
            })
        except Exception:
            logger.exception("Failed to answer callback query")

        # Edit original message to append feedback confirmation
        original_text = message.get("text", "")
        chat_id = str(message.get("chat", {}).get("id", ""))
        message_id = message.get("message_id")
        if chat_id and message_id and original_text:
            edit_url = f"{TELEGRAM_API}/bot{self._bot_token}/editMessageText"
            try:
                await client.post(edit_url, json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": original_text + suffix,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                })
            except Exception:
                logger.exception("Failed to edit message after callback")

    async def _interpret_message(self, text: str, chat_id: str, client: httpx.AsyncClient) -> None:
        loop = asyncio.get_running_loop()
        routes = await loop.run_in_executor(None, self._db.get_active_routes)

        from src.utils.airports import route_name

        # Build routes summary
        if routes:
            lines = []
            for r in routes:
                name = route_name(r.origin, r.destination)
                dates = ""
                if r.earliest_departure:
                    dates = f" ({r.earliest_departure}"
                    if r.latest_return:
                        dates += f" to {r.latest_return}"
                    dates += ")"
                lines.append(f"- {r.route_id}: {name}{dates}, {r.passengers} pax")
            routes_summary = "\n".join(lines)
        else:
            routes_summary = "(no active routes)"

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        system_prompt = _INTERPRET_SYSTEM.format(
            today=today,
            home_airport=self._home_airport,
            routes_summary=routes_summary,
            history=self._get_history_text(chat_id),
        )

        try:
            ai_client = anthropic.AsyncAnthropic(api_key=self._anthropic_key)
            resp = await ai_client.messages.create(
                model=self._anthropic_model,
                max_tokens=512,
                system=system_prompt,
                messages=[{"role": "user", "content": text}],
            )
            raw = resp.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()
            result = json.loads(raw)
        except Exception:
            logger.exception("Failed to interpret message")
            await self._send(client, chat_id, "Sorry, I didn't understand that. Try /help for commands.")
            return

        intent = result.get("intent", "general_chat")
        params = result.get("parameters", {})
        response_text = result.get("response_text", "")

        # Track last intent per chat for safety guards
        self._last_intent[chat_id] = intent

        # Clear stale pending state after non-action intents
        if intent in ("general_chat", "query_prices", "query_trips"):
            self._pending.pop(chat_id, None)

        if intent == "general_chat":
            self._add_history(chat_id, "assistant", response_text)
            await self._send(client, chat_id, response_text)

        elif intent == "add_trip":
            destination = params.get("destination")
            if not destination:
                self._add_history(chat_id, "assistant", response_text)
                await self._send(client, chat_id, response_text)
                return
            origin = params.get("origin") or self._home_airport
            earliest = params.get("earliest_departure", "")
            latest = params.get("latest_return", "")
            passengers = params.get("passengers", 2)
            max_stops = params.get("max_stops", 1)
            notes = params.get("notes") or ""

            pending = {
                "action": "add",
                "origin": origin,
                "destination": destination,
                "earliest_departure": earliest,
                "latest_return": latest,
                "passengers": passengers,
                "max_stops": max_stops,
                "notes": notes,
                "trip_duration_type": params.get("trip_duration_type"),
                "trip_duration_days": params.get("trip_duration_days"),
                "preferred_departure_days": params.get("preferred_departure_days"),
                "preferred_return_days": params.get("preferred_return_days"),
            }
            self._pending[chat_id] = pending

            stops_str = "direct only" if max_stops == 0 else f"max {max_stops} stop{'s' if max_stops > 1 else ''}"
            date_display = _format_date_display(pending)
            msg = (
                f"Add route: *{route_name(origin, destination)}*\n"
                f"📅 {date_display}\n"
                f"👥 {passengers} pax | {stops_str}"
            )
            if notes:
                msg += f"\n📝 {notes}"
            msg += "\n\nReply /yes or /no"
            self._add_history(chat_id, "assistant", msg)
            await self._send(client, chat_id, msg)

        elif intent == "modify_trip":
            route_id = params.get("route_id")
            changes = params.get("changes", {})
            if not route_id or not changes:
                self._add_history(chat_id, "assistant", response_text)
                await self._send(client, chat_id, response_text)
                return
            # Verify route exists
            matching = [r for r in routes if r.route_id == route_id]
            if not matching:
                msg = f"I couldn't find route `{route_id}`. Use /trips to see your routes."
                self._add_history(chat_id, "assistant", msg)
                await self._send(client, chat_id, msg)
                return
            r = matching[0]
            self._pending[chat_id] = {
                "action": "modify",
                "route_id": route_id,
                "changes": changes,
            }
            # Build a human-readable summary of changes
            change_lines = []
            for k, v in changes.items():
                label = k.replace("_", " ").title()
                change_lines.append(f"  {label}: {v}")
            msg = (
                f"Modify *{route_name(r.origin, r.destination)}*:\n"
                + "\n".join(change_lines)
                + "\n\nReply /yes or /no"
            )
            self._add_history(chat_id, "assistant", msg)
            await self._send(client, chat_id, msg)

        elif intent == "remove_trip":
            route_id = params.get("route_id")
            if not route_id:
                self._add_history(chat_id, "assistant", response_text)
                await self._send(client, chat_id, response_text)
                return
            matching = [r for r in routes if r.route_id == route_id]
            if not matching:
                msg = f"I couldn't find route `{route_id}`."
                self._add_history(chat_id, "assistant", msg)
                await self._send(client, chat_id, msg)
                return
            r = matching[0]
            self._pending[chat_id] = {"action": "remove", "route_id": r.route_id}
            msg = f"Remove *{route_name(r.origin, r.destination)}*? Reply /yes or /no"
            self._add_history(chat_id, "assistant", msg)
            await self._send(client, chat_id, msg)

        elif intent == "query_trips":
            await self._handle_trips(chat_id, client)

        elif intent == "query_prices":
            route_id = params.get("route_id")
            await self._handle_price_query(route_id, routes, chat_id, client)

    async def _handle_price_query(
        self, route_id: str | None, routes: list[Route], chat_id: str, client: httpx.AsyncClient
    ) -> None:
        from src.utils.airports import route_name

        loop = asyncio.get_running_loop()

        if route_id:
            target_routes = [r for r in routes if r.route_id == route_id]
        else:
            target_routes = routes

        if not target_routes:
            msg = "No matching routes found. Use /trips to see your routes."
            self._add_history(chat_id, "assistant", msg)
            await self._send(client, chat_id, msg)
            return

        lines = []
        for r in target_routes:
            name = route_name(r.origin, r.destination)
            cheapest = await loop.run_in_executor(None, self._db.get_cheapest_recent_snapshot, r.route_id)
            history = await loop.run_in_executor(None, self._db.get_price_history, r.route_id)

            if cheapest and cheapest.lowest_price:
                price = f"€{float(cheapest.lowest_price):,.0f}"
                line = f"*{name}*: {price}/pp"
                if history and history.get("avg_price"):
                    avg = history["avg_price"]
                    diff = float(cheapest.lowest_price) - avg
                    if diff < 0:
                        line += f" (€{abs(diff):,.0f} below avg)"
                    else:
                        line += f" (€{diff:,.0f} above avg)"
            else:
                line = f"*{name}*: no prices yet"
            lines.append(line)

        msg = "\n".join(lines)
        self._add_history(chat_id, "assistant", msg)
        await self._send(client, chat_id, msg)

    async def _handle_help(self, chat_id: str, client: httpx.AsyncClient) -> None:
        await self._send(client, chat_id, (
            "🐕 *FareHound Bot*\n\n"
            "I monitor flight prices and alert you when deals are good.\n\n"
            "Just talk to me naturally! For example:\n"
            "  \"Track flights to Tokyo in October\"\n"
            "  \"Push Mexico to February\"\n"
            "  \"How's Japan looking?\"\n"
            "  \"When's the best time to fly to Bali?\"\n\n"
            "*Shortcuts:*\n"
            "`/trip Tokyo, Oct 18 - Nov 8, 2 people` — add a route\n"
            "`/trips` — list your routes with prices\n"
            "`/remove Tokyo` — stop monitoring a route\n"
            "`/yes` `/no` — confirm or cancel pending actions"
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
        trip_duration_type = parsed.get("trip_duration_type")
        trip_duration_days = parsed.get("trip_duration_days")
        preferred_departure_days = parsed.get("preferred_departure_days")
        preferred_return_days = parsed.get("preferred_return_days")

        pending = {
            "action": "add",
            "origin": origin,
            "destination": destination,
            "earliest_departure": earliest,
            "latest_return": latest,
            "passengers": passengers,
            "max_stops": max_stops,
            "notes": notes,
            "trip_duration_type": trip_duration_type,
            "trip_duration_days": trip_duration_days,
            "preferred_departure_days": preferred_departure_days,
            "preferred_return_days": preferred_return_days,
        }
        self._pending[chat_id] = pending

        stops_str = "direct only" if max_stops == 0 else f"max {max_stops} stop{'s' if max_stops > 1 else ''}"
        date_display = _format_date_display(pending)
        msg = (
            f"Add route: *{route_name(origin, destination)}*\n"
            f"📅 {date_display}\n"
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
            dates = _format_date_display(r)

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
        from src.utils.airports import route_name

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
                trip_duration_type=pending.get("trip_duration_type"),
                trip_duration_days=pending.get("trip_duration_days"),
                preferred_departure_days=pending.get("preferred_departure_days"),
                preferred_return_days=pending.get("preferred_return_days"),
            )
            await loop.run_in_executor(None, self._db.upsert_route, route)

            if self._reload_callback:
                await self._reload_callback()

            await self._send(client, chat_id, f"Route added: {route_name(route.origin, route.destination)}")

        elif pending["action"] == "remove":
            await loop.run_in_executor(None, self._db.deactivate_route, pending["route_id"])

            if self._reload_callback:
                await self._reload_callback()

            # Parse origin/destination from route_id (format: origin_destination)
            parts = pending["route_id"].split("_", 1)
            if len(parts) == 2:
                removed_name = route_name(parts[0], parts[1])
            else:
                removed_name = pending["route_id"]
            await self._send(client, chat_id, f"Route removed: {removed_name}")

        elif pending["action"] == "modify":
            route_id = pending["route_id"]
            changes = pending["changes"]
            updated = await loop.run_in_executor(
                None, lambda: self._db.update_route(route_id, **changes)
            )

            if self._reload_callback:
                await self._reload_callback()

            if updated:
                routes = await loop.run_in_executor(None, self._db.get_active_routes)
                r = next((r for r in routes if r.route_id == route_id), None)
                if r:
                    await self._send(client, chat_id, f"Updated *{route_name(r.origin, r.destination)}*.")
                else:
                    await self._send(client, chat_id, f"Route `{route_id}` updated.")
            else:
                await self._send(client, chat_id, f"Could not update route `{route_id}`.")

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
