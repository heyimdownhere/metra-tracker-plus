"""CTA 'L' trains: the tracker behind /api/cta.

Kept apart from Metra's data on purpose: the schedule is cached in its own
folder (/config/cta), CTA home/work live in their own settings file, and
nothing here reads or writes the Metra tracker's state.

The pieces, one file each:
  cta_schedule   line tables, reading CTA's schedule zip, the cached copy
  cta_live       the Train Tracker client (caching, timeouts, daily budget)
  cta_recorder   turning train positions into history rows
  cta_history    the history database
  cta (this)     start/stop, schedule updates, and the queries

Train Tracker is only called when someone is looking (arrivals for a station,
all train positions for the map, one train's remaining stops) plus one
positions check every 30 s for history. With no key, or when Train Tracker
fails, boards fall back to the published schedule.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from .cta_history import MAX_GAP, CtaHistory
from .cta_live import (  # noqa: F401  (re-exported: the API and tests import these from here)
    DAILY_BUDGET, TrainTracker, TrainTrackerError, as_list, iter_positions,
)
from .cta_recorder import Recorder
from .cta_schedule import (  # noqa: F401
    CREDIT, LINE_ORDER, SHORT_NAMES, TZ, CtaCache, CtaSchedule, _platform, group_key, local_hour,
    parse_cta_time, parse_cta_zip, route_id, service_date,
)
from .history import day_type
from .util import USER_AGENT, SingleFlight

log = logging.getLogger(__name__)

RECORD_INTERVAL = 30   # seconds between position checks for history (about 2,900 requests a day)
STATS_TTL = 600        # seconds board history stats are reused
MAX_DOWNLOAD = 400 * 1024 * 1024  # CTA's schedule zip is about 50 MB; refuse anything absurd
# A gap longer than this between trains means no service (e.g. no midday
# Purple Express), not a very long wait, in the timetable's periods.
NO_SERVICE_GAP = 60

# Downtown: the Loop. Stations within DOWNTOWN_M of this point are "downtown",
# where "toward/away from downtown" means nothing, so their timetable is one list.
LOOP = (41.8819, -87.6278)
DOWNTOWN_M = 800
# How long after its last live train a scheduled one is still shown in the
# timetable (live times replace the schedule for the next few trains).
LIVE_OVERLAP = 60


def _dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return 6371000.0 * math.hypot(x, math.radians(b[0] - a[0]))


# Timetable periods (minutes after midnight of the service day).
PERIODS = [
    ("am_rush", "AM rush", 6 * 60, 9 * 60),
    ("midday", "Midday", 9 * 60, 15 * 60),
    ("pm_rush", "PM rush", 15 * 60, 18 * 60 + 30),
    ("evening", "Evening", 18 * 60 + 30, 22 * 60),
    ("late", "Late night", 22 * 60, 26 * 60),
]


def _hour(ts: float) -> int:
    return local_hour(ts)


class CtaTracker:
    def __init__(self, config_dir: Path, api_key: str | None, static_url: str,
                 check_hours: int = 24, enabled: bool = True, history_days: int = 0):
        self.static_url = static_url
        self.check_hours = check_hours
        self.enabled = enabled
        self.cache = CtaCache(config_dir)
        self.schedule: CtaSchedule | None = None
        self.static_error: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task] = []
        self._meta: dict | None = None
        self.live = TrainTracker(api_key, lambda: self._client, self.cache.dir / "budget.json")
        # History (its own database), filled by the recorder.
        self.history = CtaHistory(config_dir / "cta-history.db", history_days)
        self.recorder = Recorder(lambda: self.schedule)
        self._stats = SingleFlight(fail_ttl=60)
        self.recorded_at: float | None = None

    @property
    def live_enabled(self) -> bool:
        return self.live.enabled

    @property
    def loaded(self) -> bool:
        return self.schedule is not None

    # Kept for callers and tests that reach in.
    @property
    def _cache(self) -> SingleFlight:
        return self.live.cache

    @property
    def requests_today(self) -> int:
        return self.live.requests_today

    # ---- lifecycle -------------------------------------------------------------------
    async def start(self, client: httpx.AsyncClient | None = None) -> None:
        if not self.enabled:
            log.info("CTA is turned off (CTA_ENABLED=false)")
            return
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0), headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self.schedule = await asyncio.to_thread(self.cache.load)
        self._meta = await asyncio.to_thread(self.cache.meta)
        await asyncio.to_thread(self.live.load_budget)
        if not self.live_enabled:
            log.warning("CTA_TRAIN_TRACKER_KEY not set: CTA boards use the schedule only")
        self._tasks.append(asyncio.create_task(self._static_loop(), name="cta-static"))
        if self.live_enabled and self.history.enabled:
            try:
                await asyncio.to_thread(self.history.open)
                self.recorder.restore(await asyncio.to_thread(self.history.last_arrivals, time.time() - MAX_GAP))
                self._tasks.append(asyncio.create_task(self._record_loop(), name="cta-history"))
            except Exception:
                log.exception("Could not open the CTA history database; continuing without CTA history")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client:
            await self._client.aclose()
        self.live.save_budget()
        self.history.close()

    async def _record_loop(self) -> None:
        last_prune = 0.0
        while True:
            try:
                if self.schedule is not None:
                    await self.record_once()
                if time.time() - last_prune > 6 * 3600:
                    last_prune = time.time()
                    await asyncio.to_thread(self.history.prune)
            except asyncio.CancelledError:
                raise
            except TrainTrackerError as exc:
                log.debug("CTA history: positions unavailable (%s)", exc)
            except Exception:
                log.exception("CTA history recording failed")
            await asyncio.sleep(RECORD_INTERVAL)

    async def record_once(self, now: float | None = None) -> tuple[int, int]:
        """Check positions once and store departures and delays. Returns (departures, delays)."""
        body = await self.live.positions(recorder=True)
        now = time.time() if now is None else now
        arrivals, delays = self.observe(body, now)
        await asyncio.to_thread(self.history.record_arrivals, arrivals)
        await asyncio.to_thread(self.history.record_delays, delays)
        self.recorded_at = now
        return len(arrivals), len(delays)

    def observe(self, body: dict, now: float) -> tuple[list[tuple], list[tuple]]:
        return self.recorder.observe(body, now)

    # ---- schedule updates ------------------------------------------------------------
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

    def _save_meta(self, meta: dict) -> None:
        self._meta = meta
        self.cache.write_meta(meta)

    async def refresh_static(self, force: bool = False) -> bool:
        """Download CTA's schedule if it changed (asks with ETag/Last-Modified, so an
        unchanged file costs one small request). Returns True if a new one is in use.

        The zip goes straight to disk as it arrives, never whole into memory."""
        assert self._client
        meta = await asyncio.to_thread(self.cache.meta)
        headers = {}
        if self.schedule and not force:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
        self.cache.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cache.dir / "download.zip"
        try:
            async with self._client.stream("GET", self.static_url, headers=headers) as r:
                meta["checked_at"] = time.time()
                if r.status_code == 304:
                    await asyncio.to_thread(self._save_meta, meta)
                    self.static_error = None
                    return False
                r.raise_for_status()
                etag, modified = r.headers.get("etag"), r.headers.get("last-modified")
                size = 0
                with tmp.open("wb") as fh:
                    async for chunk in r.aiter_bytes(1 << 16):
                        size += len(chunk)
                        if size > MAX_DOWNLOAD:
                            raise ValueError(f"the download is larger than {MAX_DOWNLOAD >> 20} MB")
                        fh.write(chunk)
            version = modified or etag or f"downloaded {datetime.now(TZ):%Y-%m-%d}"
            try:
                data = await asyncio.to_thread(parse_cta_zip, tmp, version)
                problem = (None if data.stations and data.has_service(datetime.now(TZ).date())
                           else "no L trains in the coming week")
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
        meta.update(etag=etag, last_modified=modified, version=version, downloaded_at=time.time())
        await asyncio.to_thread(self._save_meta, meta)
        log.info("Now using CTA schedule %s (%d stations)", version, len(data.stations))
        self.schedule = data
        self.static_error = None
        return True

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
            if line is None:
                continue
            for coords in sch.shapes.get(rid, []):
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
                body = await self.live.arrivals(station)
                source, as_of = "live", parse_cta_time(body.get("tmst"))
                for e in as_list(body.get("eta")):
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
                error = self.live.error = str(exc)
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
        history = await self._station_history(station, now)
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
            # A-Z, so the label doesn't flip as trains come and go.
            g["destinations"] = sorted(counts)
            g["label"] = " · ".join(g["destinations"])
            g["history"] = history.get(g["key"])
            out.append(g)
        out.sort(key=lambda g: g["trains"][0]["time"])
        return {
            "system": "cta", "station": info, "to": self.station(to) if to else None,
            "source": source, "error": error, "as_of": as_of or now, "server_time": now,
            "groups": out, "credit": CREDIT,
        }

    async def _station_history(self, station: str, now: float) -> dict[str, dict]:
        """For each line/direction at a station: how regular it has been at this
        time of day, and its delays in the last 7 days. One database query for
        the station, reused for ten minutes. Groups without enough history yet
        are left out."""
        if not self.history.available:
            return {}
        hour, kind = _hour(now), day_type(service_date(now))

        async def fetch() -> dict[str, dict]:
            stats = await asyncio.to_thread(self.history.board_stats, station, hour, kind, 30, now, _hour)
            delays = await self._stats.get("delays-7d", STATS_TTL, lambda: asyncio.to_thread(
                self.history.delay_counts, now - 7 * 86400))
            return {gk: {**s, "delays_7d": delays.get(gk, 0), "day_type": kind, "hour": hour}
                    for gk, s in stats.items()}

        try:
            return await self._stats.get(f"board:{station}:{hour}:{kind}", STATS_TTL, fetch)
        except Exception as exc:
            log.warning("Reading CTA history for %s failed: %s", station, exc)
            return {}

    def _loop_dist(self, station: str) -> float | None:
        info = self._need_schedule().stations.get(station)
        return _dist_m((info["lat"], info["lon"]), LOOP) if info else None

    def _direction(self, station: str, gk: str) -> str | None:
        """'inbound' if this group's trains head toward the Loop from here (their next
        stop is closer to it), 'outbound' if away, None at a downtown station (or if
        unknown)."""
        here = self._loop_dist(station)
        if here is None or here <= DOWNTOWN_M:
            return None
        seqs = self._need_schedule().sequences.get(gk, [])
        for seq in seqs:
            if station in seq[:-1]:
                nxt = self._loop_dist(seq[seq.index(station) + 1])
                if nxt is not None:
                    return "inbound" if nxt < here else "outbound"
        # The last stop under this sign (e.g. Orange "Loop" trains at Roosevelt, just
        # before the sign changes): judge by the stop it came from.
        for seq in seqs:
            if station in seq[1:]:
                prev = self._loop_dist(seq[seq.index(station) - 1])
                if prev is not None:
                    return "inbound" if here < prev else "outbound"
        return None

    def timetable(self, station: str, now: float | None = None, to: str | None = None) -> dict:
        """Today at a station: every train still to come (`departures`, from the
        schedule; see timetable_live for live times), and per line and direction
        how often trains come, with the first and last train (`groups`).

        `to`: only trains that go on to that station, with their arrival there."""
        now = time.time() if now is None else now
        info = self.station(station)
        if to is not None:
            self.station(to)
        sch = self._need_schedule()
        today = datetime.fromtimestamp(now, TZ).date()
        midnight = datetime(today.year, today.month, today.day, tzinfo=TZ).timestamp()
        # Before 3 AM the previous service day is still running.
        if now - midnight < 3 * 3600:
            today -= timedelta(days=1)
            midnight = datetime(today.year, today.month, today.day, tzinfo=TZ).timestamp()
        services = sch.active_services(today)
        now_min = (now - midnight) / 60
        out, rows = [], []
        for gk, by_service in sch.deps.get(station, {}).items():
            travel = self._reaches(station, gk, to)
            if travel is False:
                continue  # doesn't go to `to`
            deps = sorted(d for sid in services & by_service.keys() for d in by_service[sid])
            if not deps:
                continue
            rid = gk.split(":", 1)[0]
            direction = self._direction(station, gk)
            line = self._line_info(rid)
            for secs, headsign in deps:
                t = midnight + secs
                if t >= now - 60:
                    rows.append({"key": gk, **line, "direction": direction, "destination": headsign,
                                 "time": t, "scheduled": t, "run": None, "live": False, "delayed": False,
                                 "due": False, "arrival": t + travel * 60 if travel is not None else None})
            if len(deps) < 2:
                continue
            mins = [secs / 60 for secs, _ in deps]
            gaps = [(a, b - a) for a, b in zip(mins, mins[1:])]
            periods = []
            for key, label, start, end in PERIODS:
                g = [round(gap) for at, gap in gaps if start <= at < end and gap <= NO_SERVICE_GAP]
                periods.append({"key": key, "label": label, "min": min(g) if g else None,
                                "max": max(g) if g else None, "now": start <= now_min < end})
            # Most frequent destination first; short trips that run a few times
            # a day (under 10% of trains) are left out of the label.
            counts: dict[str, int] = defaultdict(int)
            for _, h in deps:
                counts[h] += 1
            headsigns = [h for h in sorted(counts, key=lambda h: (-counts[h], h)) if counts[h] * 10 >= len(deps)]
            all_night = mins[0] < 60 and mins[-1] >= 24 * 60 and max(gap for _, gap in gaps) <= 60
            upcoming = [m for m in mins if m >= now_min]
            out.append({
                "key": gk, **line, "label": " · ".join(headsigns), "direction": direction,
                "travel_minutes": travel,
                "first": midnight + deps[0][0], "last": midnight + deps[-1][0],
                "all_night": all_night, "trains_left": len(upcoming), "count": len(deps),
                "periods": periods,
            })
        order = {r: i for i, r in enumerate(LINE_ORDER)}
        out.sort(key=lambda g: (order.get(g["route_id"], 99), g["label"]))
        rows.sort(key=lambda r: r["time"])
        return {"system": "cta", "station": info, "to": self.station(to) if to else None,
                "service_date": today.isoformat(), "downtown": (self._loop_dist(station) or 0) <= DOWNTOWN_M,
                "groups": out, "departures": rows, "source": "schedule", "error": None, "as_of": None,
                "server_time": now, "credit": CREDIT}

    async def timetable_live(self, station: str, now: float | None = None, to: str | None = None) -> dict:
        """timetable(), with Train Tracker's times for the next few trains of each
        line and direction in place of the schedule's. Uses the same cached
        arrivals as the board, so it costs no extra requests."""
        now = time.time() if now is None else now
        d = self.timetable(station, now, to)
        if not self.live_enabled:
            return d
        try:
            board = await self.arrivals(station, to=to, per_group=10, now=now)
        except TrainTrackerError:
            return d
        if board["source"] != "live":
            d["error"] = board["error"]
            return d
        directions = {g["key"]: g["direction"] for g in d["groups"]}
        live_rows: list[dict] = []
        last_live: dict[str, float] = {}
        for g in board["groups"]:
            for t in g["trains"]:
                live_rows.append({"key": g["key"], "route_id": g["route_id"], "route_short": g["route_short"],
                                  "route_name": g["route_name"], "color": g["color"], "text_color": g["text_color"],
                                  "direction": directions.get(g["key"], self._direction(station, g["key"])),
                                  "destination": t["destination"], "time": t["time"], "scheduled": None,
                                  "run": t["run"], "live": t["live"], "delayed": t["delayed"], "due": t["due"],
                                  "arrival": t.get("arrives")})
                last_live[g["key"]] = max(last_live.get(g["key"], 0), t["time"])
        # Live trains stand in for the scheduled ones up to the last live one.
        rows = [r for r in d["departures"] if r["time"] > last_live.get(r["key"], 0) + LIVE_OVERLAP] + live_rows
        rows.sort(key=lambda r: r["time"])
        d.update(departures=rows, source="live", as_of=board["as_of"])
        return d

    async def trains(self) -> dict:
        """Every L train's position (for the map)."""
        if not self.live_enabled:
            return {"system": "cta", "source": "none", "trains": [], "error": "no key", "credit": CREDIT}
        try:
            body = await self.live.positions()
        except TrainTrackerError as exc:
            self.live.error = str(exc)
            return {"system": "cta", "source": "none", "trains": [], "error": str(exc), "credit": CREDIT}
        out = []
        for rid, t in iter_positions(body):
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
                "bearing": int(str(heading)) if str(heading or "").isdigit() else None,
            })
        return {"system": "cta", "source": "live", "as_of": parse_cta_time(body.get("tmst")),
                "trains": out, "credit": CREDIT}

    async def follow(self, run: str) -> dict:
        """One train's remaining stops (for the double-tap sheet)."""
        run = run.strip()
        if not (run.isascii() and run.isdigit() and len(run) <= 4):
            raise KeyError(run)
        await self._check_running(run)
        try:
            body = await self.live.follow(run)
        except TrainTrackerError as exc:
            if exc.code is not None and 500 <= exc.code < 600:  # 5xx: no such train right now
                raise KeyError(run)
            raise
        etas = as_list(body.get("eta"))
        pos = body.get("position") or {}
        rid = route_id(etas[0].get("rt")) if etas else None
        gk = group_key(rid, etas[0].get("trDr"), etas[0].get("destNm")) if rid else None
        stops = [{"station_id": e.get("staId"), "name": e.get("staNm"), "time": parse_cta_time(e.get("arrT")),
                  "due": e.get("isApp") == "1", "delayed": e.get("isDly") == "1", "scheduled": e.get("isSch") == "1"}
                 for e in etas]
        try:
            where = {"lat": float(pos["lat"]), "lon": float(pos["lon"]),
                     "bearing": int(pos["heading"]) if str(pos.get("heading", "")).isdigit() else None}
        except (KeyError, TypeError, ValueError):
            where = None
        now = time.time()
        passed = await self._passed_stops(run, gk, stops, now) if gk and stops else []
        delays_today = []
        if self.history.available and rid:
            delays_today = await asyncio.to_thread(self.history.run_delays, service_date(now), run)
        return {"system": "cta", "run": run, **(self._line_info(rid) if rid else {}), "key": gk,
                "destination": etas[0].get("destNm") if etas else None,
                "delayed": any(s["delayed"] for s in stops[:1]),
                "position": where, "passed": passed, "stops": stops,
                "delays_today": [{"station_id": d["station"], "name": d["station_name"], "started": d["started"],
                                  "minutes": round((d["ended"] - d["started"]) / 60)} for d in delays_today],
                "as_of": parse_cta_time(body.get("tmst")), "credit": CREDIT}

    async def _check_running(self, run: str) -> None:
        """Raise KeyError if the latest positions list trains and this run isn't
        one of them: CTA is then not asked about it at all. The positions are
        the ones the map and the recorder already fetch, so this costs nothing."""
        try:
            body = await self.live.positions()
        except TrainTrackerError:
            return  # can't check right now: let the follow request decide
        running = {str(t.get("rn") or "").lstrip("0") for _, t in iter_positions(body)}
        if running and run.lstrip("0") not in running:
            raise KeyError(run)

    async def _passed_stops(self, run: str, gk: str, stops: list[dict], now: float) -> list[dict]:
        """Stations this train already left, in order. Times are the ones tracked
        for this run today where the server saw it; otherwise estimated from
        the schedule's running times ("estimated")."""
        sch = self.schedule
        upcoming = [s["station_id"] for s in stops]
        seqs = [q for q in sch.sequences.get(gk, []) if upcoming[0] in q]
        if not seqs:
            return []
        seq = max(seqs, key=lambda q: sum(1 for st in upcoming if st in q))
        before = seq[:seq.index(upcoming[0])]
        tracked = {}
        if self.history.available:
            for r in await asyncio.to_thread(self.history.run_today, service_date(now), run):
                if r["gkey"] == gk:
                    tracked[str(r["station"])] = r["t"]
        first_time = stops[0]["time"]
        out = []
        for st in before:
            t, source = tracked.get(st), "tracked"
            if t is None:
                mins = sch.reach.get((st, gk), {}).get(upcoming[0])
                t, source = (first_time - mins * 60 if first_time and mins is not None else None), "estimated"
            if t is not None and t > now:
                t = now
            info = sch.stations.get(st, {})
            out.append({"station_id": st, "name": info.get("name", st), "time": t, "source": source})
        return out

    async def alerts(self) -> list[dict]:
        """CTA's current alerts for L lines and stations (no key needed)."""
        try:
            raw = await self.live.alerts()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("CTA alerts failed: %s", exc.__class__.__name__)
            return []
        out = []
        for a in raw:
            services = as_list((a.get("ImpactedService") or {}).get("Service"))
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
        meta = self._meta if self._meta is not None else self.cache.meta()
        return {
            "enabled": self.enabled, "live": self.live_enabled,
            "schedule_loaded": self.schedule is not None,
            "schedule_version": self.schedule.version if self.schedule else None,
            "schedule_checked_at": meta.get("checked_at"), "schedule_error": self.static_error,
            "last_live": self.live.last_live, "live_error": self.live.error,
            "requests_today": self.live.requests_today, "daily_budget": DAILY_BUDGET,
            "history": self.history.available, "history_recorded_at": self.recorded_at,
            "credit": CREDIT,
        }
