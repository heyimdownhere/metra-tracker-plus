"""FastAPI entry point: JSON API + the static web frontend.

The browser only ever talks to this server; all Metra requests are made
server-side (a Metra data-license requirement).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import logging
import time
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from .basemap import BaseMap
from .config import load_settings, parse_switch_time, parse_work_days
from .tracker import AmbiguousStation, Tracker, normalize_direction
from .user_settings import UserSettings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
settings = load_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("metra")
tracker = Tracker(settings)
user_settings = UserSettings(settings.config_dir / "settings.json")
basemap = BaseMap(settings.config_dir, enabled=settings.map_download, source=settings.map_source,
                  bbox=settings.map_bbox, max_zoom=settings.map_max_zoom, refresh_days=settings.map_refresh_days)


async def _map_loop():
    """Download the self-hosted map on first start, then refresh it now and then."""
    import asyncio
    await asyncio.sleep(5)  # let the schedule download go first
    while True:
        if basemap.needs_download():
            await asyncio.to_thread(basemap.download)
            await asyncio.sleep(3600 if basemap.error else 6 * 3600)
        else:
            await asyncio.sleep(6 * 3600)


_warned: set[str] = set()


def _resolve_env_station(name: str, value: str | None) -> str | None:
    """Env station values may be an id ("EVANSTON") or a name ("Evanston (Davis St.)")."""
    if not value:
        return None
    try:
        return tracker.find_station(value)
    except KeyError:
        if (name, value) not in _warned:  # warn once, not on every request
            _warned.add((name, value))
            log.warning("%s=%r doesn't match a station (or matches several); see /api/stations", name, value)
        return None


def profile(now: float | None = None) -> dict:
    """The commute profile: home, work, switch time and workdays.

    Values saved in the app win; anything not set there falls back to the
    environment (HOME_STATION, HOME_DESTINATION, WORK_STATION,
    COMMUTE_SWITCH_TIME, WORK_DAYS). `leg` says which board to open now.
    """
    cfg = user_settings.load()
    loaded = tracker.static is not None
    out = {}
    if cfg["home_station"]:
        out.update(home_station=cfg["home_station"], home_dest=cfg["home_dest"], source="app")
    elif loaded and settings.home_station:
        home_id = _resolve_env_station("HOME_STATION", settings.home_station)
        out.update(home_station=home_id,
                   home_dest=_resolve_env_station("HOME_DESTINATION", settings.home_destination) if home_id else None,
                   source="env" if home_id else None)
    else:
        out.update(home_station=None, home_dest=None, source=None)
    if cfg["work_station"]:
        out.update(work_station=cfg["work_station"], work_source="app")
    else:
        work_id = _resolve_env_station("WORK_STATION", settings.work_station) if loaded else None
        out.update(work_station=work_id, work_source="env" if work_id else None)
    out["switch_time"] = cfg["switch_time"] or settings.commute_switch_time
    out["work_days"] = list(cfg["work_days"] if cfg["work_days"] is not None else settings.work_days)
    out["leg"] = _commute_leg(out, now) if loaded else None
    return out


def _commute_leg(p: dict, now: float | None = None) -> dict | None:
    """Which trip to show right now.

    On a workday with both home and work set: before the switch time it's
    home -> work, after it work -> home. Otherwise just the home board.
    """
    home, work = p["home_station"], p["work_station"]
    if not home:
        return None
    local = datetime.fromtimestamp(time.time() if now is None else now, tracker.static.tz)
    if work and work != home and local.weekday() in p["work_days"]:
        if local.strftime("%H:%M") < p["switch_time"]:
            return {"leg": "to_work", "from": home, "to": work}
        return {"leg": "to_home", "from": work, "to": home}
    return {"leg": "home", "from": home, "to": p["home_dest"]}


def effective_home() -> dict:
    """Backwards-compatible alias: the profile (home_station/home_dest/source + work fields)."""
    return profile()


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info(settings.summary())
    for warning in settings.warnings:
        log.warning("Config: %s", warning)
    await tracker.start()
    import asyncio
    map_task = asyncio.create_task(_map_loop(), name="map")
    yield
    map_task.cancel()
    await tracker.stop()


app = FastAPI(title="Metra Tracker Plus", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
# Compress JSON and the JS/CSS bundles (the map library alone is ~1 MB): keeps
# phone data use down.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

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
    return _json({**tracker.status(), "map": basemap.status()})


@app.get("/api/stations")
def stations():
    return _json(tracker.stations())


@app.get("/api/departures")
def departures(
    station: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
    to: str | None = Query(None, description="Only trains that later stop at this station"),
    direction: str | None = Query(None, description="inbound/to or outbound/from downtown"),
):
    if tracker.static is None:
        raise HTTPException(503, "Schedule not loaded yet")
    try:
        d = normalize_direction(direction)
    except ValueError:
        raise HTTPException(400, "direction must be inbound/to or outbound/from (or omitted for both)")
    try:
        return _json(tracker.departures(station, limit, to=to or None, direction=d))
    except KeyError as exc:
        raise HTTPException(404, f"Unknown station {exc.args[0]!r}")


@app.get("/api/trains")
def trains():
    return _json(tracker.trains())


@app.get("/api/alerts")
def alerts():
    return _json(tracker.alerts())


@app.get("/api/next")
def next_trains(
    station: str = Query(..., min_length=1, description="Station id (e.g. EVANSTON) or name"),
    direction: str | None = Query(
        None, description="inbound/to (toward downtown), outbound/from (away from downtown); omit for both"
    ),
    limit: int = Query(5, ge=1, le=20),
):
    """Next trains at a station, optionally only toward or away from downtown."""
    if tracker.static is None:
        raise HTTPException(503, "Schedule not loaded yet")
    try:
        d = normalize_direction(direction)
    except ValueError:
        raise HTTPException(400, "direction must be inbound/to or outbound/from (or omitted for both)")
    try:
        return _json(tracker.next_trains(station, d, limit))
    except AmbiguousStation as exc:
        opts = ", ".join(f"{o['id']} ({'/'.join(o['lines'])})" for o in exc.options)
        return _json({
            "detail": f"{station!r} matches more than one station; use an id: {opts}",
            "options": exc.options,
        }, 400)
    except KeyError:
        raise HTTPException(404, f"Unknown station {station!r}. See /api/stations for ids.")


@app.get("/api/settings")
def get_settings():
    """Settings shared by all devices, e.g. the home station.

    `source` says where the home station came from: "app" (saved with
    "Set as home"), "env" (HOME_STATION), or null (none).
    """
    return _json(effective_home())


@app.put("/api/settings")
def put_settings(changes: dict = Body(...)):
    s = tracker.static
    if s is None:
        # Can't validate station ids until the schedule is loaded.
        raise HTTPException(503, "Schedule not loaded yet")
    for key in ("home_station", "home_dest", "work_station"):
        if key in changes:
            value = changes[key] or None
            if value is not None and (not isinstance(value, str) or value not in s.stops):
                raise HTTPException(400, f"Unknown station for {key}: {value!r}")
            changes[key] = value
    if "switch_time" in changes and changes["switch_time"] not in (None, ""):
        parsed = parse_switch_time(str(changes["switch_time"]))
        if parsed is None:
            raise HTTPException(400, "switch_time must be a time like 12:00")
        changes["switch_time"] = parsed
    elif "switch_time" in changes:
        changes["switch_time"] = None
    if "work_days" in changes and changes["work_days"] is not None:
        parsed_days = parse_work_days(changes["work_days"])
        if parsed_days is None:
            raise HTTPException(400, "work_days must be weekdays like [0,1,2,3,4] (Monday = 0) or 'mon-fri'")
        changes["work_days"] = list(parsed_days)
    try:
        user_settings.update(changes)
        return _json(effective_home())
    except OSError as exc:
        raise HTTPException(500, f"Could not save settings: {exc}")


def _board(from_id: str, to_id: str | None, limit: int, extra: dict | None = None):
    try:
        board = tracker.departures(from_id, limit, to=to_id or None)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown station {exc.args[0]!r}")
    return _json({**board, **(extra or {})})


@app.get("/api/home")
def home(limit: int = Query(5, ge=1, le=20)):
    """Departure board for the home station (to its home destination, if set)."""
    if tracker.static is None:
        raise HTTPException(503, "Schedule not loaded yet")
    p = profile()
    if not p["home_station"]:
        raise HTTPException(404, "No home station set (use 'Set as home' in the app, or HOME_STATION)")
    return _board(p["home_station"], p["home_dest"], limit)


@app.get("/api/work")
def work(limit: int = Query(5, ge=1, le=20)):
    """Departure board for the trip home from work (work station -> home station)."""
    if tracker.static is None:
        raise HTTPException(503, "Schedule not loaded yet")
    p = profile()
    if not p["work_station"]:
        raise HTTPException(404, "No work station set (use 'Set as work' in the app, or WORK_STATION)")
    return _board(p["work_station"], p["home_station"], limit)


@app.get("/api/now")
def now_board(limit: int = Query(5, ge=1, le=20)):
    """The board for right now: home -> work before the switch time on workdays,
    work -> home after it, otherwise the home board. `leg` says which."""
    if tracker.static is None:
        raise HTTPException(503, "Schedule not loaded yet")
    leg = profile()["leg"]
    if not leg:
        raise HTTPException(404, "No home station set (use 'Set as home' in the app, or HOME_STATION)")
    return _board(leg["from"], leg["to"], limit, {"leg": leg["leg"]})


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
    if not tracker.history.available:
        raise HTTPException(404, "History is disabled (needs METRA_API_TOKEN and HISTORY_DAYS > 0)")


@app.get("/api/history/station")
def history_station(station: str = Query(..., min_length=1), days: int = Query(30, ge=1, le=3650)):
    """On-time performance at a station, per scheduled departure."""
    _require_history()
    if tracker.static is None:
        raise HTTPException(503, "Schedule not loaded yet")
    try:
        stop_ids = tracker.station_stops(station)
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


# ---- self-hosted map ---------------------------------------------------------------
@app.get("/api/map")
def map_status():
    """Self-hosted map: state (ready/downloading/missing/error/disabled), size, progress."""
    return _json(basemap.status())


@app.get("/map/tiles/{z}/{x}/{y}.mvt", include_in_schema=False)
def map_tile(z: int, x: int, y: int):
    data, encoding = basemap.tile(z, x, y)
    if data is None:
        return Response(status_code=204, headers={"Cache-Control": "public, max-age=86400"})
    headers = {"Cache-Control": "public, max-age=86400"}
    if encoding:
        headers["Content-Encoding"] = encoding  # already compressed; GZipMiddleware leaves it alone
    return Response(data, media_type="application/vnd.mapbox-vector-tile", headers=headers)


@app.get("/map/assets/{path:path}", include_in_schema=False)
def map_asset(path: str):
    """Label fonts and icon sprites, fetched once by the server and cached in /config/map."""
    file = basemap.asset(path)
    if file is None:
        raise HTTPException(404, "Not available")
    media = ("application/x-protobuf" if path.endswith(".pbf")
             else "application/json" if path.endswith(".json") else "image/png")
    return FileResponse(file, media_type=media, headers={"Cache-Control": "public, max-age=604800"})


# ---- web app --------------------------------------------------------------------------
# index.html links each script/stylesheet as "file?v=<content hash>", so the browser
# can keep those files for a year and only fetches one again after it changes.
ASSET_REF = re.compile(r'(?P<attr>(?:src|href))="(?P<path>[^"?#:]+\.(?:js|css))"')
_index_cache: dict = {}


def _versioned_index() -> bytes:
    page = WEB_DIR / "index.html"
    files = [page] + [WEB_DIR / m.group("path") for m in ASSET_REF.finditer(page.read_text())]
    key = tuple((f, f.stat().st_mtime_ns) for f in files if f.exists())
    if _index_cache.get("key") != key:
        def stamp(m: re.Match) -> str:
            f = WEB_DIR / m.group("path")
            if not f.is_file():
                return m.group(0)
            digest = hashlib.sha1(f.read_bytes()).hexdigest()[:10]
            return f'{m.group("attr")}="{m.group("path")}?v={digest}"'
        _index_cache.update(key=key, body=ASSET_REF.sub(stamp, page.read_text()).encode())
    return _index_cache["body"]


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index():
    return Response(_versioned_index(), media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


class WebFiles(StaticFiles):
    """Static files: versioned links (?v=) are cached for a year, anything else is re-checked."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            versioned = b"v=" in scope.get("query_string", b"")
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-cache"
        return response


app.mount("/", WebFiles(directory=WEB_DIR), name="web")
