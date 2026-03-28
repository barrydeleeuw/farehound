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
        # Migrate existing data: create default user if needed
        self._migrate_default_user()

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
        allowed = {"name", "home_location", "home_airport", "preferences", "onboarded", "approved", "active"}
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

    def get_active_routes(self, user_id: str | None = None) -> list[Route]:
        params: list = []
        sql = "SELECT * FROM routes WHERE active = 1"
        sql += _user_filter(user_id, params)
        cursor = self._conn.execute(sql, params)
        columns = [desc[0] for desc in cursor.description]
        return [Route.from_row(row, columns) for row in cursor.fetchall()]

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
                typical_low, typical_high, price_history, search_params, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                reasoning, booking_url, alert_sent, alert_sent_at, booked, feedback, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                deal.deal_id,
                deal.snapshot_id,
                deal.route_id,
                float(deal.score) if deal.score is not None else None,
                deal.urgency,
                deal.reasoning,
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

    def get_routes_with_pending_deals(self, user_id: str | None = None) -> dict[str, float | None]:
        """Return route_ids where deals exist with alert_sent=1 and feedback IS NULL.

        Returns dict of route_id -> snapshot price at time of alert (for price change display).
        """
        params: list = []
        sql = """
            SELECT d.route_id, ps.lowest_price
            FROM deals d
            LEFT JOIN price_snapshots ps ON d.snapshot_id = ps.snapshot_id
            WHERE d.alert_sent = 1
              AND d.feedback IS NULL
        """
        if user_id is not None:
            params.append(user_id)
            sql += " AND d.user_id = ?"
        sql += " ORDER BY d.created_at DESC"
        cursor = self._conn.execute(sql, params)
        result: dict[str, float | None] = {}
        for row in cursor.fetchall():
            route_id = row[0]
            if route_id not in result:
                result[route_id] = float(row[1]) if row[1] is not None else None
        return result

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
        """Return latest snapshot per secondary airport for this route (last 7 days)."""
        cutoff = _to_isoformat(datetime.now(UTC) - timedelta(days=7))
        cursor = self._conn.execute(
            """
            SELECT
                json_extract(search_params, '$.origin') AS origin,
                lowest_price,
                observed_at
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
                seen[origin] = {"airport_code": origin, "lowest_price": row[1]}
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
