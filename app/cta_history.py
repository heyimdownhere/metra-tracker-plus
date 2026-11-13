"""CTA 'L' history, in its own SQLite file (/config/cta-history.db).

CTA doesn't publish how late an L train is (no "5 min late"), and L trains
aren't tied to timetable trips. So "on time" for the L means trains came as
often as scheduled: every time a train leaves a station we store the actual
gap since the previous train in the same group, next to the scheduled gap at
that time. A gap more than twice the scheduled one, and at least
LONG_WAIT_EXTRA longer, counts as a long wait (close to CTA's own "big gap"
idea). Separately, each time Train Tracker flags a train as delayed (it has
stopped moving) we log a delay with where it happened and for how long.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from .history import day_type

log = logging.getLogger(__name__)

LONG_WAIT_FACTOR = 2
LONG_WAIT_EXTRA = 5 * 60   # seconds
MIN_DAYS = 5               # don't show stats before this many days of data
MAX_GAP = 2 * 3600         # a longer gap is a break in service or in our data, not a wait

SCHEMA = """
CREATE TABLE IF NOT EXISTS arrivals (
    service_date TEXT    NOT NULL,   -- YYYY-MM-DD (service day starts 3 AM)
    station      TEXT    NOT NULL,   -- CTA station id (4xxxx)
    gkey         TEXT    NOT NULL,   -- line + direction/destination, e.g. G:5
    run          TEXT    NOT NULL,
    t            INTEGER NOT NULL,   -- when it left, epoch seconds
    gap          INTEGER,            -- seconds since the previous train in this group (NULL if unknown)
    sched_gap    INTEGER,            -- scheduled gap at that time, seconds
    delayed      INTEGER NOT NULL DEFAULT 0,  -- flagged delayed on the way in
    tracked      INTEGER NOT NULL DEFAULT 1,  -- 0: filled in between two positions
    PRIMARY KEY (service_date, station, gkey, run, t)
);
CREATE INDEX IF NOT EXISTS idx_cta_arr_station ON arrivals (station, gkey, t);
CREATE INDEX IF NOT EXISTS idx_cta_arr_run ON arrivals (service_date, run);
CREATE INDEX IF NOT EXISTS idx_cta_arr_t ON arrivals (t);

CREATE TABLE IF NOT EXISTS delays (
    service_date TEXT    NOT NULL,
    route        TEXT    NOT NULL,
    gkey         TEXT    NOT NULL,
    run          TEXT    NOT NULL,
    station      TEXT,               -- next station when it was flagged
    station_name TEXT,
    started      INTEGER NOT NULL,
    ended        INTEGER NOT NULL,
    PRIMARY KEY (service_date, run, started)
);
CREATE INDEX IF NOT EXISTS idx_cta_delays_started ON delays (started);
"""


def is_long_wait(gap: int | None, sched_gap: int | None) -> bool:
    if not gap or not sched_gap:
        return False
    return gap > max(LONG_WAIT_FACTOR * sched_gap, sched_gap + LONG_WAIT_EXTRA)


class CtaHistory:
    def __init__(self, path: Path, retention_days: int):
        self.path = path
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None

    @property
    def enabled(self) -> bool:
        return self.retention_days > 0

    @property
    def available(self) -> bool:
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
        log.info("CTA history database at %s (retention %d days)", self.path, self.retention_days)

    def close(self) -> None:
        with self._lock:
            if self._db:
                self._db.close()
                self._db = None

    # ---- writes -------------------------------------------------------------------------
    def record_arrivals(self, rows: list[tuple]) -> None:
        """rows: (service_date, station, gkey, run, t, gap, sched_gap, delayed, tracked)."""
        if not self._db or not rows:
            return
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany("INSERT OR IGNORE INTO arrivals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def record_delays(self, rows: list[tuple]) -> None:
        """rows: (service_date, route, gkey, run, station, station_name, started, ended).
        The same delay is written again as it goes on; `ended` moves forward."""
        if not self._db or not rows:
            return
        with self._lock:
            self._db.executemany(
                "INSERT INTO delays VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (service_date, run, started) DO UPDATE SET ended = MAX(delays.ended, excluded.ended)",
                rows)

    def last_arrivals(self, since: float) -> list[dict]:
        """Latest departure per (station, group) since a time: lets gaps continue across a restart."""
        return self._query(
            "SELECT station, gkey, service_date, MAX(t) AS t FROM arrivals WHERE t >= ? GROUP BY station, gkey",
            (int(since),))

    def prune(self) -> int:
        if not self._db:
            return 0
        cutoff = int(time.time()) - self.retention_days * 86400
        with self._lock:
            n = self._db.execute("DELETE FROM arrivals WHERE t < ?", (cutoff,)).rowcount
            n += self._db.execute("DELETE FROM delays WHERE started < ?", (cutoff,)).rowcount
        if n:
            log.info("Pruned %d CTA history rows older than %d days", n, self.retention_days)
        return n

    # ---- reads --------------------------------------------------------------------------
    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        if not self._db:
            return []
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def group_stats(self, station: str, gkey: str, hour: int, kind: str, days: int = 30,
                    now: float | None = None, hour_fn=None) -> dict | None:
        """How regular this line/direction has been at this station around this hour
        (±1 h) on the same kind of day, over the last `days` days. None until
        MIN_DAYS days of data."""
        now = time.time() if now is None else now
        rows = self._query(
            "SELECT service_date, t, gap, sched_gap, delayed FROM arrivals "
            "WHERE station = ? AND gkey = ? AND t >= ?",
            (station, gkey, int(now - days * 86400)))
        rows = [r for r in rows if day_type(r["service_date"]) == kind and abs(hour_fn(r["t"]) - hour) <= 1]
        return summarize(rows)

    def station_stats(self, station: str, days: int, hour_fn) -> list[dict]:
        """Per group, day type and hour: regularity at a station."""
        rows = self._query(
            "SELECT service_date, gkey, t, gap, sched_gap, delayed FROM arrivals "
            "WHERE station = ? AND t >= ?",
            (station, int(time.time() - days * 86400)))
        groups: dict[tuple, list] = {}
        for r in rows:
            groups.setdefault((r["gkey"], day_type(r["service_date"]), hour_fn(r["t"])), []).append(r)
        out = []
        for (gkey, kind, hour), rs in sorted(groups.items()):
            s = summarize(rs, min_days=1)
            if s:
                out.append({"key": gkey, "day_type": kind, "hour": hour, **s})
        return out

    def delay_counts(self, since: float) -> dict[str, int]:
        """Delays per group since a time."""
        return {r["gkey"]: r["n"] for r in self._query(
            "SELECT gkey, COUNT(*) AS n FROM delays WHERE started >= ? GROUP BY gkey", (int(since),))}

    def delays(self, days: int, route: str | None = None) -> list[dict]:
        sql = "SELECT * FROM delays WHERE started >= ?"
        params: tuple = (int(time.time() - days * 86400),)
        if route:
            sql += " AND route = ?"
            params += (route,)
        return self._query(sql + " ORDER BY started DESC LIMIT 500", params)

    def delay_summary(self, days: int) -> list[dict]:
        rows = self._query(
            "SELECT route, COUNT(*) AS delays, SUM(ended - started) AS total, MAX(ended - started) AS longest, "
            "COUNT(DISTINCT service_date) AS days FROM delays WHERE started >= ? GROUP BY route ORDER BY delays DESC",
            (int(time.time() - days * 86400),))
        return [{"route_id": r["route"], "delays": r["delays"], "days_with_delays": r["days"],
                 "avg_minutes": round(r["total"] / r["delays"] / 60, 1) if r["delays"] else None,
                 "longest_minutes": round((r["longest"] or 0) / 60, 1)} for r in rows]

    def run_today(self, service_date: str, run: str) -> list[dict]:
        """Where one train has been today (stations it left, in order)."""
        return self._query(
            "SELECT station, gkey, t, tracked FROM arrivals WHERE service_date = ? AND run = ? ORDER BY t",
            (service_date, run))

    def iter_arrivals(self, days: int):
        if not self._db:
            return
        with self._lock:
            rows = self._db.execute("SELECT * FROM arrivals WHERE t >= ? ORDER BY t",
                                    (int(time.time() - days * 86400),)).fetchall()
        for r in rows:
            yield dict(r)


def summarize(rows: list[dict], min_days: int = MIN_DAYS) -> dict | None:
    days = {r["service_date"] for r in rows}
    gaps = [r for r in rows if r["gap"] and r["sched_gap"]]
    if len(days) < min_days or not gaps:
        return None
    long = [r for r in gaps if is_long_wait(r["gap"], r["sched_gap"])]
    return {
        "days": len(days),
        "trains": len(rows),
        "on_time_pct": round(100 * (1 - len(long) / len(gaps))),
        "avg_gap_min": round(sum(r["gap"] for r in gaps) / len(gaps) / 60, 1),
        "sched_gap_min": round(sum(r["sched_gap"] for r in gaps) / len(gaps) / 60, 1),
        "long_waits": len(long),
        "longest_wait_min": round(max(r["gap"] for r in gaps) / 60),
        "delayed_trains": sum(1 for r in rows if r["delayed"]),
    }
