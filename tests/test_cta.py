"""CTA 'L' tests: a tiny synthetic CTA schedule and a fake Train Tracker."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import cta as cta_mod
from app.config import Settings
from app.cta import TZ, CtaTracker, group_key, parse_cta_zip, route_id
from app.cta_api import make_router
from app.main import _commute_leg
from app.user_settings import UserSettings

MON_0755 = datetime(2026, 11, 9, 7, 55, tzinfo=TZ).timestamp()   # a Monday
TUE_0020 = datetime(2026, 11, 10, 0, 20, tzinfo=TZ).timestamp()

# Stations 4xxxx, platforms 3xxxx (as CTA's file has them). Green trips run one
# way (direction North/South); the Brown trip is a round trip through the Loop
# whose sign changes from "Loop" to "Kimball". Route 22 is a bus (ignored).
FILES = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nCTA,CTA,http://x,America/Chicago\n",
    "routes.txt": "route_id,route_short_name,route_long_name,route_type,route_color,route_text_color\n"
                  "G,,Green Line,1,009b3a,ffffff\nBrn,,Brown Line,1,62361b,ffffff\n22,22,Clark,3,565a5c,ffffff\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                 "40100,Clark/Lake,41.885,-87.631,1,\n30100,Clark/Lake (Inner),41.885,-87.631,0,40100\n"
                 "30101,Clark/Lake (Outer),41.885,-87.631,0,40100\n"
                 "40200,Roosevelt,41.867,-87.626,1,\n30200,Roosevelt,41.867,-87.626,0,40200\n"
                 "40300,Ashland/63rd,41.778,-87.663,1,\n30300,Ashland/63rd,41.778,-87.663,0,40300\n"
                 "40400,Cottage Grove,41.780,-87.605,1,\n30400,Cottage Grove,41.780,-87.605,0,40400\n"
                 "40500,Belmont,41.939,-87.653,1,\n30500,Belmont,41.939,-87.653,0,40500\n"
                 "40600,Kimball,41.967,-87.713,1,\n30600,Kimball,41.967,-87.713,0,40600\n"
                 "40700,Merchandise Mart,41.888,-87.633,1,\n30700,Merchandise Mart,41.888,-87.633,0,40700\n"
                 "1500,Clark & Lake (bus),41.885,-87.631,0,\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "WK,1,1,1,1,1,0,0,20261101,20261130\n",
    "calendar_dates.txt": "service_id,date,exception_type\nWK,20261126,2\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id,block_id,shape_id,direction,wheelchair_accessible,schd_trip_id\n"
                 "G,WK,g1,0,1,SG,South,1,g1\nG,WK,g2,0,1,SG,South,1,g2\nG,WK,g3,1,1,SG,North,1,g3\n"
                 "G,WK,g4,0,1,SG,South,1,g4\nBrn,WK,b1,0,1,SB,0,1,b1\n22,WK,bus1,0,1,,North,1,bus1\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence,stop_headsign,pickup_type,shape_dist_traveled\n"
                      "g1,08:00:00,08:00:00,30100,1,Ashland/63rd,0,0\n"
                      "g1,08:05:00,08:05:00,30200,2,Ashland/63rd,0,1\n"
                      "g1,08:20:00,08:20:00,30300,3,,0,2\n"
                      "g2,08:04:00,08:04:00,30100,1,Cottage Grove,0,0\n"
                      "g2,08:09:00,08:09:00,30200,2,Cottage Grove,1,1\n"
                      "g2,08:25:00,08:25:00,30400,3,,0,2\n"
                      "g3,08:10:00,08:10:00,30200,1,Harlem/Lake,0,0\n"
                      "g3,08:15:00,08:15:00,30101,2,,0,1\n"
                      "g4,24:30:00,24:30:00,30100,1,Ashland/63rd,0,0\n"
                      "g4,24:50:00,24:50:00,30300,2,,0,1\n"
                      "b1,07:30:00,07:30:00,30600,1,Loop,0,0\n"
                      "b1,07:40:00,07:40:00,30500,2,Loop,0,1\n"
                      "b1,07:55:00,07:55:00,30700,3,Loop,0,2\n"
                      "b1,08:00:00,08:00:00,30101,4,Kimball,0,3\n"
                      "b1,08:02:00,08:02:00,30700,5,Kimball,0,4\n"
                      "b1,08:15:00,08:15:00,30500,6,Kimball,0,5\n"
                      "b1,08:25:00,08:25:00,30600,7,,0,6\n"
                      "bus1,08:00:00,08:00:00,1500,1,North,0,0\n",
    "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,shape_dist_traveled\n"
                  "SG,41.885,-87.631,1,0\nSG,41.867,-87.626,2,1\nSG,41.778,-87.663,3,2\n"
                  "SB,41.967,-87.713,1,0\nSB,41.939,-87.653,2,1\nSB,41.885,-87.631,3,2\n",
}


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in FILES.items():
            zf.writestr(name, body)
    return buf.getvalue()


@pytest.fixture()
def schedule(tmp_path: Path):
    zp = tmp_path / "cta.zip"
    zp.write_bytes(_zip_bytes())
    return parse_cta_zip(zp, "v1")


def _tracker(tmp_path, schedule, handler=None, key="k") -> CtaTracker:
    t = CtaTracker(tmp_path, key, "https://cta.example/google_transit.zip")
    t.schedule = schedule
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(handler or (lambda r: httpx.Response(500))))
    return t


def _eta(rt, dest, trdr, arr, rn, stpde="Service at Inner Loop platform", **flags):
    return {"staId": "40100", "stpDe": stpde, "rn": rn, "rt": rt, "destNm": dest, "trDr": trdr,
            "arrT": arr, "isApp": flags.get("isApp", "0"), "isSch": flags.get("isSch", "0"),
            "isDly": flags.get("isDly", "0"), "isFlt": "0"}


LIVE = {"ctatt": {"tmst": "2026-11-09T07:55:00", "errCd": "0", "errNm": None, "eta": [
    _eta("G", "Cottage Grove", "5", "2026-11-09T07:58:00", "006"),
    _eta("G", "Ashland/63rd", "5", "2026-11-09T07:56:00", "605", isApp="1"),
    _eta("G", "Ashland/63rd", "5", "2026-11-09T08:10:00", "607"),
    _eta("Brn", "Kimball", "1", "2026-11-09T08:01:00", "404", stpde="Service at Outer Loop platform"),
]}}


def run(coro):
    return asyncio.run(coro)


# ---- schedule -------------------------------------------------------------------------
def test_parses_only_the_L(schedule):
    assert set(schedule.lines) == {"G", "Brn"}
    assert schedule.lines["G"]["color"] == "#009b3a" and schedule.lines["Brn"]["short_name"] == "Brown"
    assert set(schedule.stations) == {"40100", "40200", "40300", "40400", "40500", "40600", "40700"}
    assert schedule.stations["40100"]["routes"] == ["Brn", "G"]
    assert schedule.shapes["G"][0][0] == [-87.631, 41.885]


def test_groups_green_by_direction_and_loop_lines_by_destination(schedule):
    # Both southbound Green branches share one group; the Brown round trip is
    # "Loop" until it reaches the Loop, then "Kimball".
    assert set(schedule.deps["40100"]) == {"G:5", "Brn:Kimball"}
    assert set(schedule.deps["40500"]) == {"Brn:Loop", "Brn:Kimball"}
    heads = {h for _, h in schedule.deps["40100"]["G:5"]["WK"]}
    assert heads == {"Ashland/63rd", "Cottage Grove"}
    # Last stops and no-pickup stops aren't departures.
    assert "G:5" not in schedule.deps.get("40300", {})
    assert [s for s, _ in schedule.deps["40200"]["G:5"]["WK"]] == [8 * 3600 + 5 * 60]


def test_reach_stops_where_a_round_trip_comes_back(schedule):
    assert schedule.reach[("40500", "Brn:Loop")]["40100"] == 20           # Belmont -> Clark/Lake
    assert "40600" not in schedule.reach[("40500", "Brn:Loop")]           # not Kimball the long way round
    assert schedule.reach[("40100", "Brn:Kimball")]["40500"] == 15
    assert set(schedule.reach[("40100", "G:5")]) == {"40200", "40300", "40400"}


def test_group_key_and_route_ids():
    assert group_key("G", "5", "Cottage Grove") == group_key("G", "5", "Ashland/63rd") == "G:5"
    assert group_key("Brn", "1", "Kimball") == "Brn:Kimball"
    assert route_id("brn") == route_id("Brown Line") == route_id("Brn") == "Brn"
    assert route_id("Bus 22") is None


def test_cache_roundtrip(tmp_path, schedule):
    t = CtaTracker(tmp_path, None, "")
    t.cache.save(schedule)
    again = t.cache.load()
    assert again.deps == schedule.deps and again.active_services(datetime(2026, 11, 9).date())


# ---- boards ---------------------------------------------------------------------------
def test_live_arrivals_merge_green_branches(tmp_path, schedule):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.url)
        return httpx.Response(200, json=LIVE)

    t = _tracker(tmp_path, schedule, handler)
    d = run(t.arrivals("40100", now=MON_0755))
    assert d["source"] == "live" and d["credit"].startswith("Data provided by CTA")
    green, brown = d["groups"]
    assert green["key"] == "G:5" and [x["run"] for x in green["trains"]] == ["605", "006", "607"]
    assert green["label"] == "Ashland/63rd · Cottage Grove"   # most trains first, stable
    assert green["platform"] == "Inner Loop" and green["trains"][0]["due"]
    assert brown["key"] == "Brn:Kimball" and brown["platform"] == "Outer Loop" and brown["trains"][0]["minutes"] == 6
    # A second request within 30 s reuses the answer.
    run(t.arrivals("40100", now=MON_0755))
    assert len(seen) == 1 and seen[0].params["mapid"] == "40100" and seen[0].params["key"] == "k"


def test_destination_filter_and_arrival_estimate(tmp_path, schedule):
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=LIVE))
    d = run(t.arrivals("40100", to="40500", now=MON_0755))
    assert [g["key"] for g in d["groups"]] == ["Brn:Kimball"]
    x = d["groups"][0]["trains"][0]
    assert d["groups"][0]["travel_minutes"] == 15 and x["arrives"] == x["time"] + 15 * 60


def test_schedule_board_without_a_key(tmp_path, schedule):
    t = _tracker(tmp_path, schedule, key=None)
    d = run(t.arrivals("40100", now=MON_0755))
    assert d["source"] == "schedule" and d["error"] is None
    g = {x["key"]: x for x in d["groups"]}
    assert [x["destination"] for x in g["G:5"]["trains"]] == ["Ashland/63rd", "Cottage Grove"]
    assert g["G:5"]["trains"][0]["minutes"] == 5 and not g["G:5"]["trains"][0]["live"]


def test_falls_back_to_schedule_when_train_tracker_errors(tmp_path, schedule):
    bad = {"ctatt": {"tmst": "2026-11-09T07:55:00", "errCd": "500", "errNm": "Invalid API key"}}
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=bad))
    d = run(t.arrivals("40100", now=MON_0755))
    assert d["source"] == "schedule" and "Invalid API key" in d["error"] and d["groups"]
    assert t.status()["live_error"] == d["error"]


def test_after_midnight_trains_belong_to_yesterday(tmp_path, schedule):
    t = _tracker(tmp_path, schedule, key=None)
    d = run(t.arrivals("40100", now=TUE_0020))
    g = {x["key"]: x for x in d["groups"]}
    assert g["G:5"]["trains"][0]["minutes"] == 10  # Monday's 24:30 train


def test_daily_budget_falls_back_to_schedule(tmp_path, schedule, monkeypatch):
    monkeypatch.setattr("app.cta_live.DAILY_BUDGET", 0)
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=LIVE))
    d = run(t.arrivals("40100", now=MON_0755))
    assert d["source"] == "schedule" and "budget" in d["error"]


def test_timetable_frequency_and_no_service_gaps(tmp_path, schedule):
    t = _tracker(tmp_path, schedule, key=None)
    d = t.timetable("40100", now=MON_0755)
    g = {x["key"]: x for x in d["groups"]}["G:5"]
    am = {p["key"]: p for p in g["periods"]}
    assert am["am_rush"]["min"] == am["am_rush"]["max"] == 4
    assert am["midday"]["min"] is None  # the 08:04 -> 24:30 gap is "no service", not a wait
    assert g["label"] == "Ashland/63rd · Cottage Grove" and not g["all_night"]
    assert g["first"] == datetime(2026, 11, 9, 8, 0, tzinfo=TZ).timestamp()
    assert g["last"] == datetime(2026, 11, 10, 0, 30, tzinfo=TZ).timestamp()


def test_timetable_lists_every_train_left_today(tmp_path, schedule):
    t = _tracker(tmp_path, schedule, key=None)
    d = t.timetable("40100", now=MON_0755)
    assert d["downtown"]                                       # Clark/Lake: one list, no direction switch
    rows = [(r["key"], r["destination"], datetime.fromtimestamp(r["time"], TZ).strftime("%H:%M")) for r in d["departures"]]
    assert rows == [("G:5", "Ashland/63rd", "08:00"), ("Brn:Kimball", "Kimball", "08:00"),
                    ("G:5", "Cottage Grove", "08:04"), ("G:5", "Ashland/63rd", "00:30")]
    assert all(r["time"] == r["scheduled"] and not r["live"] for r in d["departures"])


def test_timetable_directions_and_going_to(tmp_path, schedule):
    t = _tracker(tmp_path, schedule, key=None)
    d = t.timetable("40500", now=_at(7, 0))                    # Belmont, out of downtown
    assert not d["downtown"]
    assert [(r["destination"], r["direction"]) for r in d["departures"]] == [("Loop", "inbound"), ("Kimball", "outbound")]
    d = t.timetable("40100", now=MON_0755, to="40200")         # Clark/Lake -> Roosevelt
    rows = d["departures"]
    assert {r["key"] for r in rows} == {"G:5"} and all(r["arrival"] - r["time"] == 300 for r in rows)


def test_timetable_rows_say_when_they_reach_the_end_of_the_line(tmp_path, schedule):
    t = _tracker(tmp_path, schedule, key=None)
    rows = {r["destination"]: r for r in t.timetable("40500", now=_at(7, 0))["departures"]}   # Belmont
    assert rows["Kimball"]["final_arrival"] - rows["Kimball"]["time"] == 600         # 08:15 -> Kimball 08:25
    assert rows["Loop"]["final_arrival"] is not None                                 # first stop in the Loop


def test_timetable_uses_live_times_for_the_next_trains(tmp_path, schedule):
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=LIVE))
    d = run(t.timetable_live("40100", now=MON_0755))
    assert d["source"] == "live"
    g5 = [(r["run"], r["live"], datetime.fromtimestamp(r["time"], TZ).strftime("%H:%M")) for r in d["departures"] if r["key"] == "G:5"]
    # Live trains 605, 006, 607 (up to 08:10) replace the 08:00 and 08:04 scheduled ones; the
    # 00:30 scheduled train is after them and stays.
    assert g5 == [("605", True, "07:56"), ("006", True, "07:58"), ("607", True, "08:10"), (None, False, "00:30")]
    assert [r["run"] for r in d["departures"] if r["key"] == "Brn:Kimball"] == ["404"]
    times = [r["time"] for r in d["departures"]]
    assert times == sorted(times)


# ---- map, follow, alerts ------------------------------------------------------------------
def test_positions_accept_a_single_train_object(tmp_path, schedule):
    body = {"ctatt": {"tmst": "2026-11-09T07:55:00", "errCd": "0", "route": [
        {"@name": "g", "train": {"rn": "605", "destNm": "Ashland/63rd", "lat": "41.88", "lon": "-87.63", "heading": "180",
                                 "isApp": "0", "isDly": "0", "arrT": "2026-11-09T07:56:00"}},
        {"@name": "brn", "train": [{"rn": "404", "destNm": "Kimball", "lat": "41.9", "lon": "-87.64", "heading": "0"},
                                   {"rn": "405", "destNm": "Loop", "lat": "0", "lon": "0"}]},
        {"@name": "y"},
    ]}}
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=body))
    d = run(t.trains())
    assert [(x["route_id"], x["run"]) for x in d["trains"]] == [("G", "605"), ("Brn", "404")]
    assert d["trains"][0]["bearing"] == 180 and d["trains"][0]["color"] == "#009b3a"


def test_follow_unknown_train_is_a_key_error(tmp_path, schedule):
    body = {"ctatt": {"tmst": "2026-11-09T07:55:00", "errCd": "502", "errNm": "No trains"}}
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=body))
    with pytest.raises(KeyError):
        run(t.follow("999"))
    with pytest.raises(KeyError):
        run(t.follow("abc"))


def test_alerts_keep_L_lines_and_stations_only(tmp_path, schedule):
    body = {"CTAAlerts": {"Alert": [
        {"AlertId": "1", "Headline": "Bus reroute", "SeverityScore": "37",
         "ImpactedService": {"Service": {"ServiceType": "B", "ServiceId": "22"}}},
        {"AlertId": "2", "Headline": "Elevator out", "SeverityScore": "5", "MajorAlert": "0",
         "ImpactedService": {"Service": [{"ServiceType": "T", "ServiceId": "40100"}, {"ServiceType": "R", "ServiceId": "Brn"}]},
         "AlertURL": {"#cdata-section": "http://x/2"}},
        {"AlertId": "3", "Headline": "Green delays", "SeverityScore": "40",
         "ImpactedService": {"Service": [{"ServiceType": "R", "ServiceId": "G"}]}},
    ]}}
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=body))
    d = run(t.alerts())
    assert [a["id"] for a in d] == ["3", "2"]
    assert d[1]["stations"] == ["40100"] and d[1]["routes"] == ["Brn"] and d[1]["url"] == "http://x/2"


def test_key_never_reaches_the_log(caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: GET %s "200"',
                                        "https://lapi.transitchicago.com/api/1.0/ttarrivals.aspx?mapid=1&key=SECRETKEY99&outputType=JSON")
    assert "SECRETKEY99" not in caplog.text and "key=***" in caplog.text


# ---- schedule download -------------------------------------------------------------------
def test_download_then_unchanged_check_is_one_small_request(tmp_path):
    calls = []

    def handler(req: httpx.Request):
        calls.append(dict(req.headers))
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=_zip_bytes(), headers={"ETag": '"v1"', "Last-Modified": "Mon, 02 Nov 2026 10:00:00 GMT"})

    t = CtaTracker(tmp_path, None, "https://cta.example/google_transit.zip")
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert run(t.refresh_static()) is True and t.schedule.version == "Mon, 02 Nov 2026 10:00:00 GMT"
    assert run(t.refresh_static()) is False and calls[1]["if-none-match"] == '"v1"'
    assert (tmp_path / "cta" / "schedule.pickle").exists() and not (tmp_path / "cta" / "download.zip").exists()
    # A fresh start loads the cached schedule.
    assert CtaTracker(tmp_path, None, "").cache.load().version == t.schedule.version


def test_broken_download_keeps_the_current_schedule(tmp_path, schedule):
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, content=b"not a zip"))
    assert run(t.refresh_static(force=True)) is False
    assert t.schedule is schedule and "couldn't be read" in t.static_error


# ---- API: separate settings ----------------------------------------------------------------
@pytest.fixture()
def api(tmp_path, schedule):
    t = _tracker(tmp_path, schedule, key=None)
    settings = Settings(api_token=None, config_dir=tmp_path, static_url="", published_url="", realtime_base="",
                        realtime_interval=30, static_check_interval=300, port=8080)
    app = FastAPI()
    app.include_router(make_router(t, UserSettings(tmp_path / "cta-settings.json"), settings, _commute_leg))
    return TestClient(app), tmp_path


def test_cta_settings_are_saved_apart_from_metra(api):
    c, tmp = api
    r = c.put("/api/cta/settings", json={"home_station": "40500", "work_station": "40100"})
    assert r.status_code == 200 and r.json()["home_station"] == "40500"
    assert json.loads((tmp / "cta-settings.json").read_text())["work_station"] == "40100"
    assert not (tmp / "settings.json").exists()
    assert c.put("/api/cta/settings", json={"home_station": "EVANSTON"}).status_code == 400   # a Metra id
    assert c.put("/api/cta/settings", json={"work_station": "40500"}).status_code == 400     # same as home


def test_api_endpoints(api):
    c, _ = api
    assert c.get("/api/cta/arrivals", params={"station": "nope"}).status_code == 404
    assert c.get("/api/cta/arrivals", params={"station": "40100"}).json()["system"] == "cta"
    lines = c.get("/api/cta/lines").json()
    assert [l["id"] for l in lines["lines"]] == ["Brn", "G"] and lines["geojson"]["features"]
    assert c.get("/api/cta/now").status_code == 404   # no CTA home yet
    assert c.get("/api/cta/trains").json()["source"] == "none"


# ---- history: departures, gaps, delays ------------------------------------------------------
from app.cta_history import CtaHistory, is_long_wait, summarize  # noqa: E402


def _at(h, m, s=0, day=9):
    return datetime(2026, 11, day, h, m, s, tzinfo=TZ).timestamp()


def _iso(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%dT%H:%M:%S")


def _pos(*trains):
    """trains: (route, run, trDr, dest, next station, next arrival ts, delayed)"""
    routes = {}
    for rt, rn, trdr, dest, nxt, arr, dly in trains:
        routes.setdefault(rt, []).append({"rn": rn, "trDr": trdr, "destNm": dest, "nextStaId": nxt,
                                          "nextStaNm": nxt, "arrT": _iso(arr), "isDly": "1" if dly else "0",
                                          "lat": "41.9", "lon": "-87.6", "heading": "0"})
    return {"tmst": "x", "route": [{"@name": k, "train": v} for k, v in routes.items()]}


@pytest.fixture()
def recorder(tmp_path, schedule):
    t = _tracker(tmp_path, schedule)
    t.history = CtaHistory(tmp_path / "cta-history.db", 30)
    t.history.open()
    yield t
    t.history.close()


def test_a_train_leaving_a_station_is_recorded_with_its_gap(recorder):
    t = recorder
    a, _ = t.observe(_pos(("g", "605", "5", "Ashland/63rd", "40100", _at(8, 0), False)), _at(7, 59))
    assert a == []
    a, _ = t.observe(_pos(("g", "605", "5", "Ashland/63rd", "40200", _at(8, 5), False)), _at(8, 1))
    assert len(a) == 1
    sd, station, gk, run, when, gap, sched_gap, delayed, tracked = a[0]
    assert (sd, station, gk, run, when) == ("2026-11-09", "40100", "G:5", "605", int(_at(8, 0)))
    assert gap is None and sched_gap == 240 and tracked == 1     # first train seen; scheduled every 4 min
    # The Cottage Grove train 4 minutes later is in the same group: gap 4 min, not a long wait.
    t.observe(_pos(("g", "006", "5", "Cottage Grove", "40100", _at(8, 4), False)), _at(8, 3))
    a, _ = t.observe(_pos(("g", "006", "5", "Cottage Grove", "40200", _at(8, 9), False)), _at(8, 5))
    assert a[0][5] == 240 and not is_long_wait(a[0][5], a[0][6])
    # A train 26 minutes after that is a long wait.
    t.observe(_pos(("g", "607", "5", "Ashland/63rd", "40100", _at(8, 30), False)), _at(8, 29))
    a, _ = t.observe(_pos(("g", "607", "5", "Ashland/63rd", "40200", _at(8, 35), False)), _at(8, 31))
    assert a[0][5] == 26 * 60 and is_long_wait(a[0][5], a[0][6])


def test_stations_passed_between_two_checks_are_filled_in(recorder):
    t = recorder
    t.observe(_pos(("brn", "404", "1", "Kimball", "40100", _at(8, 0), False)), _at(7, 59, 30))
    a, _ = t.observe(_pos(("brn", "404", "1", "Kimball", "40500", _at(8, 15), False)), _at(8, 0, 30))
    assert [(r[1], r[8]) for r in a] == [("40100", 1), ("40700", 0)]   # Merchandise Mart skipped over
    assert a[0][4] < a[1][4] <= _at(8, 0, 30)


def test_delays_are_one_entry_that_grows(recorder):
    t = recorder
    _, d1 = t.observe(_pos(("g", "605", "5", "Ashland/63rd", "40200", _at(8, 5), True)), _at(8, 2))
    _, d2 = t.observe(_pos(("g", "605", "5", "Ashland/63rd", "40200", _at(8, 9), True)), _at(8, 6))
    t.history.record_delays(d1)
    t.history.record_delays(d2)
    rows = t.history.delays(3650)
    assert len(rows) == 1 and rows[0]["ended"] - rows[0]["started"] == 240 and rows[0]["gkey"] == "G:5"
    # The departure after a delay is marked delayed.
    a, _ = t.observe(_pos(("g", "605", "5", "Ashland/63rd", "40300", _at(8, 20), False)), _at(8, 10))
    assert a[0][1] == "40200" and a[0][7] == 1


def test_record_once_stores_rows(recorder):
    t = recorder
    bodies = [_pos(("g", "605", "5", "Ashland/63rd", "40100", _at(8, 0), False)),
              _pos(("g", "605", "5", "Ashland/63rd", "40200", _at(8, 5), False))]
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ctatt": {"errCd": "0", **bodies.pop(0)}})))
    run(t.record_once(now=_at(7, 59)))
    t._cache.clear()
    assert run(t.record_once(now=_at(8, 1))) == (1, 0)
    assert t.history.run_today("2026-11-09", "605")[0]["station"] == "40100"


def test_stats_need_five_days_then_show_on_time_share(recorder, monkeypatch):
    h = recorder.history
    rows = []
    for day in range(2, 7):  # Mon 2 - Fri 6 November
        base = _at(8, 0, day=day)
        rows += [(f"2026-11-0{day}", "40100", "G:5", f"r{day}a", int(base), 240, 240, 0, 1),
                 (f"2026-11-0{day}", "40100", "G:5", f"r{day}b", int(base + 1500), 1500, 240, 1 if day == 6 else 0, 1)]
    h.record_arrivals(rows[:8])
    hour = lambda ts: datetime.fromtimestamp(ts, TZ).hour  # noqa: E731
    assert h.group_stats("40100", "G:5", 8, "weekday", now=_at(9, 0), hour_fn=hour) is None   # 4 days
    h.record_arrivals(rows[8:])
    s = h.group_stats("40100", "G:5", 8, "weekday", now=_at(9, 0), hour_fn=hour)
    assert s["days"] == 5 and s["on_time_pct"] == 50 and s["long_waits"] == 5
    assert s["longest_wait_min"] == 25 and s["sched_gap_min"] == 4.0 and s["delayed_trains"] == 1
    assert h.group_stats("40100", "G:5", 8, "saturday", now=_at(9, 0), hour_fn=hour) is None
    # The board shows it for that group.
    recorder._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=LIVE)))
    monkeypatch.setattr(cta_mod, "_hour", lambda ts: 8)
    d = run(recorder.arrivals("40100", now=_at(9, 0)))
    g = {x["key"]: x for x in d["groups"]}
    assert g["G:5"]["history"]["on_time_pct"] == 50 and g["Brn:Kimball"]["history"] is None


def test_summarize_ignores_unknown_gaps():
    rows = [{"service_date": f"2026-11-0{d}", "gap": None, "sched_gap": 240, "delayed": 0} for d in range(1, 7)]
    assert summarize(rows) is None


# ---- double-tap sheet: passed stops ---------------------------------------------------------
FOLLOW = {"ctatt": {"tmst": "2026-11-09T08:08:00", "errCd": "0", "position": {"lat": "41.87", "lon": "-87.62", "heading": "180"},
          "eta": [{"staId": "40200", "staNm": "Roosevelt", "rn": "006", "rt": "Green Line", "destNm": "Cottage Grove",
                   "trDr": "5", "arrT": "2026-11-09T08:09:00", "isApp": "1", "isSch": "0", "isDly": "0"},
                  {"staId": "40400", "staNm": "Cottage Grove", "rn": "006", "rt": "Green Line", "destNm": "Cottage Grove",
                   "trDr": "5", "arrT": "2026-11-09T08:25:00", "isApp": "0", "isSch": "0", "isDly": "0"}]}}


def test_follow_lists_passed_stops_estimated_from_schedule(tmp_path, schedule, monkeypatch):
    monkeypatch.setattr(cta_mod.time, "time", lambda: _at(8, 8))
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=FOLLOW))
    d = run(t.follow("006"))
    assert d["key"] == "G:5" and d["route_short"] == "Green"
    assert [s["station_id"] for s in d["stops"]] == ["40200", "40400"]
    assert d["passed"] == [{"station_id": "40100", "name": "Clark/Lake", "time": _at(8, 4), "source": "estimated"}]


def test_follow_uses_tracked_times_when_recorded(recorder, monkeypatch):
    monkeypatch.setattr(cta_mod.time, "time", lambda: _at(8, 8))
    t = recorder
    t.history.record_arrivals([("2026-11-09", "40100", "G:5", "006", int(_at(8, 5)), None, 240, 0, 1)])
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=FOLLOW)))
    d = run(t.follow("006"))
    assert d["passed"][0]["time"] == _at(8, 5) and d["passed"][0]["source"] == "tracked"
