"""/api/cta/...: the CTA side of the app.

Separate from the Metra endpoints and data: CTA home/work are saved in
/config/cta-settings.json, and station ids here are CTA's (e.g. 40380 for
Clark/Lake), never Metra's.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings, parse_switch_time, parse_work_days
from .cta import TZ, CtaTracker, TrainTrackerError
from .user_settings import UserSettings

NO_STORE = {"Cache-Control": "no-store"}


def _json(data, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=headers or NO_STORE)


def make_router(cta: CtaTracker, store: UserSettings, settings: Settings, commute_leg: Callable) -> APIRouter:
    r = APIRouter(prefix="/api/cta", tags=["CTA"])

    def need_schedule():
        if not cta.enabled:
            raise HTTPException(404, "CTA is turned off (CTA_ENABLED=false)")
        if cta.schedule is None:
            raise HTTPException(503, "CTA schedule not loaded yet")

    def station_or_404(station_id: str | None):
        if station_id is not None and station_id not in cta.schedule.stations:
            raise HTTPException(404, f"Unknown CTA station {station_id!r}. See /api/cta/stations for ids.")

    def profile() -> dict:
        """CTA home/work. Switch time and workdays fall back to COMMUTE_SWITCH_TIME
        and WORK_DAYS (they're about you, not the railroad); stations never do."""
        cfg = store.load()
        known = cta.schedule.stations if cta.schedule else {}
        home = cfg["home_station"] if cfg["home_station"] in known else None
        work = cfg["work_station"] if cfg["work_station"] in known else None
        p = {
            "home_station": home, "home_dest": cfg["home_dest"] if home and cfg["home_dest"] in known else None,
            "work_station": work,
            "switch_time": cfg["switch_time"] or settings.commute_switch_time,
            "work_days": list(cfg["work_days"] if cfg["work_days"] is not None else settings.work_days),
        }
        p["leg"] = commute_leg(p, tz=TZ) if cta.schedule else None
        return p

    @r.get("/status")
    def status():
        return _json(cta.status())

    @r.get("/stations")
    def stations():
        need_schedule()
        return _json(cta.stations())

    @r.get("/lines")
    def lines():
        """L lines (id, names, colours) and their track shapes as GeoJSON."""
        need_schedule()
        return _json({"lines": cta.lines(), "geojson": cta.lines_geojson()},
                     headers={"Cache-Control": "public, max-age=300"})

    @r.get("/arrivals")
    async def arrivals(
        station: str = Query(..., min_length=1, description="CTA station id, e.g. 40380 (Clark/Lake)"),
        to: str | None = Query(None, description="Only trains that go on to this station"),
        limit: int = Query(4, ge=1, le=10, description="Trains per line and direction"),
    ):
        """Next trains, one group per line and direction (live, or from the schedule)."""
        need_schedule()
        station_or_404(station)
        station_or_404(to or None)
        return _json(await cta.arrivals(station, to=to or None, per_group=limit))

    @r.get("/timetable")
    def timetable(station: str = Query(..., min_length=1)):
        """How often trains come today at a station, with first and last trains."""
        need_schedule()
        station_or_404(station)
        return _json(cta.timetable(station))

    @r.get("/trains")
    async def trains():
        """Every L train's live position."""
        need_schedule()
        return _json(await cta.trains())

    @r.get("/follow")
    async def follow(run: str = Query(..., min_length=1, description="Run number, e.g. 409")):
        """One train's remaining stops and position."""
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
            raise HTTPException(404, "CTA history is off (needs CTA_TRAIN_TRACKER_KEY and HISTORY_DAYS > 0)")

    @r.get("/history/station")
    def history_station(station: str = Query(..., min_length=1), days: int = Query(30, ge=1, le=3650)):
        """How regular each line/direction has been at a station, per day type and hour:
        share of waits that weren't long (more than twice the scheduled gap and 5+ min
        longer), average and scheduled gap, longest wait, trains flagged delayed."""
        need_schedule()
        need_history()
        station_or_404(station)
        return _json({"station": cta.station(station), "days": days,
                      "groups": cta.history.station_stats(station, days, lambda t: datetime.fromtimestamp(t, TZ).hour)})

    @r.get("/history/delays")
    def history_delays(days: int = Query(7, ge=1, le=3650), route: str | None = Query(None)):
        """Trains Train Tracker flagged as delayed: per line totals and the list."""
        need_history()
        return _json({"days": days, "lines": cta.history.delay_summary(days),
                      "delays": cta.history.delays(days, route or None)})

    @r.get("/history/export.csv")
    def history_export(days: int = Query(30, ge=1, le=3650)):
        """Every recorded departure as CSV."""
        need_history()
        cols = ["service_date", "station", "gkey", "run", "t", "gap", "sched_gap", "delayed", "tracked"]

        def gen():
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            for row in cta.history.iter_arrivals(days):
                row["t"] = datetime.fromtimestamp(row["t"], TZ).isoformat(timespec="seconds")
                w.writerow([row[c] for c in cols])
                if buf.tell() > 65536:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()

        return StreamingResponse(gen(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="cta-history-{days}d.csv"'})

    @r.get("/settings")
    def get_settings():
        need_schedule()
        return _json(profile())

    @r.put("/settings")
    def put_settings(changes: dict = Body(...)):
        need_schedule()
        for key in ("home_station", "home_dest", "work_station"):
            if key in changes:
                value = changes[key] or None
                if value is not None and (not isinstance(value, str) or value not in cta.schedule.stations):
                    raise HTTPException(400, f"Unknown CTA station for {key}: {value!r}")
                changes[key] = value
        if "switch_time" in changes and changes["switch_time"] not in (None, ""):
            parsed = parse_switch_time(str(changes["switch_time"]))
            if parsed is None:
                raise HTTPException(400, "switch_time must be a time like 12:00")
            changes["switch_time"] = parsed
        elif "switch_time" in changes:
            changes["switch_time"] = None
        if "work_days" in changes and changes["work_days"] is not None:
            days = parse_work_days(changes["work_days"])
            if days is None:
                raise HTTPException(400, "work_days must be weekdays like [0,1,2,3,4] (Monday = 0) or 'mon-fri'")
            changes["work_days"] = list(days)
        merged = {**store.load(), **{k: v for k, v in changes.items()}}
        if merged.get("home_station") and merged.get("home_station") == merged.get("work_station"):
            name = cta.schedule.stations[merged["home_station"]]["name"]
            raise HTTPException(400, f"Home and work can't both be {name}. Pick a different station for one of them.")
        try:
            store.update(changes)
        except OSError as exc:
            raise HTTPException(500, f"Could not save CTA settings: {exc}")
        return _json(profile())

    async def board(from_id: str, to_id: str | None, limit: int, extra: dict):
        return _json({**await cta.arrivals(from_id, to=to_id, per_group=limit), **extra})

    @r.get("/now")
    async def now_board(limit: int = Query(4, ge=1, le=10)):
        """The CTA board for right now: home -> work before the switch time on
        workdays, work -> home after it, otherwise the home board."""
        need_schedule()
        leg = profile()["leg"]
        if not leg:
            raise HTTPException(404, "No CTA home station set")
        return await board(leg["from"], leg["to"], limit, {"leg": leg["leg"]})

    return r
