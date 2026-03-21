from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import os

import duckdb

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
    route_id          VARCHAR PRIMARY KEY,
    origin            VARCHAR NOT NULL,
    destination       VARCHAR NOT NULL,
    trip_type         VARCHAR DEFAULT 'round_trip',
    earliest_departure DATE,
    latest_return     DATE,
    date_flex_days    INTEGER DEFAULT 3,
    max_stops         INTEGER DEFAULT 1,
    passengers        INTEGER DEFAULT 2,
    preferred_airlines VARCHAR[],
    notes             VARCHAR,
    active            BOOLEAN DEFAULT true,
    created_at        TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS poll_windows (
    window_id         VARCHAR PRIMARY KEY,
    route_id          VARCHAR REFERENCES routes(route_id),
    outbound_date     DATE NOT NULL,
    return_date       DATE,
    priority          VARCHAR DEFAULT 'normal',
    last_polled_at    TIMESTAMP,
    lowest_seen_price DECIMAL(10,2),
    created_at        TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    snapshot_id       VARCHAR PRIMARY KEY,
    route_id          VARCHAR REFERENCES routes(route_id),
    window_id         VARCHAR REFERENCES poll_windows(window_id),
    observed_at       TIMESTAMP NOT NULL,
    source            VARCHAR NOT NULL,
    outbound_date     DATE,
    return_date       DATE,
    passengers        INTEGER NOT NULL,
    lowest_price      DECIMAL(10,2),
    currency          VARCHAR DEFAULT 'EUR',
    best_flight       JSON,
    all_flights       JSON,
    price_level       VARCHAR,
    typical_low       DECIMAL(10,2),
    typical_high      DECIMAL(10,2),
    price_history     JSON,
    search_params     JSON,
    created_at        TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deals (
    deal_id           VARCHAR PRIMARY KEY,
    snapshot_id       VARCHAR REFERENCES price_snapshots(snapshot_id),
    route_id          VARCHAR REFERENCES routes(route_id),
    score             DECIMAL(3,2),
    urgency           VARCHAR,
    reasoning         VARCHAR,
    booking_url       VARCHAR,
    alert_sent        BOOLEAN DEFAULT false,
    alert_sent_at     TIMESTAMP,
    booked            BOOLEAN DEFAULT false,
    feedback          VARCHAR,
    created_at        TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alert_rules (
    rule_id           VARCHAR PRIMARY KEY,
    route_id          VARCHAR REFERENCES routes(route_id),
    rule_type         VARCHAR NOT NULL,
    threshold         DECIMAL(10,2),
    channel           VARCHAR NOT NULL,
    active            BOOLEAN DEFAULT true
);
"""


def _to_json(val) -> str | None:
    if val is None:
        return None
    return json.dumps(val)


class Database:
    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            data_dir = os.environ.get("FAREHOUND_DATA_DIR", "data")
            db_path = Path(data_dir) / "flights.duckdb"
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self._db_path))

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        self._conn.execute(_SCHEMA_SQL)

    # --- Routes ---

    def get_active_routes(self) -> list[Route]:
        result = self._conn.execute(
            "SELECT * FROM routes WHERE active = true"
        )
        columns = [desc[0] for desc in result.description]
        return [Route.from_row(row, columns) for row in result.fetchall()]

    def upsert_route(self, route: Route) -> None:
        self._conn.execute(
            """
            INSERT INTO routes (
                route_id, origin, destination, trip_type,
                earliest_departure, latest_return, date_flex_days,
                max_stops, passengers, preferred_airlines, notes, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (route_id) DO UPDATE SET
                origin = EXCLUDED.origin,
                destination = EXCLUDED.destination,
                trip_type = EXCLUDED.trip_type,
                earliest_departure = EXCLUDED.earliest_departure,
                latest_return = EXCLUDED.latest_return,
                date_flex_days = EXCLUDED.date_flex_days,
                max_stops = EXCLUDED.max_stops,
                passengers = EXCLUDED.passengers,
                preferred_airlines = EXCLUDED.preferred_airlines,
                notes = EXCLUDED.notes,
                active = EXCLUDED.active
            """,
            [
                route.route_id,
                route.origin,
                route.destination,
                route.trip_type,
                route.earliest_departure,
                route.latest_return,
                route.date_flex_days,
                route.max_stops,
                route.passengers,
                route.preferred_airlines,
                route.notes,
                route.active,
            ],
        )

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
                snapshot.observed_at,
                snapshot.source,
                snapshot.outbound_date,
                snapshot.return_date,
                snapshot.passengers,
                snapshot.lowest_price,
                snapshot.currency,
                _to_json(snapshot.best_flight),
                _to_json(snapshot.all_flights),
                snapshot.price_level,
                snapshot.typical_low,
                snapshot.typical_high,
                _to_json(snapshot.price_history),
                _to_json(snapshot.search_params),
            ],
        )

    def get_price_history(self, route_id: str, days: int = 90) -> dict:
        row = self._conn.execute(
            f"""
            SELECT
                AVG(lowest_price) AS avg_price,
                MIN(lowest_price) AS min_price,
                MAX(lowest_price) AS max_price,
                COUNT(*) AS sample_count
            FROM price_snapshots
            WHERE route_id = ?
              AND observed_at >= now() - INTERVAL '{days} days'
              AND lowest_price IS NOT NULL
            """,
            [route_id],
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
        result = self._conn.execute(
            """
            SELECT * FROM price_snapshots
            WHERE route_id = ?
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            [route_id, limit],
        )
        columns = [desc[0] for desc in result.description]
        return [PriceSnapshot.from_row(row, columns) for row in result.fetchall()]

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
                deal.score,
                deal.urgency,
                deal.reasoning,
                deal.booking_url,
                deal.alert_sent,
                deal.alert_sent_at,
                deal.booked,
                deal.feedback,
            ],
        )

    def update_deal_feedback(self, deal_id: str, feedback: str) -> None:
        self._conn.execute(
            "UPDATE deals SET feedback = ? WHERE deal_id = ?",
            [feedback, deal_id],
        )

    def get_recent_feedback(self, limit: int = 20) -> list[dict]:
        result = self._conn.execute(
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
        columns = [desc[0] for desc in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]

    # --- Poll Windows ---

    def get_poll_windows(self, route_id: str) -> list[PollWindow]:
        result = self._conn.execute(
            "SELECT * FROM poll_windows WHERE route_id = ? ORDER BY outbound_date",
            [route_id],
        )
        columns = [desc[0] for desc in result.description]
        return [PollWindow.from_row(row, columns) for row in result.fetchall()]

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
            [route_id, window_start],
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
                [now, new_lowest, window_end, priority, window_id],
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
                [window_id, route_id, window_start, window_end, now, latest_price],
            )

    def get_deals_since(self, route_id: str, since: datetime) -> list[Deal]:
        result = self._conn.execute(
            """
            SELECT * FROM deals
            WHERE route_id = ? AND created_at >= ?
            ORDER BY created_at DESC
            """,
            [route_id, since],
        )
        columns = [desc[0] for desc in result.description]
        return [Deal.from_row(row, columns) for row in result.fetchall()]

    def get_latest_snapshot(self, route_id: str) -> PriceSnapshot | None:
        result = self._conn.execute(
            """
            SELECT * FROM price_snapshots
            WHERE route_id = ? AND lowest_price IS NOT NULL
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            [route_id],
        )
        row = result.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in result.description]
        return PriceSnapshot.from_row(row, columns)

    # --- Alert Rules ---

    def get_alert_rules(self, route_id: str) -> list[AlertRule]:
        result = self._conn.execute(
            "SELECT * FROM alert_rules WHERE route_id = ? AND active = true",
            [route_id],
        )
        columns = [desc[0] for desc in result.description]
        return [AlertRule.from_row(row, columns) for row in result.fetchall()]
