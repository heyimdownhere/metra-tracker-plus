"""Long-term history of realtime observations, stored in SQLite under /config.

Records, per service day, the latest realtime prediction for every
trip/stop pair (so the final value before a train leaves approximates what
actually happened), cancellations, and every service alert seen.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Iterator

from .sqlite_store import SqliteStore
from .util import TTLCache

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS stop_observations (
    service_date  TEXT    NOT NULL,   -- YYYY-MM-DD
    trip_id       TEXT    NOT NULL,
    stop_id       TEXT    NOT NULL,
    route_id      TEXT,
    headsign      TEXT,
    stop_sequence INTEGER,
    scheduled     INTEGER,            -- epoch seconds
    predicted     INTEGER,            -- latest realtime prediction, epoch seconds
    delay         INTEGER,            -- latest delay, seconds
    max_delay     INTEGER,
    cancelled     INTEGER NOT NULL DEFAULT 0,
    delay_reason  TEXT,
    first_seen    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL,
    PRIMARY KEY (service_date, trip_id, stop_id)
);
CREATE INDEX IF NOT EXISTS idx_obs_stop_sched ON stop_observations (stop_id, scheduled);
CREATE INDEX IF NOT EXISTS idx_obs_sched ON stop_observations (scheduled);

CREATE TABLE IF NOT EXISTS alerts_log (
    alert_id    TEXT NOT NULL,
    header      TEXT NOT NULL,
    description TEXT,
    url         TEXT,
    cause       TEXT,
    effect      TEXT,
    routes      TEXT,                 -- JSON array of route_ids
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    PRIMARY KEY (alert_id, header)
);
CREATE INDEX IF NOT EXISTS idx_alerts_seen ON alerts_log (last_seen);
"""

UPSERT_OBS = """
INSERT INTO stop_observations
    (service_date, trip_id, stop_id, route_id, headsign, stop_sequence, scheduled,
     predicted, delay, max_delay, cancelled, delay_reason, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (service_date, trip_id, stop_id) DO UPDATE SET
    predicted    = excluded.predicted,
    delay        = excluded.delay,
    max_delay    = MAX(COALESCE(stop_observations.max_delay, excluded.max_delay), COALESCE(excluded.max_delay, stop_observations.max_delay)),
    cancelled    = excluded.cancelled,
    delay_reason = COALESCE(excluded.delay_reason, stop_observations.delay_reason),
    last_seen    = excluded.last_seen
"""

UPSERT_ALERT = """
INSERT INTO alerts_log (alert_id, header, description, url, cause, effect, routes, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (alert_id, header) DO UPDATE SET
    description = excluded.description,
    url         = excluded.url,
    cause       = excluded.cause,
    effect      = excluded.effect,
    routes      = excluded.routes,
    last_seen   = excluded.last_seen
"""

# Metra counts a train as on time if it is less than 6 minutes late.
ON_TIME_SECONDS = 6 * 60


def day_type(service_date: str | None) -> str:
    """'weekday', 'saturday' or 'sunday' for a YYYY-MM-DD service date."""
    try:
        wd = date.fromisoformat(service_date or "").weekday()
    except ValueError:
        return "weekday"
    return "saturday" if wd == 5 else "sunday" if wd == 6 else "weekday"


def _summarize(g: dict) -> dict:
    ran = g["n"] - g["cancelled"]
    return {
        "observations": g["n"],
        "on_time_pct": round(100 * g["on_time"] / ran, 1) if ran else None,
        "avg_delay_min": round(g["delay_sum"] / ran / 60, 1) if ran else None,
        "worst_delay_min": round(g["worst"] / 60, 1),
        "cancelled": g["cancelled"],
    }


class History(SqliteStore):
    SCHEMA_VERSION = 1
    LABEL = "History"

    def __init__(self, path: Path, retention_days: int):
        super().__init__(path, retention_days)
        self._stats_cache = TTLCache(300)   # COUNT(*) scans the table, which grows all year
        self._dep_cache = TTLCache(900)     # board stats per station

    def _migrate(self, db: sqlite3.Connection, version: int) -> None:
        db.executescript(SCHEMA)

    # ---- writes ---------------------------------------------------------
    def record_observations(self, rows: list[tuple]) -> None:
        """rows: (service_date, trip_id, stop_id, route_id, headsign, seq,
        scheduled, predicted, delay, cancelled, delay_reason)."""
        if not self._db or not rows:
            return
        now = int(time.time())
        params = [
            (d, t, st, r, h, seq, sched, pred, delay, delay, int(c), reason, now, now)
            for (d, t, st, r, h, seq, sched, pred, delay, c, reason) in rows
        ]
        self._write_many(UPSERT_OBS, params)

    def record_alerts(self, alerts) -> None:
        if not self._db or not alerts:
            return
        now = int(time.time())
        params = [
            (a.id, a.header or a.description[:200], a.description, a.url, a.cause, a.effect,
             json.dumps(a.route_ids), now, now)
            for a in alerts
        ]
        with self._lock:
            self._db.executemany(UPSERT_ALERT, params)

    def prune(self) -> int:
        if not self._db:
            return 0
        cutoff = int(time.time()) - self.retention_days * 86400
        with self._lock:
            n = self._db.execute("DELETE FROM stop_observations WHERE scheduled < ?", (cutoff,)).rowcount
            n += self._db.execute("DELETE FROM alerts_log WHERE last_seen < ?", (cutoff,)).rowcount
        if n:
            log.info("Pruned %d history rows older than %d days", n, self.retention_days)
        return n

    # ---- reads ----------------------------------------------------------
    def _grouped_stats(self, stop_ids: list[str], days: int, hhmm_fn) -> dict[tuple, dict]:
        """On-time stats per (route, scheduled HH:MM, destination, day type).

        Day type ("weekday", "saturday", "sunday") comes from the service date,
        because the same clock time is often a different train on weekends.
        """
        since = int(time.time()) - days * 86400
        marks = ",".join("?" * len(stop_ids))
        rows = self._query(
            f"""SELECT service_date, route_id, headsign, scheduled, delay, cancelled
                FROM stop_observations WHERE stop_id IN ({marks}) AND scheduled >= ?""",
            (*stop_ids, since),
        )
        groups: dict[tuple, dict] = {}
        for r in rows:
            key = (r["route_id"], hhmm_fn(r["scheduled"]), r["headsign"], day_type(r["service_date"]))
            g = groups.setdefault(key, {"n": 0, "on_time": 0, "cancelled": 0, "delay_sum": 0, "worst": 0})
            g["n"] += 1
            if r["cancelled"]:
                g["cancelled"] += 1
                continue
            d = max(r["delay"] or 0, 0)
            g["delay_sum"] += d
            g["worst"] = max(g["worst"], d)
            if d < ON_TIME_SECONDS:
                g["on_time"] += 1
        return {key: _summarize(g) for key, g in groups.items()}

    def station_reliability(self, stop_ids: list[str], days: int, hhmm_fn) -> list[dict]:
        """On-time stats for every scheduled departure seen at a station."""
        out = [
            {"route_id": route, "scheduled_time": hhmm, "destination": headsign, "day_type": dt, **stats}
            for (route, hhmm, headsign, dt), stats in self._grouped_stats(stop_ids, days, hhmm_fn).items()
        ]
        out.sort(key=lambda x: (x["day_type"], x["scheduled_time"], x["route_id"] or ""))
        return out

    def departure_stats(self, stop_ids: list[str], days: int, hhmm_fn) -> dict[tuple, dict]:
        """Like station_reliability, keyed for lookup from the departure board. Cached."""
        key = (tuple(sorted(stop_ids)), days)
        cached = self._dep_cache.get(key)
        if cached is None:
            cached = self._dep_cache.put(key, self._grouped_stats(stop_ids, days, hhmm_fn))
        return cached

    def alerts(self, days: int) -> list[dict]:
        since = int(time.time()) - days * 86400
        rows = self._query(
            "SELECT * FROM alerts_log WHERE last_seen >= ? ORDER BY first_seen DESC", (since,)
        )
        for r in rows:
            r["routes"] = json.loads(r["routes"] or "[]")
        return rows

    def export_observations(self, since: float, until: float, limit: int) -> tuple[dict, Iterator[dict]]:
        """Observations scheduled in [since, until), oldest first, at most about
        `limit` rows (the newest ones if there are more). See SqliteStore._export."""
        return self._export("stop_observations", "scheduled", "scheduled >= ? AND scheduled < ?",
                            (int(since), int(until)), limit)

    def stats(self) -> dict:
        """Row counts and size. Cached for a few minutes."""
        if not self._db:
            return {"enabled": False}
        cached = self._stats_cache.get("stats")
        if cached is not None:
            return cached
        with self._lock:
            n = self._db.execute("SELECT COUNT(*), MIN(scheduled) FROM stop_observations").fetchone()
            a = self._db.execute("SELECT COUNT(*) FROM alerts_log").fetchone()
        return self._stats_cache.put("stats", {
            "enabled": True,
            "retention_days": self.retention_days,
            "observations": n[0],
            "oldest": n[1],
            "alerts": a[0],
            "size_bytes": self.size_bytes(),
        })
