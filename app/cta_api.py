"""/api/cta/...: the CTA side of the app.

Separate from the Metra endpoints and data: CTA home/work are saved in
/config/cta-settings.json, and station ids here are CTA's (e.g. 40380 for
Clark/Lake), never Metra's.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Callable

from fastapi import APIRouter, HTTPException, Query, Request

from .commute import SettingsChange, clean_changes, commute_leg
from .config import Settings
from .cta import TZ, CtaTracker, TrainTrackerError, local_hour
from .cta_history import EXPORT_COLUMNS
from .metra_api import export_headers, export_window
from .user_settings import UserSettings
from .util import StaticJson, csv_response, json_response as _json

log = logging.getLogger(__name__)


def make_router(cta: CtaTracker, store: UserSettings, settings: Settings,
                leg: Callable = commute_leg, prefix: str = "/api/cta") -> APIRouter:
    r = APIRouter(prefix=prefix, tags=["CTA"])
    static: dict[str, tuple[str, StaticJson]] = {}

    def need_schedule():
        if not cta.enabled:
            raise HTTPException(404, "CTA is turned off (CTA_ENABLED=false)")
        if cta.schedule is None:
            raise HTTPException(503, "CTA schedule not loaded yet")

    def station_or_404(station_id: str | None):
        if station_id is not None and station_id not in cta.schedule.stations:
            raise HTTPException(404, f"Unknown CTA station {station_id!r}. See /api/cta/stations for ids.")

    def once(name: str, build: Callable[[], object], headers: dict | None = None) -> StaticJson:
        """An answer that only changes with the schedule, built once per schedule version."""
        version = cta.schedule.version
        hit = static.get(name)
        if hit is None or hit[0] != version or hit[1] is None:
            hit = static[name] = (version, StaticJson(build(), headers))
        return hit[1]

    def profile(cfg: dict | None = None) -> dict:
        """CTA home/work. Switch time and workdays fall back to COMMUTE_SWITCH_TIME
        and WORK_DAYS (they're about you, not the railroad); stations never do."""
        cfg = store.load() if cfg is None else cfg
        known = cta.schedule.stations if cta.schedule else {}
        home = cfg["home_station"] if cfg["home_station"] in known else None
        work = cfg["work_station"] if cfg["work_station"] in known else None
        p = {
            "home_station": home, "home_dest": cfg["home_dest"] if home and cfg["home_dest"] in known else None,
            "work_station": work,
            "switch_time": cfg["switch_time"] or settings.commute_switch_time,
            "work_days": list(cfg["work_days"] if cfg["work_days"] is not None else settings.work_days),
        }
        p["leg"] = leg(p, tz=TZ) if cta.schedule else None
        return p

    @r.get("/status")
    def status():
        return _json(cta.status())

    @r.get("/stations")
    def stations(request: Request):
        """Every L station. Only changes when the schedule does."""
        need_schedule()
        return once("stations", cta.stations).response(request)

    @r.get("/lines")
    def lines(request: Request):
        """L lines (id, names, colours) and their track shapes as GeoJSON."""
        need_schedule()
        return once("lines", lambda: {"lines": cta.lines(), "geojson": cta.lines_geojson()}).response(request)

    @r.get("/arrivals")
    async def arrivals(
        station: str = Query(..., min_length=1, max_length=10, description="CTA station id, e.g. 40380 (Clark/Lake)"),
        to: str | None = Query(None, max_length=10, description="Only trains that go on to this station"),
        limit: int = Query(4, ge=1, le=10, description="Trains per line and direction"),
    ):
        """Next trains, one group per line and direction (live, or from the schedule)."""
        need_schedule()
        station_or_404(station)
        station_or_404(to or None)
        return _json(await cta.arrivals(station, to=to or None, per_group=limit))

    @r.get("/timetable")
    async def timetable(station: str = Query(..., min_length=1, max_length=10),
                        to: str | None = Query(None, max_length=10, description="Only trains that go on to this station")):
        """Every train still to come today at a station (live times for the next
        few of each line and direction), and how often each line comes."""
        need_schedule()
        station_or_404(station)
        station_or_404(to or None)
        return _json(await cta.timetable_live(station, to=to or None))

    @r.get("/trains")
    async def trains():
        """Every L train's live position."""
        need_schedule()
        return _json(await cta.trains())

    @r.get("/follow")
    async def follow(run: str = Query(..., min_length=1, max_length=4, description="Run number, e.g. 409")):
        """One train's remaining stops and position. Only trains that are running
        now (in the latest positions) are looked up."""
        need_schedule()
        try:
            return _json(await cta.follow(run))
        except KeyError:
            raise HTTPException(404, f"No CTA train {run!r} running now")
        except TrainTrackerError as exc:
            raise HTTPException(503, f"Train Tracker unavailable: {exc}")

    @r.get("/alerts")
    async def alerts():
        """CTA alerts for L lines and stations (bus-only alerts left out)."""
        if not cta.enabled:
            raise HTTPException(404, "CTA is turned off (CTA_ENABLED=false)")
        return _json(await cta.alerts())

    def need_history():
        if not cta.history.available:
            raise HTTPException(404, "CTA history is off (needs CTA_TRAIN_TRACKER_KEY and CTA_HISTORY_DAYS > 0)")

    @r.get("/history/station")
    def history_station(station: str = Query(..., min_length=1, max_length=10),
                        days: int = Query(30, ge=1, le=3650)):
        """How regular each line/direction has been at a station, per day type and hour:
        share of waits that weren't long (more than twice the scheduled gap and 5+ min
        longer), average and scheduled gap, longest wait, trains flagged delayed."""
        need_schedule()
        need_history()
        station_or_404(station)
        return _json({"station": cta.station(station), "days": days,
                      "groups": cta.history.station_stats(station, days, local_hour)})

    @r.get("/history/delays")
    def history_delays(days: int = Query(7, ge=1, le=3650), route: str | None = Query(None, max_length=10)):
        """Trains Train Tracker flagged as delayed: per line totals and the list."""
        need_history()
        return _json({"days": days, "lines": cta.history.delay_summary(days),
                      "delays": cta.history.delays(days, route or None)})

    @r.get("/history/export.csv")
    def history_export(
        days: int = Query(30, ge=1, le=3650, description="How many days, counting back from `end`"),
        end: date | None = Query(None, description="Last day to include (YYYY-MM-DD); default today"),
        station: str | None = Query(None, max_length=10, description="Only this station"),
    ):
        """Recorded departures as CSV, oldest first.

        One download holds at most EXPORT_MAX_ROWS rows (250,000 unless
        changed; the L records about 47,000 a day). If more match, you get the
        newest ones and the header X-Export-Truncated: true; ask again with an
        earlier `end`, or for one `station`, for the rest.
        """
        need_history()
        if station and cta.schedule:
            station_or_404(station)
        since, until = export_window(days, end, TZ)
        info, rows = cta.history.export_arrivals(since, until, settings.export_max_rows, station or None)

        def readable():
            for row in rows:
                row["t"] = datetime.fromtimestamp(row["t"], TZ).isoformat(timespec="seconds")
                yield row

        name = f"cta-history-{station + '-' if station else ''}{days}d.csv"
        return csv_response(name, EXPORT_COLUMNS, readable(), export_headers(info))

    @r.get("/settings")
    def get_settings():
        need_schedule()
        return _json(profile())

    @r.put("/settings")
    def put_settings(body: SettingsChange):
        need_schedule()
        changes = clean_changes(body, lambda sid: sid in cta.schedule.stations, what="CTA station")

        def check(saved: dict) -> None:
            if saved.get("home_station") and saved.get("home_station") == saved.get("work_station"):
                name = cta.schedule.stations[saved["home_station"]]["name"]
                raise HTTPException(400, f"Home and work can't both be {name}. Pick a different station for one of them.")

        try:
            store.update(changes, check)
        except OSError as exc:
            log.warning("Could not save CTA settings: %s", exc)
            raise HTTPException(500, f"Could not save CTA settings ({exc.__class__.__name__}). Check that /config is writable.")
        return _json(profile())

    @r.get("/now")
    async def now_board(limit: int = Query(4, ge=1, le=10)):
        """The CTA board for right now: home -> work before the switch time on
        workdays, work -> home after it, otherwise the home board."""
        need_schedule()
        current = profile()["leg"]
        if not current:
            raise HTTPException(404, "No CTA home station set")
        return _json({**await cta.arrivals(current["from"], to=current["to"], per_group=limit),
                      "leg": current["leg"]})

    return r
