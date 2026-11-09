"""GTFS-Realtime parsing into plain Python structures."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from google.transit import gtfs_realtime_pb2 as rt

FEEDS = ("positions", "tripupdates", "alerts")

SCHEDULED = rt.TripUpdate.StopTimeUpdate.SCHEDULED
SKIPPED = rt.TripUpdate.StopTimeUpdate.SKIPPED
NO_DATA = rt.TripUpdate.StopTimeUpdate.NO_DATA
TRIP_CANCELED = rt.TripDescriptor.CANCELED
TRIP_ADDED = rt.TripDescriptor.ADDED


@dataclass
class TripRef:
    trip_id: str | None
    route_id: str | None
    direction_id: int | None
    start_time: str | None
    start_date: str | None
    relationship: int


@dataclass
class StopUpdate:
    stop_sequence: int | None
    stop_id: str | None
    arrival_time: int | None
    arrival_delay: int | None
    departure_time: int | None
    departure_delay: int | None
    relationship: int
    assigned_stop_id: str | None


@dataclass
class TripUpdateInfo:
    trip: TripRef
    vehicle_label: str | None
    delay: int | None
    timestamp: int | None
    stop_updates: list[StopUpdate]


@dataclass
class VehicleInfo:
    key: str
    trip: TripRef | None
    label: str | None
    lat: float
    lon: float
    bearing: float | None
    speed: float | None
    stop_id: str | None
    status: int | None
    timestamp: int | None


@dataclass
class AlertInfo:
    id: str
    header: str
    description: str
    url: str | None
    cause: str
    effect: str
    route_ids: list[str]  # every route mentioned (for display)
    stop_ids: list[str]
    trips: list[TripRef]  # informed entities naming a specific trip
    route_level_ids: list[str]  # informed entities naming a route but no trip
    periods: list[tuple[int | None, int | None]]

    def active(self, now: float) -> bool:
        if not self.periods:
            return True
        return any((s is None or s <= now) and (e is None or now <= e) for s, e in self.periods)


@dataclass
class FeedStatus:
    last_attempt: float | None = None
    last_success: float | None = None
    feed_timestamp: int | None = None
    error: str | None = None
    entities: int = 0


@dataclass
class RealtimeSnapshot:
    vehicles: list[VehicleInfo] = field(default_factory=list)
    trip_updates: list[TripUpdateInfo] = field(default_factory=list)
    alerts: list[AlertInfo] = field(default_factory=list)
    status: dict[str, FeedStatus] = field(default_factory=lambda: {f: FeedStatus() for f in FEEDS})

    def fresh(self, feed: str, max_age: float = 300) -> bool:
        st = self.status.get(feed)
        return bool(st and st.last_success and time.time() - st.last_success < max_age)


def _opt(msg, name):
    return getattr(msg, name) if msg.HasField(name) else None


def _trip_ref(td) -> TripRef:
    return TripRef(
        trip_id=_opt(td, "trip_id"),
        route_id=_opt(td, "route_id"),
        direction_id=_opt(td, "direction_id"),
        start_time=_opt(td, "start_time"),
        start_date=_opt(td, "start_date"),
        relationship=td.schedule_relationship,
    )


def _text(ts) -> str:
    """Pick English (or the first) translation of a TranslatedString."""
    if not ts.translation:
        return ""
    for tr in ts.translation:
        if tr.language.lower().startswith("en"):
            return tr.text
    for tr in ts.translation:
        if not tr.language:
            return tr.text
    return ts.translation[0].text


def parse_feed(content: bytes) -> rt.FeedMessage:
    msg = rt.FeedMessage()
    msg.ParseFromString(content)
    return msg


def parse_positions(msg: rt.FeedMessage) -> list[VehicleInfo]:
    out = []
    for ent in msg.entity:
        if not ent.HasField("vehicle"):
            continue
        v = ent.vehicle
        if not v.HasField("position"):
            continue
        pos = v.position
        vd = v.vehicle if v.HasField("vehicle") else None
        label = (_opt(vd, "label") or _opt(vd, "id")) if vd else None
        out.append(
            VehicleInfo(
                key=(vd.id if vd and vd.id else None) or ent.id,
                trip=_trip_ref(v.trip) if v.HasField("trip") else None,
                label=label,
                lat=pos.latitude,
                lon=pos.longitude,
                bearing=_opt(pos, "bearing"),
                speed=_opt(pos, "speed"),
                stop_id=_opt(v, "stop_id"),
                status=_opt(v, "current_status"),
                timestamp=_opt(v, "timestamp"),
            )
        )
    return out


def parse_trip_updates(msg: rt.FeedMessage) -> list[TripUpdateInfo]:
    out = []
    for ent in msg.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        stus = []
        for stu in tu.stop_time_update:
            arr = stu.arrival if stu.HasField("arrival") else None
            dep = stu.departure if stu.HasField("departure") else None
            props = stu.stop_time_properties if stu.HasField("stop_time_properties") else None
            stus.append(
                StopUpdate(
                    stop_sequence=_opt(stu, "stop_sequence"),
                    stop_id=_opt(stu, "stop_id"),
                    arrival_time=(_opt(arr, "time") or None) if arr else None,
                    arrival_delay=_opt(arr, "delay") if arr else None,
                    departure_time=(_opt(dep, "time") or None) if dep else None,
                    departure_delay=_opt(dep, "delay") if dep else None,
                    relationship=stu.schedule_relationship,
                    assigned_stop_id=(_opt(props, "assigned_stop_id") if props else None),
                )
            )
        vd = tu.vehicle if tu.HasField("vehicle") else None
        out.append(
            TripUpdateInfo(
                trip=_trip_ref(tu.trip),
                vehicle_label=((_opt(vd, "label") or _opt(vd, "id")) if vd else None),
                delay=_opt(tu, "delay"),
                timestamp=_opt(tu, "timestamp"),
                stop_updates=stus,
            )
        )
    return out


def parse_alerts(msg: rt.FeedMessage) -> list[AlertInfo]:
    out = []
    for ent in msg.entity:
        if not ent.HasField("alert") or ent.is_deleted:
            continue
        a = ent.alert
        routes, stops, trips, route_level = [], [], [], []
        for ie in a.informed_entity:
            if ie.route_id and ie.route_id not in routes:
                routes.append(ie.route_id)
            if ie.HasField("trip"):
                trips.append(_trip_ref(ie.trip))
                if ie.trip.route_id and ie.trip.route_id not in routes:
                    routes.append(ie.trip.route_id)
            elif ie.route_id and ie.route_id not in route_level:
                route_level.append(ie.route_id)
            if ie.stop_id and ie.stop_id not in stops:
                stops.append(ie.stop_id)
        periods = [
            (p.start if p.HasField("start") and p.start else None, p.end if p.HasField("end") and p.end else None)
            for p in a.active_period
        ]
        out.append(
            AlertInfo(
                id=ent.id,
                header=_text(a.header_text).strip(),
                description=_text(a.description_text).strip(),
                url=_text(a.url).strip() or None,
                cause=rt.Alert.Cause.Name(a.cause) if a.HasField("cause") else "UNKNOWN_CAUSE",
                effect=rt.Alert.Effect.Name(a.effect) if a.HasField("effect") else "UNKNOWN_EFFECT",
                route_ids=routes,
                stop_ids=stops,
                trips=trips,
                route_level_ids=route_level,
                periods=periods,
            )
        )
    return out
