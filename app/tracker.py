"""Background polling of Metra feeds and the queries the web API serves."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from . import realtime as rtp
from .config import Settings
from .history import History
from .static_gtfs import (
    _dist_m,
    S_DEP, S_SEQ, S_STOP, S_ARR,
    T_DIRECTION, T_ROUTE, T_SERVICE, T_SHAPE, T_SHORT_NAME,
    StaticCache, StaticData, parse_gtfs_zip,
)

log = logging.getLogger(__name__)

USER_AGENT = "metra-tracker-plus/1.0 (self-hosted)"

# A vehicle position older than this is dropped from the map (it stopped reporting).
STALE_VEHICLE_SECONDS = 180
# How far back to look for departures, so very late trains still show.
DEPARTURE_LOOKBACK_SECONDS = 4 * 3600
# GPS delay estimates are only made when the train is this close to its route.
GPS_MAX_OFF_ROUTE_M = 2000
GPS_AT_STATION_M = 200
# A train with no trip update this long after its scheduled finish is treated
# as out of service (Metra keeps tagging parked/deadheading trains with their
# last trip for hours).
TRIP_OVER_AFTER_SECONDS = 45 * 60

# Downtown Chicago (the Loop). Metra's downtown terminals (Union Station,
# Ogilvie, LaSalle St., Millennium) are all within ~1.5 km of this point.
LOOP = (41.8819, -87.6278)

DIRECTION_ALIASES = {
    "inbound": "inbound", "in": "inbound", "to": "inbound", "to_downtown": "inbound", "downtown": "inbound",
    "outbound": "outbound", "out": "outbound", "from": "outbound", "from_downtown": "outbound",
}


def normalize_direction(value: str | None) -> str | None:
    """'inbound' (toward downtown), 'outbound', or None for both. Raises ValueError."""
    if value is None or value.strip() == "" or value.strip().lower() in ("both", "any", "all"):
        return None
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in DIRECTION_ALIASES:
        raise ValueError(value)
    return DIRECTION_ALIASES[key]


class AmbiguousStation(KeyError):
    """A station name that matches more than one station."""

    def __init__(self, query: str, options: list[dict]):
        super().__init__(query)
        self.query = query
        self.options = options


def _norm_name(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", x.lower())

DELAY_EFFECTS = {"SIGNIFICANT_DELAYS", "REDUCED_SERVICE", "NO_SERVICE", "DETOUR", "MODIFIED_SERVICE"}
NON_DELAY_EFFECTS = {"ACCESSIBILITY_ISSUE", "STOP_MOVED", "NO_EFFECT", "ADDITIONAL_SERVICE"}
DELAY_WORDS = re.compile(
    r"\b(delay(s|ed)?|late|behind schedule|standing|stopped|holding|held|congestion|"
    r"mechanical|signal (problem|issue)s?|police|pedestrian|trespasser|freight|weather)\b",
    re.IGNORECASE,
)


def _status(r: dict) -> tuple[int | None, str]:
    """(delay in minutes, human status) for a departure row."""
    delay_min = round(r["delay"] / 60) if r["delay"] is not None else None
    if r["cancelled"]:
        return delay_min, "cancelled"
    if r.get("tracking"):
        return delay_min, (f"about {delay_min} min late" if delay_min and delay_min >= 1 else "on time (GPS)")
    if not r["live"]:
        return delay_min, "scheduled"
    if delay_min and delay_min >= 1:
        return delay_min, f"{delay_min} min late"
    if delay_min and delay_min <= -1:
        return delay_min, f"{-delay_min} min early"
    return delay_min, "on time"


def _is_delay_alert(a: rtp.AlertInfo) -> bool:
    """Whether a line-wide alert plausibly explains a late train."""
    if a.effect in DELAY_EFFECTS:
        return True
    if a.effect in NON_DELAY_EFFECTS:
        return False
    if a.cause not in ("UNKNOWN_CAUSE", "OTHER_CAUSE", "", None):
        return True
    return bool(DELAY_WORDS.search(f"{a.header} {a.description}"))


@dataclass
class _TripRT:
    """Realtime info for one static trip, indexed for fast lookup."""
    update: rtp.TripUpdateInfo
    service_date: date | None


class Tracker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cache = StaticCache(settings.config_dir)
        self.history = History(settings.config_dir / "history.db", settings.history_days)
        self.static: StaticData | None = None
        self.static_error: str | None = None
        self.static_checked_at: float | None = None
        self.rt = rtp.RealtimeSnapshot()
        self._rt_by_trip: dict[str, _TripRT] = {}
        self._added_trips: list[rtp.TripUpdateInfo] = []
        self._alerts_by_trip: dict[str, list[rtp.AlertInfo]] = {}
        self._alerts_by_route: dict[str, list[rtp.AlertInfo]] = {}
        self._tasks: list[asyncio.Task] = []
        self._client: httpx.AsyncClient | None = None
        self._static_lock = asyncio.Lock()
        self._trains_cache: tuple[float, dict] | None = None
        # Monotonic time of the last realtime poll. Metra allows at most one
        # request per feed every 30 seconds; refresh_realtime enforces it.
        self._last_rt_poll: float | None = None
        # trip_id -> GPS-based estimate for trains with a position but no trip update.
        self._gps: dict[str, dict] = {}
        self._labels: tuple[str | None, dict[str, str]] = (None, {})
        self.rt_polls = 0

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        try:
            self.cache.ensure_dir()
        except OSError as exc:
            log.warning("Cannot create cache dir %s: %s", self.cache.dir, exc)
        self.static = await asyncio.to_thread(self.cache.load)
        if self.settings.realtime_enabled and self.history.enabled:
            try:
                await asyncio.to_thread(self.history.open)
                self._tasks.append(asyncio.create_task(self._prune_loop(), name="prune"))
            except Exception:
                log.exception("Could not open history database; continuing without history")
        self._tasks.append(asyncio.create_task(self._static_loop(), name="static"))
        if self.settings.realtime_enabled:
            self._tasks.append(asyncio.create_task(self._realtime_loop(), name="realtime"))
        else:
            log.warning("METRA_API_TOKEN not set: running in schedule-only mode")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client:
            await self._client.aclose()
        self.history.close()

    async def _prune_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.history.prune)
            except Exception:
                log.exception("History prune failed")
            await asyncio.sleep(6 * 3600)

    # ---- static feed -----------------------------------------------------
    async def _static_loop(self) -> None:
        while True:
            try:
                await self.refresh_static()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Static GTFS refresh failed")
                self.static_error = str(exc)
            # Retry sooner if we still have nothing to show.
            await asyncio.sleep(self.settings.static_check_interval if self.static else 60)

    async def refresh_static(self, force: bool = False) -> bool:
        async with self._static_lock:
            assert self._client
            self.static_checked_at = time.time()
            published = None
            try:
                r = await self._client.get(self.settings.published_url)
                r.raise_for_status()
                published = r.text.strip() or None
            except httpx.HTTPError as exc:
                log.warning("Could not fetch published.txt: %s", exc)
            current = self.static.version if self.static else None
            if not force and self.static and (published is None or published == current):
                self.static_error = None
                return False
            log.info("Downloading static GTFS (published: %s, have: %s)", published, current)
            r = await self._client.get(self.settings.static_url)
            r.raise_for_status()
            content = r.content
            version = published or "sha256:" + hashlib.sha256(content).hexdigest()[:16]
            if not force and version == current:
                return False
            await asyncio.to_thread(self.cache.save_zip, content, version)
            data = await asyncio.to_thread(parse_gtfs_zip, self.cache.zip_path, version)
            await asyncio.to_thread(self.cache.save_pickle, data)
            self.static = data
            self.static_error = None
            self._trains_cache = None
            self._labels = (None, {})
            self._reindex_realtime()
            self._gps = self._gps_estimates(time.time())
            return True

    # ---- realtime feed ---------------------------------------------------
    async def _realtime_loop(self) -> None:
        while True:
            try:
                await self.refresh_realtime()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Realtime refresh failed")
            # Sleep until the guard in refresh_realtime will allow the next poll
            # (measured from the same timestamp), plus a small margin so a
            # slightly early wake-up can't cause a skipped poll.
            since = time.monotonic() - (self._last_rt_poll or 0.0)
            await asyncio.sleep(max(self.settings.realtime_interval - since, 0) + 0.25)

    async def _fetch_feed(self, name: str):
        assert self._client
        st = self.rt.status[name]
        st.last_attempt = time.time()
        try:
            r = await self._client.get(
                f"{self.settings.realtime_base}/{name}",
                headers={"Authorization": f"Bearer {self.settings.api_token}"},
            )
            if r.status_code in (401, 403):
                raise RuntimeError(f"HTTP {r.status_code}: token rejected")
            r.raise_for_status()
            msg = await asyncio.to_thread(rtp.parse_feed, r.content)
        except Exception as exc:
            st.error = str(exc) or exc.__class__.__name__
            log.warning("Realtime feed %s failed: %s", name, st.error)
            return None
        st.last_success = time.time()
        st.error = None
        st.entities = len(msg.entity)
        st.feed_timestamp = msg.header.timestamp or None
        return msg

    async def refresh_realtime(self) -> bool:
        """Poll Metra's three realtime feeds once. Returns False if skipped.

        This is the only place realtime requests are made, and it refuses to run
        more often than REALTIME_INTERVAL (minimum 30 s), whoever calls it.
        Web/API requests never trigger a poll; they read the cached snapshot.
        """
        mono = time.monotonic()
        if self._last_rt_poll is not None and mono - self._last_rt_poll < self.settings.realtime_interval:
            log.debug("Skipping realtime poll: last one was %.0fs ago", mono - self._last_rt_poll)
            return False
        self._last_rt_poll = mono
        self.rt_polls += 1
        positions, updates, alerts = await asyncio.gather(
            *(self._fetch_feed(f) for f in rtp.FEEDS)
        )
        if positions is not None:
            self.rt.vehicles = rtp.parse_positions(positions)
        if updates is not None:
            self.rt.trip_updates = rtp.parse_trip_updates(updates)
        if alerts is not None:
            self.rt.alerts = rtp.parse_alerts(alerts)
        self._trains_cache = None
        self._reindex_realtime()
        self._gps = self._gps_estimates(time.time())
        if self.history.available:
            try:
                rows = self._observation_rows(time.time()) if updates is not None else []
                await asyncio.to_thread(self.history.record_observations, rows)
                if alerts is not None:
                    await asyncio.to_thread(self.history.record_alerts, self.rt.alerts)
            except Exception:
                log.exception("Recording history failed")
        return True

    def _observation_rows(self, now: float) -> list[tuple]:
        """Current predictions for every upcoming stop of every realtime trip."""
        s = self.static
        if not s:
            return []
        rows = []
        for trip_id, info in self._rt_by_trip.items():
            d = info.service_date or s.trip_service_date(trip_id, now)
            if d is None:
                continue
            base = s.service_day_start(d)
            trip = s.trips[trip_id]
            reason_cache: list = []
            for st in s.trip_stops.get(trip_id, []):
                sched = base + st[S_DEP]
                pred = self._predict(trip_id, st[S_SEQ], base)
                if not pred or pred["skipped"]:
                    continue
                if pred["cancelled"]:
                    if sched < now - 120:
                        continue
                    t, delay = None, None
                else:
                    t, delay = pred["time"], pred["delay"]
                    if not t or t < now - 120:
                        continue  # already departed: keep the last pre-departure value
                reason = None
                if pred["cancelled"] or (delay is not None and delay >= 60):
                    if not reason_cache:
                        reason_cache.append(self.delay_reason(trip_id, trip[T_ROUTE], now))
                    reason = reason_cache[0]
                rows.append((
                    d.isoformat(), trip_id, st[S_STOP], trip[T_ROUTE], s.trip_destination(trip_id),
                    st[S_SEQ], int(sched), int(t) if t else None, delay, pred["cancelled"], reason,
                ))
        return rows

    def _reindex_realtime(self) -> None:
        by_trip: dict[str, _TripRT] = {}
        added: list[rtp.TripUpdateInfo] = []
        static = self.static
        for tu in self.rt.trip_updates:
            t = tu.trip
            sid = static.resolve_trip(t.trip_id, t.route_id, t.start_time, t.start_date, t.direction_id) if static else None
            if sid is None:
                added.append(tu)
                continue
            sdate = None
            if t.start_date:
                try:
                    sdate = date(int(t.start_date[:4]), int(t.start_date[4:6]), int(t.start_date[6:8]))
                except ValueError:
                    sdate = None
            by_trip[sid] = _TripRT(tu, sdate)
        self._rt_by_trip = by_trip
        self._added_trips = added

        alerts_by_trip: dict[str, list[rtp.AlertInfo]] = {}
        alerts_by_route: dict[str, list[rtp.AlertInfo]] = {}
        for a in self.rt.alerts:
            for t in a.trips:
                keys = {t.trip_id} if t.trip_id else set()
                if static:
                    sid = static.resolve_trip(t.trip_id, t.route_id, t.start_time, t.start_date, t.direction_id)
                    if sid:
                        keys.add(sid)
                for k in keys:
                    alerts_by_trip.setdefault(k, []).append(a)
            for r in a.route_level_ids:
                alerts_by_route.setdefault(r, []).append(a)
        self._alerts_by_trip = alerts_by_trip
        self._alerts_by_route = alerts_by_route

    def delay_reason(self, trip_id: str | None, route_id: str | None, now: float) -> str | None:
        """Explanation for a late/cancelled train from active service alerts.

        An alert naming this exact trip wins; otherwise a route-wide alert.
        Uses the alert's cause unless it is UNKNOWN_CAUSE, else its text.
        """
        candidates = [a for a in self._alerts_by_trip.get(trip_id or "", []) if a.active(now)]
        if not candidates:
            # Most of Metra's line-wide alerts are standing notices (elevators,
            # station construction, weekend closures) that say nothing about
            # why a particular train is late, so only use delay-related ones.
            candidates = [
                a for a in self._alerts_by_route.get(route_id or "", [])
                if a.active(now) and _is_delay_alert(a)
            ]
            candidates.sort(key=lambda a: a.effect not in DELAY_EFFECTS)
        for a in candidates:
            if a.cause and a.cause not in ("UNKNOWN_CAUSE", "OTHER_CAUSE"):
                return a.cause.replace("_", " ").capitalize()
            text = a.header or a.description
            if text:
                return text
        return None

    @property
    def realtime_live(self) -> bool:
        return self.settings.realtime_enabled and self.rt.fresh("tripupdates")

    # ---- predictions -----------------------------------------------------
    def _predict(self, trip_id: str, seq: int, base: float) -> dict | None:
        """Realtime prediction for a static trip at stop `seq`.

        Returns None when there's no usable realtime data (show schedule),
        else {"time", "delay", "skipped", "cancelled"}.
        """
        static = self.static
        info = self._rt_by_trip.get(trip_id)
        if not info or not static:
            return None
        tu = info.update
        if tu.trip.relationship == rtp.TRIP_CANCELED:
            return {"time": None, "delay": None, "skipped": False, "cancelled": True}
        seq_by_stop, sched_by_seq = static.trip_index(trip_id)

        best: rtp.StopUpdate | None = None
        best_seq = None
        for stu in tu.stop_updates:
            sseq = stu.stop_sequence if stu.stop_sequence is not None else seq_by_stop.get(stu.stop_id or "")
            if sseq is None or sseq > seq:
                continue
            if best_seq is None or sseq >= best_seq:
                best, best_seq = stu, sseq

        target = sched_by_seq.get(seq)
        if target is None:
            return None
        sched = base + target[S_DEP]

        if best is None:
            if tu.delay is not None:
                return {"time": sched + tu.delay, "delay": tu.delay, "skipped": False, "cancelled": False}
            return None
        if best.relationship == rtp.NO_DATA:
            return None
        if best_seq == seq:
            if best.relationship == rtp.SKIPPED:
                return {"time": None, "delay": None, "skipped": True, "cancelled": False}
            t = best.departure_time or best.arrival_time
            if t:
                return {"time": float(t), "delay": int(t - sched), "skipped": False, "cancelled": False}
            d = best.departure_delay if best.departure_delay is not None else best.arrival_delay
            if d is not None:
                return {"time": sched + d, "delay": d, "skipped": False, "cancelled": False}
            return None

        # Propagate delay from the most recent upstream update.
        up = sched_by_seq.get(best_seq)
        delay = best.departure_delay if best.departure_delay is not None else best.arrival_delay
        if delay is None and up is not None:
            if best.departure_time:
                delay = int(best.departure_time - (base + up[S_DEP]))
            elif best.arrival_time:
                delay = int(best.arrival_time - (base + up[S_ARR]))
        if delay is None:
            delay = tu.delay
        if delay is None:
            return None
        return {"time": sched + delay, "delay": delay, "skipped": False, "cancelled": False}

    # ---- GPS-based estimates (trains Metra tracks but sends no prediction for) ----
    def _gps_estimates(self, now: float) -> dict[str, dict]:
        s = self.static
        if not s or not self.settings.realtime_enabled:
            return {}
        out = {}
        for v in self.rt.vehicles:
            if v.trip is None or (v.timestamp and now - v.timestamp > STALE_VEHICLE_SECONDS):
                continue
            trip_id = s.resolve_trip(v.trip.trip_id, v.trip.route_id, v.trip.start_time,
                                     v.trip.start_date, v.trip.direction_id)
            if not trip_id or trip_id in self._rt_by_trip:
                continue  # Metra's own prediction wins
            est = self._gps_estimate(trip_id, v.lat, v.lon, float(v.timestamp or now))
            if est:
                out[trip_id] = est
        return out

    def _gps_estimate(self, trip_id: str, lat: float, lon: float, ts: float) -> dict | None:
        """Delay and next stop from where the train is vs where the timetable says.

        Projects the GPS point onto the straight segments between the trip's
        stations, then interpolates the scheduled time at that point. Trains
        rarely run early and GPS is noisy, so early readings count as on time.
        """
        s = self.static
        d = s.trip_service_date(trip_id, ts)
        sts = s.trip_stops.get(trip_id) or []
        if d is None or len(sts) < 2:
            return None
        base = s.service_day_start(d)
        if ts > base + sts[-1][S_ARR] + TRIP_OVER_AFTER_SECONDS:
            # Long past the scheduled finish (often parked in a yard off the
            # line): the trip is over, wherever the train is now.
            return {"finished": True, "service_date": d}
        coslat = math.cos(math.radians(lat))

        def xy(stop):
            return ((stop["lon"] - lon) * 111320 * coslat, (stop["lat"] - lat) * 110540)

        best = None  # (distance_m, index, fraction)
        for i in range(len(sts) - 1):
            a, b = s.stops.get(sts[i][S_STOP]), s.stops.get(sts[i + 1][S_STOP])
            if not a or not b:
                continue
            (ax, ay), (bx, by) = xy(a), xy(b)
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            f = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
            dist = math.hypot(ax + f * dx, ay + f * dy)
            if best is None or dist < best[0]:
                best = (dist, i, f)
        if best is None or best[0] > GPS_MAX_OFF_ROUTE_M:
            return None
        _, i, f = best
        a, b = sts[i], sts[i + 1]
        # Standing at (or pulling into) a station: compare against its departure.
        at_station = None
        for j in (i, i + 1):
            stop = s.stops.get(sts[j][S_STOP])
            if stop and math.hypot(*xy(stop)) <= GPS_AT_STATION_M:
                at_station = j
                break
        if at_station == len(sts) - 1:
            return {"finished": True, "service_date": d}  # parked at the end of the line
        if at_station is not None:
            sched_here, next_i = base + sts[at_station][S_DEP], at_station
        else:
            sched_here, next_i = base + a[S_DEP] + f * (b[S_ARR] - a[S_DEP]), i + 1
        delay = max(0, int(ts - sched_here))
        return {
            "delay": delay,
            "next_seq": sts[next_i][S_SEQ],
            "next_stop": s.stop_name(sts[next_i][S_STOP]),
            "at_station": at_station is not None,
            "service_date": d,
        }

    def _gps_pred(self, trip_id: str, d: date, seq: int, sched: float) -> dict | None:
        """A prediction-shaped dict from a GPS estimate, for a stop the train hasn't passed."""
        est = self._gps.get(trip_id)
        if not est or est.get("finished") or est["service_date"] != d or seq < est["next_seq"]:
            return None
        return {"time": sched + est["delay"], "delay": est["delay"], "skipped": False,
                "cancelled": False, "gps": True}

    def station_label(self, station_id: str) -> str:
        """Display name; stations sharing a name get their line(s), e.g. 'Lake Forest (UP-N)'."""
        s = self.static
        version, labels = self._labels
        if version != s.version:
            by_name: dict[str, list[str]] = {}
            for sid in s.station_children:
                by_name.setdefault(s.stops[sid]["name"], []).append(sid)
            labels = {}
            for name, sids in by_name.items():
                for sid in sids:
                    if len(sids) > 1:
                        lines = sorted({s.trips[x[1]][T_ROUTE] for c in s.station_children[sid]
                                        for x in s.stop_departures.get(c, [])})
                        labels[sid] = f"{name} ({'/'.join(lines) or sid})"
                    else:
                        labels[sid] = name
            self._labels = (s.version, labels)
        return labels.get(station_id) or s.stop_name(station_id) or station_id

    def _rt_matches_date(self, trip_id: str, d: date) -> bool:
        info = self._rt_by_trip.get(trip_id)
        return bool(info) and (info.service_date is None or info.service_date == d)

    # ---- public queries --------------------------------------------------
    def stations(self) -> list[dict]:
        s = self.static
        if not s:
            return []
        out = []
        for station_id, children in s.station_children.items():
            stop = s.stops[station_id]
            routes = sorted({s.trips[d[1]][T_ROUTE] for c in children for d in s.stop_departures.get(c, [])})
            out.append({
                "id": station_id,
                "name": self.station_label(station_id),
                "lat": stop["lat"],
                "lon": stop["lon"],
                "routes": routes,
            })
        out.sort(key=lambda x: x["name"].lower())
        return out

    def _loop_dist(self, stop_id: str | None) -> float | None:
        stop = self.static.stops.get(stop_id or "")
        if not stop:
            return None
        return _dist_m(stop["lat"], stop["lon"], *LOOP)

    def _direction(self, from_stop: str, final_stop: str | None) -> str | None:
        """'inbound' if the train ends closer to downtown than where you board.

        Uses geography rather than GTFS direction_id or IDs, so it keeps working
        if Metra renumbers things.
        """
        here, there = self._loop_dist(from_stop), self._loop_dist(final_stop)
        if here is None or there is None:
            return None
        return "inbound" if there < here else "outbound"

    def find_station(self, query: str) -> str:
        """Resolve a station id or name (case/punctuation-insensitive) to its id."""
        s = self.static
        if query in s.station_children:
            return query
        q = _norm_name(query)
        stations = list(s.station_children)
        for sid in stations:
            if _norm_name(sid) == q:
                return sid
        labelled = [sid for sid in stations if _norm_name(self.station_label(sid)) == q]
        if len(labelled) == 1:
            return labelled[0]
        exact = [sid for sid in stations if _norm_name(s.stops[sid]["name"]) == q]
        matches = exact or [sid for sid in stations if q and _norm_name(s.stops[sid]["name"]).startswith(q)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # e.g. "Lake Forest" is two different stations (MD-N and UP-N).
            lines = {st["id"]: st["routes"] for st in self.stations()}
            raise AmbiguousStation(query, [
                {"id": sid, "name": self.station_label(sid), "lines": lines.get(sid, [])} for sid in sorted(matches)
            ])
        raise KeyError(query)

    def station_stops(self, station_id: str) -> list[str]:
        """Stop ids belonging to a station id. Raises KeyError."""
        return self._station_stops(station_id)

    def _station_stops(self, station_id: str) -> list[str]:
        s = self.static
        stop_ids = s.station_children.get(station_id)
        if stop_ids is None and station_id in s.stop_departures:
            stop_ids = [station_id]
        if not stop_ids:
            raise KeyError(station_id)
        return stop_ids

    def _iter_departures(self, stop_ids: list[str], now: float):
        """Yield (trip_id, stop_id, seq, base, sched, effective, pred) for upcoming departures."""
        s = self.static
        lookback = DEPARTURE_LOOKBACK_SECONDS  # catch very late trains
        seen: set[tuple[str, date]] = set()
        for d in s.candidate_service_dates(now):
            base = s.service_day_start(d)
            services = s.active_services(d)
            lo = now - base - lookback
            hi = now - base + 36 * 3600
            for stop_id in stop_ids:
                deps = s.stop_departures.get(stop_id, [])
                i = bisect_left(deps, (int(lo), "", 0))
                for dep, trip_id, seq in deps[i:]:
                    if dep > hi:
                        break
                    trip = s.trips[trip_id]
                    if trip[T_SERVICE] not in services or (trip_id, d) in seen:
                        continue
                    seen.add((trip_id, d))
                    sched = base + dep
                    pred = None
                    if self.realtime_live and self._rt_matches_date(trip_id, d):
                        pred = self._predict(trip_id, seq, base)
                    if pred is None and self._gps:
                        pred = self._gps_pred(trip_id, d, seq, sched)
                    if pred and pred["skipped"]:
                        continue
                    if pred and pred["time"] and pred["time"] < sched:
                        # Metra trains don't leave a station before the timetable
                        # says, even when running ahead (predictions are arrivals).
                        pred = {**pred, "time": sched, "delay": 0}
                    effective = pred["time"] if pred and pred["time"] else sched
                    if effective < now - 30:
                        continue
                    yield trip_id, stop_id, seq, base, sched, effective, pred

    def _arrival(self, trip_id: str, seq: int, base: float, dest: set[str], pred: dict | None):
        """(scheduled, estimated) arrival at the first stop in `dest` after `seq`.

        None if the trip doesn't reach `dest` after this stop or skips it.
        """
        s = self.static
        stop = next((x for x in s.trip_stops[trip_id] if x[S_SEQ] > seq and x[S_STOP] in dest), None)
        if stop is None:
            return None
        sched = base + stop[S_ARR]
        arr_pred = self._predict(trip_id, stop[S_SEQ], base) if pred else None
        if arr_pred and arr_pred["skipped"]:
            return None
        if arr_pred and arr_pred["time"]:
            return sched, arr_pred["time"]
        return sched, sched + ((pred or {}).get("delay") or 0)

    def departures(
        self, station_id: str, limit: int = 5, now: float | None = None, to: str | None = None,
        direction: str | None = None,
    ) -> dict:
        """Next departures from a station.

        `to`: only trains that later stop there (rows gain arrival times).
        `direction`: "inbound" (toward downtown) or "outbound".
        """
        s = self.static
        if not s:
            return {"station": None, "departures": []}
        now = time.time() if now is None else now
        stop_ids = self._station_stops(station_id)
        dest = None
        if to:
            dest = set(s.station_children.get(to) or ([to] if to in s.stops else []))
            if not dest:
                raise KeyError(to)

        # Cheap filters first; full rows (predictions, final-stop ETA, alert
        # lookup) are only built for the trains we actually return.
        candidates = []
        for trip_id, stop_id, seq, base, sched, effective, pred in self._iter_departures(stop_ids, now):
            if direction:
                last = s.trip_stops[trip_id][-1][S_STOP]
                if self._direction(stop_id, last) != direction:
                    continue
            arrival = None
            if dest is not None:
                arrival = self._arrival(trip_id, seq, base, dest, pred)
                if arrival is None:
                    continue
            candidates.append((effective, trip_id, stop_id, base, sched, pred, arrival))
        candidates.sort(key=lambda c: c[0])

        rows = []
        for effective, trip_id, stop_id, base, sched, pred, arrival in candidates[:limit]:
            row = self._row(trip_id, stop_id, sched, effective, pred, base)
            if arrival is not None:
                row["arrival_scheduled"], row["arrival"] = arrival
            rows.append(row)
        if dest is None:
            rows.extend(
                r for r in self._added_departures(set(stop_ids), now)
                if not direction or r["direction"] == direction
            )
        rows.sort(key=lambda r: r["time"])
        station = s.stops.get(station_id)
        dest_stop = s.stops.get(to) if to else None
        return {
            "station": {"id": station_id, "name": self.station_label(station_id) if station else station_id},
            "to": {"id": to, "name": self.station_label(to) if dest_stop else to} if to else None,
            "direction": direction,
            "departures": rows[:limit],
            "generated_at": now,
            "realtime": self.realtime_live,
        }

    # ---- simple JSON for Home Assistant / scripts ------------------------
    def _iso(self, t: float | None) -> str | None:
        return datetime.fromtimestamp(t, self.static.tz).isoformat(timespec="seconds") if t else None

    def _api_train(self, r: dict, now: float) -> dict:
        """A departure row as plain JSON with ISO times (shared by /api/next and /api/commute)."""
        delay_min, status = _status(r)
        out = {
            "departure": self._iso(r["time"]),
            "departure_scheduled": self._iso(r["scheduled"]),
            "minutes_until": max(0, round((r["time"] - now) / 60)),
            "status": status,
            "delay_min": delay_min,
            "live": r["live"],
            "cancelled": r["cancelled"],
            "direction": r.get("direction"),
            "line": r["route_short"],
            "line_name": r["route_name"],
            "train": r["train"],
            "destination": r["destination"],
            "final_stop": r.get("final_stop"),
            "final_arrival": self._iso(r.get("final_arrival")),
            "final_arrival_scheduled": self._iso(r.get("final_arrival_scheduled")),
            "tracking": r.get("tracking", False),
            "delay_reason": r["delay_reason"],
        }
        if "arrival" in r:
            out["arrival"] = self._iso(r["arrival"])
            out["arrival_scheduled"] = self._iso(r["arrival_scheduled"])
            out["duration_min"] = round((r["arrival"] - r["time"]) / 60)
        return out

    def _summary_part(self, r: dict, status: str, with_destination: bool) -> str:
        """e.g. '7:30 AM to Chicago OTC (8 min late: Weather)'."""
        t = r["scheduled"] if r["cancelled"] and r["scheduled"] else r["time"]
        text = datetime.fromtimestamp(t, self.static.tz).strftime("%-I:%M %p")
        if with_destination and r["destination"]:
            text += f" to {r['destination']}"
        text += f" ({status}"
        if r["delay_reason"]:
            text += f": {r['delay_reason']}"
        return text + ")"

    def commute(self, from_id: str, to_id: str, limit: int = 3, now: float | None = None) -> dict:
        """Next trains that stop at `from_id` and later at `to_id` (for Home Assistant)."""
        s = self.static
        now = time.time() if now is None else now
        board = self.departures(from_id, limit, now=now, to=to_id)
        trips = [self._api_train(r, now) for r in board["departures"]]
        parts = [self._summary_part(r, t["status"], with_destination=False)
                 for r, t in zip(board["departures"], trips)]
        nxt = next((t for t in trips if not t["cancelled"]), None)
        from_name, to_name = board["station"]["name"], board["to"]["name"]
        return {
            "from": {"id": from_id, "name": from_name},
            "to": {"id": to_id, "name": to_name},
            "next_departure": nxt["departure"] if nxt else None,
            "next_minutes": nxt["minutes_until"] if nxt else None,
            "next_status": nxt["status"] if nxt else None,
            "trips": trips,
            "message": (f"{from_name} \u2192 {to_name}: " + ", ".join(parts)) if parts
                       else f"No upcoming trains {from_name} \u2192 {to_name}.",
            "realtime": self.realtime_live,
            "generated_at": self._iso(now),
        }

    def next_trains(self, station: str, direction: str | None = None, limit: int = 5,
                    now: float | None = None) -> dict:
        """Simple API: next trains at a station, optionally only to/from downtown."""
        now = time.time() if now is None else now
        station_id = self.find_station(station)
        board = self.departures(station_id, limit, now=now, direction=direction)
        trains = [self._api_train(r, now) for r in board["departures"]]
        parts = [self._summary_part(r, t["status"], with_destination=True)
                 for r, t in zip(board["departures"], trains)]
        nxt = next((t for t in trains if not t["cancelled"]), None)
        name = board["station"]["name"]
        label = {"inbound": " toward downtown", "outbound": " away from downtown"}.get(direction or "", "")
        return {
            "station": board["station"],
            "direction": direction or "both",
            "trains": trains,
            "next_departure": nxt["departure"] if nxt else None,
            "next_minutes": nxt["minutes_until"] if nxt else None,
            "message": (f"{name}{label}: " + ", ".join(parts)) if parts else f"No upcoming trains at {name}{label}.",
            "realtime": board["realtime"],
            "generated_at": self._iso(now),
        }

    def _final_arrival(self, trip_id: str, base: float, pred: dict | None) -> tuple[str | None, float | None, float | None]:
        """(final stop name, estimated arrival, scheduled arrival) for a trip."""
        s = self.static
        sts = s.trip_stops.get(trip_id)
        if not sts:
            return None, None, None
        last = sts[-1]
        sched = base + last[S_ARR]
        est = sched
        if pred and not pred.get("cancelled"):
            final_pred = self._predict(trip_id, last[S_SEQ], base)
            if final_pred and final_pred["time"]:
                est = final_pred["time"]
            elif pred.get("delay") is not None:
                est = sched + pred["delay"]
        return s.stop_name(last[S_STOP]), est, sched

    def _row(
        self, trip_id: str, stop_id: str, sched: float, effective: float, pred: dict | None,
        base: float | None = None,
    ) -> dict:
        s = self.static
        trip = s.trips[trip_id]
        route = s.routes.get(trip[T_ROUTE], {})
        final_stop, final_arrival, final_sched = (
            self._final_arrival(trip_id, base, pred) if base is not None else (None, None, None)
        )
        last_stop = s.trip_stops[trip_id][-1][S_STOP] if s.trip_stops.get(trip_id) else None
        return {
            "direction": self._direction(stop_id, last_stop),
            "final_stop": final_stop,
            "final_arrival": final_arrival,
            "final_arrival_scheduled": final_sched,
            "trip_id": trip_id,
            "train": trip[T_SHORT_NAME] or (self._rt_by_trip[trip_id].update.vehicle_label if trip_id in self._rt_by_trip else None),
            "route_id": trip[T_ROUTE],
            "route_name": route.get("long_name", trip[T_ROUTE]),
            "route_short": route.get("short_name", trip[T_ROUTE]),
            "color": route.get("color", "#888888"),
            "text_color": route.get("text_color", "#FFFFFF"),
            "destination": s.trip_destination(trip_id),
            "scheduled": sched,
            "time": effective,
            "delay": (pred or {}).get("delay"),
            "live": bool(pred and pred["time"] and not pred.get("gps")),
            "tracking": bool(pred and pred.get("gps")),
            "cancelled": bool(pred and pred["cancelled"]),
            "delay_reason": self._row_reason(trip_id, trip[T_ROUTE], pred),
        }

    def _row_reason(self, trip_id: str | None, route_id: str | None, pred: dict | None) -> str | None:
        if not pred:
            return None
        late = pred.get("delay") is not None and pred["delay"] >= 60
        if not (late or pred.get("cancelled")):
            return None
        return self.delay_reason(trip_id, route_id, time.time())

    def _added_departures(self, stop_ids: set[str], now: float) -> list[dict]:
        """Trips present only in realtime (e.g. ADDED extras)."""
        s = self.static
        if not s or not self.realtime_live:
            return []
        rows = []
        for tu in self._added_trips:
            if tu.trip.relationship == rtp.TRIP_CANCELED:
                continue
            for stu in tu.stop_updates[:-1]:  # the last stop is arrival-only
                if stu.stop_id not in stop_ids or stu.relationship == rtp.SKIPPED:
                    continue
                t = stu.departure_time or stu.arrival_time
                if not t or t < now - 30:
                    continue
                route = s.routes.get(tu.trip.route_id or "", {})
                last = tu.stop_updates[-1].stop_id if tu.stop_updates else None
                rows.append({
                    "trip_id": tu.trip.trip_id,
                    "train": tu.vehicle_label,
                    "route_id": tu.trip.route_id,
                    "route_name": route.get("long_name", tu.trip.route_id or "Metra"),
                    "route_short": route.get("short_name", tu.trip.route_id or "?"),
                    "color": route.get("color", "#888888"),
                    "text_color": route.get("text_color", "#FFFFFF"),
                    "destination": s.stop_name(last),
                    "scheduled": None,
                    "time": float(t),
                    "delay": None,
                    "live": True,
                    "tracking": False,
                    "cancelled": False,
                    "delay_reason": None,
                    "direction": self._direction(stu.stop_id, last),
                    "final_stop": s.stop_name(last),
                    "final_arrival": float(tu.stop_updates[-1].arrival_time or tu.stop_updates[-1].departure_time or 0) or None,
                    "final_arrival_scheduled": None,
                })
        return rows

    def trains(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if self._trains_cache and now - self._trains_cache[0] < 10:
            # Fresh timestamp: the app uses it to correct the phone's clock.
            return {**self._trains_cache[1], "generated_at": now}
        if self.settings.realtime_enabled and self.rt.fresh("positions"):
            result = {"source": "realtime", "trains": self._live_trains(now)}
        else:
            result = {"source": "schedule", "trains": self._estimated_trains(now)}
        result["generated_at"] = now
        self._trains_cache = (now, result)
        return result

    def _next_stop_delay(self, trip_id: str, now: float) -> tuple[int | None, str | None]:
        s = self.static
        d = s.trip_service_date(trip_id, now)
        if d is None:
            return None, None
        base = s.service_day_start(d)
        sts = s.trip_stops.get(trip_id, [])
        for st in sts:
            pred = self._predict(trip_id, st[S_SEQ], base)
            t = pred["time"] if pred and pred["time"] else base + st[S_DEP]
            if t >= now:
                return (pred or {}).get("delay"), s.stop_name(st[S_STOP])
        if sts:
            # Just reaching the end of the line: show the terminal as next stop.
            pred = self._predict(trip_id, sts[-1][S_SEQ], base)
            t = pred["time"] if pred and pred["time"] else base + sts[-1][S_ARR]
            if now - t < 15 * 60:
                return (pred or {}).get("delay"), s.stop_name(sts[-1][S_STOP])
        return None, None

    def _live_trains(self, now: float) -> list[dict]:
        s = self.static
        out = []
        labels = {tid: info.update.vehicle_label for tid, info in self._rt_by_trip.items()}
        for v in self.rt.vehicles:
            # Metra also reports cars and locomotives that aren't running a
            # trip (yards, layovers); those aren't trains a rider cares about.
            if v.trip is None or not (v.trip.trip_id or v.trip.route_id):
                continue
            if v.timestamp and now - v.timestamp > STALE_VEHICLE_SECONDS:
                continue  # stopped reporting; don't leave it frozen on the map
            trip_id = None
            if s and v.trip:
                trip_id = s.resolve_trip(v.trip.trip_id, v.trip.route_id, v.trip.start_time, v.trip.start_date, v.trip.direction_id)
            route_id = (s.trips[trip_id][T_ROUTE] if trip_id else None) or (v.trip.route_id if v.trip else None)
            route = (s.routes.get(route_id or "", {}) if s else {})
            delay, next_stop, tracking = None, None, False
            gps = self._gps.get(trip_id or "")
            if gps and gps.get("finished"):
                continue  # trip over: parked at the terminal or heading to a yard
            if trip_id and trip_id in self._rt_by_trip:
                delay, next_stop = self._next_stop_delay(trip_id, now)
            elif gps:
                # No prediction from Metra: use where the train actually is.
                delay, next_stop, tracking = gps["delay"], gps["next_stop"], True
            elif trip_id:
                delay, next_stop = self._next_stop_delay(trip_id, now)
            if next_stop is None and gps:
                next_stop = gps["next_stop"]
            if v.stop_id and s and v.status is not None:
                # VehicleStopStatus: 0 INCOMING_AT, 1 STOPPED_AT, 2 IN_TRANSIT_TO
                next_stop = s.stop_name(v.stop_id)
            out.append({
                "id": v.key,
                "train": (s.trips[trip_id][T_SHORT_NAME] if trip_id else None) or labels.get(trip_id or "") or v.label,
                "trip_id": trip_id or (v.trip.trip_id if v.trip else None),
                "route_id": route_id,
                "route_name": route.get("long_name", route_id),
                "route_short": route.get("short_name", route_id),
                "color": route.get("color", "#888888"),
                "text_color": route.get("text_color", "#FFFFFF"),
                "destination": s.trip_destination(trip_id) if (s and trip_id) else None,
                "lat": v.lat,
                "lon": v.lon,
                "bearing": v.bearing,
                "delay": delay,
                "delay_reason": (
                    self.delay_reason(trip_id or (v.trip.trip_id if v.trip else None), route_id, now)
                    if delay is not None and delay >= 60 else None
                ),
                "next_stop": next_stop,
                "stopped": v.status == 1 or bool(gps and gps["at_station"]),
                "timestamp": v.timestamp,
                "tracking": tracking,
                "live": True,
            })
        return out

    def _estimated_trains(self, now: float) -> list[dict]:
        """Where trains should be according to the schedule (no realtime)."""
        s = self.static
        if not s:
            return []
        out = []
        for d in s.candidate_service_dates(now)[:2]:
            base = s.service_day_start(d)
            services = s.active_services(d)
            t = now - base
            for trip_id, (first, last) in s.trip_spans.items():
                if not (first <= t <= last):
                    continue
                trip = s.trips[trip_id]
                if trip[T_SERVICE] not in services:
                    continue
                sts = s.trip_stops[trip_id]
                seg, frac, stopped, next_stop = 0, 0.0, False, None
                for i, st in enumerate(sts):
                    if st[S_ARR] <= t <= st[S_DEP]:
                        seg, frac, stopped, next_stop = i, 0.0, True, st[S_STOP]
                        break
                    if i + 1 < len(sts) and st[S_DEP] < t < sts[i + 1][S_ARR]:
                        span = sts[i + 1][S_ARR] - st[S_DEP]
                        seg, frac, next_stop = i, (t - st[S_DEP]) / span if span else 0.0, sts[i + 1][S_STOP]
                        break
                else:
                    continue
                lat, lon, bearing = s.point_on_trip(trip_id, seg, frac)
                route = s.routes.get(trip[T_ROUTE], {})
                out.append({
                    "id": f"{trip_id}@{d.isoformat()}",
                    "train": trip[T_SHORT_NAME] or None,
                    "trip_id": trip_id,
                    "route_id": trip[T_ROUTE],
                    "route_name": route.get("long_name", trip[T_ROUTE]),
                    "route_short": route.get("short_name", trip[T_ROUTE]),
                    "color": route.get("color", "#888888"),
                    "text_color": route.get("text_color", "#FFFFFF"),
                    "destination": s.trip_destination(trip_id),
                    "direction_id": trip[T_DIRECTION],
                    "lat": lat,
                    "lon": lon,
                    "bearing": bearing,
                    "delay": None,
                    "delay_reason": None,
                    "next_stop": s.stop_name(next_stop),
                    "stopped": stopped,
                    "timestamp": None,
                    "tracking": False,
                    "live": False,
                })
        return out

    def alerts(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        if not self.settings.realtime_enabled:
            return []
        s = self.static
        out = []
        for a in self.rt.alerts:
            if not a.active(now):
                continue
            out.append({
                "id": a.id,
                "header": a.header or a.description[:140],
                "description": a.description,
                "url": a.url,
                "cause": a.cause,
                "effect": a.effect,
                "routes": [
                    {
                        "id": r,
                        "short_name": (s.routes.get(r, {}).get("short_name", r) if s else r),
                        "color": (s.routes.get(r, {}).get("color", "#888888") if s else "#888888"),
                        "text_color": (s.routes.get(r, {}).get("text_color", "#FFFFFF") if s else "#FFFFFF"),
                    }
                    for r in a.route_ids
                ],
                "stops": [s.stop_name(x) if s else x for x in a.stop_ids],
            })
        return out

    def lines_geojson(self) -> dict:
        """One LineString per distinct shape used by each route."""
        s = self.static
        if not s:
            return {"type": "FeatureCollection", "features": []}
        features = []
        route_shapes: dict[str, set[str]] = {}
        route_longest_trip: dict[str, str] = {}
        for tid, trip in s.trips.items():
            if trip[T_SHAPE] in s.shapes:
                route_shapes.setdefault(trip[T_ROUTE], set()).add(trip[T_SHAPE])
            cur = route_longest_trip.get(trip[T_ROUTE])
            if tid in s.trip_stops and (cur is None or len(s.trip_stops[tid]) > len(s.trip_stops[cur])):
                route_longest_trip[trip[T_ROUTE]] = tid
        for rid, route in s.routes.items():
            lines = [s.shapes[sh] for sh in sorted(route_shapes.get(rid, ()))]
            if not lines and rid in route_longest_trip:
                # No shapes.txt for this route: connect its stations instead.
                pts = [s.stops[x[S_STOP]] for x in s.trip_stops[route_longest_trip[rid]] if x[S_STOP] in s.stops]
                lines = [[(p["lat"], p["lon"]) for p in pts]]
            for coords in lines:
                features.append({
                    "type": "Feature",
                    "properties": {
                        "route_id": rid,
                        "name": route["long_name"],
                        "short_name": route["short_name"],
                        "color": route["color"],
                    },
                    "geometry": {"type": "LineString", "coordinates": [[round(lon, 6), round(lat, 6)] for lat, lon in coords]},
                })
        return {"type": "FeatureCollection", "features": features}

    def status(self) -> dict:
        s = self.static
        return {
            "mode": "realtime" if self.settings.realtime_enabled else "schedule-only",
            "realtime_live": self.realtime_live,
            "static_loaded": s is not None,
            "static_version": s.version if s else None,
            "static_loaded_at": s.loaded_at if s else None,
            "static_checked_at": self.static_checked_at,
            "static_error": self.static_error,
            "timezone": s.timezone if s else "America/Chicago",
            "agency": s.agency_name if s else "Metra",
            "routes": list(s.routes.values()) if s else [],
            "realtime_interval": self.settings.realtime_interval,
            "realtime_age": (
                round(self.rt.data_age("tripupdates")) if self.rt.data_age("tripupdates") is not None else None
            ) if self.settings.realtime_enabled else None,
            "generated_at": time.time(),
            "history": self.history.stats(),
            "feeds": {
                name: {
                    "last_success": st.last_success,
                    "last_attempt": st.last_attempt,
                    "feed_timestamp": st.feed_timestamp,
                    "entities": st.entities,
                    "error": st.error,
                }
                for name, st in self.rt.status.items()
            } if self.settings.realtime_enabled else {},
        }
