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
    empty = {"home_station": None, "home_dest": None, "work_station": None, "switch_time": None, "work_days": None}
    assert us.load() == empty
    us.update({"home_station": "A", "home_dest": "C", "junk": 1})
    assert UserSettings(tmp_path / "cfg" / "settings.json").load() == {**empty, "home_station": "A", "home_dest": "C"}
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


# ---- improvements from the logic review --------------------------------------

def _vehicle(eid, trip_id, lat, lon, ts):
    e = pb.FeedEntity(id=eid)
    e.vehicle.trip.trip_id = trip_id
    e.vehicle.position.latitude, e.vehicle.position.longitude = lat, lon
    e.vehicle.vehicle.id, e.vehicle.vehicle.label = "car" + eid, "9" + eid
    e.vehicle.timestamp = int(ts)
    return e


def test_gps_tracking_estimates_delay_without_trip_update(tracker):
    # t-0800 should leave Bravo (-88.00) at 8:10. At 8:16 its GPS puts it at Bravo
    # -> about 6 min late. Metra sent no trip update for it.
    _apply(tracker, [])
    at = datetime(2026, 11, 9, 8, 16, tzinfo=TZ).timestamp()
    tracker.rt.vehicles = rtp.parse_positions(_feed(_vehicle("1", "t-0800", 41.90, -88.00, at)))
    tracker._gps = tracker._gps_estimates(at)
    est = tracker._gps["t-0800"]
    assert est["delay"] == 360 and est["at_station"] and est["next_stop"] == "Bravo"
    r = next(r for r in tracker.departures("B", now=at)["departures"] if r["trip_id"] == "t-0800")
    assert r["tracking"] and not r["live"] and r["delay"] == 360
    assert r["time"] - r["scheduled"] == 360
    train = tracker._live_trains(at)[0]
    assert train["tracking"] and train["delay"] == 360 and train["next_stop"] == "Bravo"
    from app.tracker import _status
    assert _status(r)[1] == "about 6 min late"


def test_gps_point_far_from_route_is_ignored(tracker):
    _apply(tracker, [])
    at = datetime(2026, 11, 9, 8, 5, tzinfo=TZ).timestamp()
    assert tracker._gps_estimate("t-0800", 41.50, -88.05, at) is None  # ~45 km off


def test_stale_vehicle_positions_are_dropped(tracker):
    _apply(tracker, [])
    at = datetime(2026, 11, 9, 8, 5, tzinfo=TZ).timestamp()
    tracker.rt.vehicles = rtp.parse_positions(_feed(
        _vehicle("1", "t-0800", 41.90, -88.05, at - 30),
        _vehicle("2", "t-0830", 41.90, -88.10, at - 600),  # silent for 10 min
    ))
    assert [t["trip_id"] for t in tracker._live_trains(at)] == ["t-0800"]


def test_never_shows_a_departure_before_schedule(tracker):
    _apply(tracker, [_tu("t-0800", [("A", -240)])])  # running 4 min early
    rows = {r["trip_id"]: r for r in tracker.departures("A", now=NOW)["departures"]}
    assert rows["t-0800"]["time"] == rows["t-0800"]["scheduled"] and rows["t-0800"]["delay"] == 0


def test_feed_with_old_header_timestamp_is_not_live(tracker):
    _apply(tracker, [_tu("t-0800", [("A", 300)])])
    assert tracker.realtime_live
    tracker.rt.status["tripupdates"].feed_timestamp = int(time.time()) - 600  # 10-min-old snapshot
    assert not tracker.realtime_live
    assert tracker.status()["realtime_age"] >= 600


def test_station_names_cleaned_and_duplicates_labelled(tracker):
    from app.static_gtfs import clean_name
    assert clean_name("Lake Forest.") == "Lake Forest"
    assert clean_name("Main St.") == "Main St." and clean_name("Western Ave.") == "Western Ave."
    s = tracker.static
    s.stops["A2"] = {**s.stops["A"], "id": "A2"}
    s.station_children["A2"] = ["A2"]
    s.stop_departures["A2"] = [(8 * 3600, "u-0815", 1)]
    s.version = "v2"  # labels are cached per schedule version
    names = {x["id"]: x["name"] for x in tracker.stations()}
    assert names["A"] == "Alpha (L1/L2)" and names["A2"] == "Alpha (L2)"
    assert tracker.find_station("Alpha (L2)") == "A2"


def test_responses_are_gzipped(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    import importlib
    from fastapi.testclient import TestClient

    import app.main as main
    main = importlib.reload(main)
    r = TestClient(main.app).get("/vendor/maplibre/maplibre-gl.js", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"


def test_finished_trips_are_not_tracked_or_shown(tracker):
    """Metra keeps tagging parked/deadheading trains with their old trip for hours."""
    _apply(tracker, [])
    at = datetime(2026, 11, 9, 9, 30, tzinfo=TZ).timestamp()  # t-0800 ended 8:40
    tracker.rt.vehicles = rtp.parse_positions(_feed(
        _vehicle("1", "t-0800", 41.88, -87.64, at),   # parked at Central (its last stop)
        _vehicle("2", "u-0815", 41.90, -88.05, at),   # mid-route, but 35 min past its 8:55 finish
    ))
    tracker._gps = tracker._gps_estimates(at)
    assert tracker._gps["t-0800"]["finished"]
    assert not tracker._gps["u-0815"].get("finished")  # within 45 min: maybe just very late
    assert [t["trip_id"] for t in tracker._live_trains(at)] == ["u-0815"]
    later = at + 20 * 60  # 55 min past finish
    tracker.rt.vehicles = rtp.parse_positions(_feed(_vehicle("2", "u-0815", 41.90, -88.05, later)))
    tracker._gps = tracker._gps_estimates(later)
    assert tracker._live_trains(later) == []


def test_long_finished_trip_hidden_even_when_off_the_line(tracker):
    _apply(tracker, [])
    at = datetime(2026, 11, 9, 11, 0, tzinfo=TZ).timestamp()  # t-0800 ended 8:40
    tracker.rt.vehicles = rtp.parse_positions(_feed(_vehicle("1", "t-0800", 41.70, -87.90, at)))  # in a yard, far off
    tracker._gps = tracker._gps_estimates(at)
    assert tracker._gps["t-0800"]["finished"] and tracker._live_trains(at) == []


def test_train_arriving_at_terminal_shows_it_as_next_stop(tracker):
    at = datetime(2026, 11, 9, 8, 41, tzinfo=TZ).timestamp()  # t-0800 due at Central 8:40
    assert tracker._next_stop_delay("t-0800", at) == (None, "Central")


# ---- on-time history on the board ---------------------------------------------

def _seed_history(tracker, tmp_path, days, delays, trip="t-0800", stop="A", hhmm=(8, 0), headsign="Central", route="L1"):
    from datetime import timedelta
    from app.history import History

    h = tracker.history if tracker.history.available else History(tmp_path / "h.db", 365)
    if not h.available:
        h.open()
    tracker.history = h
    rows = []
    for d, delay in zip(days, delays):
        sched = datetime(d.year, d.month, d.day, *hhmm, tzinfo=TZ).timestamp()
        cancelled = delay == "X"
        rows.append((d.isoformat(), trip, stop, route, headsign, 1, int(sched),
                     None if cancelled else int(sched + delay), None if cancelled else delay, cancelled, None))
    h.record_observations(rows)
    h._dep_cache.clear()
    return h


def test_board_shows_on_time_history(tracker, tmp_path):
    from datetime import timedelta
    # Six previous weekdays (Mon Nov 2 - Fri Nov 6 2026 + Fri Oct 30), real timestamps "now" so the 30-day window covers them.
    days = [date(2026, 11, d) for d in (2, 3, 4, 5, 6)] + [date(2026, 10, 30)]
    _seed_history(tracker, tmp_path, days, [0, 120, 600, 60, 900, "X"])
    import app.history as hist
    real = hist.time.time
    hist.time.time = lambda: NOW  # window relative to the fixture's "now"
    try:
        tracker.settings = tracker.settings.__class__(**{**tracker.settings.__dict__, "api_token": None})
        rows = {r["trip_id"]: r for r in tracker.departures("A", limit=3, now=NOW)["departures"]}
    finally:
        hist.time.time = real
    h = rows["t-0800"]["history"]
    # 5 ran (0, 2, 10, 1, 15 min late), 1 cancelled; on time (< 6 min) = 3 of 5.
    assert h["observations"] == 6 and h["cancelled"] == 1
    assert h["on_time_pct"] == 60.0 and h["avg_delay_min"] == 5.6 and h["worst_delay_min"] == 15.0
    assert rows["t-0830"]["history"] is None  # never seen


def test_board_history_needs_enough_days_and_matching_day_type(tracker, tmp_path):
    days = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]  # only 3 weekdays
    _seed_history(tracker, tmp_path, days, [0, 0, 0])
    # 5 Saturdays at the same time don't count for a weekday train.
    _seed_history(tracker, tmp_path, [date(2026, 10, d) for d in (3, 10, 17, 24, 31)], [0] * 5)
    import app.history as hist
    real = hist.time.time
    hist.time.time = lambda: NOW
    try:
        tracker.settings = tracker.settings.__class__(**{**tracker.settings.__dict__, "api_token": None})
        rows = {r["trip_id"]: r for r in tracker.departures("A", limit=3, now=NOW)["departures"]}
        stats = tracker.history.station_reliability(["A"], 365, lambda ts: datetime.fromtimestamp(ts, TZ).strftime("%H:%M"))
    finally:
        hist.time.time = real
    assert rows["t-0800"]["history"] is None
    assert {(x["day_type"], x["observations"]) for x in stats} == {("weekday", 3), ("saturday", 5)}


# ---- environment settings and the home/work commute -----------------------

def test_env_settings_parsing_and_warnings(tmp_path, monkeypatch):
    from app.config import load_settings
    secret = tmp_path / "token"
    secret.write_text("abcd1234wxyz\n")
    for k, v in {"METRA_API_TOKEN": "", "METRA_API_TOKEN_FILE": str(secret), "PORT": "9090", "HOST": "127.0.0.1",
                 "REALTIME_INTERVAL": "5", "HISTORY_DAYS": "abc", "LOG_LEVEL": "loud",
                 "WORK_DAYS": "mon,wed", "COMMUTE_SWITCH_TIME": "13:30", "HOME_STATION": "Evanston"}.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert s.api_token == "abcd1234wxyz" and s.port == 9090 and s.host == "127.0.0.1"
    assert s.realtime_interval == 30            # clamped to Metra's minimum
    assert s.history_days == 365 and s.log_level == "INFO"
    assert s.work_days == (0, 2) and s.commute_switch_time == "13:30" and s.home_station == "Evanston"
    assert len(s.warnings) == 3                 # interval, history days, log level
    summary = s.summary()
    assert "abcd1234wxyz" not in summary and "…wxyz" in summary
    monkeypatch.setenv("METRA_API_TOKEN", "direct-token-value")   # the variable wins over the file
    assert load_settings().api_token == "direct-token-value"


def _app_with_fixture(tracker, tmp_path, monkeypatch, **env):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("METRA_API_TOKEN", "")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app.main as main
    main = importlib.reload(main)
    main.tracker.static = tracker.static          # skip the download: use the fixture schedule
    return main, TestClient(main.app)


def test_commute_leg_switches_by_time_and_day(tracker, tmp_path, monkeypatch):
    main, c = _app_with_fixture(tracker, tmp_path, monkeypatch)
    assert c.put("/api/settings", json={"home_station": "A", "work_station": "B", "switch_time": "12:00"}).status_code == 200
    at = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=TZ).timestamp()
    assert main.profile(at(9, 7, 30))["leg"] == {"leg": "to_work", "from": "A", "to": "B"}     # Monday morning
    assert main.profile(at(9, 17, 0))["leg"] == {"leg": "to_home", "from": "B", "to": "A"}     # Monday evening
    assert main.profile(at(14, 7, 30))["leg"] == {"leg": "home", "from": "A", "to": None}      # Saturday
    c.put("/api/settings", json={"work_days": "mon-sat", "switch_time": "6:45"})
    p = main.profile(at(14, 7, 30))
    assert p["work_days"] == [0, 1, 2, 3, 4, 5] and p["switch_time"] == "06:45"
    assert p["leg"]["leg"] == "to_home"                                                         # after 6:45 now
    # Validation
    assert c.put("/api/settings", json={"work_station": "NOPE"}).status_code == 400
    assert c.put("/api/settings", json={"switch_time": "noon"}).status_code == 400
    assert c.put("/api/settings", json={"work_days": [9]}).status_code == 400


def test_work_and_now_endpoints(tracker, tmp_path, monkeypatch):
    main, c = _app_with_fixture(tracker, tmp_path, monkeypatch)
    assert c.get("/api/work").status_code == 404                 # nothing set yet
    c.put("/api/settings", json={"home_station": "A", "work_station": "B"})
    w = c.get("/api/work").json()
    assert w["station"]["id"] == "B" and w["to"]["id"] == "A"
    n = c.get("/api/now").json()
    assert n["leg"] in ("to_work", "to_home", "home") and n["station"]["id"] in ("A", "B")


def test_home_and_work_fall_back_to_env(tracker, tmp_path, monkeypatch):
    main, c = _app_with_fixture(tracker, tmp_path, monkeypatch,
                                HOME_STATION="alpha", WORK_STATION="Bravo", WORK_DAYS="mon-fri", COMMUTE_SWITCH_TIME="11:00")
    p = c.get("/api/settings").json()
    assert (p["home_station"], p["source"], p["work_station"], p["work_source"]) == ("A", "env", "B", "env")
    assert p["switch_time"] == "11:00" and p["work_days"] == [0, 1, 2, 3, 4]
    c.put("/api/settings", json={"work_station": "C"})          # the app overrides the env value
    p = c.get("/api/settings").json()
    assert (p["work_station"], p["work_source"]) == ("C", "app") and p["home_station"] == "A"


def test_schedule_change_alerts_are_tagged(tracker):
    from app.tracker import is_schedule_change
    special = _alert("s", pb.Alert.UNKNOWN_CAUSE, "UPN - Northwestern Football Weekend Service", route_id="L1")
    elevator = _alert("e", pb.Alert.MAINTENANCE, "Elevator out of service at Alpha", route_id="L1")
    trip = _alert("t", pb.Alert.UNKNOWN_CAUSE, "Train t-0800 will not stop at Bravo today", trip_id="t-0800")
    _apply(tracker, [], [special, elevator, trip])
    flags = {a["id"]: a["schedule_change"] for a in tracker.alerts(NOW)}
    assert flags == {"s": True, "e": False, "t": True}
    rows = {r["trip_id"]: r for r in tracker.departures("A", limit=5, now=NOW)["departures"]}
    assert rows["t-0800"]["schedule_change"].startswith("Train t-0800 will not stop")
    # Line-wide notices go to the board notice, not onto every train.
    assert rows["t-0830"]["schedule_change"] is None
    assert all(r["added"] is False for r in rows.values())
    assert not is_schedule_change(tracker.rt.alerts[1])


@pytest.mark.parametrize("header,description,expected", [
    # Real Metra alerts (Nov 2026); Metra sets effect=UNKNOWN_EFFECT on almost all of them.
    ("Metra Rock Island Line Closure the Weekend of Nov. 13-15", "The last outbound Rock Island mainline train will be Train 511", True),
    ("UPN - Northwestern Football Weekend Service", "Union Pacific North line will be operating extra service", True),
    ("NCS Train #100 On the Move - Expressing (UPDATE)", "will express from O'Hare Transfer to Western Avenue", True),
    ("NCS Inbound and Outbound Trains On the Move", "some trains may be extensively delayed", False),
    ("UPW Kedzie Station Construction", "the east end of platform 2 at Kedzie will be closed", False),
    ("ME Beginning November 21, Station Repairs at 211th Street Station Will Affect Station Entrances",
     "211th Station will have entrance closures due to station renovations", False),
    ("RID - 115th St./Morgan Park Station Construction", "The south parking lot will be closed", False),
])
def test_schedule_change_wording(header, description, expected):
    from app.tracker import is_schedule_change
    a = rtp.AlertInfo(id="x", header=header, description=description, url=None, cause="UNKNOWN_CAUSE",
                      effect="UNKNOWN_EFFECT", route_ids=[], stop_ids=[], trips=[], route_level_ids=[], periods=[])
    assert is_schedule_change(a) is expected


def test_timetable_lists_rest_of_today_only(tracker):
    tt = tracker.timetable("A", now=NOW)  # Monday 7:50
    times = [datetime.fromtimestamp(r["scheduled"], TZ) for r in tt["departures"]]
    assert [t.strftime("%H:%M") for t in times] == ["08:00", "08:15", "08:30"]
    assert all(t.date() == TODAY for t in times)  # Tuesday's trains are not included
    assert datetime.fromtimestamp(tt["until"], TZ) == datetime(2026, 11, 10, 3, 0, tzinfo=TZ)
    # With a destination: only trains that go there.
    assert [r["trip_id"] for r in tracker.timetable("A", now=NOW, to="B")["departures"]] == ["t-0800", "u-0815", "t-0830"]
    # Just after midnight, "today" still means the service day that is ending.
    late = datetime(2026, 11, 10, 1, 0, tzinfo=TZ).timestamp()
    assert datetime.fromtimestamp(tracker.day_end(late), TZ) == datetime(2026, 11, 10, 3, 0, tzinfo=TZ)


def test_map_trains_only_those_still_going_to_a_station(tracker):
    at = datetime(2026, 11, 9, 8, 5, tzinfo=TZ).timestamp()
    every = {t["trip_id"] for t in tracker.trains(at)["trains"]}
    assert "t-0800" in every
    assert "t-0800" in {t["trip_id"] for t in tracker.trains(at, to="B")["trains"]}  # Bravo still ahead
    assert "t-0800" not in {t["trip_id"] for t in tracker.trains(at, to="A")["trains"]}  # already left Alpha
    with pytest.raises(KeyError):
        tracker.trains(at, to="NOPE")
    # With a From station: only trains running From -> To.
    assert "t-0800" in {t["trip_id"] for t in tracker.trains(at, to="C", frm="A")["trains"]}
    assert "t-0800" not in {t["trip_id"] for t in tracker.trains(at, to="A", frm="C")["trains"]}


def test_train_standing_at_platform_stays_on_board_when_gps_fix_ages(tracker):
    # Same as above, but we look 2 minutes after the last GPS fix, with the train still at Bravo.
    _apply(tracker, [])
    fix = datetime(2026, 11, 9, 8, 16, tzinfo=TZ).timestamp()
    tracker.rt.vehicles = rtp.parse_positions(_feed(_vehicle("1", "t-0800", 41.90, -88.00, fix)))
    tracker._gps = tracker._gps_estimates(fix)
    later = fix + 120
    r = next((r for r in tracker.departures("B", now=later)["departures"] if r["trip_id"] == "t-0800"), None)
    assert r is not None and r["tracking"] and r["time"] >= later


def test_board_train_number_not_taken_from_next_trips_train_set(tracker):
    # Metra's update for a later trip names the train set that will run it (currently
    # running another trip). That isn't this trip's train number.
    tu = _tu("t-0830", [("A", 0)])
    tu.trip_update.vehicle.label = "1294"
    _apply(tracker, [tu])
    r = next(r for r in tracker.departures("A", now=NOW)["departures"] if r["trip_id"] == "t-0830")
    assert r["train"] is None
    # While a train is actually running the trip, its label is the train number.
    tracker.rt.vehicles = rtp.parse_positions(_feed(_vehicle("1", "t-0830", 41.90, -88.10, NOW)))
    tracker._reindex_realtime()
    r = next(r for r in tracker.departures("A", now=NOW)["departures"] if r["trip_id"] == "t-0830")
    assert r["train"] == "91"


def test_map_filter_uses_gps_delay(tracker):
    # t-0800 is due at Bravo 8:10; at 8:16 GPS shows it still at Bravo (6 min late),
    # so it hasn't left Bravo yet and stays on a map filtered to trains from Alpha to Bravo.
    _apply(tracker, [])
    at = datetime(2026, 11, 9, 8, 16, tzinfo=TZ).timestamp()
    tracker.rt.vehicles = rtp.parse_positions(_feed(_vehicle("1", "t-0800", 41.90, -88.00, at)))
    tracker._gps = tracker._gps_estimates(at)
    tracker._trains_cache = None
    assert "t-0800" in {t["trip_id"] for t in tracker.trains(at + 90, to="B", frm="A")["trains"]}


def test_settings_refuse_same_home_and_work(tracker, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.user_settings import UserSettings
    monkeypatch.setattr(main, "tracker", tracker)
    monkeypatch.setattr(main, "user_settings", UserSettings(tmp_path / "settings.json"))
    c = TestClient(main.app)
    assert c.put("/api/settings", json={"home_station": "A", "work_station": "C"}).status_code == 200
    r = c.put("/api/settings", json={"work_station": "A"})
    assert r.status_code == 400 and "can't both be Alpha" in r.json()["detail"]
    r = c.put("/api/settings", json={"home_station": "C"})
    assert r.status_code == 400
    # Nothing was changed by the refused saves.
    p = c.get("/api/settings").json()
    assert (p["home_station"], p["work_station"]) == ("A", "C")
    # Swapping both in one save is fine.
    assert c.put("/api/settings", json={"home_station": "C", "work_station": "A"}).status_code == 200


def test_destinations_are_direct_trains_nearest_first(tracker):
    d = tracker.destinations("A")
    assert [(x["id"], x["minutes"]) for x in d] == [("B", 10), ("C", 40)]
    assert {x["direction"] for x in d} == {"inbound"}  # Central is near the Loop
    assert tracker.destinations("C") == []  # last stop: nothing further on
    assert [x["id"] for x in tracker.destinations("B")] == ["C"]
