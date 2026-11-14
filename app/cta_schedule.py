"""CTA 'L' schedule: the line tables, reading CTA's schedule zip, and the
cached copy under /config/cta. Nothing here talks to the network."""

from __future__ import annotations

import csv
import io
import json
import logging
import pickle
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from .util import atomic_write, load_cache_file

log = logging.getLogger(__name__)

TZ = ZoneInfo("America/Chicago")
CREDIT = "Data provided by CTA. Not affiliated with or endorsed by CTA."

# Bump when the pickled schedule's layout changes: an old pickle is then
# ignored and the schedule downloaded again.
SCHEMA = 2

SERVICE_DAY_START = 3 * 3600  # trains before 3 AM belong to the previous day's service

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


@lru_cache(maxsize=20000)
def _local_hour(hours_since_epoch: int) -> tuple[str, int]:
    """(service date, hour of day) in Chicago for an hour of clock time."""
    ts = hours_since_epoch * 3600
    return (datetime.fromtimestamp(ts - SERVICE_DAY_START, TZ).date().isoformat(),
            datetime.fromtimestamp(ts, TZ).hour)


def service_date(ts: float) -> str:
    """The service day a moment belongs to (YYYY-MM-DD; the day starts at 3 AM).
    Chicago's clock only changes on the hour, so one answer per hour is exact."""
    return _local_hour(int(ts // 3600))[0]


def local_hour(ts: float) -> int:
    return _local_hour(int(ts // 3600))[1]


def service_day_span(day: str) -> tuple[float, float]:
    """(start, end) in epoch seconds of a service day given as YYYY-MM-DD."""
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, tzinfo=TZ) + timedelta(seconds=SERVICE_DAY_START)
    n = d + timedelta(days=1)
    end = datetime(n.year, n.month, n.day, tzinfo=TZ) + timedelta(seconds=SERVICE_DAY_START)
    return start.timestamp(), end.timestamp()


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
    # group key -> the station orders its trains run (one per branch/pattern)
    sequences: dict[str, list[list[str]]]
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
        patterns: dict[str, set] = defaultdict(set)
        for tid, sts in by_trip.items():
            rid, service, shape, dcode = trips[tid]
            if shape:
                shape_trips[rid].add(shape)
            sts.sort()
            legs: dict[str, list[str]] = defaultdict(list)
            # Some stops leave the sign blank mid-trip (e.g. King Drive on
            # Cottage Grove trains): carry the last one forward.
            carried = next((h for _, _, _, h, _ in sts if h), "")
            for i, (_, station, secs, headsign, no_pickup) in enumerate(sts):
                station_routes[station].add(rid)
                headsign = carried = headsign or carried
                if headsign:
                    # Every stop the train makes under this sign, including the
                    # last one and drop-off-only stops (for placing a train).
                    gk = group_key(rid, dcode, headsign)
                    if station not in legs[gk]:
                        legs[gk].append(station)
                if not headsign or no_pickup or i == len(sts) - 1:
                    continue  # last stop or no boarding: not a departure
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

            for gk, leg in legs.items():
                patterns[gk].add(tuple(leg))

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
            coords: list[list[float]] = []
            for _, lon, lat in sorted(points[sid]):
                if not coords or coords[-1] != [lon, lat]:
                    coords.append([lon, lat])
            if len(coords) >= 2:
                drawn.append(coords)
        shapes[rid] = drawn

    sequences: dict[str, list[list[str]]] = {}
    for gk, pats in patterns.items():
        # Drop patterns that are just a part of a longer one (short trips).
        def inside(a: tuple, b: tuple) -> bool:
            return len(a) < len(b) and any(b[i:i + len(a)] == a for i in range(len(b) - len(a) + 1))
        full = [p for p in pats if not any(inside(p, q) for q in pats)]
        sequences[gk] = [list(p) for p in sorted(full, key=len, reverse=True)[:6]]

    for station in deps.values():
        for groups in station.values():
            for times in groups.values():
                times.sort()
    return CtaSchedule(
        version=version, lines=lines, shapes=shapes, stations=stations,
        deps={s: {g: dict(v) for g, v in gs.items()} for s, gs in deps.items()},
        reach=dict(reach), sequences=sequences, calendar=calendar, exceptions=dict(exceptions),
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
        atomic_write(self.meta_path, json.dumps(meta, indent=2).encode())

    def load(self) -> CtaSchedule | None:
        try:
            # Only a schedule can come out of this file: a tampered cache can't run code.
            # (Files saved before this module was split name the class app.cta.CtaSchedule.)
            data = load_cache_file(self.pickle, [CtaSchedule], extra={("app.cta", "CtaSchedule")})
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
        atomic_write(self.pickle, pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))
