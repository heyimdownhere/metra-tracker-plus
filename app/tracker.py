"""Background polling of Metra feeds and the queries the web API serves."""

from __future__ import annotations

import asyncio
import hashlib
import logging
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
            self._reindex_realtime()
            return True

    # ---- realtime feed ---------------------------------------------------
    async def _realtime_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.refresh_realtime()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Realtime refresh failed")
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(self.settings.realtime_interval - elapsed, 5))

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
        if self.history.enabled and self.history._db is not None:
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
        else {"time", "delay", "skipped", "cancelled", "track"}.
        """
        static = self.static
        info = self._rt_by_trip.get(trip_id)
        if not info or not static:
            return None
        tu = info.update
        if tu.trip.relationship == rtp.TRIP_CANCELED:
            return {"time": None, "delay": None, "skipped": False, "cancelled": True, "track": None}
        sts = static.trip_stops.get(trip_id, [])
        seq_by_stop = {s[S_STOP]: s[S_SEQ] for s in sts}
        sched_by_seq = {s[S_SEQ]: s for s in sts}

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
                return {"time": sched + tu.delay, "delay": tu.delay, "skipped": False, "cancelled": False, "track": None}
            return None
        if best.relationship == rtp.NO_DATA:
            return None
        track = self._track(best.assigned_stop_id) if best_seq == seq else None

        if best_seq == seq:
            if best.relationship == rtp.SKIPPED:
                return {"time": None, "delay": None, "skipped": True, "cancelled": False, "track": None}
            t = best.departure_time or best.arrival_time
            if t:
                return {"time": float(t), "delay": int(t - sched), "skipped": False, "cancelled": False, "track": track}
            d = best.departure_delay if best.departure_delay is not None else best.arrival_delay
            if d is not None:
                return {"time": sched + d, "delay": d, "skipped": False, "cancelled": False, "track": track}
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
        return {"time": sched + delay, "delay": delay, "skipped": False, "cancelled": False, "track": None}

    def _track(self, stop_id: str | None) -> str | None:
        if not stop_id or not self.static:
            return None
        stop = self.static.stops.get(stop_id)
        if stop and stop.get("platform_code"):
            return stop["platform_code"]
        return None

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
                "name": stop["name"],
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
        for sid in stations:
            if _norm_name(s.stops[sid]["name"]) == q:
                return sid
        matches = [sid for sid in stations if q and _norm_name(s.stops[sid]["name"]).startswith(q)]
        if len(matches) == 1:
            return matches[0]
        raise KeyError(query)

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
        lookback = 2 * 3600  # catch delayed trains scheduled up to 2h ago
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
                    if pred and pred["skipped"]:
                        continue
                    effective = pred["time"] if pred and pred["time"] else sched
                    if effective < now - 30:
                        continue
                    yield trip_id, stop_id, seq, base, sched, effective, pred

    def departures(
        self, station_id: str, limit: int = 5, now: float | None = None, to: str | None = None,
        direction: str | None = None,
    ) -> dict:
        """Next departures from a station; with `to`, only trains that later stop there."""
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
        rows = []
        for trip_id, stop_id, seq, base, sched, effective, pred in self._iter_departures(stop_ids, now):
            row = self._row(trip_id, stop_id, sched, effective, pred, base)
            if direction and row["direction"] != direction:
                continue
            if dest is not None:
                arrival = next((x for x in s.trip_stops[trip_id] if x[S_SEQ] > seq and x[S_STOP] in dest), None)
                if arrival is None:
                    continue
                arr_pred = self._predict(trip_id, arrival[S_SEQ], base) if pred else None
                if arr_pred and arr_pred["skipped"]:
                    continue
                row["arrival_scheduled"] = base + arrival[S_ARR]
                row["arrival"] = (
                    arr_pred["time"] if arr_pred and arr_pred["time"]
                    else row["arrival_scheduled"] + (row["delay"] or 0)
                )
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
            "station": {"id": station_id, "name": station["name"] if station else station_id},
            "to": {"id": to, "name": dest_stop["name"] if dest_stop else to} if to else None,
            "direction": direction,
            "departures": rows[:limit],
            "generated_at": now,
            "realtime": self.realtime_live,
        }

    def commute(self, from_id: str, to_id: str, limit: int = 3, now: float | None = None) -> dict:
        """Next trains that stop at `from_id` and later at `to_id` (for Home Assistant)."""
        s = self.static
        now = time.time() if now is None else now
        origin = self._station_stops(from_id)
        dest = set(s.station_children.get(to_id) or ([to_id] if to_id in s.stops else []))
        if not dest:
            raise KeyError(to_id)
        rows = []
        for trip_id, stop_id, seq, base, sched, effective, pred in self._iter_departures(origin, now):
            arrival = next((st for st in s.trip_stops[trip_id] if st[S_SEQ] > seq and st[S_STOP] in dest), None)
            if arrival is None:
                continue
            row = self._row(trip_id, stop_id, sched, effective, pred, base)
            arr_sched = base + arrival[S_ARR]
            arr_pred = self._predict(trip_id, arrival[S_SEQ], base) if pred else None
            if arr_pred and arr_pred["skipped"]:
                continue
            row["arrival_scheduled"] = arr_sched
            row["arrival"] = arr_pred["time"] if arr_pred and arr_pred["time"] else arr_sched + (row["delay"] or 0)
            rows.append(row)
        rows.sort(key=lambda r: r["time"])
        rows = rows[:limit]

        tz = s.tz
        def iso(t):
            return datetime.fromtimestamp(t, tz).isoformat(timespec="seconds") if t else None
        def hm(t):
            return datetime.fromtimestamp(t, tz).strftime("%-I:%M %p")

        trips, parts = [], []
        for r in rows:
            delay_min, status = _status(r)
            trips.append({
                "departure": iso(r["time"]),
                "departure_scheduled": iso(r["scheduled"]),
                "arrival": iso(r["arrival"]),
                "arrival_scheduled": iso(r["arrival_scheduled"]),
                "minutes_until": max(0, round((r["time"] - now) / 60)),
                "duration_min": round((r["arrival"] - r["time"]) / 60),
                "delay_min": delay_min,
                "status": status,
                "live": r["live"],
                "cancelled": r["cancelled"],
                "track": r["track"],
                "train": r["train"],
                "line": r["route_short"],
                "line_name": r["route_name"],
                "destination": r["destination"],
                "delay_reason": r["delay_reason"],
            })
            text = f"{hm(r['scheduled'] if r['cancelled'] else r['time'])} ({status}"
            if r["track"]:
                text += f", track {r['track']}"
            if r["delay_reason"]:
                text += f": {r['delay_reason']}"
            parts.append(text + ")")

        nxt = next((t for t in trips if not t["cancelled"]), None)
        from_name, to_name = s.stop_name(from_id), s.stop_name(to_id)
        return {
            "from": {"id": from_id, "name": from_name},
            "to": {"id": to_id, "name": to_name},
            "next_departure": nxt["departure"] if nxt else None,
            "next_minutes": nxt["minutes_until"] if nxt else None,
            "next_status": nxt["status"] if nxt else None,
            "trips": trips,
            "message": (f"{from_name} → {to_name}: " + ", ".join(parts)) if parts else f"No upcoming trains {from_name} → {to_name}.",
            "realtime": self.realtime_live,
            "generated_at": iso(now),
        }

    def next_trains(self, station: str, direction: str | None = None, limit: int = 5,
                    now: float | None = None) -> dict:
        """Simple API: next trains at a station, optionally only to/from downtown."""
        s = self.static
        now = time.time() if now is None else now
        station_id = self.find_station(station)
        board = self.departures(station_id, limit, now=now, direction=direction)
        tz = s.tz

        def iso(t):
            return datetime.fromtimestamp(t, tz).isoformat(timespec="seconds") if t else None

        trains, parts = [], []
        for r in board["departures"]:
            delay_min, status = _status(r)
            trains.append({
                "departure": iso(r["time"]),
                "departure_scheduled": iso(r["scheduled"]),
                "minutes_until": max(0, round((r["time"] - now) / 60)),
                "status": status,
                "delay_min": delay_min,
                "live": r["live"],
                "cancelled": r["cancelled"],
                "direction": r["direction"],
                "line": r["route_short"],
                "line_name": r["route_name"],
                "train": r["train"],
                "destination": r["destination"],
                "final_stop": r["final_stop"],
                "final_arrival": iso(r["final_arrival"]),
                "final_arrival_scheduled": iso(r["final_arrival_scheduled"]),
                "track": r["track"],
                "delay_reason": r["delay_reason"],
            })
            when = datetime.fromtimestamp(r["scheduled"] if r["cancelled"] and r["scheduled"] else r["time"], tz)
            parts.append(f"{when.strftime('%-I:%M %p')} to {r['destination']} ({status})")

        name = board["station"]["name"]
        label = {"inbound": " toward downtown", "outbound": " away from downtown"}.get(direction or "", "")
        return {
            "station": board["station"],
            "direction": direction or "both",
            "trains": trains,
            "next_departure": trains[0]["departure"] if trains else None,
            "next_minutes": trains[0]["minutes_until"] if trains else None,
            "message": (f"{name}{label}: " + ", ".join(parts)) if parts else f"No upcoming trains at {name}{label}.",
            "realtime": board["realtime"],
            "generated_at": iso(now),
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
        track = (pred or {}).get("track") or s.stops.get(stop_id, {}).get("platform_code")
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
            "live": bool(pred and pred["time"]),
            "cancelled": bool(pred and pred["cancelled"]),
            "track": track,
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
            for stu in tu.stop_updates:
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
                    "cancelled": False,
                    "track": self._track(stu.assigned_stop_id),
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
            return self._trains_cache[1]
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
        for st in s.trip_stops.get(trip_id, []):
            pred = self._predict(trip_id, st[S_SEQ], base)
            t = pred["time"] if pred and pred["time"] else base + st[S_DEP]
            if t >= now:
                return (pred or {}).get("delay"), s.stop_name(st[S_STOP])
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
            trip_id = None
            if s and v.trip:
                trip_id = s.resolve_trip(v.trip.trip_id, v.trip.route_id, v.trip.start_time, v.trip.start_date, v.trip.direction_id)
            route_id = (s.trips[trip_id][T_ROUTE] if trip_id else None) or (v.trip.route_id if v.trip else None)
            route = (s.routes.get(route_id or "", {}) if s else {})
            delay, next_stop = (None, None)
            if trip_id:
                delay, next_stop = self._next_stop_delay(trip_id, now)
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
                "stopped": v.status == 1,
                "timestamp": v.timestamp,
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
