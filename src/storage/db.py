from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import os

from src.storage.models import (
    AlertRule,
    Deal,
    PollWindow,
    PriceSnapshot,
    Route,
)

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id           TEXT PRIMARY KEY,
    telegram_chat_id  TEXT UNIQUE NOT NULL,
    name              TEXT,
    home_location     TEXT,
    home_airport      TEXT DEFAULT 'AMS',
    preferences       TEXT,
    onboarded         INTEGER DEFAULT 0,
    approved          INTEGER DEFAULT 0,
    active            INTEGER DEFAULT 1,
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS routes (
    route_id          TEXT PRIMARY KEY,
    origin            TEXT NOT NULL,
    destination       TEXT NOT NULL,
    trip_type         TEXT DEFAULT 'round_trip',
    earliest_departure TEXT,
    latest_return     TEXT,
    date_flex_days    INTEGER DEFAULT 3,
    max_stops         INTEGER DEFAULT 1,
    passengers        INTEGER DEFAULT 2,
    preferred_airlines TEXT,
    notes             TEXT,
    active            INTEGER DEFAULT 1,
    created_at        TEXT DEFAULT (datetime('now')),
    trip_duration_type TEXT,
    trip_duration_days INTEGER,
    preferred_departure_days TEXT,
    preferred_return_days TEXT
);

CREATE TABLE IF NOT EXISTS poll_windows (
    window_id         TEXT PRIMARY KEY,
    route_id          TEXT REFERENCES routes(route_id),
    outbound_date     TEXT NOT NULL,
    return_date       TEXT,
    priority          TEXT DEFAULT 'normal',
    last_polled_at    TEXT,
    lowest_seen_price REAL,
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    route_id          TEXT REFERENCES routes(route_id),
    window_id         TEXT REFERENCES poll_windows(window_id),
    observed_at       TEXT NOT NULL,
    source            TEXT NOT NULL,
    outbound_date     TEXT,
    return_date       TEXT,
    passengers        INTEGER NOT NULL,
    lowest_price      REAL,
    currency          TEXT DEFAULT 'EUR',
    best_flight       TEXT,
    all_flights       TEXT,
    price_level       TEXT,
    typical_low       REAL,
    typical_high      REAL,
    price_history     TEXT,
    search_params     TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deals (
    deal_id           TEXT PRIMARY KEY,
    snapshot_id       TEXT REFERENCES price_snapshots(snapshot_id),
    route_id          TEXT REFERENCES routes(route_id),
    score             REAL,
    urgency           TEXT,
    reasoning         TEXT,
    booking_url       TEXT,
    alert_sent        INTEGER DEFAULT 0,
    alert_sent_at     TEXT,
    booked            INTEGER DEFAULT 0,
    feedback          TEXT,
    follow_up_sent_at TEXT,
    follow_up_count   INTEGER DEFAULT 0,
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alert_rules (
    rule_id           TEXT PRIMARY KEY,
    route_id          TEXT REFERENCES routes(route_id),
    rule_type         TEXT NOT NULL,
    threshold         REAL,
    channel           TEXT NOT NULL,
    active            INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS airport_transport (
    airport_code      TEXT PRIMARY KEY,
    airport_name      TEXT,
    transport_mode    TEXT,
    transport_cost_eur REAL,
    transport_time_min INTEGER,
    parking_cost_eur  REAL,
    is_primary        INTEGER DEFAULT 0
);

-- R9 ITEM-053: multi-mode transport options per airport.
-- Replaces the one-mode-per-airport airport_transport table. The legacy table
-- stays for one release as compat; rows are migrated forward at first boot.
CREATE TABLE IF NOT EXISTS airport_transport_option (
    user_id                  TEXT NOT NULL,
    airport_code             TEXT NOT NULL,
    mode                     TEXT NOT NULL,        -- drive | train | taxi | uber | bus | other
    cost_eur                 REAL,                 -- one-way; doubled at render time for round-trip
    cost_scales_with_pax     INTEGER NOT NULL DEFAULT 0,  -- 1 for train/bus, 0 for drive/taxi/uber
    time_min                 INTEGER,
    parking_cost_per_day_eur REAL,                 -- only for drive; null otherwise
    enabled                  INTEGER NOT NULL DEFAULT 1,  -- user can disable a mode without deleting
    source                   TEXT,                 -- google_maps | curated | user_override | user_added | legacy
    confidence               TEXT,                 -- high | medium | low (for "estimate — confirm" UI)
    label                    TEXT,                 -- optional user-facing label, e.g. "ride from family"
    created_at               TEXT DEFAULT (datetime('now')),
    updated_at               TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, airport_code, mode)
);

CREATE INDEX IF NOT EXISTS idx_transport_option_lookup
    ON airport_transport_option(user_id, airport_code, enabled);

CREATE TABLE IF NOT EXISTS savings_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    deal_id TEXT,
    route_id TEXT NOT NULL,
    primary_cost REAL NOT NULL,
    alternative_cost REAL NOT NULL,
    savings_amount REAL NOT NULL,
    airport_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(route_id, airport_code, snapshot_date)
);
"""

# Tables that need a user_id column added via migration
_USER_ID_TABLES = ["routes", "price_snapshots", "deals", "poll_windows", "airport_transport"]


def _to_json(val) -> str | None:
    if val is None:
        return None
    return json.dumps(val)


def _to_isoformat(val) -> str | None:
    """Convert datetime/date to ISO string for sqlite storage (UTC, no tz suffix)."""
    if val is None:
        return None
    if isinstance(val, datetime):
        # Store as naive UTC with space separator to match sqlite's datetime('now')
        utc = val.astimezone(UTC).replace(tzinfo=None)
        return utc.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(val, date):
        return val.isoformat()
    return str(val)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def _user_filter(user_id: str | None, params: list) -> str:
    """Return SQL fragment and append param if user_id is provided."""
    if user_id is None:
        return ""
    params.append(user_id)
    return " AND user_id = ?"


class Database:
    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            data_dir = os.environ.get("FAREHOUND_DATA_DIR", "data")
            db_path = Path(data_dir) / "flights.db"
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        # Add user_id column to existing tables if missing
        for table in _USER_ID_TABLES:
            if not _has_column(self._conn, table, "user_id"):
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
        self._conn.commit()
        # Add follow-up tracking columns to deals if missing
        for col, default in [("follow_up_sent_at", None), ("follow_up_count", "0")]:
            if not _has_column(self._conn, "deals", col):
                alter = f"ALTER TABLE deals ADD COLUMN {col} {'TEXT' if default is None else 'INTEGER DEFAULT ' + default}"
                self._conn.execute(alter)
        self._conn.commit()
        # Add approved column if missing; auto-approve existing users
        if not _has_column(self._conn, "users", "approved"):
            self._conn.execute("ALTER TABLE users ADD COLUMN approved INTEGER DEFAULT 0")
            self._conn.execute("UPDATE users SET approved = 1 WHERE onboarded = 1")
            self._conn.commit()
        # R7 ITEM-051 migrations
        # A1: routes.snoozed_until — per-route snooze
        if not _has_column(self._conn, "routes", "snoozed_until"):
            self._conn.execute("ALTER TABLE routes ADD COLUMN snoozed_until TEXT")
        # A2: users.baggage_needs — preference (default 'one_checked')
        if not _has_column(self._conn, "users", "baggage_needs"):
            self._conn.execute(
                "ALTER TABLE users ADD COLUMN baggage_needs TEXT DEFAULT 'one_checked'"
            )
        # A3: users digest fingerprint + skip-tracking
        if not _has_column(self._conn, "users", "last_digest_fingerprint"):
            self._conn.execute("ALTER TABLE users ADD COLUMN last_digest_fingerprint TEXT")
        if not _has_column(self._conn, "users", "last_digest_sent_at"):
            self._conn.execute("ALTER TABLE users ADD COLUMN last_digest_sent_at TEXT")
        if not _has_column(self._conn, "users", "digest_skip_count_7d"):
            self._conn.execute(
                "ALTER TABLE users ADD COLUMN digest_skip_count_7d INTEGER DEFAULT 0"
            )
        # A4: price_snapshots.baggage_estimate — JSON blob
        if not _has_column(self._conn, "price_snapshots", "baggage_estimate"):
            self._conn.execute(
                "ALTER TABLE price_snapshots ADD COLUMN baggage_estimate TEXT"
            )
        # A5: deals.reasoning_json — structured scorer output
        if not _has_column(self._conn, "deals", "reasoning_json"):
            self._conn.execute("ALTER TABLE deals ADD COLUMN reasoning_json TEXT")
        self._conn.commit()
        # R9 ITEM-053: per-user per-airport "always use [mode]" override map (JSON).
        # Stored as JSON {airport_code: mode_string} on users to avoid yet another
        # lookup table for what is functionally a small flat preference set.
        if not _has_column(self._conn, "users", "airport_override_mode"):
            self._conn.execute("ALTER TABLE users ADD COLUMN airport_override_mode TEXT")
        self._conn.commit()
        # R9 ITEM-053: forward-migrate legacy airport_transport rows into
        # airport_transport_option. Idempotent — runs on every boot but only
        # writes rows that don't already exist in the new table.
        self._migrate_airport_transport_to_options()
        # Migrate existing data: create default user if needed
        self._migrate_default_user()

    def _migrate_airport_transport_to_options(self) -> None:
        """Forward-migrate legacy airport_transport rows into airport_transport_option.

        Idempotent: skips rows that already exist in the new table. Treats the
        legacy single-mode row as a `source='legacy'` entry; the user can promote
        it to `user_override` by editing in Settings.
        """
        try:
            cursor = self._conn.execute(
                "SELECT airport_code, airport_name, transport_mode, transport_cost_eur, "
                "transport_time_min, parking_cost_eur, user_id FROM airport_transport"
            )
        except sqlite3.OperationalError:
            # Legacy table missing — nothing to migrate.
            return
        migrated = 0
        for row in cursor.fetchall():
            airport_code, _name, mode, cost, time_min, parking, user_id = row
            if not user_id or not mode:
                # Pre-multi-user rows or rows missing the mode field — skip.
                continue
            existing = self._conn.execute(
                "SELECT 1 FROM airport_transport_option "
                "WHERE user_id = ? AND airport_code = ? AND mode = ?",
                [user_id, airport_code, mode],
            ).fetchone()
            if existing:
                continue
            from src.analysis.transport import is_per_person_mode  # local import to avoid cycle
            self._conn.execute(
                """
                INSERT INTO airport_transport_option (
                    user_id, airport_code, mode, cost_eur, cost_scales_with_pax,
                    time_min, parking_cost_per_day_eur, enabled, source, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'legacy', 'high')
                """,
                [
                    user_id, airport_code, mode, cost,
                    1 if is_per_person_mode(mode) else 0,
                    time_min, parking,
                ],
            )
            migrated += 1
        if migrated:
            self._conn.commit()
            logger.info("Migrated %d airport_transport rows → airport_transport_option", migrated)

    def _migrate_default_user(self) -> None:
        user_count = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        route_count = self._conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
        if user_count == 0 and route_count > 0:
            default_id = uuid4().hex
            self._conn.execute(
                "INSERT INTO users (user_id, telegram_chat_id, name, onboarded, active) VALUES (?, ?, ?, 1, 1)",
                [default_id, "default", "barry"],
            )
            for table in _USER_ID_TABLES:
                self._conn.execute(
                    f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL",
                    [default_id],
                )
            self._conn.commit()

    # --- Users ---

    def create_user(self, telegram_chat_id: str, name: str | None = None) -> str:
        user_id = uuid4().hex
        self._conn.execute(
            "INSERT INTO users (user_id, telegram_chat_id, name) VALUES (?, ?, ?)",
            [user_id, telegram_chat_id, name],
        )
        self._conn.commit()
        return user_id

    def get_user_by_chat_id(self, chat_id: str) -> dict | None:
        cursor = self._conn.execute(
            "SELECT * FROM users WHERE telegram_chat_id = ?", [chat_id]
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cursor.description]
        result = dict(zip(columns, row))
        result["onboarded"] = bool(result["onboarded"])
        result["approved"] = bool(result.get("approved"))
        result["active"] = bool(result["active"])
        if result.get("preferences"):
            result["preferences"] = json.loads(result["preferences"])
        return result

    def get_user(self, user_id: str) -> dict | None:
        cursor = self._conn.execute(
            "SELECT * FROM users WHERE user_id = ?", [user_id]
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cursor.description]
        result = dict(zip(columns, row))
        result["onboarded"] = bool(result["onboarded"])
        result["approved"] = bool(result.get("approved"))
        result["active"] = bool(result["active"])
        if result.get("preferences"):
            result["preferences"] = json.loads(result["preferences"])
        return result

    def update_user(self, user_id: str, **fields) -> bool:
        allowed = {
            "name", "home_location", "home_airport", "preferences",
            "onboarded", "approved", "active",
            "baggage_needs",
            "last_digest_fingerprint", "last_digest_sent_at", "digest_skip_count_7d",
        }
        to_update = {k: v for k, v in fields.items() if k in allowed}
        if not to_update:
            return False
        sets = ", ".join(f"{k} = ?" for k in to_update)
        vals = []
        for k, v in to_update.items():
            if k == "preferences" and isinstance(v, (dict, list)):
                vals.append(json.dumps(v))
            else:
                vals.append(v)
        vals.append(user_id)
        cursor = self._conn.execute(
            f"UPDATE users SET {sets} WHERE user_id = ?", vals
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_all_active_users(self) -> list[dict]:
        cursor = self._conn.execute("SELECT * FROM users WHERE active = 1")
        columns = [desc[0] for desc in cursor.description]
        results = []
        for row in cursor.fetchall():
            d = dict(zip(columns, row))
            d["onboarded"] = bool(d["onboarded"])
            d["approved"] = bool(d.get("approved"))
            d["active"] = bool(d["active"])
            if d.get("preferences"):
                d["preferences"] = json.loads(d["preferences"])
            results.append(d)
        return results

    # --- Routes ---

    def get_active_routes(
        self, user_id: str | None = None, include_snoozed: bool = False
    ) -> list[Route]:
        params: list = []
        sql = "SELECT * FROM routes WHERE active = 1"
        sql += _user_filter(user_id, params)
        if not include_snoozed:
            # Filter routes whose snooze has not expired. NULL snoozed_until means active.
            now_iso = _to_isoformat(datetime.now(UTC))
            sql += " AND (snoozed_until IS NULL OR snoozed_until <= ?)"
            params.append(now_iso)
        cursor = self._conn.execute(sql, params)
        columns = [desc[0] for desc in cursor.description]
        return [Route.from_row(row, columns) for row in cursor.fetchall()]

    def snooze_route(self, route_id: str, days: int) -> None:
        """Set `snoozed_until = now + days` on a route. Used by /snooze and auto-snooze on book."""
        until = datetime.now(UTC) + timedelta(days=int(days))
        self._conn.execute(
            "UPDATE routes SET snoozed_until = ? WHERE route_id = ?",
            [_to_isoformat(until), route_id],
        )
        self._conn.commit()

    def unsnooze_route(self, route_id: str) -> None:
        """Clear snoozed_until on a route. Used by /unsnooze."""
        self._conn.execute(
            "UPDATE routes SET snoozed_until = NULL WHERE route_id = ?",
            [route_id],
        )
        self._conn.commit()

    def get_status_stats(self, user_id: str) -> dict:
        """Aggregate stats for /status — counts and timestamps for the user-facing dashboard."""
        now = datetime.now(UTC)
        now_iso = _to_isoformat(now)
        # Total + snoozed routes (active=1, regardless of snooze).
        total_active = self._conn.execute(
            "SELECT COUNT(*) FROM routes WHERE active = 1 AND user_id = ?", [user_id],
        ).fetchone()[0]
        snoozed = self._conn.execute(
            "SELECT COUNT(*) FROM routes WHERE active = 1 AND user_id = ? "
            "AND snoozed_until IS NOT NULL AND snoozed_until > ?",
            [user_id, now_iso],
        ).fetchone()[0]
        # Last poll = max observed_at across this user's snapshots.
        last_poll_row = self._conn.execute(
            "SELECT MAX(observed_at) FROM price_snapshots WHERE user_id = ?", [user_id],
        ).fetchone()
        last_poll = last_poll_row[0] if last_poll_row else None
        # Alerts this week with feedback breakdown.
        week_ago = _to_isoformat(now - timedelta(days=7))
        alert_rows = self._conn.execute(
            "SELECT feedback, COUNT(*) FROM deals WHERE user_id = ? AND alert_sent = 1 "
            "AND alert_sent_at IS NOT NULL AND alert_sent_at >= ? GROUP BY feedback",
            [user_id, week_ago],
        ).fetchall()
        feedback_breakdown: dict[str, int] = {}
        total_alerts_week = 0
        for fb, cnt in alert_rows:
            label = fb or "no_response"
            feedback_breakdown[label] = cnt
            total_alerts_week += cnt
        # SerpAPI calls this month — proxy by counting unique snapshots in last 30 days
        # across this user. Approximate; the precise counter lives in-memory on SerpAPIClient.
        thirty_days_ago = _to_isoformat(now - timedelta(days=30))
        snapshots_30d = self._conn.execute(
            "SELECT COUNT(*) FROM price_snapshots WHERE user_id = ? AND observed_at >= ?",
            [user_id, thirty_days_ago],
        ).fetchone()[0]
        # Digest skip count + total savings come from existing helpers.
        user = self.get_user(user_id)
        digest_skip_count = (user or {}).get("digest_skip_count_7d") or 0
        savings = self.get_total_savings(user_id)
        return {
            "total_active": total_active,
            "snoozed": snoozed,
            "monitoring": total_active - snoozed,
            "last_poll": last_poll,
            "alerts_this_week": total_alerts_week,
            "feedback_breakdown": feedback_breakdown,
            "snapshots_30d": snapshots_30d,
            "digest_skip_count": digest_skip_count,
            "savings_total": savings.get("total", 0),
            "savings_route_count": savings.get("route_count", 0),
        }

    def upsert_route(self, route: Route, user_id: str | None = None) -> None:
        uid = user_id or route.user_id
        self._conn.execute(
            """
            INSERT INTO routes (
                route_id, origin, destination, trip_type,
                earliest_departure, latest_return, date_flex_days,
                max_stops, passengers, preferred_airlines, notes, active,
                trip_duration_type, trip_duration_days,
                preferred_departure_days, preferred_return_days, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (route_id) DO UPDATE SET
                origin = excluded.origin,
                destination = excluded.destination,
                trip_type = excluded.trip_type,
                earliest_departure = excluded.earliest_departure,
                latest_return = excluded.latest_return,
                date_flex_days = excluded.date_flex_days,
                max_stops = excluded.max_stops,
                passengers = excluded.passengers,
                preferred_airlines = excluded.preferred_airlines,
                notes = excluded.notes,
                active = excluded.active,
                trip_duration_type = excluded.trip_duration_type,
                trip_duration_days = excluded.trip_duration_days,
                preferred_departure_days = excluded.preferred_departure_days,
                preferred_return_days = excluded.preferred_return_days,
                user_id = excluded.user_id
            """,
            [
                route.route_id,
                route.origin,
                route.destination,
                route.trip_type,
                _to_isoformat(route.earliest_departure),
                _to_isoformat(route.latest_return),
                route.date_flex_days,
                route.max_stops,
                route.passengers,
                json.dumps(route.preferred_airlines) if route.preferred_airlines else None,
                route.notes,
                1 if route.active else 0,
                route.trip_duration_type,
                route.trip_duration_days,
                _to_json(route.preferred_departure_days),
                _to_json(route.preferred_return_days),
                uid,
            ],
        )
        self._conn.commit()

    def deactivate_route(self, route_id: str) -> None:
        self._conn.execute(
            "UPDATE routes SET active = 0 WHERE route_id = ?",
            [route_id],
        )
        self._conn.commit()

    def update_route(self, route_id: str, **fields) -> bool:
        """Update specific fields on a route. Returns True if a row was updated."""
        allowed = {
            "origin", "destination", "trip_type", "earliest_departure",
            "latest_return", "date_flex_days", "max_stops", "passengers",
            "preferred_airlines", "notes", "active",
            "trip_duration_type", "trip_duration_days",
            "preferred_departure_days", "preferred_return_days",
        }
        json_fields = {"preferred_departure_days", "preferred_return_days"}
        to_update = {k: v for k, v in fields.items() if k in allowed}
        if not to_update:
            return False
        sets = ", ".join(f"{k} = ?" for k in to_update)
        vals = []
        for k, v in to_update.items():
            if k in ("earliest_departure", "latest_return"):
                vals.append(_to_isoformat(v))
            elif k in json_fields:
                vals.append(_to_json(v))
            else:
                vals.append(v)
        vals.append(route_id)
        cursor = self._conn.execute(
            f"UPDATE routes SET {sets} WHERE route_id = ?",
            vals,
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # --- Snapshots ---

    def insert_snapshot(self, snapshot: PriceSnapshot, user_id: str | None = None) -> None:
        uid = user_id or snapshot.user_id
        self._conn.execute(
            """
            INSERT INTO price_snapshots (
                snapshot_id, route_id, window_id, observed_at, source,
                outbound_date, return_date, passengers, lowest_price,
                currency, best_flight, all_flights, price_level,
                typical_low, typical_high, price_history, search_params,
                baggage_estimate, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                snapshot.snapshot_id,
                snapshot.route_id,
                snapshot.window_id,
                _to_isoformat(snapshot.observed_at),
                snapshot.source,
                _to_isoformat(snapshot.outbound_date),
                _to_isoformat(snapshot.return_date),
                snapshot.passengers,
                float(snapshot.lowest_price) if snapshot.lowest_price is not None else None,
                snapshot.currency,
                _to_json(snapshot.best_flight),
                _to_json(snapshot.all_flights),
                snapshot.price_level,
                float(snapshot.typical_low) if snapshot.typical_low is not None else None,
                float(snapshot.typical_high) if snapshot.typical_high is not None else None,
                _to_json(snapshot.price_history),
                _to_json(snapshot.search_params),
                _to_json(snapshot.baggage_estimate),
                uid,
            ],
        )
        self._conn.commit()

    def get_price_history(self, route_id: str, days: int = 90, user_id: str | None = None) -> dict:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        params: list = [route_id, _to_isoformat(cutoff)]
        sql = """
            SELECT
                AVG(lowest_price) AS avg_price,
                MIN(lowest_price) AS min_price,
                MAX(lowest_price) AS max_price,
                COUNT(*) AS sample_count
            FROM price_snapshots
            WHERE route_id = ?
              AND observed_at >= ?
              AND lowest_price IS NOT NULL
        """
        sql += _user_filter(user_id, params)
        row = self._conn.execute(sql, params).fetchone()
        return {
            "avg_price": row[0],
            "min_price": row[1],
            "max_price": row[2],
            "count": row[3],
        }

    def get_recent_snapshots(
        self, route_id: str, limit: int = 10
    ) -> list[PriceSnapshot]:
        cursor = self._conn.execute(
            """
            SELECT * FROM price_snapshots
            WHERE route_id = ?
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            [route_id, limit],
        )
        columns = [desc[0] for desc in cursor.description]
        return [PriceSnapshot.from_row(row, columns) for row in cursor.fetchall()]

    # --- Deals ---

    def insert_deal(self, deal: Deal, user_id: str | None = None) -> None:
        uid = user_id or deal.user_id
        self._conn.execute(
            """
            INSERT INTO deals (
                deal_id, snapshot_id, route_id, score, urgency,
                reasoning, reasoning_json, booking_url, alert_sent, alert_sent_at,
                booked, feedback, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                deal.deal_id,
                deal.snapshot_id,
                deal.route_id,
                float(deal.score) if deal.score is not None else None,
                deal.urgency,
                deal.reasoning,
                _to_json(deal.reasoning_json),
                deal.booking_url,
                1 if deal.alert_sent else 0,
                _to_isoformat(deal.alert_sent_at),
                1 if deal.booked else 0,
                deal.feedback,
                uid,
            ],
        )
        self._conn.commit()

    def update_deal_feedback(self, deal_id: str, feedback: str) -> None:
        self._conn.execute(
            "UPDATE deals SET feedback = ? WHERE deal_id = ?",
            [feedback, deal_id],
        )
        self._conn.commit()

    def get_deals_pending_feedback(self, older_than_days: int = 3) -> list[dict]:
        """Return deals where alert was sent 3+ days ago with no feedback and < 2 follow-ups."""
        cutoff = _to_isoformat(datetime.now(UTC) - timedelta(days=older_than_days))
        second_cutoff = _to_isoformat(datetime.now(UTC) - timedelta(days=older_than_days + 4))
        cursor = self._conn.execute(
            """
            SELECT
                d.deal_id, d.route_id, r.origin, r.destination,
                ps.lowest_price AS price
            FROM deals d
            JOIN routes r ON d.route_id = r.route_id
            LEFT JOIN price_snapshots ps ON d.snapshot_id = ps.snapshot_id
            WHERE d.alert_sent = 1
              AND d.feedback IS NULL
              AND d.alert_sent_at IS NOT NULL
              AND d.alert_sent_at <= ?
              AND (d.follow_up_count < 2)
              AND (d.follow_up_sent_at IS NULL OR d.follow_up_sent_at <= ?)
            ORDER BY d.alert_sent_at ASC
            """,
            [cutoff, second_cutoff],
        )
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def mark_follow_up_sent(self, deal_id: str) -> None:
        self._conn.execute(
            "UPDATE deals SET follow_up_sent_at = ?, follow_up_count = follow_up_count + 1 WHERE deal_id = ?",
            [_to_isoformat(datetime.now(UTC)), deal_id],
        )
        self._conn.commit()

    def expire_stale_deals(self) -> None:
        self._conn.execute(
            "UPDATE deals SET feedback = 'expired' WHERE follow_up_count >= 2 AND feedback IS NULL"
        )
        self._conn.commit()

    def get_routes_with_pending_deals(self, user_id: str | None = None) -> dict[str, dict]:
        """Return route_ids where deals exist with alert_sent=1 and feedback IS NULL.

        Returns dict of route_id -> {"price": float|None, "deal_ids": list[str]}.
        Snoozed routes are excluded (Condition C8 — digest must respect snoozes).
        """
        params: list = []
        now_iso = _to_isoformat(datetime.now(UTC))
        sql = """
            SELECT d.route_id, ps.lowest_price, d.deal_id
            FROM deals d
            LEFT JOIN price_snapshots ps ON d.snapshot_id = ps.snapshot_id
            JOIN routes r ON d.route_id = r.route_id
            WHERE d.alert_sent = 1
              AND d.feedback IS NULL
              AND (r.snoozed_until IS NULL OR r.snoozed_until <= ?)
        """
        params.append(now_iso)
        if user_id is not None:
            params.append(user_id)
            sql += " AND d.user_id = ?"
        sql += " ORDER BY d.created_at DESC"
        cursor = self._conn.execute(sql, params)
        result: dict[str, dict] = {}
        for row in cursor.fetchall():
            route_id = row[0]
            if route_id not in result:
                result[route_id] = {
                    "price": float(row[1]) if row[1] is not None else None,
                    "deal_ids": [],
                }
            result[route_id]["deal_ids"].append(row[2])
        return result

    def bulk_dismiss_route_deals(self, route_id: str, user_id: str) -> int:
        cursor = self._conn.execute(
            "UPDATE deals SET feedback = 'dismissed' WHERE route_id = ? AND user_id = ? AND feedback IS NULL",
            [route_id, user_id],
        )
        self._conn.commit()
        return cursor.rowcount

    def get_recent_feedback(self, limit: int = 20) -> list[dict]:
        cursor = self._conn.execute(
            """
            SELECT
                d.deal_id, d.route_id, r.origin, r.destination,
                ps.lowest_price AS price, d.score, d.urgency,
                d.feedback, d.reasoning
            FROM deals d
            JOIN routes r ON d.route_id = r.route_id
            LEFT JOIN price_snapshots ps ON d.snapshot_id = ps.snapshot_id
            WHERE d.feedback IS NOT NULL
            ORDER BY d.created_at DESC
            LIMIT ?
            """,
            [limit],
        )
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    # --- Poll Windows ---

    def get_poll_windows(self, route_id: str) -> list[PollWindow]:
        cursor = self._conn.execute(
            "SELECT * FROM poll_windows WHERE route_id = ? ORDER BY outbound_date",
            [route_id],
        )
        columns = [desc[0] for desc in cursor.description]
        return [PollWindow.from_row(row, columns) for row in cursor.fetchall()]

    def update_poll_window(
        self,
        route_id: str,
        window_start: date,
        window_end: date | None,
        latest_price: float | None,
    ) -> None:
        existing = self._conn.execute(
            """
            SELECT window_id, lowest_seen_price FROM poll_windows
            WHERE route_id = ? AND outbound_date = ?
            """,
            [route_id, _to_isoformat(window_start)],
        ).fetchone()

        now = datetime.now(UTC)

        if existing:
            window_id, current_lowest = existing
            new_lowest = latest_price
            if current_lowest is not None and latest_price is not None:
                new_lowest = min(float(current_lowest), latest_price)
            priority = (
                "focus"
                if new_lowest == latest_price and latest_price is not None
                else "normal"
            )
            self._conn.execute(
                """
                UPDATE poll_windows
                SET last_polled_at = ?,
                    lowest_seen_price = ?,
                    return_date = ?,
                    priority = ?
                WHERE window_id = ?
                """,
                [_to_isoformat(now), new_lowest, _to_isoformat(window_end), priority, window_id],
            )
        else:
            window_id = uuid4().hex
            self._conn.execute(
                """
                INSERT INTO poll_windows (
                    window_id, route_id, outbound_date, return_date,
                    priority, last_polled_at, lowest_seen_price
                ) VALUES (?, ?, ?, ?, 'normal', ?, ?)
                """,
                [window_id, route_id, _to_isoformat(window_start),
                 _to_isoformat(window_end), _to_isoformat(now), latest_price],
            )
        self._conn.commit()

    def get_deals_since(self, route_id: str, since: datetime, user_id: str | None = None) -> list[Deal]:
        params: list = [route_id, _to_isoformat(since)]
        sql = """
            SELECT * FROM deals
            WHERE route_id = ? AND created_at >= ?
        """
        sql += _user_filter(user_id, params)
        sql += " ORDER BY created_at DESC"
        cursor = self._conn.execute(sql, params)
        columns = [desc[0] for desc in cursor.description]
        return [Deal.from_row(row, columns) for row in cursor.fetchall()]

    def get_cheapest_recent_snapshot(self, route_id: str, days: int = 7, user_id: str | None = None) -> PriceSnapshot | None:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        params: list = [route_id, _to_isoformat(cutoff)]
        sql = """
            SELECT * FROM price_snapshots
            WHERE route_id = ? AND lowest_price IS NOT NULL
              AND observed_at >= ?
        """
        sql += _user_filter(user_id, params)
        sql += " ORDER BY lowest_price ASC LIMIT 1"
        cursor = self._conn.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cursor.description]
        return PriceSnapshot.from_row(row, columns)

    def get_latest_snapshot(self, route_id: str, user_id: str | None = None) -> PriceSnapshot | None:
        params: list = [route_id]
        sql = """
            SELECT * FROM price_snapshots
            WHERE route_id = ? AND lowest_price IS NOT NULL
        """
        sql += _user_filter(user_id, params)
        sql += " ORDER BY observed_at DESC LIMIT 1"
        cursor = self._conn.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cursor.description]
        return PriceSnapshot.from_row(row, columns)

    # --- Alert Dedup Helpers ---

    def get_last_alerted_price(self, route_id: str, user_id: str | None = None) -> float | None:
        """Return the lowest_price from the most recent deal where an alert was sent."""
        params: list = [route_id]
        sql = """
            SELECT ps.lowest_price
            FROM deals d
            JOIN price_snapshots ps ON d.snapshot_id = ps.snapshot_id
            WHERE d.route_id = ?
              AND d.alert_sent = 1
              AND ps.lowest_price IS NOT NULL
        """
        if user_id is not None:
            sql += " AND d.user_id = ?"
            params.append(user_id)
        sql += " ORDER BY d.alert_sent_at DESC LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def detect_price_inflection(self, route_id: str, user_id: str | None = None) -> tuple[bool, float | None]:
        """Check if price was dropping for 3+ snapshots then ticked up.

        Returns (inflection_detected, bottom_price).
        """
        params: list = [route_id]
        sql = """
            SELECT lowest_price
            FROM price_snapshots
            WHERE route_id = ?
              AND lowest_price IS NOT NULL
        """
        sql += _user_filter(user_id, params)
        sql += " ORDER BY observed_at DESC LIMIT 5"
        cursor = self._conn.execute(sql, params)
        prices = [float(row[0]) for row in cursor.fetchall()]

        # Need at least 4 snapshots: current (up-tick) + 3 consecutive drops before it
        if len(prices) < 4:
            return False, None

        # prices[0] is most recent, prices[1] is previous, etc.
        # Check: prices[0] > prices[1] (just ticked up)
        if prices[0] <= prices[1]:
            return False, None

        # Check: prices[1] < prices[2] < prices[3] (was dropping for 3+ consecutive)
        for i in range(1, len(prices) - 1):
            if prices[i] >= prices[i + 1]:
                return False, None

        return True, prices[1]  # bottom is the previous snapshot

    # --- Alert Rules ---

    def get_alert_rules(self, route_id: str) -> list[AlertRule]:
        cursor = self._conn.execute(
            "SELECT * FROM alert_rules WHERE route_id = ? AND active = 1",
            [route_id],
        )
        columns = [desc[0] for desc in cursor.description]
        return [AlertRule.from_row(row, columns) for row in cursor.fetchall()]

    # --- Airport Transport ---

    def seed_airport_transport(self, airports: list[dict], user_id: str | None = None) -> None:
        for ap in airports:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO airport_transport (
                    airport_code, airport_name, transport_mode,
                    transport_cost_eur, transport_time_min,
                    parking_cost_eur, is_primary, user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ap["code"],
                    ap.get("name"),
                    ap.get("transport_mode"),
                    ap.get("transport_cost_eur"),
                    ap.get("transport_time_min"),
                    ap.get("parking_cost_eur"),
                    1 if ap.get("is_primary") else 0,
                    user_id,
                ],
            )
        self._conn.commit()

    def get_airport_transport(self, code: str, user_id: str | None = None) -> dict | None:
        params: list = [code]
        sql = "SELECT * FROM airport_transport WHERE airport_code = ?"
        sql += _user_filter(user_id, params)
        row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None
        columns = ["airport_code", "airport_name", "transport_mode",
                    "transport_cost_eur", "transport_time_min",
                    "parking_cost_eur", "is_primary", "user_id"]
        result = dict(zip(columns, row))
        result["is_primary"] = bool(result["is_primary"])
        return result

    def get_all_airport_transports(self, user_id: str | None = None) -> list[dict]:
        params: list = []
        sql = "SELECT * FROM airport_transport WHERE 1=1"
        sql += _user_filter(user_id, params)
        cursor = self._conn.execute(sql, params)
        columns = ["airport_code", "airport_name", "transport_mode",
                    "transport_cost_eur", "transport_time_min",
                    "parking_cost_eur", "is_primary", "user_id"]
        results = []
        for row in cursor.fetchall():
            d = dict(zip(columns, row))
            d["is_primary"] = bool(d["is_primary"])
            results.append(d)
        return results

    def get_primary_airport(self, user_id: str | None = None) -> dict | None:
        params: list = []
        sql = "SELECT * FROM airport_transport WHERE is_primary = 1"
        sql += _user_filter(user_id, params)
        sql += " LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None
        columns = ["airport_code", "airport_name", "transport_mode",
                    "transport_cost_eur", "transport_time_min",
                    "parking_cost_eur", "is_primary", "user_id"]
        result = dict(zip(columns, row))
        result["is_primary"] = True
        return result

    def get_nearby_snapshots(self, route_id: str, primary_origin: str) -> list[dict]:
        """Return latest snapshot per secondary airport for this route (last 7 days).

        v0.11.7: now also returns `baggage_estimate` so the deal page's
        Alternatives table can compute baggage-inclusive door-to-door totals
        consistent with the deal hero.
        """
        cutoff = _to_isoformat(datetime.now(UTC) - timedelta(days=7))
        cursor = self._conn.execute(
            """
            SELECT
                json_extract(search_params, '$.origin') AS origin,
                lowest_price,
                observed_at,
                baggage_estimate
            FROM price_snapshots
            WHERE route_id = ?
              AND search_params IS NOT NULL
              AND json_extract(search_params, '$.origin') IS NOT NULL
              AND json_extract(search_params, '$.origin') != ?
              AND lowest_price IS NOT NULL
              AND observed_at >= ?
            ORDER BY observed_at DESC
            """,
            [route_id, primary_origin, cutoff],
        )
        seen: dict[str, dict] = {}
        for row in cursor.fetchall():
            origin = row[0]
            if origin not in seen:
                seen[origin] = {
                    "airport_code": origin,
                    "lowest_price": row[1],
                    "baggage_estimate": row[3],  # raw JSON string; caller parses
                }
        return list(seen.values())

    def get_secondary_airports(self, user_id: str | None = None) -> list[dict]:
        params: list = []
        sql = "SELECT * FROM airport_transport WHERE is_primary = 0"
        sql += _user_filter(user_id, params)
        cursor = self._conn.execute(sql, params)
        columns = ["airport_code", "airport_name", "transport_mode",
                    "transport_cost_eur", "transport_time_min",
                    "parking_cost_eur", "is_primary", "user_id"]
        results = []
        for row in cursor.fetchall():
            d = dict(zip(columns, row))
            d["is_primary"] = False
            results.append(d)
        return results

    # --- R9 ITEM-053: Multi-mode Transport Options ---

    _OPTION_COLUMNS = [
        "user_id", "airport_code", "mode", "cost_eur", "cost_scales_with_pax",
        "time_min", "parking_cost_per_day_eur", "enabled", "source", "confidence",
        "label", "created_at", "updated_at",
    ]

    def _row_to_option(self, row) -> dict:
        d = dict(zip(self._OPTION_COLUMNS, row))
        d["enabled"] = bool(d["enabled"])
        d["cost_scales_with_pax"] = bool(d["cost_scales_with_pax"])
        return d

    def get_transport_options(
        self, airport_code: str, user_id: str, *, include_disabled: bool = False
    ) -> list[dict]:
        """Return all transport options for an airport. Empty list if none configured."""
        sql = (
            "SELECT user_id, airport_code, mode, cost_eur, cost_scales_with_pax, "
            "time_min, parking_cost_per_day_eur, enabled, source, confidence, "
            "label, created_at, updated_at "
            "FROM airport_transport_option WHERE user_id = ? AND airport_code = ?"
        )
        params: list = [user_id, airport_code]
        if not include_disabled:
            sql += " AND enabled = 1"
        cursor = self._conn.execute(sql, params)
        return [self._row_to_option(row) for row in cursor.fetchall()]

    def get_all_transport_options(self, user_id: str) -> list[dict]:
        """Return all options across all airports for a user, sorted by airport then mode."""
        cursor = self._conn.execute(
            "SELECT user_id, airport_code, mode, cost_eur, cost_scales_with_pax, "
            "time_min, parking_cost_per_day_eur, enabled, source, confidence, "
            "label, created_at, updated_at "
            "FROM airport_transport_option WHERE user_id = ? "
            "ORDER BY airport_code, mode",
            [user_id],
        )
        return [self._row_to_option(row) for row in cursor.fetchall()]

    def add_transport_option(
        self,
        *,
        user_id: str,
        airport_code: str,
        mode: str,
        cost_eur: float | None,
        cost_scales_with_pax: bool,
        time_min: int | None = None,
        parking_cost_per_day_eur: float | None = None,
        source: str = "user_added",
        confidence: str = "high",
        label: str | None = None,
        enabled: bool = True,
    ) -> None:
        """Insert (or replace) a transport option for an airport.

        Replace-on-conflict means re-running onboarding with the same airport+mode
        updates the row instead of erroring. User edits go through update_transport_option().
        """
        self._conn.execute(
            """
            INSERT INTO airport_transport_option (
                user_id, airport_code, mode, cost_eur, cost_scales_with_pax,
                time_min, parking_cost_per_day_eur, enabled, source, confidence, label
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, airport_code, mode) DO UPDATE SET
                cost_eur = excluded.cost_eur,
                cost_scales_with_pax = excluded.cost_scales_with_pax,
                time_min = excluded.time_min,
                parking_cost_per_day_eur = excluded.parking_cost_per_day_eur,
                enabled = excluded.enabled,
                source = excluded.source,
                confidence = excluded.confidence,
                label = excluded.label,
                updated_at = datetime('now')
            """,
            [
                user_id, airport_code, mode, cost_eur,
                1 if cost_scales_with_pax else 0,
                time_min, parking_cost_per_day_eur,
                1 if enabled else 0,
                source, confidence, label,
            ],
        )
        self._conn.commit()

    def update_transport_option(
        self,
        *,
        user_id: str,
        airport_code: str,
        mode: str,
        cost_eur: float | None = None,
        time_min: int | None = None,
        parking_cost_per_day_eur: float | None = None,
        enabled: bool | None = None,
        label: str | None = None,
    ) -> bool:
        """Patch fields on an existing option. Returns True if a row was updated.

        Promotes source to 'user_override' on any user-driven edit so we know not
        to overwrite it on a future auto-fill pass.
        """
        sets: list[str] = ["source = 'user_override'", "updated_at = datetime('now')"]
        params: list = []
        if cost_eur is not None:
            sets.append("cost_eur = ?")
            params.append(cost_eur)
        if time_min is not None:
            sets.append("time_min = ?")
            params.append(time_min)
        if parking_cost_per_day_eur is not None:
            sets.append("parking_cost_per_day_eur = ?")
            params.append(parking_cost_per_day_eur)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(1 if enabled else 0)
        if label is not None:
            sets.append("label = ?")
            params.append(label)
        params.extend([user_id, airport_code, mode])
        sql = (
            "UPDATE airport_transport_option SET " + ", ".join(sets) +
            " WHERE user_id = ? AND airport_code = ? AND mode = ?"
        )
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_transport_option(
        self, *, user_id: str, airport_code: str, mode: str
    ) -> bool:
        """Hard-delete an option. Use update_transport_option(enabled=False) to soft-disable."""
        cursor = self._conn.execute(
            "DELETE FROM airport_transport_option "
            "WHERE user_id = ? AND airport_code = ? AND mode = ?",
            [user_id, airport_code, mode],
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_airport_override_mode(self, airport_code: str, user_id: str) -> str | None:
        """Return the user's per-airport 'always use [mode]' override, if set."""
        row = self._conn.execute(
            "SELECT airport_override_mode FROM users WHERE user_id = ?",
            [user_id],
        ).fetchone()
        if not row or not row[0]:
            return None
        try:
            overrides = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        if not isinstance(overrides, dict):
            return None
        val = overrides.get(airport_code)
        return val if isinstance(val, str) and val else None

    def get_resolved_transport(
        self,
        airport_code: str,
        user_id: str | None = None,
        *,
        passengers: int = 1,
        trip_days: int = 0,
    ) -> dict | None:
        """Drop-in replacement for get_airport_transport() that resolves
        multiple airport_transport_option rows to the cheapest mode for the
        given party + duration.

        Returns the same shape as get_airport_transport() (so existing dict-
        accessing callers keep working) plus four R9 fields:
            mode_label    — display string e.g. "train (cheapest)"
            is_cheapest   — True if cost-optimal, False if override forced a different mode
            override_used — True if the per-airport override was honored
            no_options    — True if user has no options configured (legacy data also missing)

        Falls back to legacy get_airport_transport() data when the user has
        no rows in airport_transport_option for this airport. This handles
        users who exist before migration completes (no data loss); the
        forward-migration in init_schema() ensures fallback is the exception
        not the rule.
        """
        from src.analysis.transport import resolve_breakdown_inputs  # local — avoid circular import

        if not user_id:
            # Pre-multi-user callers (or tests). Defer to legacy path.
            return self.get_airport_transport(airport_code, user_id=user_id)

        options = self.get_transport_options(airport_code, user_id, include_disabled=False)
        if not options:
            # No options yet — fall back to legacy single-mode row, if any.
            legacy = self.get_airport_transport(airport_code, user_id=user_id)
            if legacy is None:
                return None
            return {
                **legacy,
                "mode_label": legacy.get("transport_mode") or "",
                "is_cheapest": False,
                "override_used": False,
                "no_options": False,
            }

        override_mode = self.get_airport_override_mode(airport_code, user_id)
        resolved = resolve_breakdown_inputs(
            options, passengers=passengers, trip_days=trip_days,
            override_mode=override_mode,
        )
        # Legacy primary-row metadata (airport_name, is_primary) — read once if present.
        legacy_meta = self.get_airport_transport(airport_code, user_id=user_id) or {}
        mode = resolved["mode"]
        if resolved["override_used"]:
            mode_label = f"{mode} (your choice)"
        elif resolved["is_cheapest"] and len(options) > 1:
            mode_label = f"{mode} (cheapest)"
        else:
            mode_label = mode
        return {
            "airport_code": airport_code,
            "airport_name": legacy_meta.get("airport_name"),
            "transport_mode": mode,
            "transport_cost_eur": resolved["transport_cost_eur"],
            "transport_time_min": resolved["transport_time_min"],
            "parking_cost_eur": resolved["parking_cost_eur"],
            "is_primary": legacy_meta.get("is_primary", False),
            "user_id": user_id,
            "mode_label": mode_label,
            "is_cheapest": resolved["is_cheapest"],
            "override_used": resolved["override_used"],
            "no_options": resolved["no_options"],
        }

    def set_airport_override_mode(
        self, *, user_id: str, airport_code: str, mode: str | None
    ) -> None:
        """Set or clear the per-airport mode override. Pass mode=None to clear."""
        row = self._conn.execute(
            "SELECT airport_override_mode FROM users WHERE user_id = ?",
            [user_id],
        ).fetchone()
        try:
            overrides = json.loads(row[0]) if row and row[0] else {}
        except (TypeError, ValueError):
            overrides = {}
        if not isinstance(overrides, dict):
            overrides = {}
        if mode is None:
            overrides.pop(airport_code, None)
        else:
            overrides[airport_code] = mode
        self._conn.execute(
            "UPDATE users SET airport_override_mode = ? WHERE user_id = ?",
            [json.dumps(overrides) if overrides else None, user_id],
        )
        self._conn.commit()

    # --- Savings ---

    def log_saving(
        self,
        user_id: str,
        route_id: str,
        primary_cost: float,
        alternative_cost: float,
        savings_amount: float,
        airport_code: str,
        snapshot_date: str,
        deal_id: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO savings_log (
                user_id, deal_id, route_id, primary_cost,
                alternative_cost, savings_amount, airport_code, snapshot_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [user_id, deal_id, route_id, primary_cost,
             alternative_cost, savings_amount, airport_code, snapshot_date],
        )
        self._conn.commit()

    def get_total_savings(self, user_id: str) -> dict:
        cursor = self._conn.execute(
            """
            SELECT route_id, airport_code, MAX(savings_amount) AS best_saving
            FROM savings_log
            WHERE user_id = ?
            GROUP BY route_id, airport_code
            """,
            [user_id],
        )
        rows = cursor.fetchall()
        if not rows:
            return {"total": 0, "route_count": 0, "details": []}
        route_best: dict[str, dict] = {}
        for route_id, airport_code, best_saving in rows:
            if route_id not in route_best or best_saving > route_best[route_id]["savings"]:
                route_best[route_id] = {
                    "route_id": route_id,
                    "airport_code": airport_code,
                    "savings": float(best_saving),
                }
        details = list(route_best.values())
        total = sum(d["savings"] for d in details)
        return {"total": total, "route_count": len(details), "details": details}
