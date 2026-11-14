"""Small server-side settings shared by every device (e.g. the home station)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from .util import atomic_write

# None means "not set here": fall back to the environment (HOME_STATION etc.).
DEFAULTS = {
    "home_station": None,
    "home_dest": None,
    "work_station": None,
    "switch_time": None,  # "HH:MM": before it the app shows home -> work, after it work -> home
    "work_days": None,    # list of weekdays, Monday = 0
}


class UserSettings:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        # The file as last read, with its modification time and size: boards ask
        # for the settings on every refresh, and the file rarely changes.
        self._seen: tuple[tuple[int, int] | None, dict] | None = None

    def _stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def load(self) -> dict:
        stamp = self._stamp()
        seen = self._seen
        if seen is None or seen[0] != stamp:
            try:
                data = json.loads(self.path.read_text()) if stamp else {}
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            seen = self._seen = (stamp, {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}})
        return dict(seen[1])

    def update(self, changes: dict, check: Callable[[dict], None] | None = None) -> dict:
        """Save changes. `check` is given the settings as they would be saved and
        may raise to refuse them: nothing is written then. One update at a time."""
        with self._lock:
            data = self.load()
            data.update({k: v for k, v in changes.items() if k in DEFAULTS})
            if check is not None:
                check(data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, json.dumps(data, indent=2).encode())
            self._seen = None
            return data
