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
    created_at        TEXT DEFAULT (datetime('now'))
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
"""


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

    # --- Routes ---

    def get_active_routes(self) -> list[Route]:
        cursor = self._conn.execute(
            "SELECT * FROM routes WHERE active = 1"
        )
        columns = [desc[0] for desc in cursor.description]
        return [Route.from_row(row, columns) for row in cursor.fetchall()]

    def upsert_route(self, route: Route) -> None:
        self._conn.execute(
            """
            INSERT INTO routes (
                route_id, origin, destination, trip_type,
                earliest_departure, latest_return, date_flex_days,
                max_stops, passengers, preferred_airlines, notes, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                active = excluded.active
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
            ],
        )
        self._conn.commit()

    def deactivate_route(self, route_id: str) -> None:
        self._conn.execute(
            "UPDATE routes SET active = 0 WHERE route_id = ?",
            [route_id],
        )
        self._conn.commit()

    # --- Snapshots ---

    def insert_snapshot(self, snapshot: PriceSnapshot) -> None:
        self._conn.execute(
            """
            INSERT INTO price_snapshots (
                snapshot_id, route_id, window_id, observed_at, source,
                outbound_date, return_date, passengers, lowest_price,
                currency, best_flight, all_flights, price_level,
                typical_low, typical_high, price_history, search_params
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ],
        )
        self._conn.commit()

    def get_price_history(self, route_id: str, days: int = 90) -> dict:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        row = self._conn.execute(
            """
            SELECT
                AVG(lowest_price) AS avg_price,
                MIN(lowest_price) AS min_price,
                MAX(lowest_price) AS max_price,
                COUNT(*) AS sample_count
            FROM price_snapshots
            WHERE route_id = ?
              AND observed_at >= ?
              AND lowest_price IS NOT NULL
            """,
            [route_id, _to_isoformat(cutoff)],
        ).fetchone()
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

    def insert_deal(self, deal: Deal) -> None:
        self._conn.execute(
            """
            INSERT INTO deals (
                deal_id, snapshot_id, route_id, score, urgency,
                reasoning, booking_url, alert_sent, alert_sent_at, booked, feedback
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ],
        )
        self._conn.commit()

    def update_deal_feedback(self, deal_id: str, feedback: str) -> None:
        self._conn.execute(
            "UPDATE deals SET feedback = ? WHERE deal_id = ?",
            [feedback, deal_id],
        )
        self._conn.commit()

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

    def get_deals_since(self, route_id: str, since: datetime) -> list[Deal]:
        cursor = self._conn.execute(
            """
            SELECT * FROM deals
            WHERE route_id = ? AND created_at >= ?
            ORDER BY created_at DESC
            """,
            [route_id, _to_isoformat(since)],
        )
        columns = [desc[0] for desc in cursor.description]
        return [Deal.from_row(row, columns) for row in cursor.fetchall()]

    def get_latest_snapshot(self, route_id: str) -> PriceSnapshot | None:
        cursor = self._conn.execute(
            """
            SELECT * FROM price_snapshots
            WHERE route_id = ? AND lowest_price IS NOT NULL
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            [route_id],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cursor.description]
        return PriceSnapshot.from_row(row, columns)

    # --- Alert Dedup Helpers ---

    def get_last_alerted_price(self, route_id: str) -> float | None:
        """Return the lowest_price from the most recent deal where an alert was sent."""
        row = self._conn.execute(
            """
            SELECT ps.lowest_price
            FROM deals d
            JOIN price_snapshots ps ON d.snapshot_id = ps.snapshot_id
            WHERE d.route_id = ?
              AND d.alert_sent = 1
              AND ps.lowest_price IS NOT NULL
            ORDER BY d.alert_sent_at DESC
            LIMIT 1
            """,
            [route_id],
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def detect_price_inflection(self, route_id: str) -> tuple[bool, float | None]:
        """Check if price was dropping for 3+ snapshots then ticked up.

        Returns (inflection_detected, bottom_price).
        """
        cursor = self._conn.execute(
            """
            SELECT lowest_price
            FROM price_snapshots
            WHERE route_id = ?
              AND lowest_price IS NOT NULL
            ORDER BY observed_at DESC
            LIMIT 5
            """,
            [route_id],
        )
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
