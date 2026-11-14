"""Station queries: names, the stations list, where you can go from a
station, and which way a train is heading."""

from __future__ import annotations

from .static_gtfs import S_ARR, S_SEQ, S_STOP, T_ROUTE, _dist_m
from .tracker_core import TrackerCore
from .tracker_helpers import LOOP, AmbiguousStation, _norm_name
from .util import StaticJson


class StationQueries(TrackerCore):
    def station_label(self, station_id: str) -> str:
        """Display name; stations sharing a name get their line(s), e.g. 'Lake Forest (UP-N)'."""
        s = self.static
        return self._once("labels", self._build_labels).get(station_id) or s.stop_name(station_id) or station_id

    def _build_labels(self) -> dict[str, str]:
        s = self.static
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
        return labels

    # ---- public queries --------------------------------------------------
    def stations(self) -> list[dict]:
        """Every station with its lines, A-Z. Built once per schedule."""
        if not self.static:
            return []
        return self._once("stations", self._build_stations)

    def stations_json(self) -> StaticJson:
        return self._once("stations_json", lambda: StaticJson(self.stations()))

    def _build_stations(self) -> list[dict]:
        s = self.static
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

    def destinations(self, station_id: str) -> list[dict]:
        """Stations you can reach from `station_id` on a direct train, nearest first (by distance).

        Each: {id, minutes (shortest scheduled ride), direction (inbound/outbound)}.
        Built from every trip in the schedule that stops here, so branches the
        station isn't on are left out. Cached per schedule version.
        """
        s = self.static
        if not s:
            return []
        parent, memo = self._once("destinations", lambda: (
            {c: sid for sid, kids in s.station_children.items() for c in kids}, {}))
        if station_id in memo:
            return memo[station_id]
        try:
            here = set(self._station_stops(station_id))
        except KeyError:
            if station_id in s.stops:
                return []  # a stop no train leaves from
            raise
        best: dict[str, tuple[int, str | None]] = {}
        seen_trips: set[str] = set()
        for stop_id in here:
            for dep, trip_id, seq in s.stop_departures.get(stop_id, []):
                if trip_id in seen_trips:
                    continue
                seen_trips.add(trip_id)
                sts = s.trip_stops.get(trip_id, [])
                direction = self._direction(stop_id, sts[-1][S_STOP] if sts else None)
                for x in sts:
                    if x[S_SEQ] <= seq:
                        continue
                    dest = parent.get(x[S_STOP], x[S_STOP])
                    if dest == station_id or x[S_STOP] in here:
                        continue
                    mins = max(0, round((x[S_ARR] - dep) / 60))
                    if dest not in best or mins < best[dest][0]:
                        best[dest] = (mins, direction)
        # Order by distance from here, which follows the line; ride times jump around
        # because express trains skip stops.
        home = s.stops.get(station_id) or s.stops[next(iter(here))]

        def dist(sid: str) -> float:
            st = s.stops.get(sid)
            return _dist_m(home["lat"], home["lon"], st["lat"], st["lon"]) if st else float("inf")

        out = [{"id": d, "minutes": m, "direction": direc} for d, (m, direc) in best.items()]
        out.sort(key=lambda r: (dist(str(r["id"])), str(r["id"])))
        memo[station_id] = out
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

    def _station_of(self, stop_id: str | None) -> str | None:
        """The station (parent) id a stop belongs to, as used by /api/stations."""
        s = self.static
        if not s or not stop_id:
            return None
        parent = (s.stops.get(stop_id) or {}).get("parent")
        return parent if parent in s.station_children else stop_id

    def _station_stops(self, station_id: str) -> list[str]:
        s = self.static
        stop_ids = s.station_children.get(station_id)
        if stop_ids is None and (station_id in s.stop_departures):
            stop_ids = [station_id]
        if not stop_ids:
            raise KeyError(station_id)
        return stop_ids
