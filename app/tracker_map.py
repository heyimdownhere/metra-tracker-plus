"""Trains for the map: live positions (moved onto the drawn track), where the
schedule says trains should be when there is no live data, and the lines."""

from __future__ import annotations

import math
import time

from .static_gtfs import S_ARR, S_DEP, S_SEQ, S_STOP, T_DIRECTION, T_ROUTE, T_SERVICE, T_SHAPE, T_SHORT_NAME
from .tracker_helpers import SNAP_MAX_M, STALE_VEHICLE_SECONDS
from .tracker_stations import StationQueries
from .util import StaticJson


class MapQueries(StationQueries):
    def trains(self, now: float | None = None, to: str | None = None, frm: str | None = None) -> dict:
        """Trains for the map. With `to`, only trains that will still stop at that station;
        with `frm` too, only those running from `frm` toward `to` (your direction of travel)."""
        now = time.time() if now is None else now
        result = self._all_trains(now)
        if not to or not self.static:
            return result
        dest = set(self.static.station_children.get(to) or ([to] if to in self.static.stops else []))
        if not dest:
            raise KeyError(to)
        origin = None
        if frm:
            origin = set(self.static.station_children.get(frm) or ([frm] if frm in self.static.stops else []))
            if not origin:
                raise KeyError(frm)
        keep = [t for t in result["trains"] if self._will_stop_at(t.get("trip_id"), dest, now, origin)]
        return {**result, "to": to, "from": frm, "trains": keep}

    def _will_stop_at(self, trip_id: str | None, dest: set[str], now: float, origin: set[str] | None = None) -> bool:
        """Whether a running train still has a stop at one of `dest` ahead of it
        (and, with `origin`, stops at one of those earlier on the same trip)."""
        s = self.static
        if not trip_id or trip_id not in s.trips:
            return False
        stops = s.trip_stops.get(trip_id, [])
        stop = next((x for x in stops if x[S_STOP] in dest), None)
        if stop is None:
            return False
        if origin is not None and not any(x[S_STOP] in origin and x[S_SEQ] < stop[S_SEQ] for x in stops):
            return False
        d = s.trip_service_date(trip_id, now)
        if d is None:
            return False
        base = s.service_day_start(d)
        sched = base + stop[S_ARR]
        pred = self._predict(trip_id, stop[S_SEQ], base) if self.realtime_live else None
        if pred is None and self._gps:
            pred = self._gps_pred(trip_id, d, stop[S_SEQ], sched, now)  # no Metra prediction: use GPS delay
        if pred and (pred["skipped"] or pred["cancelled"]):
            return False
        t = pred["time"] if pred and pred["time"] else sched + ((pred or {}).get("delay") or 0)
        return t >= now - 60

    def _all_trains(self, now: float) -> dict:
        if self._trains_cache and now - self._trains_cache[0] < 10:
            # Fresh timestamp: the app uses it to correct the phone's clock.
            return {**self._trains_cache[1], "generated_at": now}
        result: dict
        if self.settings.realtime_enabled and self.rt.fresh("positions"):
            result = {"source": "realtime", "trains": self._live_trains(now)}
        else:
            result = {"source": "schedule", "trains": self._estimated_trains(now)}
        result["generated_at"] = now
        result["data_as_of"] = self.data_as_of("positions")
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

    def _snap_to_shape(self, trip_id: str | None, lat: float, lon: float) -> tuple[float, float, int]:
        """Move a GPS point onto the trip's drawn track (shapes.txt) if it's close.

        GPS fixes are often a few dozen metres off, which puts the marker beside
        the line on the map. Points further than SNAP_MAX_M stay where they are.
        """
        s = self.static
        shape = s.shapes.get(s.trips[trip_id][T_SHAPE]) if s and trip_id in s.trips else None
        if not shape or len(shape) < 2:
            return lat, lon, 0
        coslat = math.cos(math.radians(lat))
        best = None
        for (alat, alon), (blat, blon) in zip(shape, shape[1:]):
            ax, ay = (alon - lon) * 111320 * coslat, (alat - lat) * 110540
            bx, by = (blon - lon) * 111320 * coslat, (blat - lat) * 110540
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            f = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
            px, py = ax + f * dx, ay + f * dy
            d2 = px * px + py * py
            if best is None or d2 < best[0]:
                best = (d2, alat + f * (blat - alat), alon + f * (blon - alon))
        if best is None or best[0] > SNAP_MAX_M ** 2:
            return lat, lon, 0
        return best[1], best[2], round(math.sqrt(best[0]))

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
            snapped = self._snap_to_shape(trip_id, v.lat, v.lon)
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
                "lat": snapped[0],
                "lon": snapped[1],
                # Metres the GPS point was moved to sit on the drawn track (0 = shown as reported).
                "snapped_m": snapped[2],
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

    def lines_geojson(self) -> dict:
        """One LineString per distinct shape used by each route. Built once per schedule."""
        if not self.static:
            return {"type": "FeatureCollection", "features": []}
        return self._once("lines", self._build_lines)

    def lines_json(self) -> StaticJson:
        return self._once("lines_json", lambda: StaticJson(
            self.lines_geojson(), {"X-GTFS-Version": self.static.version if self.static else ""}))

    def _build_lines(self) -> dict:
        s = self.static
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

