"""Unit tests using a tiny synthetic GTFS feed and hand-built GTFS-RT messages."""

from __future__ import annotations

import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from google.transit import gtfs_realtime_pb2 as pb

from app import realtime as rtp
from app.config import Settings
from app.static_gtfs import StaticCache, parse_gtfs_zip
from app.tracker import Tracker

TZ = ZoneInfo("America/Chicago")
TODAY = date(2026, 11, 9)  # a Monday
NOW = datetime(2026, 11, 9, 7, 50, tzinfo=TZ).timestamp()

# Metra's files have spaces after commas; mimic that.
FILES = {
    "agency.txt": "agency_id, agency_name, agency_url, agency_timezone\nMETRA, Metra, http://x, America/Chicago\n",
    "routes.txt": "route_id, route_short_name, route_long_name, route_type, route_color, route_text_color\n"
                  "L1, L1, Line One, 2, 112233, FFFFFF\nL2, L2, Line Two, 2, 445566, FFFFFF\n",
    "stops.txt": "stop_id, stop_name, stop_lat, stop_lon\n"
                 "A, Alpha, 41.90, -88.10\nB, Bravo, 41.90, -88.00\nC, Central, 41.88, -87.64\n",
    "calendar.txt": "service_id, monday, tuesday, wednesday, thursday, friday, saturday, sunday, start_date, end_date\n"
                    "WK, 1, 1, 1, 1, 1, 0, 0, 20261101, 20261130\nWE, 0, 0, 0, 0, 0, 1, 1, 20261101, 20261130\n",
    "calendar_dates.txt": "service_id, date, exception_type\nWK, 20261126, 2\n",
    "trips.txt": "route_id, service_id, trip_id, trip_headsign, shape_id, direction_id\n"
                 "L1, WK, t-0800, Central, S1, 1\n"
                 "L1, WK, t-0830, Central, S1, 1\n"
                 "L1, WE, t-we, Central, S1, 1\n"
                 "L2, WK, u-0815, Central, , 1\n",
    "stop_times.txt": "trip_id, arrival_time, departure_time, stop_id, stop_sequence\n"
                      "t-0800,  08:00:00,  08:00:00, A, 1\n"
                      "t-0800,  08:10:00,  08:10:00, B, 2\n"
                      "t-0800,  08:40:00,  08:40:00, C, 3\n"
                      "t-0830,  08:30:00,  08:30:00, A, 1\n"
                      "t-0830,  08:40:00,  08:40:00, B, 2\n"
                      "t-0830,  09:10:00,  09:10:00, C, 3\n"
                      "t-we,  08:00:00,  08:00:00, A, 1\n"
                      "t-we,  08:40:00,  08:40:00, C, 2\n"
                      "u-0815,  08:15:00,  08:15:00, A, 1\n"
                      "u-0815,  ,  , B, 2\n"
                      "u-0815,  08:55:00,  08:55:00, C, 3\n",
    "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
                  "S1,41.90,-88.10,1\nS1,41.90,-88.00,2\nS1,41.88,-87.64,3\n",
}


@pytest.fixture()
def tracker(tmp_path: Path) -> Tracker:
    zp = tmp_path / "schedule.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for name, body in FILES.items():
            zf.writestr(name, body)
    settings = Settings(
        api_token="test", config_dir=tmp_path, static_url="", published_url="",
        realtime_base="", realtime_interval=30, static_check_interval=300, port=8080,
    )
    t = Tracker(settings)
    t.static = parse_gtfs_zip(zp, "v1")
    return t


def _feed(*entities) -> pb.FeedMessage:
    msg = pb.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = int(NOW)
    for e in entities:
        msg.entity.append(e)
    return msg


def _tu(trip_id, stus, relationship=pb.TripDescriptor.SCHEDULED, **trip_fields):
    e = pb.FeedEntity(id="tu-" + trip_id)
    e.trip_update.trip.trip_id = trip_id
    e.trip_update.trip.schedule_relationship = relationship
    for k, v in trip_fields.items():
        setattr(e.trip_update.trip, k, v)
    for stop_id, delay in stus:
        s = e.trip_update.stop_time_update.add()
        s.stop_id = stop_id
        s.departure.delay = delay
    return e


def _alert(aid, cause=None, header="", trip_id=None, route_id=None):
    e = pb.FeedEntity(id=aid)
    if cause is not None:
        e.alert.cause = cause
    e.alert.header_text.translation.add(text=header, language="en")
    ie = e.alert.informed_entity.add()
    if trip_id:
        ie.trip.trip_id = trip_id
    if route_id:
        ie.route_id = route_id
    return e


def _apply(t: Tracker, updates=(), alerts=()):
    t.rt.trip_updates = rtp.parse_trip_updates(_feed(*updates))
    t.rt.alerts = rtp.parse_alerts(_feed(*alerts))
    for st in t.rt.status.values():
        st.last_success = time.time()
    t._reindex_realtime()


def test_parses_padded_csv_and_calendar(tracker):
    s = tracker.static
    assert set(s.routes) == {"L1", "L2"}
    assert s.routes["L1"]["color"] == "#112233"
    assert s.active_services(TODAY) == {"WK"}
    assert s.active_services(date(2026, 11, 26)) == frozenset()  # removed by calendar_dates
    # blank non-timepoint time interpolated
    assert [x[3] for x in s.trip_stops["u-0815"]] == [8 * 3600 + 900, 8 * 3600 + 35 * 60, 8 * 3600 + 55 * 60]


def test_schedule_departures(tracker):
    tracker.settings = tracker.settings.__class__(**{**tracker.settings.__dict__, "api_token": None})
    rows = tracker.departures("A", limit=3, now=NOW)["departures"]
    assert [r["trip_id"] for r in rows] == ["t-0800", "u-0815", "t-0830"]
    assert all(not r["live"] for r in rows)
    # Final stop is arrival-only, so Central isn't a departure station.
    assert "C" not in {x["id"] for x in tracker.stations()}
    with pytest.raises(KeyError):
        tracker.departures("C", now=NOW)


def test_realtime_overrides_and_propagates(tracker):
    _apply(tracker, [_tu("t-0800", [("A", 300)])])
    rows = {r["trip_id"]: r for r in tracker.departures("B", now=NOW)["departures"]}
    r = rows["t-0800"]
    assert r["live"] and r["delay"] == 300
    assert r["time"] - r["scheduled"] == 300  # propagated downstream from A to B
    assert not rows["t-0830"]["live"]  # no RT -> schedule fallback


def test_cancelled_trip(tracker):
    _apply(tracker, [_tu("t-0830", [], relationship=pb.TripDescriptor.CANCELED)])
    rows = {r["trip_id"]: r for r in tracker.departures("A", now=NOW)["departures"]}
    assert rows["t-0830"]["cancelled"]


def test_resolves_unknown_trip_id_by_start_time(tracker):
    # New-format realtime trip_id that doesn't exist in static GTFS.
    e = _tu("NEW-FORMAT-123", [("A", 120)], route_id="L1", start_time="08:30:00", start_date="20261109")
    _apply(tracker, [e])
    rows = {r["trip_id"]: r for r in tracker.departures("A", now=NOW)["departures"]}
    assert rows["t-0830"]["live"] and rows["t-0830"]["delay"] == 120


def test_delay_reason_prefers_trip_alert_cause(tracker):
    _apply(
        tracker,
        [_tu("t-0800", [("A", 600)])],
        [
            _alert("route", pb.Alert.WEATHER, "Weather delays on L1", route_id="L1"),
            _alert("trip", pb.Alert.TECHNICAL_PROBLEM, "Train 0800 mechanical", trip_id="t-0800"),
        ],
    )
    rows = {r["trip_id"]: r for r in tracker.departures("A", now=NOW)["departures"]}
    assert rows["t-0800"]["delay_reason"] == "Technical problem"


def test_delay_reason_route_fallback_uses_text_when_cause_unknown(tracker):
    _apply(
        tracker,
        [_tu("t-0800", [("A", 600)]), _tu("t-0830", [("A", 0)])],
        [
            _alert("route", pb.Alert.UNKNOWN_CAUSE, "Freight interference near Bravo", route_id="L1"),
            # Trip-specific alert for another train must not leak via the route fallback.
            _alert("other", pb.Alert.ACCIDENT, "Other train", trip_id="u-0815"),
        ],
    )
    rows = {r["trip_id"]: r for r in tracker.departures("A", now=NOW)["departures"]}
    assert rows["t-0800"]["delay_reason"] == "Freight interference near Bravo"
    assert rows["t-0830"]["delay_reason"] is None  # on time -> no reason shown


def test_delay_reason_absent_cause_field_falls_back_to_text(tracker):
    _apply(tracker, [_tu("t-0800", [("A", 600)])], [_alert("a", None, "Signal problems", trip_id="t-0800")])
    rows = {r["trip_id"]: r for r in tracker.departures("A", now=NOW)["departures"]}
    assert rows["t-0800"]["delay_reason"] == "Signal problems"


def test_estimated_trains(tracker):
    at = datetime(2026, 11, 9, 8, 5, tzinfo=TZ).timestamp()
    trains = tracker._estimated_trains(at)
    t = next(x for x in trains if x["trip_id"] == "t-0800")
    assert -88.10 < t["lon"] < -88.00 and t["next_stop"] == "Bravo"


def test_cache_roundtrip(tracker, tmp_path):
    cache = StaticCache(tmp_path / "cfg")
    cache.save_pickle(tracker.static)
    loaded = cache.load()
    assert loaded and loaded.version == "v1" and set(loaded.trips) == set(tracker.static.trips)


def test_commute_only_trips_serving_both_stations(tracker):
    _apply(tracker, [_tu("t-0800", [("A", 300)])], [_alert("w", pb.Alert.WEATHER, "Snow", route_id="L1")])
    c = tracker.commute("A", "C", limit=3, now=NOW)
    assert [t["line"] for t in c["trips"]] == ["L1", "L2", "L1"]
    first = c["trips"][0]
    assert first["status"] == "5 min late" and first["delay_reason"] == "Weather"
    assert first["duration_min"] == 40 and first["arrival"].startswith("2026-11-09T08:45")
    assert c["next_minutes"] == 15 and "Alpha" in c["message"] and "5 min late: Weather" in c["message"]
    # Wrong direction: nothing goes from Central back to Alpha.
    with pytest.raises(KeyError):
        tracker.commute("C", "A", now=NOW)
    assert tracker.commute("B", "A", now=NOW)["trips"] == []


def test_history_records_and_summarises(tracker, tmp_path):
    from app.history import History

    h = History(tmp_path / "h.db", 30)
    h.open()
    tracker.history = h
    _apply(tracker, [_tu("t-0800", [("A", 600)]), _tu("t-0830", [], relationship=pb.TripDescriptor.CANCELED)],
           [_alert("x", pb.Alert.STRIKE, "Strike", route_id="L1")])
    sched_0800 = datetime(2026, 11, 9, 8, 0, tzinfo=TZ).timestamp()
    rows = tracker._observation_rows(sched_0800 - 600)
    h.record_observations(rows)
    h.record_alerts(tracker.rt.alerts)
    by_trip_stop = {(r[1], r[2]): r for r in rows}
    assert by_trip_stop[("t-0800", "A")][8] == 600 and by_trip_stop[("t-0800", "A")][10] == "Strike"
    assert by_trip_stop[("t-0830", "A")][9] is True
    # Second poll with a smaller delay updates the row but keeps the max.
    _apply(tracker, [_tu("t-0800", [("A", 120)])])
    h.record_observations(tracker._observation_rows(sched_0800 - 300))
    obs = {(r["trip_id"], r["stop_id"]): r for r in h.iter_observations(3650)}
    assert obs[("t-0800", "A")]["delay"] == 120 and obs[("t-0800", "A")]["max_delay"] == 600
    stats = h.station_reliability(["A"], 3650, lambda ts: datetime.fromtimestamp(ts, TZ).strftime("%H:%M"))
    by_time = {s["scheduled_time"]: s for s in stats}
    assert by_time["08:00"]["on_time_pct"] == 100.0 and by_time["08:30"]["cancelled"] == 1
    assert h.alerts(3650)[0]["header"] == "Strike"
    assert h.stats()["observations"] == len(obs)


def test_route_fallback_ignores_standing_notices(tracker):
    elevator = _alert("e", None, "Alpha elevator out of service", route_id="L1")
    elevator.alert.effect = pb.Alert.ACCESSIBILITY_ISSUE
    _apply(tracker, [_tu("t-0800", [("A", 600)])], [
        elevator,
        _alert("c", None, "Bravo Station Construction", route_id="L1"),
    ])
    rows = {r["trip_id"]: r for r in tracker.departures("A", now=NOW)["departures"]}
    assert rows["t-0800"]["delay"] == 600 and rows["t-0800"]["delay_reason"] is None
    # ...but a line-wide alert that talks about delays is used.
    _apply(tracker, [_tu("t-0800", [("A", 600)])], [_alert("d", None, "L1 trains delayed 10-15 minutes", route_id="L1")])
    rows = {r["trip_id"]: r for r in tracker.departures("A", now=NOW)["departures"]}
    assert rows["t-0800"]["delay_reason"] == "L1 trains delayed 10-15 minutes"


def test_live_trains_skip_vehicles_without_trip(tracker):
    def veh(eid, trip_id=None, label=None):
        e = pb.FeedEntity(id=eid)
        e.vehicle.position.latitude, e.vehicle.position.longitude = 41.9, -88.05
        e.vehicle.vehicle.id = "car" + eid
        if label:
            e.vehicle.vehicle.label = label
        if trip_id:
            e.vehicle.trip.trip_id = trip_id
        return e
    tracker.rt.vehicles = rtp.parse_positions(_feed(veh("1", "t-0800", "1234"), veh("2"), veh("3")))
    trains = tracker._live_trains(NOW)
    assert [t["train"] for t in trains] == ["1234"] and trains[0]["route_id"] == "L1"


def test_departures_filtered_by_destination(tracker):
    tracker.settings = tracker.settings.__class__(**{**tracker.settings.__dict__, "api_token": None})
    d = tracker.departures("A", limit=3, now=NOW, to="B")
    assert d["to"]["name"] == "Bravo"
    assert [r["trip_id"] for r in d["departures"]] == ["t-0800", "u-0815", "t-0830"]
    first = d["departures"][0]
    assert first["arrival"] - first["time"] == 600
    assert tracker.departures("B", now=NOW, to="A")["departures"] == []  # wrong direction
    with pytest.raises(KeyError):
        tracker.departures("A", now=NOW, to="NOPE")


def test_final_station_eta_uses_live_delay(tracker):
    _apply(tracker, [_tu("t-0800", [("A", 300)])])
    rows = {r["trip_id"]: r for r in tracker.departures("A", now=NOW)["departures"]}
    live, sched = rows["t-0800"], rows["t-0830"]
    assert live["final_stop"] == "Central"
    assert live["final_arrival"] - live["final_arrival_scheduled"] == 300  # 8:40 + 5 min
    assert datetime.fromtimestamp(live["final_arrival_scheduled"], TZ).strftime("%H:%M") == "08:40"
    assert sched["final_arrival"] == sched["final_arrival_scheduled"]  # no realtime -> schedule


def test_user_settings_roundtrip(tmp_path):
    from app.user_settings import UserSettings

    us = UserSettings(tmp_path / "cfg" / "settings.json")
    assert us.load() == {"home_station": None, "home_dest": None}
    us.update({"home_station": "A", "home_dest": "C", "junk": 1})
    assert UserSettings(tmp_path / "cfg" / "settings.json").load() == {"home_station": "A", "home_dest": "C"}
    us.update({"home_dest": None})
    assert us.load()["home_dest"] is None


def test_direction_flag_to_or_from_downtown(tracker):
    # Fixture stops: Alpha (far west) -> Bravo -> Central (downtown Chicago).
    tracker.settings = tracker.settings.__class__(**{**tracker.settings.__dict__, "api_token": None})
    from app.tracker import normalize_direction

    assert normalize_direction("to") == "inbound" and normalize_direction("From-Downtown") == "outbound"
    assert normalize_direction(None) is None and normalize_direction("both") is None
    with pytest.raises(ValueError):
        normalize_direction("sideways")
    rows = tracker.departures("A", limit=3, now=NOW, direction="inbound")["departures"]
    assert len(rows) == 3 and all(r["direction"] == "inbound" for r in rows)
    assert tracker.departures("A", now=NOW, direction="outbound")["departures"] == []
    n = tracker.next_trains("alpha", "inbound", limit=2, now=NOW)  # name lookup, any case
    assert n["station"]["id"] == "A" and n["direction"] == "inbound" and len(n["trains"]) == 2
    assert n["trains"][0]["final_stop"] == "Central" and n["trains"][0]["minutes_until"] == 10
    assert n["message"].startswith("Alpha toward downtown: 8:00 AM to Central (scheduled)")
    with pytest.raises(KeyError):
        tracker.next_trains("Nowhere", None, now=NOW)


def test_realtime_polls_never_faster_than_30s(tracker, monkeypatch):
    import asyncio

    calls = []

    async def fake_fetch(name):
        calls.append(name)
        return None

    monkeypatch.setattr(tracker, "_fetch_feed", fake_fetch)
    clock = [1000.0]
    monkeypatch.setattr("app.tracker.time.monotonic", lambda: clock[0])

    async def run():
        assert await tracker.refresh_realtime() is True
        for _ in range(20):  # hammering it does nothing until 30 s have passed
            clock[0] += 1
            assert await tracker.refresh_realtime() is False
        clock[0] += 10  # now 30 s after the first poll
        assert await tracker.refresh_realtime() is True

    asyncio.run(run())
    assert len(calls) == 6 and tracker.rt_polls == 2  # 3 feeds x 2 polls


def test_api_requests_do_not_poll_metra(tracker, monkeypatch):
    """Board, map, alerts and /api/next only read the cached snapshot."""
    def boom(*a, **k):
        raise AssertionError("an API read tried to contact Metra")

    monkeypatch.setattr(tracker, "_fetch_feed", boom)
    monkeypatch.setattr(tracker, "refresh_realtime", boom)
    monkeypatch.setattr(tracker, "refresh_static", boom)
    for _ in range(50):
        tracker.departures("A", now=NOW)
        tracker.next_trains("A", "inbound", now=NOW)
        tracker.trains(NOW)
        tracker.alerts(NOW)
        tracker.commute("A", "C", now=NOW)


# ---- regression tests from code review -------------------------------------

def test_ambiguous_station_name_is_rejected_not_guessed(tracker):
    from app.tracker import AmbiguousStation

    s = tracker.static
    # Add a second "Alpha" on another line, like Metra's two Lake Forest stations.
    s.stops["A2"] = {**s.stops["A"], "id": "A2", "lat": 42.0}
    s.station_children["A2"] = ["A2"]
    with pytest.raises(AmbiguousStation) as exc:
        tracker.find_station("Alpha")
    assert {o["id"] for o in exc.value.options} == {"A", "A2"}
    assert tracker.find_station("A2") == "A2"  # ids still work


def test_added_trip_final_stop_is_not_a_departure(tracker):
    e = pb.FeedEntity(id="x")
    e.trip_update.trip.trip_id = "EXTRA-1"
    e.trip_update.trip.route_id = "L1"
    e.trip_update.trip.schedule_relationship = pb.TripDescriptor.ADDED
    for stop, t in (("B", NOW + 600), ("C", NOW + 1800)):
        u = e.trip_update.stop_time_update.add()
        u.stop_id = stop
        u.arrival.time = int(t)
    _apply(tracker, [e])
    at_b = tracker._added_departures({"B"}, NOW)
    assert len(at_b) == 1 and at_b[0]["direction"] == "inbound" and at_b[0]["final_stop"] == "Central"
    assert tracker._added_departures({"C"}, NOW) == []  # ends at C: not a departure


def test_poll_loop_never_skips_a_poll(tracker, monkeypatch):
    """The loop must wait on the guard's own clock, so polls land every ~30s, not 60s."""
    import asyncio

    clock = [1000.0]
    polls = []
    monkeypatch.setattr("app.tracker.time.monotonic", lambda: clock[0])

    async def fake_fetch(name):
        return None

    async def fake_sleep(seconds):
        clock[0] += seconds - 1e-6  # wake a hair early, as event loops may
        if len(polls) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(tracker, "_fetch_feed", fake_fetch)
    orig = tracker.refresh_realtime

    async def counted():
        r = await orig()
        if r:
            polls.append(clock[0])
        return r

    monkeypatch.setattr(tracker, "refresh_realtime", counted)
    monkeypatch.setattr("app.tracker.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tracker._realtime_loop())
    gaps = [b - a for a, b in zip(polls, polls[1:])]
    assert len(polls) == 4 and all(30 <= g < 31 for g in gaps), gaps


def test_endpoints_return_503_not_500_before_schedule_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("METRA_API_TOKEN", "")
    import importlib
    from fastapi.testclient import TestClient

    import app.main as main
    main = importlib.reload(main)
    c = TestClient(main.app)  # no lifespan: schedule never loads
    assert c.put("/api/settings", json={"home_station": "typo"}).status_code == 503
    assert not (tmp_path / "settings.json").exists()
    main.tracker.history._db = object()  # pretend history is open
    try:
        assert c.get("/api/history/station?station=X").status_code == 503
    finally:
        main.tracker.history._db = None
    assert c.get("/api/next?station=X").status_code == 503
    assert c.get("/api/next?station=X&direction=sideways").status_code in (400, 503)
