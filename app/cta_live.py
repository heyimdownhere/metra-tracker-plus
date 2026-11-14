"""CTA Train Tracker client: the only code that calls CTA's live API.

Every answer is reused for at least 30 s, only one request per question runs
at a time, a failure is remembered for 30 s (so a slow or broken CTA server
doesn't hold every board up), and a daily request budget keeps the key well
under CTA's limit of 100,000 requests a day. Part of that budget is kept back
for the history recorder, so heavy use of the app can't stop the recording.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterator

import httpx

from .cta_schedule import TZ, route_id
from .util import SingleFlight, atomic_write, install_redaction

log = logging.getLogger(__name__)

API_BASE = "https://lapi.transitchicago.com/api/1.0"
ALERTS_URL = "https://www.transitchicago.com/api/1.0/alerts.aspx"
POSITION_ROUTES = "red,blue,brn,g,org,p,pink,y"

ARRIVALS_TTL = 30      # seconds a station's arrivals are reused
FOLLOW_TTL = 30        # seconds one train's remaining stops are reused
ALERTS_TTL = 120       # seconds the alerts list is reused
FAIL_TTL = 30          # seconds a failed request is not tried again
# Train Tracker normally answers in well under a second. Giving up after a few
# seconds lets a board fall back to the schedule instead of hanging.
LIVE_TIMEOUT = httpx.Timeout(8.0, connect=5.0)

DAILY_BUDGET = 80_000      # Train Tracker requests per day (CTA's limit: 100,000)
RECORDER_RESERVE = 4_000   # of those, kept for the history recorder (it needs about 2,900)
SAVE_EVERY = 50            # write the day's count to disk every this many requests

# The key travels in the address, and HTTP libraries log addresses.
install_redaction()


class TrainTrackerError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class BudgetUsedUp(TrainTrackerError):
    """The day's requests are spent. Not remembered as a failure: the recorder's
    share may still be available to the next caller."""


def as_list(value) -> list:
    """CTA sends a single item as an object instead of a one-item list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def iter_positions(body: dict) -> Iterator[tuple[str, dict]]:
    """(line id, train) for every train in a positions answer."""
    for route in as_list(body.get("route")):
        rid = route_id(route.get("@name"))
        if not rid:
            continue
        for train in as_list(route.get("train")):
            yield rid, train


class TrainTracker:
    def __init__(self, api_key: str | None, get_client: Callable[[], httpx.AsyncClient | None],
                 budget_path: Path | None = None):
        self.api_key = api_key
        self._get_client = get_client
        self.budget_path = budget_path
        self.cache = SingleFlight(fail_ttl=FAIL_TTL, failures=(TrainTrackerError, httpx.HTTPError, ValueError),
                                  forget=(BudgetUsedUp,))
        self._budget_day: date | None = None
        self.requests_today = 0
        self.last_live: float | None = None
        self.error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ---- daily budget ----------------------------------------------------------------
    def load_budget(self) -> None:
        """Carry today's count over a restart, so restarts can't add up to more
        than the daily budget."""
        if not self.budget_path:
            return
        try:
            saved = json.loads(self.budget_path.read_text())
            if saved.get("day") == datetime.now(TZ).date().isoformat():
                self._budget_day = datetime.now(TZ).date()
                # The file can be up to SAVE_EVERY requests behind.
                self.requests_today = int(saved.get("requests", 0)) + SAVE_EVERY
        except (OSError, ValueError, TypeError):
            pass

    def save_budget(self) -> None:
        if not self.budget_path or self._budget_day is None:
            return
        try:
            self.budget_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.budget_path, json.dumps(
                {"day": self._budget_day.isoformat(), "requests": self.requests_today}).encode())
        except OSError as exc:
            log.debug("Could not save the CTA request count: %s", exc)

    def _spend(self, recorder: bool = False) -> bool:
        today = datetime.now(TZ).date()
        if self._budget_day != today:
            self._budget_day, self.requests_today = today, 0
        if self.requests_today >= (DAILY_BUDGET if recorder else DAILY_BUDGET - RECORDER_RESERVE):
            return False
        self.requests_today += 1
        return True

    # ---- requests --------------------------------------------------------------------
    async def call(self, endpoint: str, params: dict, recorder: bool = False) -> dict:
        """One Train Tracker request. Raises TrainTrackerError (never shows the key)."""
        client = self._get_client()
        if not self.api_key or client is None:
            raise TrainTrackerError("no key")
        if not self._spend(recorder):
            raise BudgetUsedUp("daily request budget used up")
        if self.requests_today % SAVE_EVERY == 0:
            await asyncio.to_thread(self.save_budget)
        try:
            r = await client.get(f"{API_BASE}/{endpoint}", timeout=LIVE_TIMEOUT,
                                 params={**params, "outputType": "JSON", "key": self.api_key})
            r.raise_for_status()
            data = r.json()
            body = data.get("ctatt") if isinstance(data, dict) else None
            if not isinstance(body, dict):
                raise ValueError("unexpected answer")
        except (httpx.HTTPError, ValueError) as exc:
            # httpx errors can include the URL, and the URL holds the key.
            status = f" {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else ""
            raise TrainTrackerError(exc.__class__.__name__ + status) from None
        if str(body.get("errCd", "0")) != "0":
            code = int(body["errCd"]) if str(body.get("errCd")).isdigit() else None
            raise TrainTrackerError(f"CTA error {body.get('errCd')}: {body.get('errNm') or 'unknown'}", code)
        self.last_live = time.time()
        self.error = None
        return body

    async def positions(self, recorder: bool = False) -> dict:
        """Every train on every line. Shared by the map, the recorder and the
        check that a train exists before following it."""
        return await self.cache.get(
            "positions", ARRIVALS_TTL,
            lambda: self.call("ttpositions.aspx", {"rt": POSITION_ROUTES}, recorder=recorder))

    async def arrivals(self, station: str) -> dict:
        return await self.cache.get(f"arr:{station}", ARRIVALS_TTL,
                                    lambda: self.call("ttarrivals.aspx", {"mapid": station}))

    async def follow(self, run: str) -> dict:
        return await self.cache.get(f"follow:{run}", FOLLOW_TTL,
                                    lambda: self.call("ttfollow.aspx", {"runnumber": run}))

    async def alerts(self) -> list:
        """CTA's current alerts, as CTA sends them (no key needed)."""
        async def fetch() -> list:
            client = self._get_client()
            if client is None:
                raise ValueError("not started")
            r = await client.get(ALERTS_URL, timeout=LIVE_TIMEOUT,
                                 params={"outputType": "JSON", "activeonly": "true"})
            r.raise_for_status()
            data = r.json()
            return as_list(((data if isinstance(data, dict) else {}).get("CTAAlerts") or {}).get("Alert"))

        return await self.cache.get("alerts", ALERTS_TTL, fetch)
