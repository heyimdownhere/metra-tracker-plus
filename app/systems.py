"""The transit systems the app serves.

Each system (Metra, CTA, ...) is a tracker object plus a router with its API
under /api/<id>/. The app only needs the few things below from a tracker, so
adding a system means: write its tracker and router, and register it in
app.main.create_app. Nothing else in the app has to know about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import APIRouter


class Tracker(Protocol):
    """What the app needs from any system's tracker."""

    @property
    def loaded(self) -> bool:
        """The schedule is in memory: the system can answer questions."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def status(self) -> dict: ...


@dataclass
class System:
    id: str            # "metra", "cta": the part of the address after /api/
    name: str
    tracker: Tracker
    router: APIRouter  # routes without a prefix; mounted at /api/<id>
    enabled: bool = True
    live: bool = False  # has a key/token for live data

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "enabled": self.enabled,
                "loaded": bool(self.enabled and self.tracker.loaded), "live": self.live,
                "api": f"/api/{self.id}"}
