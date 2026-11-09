"""Small server-side settings shared by every device (e.g. the home station)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DEFAULTS = {"home_station": None, "home_dest": None}


class UserSettings:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}

    def update(self, changes: dict) -> dict:
        with self._lock:
            data = self.load()
            data.update({k: v for k, v in changes.items() if k in DEFAULTS})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self.path)
            return data
