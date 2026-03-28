from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import anthropic
import httpx

from src.analysis.nearby_airports import transport_total
from src.storage.db import Database
from src.storage.models import Route

logger = logging.getLogger("farehound.bot")

TELEGRAM_API = "https://api.telegram.org"

_PARSE_PROMPT = """\
Extract flight route from this text. Return ONLY valid JSON with these fields:
- origin: IATA airport code or null if not specified
- destination: IATA airport code (use city codes like TYO for Tokyo when multiple airports)
- earliest_departure: YYYY-MM-DD (first day of the search window)
- latest_return: YYYY-MM-DD (last day of the search window)
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

CRITICAL — Date window rules (earliest_departure and latest_return):
The search window must stay close to what the user asked for. Do NOT expand to full months.

1. SPECIFIC DATE: "departure October 18" or "leaving on Oct 18"
   → earliest_departure = Oct 16, latest_return = Oct 20 + trip_duration_days
   (±2 days around the stated date)

2. APPROXIMATE DATE: "departure around October 18" or "mid-October"
   → earliest_departure = Oct 11, latest_return = Oct 25 + trip_duration_days
   (±1 week around the stated date)

3. VAGUE/MONTH ONLY: "sometime in October" or "in May"
   → earliest_departure = Oct 1, latest_return = Oct 31
   (full month, trip_duration_type="flexible")

4. WEEKEND: "long weekend in May"
   → earliest_departure = May 1, latest_return = May 31

5. EXACT RANGE: "Oct 18 - Nov 8"
   → earliest_departure = Oct 18, latest_return = Nov 8 (exact, no expansion)

Examples:
- "Japan for 3 weeks departure October 18" → earliest_departure=2026-10-16, latest_return=2026-11-10 (tight ±2 days)
- "Japan for 3 weeks departure around October 18" → earliest_departure=2026-10-11, latest_return=2026-11-15 (±1 week)
- "Japan for 3 weeks in October" → earliest_departure=2026-10-01, latest_return=2026-10-31 (full month, flexible)

If the destination is a country (e.g. "Japan", "Mexico", "Spain"), a region, an archipelago,
or an island group (e.g. "Canary Islands", "Balearic Islands", "Greek Islands", "Hawaii",
"Caribbean"), set needs_clarification=true and suggest the top 2-4 airports with their IATA codes.
For archipelagos/island groups, list the main airports on different islands.
Example: "Canary Islands" → options: ["Gran Canaria (LPA)", "Tenerife South (TFS)", "Fuerteventura (FUE)", "Lanzarote (ACE)"]

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
- add_trip: {{"destination": "IATA airport code (e.g. ICN, NRT, MEX — use the main city code)", "origin": "IATA airport code or null", "earliest_departure": "YYYY-MM-DD", "latest_return": "YYYY-MM-DD", "passengers": int, "max_stops": int, "notes": "...", "trip_duration_type": "weekend|weeks|days|flexible|null", "trip_duration_days": int or null, "preferred_departure_days": [int] or null, "preferred_return_days": [int] or null}}
- modify_trip: {{"route_id": "...", "changes": {{"earliest_departure": "...", "latest_return": "...", "passengers": int, ...}}}}
- remove_trip: {{"route_id": "...", "confirm": true/false}}
- query_trips: {{}}
- query_prices: {{"route_id": "..." or null}}
- general_chat: {{}}

For general_chat, just set response_text to your helpful answer. No parameters needed.

ADD_TRIP DURATION RULES:
- "long weekend" or "weekend trip" → trip_duration_type="weekend", trip_duration_days=3, preferred_departure_days=[3,4], preferred_return_days=[0,6]
- "2 weeks" → trip_duration_type="weeks", trip_duration_days=14
- "10 days" → trip_duration_type="days", trip_duration_days=10
- "sometime in October" with no specific dates → trip_duration_type="flexible", earliest_departure=first of month, latest_return=last of month
- Specific dates like "Oct 18 - Nov 8" → trip_duration_type=null (exact dates)
- When the user references another trip's attributes (e.g. "same dates as Japan", "like the Mexico trip"), \
look up that route in ACTIVE ROUTES and copy the relevant fields (dates, passengers, stops, etc.).

TIMING AND PRICING HONESTY:
- When answering questions about timing (e.g. "when should I book?", "is this a good time?"), \
check whether the user has an active route with price data. If so, reference that data — \
don't give generic advice that could contradict the deal scoring.
- Never claim certainty about future price direction. Say what the data shows, not what \
prices "will" do.
- If the user's trip is far out (> 3 months), acknowledge that prices can change \
significantly and early data is directional, not definitive.

CRITICAL SAFETY RULES:
- NEVER return modify_trip or remove_trip intent for informational questions (e.g. "when's the best time to fly?", "how much does it cost?", "what's the weather like?").
- Only return add_trip, modify_trip, or remove_trip when the user EXPLICITLY asks to change, add, or remove something.
- A follow-up "yes", "ok", "sure", "yeah" after an informational response means "tell me more" or agreement with the information — it does NOT mean "change my data".
- Questions about travel (best time, prices, weather, tips) are ALWAYS general_chat or query_prices, never action intents.

When the user refers to a destination by name (e.g. "Japan", "Mexico"), match it to the active route if one exists.
When the user says "same dates as X" or "like the X trip", find route X in ACTIVE ROUTES and use its dates/settings.
When the user says "all of them" or similar, look at the conversation history to understand what they're referring to.
If modifying a trip, include all the changed fields in parameters.changes — use YYYY-MM-DD for dates.
Always use IATA airport codes for origin and destination, never city or country names."""


_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_AIRPORT_RESOLVE_PROMPT = """\
The user lives in "{location}". Identify:
1. Their closest major commercial airport (the "primary" airport they'd normally fly from)
2. 4-5 nearby alternative airports within roughly 3 hours travel, ordered by proximity

Return ONLY valid JSON:
{{
  "primary": {{"code": "IATA", "name": "Full airport name"}},
  "nearby": [
    {{"code": "IATA", "name": "Full airport name"}},
    ...
  ]
}}

Rules:
- Use standard 3-letter IATA codes (e.g. LHR, AMS, CDG, JFK)
- For cities with multiple airports, pick the main international one as primary
- Only include airports with scheduled commercial passenger service
- "nearby" should be OTHER airports, not the primary repeated
- Order nearby by rough proximity to the user's city
- Include airports in neighbouring countries if they're within range (e.g. Brussels for someone in The Hague)
"""


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
        db: Database,
        anthropic_api_key: str,
        anthropic_model: str,
        serpapi_key: str | None = None,
        reload_callback=None,
    ) -> None:
        self._bot_token = bot_token
        self._db = db
        self._anthropic_key = anthropic_api_key
        self._anthropic_model = anthropic_model
        self._serpapi_key = serpapi_key
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

    def _get_user(self, chat_id: str) -> dict | None:
        """Look up user by Telegram chat_id. Returns None if unknown."""
        return self._db.get_user_by_chat_id(chat_id)

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

        # Look up user
        user = self._get_user(chat_id)

        # Onboarding: check for pending onboarding flow
        pending = self._pending.get(chat_id)
        if pending and pending.get("action") == "onboarding":
            await self._handle_onboarding_step(text, chat_id, pending, client)
            return

        # Unknown user → start onboarding
        if user is None:
            await self._start_onboarding(chat_id, client)
            return

        # User exists but not onboarded → resume onboarding
        if not user.get("onboarded"):
            await self._start_onboarding(chat_id, client)
            return

        user_id = user["user_id"]
        home_airport = user.get("home_airport", "AMS")

        # Slash commands as shortcuts
        if text.startswith("/"):
            cmd = text.split()[0].lower()
            if cmd in ("/start", "/help"):
                await self._handle_help(chat_id, client)
                return
            elif cmd == "/trips":
                await self._handle_trips(chat_id, user_id, client)
                return
            elif cmd == "/trip":
                user_text = text[5:].strip()
                if user_text:
                    await self._handle_trip(user_text, chat_id, user_id, home_airport, client)
                else:
                    await self._handle_trip("", chat_id, user_id, home_airport, client)
                return
            elif cmd == "/remove":
                await self._handle_remove(text[7:].strip(), chat_id, user_id, client)
                return
            elif cmd == "/yes":
                await self._handle_yes(chat_id, user_id, home_airport, client)
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
        await self._interpret_message(text, chat_id, user_id, home_airport, client)

    # --- Onboarding ---

    async def _start_onboarding(self, chat_id: str, client: httpx.AsyncClient) -> None:
        self._pending[chat_id] = {"action": "onboarding", "step": "name", "chat_id": chat_id}
        await self._send(client, chat_id,
            "Welcome to FareHound! I find cheap flights from airports near you.\n\nWhat's your name?")

    async def _handle_onboarding_step(
        self, text: str, chat_id: str, pending: dict, client: httpx.AsyncClient
    ) -> None:
        step = pending.get("step")
        loop = asyncio.get_running_loop()

        if step == "name":
            name = text.strip()
            # Create or update user in DB
            existing = self._get_user(chat_id)
            if existing:
                user_id = existing["user_id"]
                await loop.run_in_executor(None, lambda: self._db.update_user(user_id, name=name))
            else:
                user_id = await loop.run_in_executor(None, self._db.create_user, chat_id, name)
            pending["user_id"] = user_id
            pending["name"] = name
            pending["step"] = "location"
            await self._send(client, chat_id,
                f"Hi {name}! Where do you live? (city, e.g. 'The Hague' or 'Amsterdam')")

        elif step == "location":
            location = text.strip()
            user_id = pending.get("user_id")
            if not user_id:
                user = self._get_user(chat_id)
                if user:
                    user_id = user["user_id"]
                else:
                    await self._start_onboarding(chat_id, client)
                    return

            # Store location immediately
            await loop.run_in_executor(None, lambda: self._db.update_user(
                user_id, home_location=location,
            ))

            # Resolve airports via Claude
            await self._send_typing(client, chat_id)
            airports = await self._resolve_airports_via_claude(location)
            if not airports:
                # Fallback: ask user to specify their airport manually
                pending["step"] = "manual_airport"
                await self._send(client, chat_id,
                    "I couldn't determine airports near you. "
                    "What's the IATA code of your home airport? (e.g. LHR, CDG, JFK)")
                return

            primary = airports["primary"]
            nearby = airports.get("nearby", [])

            # Store resolved airports in pending for confirmation
            pending["step"] = "confirm_airports"
            pending["airports"] = airports
            pending["location"] = location

            lines = [
                f"Based on *{location}*, your home airport is:",
                f"  ✈️ *{primary['name']} ({primary['code']})* (primary)",
            ]
            if nearby:
                lines.append("")
                lines.append("I'll also check nearby airports for cheaper flights:")
                for ap in nearby:
                    lines.append(f"  ✈️ {ap['name']} ({ap['code']})")

            reply_markup = {"inline_keyboard": [[
                {"text": "Looks good ✅", "callback_data": "confirm_airports:_"},
                {"text": "Change ✏️", "callback_data": "change_airports:_"},
            ]]}
            await self._send(client, chat_id, "\n".join(lines), reply_markup=reply_markup)

        elif step == "manual_airport":
            code = text.strip().upper()
            if len(code) != 3 or not code.isalpha():
                await self._send(client, chat_id,
                    "That doesn't look like an IATA code. Please enter a 3-letter airport code (e.g. LHR, CDG, JFK).")
                return
            user_id = pending.get("user_id")
            await self._finish_onboarding(
                chat_id, user_id, pending.get("name", "there"),
                pending.get("location", ""),
                {"primary": {"code": code, "name": code}, "nearby": []},
                client,
            )

        elif step == "change_airport":
            location = text.strip()
            user_id = pending.get("user_id")
            await self._send_typing(client, chat_id)
            airports = await self._resolve_airports_via_claude(location)
            if not airports:
                await self._send(client, chat_id,
                    "I still couldn't determine airports there. "
                    "What's the IATA code of your home airport? (e.g. LHR, CDG, JFK)")
                pending["step"] = "manual_airport"
                return

            primary = airports["primary"]
            nearby = airports.get("nearby", [])
            pending["step"] = "confirm_airports"
            pending["airports"] = airports

            lines = [
                f"Got it! Your home airport is:",
                f"  ✈️ *{primary['name']} ({primary['code']})* (primary)",
            ]
            if nearby:
                lines.append("")
                lines.append("Nearby airports:")
                for ap in nearby:
                    lines.append(f"  ✈️ {ap['name']} ({ap['code']})")

            reply_markup = {"inline_keyboard": [[
                {"text": "Looks good ✅", "callback_data": "confirm_airports:_"},
                {"text": "Change ✏️", "callback_data": "change_airports:_"},
            ]]}
            await self._send(client, chat_id, "\n".join(lines), reply_markup=reply_markup)

    async def _resolve_airports_via_claude(self, location: str) -> dict | None:
        """Use Claude to resolve a city name to primary + nearby airports."""
        try:
            ai_client = anthropic.AsyncAnthropic(api_key=self._anthropic_key)
            resp = await ai_client.messages.create(
                model=self._anthropic_model,
                max_tokens=256,
                messages=[{"role": "user", "content": _AIRPORT_RESOLVE_PROMPT.format(location=location)}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()
            result = json.loads(raw)
            if "primary" not in result or "code" not in result["primary"]:
                logger.warning("Claude airport response missing primary: %s", raw)
                return None
            return result
        except Exception:
            logger.warning("Failed to resolve airports for %s", location, exc_info=True)
            return None

    async def _finish_onboarding(
        self, chat_id: str, user_id: str, name: str, location: str,
        airports: dict, client: httpx.AsyncClient,
    ) -> None:
        """Complete onboarding: store airports, mark user as onboarded."""
        loop = asyncio.get_running_loop()
        primary = airports["primary"]
        nearby = airports.get("nearby", [])

        # Update user with resolved home airport
        await loop.run_in_executor(None, lambda: self._db.update_user(
            user_id, home_airport=primary["code"], onboarded=1,
        ))

        # Seed airport_transport (no transport costs yet — ITEM-004 will add those)
        airport_data = [{"code": primary["code"], "name": primary["name"], "is_primary": True}]
        for ap in nearby:
            airport_data.append({"code": ap["code"], "name": ap["name"], "is_primary": False})
        await loop.run_in_executor(None, self._db.seed_airport_transport, airport_data, user_id)

        # Check if this user is the first user (auto-approve as admin)
        all_users = await loop.run_in_executor(None, self._db.get_all_active_users)
        is_first_user = len(all_users) <= 1

        if is_first_user:
            await loop.run_in_executor(None, lambda: self._db.update_user(user_id, approved=1))

        lines = [
            f"Great, {name}! You're all set in *{location}*.",
            "",
            "I'll monitor flights from these airports for you:",
            f"  ✈️ {primary['name']} ({primary['code']}) (primary)",
        ]
        for ap in nearby:
            lines.append(f"  ✈️ {ap['name']} ({ap['code']})")
        lines.append("")

        if is_first_user:
            lines.append("Now tell me about a trip! For example:")
            lines.append("`Japan for 2 weeks in October, 2 passengers`")
        else:
            lines.append("You can already add trips — for example:")
            lines.append("`Japan for 2 weeks in October, 2 passengers`")
            lines.append("")
            lines.append("🕐 *You're on the waitlist!* I'll start monitoring your flights "
                         "once the admin approves you — usually within a few hours. "
                         "You can WhatsApp Barry to speed things up.")

            # Notify admin (first approved user)
            await self._notify_admin_new_user(name, location, primary["code"], user_id, client)

        await self._send(client, chat_id, "\n".join(lines))
        self._pending.pop(chat_id, None)

    async def _notify_admin_new_user(
        self, name: str, location: str, airport: str, user_id: str,
        client: httpx.AsyncClient,
    ) -> None:
        """Send a Telegram notification to the admin about a new user awaiting approval."""
        loop = asyncio.get_running_loop()
        all_users = await loop.run_in_executor(None, self._db.get_all_active_users)
        admin = next((u for u in all_users if u.get("approved")), None)
        if not admin:
            logger.warning("No admin user found to notify about new user %s", name)
            return

        admin_chat_id = admin["telegram_chat_id"]
        msg = (
            f"🐕 *New user awaiting approval*\n\n"
            f"*Name:* {name}\n"
            f"*Location:* {location}\n"
            f"*Home airport:* {airport}"
        )
        reply_markup = {"inline_keyboard": [[
            {"text": "Approve ✅", "callback_data": f"approve_user:{user_id}"},
            {"text": "Reject ❌", "callback_data": f"reject_user:{user_id}"},
        ]]}
        await self._send(client, admin_chat_id, msg, reply_markup=reply_markup)

    # --- Callbacks ---

    async def _edit_remove_buttons(
        self, client: httpx.AsyncClient, chat_id: str, message_id: int, message: dict, status: str,
    ) -> None:
        original_text = message.get("text", "")
        edit_url = f"{TELEGRAM_API}/bot{self._bot_token}/editMessageText"
        try:
            await client.post(edit_url, json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": original_text + f"\n\n{status}",
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            })
        except Exception:
            logger.exception("Failed to edit message to remove buttons")

    async def _answer_callback(
        self, client: httpx.AsyncClient, callback_id: str, text: str,
    ) -> None:
        url = f"{TELEGRAM_API}/bot{self._bot_token}/answerCallbackQuery"
        try:
            await client.post(url, json={"callback_query_id": callback_id, "text": text})
        except Exception:
            logger.exception("Failed to answer callback query")

    async def _handle_callback(self, callback: dict, client: httpx.AsyncClient) -> None:
        callback_id = callback.get("id")
        data = callback.get("data", "")
        message = callback.get("message", {})

        parts = data.split(":", 1)
        if len(parts) != 2:
            return
        action, payload = parts

        chat_id = str(message.get("chat", {}).get("id", ""))

        message_id = message.get("message_id")

        # Admin approval callbacks
        if action == "approve_user":
            target_user_id = payload
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._db.update_user(target_user_id, approved=1))
            await self._answer_callback(client, callback_id, "User approved!")
            if chat_id and message_id:
                await self._edit_remove_buttons(client, chat_id, message_id, message, "✅ Approved")
            # Notify the approved user
            target_user = await loop.run_in_executor(None, self._db.get_user, target_user_id)
            if target_user:
                await self._send(client, target_user["telegram_chat_id"],
                    "🎉 *You're approved!* I'm now monitoring your flights. "
                    "You'll get alerts when I find good deals.")
            return
        if action == "reject_user":
            target_user_id = payload
            loop = asyncio.get_running_loop()
            await self._answer_callback(client, callback_id, "User rejected")
            if chat_id and message_id:
                await self._edit_remove_buttons(client, chat_id, message_id, message, "❌ Rejected")
            target_user = await loop.run_in_executor(None, self._db.get_user, target_user_id)
            if target_user:
                await self._send(client, target_user["telegram_chat_id"],
                    "Sorry, your access request wasn't approved at this time. "
                    "Reach out to Barry if you think this is a mistake.")
                await loop.run_in_executor(None, lambda: self._db.update_user(target_user_id, active=0))
            return

        # Onboarding airport confirmation callbacks
        if action == "confirm_airports":
            await self._answer_callback(client, callback_id, "Confirmed!")
            if chat_id and message_id:
                await self._edit_remove_buttons(client, chat_id, message_id, message, "✅ Confirmed")
            pending = self._pending.get(chat_id)
            if pending and pending.get("step") == "confirm_airports":
                await self._finish_onboarding(
                    chat_id, pending["user_id"], pending.get("name", "there"),
                    pending.get("location", ""), pending["airports"], client,
                )
            return
        if action == "change_airports":
            await self._answer_callback(client, callback_id, "Tell me your city")
            if chat_id and message_id:
                await self._edit_remove_buttons(client, chat_id, message_id, message, "✏️ Changing")
            pending = self._pending.get(chat_id)
            if pending:
                pending["step"] = "change_airport"
            if chat_id:
                await self._send(client, chat_id,
                    "No problem! Tell me a different city or your airport code (e.g. LHR, CDG).")
            return

        # Route confirmation callbacks
        if action in ("confirm_route", "confirm_modify", "confirm_remove"):
            await self._answer_callback(client, callback_id, "Confirmed!")
            if chat_id and message_id:
                await self._edit_remove_buttons(client, chat_id, message_id, message, "✅ Confirmed")
            if chat_id:
                user = self._get_user(chat_id)
                if user:
                    await self._handle_yes(
                        chat_id, user["user_id"],
                        user.get("home_airport", "AMS"), client,
                    )
            return
        if action == "edit_route":
            await self._answer_callback(client, callback_id, "Tell me what to change")
            if chat_id and message_id:
                await self._edit_remove_buttons(client, chat_id, message_id, message, "✏️ Editing")
            if chat_id:
                await self._send(client, chat_id, "What would you like to change? Just describe the edit naturally.")
            return
        if action in ("cancel_route", "cancel_modify", "cancel_remove"):
            await self._answer_callback(client, callback_id, "Cancelled")
            if chat_id and message_id:
                await self._edit_remove_buttons(client, chat_id, message_id, message, "❌ Cancelled")
            if chat_id:
                await self._handle_no(chat_id, client)
            return

        deal_id = payload

        if action == "book":
            self._db.update_deal_feedback(deal_id, "booked")
            answer_text = "Marked as booked!"
            suffix = "\n\n✅ Marked as booked!"
        elif action == "dismiss":
            self._db.update_deal_feedback(deal_id, "dismissed")
            answer_text = "Dismissed"
            suffix = "\n\n👎 Dismissed"
        elif action == "wait":
            self._db.update_deal_feedback(deal_id, "waiting")
            answer_text = "Still watching this route"
            suffix = "\n\n🕐 Noted — still watching this route"
        elif action == "booked":
            self._db.update_deal_feedback(deal_id, "booked")
            answer_text = "Marked as booked!"
            suffix = "\n\n✅ Marked as booked!"
        elif action == "watching":
            self._db.update_deal_feedback(deal_id, "watching")
            answer_text = "Still watching"
            suffix = "\n\n👀 Still watching this route"
        else:
            return

        await self._answer_callback(client, callback_id, answer_text)

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

    # --- Message interpretation ---

    async def _interpret_message(
        self, text: str, chat_id: str, user_id: str, home_airport: str, client: httpx.AsyncClient
    ) -> None:
        loop = asyncio.get_running_loop()
        routes = await loop.run_in_executor(None, self._db.get_active_routes, user_id)

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
            home_airport=home_airport,
            routes_summary=routes_summary,
            history=self._get_history_text(chat_id),
        )

        # Inject pending proposal context so the user can modify it naturally
        pending = self._pending.get(chat_id)
        if pending and pending.get("action") == "add":
            from src.utils.airports import route_name as _rn
            pending_desc = (
                f"\n\nPENDING ROUTE PROPOSAL (awaiting confirmation):\n"
                f"- Route: {_rn(pending['origin'], pending['destination'])}\n"
                f"- Dates: {pending.get('earliest_departure', '?')} to {pending.get('latest_return', '?')}\n"
                f"- Passengers: {pending.get('passengers', 2)}\n"
                f"- Max stops: {pending.get('max_stops', 1)}\n"
                f"- Duration type: {pending.get('trip_duration_type', 'N/A')}\n"
                f"- Duration days: {pending.get('trip_duration_days', 'N/A')}\n"
                f"\nIf the user wants to change something about this pending proposal "
                f"(dates, passengers, destination, stops, etc.), return intent \"modify_pending\" with "
                f"parameters containing the FULL updated proposal fields (origin, destination, "
                f"earliest_departure, latest_return, passengers, max_stops, notes, "
                f"trip_duration_type, trip_duration_days, preferred_departure_days, preferred_return_days). "
                f"Merge the user's changes with the existing values above."
            )
            system_prompt += pending_desc

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
        except json.JSONDecodeError:
            logger.warning("Failed to parse Claude response as JSON: %s", raw)
            await self._send(client, chat_id, "I had trouble understanding that. Could you rephrase?")
            return
        except Exception:
            logger.warning("Failed to interpret message", exc_info=True)
            await self._send(client, chat_id, "I had trouble understanding that. Could you rephrase?")
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
            msg = response_text or "I'm not sure how to respond to that. Try /help for commands."
            self._add_history(chat_id, "assistant", msg)
            await self._send(client, chat_id, msg)

        elif intent == "add_trip":
            destination = params.get("destination")
            if not destination:
                msg = response_text or "I couldn't understand that destination. Could you try again with a city or airport code?"
                self._add_history(chat_id, "assistant", msg)
                await self._send(client, chat_id, msg)
                return
            origin = params.get("origin") or home_airport
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
                "user_id": user_id,
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
            reply_markup = {"inline_keyboard": [[
                {"text": "Confirm ✅", "callback_data": "confirm_route:_"},
                {"text": "Edit ✏️", "callback_data": "edit_route:_"},
                {"text": "Cancel ❌", "callback_data": "cancel_route:_"},
            ]]}
            self._add_history(chat_id, "assistant", msg)
            await self._send(client, chat_id, msg, reply_markup=reply_markup)

        elif intent == "modify_trip":
            route_id = params.get("route_id")
            changes = params.get("changes", {})
            if not route_id or not changes:
                msg = response_text or "I couldn't understand which route to modify. Use /trips to see your routes."
                self._add_history(chat_id, "assistant", msg)
                await self._send(client, chat_id, msg)
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
                "user_id": user_id,
            }
            # Build a human-readable summary of changes
            change_lines = []
            for k, v in changes.items():
                label = k.replace("_", " ").title()
                change_lines.append(f"  {label}: {v}")
            msg = (
                f"Modify *{route_name(r.origin, r.destination)}*:\n"
                + "\n".join(change_lines)
            )
            reply_markup = {"inline_keyboard": [[
                {"text": "Confirm ✅", "callback_data": "confirm_modify:_"},
                {"text": "Cancel ❌", "callback_data": "cancel_modify:_"},
            ]]}
            self._add_history(chat_id, "assistant", msg)
            await self._send(client, chat_id, msg, reply_markup=reply_markup)

        elif intent == "remove_trip":
            route_id = params.get("route_id")
            if not route_id:
                msg = response_text or "I couldn't understand which route to remove. Use /trips to see your routes."
                self._add_history(chat_id, "assistant", msg)
                await self._send(client, chat_id, msg)
                return
            matching = [r for r in routes if r.route_id == route_id]
            if not matching:
                msg = f"I couldn't find route `{route_id}`."
                self._add_history(chat_id, "assistant", msg)
                await self._send(client, chat_id, msg)
                return
            r = matching[0]
            self._pending[chat_id] = {"action": "remove", "route_id": r.route_id, "user_id": user_id}
            msg = f"Remove *{route_name(r.origin, r.destination)}*?"
            reply_markup = {"inline_keyboard": [[
                {"text": "Confirm ✅", "callback_data": "confirm_remove:_"},
                {"text": "Cancel ❌", "callback_data": "cancel_remove:_"},
            ]]}
            self._add_history(chat_id, "assistant", msg)
            await self._send(client, chat_id, msg, reply_markup=reply_markup)

        elif intent == "query_trips":
            await self._handle_trips(chat_id, user_id, client)

        elif intent == "query_prices":
            route_id = params.get("route_id")
            await self._handle_price_query(route_id, routes, chat_id, client)

        elif intent == "modify_pending":
            old_pending = self._pending.get(chat_id)
            if not old_pending or old_pending.get("action") != "add":
                self._add_history(chat_id, "assistant", response_text or "No pending proposal to modify.")
                await self._send(client, chat_id, response_text or "No pending proposal to modify.")
                return
            # Update pending with new parameters
            for key in ("origin", "destination", "earliest_departure", "latest_return",
                        "passengers", "max_stops", "notes", "trip_duration_type",
                        "trip_duration_days", "preferred_departure_days", "preferred_return_days"):
                if key in params:
                    old_pending[key] = params[key]
            # Re-present proposal with inline buttons
            stops_str = "direct only" if old_pending.get("max_stops", 1) == 0 else f"max {old_pending.get('max_stops', 1)} stop{'s' if old_pending.get('max_stops', 1) != 1 else ''}"
            date_display = _format_date_display(old_pending)
            msg = (
                f"Updated proposal: *{route_name(old_pending['origin'], old_pending['destination'])}*\n"
                f"📅 {date_display}\n"
                f"👥 {old_pending.get('passengers', 2)} pax | {stops_str}"
            )
            notes = old_pending.get("notes")
            if notes:
                msg += f"\n📝 {notes}"
            reply_markup = {"inline_keyboard": [[
                {"text": "Confirm ✅", "callback_data": "confirm_route:_"},
                {"text": "Edit ✏️", "callback_data": "edit_route:_"},
                {"text": "Cancel ❌", "callback_data": "cancel_route:_"},
            ]]}
            self._add_history(chat_id, "assistant", msg)
            await self._send(client, chat_id, msg, reply_markup=reply_markup)

        else:
            logger.warning("Unknown intent %r from Claude response", intent)
            msg = response_text or "I'm not sure how to help with that. Try /help for commands."
            self._add_history(chat_id, "assistant", msg)
            await self._send(client, chat_id, msg)

    async def _handle_price_query(
        self, route_id: str | None, routes: list[Route], chat_id: str, client: httpx.AsyncClient
    ) -> None:
        from src.utils.airports import route_name, airport_name

        loop = asyncio.get_running_loop()
        user = self._get_user(chat_id)
        user_id = user["user_id"] if user else None

        if route_id:
            target_routes = [r for r in routes if r.route_id == route_id]
        else:
            target_routes = routes

        if not target_routes:
            msg = "No matching routes found. Use /trips to see your routes."
            self._add_history(chat_id, "assistant", msg)
            await self._send(client, chat_id, msg)
            return

        for r in target_routes:
            name = route_name(r.origin, r.destination)
            cheapest = await loop.run_in_executor(None, self._db.get_cheapest_recent_snapshot, r.route_id)
            history = await loop.run_in_executor(None, self._db.get_price_history, r.route_id)

            if not cheapest or not cheapest.lowest_price:
                msg = f"*{name}*: no prices yet"
                self._add_history(chat_id, "assistant", msg)
                await self._send(client, chat_id, msg)
                continue

            # Fix /pp bug: divide total price by passengers
            total_price = float(cheapest.lowest_price)
            price_pp = total_price / r.passengers if r.passengers > 1 else total_price

            lines = [f"💰 *{name}*"]
            lines.append(f"*€{price_pp:,.0f}/pp* ({r.passengers} pax)")

            # Cost breakdown with transport
            transport = await loop.run_in_executor(
                None, self._db.get_airport_transport, r.origin, user_id,
            )
            t_cost = transport["transport_cost_eur"] if transport else 0
            p_cost = (transport or {}).get("parking_cost_eur") or 0
            t_mode = (transport or {}).get("transport_mode", "transport")
            t_total = transport_total(t_cost, t_mode, r.passengers)
            trip_total = total_price + t_total + p_cost
            cost_parts = [f"€{total_price:,.0f} flights"]
            if t_total:
                cost_parts.append(f"€{t_total:,.0f} {t_mode.lower()}")
            if p_cost:
                cost_parts.append(f"€{p_cost:,.0f} parking")
            lines.append(f"{' + '.join(cost_parts)} = *€{trip_total:,.0f} total*")

            # Dates and airline
            if cheapest.outbound_date and cheapest.return_date:
                out = cheapest.outbound_date
                ret = cheapest.return_date
                if hasattr(out, "strftime"):
                    out = out.strftime("%b %d")
                if hasattr(ret, "strftime"):
                    ret = ret.strftime("%b %d")
                lines.append(f"📅 {out} → {ret}")

            if cheapest.best_flight:
                flights = cheapest.best_flight.get("flights", [])
                if flights:
                    airline = flights[0].get("airline", "")
                    if airline:
                        lines.append(f"✈️ {airline}")

            # Trend vs average
            if history and history.get("avg_price"):
                avg = float(history["avg_price"])
                diff = total_price - avg
                if diff < 0:
                    lines.append(f"📉 €{abs(diff):,.0f} below average (avg €{avg:,.0f})")
                elif diff > 0:
                    lines.append(f"📈 €{diff:,.0f} above average (avg €{avg:,.0f})")
                else:
                    lines.append(f"➡️ At average (€{avg:,.0f})")

            if cheapest.price_level:
                level_icon = {"low": "📉", "typical": "➡️", "high": "📈"}.get(cheapest.price_level, "")
                lines.append(f"{level_icon} Price level: {cheapest.price_level}")

            # Nearby alternatives from DB (no new API calls)
            if hasattr(self._db, "get_nearby_snapshots"):
                nearby_snaps = await loop.run_in_executor(
                    None, self._db.get_nearby_snapshots, r.route_id, r.origin,
                )
                alts_shown = 0
                for alt in nearby_snaps:
                    alt_price = float(alt["lowest_price"])
                    alt_pp = alt_price / r.passengers if r.passengers > 1 else alt_price
                    if alt_pp < price_pp:
                        if alts_shown == 0:
                            lines.append("")
                            lines.append("*Nearby alternatives:*")
                        alt_code = alt["airport_code"].upper()
                        alt_transport = await loop.run_in_executor(
                            None, self._db.get_airport_transport, alt_code, user_id,
                        )
                        at_cost = alt_transport["transport_cost_eur"] if alt_transport else 0
                        ap_cost = (alt_transport or {}).get("parking_cost_eur") or 0
                        at_mode = (alt_transport or {}).get("transport_mode", "transport")
                        at_total = transport_total(at_cost, at_mode, r.passengers)
                        alt_total = alt_price + at_total + ap_cost
                        savings = trip_total - alt_total
                        if savings > 0:
                            icon = "🟢" if alts_shown == 0 else "🟡"
                            alt_parts = [f"€{alt_price:,.0f} flights"]
                            if at_total:
                                alt_parts.append(f"€{at_total:,.0f} {at_mode.lower()}")
                            if ap_cost:
                                alt_parts.append(f"€{ap_cost:,.0f} parking")
                            lines.append(
                                f"{icon} *{airport_name(alt_code)}*: €{alt_pp:,.0f}/pp (save €{savings:,.0f})"
                            )
                            lines.append(
                                f"    {' + '.join(alt_parts)} = *€{alt_total:,.0f} total*"
                            )
                            alts_shown += 1
                            if alts_shown >= 3:
                                break

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
            "When I propose a route, use the inline buttons to confirm, "
            "edit, or cancel. You can also reply with changes in plain text.\n\n"
            "*Shortcuts:*\n"
            "`/trip Tokyo, Oct 18 - Nov 8, 2 people` — add a route\n"
            "`/trips` — list your routes with prices\n"
            "`/remove Tokyo` — stop monitoring a route\n"
            "`/yes` `/no` — confirm or cancel (also works as fallback)"
        ), parse_mode="Markdown")

    async def _handle_trip(
        self, user_text: str, chat_id: str, user_id: str, home_airport: str, client: httpx.AsyncClient
    ) -> None:
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
                "user_id": user_id,
            }
            await self._send(client, chat_id, question)
            return

        origin = parsed.get("origin") or home_airport
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
            "user_id": user_id,
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
        reply_markup = {"inline_keyboard": [[
            {"text": "Confirm ✅", "callback_data": "confirm_route:_"},
            {"text": "Edit ✏️", "callback_data": "edit_route:_"},
            {"text": "Cancel ❌", "callback_data": "cancel_route:_"},
        ]]}
        await self._send(client, chat_id, msg, reply_markup=reply_markup)

    async def _handle_clarification_reply(self, reply: str, chat_id: str, client: httpx.AsyncClient) -> None:
        """User replied to a clarification question — re-parse with the specific city."""
        pending = self._pending.pop(chat_id, None)
        if not pending:
            return
        user_id = pending.get("user_id")
        user = self._get_user(chat_id)
        home_airport = (user or {}).get("home_airport", "AMS")
        original = pending.get("original_text", "")
        # Replace the ambiguous part with the user's specific answer
        new_text = f"{reply}, {original}"
        await self._handle_trip(new_text, chat_id, user_id, home_airport, client)

    async def _handle_trips(self, chat_id: str, user_id: str, client: httpx.AsyncClient) -> None:
        from src.utils.airports import airport_name, route_name

        loop = asyncio.get_running_loop()
        routes = await loop.run_in_executor(None, self._db.get_active_routes, user_id)

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

    async def _handle_remove(
        self, query: str, chat_id: str, user_id: str, client: httpx.AsyncClient
    ) -> None:
        from src.utils.airports import route_name, AIRPORTS

        if not query:
            # Show routes with IDs for easy removal
            loop = asyncio.get_running_loop()
            routes = await loop.run_in_executor(None, self._db.get_active_routes, user_id)
            if not routes:
                await self._send(client, chat_id, "No active routes to remove.")
                return
            lines = ["Which route do you want to remove?\n"]
            for r in routes:
                lines.append(f"  `/remove {r.route_id}` — {route_name(r.origin, r.destination)}")
            await self._send(client, chat_id, "\n".join(lines))
            return

        loop = asyncio.get_running_loop()
        routes = await loop.run_in_executor(None, self._db.get_active_routes, user_id)

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
        self._pending[chat_id] = {"action": "remove", "route_id": r.route_id, "user_id": user_id}
        reply_markup = {"inline_keyboard": [[
            {"text": "Confirm ✅", "callback_data": "confirm_remove:_"},
            {"text": "Cancel ❌", "callback_data": "cancel_remove:_"},
        ]]}
        await self._send(client, chat_id, f"Remove *{route_name(r.origin, r.destination)}*?", reply_markup=reply_markup)

    async def _handle_yes(
        self, chat_id: str, user_id: str, home_airport: str, client: httpx.AsyncClient
    ) -> None:
        from src.utils.airports import route_name

        pending = self._pending.pop(chat_id, None)
        if not pending:
            await self._send(client, chat_id, "Nothing pending to confirm.")
            return

        loop = asyncio.get_running_loop()
        uid = pending.get("user_id", user_id)

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
                user_id=uid,
            )
            await loop.run_in_executor(None, self._db.upsert_route, route, uid)

            if self._reload_callback:
                await self._reload_callback()

            await self._send(client, chat_id, f"Route added: {route_name(route.origin, route.destination)}")

            # Immediate price check (non-blocking)
            task = asyncio.create_task(self._immediate_price_check(route, uid, chat_id, client))
            task.add_done_callback(self._on_price_check_done)

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
                routes = await loop.run_in_executor(None, self._db.get_active_routes, uid)
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

    async def _send(
        self, client: httpx.AsyncClient, chat_id: str, text: str,
        parse_mode: str = "Markdown", reply_markup: dict | None = None,
    ) -> None:
        url = f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        except Exception:
            logger.exception("Failed to send Telegram message")

    async def _send_typing(self, client: httpx.AsyncClient, chat_id: str) -> None:
        url = f"{TELEGRAM_API}/bot{self._bot_token}/sendChatAction"
        try:
            await client.post(url, json={"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass

    def _on_price_check_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("Background price check failed: %s", exc, exc_info=exc)

    async def _immediate_price_check(
        self, route: Route, user_id: str, chat_id: str, client: httpx.AsyncClient,
    ) -> None:
        if not self._serpapi_key:
            return

        from datetime import date as date_type
        from src.apis.serpapi import SerpAPIClient, extract_lowest_price, extract_min_duration, generate_date_windows
        from src.analysis.nearby_airports import compare_airports
        from src.utils.airports import route_name, airport_name

        loop = asyncio.get_running_loop()

        try:
            await self._send(client, chat_id, "🔍 Checking prices now...")

            # Determine date windows
            earliest = route.earliest_departure
            latest = route.latest_return
            if isinstance(earliest, str) and earliest:
                earliest = date_type.fromisoformat(earliest)
            if isinstance(latest, str) and latest:
                latest = date_type.fromisoformat(latest)
            if not earliest or not latest:
                return

            duration = route.trip_duration_days or 14
            windows = generate_date_windows(earliest, latest, duration, max_windows=2)
            if not windows:
                return

            serp = SerpAPIClient(api_key=self._serpapi_key, currency="EUR")
            try:
                # Poll primary airport
                out_date, ret_date = windows[0]
                await self._send_typing(client, chat_id)
                primary_result = await serp.search_flights(
                    origin=route.origin,
                    destination=route.destination,
                    outbound_date=out_date,
                    return_date=ret_date,
                    passengers=route.passengers,
                    max_stops=route.max_stops,
                )

                primary_price = extract_lowest_price(primary_result, max_stops=route.max_stops)

                if not primary_price:
                    await self._send(client, chat_id,
                        "No flights found for those dates. I'll keep checking on the next poll cycle.")
                    return

                # Get transport info
                primary_transport = await loop.run_in_executor(
                    None, self._db.get_airport_transport, route.origin, user_id,
                )
                p_transport_cost = primary_transport["transport_cost_eur"] if primary_transport else 0
                p_parking_cost = (primary_transport or {}).get("parking_cost_eur") or 0
                p_mode = (primary_transport or {}).get("transport_mode", "transport")

                price_pp = float(primary_price) / route.passengers if route.passengers > 1 else float(primary_price)
                price_level = primary_result.price_insights.get("price_level", "")
                typical_range = primary_result.price_insights.get("typical_price_range", [])

                # Build primary cost breakdown
                p_t_total = transport_total(p_transport_cost, p_mode, route.passengers)
                total = float(primary_price) + p_t_total + p_parking_cost
                cost_parts = [f"€{float(primary_price):,.0f} flights"]
                if p_t_total:
                    cost_parts.append(f"€{p_t_total:,.0f} {p_mode.lower()}")
                if p_parking_cost:
                    cost_parts.append(f"€{p_parking_cost:,.0f} parking")

                lines = [
                    f"💰 *Current prices — {route_name(route.origin, route.destination)}*",
                    f"*€{price_pp:,.0f}/pp* from {airport_name(route.origin)}",
                    f"{' + '.join(cost_parts)} = *€{total:,.0f} total*",
                    f"📅 {out_date} → {ret_date}",
                ]

                if price_level:
                    level_icon = {"low": "📉", "typical": "➡️", "high": "📈"}.get(price_level, "")
                    lines.append(f"{level_icon} Price level: {price_level}")
                if typical_range and len(typical_range) == 2:
                    lines.append(f"Typical range: €{typical_range[0]:,.0f} – €{typical_range[1]:,.0f}")

                # Flight details
                primary_duration = extract_min_duration(primary_result)
                if primary_result.best_flights:
                    bf = primary_result.best_flights[0]
                    legs = bf.get("flights", [])
                    airline = legs[0].get("airline", "") if legs else ""
                    stops = max(0, len(legs) - 1)
                    dur = bf.get("total_duration")
                    parts = []
                    if airline:
                        parts.append(airline)
                    parts.append("Direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}")
                    if dur:
                        parts.append(f"{dur // 60}h{dur % 60:02d}m")
                    lines.insert(1, f"✈️ {' · '.join(parts)}")

                # Poll secondary airports
                await self._send_typing(client, chat_id)
                secondary_airports = await loop.run_in_executor(
                    None, self._db.get_secondary_airports, user_id,
                )

                primary_for_compare = {
                    "airport_code": route.origin,
                    "fare_pp": price_pp,
                    "transport_cost": p_transport_cost,
                    "parking_cost": p_parking_cost,
                    "transport_mode": p_mode,
                    "flight_duration_min": primary_duration,
                }

                secondary_results = []
                for apt in secondary_airports[:4]:
                    await self._send_typing(client, chat_id)
                    try:
                        sec_result = await serp.search_flights(
                            origin=apt["airport_code"],
                            destination=route.destination,
                            outbound_date=out_date,
                            return_date=ret_date,
                            passengers=route.passengers,
                            max_stops=route.max_stops,
                        )
                        sec_price = extract_lowest_price(sec_result, max_stops=route.max_stops)
                        if sec_price:
                            sec_pp = float(sec_price) / route.passengers if route.passengers > 1 else float(sec_price)
                            secondary_results.append({
                                "airport_code": apt["airport_code"],
                                "fare_pp": sec_pp,
                                "transport_cost": apt.get("transport_cost_eur", 0),
                                "parking_cost": apt.get("parking_cost_eur"),
                                "transport_mode": apt.get("transport_mode", ""),
                                "transport_time_min": apt.get("transport_time_min", 0),
                                "flight_duration_min": extract_min_duration(sec_result),
                            })
                    except Exception:
                        logger.debug("Secondary airport %s check failed", apt["airport_code"])

                alternatives = compare_airports(
                    primary_for_compare, secondary_results, route.passengers,
                )

                if alternatives:
                    lines.append("")
                    lines.append("*Nearby alternatives:*")
                    for i, alt in enumerate(alternatives[:3]):
                        icon = "🟢" if i == 0 else "🟡"
                        t_min = alt.get("transport_time_min", 0)
                        hours = t_min / 60
                        alt_fare_total = alt["fare_pp"] * route.passengers
                        alt_t_total = transport_total(alt["transport_cost"], alt["transport_mode"], route.passengers)
                        alt_parts = [f"€{alt_fare_total:,.0f} flights"]
                        if alt_t_total:
                            alt_parts.append(f"€{alt_t_total:,.0f} {alt['transport_mode'].lower()}")
                        if alt.get("parking_cost"):
                            alt_parts.append(f"€{alt['parking_cost']:,.0f} parking")
                        alt_dur = alt.get("flight_duration_min")
                        alt_pri_dur = alt.get("primary_flight_duration_min")
                        alt_dur_str = ""
                        if alt_dur:
                            alt_dur_h = alt_dur / 60
                            if alt_pri_dur and alt_dur != alt_pri_dur:
                                diff_h = (alt_dur - alt_pri_dur) / 60
                                sign = "+" if diff_h > 0 else ""
                                alt_dur_str = f" | {alt_dur_h:.0f}h flight ({sign}{diff_h:.0f}h)"
                            else:
                                alt_dur_str = f" | {alt_dur_h:.0f}h flight"
                        lines.append(
                            f"{icon} *{alt['airport_name']}*: €{alt['fare_pp']:,.0f}/pp (save €{alt['savings']:,.0f}){alt_dur_str}"
                        )
                        lines.append(
                            f"    {' + '.join(alt_parts)} = *€{alt['net_cost']:,.0f} total*"
                        )
                        lines.append(
                            f"    {alt['transport_mode']} {hours:.1f}h to airport"
                        )

                await self._send(client, chat_id, "\n".join(lines))

            finally:
                await serp.close()

        except Exception:
            logger.exception("Immediate price check failed")
            await self._send(client, chat_id,
                "Couldn't check prices right now — I'll try again on the next poll cycle.")
