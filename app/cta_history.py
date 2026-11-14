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
import time
from pathlib import Path
from typing import Iterator

from .cta_schedule import local_hour, service_date, service_day_span
from .history import day_type
from .sqlite_store import SqliteStore

log = logging.getLogger(__name__)

LONG_WAIT_FACTOR = 2
LONG_WAIT_EXTRA = 5 * 60   # seconds
MIN_DAYS = 5               # don't show stats before this many days of data
MAX_GAP = 2 * 3600         # a longer gap is a break in service or in our data, not a wait

# About 47,000 departures are stored a day, so the layout is kept small: the
# line/direction is a number (see `groups`), the service date isn't stored
# (it follows from `t`), and the table is its own index (WITHOUT ROWID).
# Roughly 50 bytes a row instead of 170.
SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id   INTEGER PRIMARY KEY,
    gkey TEXT NOT NULL UNIQUE          -- line + direction/destination, e.g. G:5
);
CREATE TABLE IF NOT EXISTS arrivals (
    station   TEXT    NOT NULL,        -- CTA station id (4xxxx)
    grp       INTEGER NOT NULL,        -- groups.id
    t         INTEGER NOT NULL,        -- when it left, epoch seconds
    run       TEXT    NOT NULL,
    gap       INTEGER,                 -- seconds since the previous train in this group (NULL if unknown)
    sched_gap INTEGER,                 -- scheduled gap at that time, seconds
    delayed   INTEGER NOT NULL DEFAULT 0,  -- flagged delayed on the way in
    tracked   INTEGER NOT NULL DEFAULT 1,  -- 0: filled in between two positions
    PRIMARY KEY (station, grp, t, run)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_cta_arr_time ON arrivals (t);

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

ARRIVALS = "arrivals a JOIN groups g ON g.id = a.grp"
EXPORT_COLUMNS = ["service_date", "station", "gkey", "run", "t", "gap", "sched_gap", "delayed", "tracked"]


def is_long_wait(gap: int | None, sched_gap: int | None) -> bool:
    if not gap or not sched_gap:
        return False
    return gap > max(LONG_WAIT_FACTOR * sched_gap, sched_gap + LONG_WAIT_EXTRA)


class CtaHistory(SqliteStore):
    SCHEMA_VERSION = 2
    LABEL = "CTA history"

    def __init__(self, path: Path, retention_days: int):
        super().__init__(path, retention_days)
        self._group_ids: dict[str, int] = {}

    def _migrate(self, db: sqlite3.Connection, version: int) -> None:
        columns = {r[1] for r in db.execute("PRAGMA table_info(arrivals)")}
        if "service_date" not in columns:
            db.executescript(SCHEMA)
            return
        # Version 1 stored the service date and the group's text in every row,
        # with three extra indexes. Copy the rows into the small layout.
        n = db.execute("SELECT COUNT(*) FROM arrivals").fetchone()[0]
        log.info("CTA history: moving %d rows to the smaller layout (once)", n)
        db.execute("BEGIN")
        try:
            db.execute("ALTER TABLE arrivals RENAME TO arrivals_v1")
            for index in ("idx_cta_arr_station", "idx_cta_arr_run", "idx_cta_arr_t"):
                db.execute(f"DROP INDEX IF EXISTS {index}")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT OR IGNORE INTO groups (gkey) SELECT DISTINCT gkey FROM arrivals_v1")
            db.execute("INSERT OR IGNORE INTO arrivals (station, grp, t, run, gap, sched_gap, delayed, tracked) "
                       "SELECT v.station, g.id, v.t, v.run, v.gap, v.sched_gap, v.delayed, v.tracked "
                       "FROM arrivals_v1 v JOIN groups g ON g.gkey = v.gkey")
            db.execute("DROP TABLE arrivals_v1")
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        db.execute("VACUUM")  # give the freed space back to the disk

    def _group_id(self, gkey: str) -> int:
        """The number stored for a line/direction (made on first use). Call with the lock held."""
        gid = self._group_ids.get(gkey)
        if gid is None:
            self._db.execute("INSERT OR IGNORE INTO groups (gkey) VALUES (?)", (gkey,))
            gid = self._db.execute("SELECT id FROM groups WHERE gkey = ?", (gkey,)).fetchone()[0]
            self._group_ids[gkey] = gid
        return gid

    # ---- writes -------------------------------------------------------------------------
    def record_arrivals(self, rows: list[tuple]) -> None:
        """rows: (service_date, station, gkey, run, t, gap, sched_gap, delayed, tracked).
        The service date isn't stored: it follows from t."""
        if not self._db or not rows:
            return
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(
                    "INSERT OR IGNORE INTO arrivals (station, grp, t, run, gap, sched_gap, delayed, tracked) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(station, self._group_id(gkey), t, run, gap, sched_gap, delayed, tracked)
                     for (_, station, gkey, run, t, gap, sched_gap, delayed, tracked) in rows])
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                self._group_ids.clear()
                raise

    def record_delays(self, rows: list[tuple]) -> None:
        """rows: (service_date, route, gkey, run, station, station_name, started, ended).
        The same delay is written again as it goes on; `ended` moves forward."""
        self._write_many(
            "INSERT INTO delays VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (service_date, run, started) DO UPDATE SET ended = MAX(delays.ended, excluded.ended)",
            rows)

    def last_arrivals(self, since: float) -> list[dict]:
        """Latest departure per (station, group) since a time: lets gaps continue across a restart."""
        return self._query(
            f"SELECT a.station, g.gkey, MAX(a.t) AS t FROM {ARRIVALS} WHERE a.t >= ? GROUP BY a.station, a.grp",
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
    def _station_rows(self, station: str, since: float) -> list[dict]:
        rows = self._query(
            f"SELECT g.gkey, a.t, a.gap, a.sched_gap, a.delayed FROM {ARRIVALS} WHERE a.station = ? AND a.t >= ?",
            (station, int(since)))
        for r in rows:
            r["service_date"] = service_date(r["t"])
        return rows

    def board_stats(self, station: str, hour: int, kind: str, days: int = 30,
                    now: float | None = None, hour_fn=local_hour) -> dict[str, dict]:
        """For every line/direction at a station: how regular it has been around
        this hour (±1 h) on the same kind of day, over the last `days` days.
        One query for the whole station. Groups with under MIN_DAYS days of
        data are left out."""
        now = time.time() if now is None else now
        groups: dict[str, list] = {}
        for r in self._station_rows(station, now - days * 86400):
            if day_type(r["service_date"]) == kind and abs(hour_fn(r["t"]) - hour) <= 1:
                groups.setdefault(r["gkey"], []).append(r)
        out = {}
        for gkey, rows in groups.items():
            s = summarize(rows)
            if s:
                out[gkey] = s
        return out

    def group_stats(self, station: str, gkey: str, hour: int, kind: str, days: int = 30,
                    now: float | None = None, hour_fn=local_hour) -> dict | None:
        """board_stats for one line/direction. None until MIN_DAYS days of data."""
        return self.board_stats(station, hour, kind, days, now, hour_fn).get(gkey)

    def station_stats(self, station: str, days: int, hour_fn=local_hour) -> list[dict]:
        """Per group, day type and hour: regularity at a station."""
        groups: dict[tuple, list] = {}
        for r in self._station_rows(station, time.time() - days * 86400):
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

    def run_delays(self, day: str, run: str) -> list[dict]:
        """One train's delays on a service day."""
        return self._query("SELECT * FROM delays WHERE service_date = ? AND run = ? ORDER BY started", (day, run))

    def delay_summary(self, days: int) -> list[dict]:
        rows = self._query(
            "SELECT route, COUNT(*) AS delays, SUM(ended - started) AS total, MAX(ended - started) AS longest, "
            "COUNT(DISTINCT service_date) AS days FROM delays WHERE started >= ? GROUP BY route ORDER BY delays DESC",
            (int(time.time() - days * 86400),))
        return [{"route_id": r["route"], "delays": r["delays"], "days_with_delays": r["days"],
                 "avg_minutes": round(r["total"] / r["delays"] / 60, 1) if r["delays"] else None,
                 "longest_minutes": round((r["longest"] or 0) / 60, 1)} for r in rows]

    def run_today(self, day: str, run: str) -> list[dict]:
        """Where one train has been on a service day (stations it left, in order)."""
        start, end = service_day_span(day)
        return self._query(
            f"SELECT a.station, g.gkey, a.t, a.tracked FROM {ARRIVALS} "
            "WHERE a.t >= ? AND a.t < ? AND a.run = ? ORDER BY a.t",
            (int(start), int(end), run))

    def export_arrivals(self, since: float, until: float, limit: int,
                        station: str | None = None) -> tuple[dict, Iterator[dict]]:
        """Departures in [since, until), oldest first, at most about `limit` rows
        (the newest ones if there are more), optionally for one station."""
        where = "a.t >= ? AND a.t < ?"
        params: tuple = (int(since), int(until))
        if station:
            where, params = "a.station = ? AND " + where, (station, *params)
        info, rows = self._export(ARRIVALS, "a.t", where, params, limit,
                                  select="a.station, g.gkey, a.run, a.t, a.gap, a.sched_gap, a.delayed, a.tracked")

        def with_date() -> Iterator[dict]:
            for r in rows:
                r["service_date"] = service_date(r["t"])
                yield r

        return info, with_date()


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
