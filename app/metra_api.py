"""Metra's API: boards, timetable, trains, alerts, history and settings.

Served twice: at /api/metra/... (the same layout as every other system), and
at /api/... where it has always been, so Home Assistant sensors and bookmarks
keep working.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from .commute import SettingsChange, clean_changes, commute_leg
from .config import Settings
from .tracker import AmbiguousStation, Tracker, normalize_direction
from .user_settings import UserSettings
from .util import NO_STORE, csv_response, json_response as _json

log = logging.getLogger("metra")

EXPORT_COLUMNS = ["service_date", "trip_id", "stop_id", "route_id", "headsign", "stop_sequence",
                  "scheduled", "predicted", "delay", "max_delay", "cancelled", "delay_reason"]


# ---- shapes of the answers Home Assistant reads -------------------------------------------
class History(BaseModel):
    model_config = ConfigDict(extra="allow")

    observations: int
    on_time_pct: float | None
    avg_delay_min: float | None
    worst_delay_min: float
    cancelled: int
    days: int


class Train(BaseModel):
    departure: str | None
    departure_scheduled: str | None
    minutes_until: int
    status: str
    delay_min: int | None
    live: bool
    cancelled: bool
    direction: str | None
    line: str | None
    line_name: str | None
    train: str | None
    destination: str | None
    final_stop: str | None
    final_arrival: str | None
    final_arrival_scheduled: str | None
    tracking: bool
    delay_reason: str | None
    history: History | None
    # Only when a destination was asked for (/commute):
    arrival: str | None = None
    arrival_scheduled: str | None = None
    duration_min: int | None = None


class Place(BaseModel):
    id: str
    name: str


class NextTrains(BaseModel):
    """GET /next: the next trains at a station."""

    station: Place
    direction: str
    trains: list[Train]
    next_departure: str | None
    next_minutes: int | None
    message: str
    realtime: bool
    generated_at: str | None


class Commute(BaseModel):
    """GET /commute: the next trains from one station to another."""

    model_config = ConfigDict(populate_by_name=True)

    from_: Place
    to: Place
    next_departure: str | None
    next_minutes: int | None
    next_status: str | None
    trips: list[Train]
    message: str
    realtime: bool
    generated_at: str | None


Commute.model_fields["from_"].alias = "from"
Commute.model_fields["from_"].serialization_alias = "from"
Commute.model_fields["from_"].validation_alias = "from"
Commute.model_rebuild(force=True)


# ---- home/work ---------------------------------------------------------------------------
class MetraCommute:
    """The commute profile: home, work, switch time and workdays.

    Values saved in the app win; anything not set there falls back to the
    environment (HOME_STATION, HOME_DESTINATION, WORK_STATION,
    COMMUTE_SWITCH_TIME, WORK_DAYS).
    """

    def __init__(self, tracker: Tracker, store: UserSettings, settings: Settings):
        self.tracker, self.store, self.settings = tracker, store, settings
        self._warned: set[tuple[str, str]] = set()

    def _env_station(self, name: str, value: str | None) -> str | None:
        """Env station values may be an id ("EVANSTON") or a name ("Evanston (Davis St.)")."""
        if not value:
            return None
        try:
            return self.tracker.find_station(value)
        except KeyError:
            if (name, value) not in self._warned:  # warn once, not on every request
                self._warned.add((name, value))
                log.warning("%s=%r doesn't match a station (or matches several); see /api/stations", name, value)
            return None

    def profile(self, now: float | None = None, cfg: dict | None = None) -> dict:
        """`cfg`: saved settings to use instead of the stored ones (to check a
        change before saving it). `leg` says which board to open now."""
        cfg = self.store.load() if cfg is None else cfg
        settings, tracker = self.settings, self.tracker
        loaded = tracker.static is not None
        out: dict = {}
        if cfg["home_station"]:
            out.update(home_station=cfg["home_station"], home_dest=cfg["home_dest"], source="app")
        elif loaded and settings.home_station:
            home_id = self._env_station("HOME_STATION", settings.home_station)
            out.update(home_station=home_id,
                       home_dest=self._env_station("HOME_DESTINATION", settings.home_destination) if home_id else None,
                       source="env" if home_id else None)
        else:
            out.update(home_station=None, home_dest=None, source=None)
        if cfg["work_station"]:
            out.update(work_station=cfg["work_station"], work_source="app")
        else:
            work_id = self._env_station("WORK_STATION", settings.work_station) if loaded else None
            out.update(work_station=work_id, work_source="env" if work_id else None)
        out["switch_time"] = cfg["switch_time"] or settings.commute_switch_time
        out["work_days"] = list(cfg["work_days"] if cfg["work_days"] is not None else settings.work_days)
        out["leg"] = commute_leg(out, now, tracker.static.tz) if loaded else None
        return out


def export_window(days: int, end: date | None, tz) -> tuple[float, float]:
    """(since, until) in epoch seconds: the `days` up to now, or up to the end of `end`."""
    if end is None:
        until = time.time() + 86400  # include what is scheduled for the rest of today
        return until - (days + 1) * 86400, until
    nxt = end + timedelta(days=1)
    until = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).timestamp()
    return until - days * 86400, until


def export_headers(info: dict) -> dict:
    """Tell the client what it got: how many rows matched, how many were sent."""
    return {"X-Export-Total-Rows": str(info["total"]), "X-Export-Rows": str(info["rows"]),
            "X-Export-Limit": str(info["limit"]), "X-Export-Truncated": "true" if info["truncated"] else "false"}


def make_router(tracker: Tracker, commute: MetraCommute, settings: Settings) -> APIRouter:
    r = APIRouter(tags=["Metra"])

    def need_schedule():
        if tracker.static is None:
            raise HTTPException(503, "Schedule not loaded yet")

    def direction_or_400(value: str | None) -> str | None:
        try:
            return normalize_direction(value)
        except ValueError:
            raise HTTPException(400, "direction must be inbound/to or outbound/from (or omitted for both)")

    def need_history():
        if not tracker.history.available:
            raise HTTPException(404, "History is disabled (needs METRA_API_TOKEN and HISTORY_DAYS > 0)")

    @r.get("/stations")
    def stations(request: Request):
        """Every station with its lines. Only changes when the schedule does."""
        return tracker.stations_json().response(request) if tracker.static else _json([])

    @r.get("/departures")
    def departures(
        station: str = Query(..., min_length=1),
        limit: int = Query(5, ge=1, le=50),
        to: str | None = Query(None, description="Only trains that later stop at this station"),
        direction: str | None = Query(None, description="inbound/to or outbound/from downtown"),
        route: str | None = Query(None, description="Only trains on this line (route id, e.g. UP-N)"),
    ):
        need_schedule()
        d = direction_or_400(direction)
        try:
            return _json(tracker.departures(station, limit, to=to or None, direction=d, route=route or None))
        except KeyError as exc:
            raise HTTPException(404, f"Unknown station {exc.args[0]!r}")

    @r.get("/destinations")
    def destinations(station: str = Query(..., min_length=1)):
        """Stations reachable on a direct train from `station`, nearest first (for the Going-to picker)."""
        need_schedule()
        try:
            return _json(tracker.destinations(station), headers={"Cache-Control": "public, max-age=3600"})
        except KeyError as exc:
            raise HTTPException(404, f"Unknown station {exc.args[0]!r}")

    @r.get("/line")
    def line(
        route: str = Query(..., min_length=1, description="Route id, e.g. UP-N"),
        direction: str | None = Query(None, description="inbound/to or outbound/from downtown; omit for both"),
    ):
        """Every train on a line still to run today, running ones first."""
        need_schedule()
        d = direction_or_400(direction)
        try:
            return _json(tracker.line_trains(route, d))
        except KeyError as exc:
            raise HTTPException(404, f"Unknown line {exc.args[0]!r}")

    @r.get("/trip")
    def trip(trip_id: str = Query(..., min_length=1)):
        """One train's every stop with scheduled and expected times, and where it is if running."""
        need_schedule()
        try:
            return _json(tracker.trip_detail(trip_id))
        except KeyError:
            raise HTTPException(404, f"Unknown train {trip_id!r}")

    @r.get("/timetable")
    def timetable(
        station: str = Query(..., min_length=1),
        to: str | None = Query(None, description="Only trains that later stop at this station"),
    ):
        """Every train still to come today (until 3 AM) from a station, with live times where available."""
        need_schedule()
        try:
            return _json(tracker.timetable(station, to=to or None))
        except KeyError as exc:
            raise HTTPException(404, f"Unknown station {exc.args[0]!r}")

    @r.get("/trains")
    def trains(
        to: str | None = Query(None, description="Only trains that will still stop at this station"),
        frm: str | None = Query(None, alias="from", description="With `to`: only trains running from this station toward `to`"),
    ):
        try:
            return _json(tracker.trains(to=to or None, frm=frm or None))
        except KeyError as exc:
            raise HTTPException(404, f"Unknown station {exc.args[0]!r}")

    @r.get("/alerts")
    def alerts():
        return _json(tracker.alerts())

    @r.get("/lines")
    def lines(request: Request):
        """The lines as GeoJSON. Only changes when the schedule does."""
        if tracker.static is None:
            return _json({"type": "FeatureCollection", "features": []})
        return tracker.lines_json().response(request)

    # ---- plain JSON for Home Assistant / scripts ---------------------------------------
    @r.get("/next", response_model=NextTrains, response_model_exclude_unset=True)
    def next_trains(
        response: Response,
        station: str = Query(..., min_length=1, description="Station id (e.g. EVANSTON) or name"),
        direction: str | None = Query(
            None, description="inbound/to (toward downtown), outbound/from (away from downtown); omit for both"
        ),
        limit: int = Query(5, ge=1, le=20),
    ):
        """Next trains at a station, optionally only toward or away from downtown."""
        need_schedule()
        d = direction_or_400(direction)
        response.headers.update(NO_STORE)
        try:
            return tracker.next_trains(station, d, limit)
        except AmbiguousStation as exc:
            opts = ", ".join(f"{o['id']} ({'/'.join(o['lines'])})" for o in exc.options)
            return _json({
                "detail": f"{station!r} matches more than one station; use an id: {opts}",
                "options": exc.options,
            }, 400)
        except KeyError:
            raise HTTPException(404, f"Unknown station {station!r}. See /api/stations for ids.")

    @r.get("/commute", response_model=Commute, response_model_exclude_unset=True, response_model_by_alias=True)
    def commute_trains(
        response: Response,
        from_: str = Query(..., alias="from", min_length=1, description="Origin station id"),
        to: str = Query(..., min_length=1, description="Destination station id"),
        limit: int = Query(3, ge=1, le=10),
    ):
        """Next trains from one station to another. Designed for Home Assistant's REST sensor."""
        need_schedule()
        response.headers.update(NO_STORE)
        try:
            return tracker.commute(from_, to, limit)
        except KeyError as exc:
            raise HTTPException(404, f"Unknown station {exc.args[0]!r}")

    # ---- settings and the boards built on them -----------------------------------------
    @r.get("/settings")
    def get_settings():
        """Settings shared by all devices, e.g. the home station.

        `source` says where the home station came from: "app" (saved with
        "Set as home"), "env" (HOME_STATION), or null (none).
        """
        return _json(commute.profile())

    @r.put("/settings")
    def put_settings(body: SettingsChange):
        s = tracker.static
        if s is None:
            # Can't validate station ids until the schedule is loaded.
            raise HTTPException(503, "Schedule not loaded yet")
        changes = clean_changes(body, lambda sid: sid in s.stops)

        def check(saved: dict) -> None:
            # Home and work (from the app or the environment) can't be the same
            # station. Checked on what would be saved, before anything is written.
            p = commute.profile(cfg=saved)
            if p["home_station"] and p["home_station"] == p["work_station"]:
                name = tracker.station_label(p["home_station"])
                raise HTTPException(400, f"Home and work can't both be {name}. Pick a different station for one of them.")

        try:
            commute.store.update(changes, check)
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)
            raise HTTPException(500, f"Could not save settings ({exc.__class__.__name__}). Check that /config is writable.")
        return _json(commute.profile())

    def board(from_id: str, to_id: str | None, limit: int, extra: dict | None = None):
        try:
            data = tracker.departures(from_id, limit, to=to_id or None)
        except KeyError as exc:
            raise HTTPException(404, f"Unknown station {exc.args[0]!r}")
        return _json({**data, **(extra or {})})

    @r.get("/home")
    def home(limit: int = Query(5, ge=1, le=50)):
        """Departure board for the home station (to its home destination, if set)."""
        need_schedule()
        p = commute.profile()
        if not p["home_station"]:
            raise HTTPException(404, "No home station set (use 'Set as home' in the app, or HOME_STATION)")
        return board(p["home_station"], p["home_dest"], limit)

    @r.get("/work")
    def work(limit: int = Query(5, ge=1, le=50)):
        """Departure board for the trip home from work (work station -> home station)."""
        need_schedule()
        p = commute.profile()
        if not p["work_station"]:
            raise HTTPException(404, "No work station set (use 'Set as work' in the app, or WORK_STATION)")
        return board(p["work_station"], p["home_station"], limit)

    @r.get("/now")
    def now_board(limit: int = Query(5, ge=1, le=50)):
        """The board for right now: home -> work before the switch time on workdays,
        work -> home after it, otherwise the home board. `leg` says which."""
        need_schedule()
        leg = commute.profile()["leg"]
        if not leg:
            raise HTTPException(404, "No home station set (use 'Set as home' in the app, or HOME_STATION)")
        return board(leg["from"], leg["to"], limit, {"leg": leg["leg"]})

    # ---- history -----------------------------------------------------------------------
    @r.get("/history/station")
    def history_station(station: str = Query(..., min_length=1), days: int = Query(30, ge=1, le=3650)):
        """On-time performance at a station, per scheduled departure."""
        need_history()
        need_schedule()
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

    @r.get("/history/alerts")
    def history_alerts(days: int = Query(30, ge=1, le=3650)):
        need_history()
        return _json(tracker.history.alerts(days))

    @r.get("/history/export.csv")
    def history_export(
        days: int = Query(30, ge=1, le=3650, description="How many days, counting back from `end`"),
        end: date | None = Query(None, description="Last day to include (YYYY-MM-DD); default today"),
    ):
        """Raw observations as CSV, oldest first.

        One download holds at most EXPORT_MAX_ROWS rows (250,000 unless
        changed). If more match, you get the newest ones and the header
        X-Export-Truncated: true; ask again with an earlier `end` for the rest.
        """
        need_history()
        tz = tracker.static.tz if tracker.static else None
        since, until = export_window(days, end, tz)
        info, rows = tracker.history.export_observations(since, until, settings.export_max_rows)

        def readable():
            for row in rows:
                for k in ("scheduled", "predicted"):
                    if row[k] is not None:
                        row[k] = datetime.fromtimestamp(row[k], tz).isoformat(timespec="seconds")
                yield row

        return csv_response(f"metra-history-{days}d.csv", EXPORT_COLUMNS, readable(), export_headers(info))

    return r
