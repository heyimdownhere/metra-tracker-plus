"""What the history databases have in common: one SQLite file under /config,
opened once, with a schema version, a write lock, and exports that are read
in batches and capped in size.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)


class SqliteStore:
    # Bump in a subclass when its tables change, and handle the old version in _migrate.
    SCHEMA_VERSION = 1
    LABEL = "History"

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
        """Enabled and the database is open."""
        return self._db is not None

    def open(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > self.SCHEMA_VERSION:
            db.close()
            raise RuntimeError(f"{self.path.name} was written by a newer version of the app "
                               f"(schema {version}, this version knows {self.SCHEMA_VERSION})")
        self._migrate(db, version)
        db.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        db.row_factory = sqlite3.Row
        self._db = db
        log.info("%s database at %s (retention %d days)", self.LABEL, self.path, self.retention_days)

    def _migrate(self, db: sqlite3.Connection, version: int) -> None:
        """Create the tables, or bring an older database up to SCHEMA_VERSION."""
        raise NotImplementedError

    def close(self) -> None:
        with self._lock:
            if self._db:
                self._db.close()
                self._db = None

    # ---- helpers for subclasses -------------------------------------------------------
    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        if not self._db:
            return []
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def _write_many(self, sql: str, rows: list[tuple]) -> None:
        """All rows or none."""
        if not self._db or not rows:
            return
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(sql, rows)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _read_only(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db

    def _export(self, table: str, time_col: str, where: str, params: tuple, limit: int,
                select: str = "*") -> tuple[dict, Iterator[dict]]:
        """Rows of `table` matching `where`, oldest first, at most about `limit`.

        If there are more, only the newest `limit` are returned (rows sharing
        the cut-off second are all kept). Returns ({total, rows, truncated,
        limit}, iterator). Reads happen on a separate read-only connection, in
        batches: a big export neither fills memory nor blocks recording.
        """
        info = {"total": 0, "rows": 0, "truncated": False, "limit": limit}
        if not self._db:
            return info, iter(())
        db = self._read_only()
        try:
            total = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
            info["total"] = info["rows"] = total
            if total > limit:
                cut = db.execute(
                    f"SELECT {time_col} FROM {table} WHERE {where} ORDER BY {time_col} DESC LIMIT 1 OFFSET ?",
                    (*params, limit - 1)).fetchone()[0]
                where, params = f"({where}) AND {time_col} >= ?", (*params, cut)
                info["rows"] = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
                info["truncated"] = True
        except Exception:
            db.close()
            raise

        def rows(batch: int = 5000) -> Iterator[dict]:
            try:
                cur = db.execute(f"SELECT {select} FROM {table} WHERE {where} ORDER BY {time_col}", params)
                while True:
                    chunk = cur.fetchmany(batch)
                    if not chunk:
                        break
                    for r in chunk:
                        yield dict(r)
            finally:
                db.close()

        return info, rows()

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*") if p.exists())
