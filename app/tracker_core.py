"""The tracker's state: the schedule in use, polling the realtime feeds,
and what the feeds say about each train (predictions, GPS estimates).

Queries are layered on top of this in tracker_stations, tracker_map and
tracker_boards; app.tracker.Tracker is the finished class."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from datetime import date

import httpx

from . import realtime as rtp
from .config import Settings
from .history import History
from .static_gtfs import S_ARR, S_DEP, S_SEQ, S_STOP, T_ROUTE, StaticCache, StaticData, check_usable, parse_gtfs_zip
from .tracker_helpers import (
    DELAY_EFFECTS, GPS_AT_STATION_M, GPS_MAX_OFF_ROUTE_M, STALE_VEHICLE_SECONDS,
    TRIP_OVER_AFTER_SECONDS, _is_delay_alert, _TripRT, is_schedule_change,
)
from .util import USER_AGENT

log = logging.getLogger(__name__)


class TrackerCore:
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
        self._running_labels: dict[str, str] = {}
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
            # Once a week (STATIC_FULL_REFRESH_DAYS), download the full schedule
            # even if published.txt hasn't changed, in case it changed silently.
            try:
                if self.static and self._full_refresh_due():
                    log.info("Weekly schedule check: downloading the full schedule")
                    await self.refresh_static(full=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Weekly schedule download failed; keeping the current schedule")
                self.static_error = str(exc)
            # Retry sooner if we still have nothing to show.
            await asyncio.sleep(self.settings.static_check_interval if self.static else 60)

    def _full_refresh_due(self, now: float | None = None) -> bool:
        days = self.settings.static_full_refresh_days
        if not days:
            return False
        last = self.cache.downloaded_at()
        now = time.time() if now is None else now
        return last is None or now - last >= days * 86400

    async def refresh_static(self, force: bool = False, full: bool = False) -> bool:
        """Download the schedule if Metra published a new one (or always, with force/full).

        A download only replaces the schedule in use if it parses and has trains
        in the coming week; the previous file is kept as schedule.prev.zip.
        `full` is the weekly re-check: an identical file just records the check.
        """
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
            if not force and not full and self.static and (published is None or published == current):
                self.static_error = None
                return False
            log.info("Downloading static GTFS (published: %s, have: %s)", published, current)
            r = await self._client.get(self.settings.static_url)
            r.raise_for_status()
            content = r.content
            if self.static and await asyncio.to_thread(self.cache.same_as_current, content):
                await asyncio.to_thread(self.cache.mark_checked)
                log.info("Schedule unchanged (%s)", current)
                self.static_error = None
                return False
            version = published or "sha256:" + hashlib.sha256(content).hexdigest()[:16]
            if self.static and version == current:
                # Same published.txt but different contents: Metra changed it silently.
                version = f"{version} (sha256:{hashlib.sha256(content).hexdigest()[:8]})"
            staged = await asyncio.to_thread(self.cache.stage, content)
            try:
                data = await asyncio.to_thread(parse_gtfs_zip, staged, version)
                problem = check_usable(data, data.local_date(time.time()))
            except Exception as exc:
                problem = f"it couldn't be read ({exc})"
            if problem:
                await asyncio.to_thread(self.cache.discard_staged)
                msg = f"Downloaded schedule {version} not used: {problem}"
                if self.static:
                    msg += f"; keeping {self.static.version}"
                log.warning(msg)
                self.static_error = msg
                return False
            await asyncio.to_thread(self.cache.promote, version)
            await asyncio.to_thread(self.cache.save_pickle, data)
            log.info("Now using schedule %s (previous kept as backup: %s)", version, current)
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
        # Train numbers from the positions feed, for trips a train is running right now.
        # (Trip updates for later trips name the train set that will run them, which is
        # usually still out on another trip with a different number.)
        running: dict[str, str] = {}
        for v in self.rt.vehicles:
            if static and v.trip and v.label:
                sid = static.resolve_trip(v.trip.trip_id, v.trip.route_id, v.trip.start_time, v.trip.start_date, v.trip.direction_id)
                if sid:
                    running[sid] = v.label
        self._running_labels = running

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

    def _gps_pred(self, trip_id: str, d: date, seq: int, sched: float, now: float | None = None) -> dict | None:
        """A prediction-shaped dict from a GPS estimate, for a stop the train hasn't passed."""
        est = self._gps.get(trip_id)
        if not est or est.get("finished") or est["service_date"] != d or seq < est["next_seq"]:
            return None
        t = sched + est["delay"]
        if est.get("at_station") and seq == est["next_seq"] and now is not None:
            # Standing at this platform: it leaves now-ish, however old the GPS fix is.
            t = max(t, now)
        return {"time": t, "delay": max(est["delay"], int(t - sched)), "skipped": False,
                "cancelled": False, "gps": True}

    def _rt_matches_date(self, trip_id: str, d: date) -> bool:
        info = self._rt_by_trip.get(trip_id)
        return bool(info) and (info.service_date is None or info.service_date == d)

    def data_as_of(self, feed: str = "tripupdates") -> dict:
        """When Metra last updated the data being shown (its feed's own timestamp),
        plus the schedule's published version. Shown with the data, per Metra's license."""
        st = self.rt.status.get(feed) if self.settings.realtime_enabled else None
        ts = st.feed_timestamp if st and st.feed_timestamp else None  # shown even when old: that's the point
        return {"realtime_updated": ts, "schedule_published": self.static.version if self.static else None}

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
                "schedule_change": is_schedule_change(a),
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

    def status(self) -> dict:
        s = self.static
        return {
            "mode": "realtime" if self.settings.realtime_enabled else "schedule-only",
            "realtime_live": self.realtime_live,
            "static_loaded": s is not None,
            "static_version": s.version if s else None,
            "static_loaded_at": s.loaded_at if s else None,
            "static_checked_at": self.static_checked_at,
            "static_downloaded_at": self.cache.downloaded_at(),
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
