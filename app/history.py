"""Long-term history of realtime observations, stored in SQLite under /config.

Records, per service day, the latest realtime prediction for every
trip/stop pair (so the final value before a train leaves approximates what
actually happened), cancellations, and every service alert seen.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

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
    from datetime import date
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


class History:
    def __init__(self, path: Path, retention_days: int):
        self.path = path
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._stats_cache: tuple[float, dict] | None = None
        self._dep_cache: dict[tuple, tuple[float, dict]] = {}

    @property
    def enabled(self) -> bool:
        return self.retention_days > 0

    @property
    def available(self) -> bool:
        """Enabled and the database is open (it only opens when realtime is on)."""
        return self._db is not None

    def open(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript(SCHEMA)
        db.row_factory = sqlite3.Row
        self._db = db
        log.info("History database at %s (retention %d days)", self.path, self.retention_days)

    def close(self) -> None:
        with self._lock:
            if self._db:
                self._db.close()
                self._db = None

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
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(UPSERT_OBS, params)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

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
    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        if not self._db:
            return []
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

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

    def departure_stats(self, stop_ids: list[str], days: int, hhmm_fn, max_age: float = 900) -> dict[tuple, dict]:
        """Like station_reliability, keyed for lookup from the departure board. Cached."""
        key = (tuple(sorted(stop_ids)), days)
        cached = self._dep_cache.get(key)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        result = self._grouped_stats(stop_ids, days, hhmm_fn)
        self._dep_cache[key] = (time.monotonic(), result)
        return result

    def alerts(self, days: int) -> list[dict]:
        since = int(time.time()) - days * 86400
        rows = self._query(
            "SELECT * FROM alerts_log WHERE last_seen >= ? ORDER BY first_seen DESC", (since,)
        )
        for r in rows:
            r["routes"] = json.loads(r["routes"] or "[]")
        return rows

    def iter_observations(self, days: int, batch: int = 5000):
        """Stream observations in batches on a separate read-only connection.

        WAL mode lets this read run alongside the poller's writes, so a large
        export neither loads everything into memory nor blocks recording.
        """
        if not self._db:
            return
        since = int(time.time()) - days * 86400
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            cur = db.execute(
                "SELECT * FROM stop_observations WHERE scheduled >= ? ORDER BY scheduled", (since,)
            )
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                for r in rows:
                    yield dict(r)
        finally:
            db.close()

    def stats(self, max_age: float = 300) -> dict:
        """Row counts and size. Cached: COUNT(*) scans the table, which grows all year."""
        if not self._db:
            return {"enabled": False}
        cached = self._stats_cache
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        with self._lock:
            n = self._db.execute("SELECT COUNT(*), MIN(scheduled) FROM stop_observations").fetchone()
            a = self._db.execute("SELECT COUNT(*) FROM alerts_log").fetchone()
        size = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*") if p.exists())
        result = {
            "enabled": True,
            "retention_days": self.retention_days,
            "observations": n[0],
            "oldest": n[1],
            "alerts": a[0],
            "size_bytes": size,
        }
        self._stats_cache = (time.monotonic(), result)
        return result
