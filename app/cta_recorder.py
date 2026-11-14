"""Turning Train Tracker positions into history.

Every 30 s the recorder is shown where every train is. A train whose next
station changed since the last look has left the old one (and any it passed
in between); a train flagged as delayed starts or continues a delay.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date, datetime
from typing import Callable

from .cta_history import MAX_GAP
from .cta_live import iter_positions
from .cta_schedule import TZ, CtaSchedule, group_key, parse_cta_time, service_date


class Recorder:
    def __init__(self, get_schedule: Callable[[], CtaSchedule | None]):
        self._get_schedule = get_schedule
        # What each train was doing at the last look, and when the last train
        # left each station in each group (for the gap to the next one).
        self._runs: dict[tuple[str, str], dict] = {}
        self._last_dep: dict[tuple[str, str], float] = {}
        self._sched_cache: dict[tuple, list[float]] = {}

    def restore(self, last_arrivals: list[dict]) -> None:
        """Carry on from the departures already stored, so gaps continue across a restart."""
        for r in last_arrivals:
            self._last_dep[(str(r["station"]), r["gkey"])] = r["t"]

    def _between(self, gk: str, a: str, b: str) -> list[str] | None:
        """Stations a train on this group passes strictly between a and b, or None if
        a and b aren't in that order on any of its routes."""
        schedule = self._get_schedule()
        for seq in (schedule.sequences.get(gk, []) if schedule else []):
            if a in seq and b in seq:
                i, j = seq.index(a), seq.index(b)
                if i < j:
                    return seq[i + 1:j]
        return None

    def _sched_gap(self, station: str, gk: str, t: float) -> int | None:
        """Scheduled time between trains in this group at this station around t."""
        schedule = self._get_schedule()
        if schedule is None:
            return None
        sd = service_date(t)
        key = (station, gk, sd)
        times = self._sched_cache.get(key)
        if times is None:
            d = date.fromisoformat(sd)
            midnight = datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()
            by_service = schedule.deps.get(station, {}).get(gk, {})
            services = schedule.active_services(d)
            times = sorted({midnight + secs for sid in services & by_service.keys() for secs, _ in by_service[sid]})
            if len(self._sched_cache) > 5000:
                self._sched_cache.clear()
            self._sched_cache[key] = times
        if len(times) < 2:
            return None
        # Gaps around t, leaving out breaks in service (e.g. overnight).
        gaps = [b - a for a, b in zip(times, times[1:]) if b >= t - 1800 and a <= t + 1800 and 0 < b - a <= MAX_GAP]
        if not gaps:
            i = min(max(bisect_left(times, t), 1), len(times) - 1)
            gaps = [g for g in [times[i] - times[i - 1]] if 0 < g <= MAX_GAP]
        if not gaps:
            return None
        gaps.sort()
        return int(gaps[(len(gaps) - 1) // 2])

    def _departure(self, station: str, gk: str, run: str, t: float, delayed: bool, tracked: bool) -> tuple:
        last = self._last_dep.get((station, gk))
        gap = int(t - last) if last and 0 < t - last <= MAX_GAP else None
        if not last or t > last:
            self._last_dep[(station, gk)] = t
        return (service_date(t), station, gk, run, int(t), gap, self._sched_gap(station, gk, t), int(delayed), int(tracked))

    def observe(self, body: dict, now: float) -> tuple[list[tuple], list[tuple]]:
        """Compare positions with the previous look. Returns (departures, delays)
        as rows for CtaHistory.record_arrivals / record_delays."""
        arrivals, delays = [], []
        seen = set()
        for rid, t in iter_positions(body):
            run, nxt = t.get("rn"), t.get("nextStaId")
            if not run or not nxt:
                continue
            gk = group_key(rid, t.get("trDr"), t.get("destNm"))
            key = (rid, run)
            seen.add(key)
            prev = self._runs.get(key)
            delayed_now = t.get("isDly") == "1"
            state = {"gk": gk, "route": rid, "next": nxt, "arr": parse_cta_time(t.get("arrT")), "seen": now,
                     "delayed": delayed_now or bool(prev and prev["next"] == nxt and prev["delayed"]),
                     "delay_started": None}
            if prev and prev["next"] != nxt:
                left_at = min(prev["arr"] or now, now)
                arrivals.append(self._departure(prev["next"], prev["gk"], run, left_at, prev["delayed"], True))
                between = self._between(prev["gk"], prev["next"], nxt) or []
                if len(between) <= 4:  # a bigger jump is a gap in the data, not a fast train
                    for k, st in enumerate(between, 1):
                        est = left_at + (now - left_at) * k / (len(between) + 1)
                        arrivals.append(self._departure(st, prev["gk"], run, est, False, False))
                state["delayed"] = delayed_now
            if delayed_now:
                started = prev["delay_started"] if prev and prev.get("delay_started") else now
                state["delay_started"] = started
                delays.append((service_date(started), rid, gk, run, nxt, t.get("nextStaNm"), int(started), int(now)))
            self._runs[key] = state
        for key in [k for k, v in self._runs.items() if k not in seen and now - v["seen"] > 300]:
            del self._runs[key]  # finished, or out of service
        return arrivals, delays
