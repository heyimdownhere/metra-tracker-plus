"""FastAPI entry point: JSON API + the static web frontend.

The browser only ever talks to this server; all Metra requests are made
server-side (a Metra data-license requirement).
"""

from __future__ import annotations

import csv
import io
import logging
import os
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import load_settings
from .tracker import Tracker
from .user_settings import UserSettings

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
settings = load_settings()
tracker = Tracker(settings)
user_settings = UserSettings(settings.config_dir / "settings.json")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await tracker.start()
    yield
    await tracker.stop()


app = FastAPI(title="Metra Tracker Plus", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)

NO_STORE = {"Cache-Control": "no-store"}


def _json(data, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=NO_STORE)


@app.get("/healthz")
@app.get("/api/health")
def health():
    ok = tracker.static is not None
    return _json(
        {
            "status": "ok" if ok else "starting",
            "static_loaded": ok,
            "mode": "realtime" if settings.realtime_enabled else "schedule-only",
            "realtime_live": tracker.realtime_live,
        },
        200 if ok else 503,
    )


@app.get("/api/status")
def status():
    return _json(tracker.status())


@app.get("/api/stations")
def stations():
    return _json(tracker.stations())


@app.get("/api/departures")
def departures(
    station: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
    to: str | None = Query(None, description="Only trains that later stop at this station"),
):
    if tracker.static is None:
        raise HTTPException(503, "Schedule not loaded yet")
    try:
        return _json(tracker.departures(station, limit, to=to or None))
    except KeyError as exc:
        raise HTTPException(404, f"Unknown station {exc.args[0]!r}")


@app.get("/api/trains")
def trains():
    return _json(tracker.trains())


@app.get("/api/alerts")
def alerts():
    return _json(tracker.alerts())


@app.get("/api/settings")
def get_settings():
    """Settings shared by all devices, e.g. the home station."""
    return _json(user_settings.load())


@app.put("/api/settings")
def put_settings(changes: dict = Body(...)):
    s = tracker.static
    for key in ("home_station", "home_dest"):
        if key in changes:
            value = changes[key] or None
            if value is not None and (not isinstance(value, str) or (s and value not in s.stops)):
                raise HTTPException(400, f"Unknown station for {key}: {value!r}")
            changes[key] = value
    try:
        return _json(user_settings.update(changes))
    except OSError as exc:
        raise HTTPException(500, f"Could not save settings: {exc}")


@app.get("/api/home")
def home(limit: int = Query(5, ge=1, le=20)):
    """Departure board for the saved home station (handy for wall displays and Home Assistant)."""
    cfg = user_settings.load()
    if not cfg["home_station"]:
        raise HTTPException(404, "No home station set")
    if tracker.static is None:
        raise HTTPException(503, "Schedule not loaded yet")
    try:
        return _json(tracker.departures(cfg["home_station"], limit, to=cfg["home_dest"]))
    except KeyError as exc:
        raise HTTPException(404, f"Unknown station {exc.args[0]!r}")


@app.get("/api/commute")
def commute(
    from_: str = Query(..., alias="from", min_length=1, description="Origin station id"),
    to: str = Query(..., min_length=1, description="Destination station id"),
    limit: int = Query(3, ge=1, le=10),
):
    """Next trains from one station to another. Designed for Home Assistant's REST sensor."""
    if tracker.static is None:
        raise HTTPException(503, "Schedule not loaded yet")
    try:
        return _json(tracker.commute(from_, to, limit))
    except KeyError as exc:
        raise HTTPException(404, f"Unknown station {exc.args[0]!r}")


def _require_history():
    if not tracker.history.enabled or tracker.history._db is None:
        raise HTTPException(404, "History is disabled (needs METRA_API_TOKEN and HISTORY_DAYS > 0)")


@app.get("/api/history/station")
def history_station(station: str = Query(..., min_length=1), days: int = Query(30, ge=1, le=3650)):
    """On-time performance at a station, per scheduled departure."""
    _require_history()
    try:
        stop_ids = tracker._station_stops(station)
    except KeyError:
        raise HTTPException(404, f"Unknown station {station!r}")
    tz = tracker.static.tz
    return _json({
        "station": station,
        "days": days,
        "departures": tracker.history.station_reliability(
            stop_ids, days, lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M")
        ),
    })


@app.get("/api/history/alerts")
def history_alerts(days: int = Query(30, ge=1, le=3650)):
    _require_history()
    return _json(tracker.history.alerts(days))


@app.get("/api/history/export.csv")
def history_export(days: int = Query(30, ge=1, le=3650)):
    """Raw observations as CSV, for spreadsheets or long-term archiving."""
    _require_history()
    tz = tracker.static.tz if tracker.static else None
    cols = ["service_date", "trip_id", "stop_id", "route_id", "headsign", "stop_sequence",
            "scheduled", "predicted", "delay", "max_delay", "cancelled", "delay_reason"]

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in tracker.history.iter_observations(days):
            for k in ("scheduled", "predicted"):
                if r[k] is not None:
                    r[k] = datetime.fromtimestamp(r[k], tz).isoformat(timespec="seconds")
            w.writerow([r[c] for c in cols])
            if buf.tell() > 65536:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    return StreamingResponse(
        gen(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="metra-history-{days}d.csv"'},
    )


@app.get("/api/lines")
def lines():
    data = tracker.lines_geojson()
    version = tracker.static.version if tracker.static else ""
    return JSONResponse(data, headers={"Cache-Control": "public, max-age=300", "X-GTFS-Version": version})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
