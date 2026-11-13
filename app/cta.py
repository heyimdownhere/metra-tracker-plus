"""CTA 'L' trains: schedule, live Train Tracker data and the /api/cta queries.

Kept apart from Metra's data on purpose: the schedule is cached in its own
folder (/config/cta), CTA home/work live in their own settings file, and
nothing here reads or writes the Metra tracker's state.

Train Tracker is only called when someone is looking: arrivals for a station,
all train positions for the map, and one train's remaining stops. Each answer
is cached for at least 30 s and a daily request budget keeps the key well
under CTA's limit of 100,000 requests a day. With no key, or when Train Tracker
fails, boards fall back to the published schedule.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import pickle
import re
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)


class _RedactKey(logging.Filter):
    """Train Tracker takes the key in the address, and httpx logs addresses:
    blank the key out of any log line before it's written."""

    PATTERN = re.compile(r"([?&]key=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "key=" in msg:
            record.msg, record.args = self.PATTERN.sub(r"\1***", msg), ()
        return True


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_RedactKey())

TZ = ZoneInfo("America/Chicago")
API_BASE = "https://lapi.transitchicago.com/api/1.0"
ALERTS_URL = "https://www.transitchicago.com/api/1.0/alerts.aspx"
USER_AGENT = "next-stop/1.0 (self-hosted)"
CREDIT = "Data provided by CTA. Not affiliated with or endorsed by CTA."

# Bump when the pickled schedule's layout changes: an old pickle is then
# ignored and the schedule downloaded again.
SCHEMA = 1

ARRIVALS_TTL = 30      # seconds a station's arrivals are reused
FOLLOW_TTL = 30        # seconds one train's remaining stops are reused
ALERTS_TTL = 120       # seconds the alerts list is reused
DAILY_BUDGET = 80_000  # Train Tracker requests per day before falling back to the schedule (CTA's limit: 100,000)

# Line ids as the schedule uses them; Train Tracker uses lower-case ids for
# positions ("brn") and long names in follow results ("Brown Line").
ROUTE_IDS = {
    "red": "Red", "blue": "Blue", "brn": "Brn", "brown": "Brn", "g": "G", "green": "G",
    "org": "Org", "orange": "Org", "p": "P", "purple": "P", "pexp": "P", "pink": "Pink",
    "y": "Y", "yellow": "Y",
}
SHORT_NAMES = {"Red": "Red", "Blue": "Blue", "Brn": "Brown", "G": "Green", "Org": "Orange",
               "P": "Purple", "Pink": "Pink", "Y": "Yellow"}
LINE_ORDER = ["Red", "Blue", "Brn", "G", "Org", "P", "Pink", "Y"]
POSITION_ROUTES = "red,blue,brn,g,org,p,pink,y"
# Red, Blue and Green trips run one way, so their trains are grouped by
# direction: e.g. southbound Green trains to Ashland/63rd and to Cottage
# Grove share a platform and one board card. The other lines run round trips
# through the Loop, so their trains are grouped by destination.
DIRECTIONAL = {"Red", "Blue", "G"}
DIR_CODES = {"North": "1", "South": "5"}  # schedule direction -> Train Tracker trDr
# A gap longer than this between trains means no service (e.g. no midday
# Purple Express), not a very long wait, in the timetable's periods.
NO_SERVICE_GAP = 60

# Timetable periods (minutes after midnight of the service day).
PERIODS = [
    ("am_rush", "AM rush", 6 * 60, 9 * 60),
    ("midday", "Midday", 9 * 60, 15 * 60),
    ("pm_rush", "PM rush", 15 * 60, 18 * 60 + 30),
    ("evening", "Evening", 18 * 60 + 30, 22 * 60),
    ("late", "Late night", 22 * 60, 26 * 60),
]


def route_id(value: str | None) -> str | None:
    """'brn', 'Brown Line', 'Brn' -> 'Brn'."""
    if not value:
        return None
    v = value.strip()
    if v in SHORT_NAMES:
        return v
    v = v.lower().removesuffix(" line").strip()
    return ROUTE_IDS.get(v)


def group_key(route: str, direction_code: str | None, destination: str | None) -> str:
    """Which board card a train belongs to (see DIRECTIONAL)."""
    if route in DIRECTIONAL and direction_code:
        return f"{route}:{direction_code}"
    return f"{route}:{destination or ''}"


def parse_cta_time(value: str | None) -> float | None:
    """Train Tracker times are Chicago wall-clock times without an offset."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp()
    except ValueError:
        return None


def _secs(value: str) -> int | None:
    try:
        h, m, s = value.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except (ValueError, AttributeError):
        return None


def _ymd(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y%m%d").date()


def _platform(description: str | None) -> str | None:
    """'Service at Outer Loop platform' -> 'Outer Loop'; 'Subway service toward O'Hare' -> 'Subway'."""
    if not description:
        return None
    d = description.strip()
    if d.lower().startswith("subway"):
        return "Subway"
    if d.startswith("Service at ") and d.endswith(" platform"):
        return d[len("Service at "):-len(" platform")]
    if d.startswith("Service toward "):
        return None
    return d


# ---- schedule ----------------------------------------------------------------------
@dataclass
class CtaSchedule:
    version: str
    lines: dict[str, dict]
    shapes: dict[str, list[list[list[float]]]]
    stations: dict[str, dict]
    # station -> group key -> service id -> [(seconds after service-day midnight, headsign)]
    deps: dict[str, dict[str, dict[str, list[tuple[int, str]]]]]
    # (station, group key) -> destination station -> typical minutes
    reach: dict[tuple[str, str], dict[str, int]]
    calendar: dict[str, tuple[date, date, tuple[bool, ...]]]
    exceptions: dict[date, dict[str, int]]
    schema: int = SCHEMA
    _active: dict = field(default_factory=dict, repr=False, compare=False)

    def active_services(self, d: date) -> frozenset:
        hit = self._active.get(d)
        if hit is None:
            ids = {sid for sid, (start, end, days) in self.calendar.items() if start <= d <= end and days[d.weekday()]}
            for sid, kind in self.exceptions.get(d, {}).items():
                (ids.add if kind == 1 else ids.discard)(sid)
            hit = self._active[d] = frozenset(ids)
        return hit

    def has_service(self, start: date, days: int = 7) -> bool:
        return any(self.active_services(start + timedelta(days=i)) for i in range(days))

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_active"] = {}
        return state


def parse_cta_zip(path: Path, version: str) -> CtaSchedule:
    """Read the L (route_type 1) out of CTA's schedule zip. Buses are skipped."""
    with zipfile.ZipFile(path) as zf:
        def rows(name: str):
            with zf.open(name) as fh:
                yield from csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig"))

        lines = {}
        for r in rows("routes.txt"):
            if r.get("route_type", "").strip() == "1":
                rid = r["route_id"].strip()
                lines[rid] = {
                    "id": rid, "short_name": SHORT_NAMES.get(rid, rid),
                    "name": r.get("route_long_name", "").strip() or rid,
                    "color": "#" + (r.get("route_color", "").strip() or "565a5c"),
                    "text_color": "#" + (r.get("route_text_color", "").strip() or "ffffff"),
                }
        trips = {}
        for r in rows("trips.txt"):
            rid = r["route_id"].strip()
            if rid in lines:
                trips[r["trip_id"].strip()] = (rid, r["service_id"].strip(), r.get("shape_id", "").strip(),
                                               DIR_CODES.get(r.get("direction", "").strip()))
        stops = {}
        for r in rows("stops.txt"):
            stops[r["stop_id"].strip()] = r
        calendar = {}
        for r in rows("calendar.txt"):
            days = tuple(r[d].strip() == "1" for d in
                         ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))
            calendar[r["service_id"].strip()] = (_ymd(r["start_date"]), _ymd(r["end_date"]), days)
        exceptions: dict[date, dict[str, int]] = defaultdict(dict)
        if "calendar_dates.txt" in zf.namelist():
            for r in rows("calendar_dates.txt"):
                exceptions[_ymd(r["date"])][r["service_id"].strip()] = int(r["exception_type"])

        # Every L stop time, per trip.
        by_trip: dict[str, list] = defaultdict(list)
        for r in rows("stop_times.txt"):
            tid = r["trip_id"].strip()
            if tid not in trips:
                continue
            stop = stops.get(r["stop_id"].strip())
            if stop is None:
                continue
            station = (stop.get("parent_station") or "").strip() or r["stop_id"].strip()
            secs = _secs(r.get("departure_time") or r.get("arrival_time") or "")
            if secs is None:
                continue
            by_trip[tid].append((int(r["stop_sequence"]), station, secs, (r.get("stop_headsign") or "").strip(),
                                 (r.get("pickup_type") or "").strip() == "1"))

        deps: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        station_routes: dict[str, set] = defaultdict(set)
        travel: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
        shape_trips: dict[str, set] = defaultdict(set)
        for tid, sts in by_trip.items():
            rid, service, shape, dcode = trips[tid]
            if shape:
                shape_trips[rid].add(shape)
            sts.sort()
            for i, (_, station, secs, headsign, no_pickup) in enumerate(sts):
                station_routes[station].add(rid)
                if not headsign or no_pickup or i == len(sts) - 1:
                    continue  # last stop or no boarding: not a departure
                gk = group_key(rid, dcode, headsign)
                deps[station][gk][service].append((secs, headsign))
                # Stations this train still reaches, until it comes back to one
                # it already passed (round trips through the Loop).
                seen = {station}
                for _, other, osecs, _, _ in sts[i + 1:]:
                    if other in seen:
                        break
                    seen.add(other)
                    acc = travel[(station, gk, other)]
                    acc[0] += osecs - secs
                    acc[1] += 1

        reach: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
        for (station, gk, other), (total, n) in travel.items():
            reach[(station, gk)][other] = round(total / n / 60)

        stations = {}
        for sid, routes in station_routes.items():
            s = stops.get(sid)
            if s is None:
                continue
            stations[sid] = {
                "id": sid, "name": s["stop_name"].strip(),
                "lat": float(s["stop_lat"]), "lon": float(s["stop_lon"]),
                "routes": [r for r in LINE_ORDER if r in routes] + sorted(routes - set(LINE_ORDER)),
            }

        wanted = set().union(*shape_trips.values()) if shape_trips else set()
        points: dict[str, list] = defaultdict(list)
        if wanted:
            for r in rows("shapes.txt"):
                sid = r["shape_id"].strip()
                if sid in wanted:
                    points[sid].append((int(r["shape_pt_sequence"]), round(float(r["shape_pt_lon"]), 5),
                                        round(float(r["shape_pt_lat"]), 5)))

    shapes = {}
    for rid, ids in shape_trips.items():
        # The longest few shapes per line cover its branches; shorter ones are
        # short-turn trips drawn on top of them anyway.
        best = sorted(ids, key=lambda s: -len(points[s]))[:4]
        drawn = []
        for sid in best:
            coords = []
            for _, lon, lat in sorted(points[sid]):
                if not coords or coords[-1] != [lon, lat]:
                    coords.append([lon, lat])
            if len(coords) >= 2:
                drawn.append(coords)
        shapes[rid] = drawn

    for station in deps.values():
        for groups in station.values():
            for times in groups.values():
                times.sort()
    return CtaSchedule(
        version=version, lines=lines, shapes=shapes, stations=stations,
        deps={s: {g: dict(v) for g, v in gs.items()} for s, gs in deps.items()},
        reach=dict(reach), calendar=calendar, exceptions=dict(exceptions),
    )


class CtaCache:
    """/config/cta: the parsed schedule (pickle) and what CTA's server said about it."""

    def __init__(self, config_dir: Path):
        self.dir = config_dir / "cta"
        self.pickle = self.dir / "schedule.pickle"
        self.meta_path = self.dir / "meta.json"

    def meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text())
        except (OSError, ValueError):
            return {}

    def write_meta(self, meta: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.meta_path, json.dumps(meta, indent=2).encode())

    def load(self) -> CtaSchedule | None:
        try:
            data = pickle.loads(self.pickle.read_bytes())
        except FileNotFoundError:
            return None
        except Exception as exc:
            log.warning("CTA schedule cache unreadable (%s); downloading again", exc)
            return None
        if not isinstance(data, CtaSchedule) or getattr(data, "schema", None) != SCHEMA:
            return None
        return data

    def save(self, data: CtaSchedule) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.pickle, pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))


def _atomic_write(path: Path, content: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


class TrainTrackerError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


# ---- tracker -----------------------------------------------------------------------
class CtaTracker:
    def __init__(self, config_dir: Path, api_key: str | None, static_url: str,
                 check_hours: int = 24, enabled: bool = True):
        self.api_key = api_key
        self.static_url = static_url
        self.check_hours = check_hours
        self.enabled = enabled
        self.cache = CtaCache(config_dir)
        self.schedule: CtaSchedule | None = None
        self.static_error: str | None = None
        self.live_error: str | None = None
        self.last_live: float | None = None
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task] = []
        self._cache: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._budget_day: date | None = None
        self.requests_today = 0

    @property
    def live_enabled(self) -> bool:
        return bool(self.api_key)

    # ---- lifecycle -------------------------------------------------------------------
    async def start(self, client: httpx.AsyncClient | None = None) -> None:
        if not self.enabled:
            log.info("CTA is turned off (CTA_ENABLED=false)")
            return
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0), headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self.schedule = await asyncio.to_thread(self.cache.load)
        if not self.live_enabled:
            log.warning("CTA_TRAIN_TRACKER_KEY not set: CTA boards use the schedule only")
        self._tasks.append(asyncio.create_task(self._static_loop(), name="cta-static"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client:
            await self._client.aclose()

    async def _static_loop(self) -> None:
        await asyncio.sleep(2)  # let Metra's schedule go first on a fresh start
        while True:
            try:
                await self.refresh_static()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("CTA schedule refresh failed")
                self.static_error = str(exc) or exc.__class__.__name__
            await asyncio.sleep(self.check_hours * 3600 if self.schedule else 600)

    async def refresh_static(self, force: bool = False) -> bool:
        """Download CTA's schedule if it changed (asks with ETag/Last-Modified, so an
        unchanged file costs one small request). Returns True if a new one is in use."""
        assert self._client
        meta = self.cache.meta()
        headers = {}
        if self.schedule and not force:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
        r = await self._client.get(self.static_url, headers=headers)
        meta["checked_at"] = time.time()
        if r.status_code == 304:
            self.cache.write_meta(meta)
            self.static_error = None
            return False
        r.raise_for_status()
        version = r.headers.get("last-modified") or r.headers.get("etag") or f"downloaded {datetime.now(TZ):%Y-%m-%d}"
        tmp = self.cache.dir / "download.zip"
        self.cache.dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(tmp.write_bytes, r.content)
        try:
            data = await asyncio.to_thread(parse_cta_zip, tmp, version)
            problem = None if data.stations and data.has_service(datetime.now(TZ).date()) else "no L trains in the coming week"
        except Exception as exc:
            problem = f"it couldn't be read ({exc})"
        finally:
            tmp.unlink(missing_ok=True)
        if problem:
            msg = f"Downloaded CTA schedule not used: {problem}"
            if self.schedule:
                msg += f"; keeping {self.schedule.version}"
            log.warning(msg)
            self.static_error = msg
            return False
        await asyncio.to_thread(self.cache.save, data)
        meta.update(etag=r.headers.get("etag"), last_modified=r.headers.get("last-modified"),
                    version=version, downloaded_at=time.time())
        self.cache.write_meta(meta)
        log.info("Now using CTA schedule %s (%d stations)", version, len(data.stations))
        self.schedule = data
        self.static_error = None
        return True

    # ---- Train Tracker -----------------------------------------------------------------
    def _spend(self) -> bool:
        today = datetime.now(TZ).date()
        if self._budget_day != today:
            self._budget_day, self.requests_today = today, 0
        if self.requests_today >= DAILY_BUDGET:
            return False
        self.requests_today += 1
        return True

    async def _call(self, endpoint: str, params: dict) -> dict:
        """One Train Tracker request. Raises TrainTrackerError (never shows the key)."""
        if not self.api_key or not self._client:
            raise TrainTrackerError("no key")
        if not self._spend():
            raise TrainTrackerError("daily request budget used up")
        try:
            r = await self._client.get(f"{API_BASE}/{endpoint}",
                                       params={**params, "outputType": "JSON", "key": self.api_key})
            r.raise_for_status()
            body = r.json().get("ctatt") or {}
        except (httpx.HTTPError, ValueError) as exc:
            # httpx errors can include the URL, and the URL holds the key.
            raise TrainTrackerError(exc.__class__.__name__ + (f" {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else ""))
        if str(body.get("errCd", "0")) != "0":
            code = int(body["errCd"]) if str(body.get("errCd")).isdigit() else None
            raise TrainTrackerError(f"CTA error {body.get('errCd')}: {body.get('errNm') or 'unknown'}", code)
        self.last_live = time.time()
        self.live_error = None
        return body

    async def _cached(self, key: str, ttl: float, fetch):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        async with self._locks[key]:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            value = await fetch()
            self._cache[key] = (time.monotonic(), value)
            if len(self._cache) > 500:  # forget old stations/trains
                for k, (t, _) in list(self._cache.items()):
                    if time.monotonic() - t > 600:
                        self._cache.pop(k, None)
            return value

    async def _live_arrivals(self, station: str) -> dict:
        return await self._cached(f"arr:{station}", ARRIVALS_TTL,
                                  lambda: self._call("ttarrivals.aspx", {"mapid": station}))

    # ---- queries -------------------------------------------------------------------------
    def _need_schedule(self) -> CtaSchedule:
        if self.schedule is None:
            raise LookupError("CTA schedule not loaded yet")
        return self.schedule

    def station(self, station_id: str) -> dict:
        s = self._need_schedule().stations.get(station_id)
        if s is None:
            raise KeyError(station_id)
        return s

    def stations(self) -> list[dict]:
        return sorted(self._need_schedule().stations.values(), key=lambda s: s["name"])

    def lines(self) -> list[dict]:
        sch = self._need_schedule()
        return [sch.lines[r] for r in LINE_ORDER if r in sch.lines]

    def lines_geojson(self) -> dict:
        sch = self._need_schedule()
        feats = []
        for rid in LINE_ORDER:
            line = sch.lines.get(rid)
            for coords in sch.shapes.get(rid, []) if line else []:
                feats.append({"type": "Feature",
                              "properties": {"route_id": rid, "short_name": line["short_name"], "color": line["color"]},
                              "geometry": {"type": "LineString", "coordinates": coords}})
        return {"type": "FeatureCollection", "features": feats}

    def _line_info(self, rid: str) -> dict:
        line = self._need_schedule().lines.get(rid) or {}
        return {"route_id": rid, "route_short": SHORT_NAMES.get(rid, rid), "route_name": line.get("name", rid),
                "color": line.get("color", "#565a5c"), "text_color": line.get("text_color", "#ffffff")}

    def _service_days(self, now: float):
        """(service-day midnight, active services) for yesterday and today: trips
        after midnight belong to the previous service day."""
        sch = self._need_schedule()
        today = datetime.fromtimestamp(now, TZ).date()
        for d in (today - timedelta(days=1), today):
            midnight = datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()
            yield d, midnight, sch.active_services(d)

    def _scheduled(self, station: str, now: float, until: float) -> dict[str, list[tuple[float, str]]]:
        """Scheduled departures per group between now and until."""
        out: dict[str, list] = defaultdict(list)
        groups = self._need_schedule().deps.get(station, {})
        for _, midnight, services in self._service_days(now):
            for gk, by_service in groups.items():
                for sid in services & by_service.keys():
                    for secs, headsign in by_service[sid]:
                        t = midnight + secs
                        if now - 60 <= t <= until:
                            out[gk].append((t, headsign))
        for v in out.values():
            v.sort()
        return out

    def _reaches(self, station: str, gk: str, to: str | None) -> int | None | bool:
        """Typical minutes from station to `to` on this group's trains; False if they don't go there."""
        if not to:
            return None
        mins = self._need_schedule().reach.get((station, gk), {}).get(to)
        return False if mins is None else mins

    async def arrivals(self, station: str, to: str | None = None, per_group: int = 4,
                       now: float | None = None) -> dict:
        """Next trains at a station, one group per line and direction.

        Live from Train Tracker when there's a key; otherwise (or if CTA fails)
        from the schedule. `to`: only trains that go on to that station, with
        an estimated arrival there.
        """
        now = time.time() if now is None else now
        info = self.station(station)
        if to is not None:
            self.station(to)
        groups: dict[str, dict] = {}
        source, error, as_of = "schedule", None, None

        def group(gk: str, rid: str) -> dict | None:
            if gk not in groups:
                mins = self._reaches(station, gk, to)
                if mins is False:
                    return None
                groups[gk] = {"key": gk, **self._line_info(rid), "platform": None, "destinations": [],
                              "travel_minutes": mins, "trains": []}
            return groups[gk]

        if self.live_enabled:
            try:
                body = await self._live_arrivals(station)
                source, as_of = "live", parse_cta_time(body.get("tmst"))
                for e in body.get("eta") or []:
                    rid = route_id(e.get("rt"))
                    t = parse_cta_time(e.get("arrT"))
                    if not rid or t is None:
                        continue
                    g = group(group_key(rid, e.get("trDr"), e.get("destNm")), rid)
                    if g is None:
                        continue
                    g["platform"] = g["platform"] or _platform(e.get("stpDe"))
                    g["trains"].append({
                        "run": e.get("rn"), "destination": e.get("destNm"), "time": t,
                        "live": e.get("isSch") != "1", "scheduled": e.get("isSch") == "1",
                        "due": e.get("isApp") == "1", "delayed": e.get("isDly") == "1",
                        "fault": e.get("isFlt") == "1",
                    })
            except TrainTrackerError as exc:
                error = self.live_error = str(exc)
                log.warning("CTA arrivals for %s failed: %s", station, error)
                groups.clear()
        if source == "schedule":
            for gk, deps in self._scheduled(station, now, now + 3 * 3600).items():
                rid = gk.split(":", 1)[0]
                g = group(gk, rid)
                if g is None:
                    continue
                for t, headsign in deps:
                    if t >= now:
                        g["trains"].append({"run": None, "destination": headsign, "time": t, "live": False,
                                            "scheduled": True, "due": False, "delayed": False, "fault": False})

        out = []
        for g in groups.values():
            g["trains"].sort(key=lambda x: x["time"])
            del g["trains"][per_group:]
            if not g["trains"]:
                continue
            counts: dict[str, int] = defaultdict(int)
            for x in g["trains"]:
                x["minutes"] = max(0, round((x["time"] - now) / 60))
                if g["travel_minutes"] is not None:
                    x["arrives"] = x["time"] + g["travel_minutes"] * 60
                if x["destination"]:
                    counts[x["destination"]] += 1
            # Stable order (doesn't flip as trains come and go): most trains first, then A-Z.
            g["destinations"] = sorted(counts, key=lambda h: (-counts[h], h))
            g["label"] = " · ".join(g["destinations"])
            out.append(g)
        out.sort(key=lambda g: g["trains"][0]["time"])
        return {
            "system": "cta", "station": info, "to": self.station(to) if to else None,
            "source": source, "error": error, "as_of": as_of or now, "server_time": now,
            "groups": out, "credit": CREDIT,
        }

    def timetable(self, station: str, now: float | None = None) -> dict:
        """How often trains come today, per line and direction, plus first and last
        train: L trains run every few minutes, so this replaces a full list."""
        now = time.time() if now is None else now
        info = self.station(station)
        sch = self._need_schedule()
        today = datetime.fromtimestamp(now, TZ).date()
        midnight = datetime(today.year, today.month, today.day, tzinfo=TZ).timestamp()
        # Before 3 AM the previous service day is still running.
        if now - midnight < 3 * 3600:
            today -= timedelta(days=1)
            midnight = datetime(today.year, today.month, today.day, tzinfo=TZ).timestamp()
        services = sch.active_services(today)
        now_min = (now - midnight) / 60
        out = []
        for gk, by_service in sch.deps.get(station, {}).items():
            deps = sorted(d for sid in services & by_service.keys() for d in by_service[sid])
            if len(deps) < 2:
                continue
            mins = [secs / 60 for secs, _ in deps]
            gaps = [(a, b - a) for a, b in zip(mins, mins[1:])]
            periods = []
            for key, label, start, end in PERIODS:
                g = [round(gap) for at, gap in gaps if start <= at < end and gap <= NO_SERVICE_GAP]
                periods.append({"key": key, "label": label, "min": min(g) if g else None,
                                "max": max(g) if g else None, "now": start <= now_min < end})
            rid = gk.split(":", 1)[0]
            # Most frequent destination first; short trips that run a few times
            # a day (under 10% of trains) are left out of the label.
            counts: dict[str, int] = defaultdict(int)
            for _, h in deps:
                counts[h] += 1
            headsigns = [h for h in sorted(counts, key=lambda h: (-counts[h], h)) if counts[h] * 10 >= len(deps)]
            all_night = mins[0] < 60 and mins[-1] >= 24 * 60 and max(gap for _, gap in gaps) <= 60
            upcoming = [m for m in mins if m >= now_min]
            out.append({
                "key": gk, **self._line_info(rid), "label": " · ".join(headsigns),
                "first": midnight + deps[0][0], "last": midnight + deps[-1][0],
                "all_night": all_night, "trains_left": len(upcoming), "count": len(deps),
                "periods": periods,
            })
        order = {r: i for i, r in enumerate(LINE_ORDER)}
        out.sort(key=lambda g: (order.get(g["route_id"], 99), g["label"]))
        return {"system": "cta", "station": info, "service_date": today.isoformat(), "groups": out,
                "server_time": now, "credit": CREDIT}

    async def trains(self) -> dict:
        """Every L train's position (for the map)."""
        if not self.live_enabled:
            return {"system": "cta", "source": "none", "trains": [], "error": "no key", "credit": CREDIT}
        try:
            body = await self._cached("positions", ARRIVALS_TTL,
                                      lambda: self._call("ttpositions.aspx", {"rt": POSITION_ROUTES}))
        except TrainTrackerError as exc:
            self.live_error = str(exc)
            return {"system": "cta", "source": "none", "trains": [], "error": str(exc), "credit": CREDIT}
        out = []
        for route in body.get("route") or []:
            rid = route_id(route.get("@name"))
            if not rid:
                continue
            raw = route.get("train") or []
            for t in raw if isinstance(raw, list) else [raw]:  # one train comes as an object
                try:
                    lat, lon = float(t["lat"]), float(t["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not lat or not lon:
                    continue
                heading = t.get("heading")
                out.append({
                    **self._line_info(rid), "run": t.get("rn"), "destination": t.get("destNm"),
                    "next_station": t.get("nextStaNm"), "next_station_id": t.get("nextStaId"),
                    "next_arrival": parse_cta_time(t.get("arrT")), "due": t.get("isApp") == "1",
                    "delayed": t.get("isDly") == "1", "lat": lat, "lon": lon,
                    "bearing": int(heading) if str(heading or "").isdigit() else None,
                })
        return {"system": "cta", "source": "live", "as_of": parse_cta_time(body.get("tmst")),
                "trains": out, "credit": CREDIT}

    async def follow(self, run: str) -> dict:
        """One train's remaining stops (for the double-tap sheet)."""
        run = run.strip()
        if not run.isdigit():
            raise KeyError(run)
        try:
            body = await self._cached(f"follow:{run}", FOLLOW_TTL,
                                      lambda: self._call("ttfollow.aspx", {"runnumber": run}))
        except TrainTrackerError as exc:
            if exc.code is not None and 500 <= exc.code < 600:  # 5xx: no such train right now
                raise KeyError(run)
            raise
        etas = body.get("eta") or []
        etas = etas if isinstance(etas, list) else [etas]
        pos = body.get("position") or {}
        rid = route_id(etas[0].get("rt")) if etas else None
        stops = [{"station_id": e.get("staId"), "name": e.get("staNm"), "time": parse_cta_time(e.get("arrT")),
                  "due": e.get("isApp") == "1", "delayed": e.get("isDly") == "1", "scheduled": e.get("isSch") == "1"}
                 for e in etas]
        try:
            where = {"lat": float(pos["lat"]), "lon": float(pos["lon"]),
                     "bearing": int(pos["heading"]) if str(pos.get("heading", "")).isdigit() else None}
        except (KeyError, TypeError, ValueError):
            where = None
        return {"system": "cta", "run": run, **(self._line_info(rid) if rid else {}),
                "destination": etas[0].get("destNm") if etas else None,
                "position": where, "stops": stops, "as_of": parse_cta_time(body.get("tmst")), "credit": CREDIT}

    async def alerts(self) -> list[dict]:
        """CTA's current alerts for L lines and stations (no key needed)."""
        async def fetch():
            assert self._client
            r = await self._client.get(ALERTS_URL, params={"outputType": "JSON", "activeonly": "true"})
            r.raise_for_status()
            return (r.json().get("CTAAlerts") or {}).get("Alert") or []

        try:
            raw = await self._cached("alerts", ALERTS_TTL, fetch)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("CTA alerts failed: %s", exc.__class__.__name__)
            return []
        out = []
        for a in raw if isinstance(raw, list) else [raw]:
            services = (a.get("ImpactedService") or {}).get("Service") or []
            services = services if isinstance(services, list) else [services]
            routes = [route_id(s.get("ServiceId")) for s in services if s.get("ServiceType") == "R"]
            stations = [s.get("ServiceId") for s in services if s.get("ServiceType") == "T"]
            routes = [r for r in routes if r]
            if not routes and not stations:
                continue  # bus-only alert
            url = a.get("AlertURL")
            out.append({
                "id": a.get("AlertId"), "headline": a.get("Headline"), "description": a.get("ShortDescription"),
                "impact": a.get("Impact"), "severity": int(a.get("SeverityScore") or 0),
                "major": a.get("MajorAlert") == "1", "routes": routes, "stations": stations,
                "start": parse_cta_time(a.get("EventStart")), "end": parse_cta_time(a.get("EventEnd")),
                "url": url.get("#cdata-section") if isinstance(url, dict) else url,
            })
        out.sort(key=lambda a: -a["severity"])
        return out

    def status(self) -> dict:
        meta = self.cache.meta()
        return {
            "enabled": self.enabled, "live": self.live_enabled,
            "schedule_loaded": self.schedule is not None,
            "schedule_version": self.schedule.version if self.schedule else None,
            "schedule_checked_at": meta.get("checked_at"), "schedule_error": self.static_error,
            "last_live": self.last_live, "live_error": self.live_error,
            "requests_today": self.requests_today, "daily_budget": DAILY_BUDGET,
            "credit": CREDIT,
        }
