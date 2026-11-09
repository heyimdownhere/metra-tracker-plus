"""Static GTFS loading, parsing and on-disk caching.

Nothing here assumes a particular format for trip, service, shape or stop IDs:
they are treated as opaque strings, because Metra is changing their format.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import math
import os
import pickle
import time
import zipfile
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

CACHE_VERSION = 4  # bump when StaticData's fields change

# Trip tuple field indexes.
T_ROUTE, T_SERVICE, T_HEADSIGN, T_SHAPE, T_DIRECTION, T_SHORT_NAME = range(6)
# Trip-stop tuple field indexes.
S_SEQ, S_STOP, S_ARR, S_DEP, S_PICKUP, S_DROPOFF = range(6)


def _parse_time(value: str) -> int | None:
    """GTFS HH:MM:SS (hours may exceed 23) -> seconds after service-day start."""
    value = value.strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def _parse_date(value: str) -> date | None:
    value = value.strip()
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _read_csv(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    """Read a GTFS table, tolerating whitespace around headers and values."""
    names = {Path(n).name: n for n in zf.namelist()}
    if name not in names:
        return []
    with zf.open(names[name]) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = csv.reader(text, skipinitialspace=True)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            return []
        rows = []
        for row in reader:
            if not row or all(not c.strip() for c in row):
                continue
            rows.append({header[i]: row[i].strip() for i in range(min(len(header), len(row)))})
        return rows


# Abbreviations whose trailing period is part of the name ("Main St.").
_ABBREVIATIONS = {"st", "ave", "av", "rd", "blvd", "pk", "jct", "hts", "mt", "ft", "sq", "ctr", "dr", "ln", "pl", "n", "s", "e", "w"}


def clean_name(name: str) -> str:
    """Tidy a stop name for display: 'Lake Forest.' -> 'Lake Forest', keep 'Main St.'."""
    name = " ".join(name.split())
    if name.endswith(".") and not name.endswith(".."):
        last = name[:-1].rsplit(" ", 1)[-1].strip("(").lower()
        if last not in _ABBREVIATIONS and len(last) > 2:
            name = name[:-1]
    return name


def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Fast equirectangular distance in metres (fine at city scale)."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371000.0 * math.hypot(x, y)


@dataclass
class StaticData:
    version: str  # value of published.txt (or a hash of the zip)
    loaded_at: float
    timezone: str
    agency_name: str
    routes: dict[str, dict]
    stops: dict[str, dict]
    trips: dict[str, tuple]
    trip_stops: dict[str, list[tuple]]
    stop_departures: dict[str, list[tuple[int, str, int]]]  # stop -> [(dep, trip, seq)]
    calendar: dict[str, tuple]  # service -> (weekday flags, start, end)
    calendar_dates: dict[date, dict[str, int]]
    shapes: dict[str, list[tuple[float, float]]]
    shape_dists: dict[str, list[float]]
    trip_spans: dict[str, tuple[int, int]]  # trip -> (first departure, last arrival)
    start_index: dict[tuple[str, int], list[str]]  # (route, first dep) -> trips
    station_children: dict[str, list[str]]  # station id -> stop ids with service
    _services_cache: dict[date, frozenset] = field(default_factory=dict, repr=False)
    _stop_dist_cache: dict[tuple, list[float] | None] = field(default_factory=dict, repr=False)
    _trip_index_cache: dict[str, tuple] = field(default_factory=dict, repr=False)

    def trip_index(self, trip_id: str) -> tuple[dict[str, int], dict[int, tuple]]:
        """(stop_id -> stop_sequence, stop_sequence -> stop tuple) for a trip, cached."""
        idx = self._trip_index_cache.get(trip_id)
        if idx is None:
            sts = self.trip_stops.get(trip_id, [])
            idx = ({x[S_STOP]: x[S_SEQ] for x in sts}, {x[S_SEQ]: x for x in sts})
            self._trip_index_cache[trip_id] = idx
        return idx

    # ---- time helpers -------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def service_day_start(self, d: date) -> float:
        """Epoch seconds of 'noon minus 12h' on service date d (GTFS definition)."""
        noon = datetime(d.year, d.month, d.day, 12, tzinfo=self.tz)
        return noon.timestamp() - 12 * 3600

    def local_date(self, ts: float) -> date:
        return datetime.fromtimestamp(ts, self.tz).date()

    def active_services(self, d: date) -> frozenset:
        cached = self._services_cache.get(d)
        if cached is not None:
            return cached
        weekday = d.weekday()
        active = {
            sid
            for sid, (days, start, end) in self.calendar.items()
            if days[weekday] and start <= d <= end
        }
        for sid, exception in self.calendar_dates.get(d, {}).items():
            if exception == 1:
                active.add(sid)
            elif exception == 2:
                active.discard(sid)
        result = frozenset(active)
        self._services_cache[d] = result
        return result

    def candidate_service_dates(self, now: float) -> list[date]:
        today = self.local_date(now)
        return [today - timedelta(days=1), today, today + timedelta(days=1)]

    def trip_service_date(self, trip_id: str, now: float) -> date | None:
        """Best guess of which service date a trip is running on right now."""
        trip = self.trips.get(trip_id)
        span = self.trip_spans.get(trip_id)
        if not trip or not span:
            return None
        best, best_gap = None, None
        for d in self.candidate_service_dates(now):
            if trip[T_SERVICE] not in self.active_services(d):
                continue
            base = self.service_day_start(d)
            start, end = base + span[0], base + span[1]
            gap = 0 if start - 3600 <= now <= end + 3 * 3600 else min(abs(now - start), abs(now - end))
            if best_gap is None or gap < best_gap:
                best, best_gap = d, gap
        return best

    # ---- lookups -------------------------------------------------------
    def stop_name(self, stop_id: str | None) -> str | None:
        if not stop_id:
            return None
        stop = self.stops.get(stop_id)
        return stop["name"] if stop else stop_id

    def trip_destination(self, trip_id: str) -> str | None:
        trip = self.trips.get(trip_id)
        if trip and trip[T_HEADSIGN]:
            return trip[T_HEADSIGN]
        stops = self.trip_stops.get(trip_id)
        return self.stop_name(stops[-1][S_STOP]) if stops else None

    def resolve_trip(
        self,
        trip_id: str | None,
        route_id: str | None = None,
        start_time: str | None = None,
        start_date: str | None = None,
        direction_id: int | None = None,
    ) -> str | None:
        """Map a realtime TripDescriptor onto a static trip_id.

        First try the trip_id verbatim. If realtime IDs stop matching the static
        feed (Metra is changing ID formats), fall back to matching on route +
        scheduled start time + active service, which is ID-format independent.
        """
        if trip_id and trip_id in self.trips:
            return trip_id
        start = _parse_time(start_time or "")
        if route_id is None or start is None:
            return None
        candidates = self.start_index.get((route_id, start), [])
        d = _parse_date(start_date or "")
        if d is not None:
            services = self.active_services(d)
            candidates = [t for t in candidates if self.trips[t][T_SERVICE] in services]
        if direction_id is not None:
            filtered = [t for t in candidates if self.trips[t][T_DIRECTION] == direction_id]
            candidates = filtered or candidates
        return candidates[0] if candidates else None

    # ---- geometry ------------------------------------------------------
    def _trip_stop_distances(self, trip_id: str) -> list[float] | None:
        """Distance along the trip's shape of each of its stops (monotonic)."""
        trip = self.trips[trip_id]
        stops = self.trip_stops.get(trip_id) or []
        key = (trip[T_SHAPE], tuple(s[S_STOP] for s in stops))
        if key in self._stop_dist_cache:
            return self._stop_dist_cache[key]
        pts = self.shapes.get(trip[T_SHAPE] or "")
        cum = self.shape_dists.get(trip[T_SHAPE] or "")
        result: list[float] | None = None
        if pts and cum and len(pts) > 1:
            result = []
            idx = 0
            for s in stops:
                stop = self.stops.get(s[S_STOP])
                if not stop:
                    result = None
                    break
                best_i, best_d = idx, float("inf")
                for i in range(idx, len(pts)):
                    dd = _dist_m(stop["lat"], stop["lon"], pts[i][0], pts[i][1])
                    if dd < best_d:
                        best_i, best_d = i, dd
                if best_d > 1500:  # shape doesn't really pass this stop
                    result = None
                    break
                idx = best_i
                result.append(cum[best_i])
        self._stop_dist_cache[key] = result
        return result

    def point_on_trip(self, trip_id: str, seg: int, frac: float) -> tuple[float, float, float | None]:
        """Position between stop `seg` and `seg+1` of a trip; returns lat, lon, bearing."""
        stops = self.trip_stops[trip_id]
        a = self.stops.get(stops[seg][S_STOP])
        b = self.stops.get(stops[min(seg + 1, len(stops) - 1)][S_STOP])
        dists = self._trip_stop_distances(trip_id)
        shape_id = self.trips[trip_id][T_SHAPE]
        if dists:
            pts, cum = self.shapes[shape_id], self.shape_dists[shape_id]
            target = dists[seg] + (dists[min(seg + 1, len(dists) - 1)] - dists[seg]) * frac
            i = max(1, min(bisect_left(cum, target), len(cum) - 1))
            span = cum[i] - cum[i - 1]
            f = 0.0 if span <= 0 else (target - cum[i - 1]) / span
            lat = pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * f
            lon = pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * f
            return lat, lon, _bearing(pts[i - 1], pts[i])
        if not a or not b:
            stop = a or b
            return stop["lat"], stop["lon"], None
        lat = a["lat"] + (b["lat"] - a["lat"]) * frac
        lon = a["lon"] + (b["lon"] - a["lon"]) * frac
        return lat, lon, _bearing((a["lat"], a["lon"]), (b["lat"], b["lon"]))


def _bearing(p1: tuple[float, float], p2: tuple[float, float]) -> float | None:
    if p1 == p2:
        return None
    lat1, lat2 = math.radians(p1[0]), math.radians(p2[0])
    dlon = math.radians(p2[1] - p1[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def parse_gtfs_zip(path: Path, version: str) -> StaticData:
    started = time.monotonic()
    with zipfile.ZipFile(path) as zf:
        agency = _read_csv(zf, "agency.txt")
        routes_rows = _read_csv(zf, "routes.txt")
        stops_rows = _read_csv(zf, "stops.txt")
        trips_rows = _read_csv(zf, "trips.txt")
        st_rows = _read_csv(zf, "stop_times.txt")
        cal_rows = _read_csv(zf, "calendar.txt")
        cd_rows = _read_csv(zf, "calendar_dates.txt")
        shape_rows = _read_csv(zf, "shapes.txt")

    tz = (agency[0].get("agency_timezone") if agency else None) or "America/Chicago"
    agency_name = (agency[0].get("agency_name") if agency else None) or "Metra"

    routes = {}
    for r in routes_rows:
        rid = r.get("route_id", "")
        if not rid:
            continue
        color = (r.get("route_color") or "").lstrip("#") or "888888"
        text = (r.get("route_text_color") or "").lstrip("#") or "FFFFFF"
        routes[rid] = {
            "id": rid,
            "short_name": r.get("route_short_name") or rid,
            "long_name": r.get("route_long_name") or r.get("route_short_name") or rid,
            "color": "#" + color.upper(),
            "text_color": "#" + text.upper(),
            "url": r.get("route_url") or None,
        }

    stops = {}
    for s in stops_rows:
        sid = s.get("stop_id", "")
        try:
            lat, lon = float(s.get("stop_lat", "")), float(s.get("stop_lon", ""))
        except ValueError:
            continue
        stops[sid] = {
            "id": sid,
            "name": clean_name(s.get("stop_name") or sid),
            "lat": lat,
            "lon": lon,
            "parent": s.get("parent_station") or None,
            "location_type": _int(s.get("location_type"), 0),
            "platform_code": s.get("platform_code") or None,
            "wheelchair": _int(s.get("wheelchair_boarding"), 0),
        }

    trips = {}
    for t in trips_rows:
        tid = t.get("trip_id", "")
        if not tid:
            continue
        trips[tid] = (
            t.get("route_id", ""),
            t.get("service_id", ""),
            t.get("trip_headsign") or "",
            t.get("shape_id") or "",
            _int(t.get("direction_id"), 0),
            t.get("trip_short_name") or "",
        )

    trip_stops: dict[str, list[tuple]] = {}
    for row in st_rows:
        tid = row.get("trip_id", "")
        if tid not in trips:
            continue
        arr = _parse_time(row.get("arrival_time", ""))
        dep = _parse_time(row.get("departure_time", ""))
        arr = arr if arr is not None else dep
        dep = dep if dep is not None else arr
        trip_stops.setdefault(tid, []).append(
            (
                _int(row.get("stop_sequence")),
                row.get("stop_id", ""),
                arr,
                dep,
                _int(row.get("pickup_type"), 0),
                _int(row.get("drop_off_type"), 0),
            )
        )

    stop_departures: dict[str, list[tuple[int, str, int]]] = {}
    trip_spans: dict[str, tuple[int, int]] = {}
    start_index: dict[tuple[str, int], list[str]] = {}
    for tid, sts in trip_stops.items():
        sts.sort(key=lambda s: s[S_SEQ])
        _fill_missing_times(sts)
        timed = [s for s in sts if s[S_DEP] is not None]
        if not timed:
            continue
        trip_spans[tid] = (timed[0][S_DEP], timed[-1][S_ARR])
        start_index.setdefault((trips[tid][T_ROUTE], timed[0][S_DEP]), []).append(tid)
        for s in sts[:-1]:  # the final stop is arrival-only
            if s[S_DEP] is None or s[S_PICKUP] == 1:
                continue
            stop_departures.setdefault(s[S_STOP], []).append((s[S_DEP], tid, s[S_SEQ]))
    for deps in stop_departures.values():
        deps.sort()

    calendar = {}
    for c in cal_rows:
        start, end = _parse_date(c.get("start_date", "")), _parse_date(c.get("end_date", ""))
        if not start or not end:
            continue
        days = tuple(
            c.get(d, "0") == "1"
            for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        )
        calendar[c.get("service_id", "")] = (days, start, end)

    calendar_dates: dict[date, dict[str, int]] = {}
    for c in cd_rows:
        d = _parse_date(c.get("date", ""))
        if d:
            calendar_dates.setdefault(d, {})[c.get("service_id", "")] = _int(c.get("exception_type"))

    raw_shapes: dict[str, list[tuple[int, float, float]]] = {}
    for p in shape_rows:
        try:
            raw_shapes.setdefault(p["shape_id"], []).append(
                (_int(p.get("shape_pt_sequence")), float(p["shape_pt_lat"]), float(p["shape_pt_lon"]))
            )
        except (KeyError, ValueError):
            continue
    shapes, shape_dists = {}, {}
    for sid, pts in raw_shapes.items():
        pts.sort()
        coords = [(lat, lon) for _, lat, lon in pts]
        cum = [0.0]
        for i in range(1, len(coords)):
            cum.append(cum[-1] + _dist_m(*coords[i - 1], *coords[i]))
        shapes[sid], shape_dists[sid] = coords, cum

    station_children: dict[str, list[str]] = {}
    for sid in stop_departures:
        stop = stops.get(sid)
        if not stop:
            continue
        parent = stop["parent"] if stop["parent"] in stops else sid
        station_children.setdefault(parent, []).append(sid)

    data = StaticData(
        version=version,
        loaded_at=time.time(),
        timezone=tz,
        agency_name=agency_name,
        routes=routes,
        stops=stops,
        trips=trips,
        trip_stops=trip_stops,
        stop_departures=stop_departures,
        calendar=calendar,
        calendar_dates=calendar_dates,
        shapes=shapes,
        shape_dists=shape_dists,
        trip_spans=trip_spans,
        start_index=start_index,
        station_children=station_children,
    )
    log.info(
        "Parsed GTFS %s: %d routes, %d stops, %d trips, %d shapes in %.1fs",
        version, len(routes), len(stops), len(trips), len(shapes), time.monotonic() - started,
    )
    return data


def _fill_missing_times(sts: list[tuple]) -> None:
    """Linearly interpolate stop times left blank (non-timepoints)."""
    n = len(sts)
    i = 0
    while i < n:
        if sts[i][S_DEP] is not None:
            i += 1
            continue
        j = i
        while j < n and sts[j][S_DEP] is None:
            j += 1
        if i == 0 or j == n:
            i = j
            continue
        t0, t1 = sts[i - 1][S_DEP], sts[j][S_ARR]
        for k in range(i, j):
            t = int(t0 + (t1 - t0) * (k - i + 1) / (j - i + 1))
            s = sts[k]
            sts[k] = (s[S_SEQ], s[S_STOP], t, t, s[S_PICKUP], s[S_DROPOFF])
        i = j


# ---- on-disk cache --------------------------------------------------------

class StaticCache:
    """Persists the downloaded zip and a parsed pickle under CONFIG_DIR/gtfs."""

    def __init__(self, config_dir: Path):
        self.dir = config_dir / "gtfs"
        self.zip_path = self.dir / "schedule.zip"
        self.pickle_path = self.dir / "static.pkl"
        self.version_path = self.dir / "published.txt"

    def ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def cached_version(self) -> str | None:
        try:
            return self.version_path.read_text().strip() or None
        except OSError:
            return None

    def load(self) -> StaticData | None:
        """Load the fastest available cached copy."""
        try:
            with self.pickle_path.open("rb") as fh:
                payload = pickle.load(fh)
            if payload.get("cache_version") == CACHE_VERSION:
                data: StaticData = payload["data"]
                log.info("Loaded cached static GTFS %s from %s", data.version, self.pickle_path)
                return data
        except FileNotFoundError:
            pass
        except Exception as exc:  # corrupt/old pickle: fall through to the zip
            log.warning("Ignoring unreadable cache %s: %s", self.pickle_path, exc)
        if self.zip_path.exists():
            version = self.cached_version() or file_digest(self.zip_path)
            try:
                data = parse_gtfs_zip(self.zip_path, version)
            except Exception as exc:
                log.warning("Cached zip unusable: %s", exc)
                return None
            self.save_pickle(data)
            return data
        return None

    def save_zip(self, content: bytes, version: str) -> None:
        self.ensure_dir()
        _atomic_write(self.zip_path, content)
        _atomic_write(self.version_path, (version + "\n").encode())

    def save_pickle(self, data: StaticData) -> None:
        self.ensure_dir()
        payload = {"cache_version": CACHE_VERSION, "data": data}
        _atomic_write(self.pickle_path, pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))


def _atomic_write(path: Path, content: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
