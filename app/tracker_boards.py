"""Departure boards, the day's timetable, one train's stops, a line's trains,
and the plain JSON for Home Assistant."""

from __future__ import annotations

import logging
import time
from bisect import bisect_left
from datetime import date, datetime, timedelta

from . import realtime as rtp
from .history import day_type
from .static_gtfs import S_ARR, S_DEP, S_SEQ, S_STOP, T_ROUTE, T_SERVICE, T_SHORT_NAME
from .tracker_helpers import (
    BOARD_HISTORY_DAYS, BOARD_HISTORY_MIN_OBS, DEPARTURE_LOOKBACK_SECONDS, _status, is_schedule_change,
)
from .tracker_map import MapQueries

log = logging.getLogger(__name__)


class BoardQueries(MapQueries):
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
                        pred = self._gps_pred(trip_id, d, seq, sched, now)
                    if pred and pred["skipped"]:
                        continue
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

    def day_end(self, now: float) -> float:
        """End of "today" for the timetable: 3 AM local, after Metra's last late-night trains."""
        tz = self.static.tz
        local = datetime.fromtimestamp(now, tz)
        end = local.replace(hour=3, minute=0, second=0, microsecond=0)
        if local >= end:
            end = datetime.combine(local.date() + timedelta(days=1), end.time(), tzinfo=tz)
        return end.timestamp()

    def timetable(self, station_id: str, now: float | None = None, to: str | None = None) -> dict:
        """Every train still to come today from a station (live times where Metra has them).

        Without `to`, trains that end their trip here (e.g. at Ogilvie or Union Station)
        are included too, as arrivals (`arriving: true`, `origin`, `time` = arrival).
        """
        now = time.time() if now is None else now
        until = self.day_end(now)
        out = self.departures(station_id, limit=500, now=now, to=to, until=until)
        if not to and self.static:
            out["departures"] = sorted(
                out["departures"] + self._terminal_arrivals(self._station_stops(station_id), now, until),
                key=lambda r: r["time"],
            )
        out["until"] = until
        return out

    def _terminal_index(self) -> dict[str, list[tuple[int, str, int]]]:
        """stop -> [(scheduled arrival, trip, seq)] for trips that end at that stop."""
        s = self.static
        if getattr(self, "_term_for", None) is not s:
            idx: dict[str, list[tuple[int, str, int]]] = {}
            for tid, sts in s.trip_stops.items():
                if len(sts) > 1 and sts[-1][S_ARR] is not None:
                    idx.setdefault(sts[-1][S_STOP], []).append((sts[-1][S_ARR], tid, sts[-1][S_SEQ]))
            for v in idx.values():
                v.sort()
            self._term_idx, self._term_for = idx, s
        return self._term_idx

    def _terminal_arrivals(self, stop_ids: list[str], now: float, until: float) -> list[dict]:
        """Trains still to arrive today at the end of their trip at one of these stops."""
        s = self.static
        idx = self._terminal_index()
        rows, seen = [], set()
        for d in s.candidate_service_dates(now):
            base = s.service_day_start(d)
            services = s.active_services(d)
            lo = now - base - DEPARTURE_LOOKBACK_SECONDS
            for stop_id in stop_ids:
                arrs = idx.get(stop_id, [])
                for arr, trip_id, seq in arrs[bisect_left(arrs, (int(lo), "", 0)):]:
                    sched = base + arr
                    if sched >= until:
                        break
                    trip = s.trips[trip_id]
                    if trip[T_SERVICE] not in services or (trip_id, d) in seen:
                        continue
                    seen.add((trip_id, d))
                    pred = None
                    if self.realtime_live and self._rt_matches_date(trip_id, d):
                        pred = self._predict(trip_id, seq, base)
                    if pred is None and self._gps:
                        pred = self._gps_pred(trip_id, d, seq, sched, now)
                    if pred and pred["skipped"]:
                        continue
                    effective = pred["time"] if pred and pred["time"] else sched
                    if effective < now - 30:
                        continue
                    sts = s.trip_stops[trip_id]
                    row = self._row(trip_id, stop_id, sched, effective, pred, base)
                    first = sts[0]
                    row.update({
                        "arriving": True,
                        "direction": self._direction(first[S_STOP], stop_id),
                        "origin": s.stop_name(first[S_STOP]),
                        "origin_time": base + first[S_DEP] if first[S_DEP] is not None else None,
                        "final_arrival": None,
                        "history": None,
                    })
                    rows.append(row)
        return rows

    def departures(
        self, station_id: str, limit: int = 5, now: float | None = None, to: str | None = None,
        direction: str | None = None, until: float | None = None, route: str | None = None,
    ) -> dict:
        """Next departures from a station.

        `to`: only trains that later stop there (rows gain arrival times).
        `direction`: "inbound" (toward downtown) or "outbound".
        `until`: only trains scheduled to leave before this time.
        `route`: only trains on this line (route id).
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
            if until is not None and sched >= until:
                continue
            if route and s.trips[trip_id][T_ROUTE] != route:
                continue
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
        stats = self._board_history(stop_ids)
        for effective, trip_id, stop_id, base, sched, pred, arrival in candidates[:limit]:
            row = self._row(trip_id, stop_id, sched, effective, pred, base)
            if arrival is not None:
                row["arrival_scheduled"], row["arrival"] = arrival
            row["history"] = self._history_for(stats, row, base)
            rows.append(row)
        if dest is None:
            rows.extend(
                r for r in self._added_departures(set(stop_ids), now)
                if (not direction or r["direction"] == direction) and (until is None or r["time"] < until)
                and (not route or r["route_id"] == route)
            )
        rows.sort(key=lambda r: r["time"])
        station = s.stops.get(station_id)
        dest_stop = s.stops.get(to) if to else None
        return {
            "station": {"id": station_id, "name": self.station_label(station_id) if station else station_id},
            "to": {"id": to, "name": self.station_label(to) if dest_stop else to} if to else None,
            "direction": direction,
            "route": route,
            "departures": rows[:limit],
            "data_as_of": self.data_as_of("tripupdates"),
            "generated_at": now,
            "realtime": self.realtime_live,
        }

    # ---- on-time history for the board -------------------------------------
    def _board_history(self, stop_ids: list[str]) -> dict:
        if not self.history.available:
            return {}
        tz = self.static.tz
        try:
            return self.history.departure_stats(
                stop_ids, BOARD_HISTORY_DAYS, lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M")
            )
        except Exception:
            log.exception("Reading departure history failed")
            return {}

    def _history_for(self, stats: dict, row: dict, base: float) -> dict | None:
        """This train's record at this station on the same kind of day, if seen enough."""
        if not stats or not row.get("scheduled"):
            return None
        tz = self.static.tz
        hhmm = datetime.fromtimestamp(row["scheduled"], tz).strftime("%H:%M")
        service_date = self.static.local_date(base + 12 * 3600).isoformat()
        h = stats.get((row["route_id"], hhmm, row["destination"], day_type(service_date)))
        if not h or h["observations"] < BOARD_HISTORY_MIN_OBS:
            return None
        return {**h, "days": BOARD_HISTORY_DAYS}

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
            "history": r.get("history"),
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
            "final_station": self._station_of(last_stop),
            "final_arrival": final_arrival,
            "final_arrival_scheduled": final_sched,
            "trip_id": trip_id,
            "train": trip[T_SHORT_NAME] or self._running_labels.get(trip_id),
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
            "added": False,
            "schedule_change": self.schedule_note(trip_id, time.time()),
        }

    def schedule_note(self, trip_id: str | None, now: float) -> str | None:
        """Header of an active alert saying this specific train runs differently from the timetable."""
        for a in self._alerts_by_trip.get(trip_id or "", []):
            if a.active(now) and is_schedule_change(a):
                return a.header or a.description[:140] or "Schedule change"
        return None

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
                    "added": True,
                    "schedule_change": self.schedule_note(tu.trip.trip_id, now),
                    "direction": self._direction(stu.stop_id, last),
                    "final_stop": s.stop_name(last),
                    "final_station": self._station_of(last),
                    "final_arrival": float(tu.stop_updates[-1].arrival_time or tu.stop_updates[-1].departure_time or 0) or None,
                    "final_arrival_scheduled": None,
                })
        return rows

    def trip_detail(self, trip_id: str, now: float | None = None) -> dict:
        """One train's whole schedule: every stop with its scheduled and expected time,
        and, while it's running, where it is (stops passed, the next stop, its GPS fix)."""
        s = self.static
        now = time.time() if now is None else now
        if not s or trip_id not in s.trips:
            raise KeyError(trip_id)
        trip = s.trips[trip_id]
        sts = s.trip_stops.get(trip_id) or []
        span = s.trip_spans.get(trip_id)
        if not sts or not span:
            raise KeyError(trip_id)
        # The service day this trip runs on that's closest to now (today's run, or the
        # one still finishing after midnight).
        best = None
        for d in s.candidate_service_dates(now):
            if trip[T_SERVICE] not in s.active_services(d):
                continue
            base = s.service_day_start(d)
            start, end = base + span[0], base + span[1]
            dist = 0 if start - 1800 <= now <= end + 1800 else min(abs(now - start), abs(now - end))
            if best is None or dist < best[0]:
                best = (dist, d, base)
        if best is None:
            raise KeyError(trip_id)
        _, d, base = best
        live = self.realtime_live and self._rt_matches_date(trip_id, d)
        vehicle = next((t for t in self._all_trains(now).get("trains", []) if t.get("trip_id") == trip_id), None)
        gps = self._gps.get(trip_id)
        gps = gps if gps and gps.get("service_date") == d else None
        preds = [self._predict(trip_id, x[S_SEQ], base) if live else None for x in sts]
        known = [p for p in preds if p and p.get("delay") is not None]
        delay = (known[-1]["delay"] if known else None)
        tracking = False
        if delay is None and vehicle and vehicle.get("delay") is not None:
            delay, tracking = vehicle["delay"], bool(vehicle.get("tracking"))
        elif delay is None and gps:
            delay, tracking = gps.get("delay"), True
        cancelled = bool(preds[0] and preds[0].get("cancelled"))
        stops = []
        for x, p in zip(sts, preds):
            sched = base + (x[S_ARR] if x[S_ARR] is not None else x[S_DEP])
            est = p["time"] if p and p.get("time") else sched + (delay or 0)
            stops.append({
                "stop_id": x[S_STOP],
                "station_id": self._station_of(x[S_STOP]),
                "name": s.stop_name(x[S_STOP]),
                "scheduled": sched,
                "time": est,
                "skipped": bool(p and p.get("skipped")),
            })
        running = not cancelled and stops[0]["time"] - 60 <= now <= stops[-1]["time"] + 60
        next_i = None
        if running:
            name = (vehicle or {}).get("next_stop") or (gps or {}).get("next_stop")
            if name:
                next_i = next((i for i, st in enumerate(stops) if st["name"] == name), None)
            if next_i is None:
                next_i = next((i for i, st in enumerate(stops) if st["time"] >= now and not st["skipped"]), None)
        for i, st in enumerate(stops):
            st["passed"] = next_i is not None and i < next_i or (not running and stops[-1]["time"] < now)
        route = s.routes.get(trip[T_ROUTE], {})
        return {
            "trip_id": trip_id,
            "train": trip[T_SHORT_NAME] or self._running_labels.get(trip_id),
            "route_id": trip[T_ROUTE],
            "route_short": route.get("short_name", trip[T_ROUTE]),
            "route_name": route.get("long_name", trip[T_ROUTE]),
            "color": route.get("color", "#888888"),
            "text_color": route.get("text_color", "#FFFFFF"),
            "origin": stops[0]["name"],
            "destination": s.trip_destination(trip_id),
            "direction": self._direction(sts[0][S_STOP], sts[-1][S_STOP]),
            "delay": delay,
            "live": bool(live and known),
            "tracking": tracking and not (live and known),
            "cancelled": cancelled,
            "running": running,
            "next_index": next_i,
            "position_at": (vehicle or {}).get("timestamp"),
            "stops": stops,
            "data_as_of": self.data_as_of("tripupdates"),
            "generated_at": now,
        }

    def line_trains(self, route: str, direction: str | None = None, now: float | None = None, limit: int = 60) -> dict:
        """Every train on one line still to run today in one direction (running ones first).

        For each: train number, where it starts and ends with times, and for a running
        train its next stop and delay (Metra's prediction, else the GPS estimate).
        """
        s = self.static
        now = time.time() if now is None else now
        if not s:
            return {"route": route, "direction": direction, "trains": []}
        if route not in s.routes:
            raise KeyError(route)
        until = self.day_end(now)
        rows = []
        seen: set[tuple[str, date]] = set()
        for d in s.candidate_service_dates(now):
            base = s.service_day_start(d)
            services = s.active_services(d)
            for trip_id, trip in s.trips.items():
                if trip[T_ROUTE] != route or trip[T_SERVICE] not in services or (trip_id, d) in seen:
                    continue
                span = s.trip_spans.get(trip_id)
                sts = s.trip_stops.get(trip_id)
                if not span or not sts:
                    continue
                start, end = base + span[0], base + span[1]
                if start >= until or end < now - 3 * 3600:
                    continue
                first, last = sts[0], sts[-1]
                if direction and self._direction(first[S_STOP], last[S_STOP]) != direction:
                    continue
                live = self.realtime_live and self._rt_matches_date(trip_id, d)
                pred_first = self._predict(trip_id, first[S_SEQ], base) if live else None
                cancelled = bool(pred_first and pred_first["cancelled"])
                pred_last = self._predict(trip_id, last[S_SEQ], base) if live else None
                delay, next_stop, tracking = None, None, False
                if live:
                    delay = (pred_last or pred_first or {}).get("delay")
                gps = self._gps.get(trip_id)
                if gps and gps.get("service_date") == d and not gps.get("finished") and delay is None:
                    delay, next_stop, tracking = gps["delay"], gps["next_stop"], True
                end_est = end + (delay or 0)
                if end_est < now - 60 and not cancelled:
                    continue  # already done
                running = start + min(delay or 0, 0) <= now <= end_est
                if running and next_stop is None:
                    # first stop the train hasn't reached yet
                    for x in sts:
                        p = self._predict(trip_id, x[S_SEQ], base) if live else None
                        t = p["time"] if p and p["time"] else base + x[S_DEP] + (delay or 0)
                        if t >= now:
                            next_stop = s.stop_name(x[S_STOP])
                            break
                if cancelled and end < now:
                    continue
                seen.add((trip_id, d))
                route_info = s.routes.get(route, {})
                rows.append({
                    "trip_id": trip_id,
                    "train": trip[T_SHORT_NAME] or self._running_labels.get(trip_id),
                    "origin": s.stop_name(first[S_STOP]),
                    "origin_time": start,
                    "destination": s.trip_destination(trip_id),
                    "dest_time": end_est,
                    "dest_time_scheduled": end,
                    "delay": delay,
                    "live": bool(live and (pred_first or pred_last) and not tracking),
                    "tracking": tracking,
                    "cancelled": cancelled,
                    "running": running and not cancelled,
                    "next_stop": next_stop if running else None,
                    "direction": self._direction(first[S_STOP], last[S_STOP]),
                    "color": route_info.get("color", "#888888"),
                })
        rows.sort(key=lambda r: (not r["running"], r["origin_time"]))
        r = s.routes.get(route, {})
        return {
            "route": route, "route_short": r.get("short_name", route), "route_name": r.get("long_name", route),
            "color": r.get("color"), "text_color": r.get("text_color"), "direction": direction,
            "trains": rows[:limit], "data_as_of": self.data_as_of("tripupdates"), "generated_at": now,
        }

