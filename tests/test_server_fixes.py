"""Tests for the server review fixes: exports, map tiles and assets, CTA history
layout, CTA live requests, safety layers, caching and the shared app layout."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import pickle
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pmtiles.reader import MmapSource

from app import cta_live
from app.basemap import BaseMap, extract, valid_asset
from app.cta_history import CtaHistory
from app.history import History
from app.util import RedactSecrets, SingleFlight, TTLCache, load_cache_file
from tests.test_basemap import BBOX, _payload, world  # noqa: F401  (fixture)
from tests.test_cta import LIVE, MON_0755, TZ, _at, _pos, _tracker, run, schedule  # noqa: F401  (fixture)
from tests.test_tracker import _app_with_fixture, tracker  # noqa: F401  (fixture)


# ---- 1. exports are streamed and capped ------------------------------------------------------
def _cta_rows(n, start, station="40100", gkey="G:5"):
    return [("2026-11-09", station, gkey, str(i), int(start + i * 60), 60, 60, 0, 1) for i in range(n)]


def test_cta_export_is_capped_to_the_newest_rows(tmp_path):
    h = CtaHistory(tmp_path / "c.db", 30)
    h.open()
    start = time.time() - 3600
    h.record_arrivals(_cta_rows(50, start) + _cta_rows(5, start, station="40200"))
    info, rows = h.export_arrivals(0, 2**40, limit=20)
    rows = list(rows)
    assert info == {"total": 55, "rows": 20, "truncated": True, "limit": 20}
    assert len(rows) == 20 and rows == sorted(rows, key=lambda r: r["t"])          # oldest first
    assert rows[-1]["t"] == int(start + 49 * 60)                                     # the newest kept
    info, rows = h.export_arrivals(0, 2**40, limit=20, station="40200")
    assert info["total"] == 5 and not info["truncated"] and {r["station"] for r in rows} == {"40200"}
    h.close()


def test_export_endpoint_says_when_it_was_cut(tracker, tmp_path, monkeypatch):  # noqa: F811
    main, c = _app_with_fixture(tracker, tmp_path, monkeypatch, EXPORT_MAX_ROWS="1000")
    h = History(tmp_path / "h.db", 3650)
    h.open()
    now = int(time.time())
    h.record_observations([("2026-11-09", f"t{i}", "A", "L1", "Central", 1, now - i, now - i, 0, False, None)
                           for i in range(1500)])
    main.tracker.history = h
    r = c.get("/api/history/export.csv?days=2")
    lines = r.text.strip().splitlines()
    assert r.status_code == 200 and r.headers["x-export-truncated"] == "true"
    assert r.headers["x-export-total-rows"] == "1500" and len(lines) == 1001       # header + 1,000 rows
    assert r.headers["cache-control"] == "no-store"
    r = c.get("/api/history/export.csv?days=1&end=2020-01-01")
    assert r.headers["x-export-total-rows"] == "0" and r.headers["x-export-truncated"] == "false"
    h.close()


def test_metra_export_reads_in_batches_on_its_own_connection(tmp_path):
    h = History(tmp_path / "h.db", 3650)
    h.open()
    now = int(time.time())
    h.record_observations([("2026-11-09", f"t{i}", "A", "L1", "C", 1, now + i, now, 0, False, None) for i in range(12)])
    info, rows = h.export_observations(0, 2**40, limit=100)
    first = next(rows)
    # Recording keeps working while an export is half-read.
    h.record_observations([("2026-11-09", "late", "A", "L1", "C", 1, now, now, 0, False, None)])
    assert first["trip_id"] == "t0" and len(list(rows)) == 11 and info["total"] == 12
    h.close()


# ---- 2 and 5. map tiles and assets -------------------------------------------------------------
def test_tile_index_pages_are_decoded_once(world, tmp_path, monkeypatch):  # noqa: F811
    bm = BaseMap(tmp_path, enabled=False)
    with open(world, "rb") as f:
        extract(MmapSource(f), bm.path, BBOX, max_zoom=7)
    calls = []
    import app.basemap as basemap_mod
    real = basemap_mod.deserialize_directory
    monkeypatch.setattr(basemap_mod, "deserialize_directory", lambda b: calls.append(1) or real(b))
    data, enc = bm.tile(7, 32, 47)
    assert enc == "gzip" and gzip.decompress(data) == gzip.decompress(_payload(7, 32, 47))
    first = len(calls)
    for _ in range(20):
        bm.tile(7, 32, 47)
    assert first >= 1 and len(calls) == first       # no decoding after the first lookup


def test_a_new_map_file_is_picked_up_and_old_readers_finish(world, tmp_path):  # noqa: F811
    bm = BaseMap(tmp_path, enabled=False)
    with open(world, "rb") as f:
        extract(MmapSource(f), bm.path, BBOX, max_zoom=7)
    old = bm._open()
    tile_before = bytes(old.tile(7, 32, 47))
    with open(world, "rb") as f:                                # a fresh download replaces the file
        extract(MmapSource(f), bm.path, BBOX, max_zoom=6)
    assert bytes(old.tile(7, 32, 47)) == tile_before           # a request mid-read still works
    assert bm._open() is not old and bm.tile(7, 32, 47) == (None, None)


def test_only_real_font_ranges_are_fetched_and_failures_wait(tmp_path, monkeypatch):
    assert valid_asset("fonts/Noto Sans Regular/0-255.pbf") and valid_asset("fonts/Noto Sans Italic/65280-65535.pbf")
    assert valid_asset("sprites/v4/dark@2x.png")
    for bad in ("fonts/Noto Sans Regular/1-2.pbf", "fonts/Noto Sans Regular/0-256.pbf",
                "fonts/Noto Sans Regular/65536-65791.pbf", "fonts/Noto Sans Regular/99999-100254.pbf",
                "fonts/Evil/0-255.pbf", "../settings.json", "sprites/v4/dark.svg"):
        assert not valid_asset(bad), bad
    bm = BaseMap(tmp_path, enabled=False)
    tries = []

    def broken(path, client=None):
        tries.append(path)
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(bm, "_fetch_asset", broken)
    assert bm.asset("fonts/Noto Sans Regular/256-511.pbf") is None
    assert bm.asset("fonts/Noto Sans Regular/256-511.pbf") is None      # remembered: not asked again
    assert bm.asset("fonts/Noto Sans Regular/1-2.pbf") is None           # never asked
    assert tries == ["fonts/Noto Sans Regular/256-511.pbf"]


# ---- 3. CTA history: small layout, old databases moved over ------------------------------------
V1 = """
CREATE TABLE arrivals (service_date TEXT NOT NULL, station TEXT NOT NULL, gkey TEXT NOT NULL, run TEXT NOT NULL,
    t INTEGER NOT NULL, gap INTEGER, sched_gap INTEGER, delayed INTEGER NOT NULL DEFAULT 0,
    tracked INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (service_date, station, gkey, run, t));
CREATE INDEX idx_cta_arr_station ON arrivals (station, gkey, t);
CREATE INDEX idx_cta_arr_run ON arrivals (service_date, run);
CREATE INDEX idx_cta_arr_t ON arrivals (t);
CREATE TABLE delays (service_date TEXT NOT NULL, route TEXT NOT NULL, gkey TEXT NOT NULL, run TEXT NOT NULL,
    station TEXT, station_name TEXT, started INTEGER NOT NULL, ended INTEGER NOT NULL,
    PRIMARY KEY (service_date, run, started));
"""


def test_old_cta_history_is_moved_to_the_small_layout(tmp_path):
    path = tmp_path / "cta-history.db"
    db = sqlite3.connect(path)
    db.executescript(V1)
    t0 = int(_at(8, 0))
    db.executemany("INSERT INTO arrivals VALUES (?,?,?,?,?,?,?,?,?)",
                   [("2026-11-09", "40100", "G:5", "605", t0, None, 240, 0, 1),
                    ("2026-11-09", "40200", "G:5", "605", t0 + 300, None, 240, 1, 0),
                    ("2026-11-09", "40100", "Brn:Kimball", "404", t0 + 60, 600, 480, 0, 1)])
    db.execute("INSERT INTO delays VALUES ('2026-11-09','G','G:5','605','40200','Roosevelt',?,?)", (t0, t0 + 120))
    db.commit()
    db.close()
    h = CtaHistory(path, 3650)
    h.open()
    assert h._db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert [r["station"] for r in h.run_today("2026-11-09", "605")] == ["40100", "40200"]
    _, rows = h.export_arrivals(0, 2**40, 100)
    assert [(r["gkey"], r["delayed"], r["tracked"], r["service_date"]) for r in rows] == [
        ("G:5", 0, 1, "2026-11-09"), ("Brn:Kimball", 0, 1, "2026-11-09"), ("G:5", 1, 0, "2026-11-09")]
    assert len(h.delays(3650)) == 1
    tables = {r[0] for r in h._db.execute("SELECT name FROM sqlite_master")}
    assert "arrivals_v1" not in tables and "idx_cta_arr_run" not in tables
    h.close()
    CtaHistory(path, 3650).open()   # opening again changes nothing


def test_cta_history_rows_are_small(tmp_path):
    h = CtaHistory(tmp_path / "c.db", 30)
    h.open()
    start = time.time() - 86400
    rows = []
    for s in range(60):
        rows += _cta_rows(400, start, station=str(40000 + s * 10), gkey=["G:5", "G:1", "Red:1", "Brn:Kimball"][s % 4])
    h.record_arrivals(rows)
    h._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    per_row = (tmp_path / "c.db").stat().st_size / len(rows)
    assert per_row < 80, per_row          # the old layout took about 170 bytes a row
    h.close()


def test_cta_history_has_its_own_retention_setting(monkeypatch):
    from app.config import load_settings
    monkeypatch.setenv("HISTORY_DAYS", "365")
    monkeypatch.delenv("CTA_HISTORY_DAYS", raising=False)
    assert load_settings().cta_history_days == 90
    monkeypatch.setenv("CTA_HISTORY_DAYS", "30")
    assert load_settings().cta_history_days == 30


def test_a_database_from_a_newer_version_is_left_alone(tmp_path):
    path = tmp_path / "h.db"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()
    with pytest.raises(RuntimeError, match="newer version"):
        History(path, 30).open()


# ---- 4. CTA live requests ------------------------------------------------------------------------
def _counting(handler):
    calls = []

    def wrapped(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        return handler(request)

    return wrapped, calls


def test_follow_only_asks_about_trains_that_are_running(tmp_path, schedule):  # noqa: F811
    positions = {"ctatt": {"errCd": "0", **_pos(("g", "605", "5", "Ashland/63rd", "40200", _at(8, 5), False))}}
    handler, calls = _counting(lambda r: httpx.Response(200, json=positions if "positions" in r.url.path
                                                        else {"ctatt": {"errCd": "0", "eta": []}}))
    t = _tracker(tmp_path, schedule, handler)
    for bad in ("123456", "x1", "١٢٣"):                      # too long, not a number, not ASCII digits
        with pytest.raises(KeyError):
            run(t.follow(bad))
    for missing in ("999", "1", "42"):
        with pytest.raises(KeyError):
            run(t.follow(missing))
    assert calls == ["ttpositions.aspx"]                     # one positions check, no follow requests
    run(t.follow("605"))
    assert calls == ["ttpositions.aspx", "ttfollow.aspx"]


def test_a_failing_cta_server_is_asked_once_not_by_every_board(tmp_path, schedule):  # noqa: F811
    handler, calls = _counting(lambda r: httpx.Response(503))
    t = _tracker(tmp_path, schedule, handler)
    for _ in range(5):
        d = run(t.arrivals("40100", now=MON_0755))
        assert d["source"] == "schedule" and "503" in d["error"]
    assert calls == ["ttarrivals.aspx"]


def test_a_slow_cta_server_times_out_quickly(tmp_path, schedule, monkeypatch):  # noqa: F811
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    t = _tracker(tmp_path, schedule, slow)
    d = run(t.arrivals("40100", now=MON_0755))
    assert d["source"] == "schedule" and d["error"] == "ReadTimeout"
    assert cta_live.LIVE_TIMEOUT.read <= 10


def test_one_request_at_a_time_per_question():
    calls = []

    async def fetch():
        calls.append(1)
        await asyncio.sleep(0.01)
        return {"ok": True}

    async def many():
        cache = SingleFlight()
        return await asyncio.gather(*(cache.get("k", 30, fetch) for _ in range(10)))

    assert run(many()) == [{"ok": True}] * 10 and calls == [1]


def test_nothing_is_kept_per_key_once_it_expires():
    cache = TTLCache(0.01, max_items=10)
    for i in range(100):
        cache.put(i, i)
        time.sleep(0.0002)
    time.sleep(0.02)
    cache.put("last", 1)
    assert len(cache) <= 11


def test_the_recorder_keeps_its_share_of_the_daily_budget(tmp_path, monkeypatch, schedule):  # noqa: F811
    monkeypatch.setattr(cta_live, "DAILY_BUDGET", 10)
    monkeypatch.setattr(cta_live, "RECORDER_RESERVE", 4)
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=LIVE))
    for i in range(6):
        t.live.cache.clear()
        assert run(t.arrivals(["40100", "40200", "40300"][i % 3], now=MON_0755))["source"] == "live"
    t.live.cache.clear()
    assert "budget" in run(t.arrivals("40100", now=MON_0755))["error"]       # the app's share is used up
    assert t.live._spend(recorder=True)                                        # the recorder can still go


def test_the_request_count_survives_a_restart(tmp_path, schedule):  # noqa: F811
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=LIVE))
    run(t.arrivals("40100", now=MON_0755))
    t.live.save_budget()
    t2 = _tracker(tmp_path, schedule)
    t2.live.load_budget()
    assert t2.live.requests_today >= 1 + cta_live.SAVE_EVERY - 1


def test_a_single_arrival_object_is_read_like_a_list(tmp_path, schedule):  # noqa: F811
    one = {"ctatt": {**LIVE["ctatt"], "eta": LIVE["ctatt"]["eta"][1]}}
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=one))
    d = run(t.arrivals("40100", now=MON_0755))
    assert d["source"] == "live" and [g["key"] for g in d["groups"]] == ["G:5"]


# ---- safety layers --------------------------------------------------------------------------------
def test_unknown_host_names_are_refused_when_set(tracker, tmp_path, monkeypatch):  # noqa: F811
    main, c = _app_with_fixture(tracker, tmp_path, monkeypatch, ALLOWED_HOSTS="nextstop.lan, 192.168.1.20")
    assert c.get("/api/settings", headers={"host": "nextstop.lan"}).status_code == 200
    assert c.get("/healthz", headers={"host": "127.0.0.1:8080"}).status_code in (200, 503)
    assert c.get("/api/settings", headers={"host": "evil.example"}).status_code == 400


def test_every_response_has_safety_headers_and_the_page_a_policy(tracker, tmp_path, monkeypatch):  # noqa: F811
    main, c = _app_with_fixture(tracker, tmp_path, monkeypatch)
    r = c.get("/api/settings")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "same-origin"
    page = c.get("/")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self' 'sha256-" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert "object-src 'none'" in csp


def test_settings_bodies_are_checked_and_capped(tracker, tmp_path, monkeypatch):  # noqa: F811
    main, c = _app_with_fixture(tracker, tmp_path, monkeypatch)
    big = json.dumps({"home_station": "A", "pad": "x" * 70_000})
    assert c.put("/api/settings", content=big, headers={"content-type": "application/json"}).status_code == 413
    assert c.put("/api/settings", json={"home_station": 5}).status_code == 422
    assert c.put("/api/settings", json=["A"]).status_code == 422
    assert c.put("/api/settings", json={"work_days": "x" * 100}).status_code in (400, 422)
    assert not (tmp_path / "cfg" / "settings.json").exists()       # nothing refused was saved
    assert c.put("/api/settings", json={"home_station": "A", "ignored": 1}).status_code == 200


def test_a_tampered_schedule_cache_is_not_run(tmp_path):
    class Boom:
        def __reduce__(self):
            return (exec, ("raise SystemExit('ran code from the cache')",))

    path = tmp_path / "evil.pkl"
    path.write_bytes(pickle.dumps({"cache_version": 4, "data": Boom()}))
    with pytest.raises(pickle.UnpicklingError, match="not allowed"):
        load_cache_file(path, [])


def test_api_docs_load_nothing_from_other_sites(tracker, tmp_path, monkeypatch):  # noqa: F811
    main, c = _app_with_fixture(tracker, tmp_path, monkeypatch)
    html = c.get("/api/docs").text
    assert "/vendor/swagger/swagger-ui-bundle.js" in html and "cdn." not in html
    assert c.get("/vendor/swagger/swagger-ui-bundle.js").status_code == 200


def test_keys_are_blanked_in_every_log_line(caplog):
    record = logging.LogRecord("anything", logging.INFO, __file__, 1,
                               "GET https://lapi.transitchicago.com/api/1.0/ttarrivals.aspx?mapid=1&key=%s&x=1",
                               ("SECRETKEY",), None)
    RedactSecrets().filter(record)
    assert "SECRETKEY" not in record.getMessage() and "key=***" in record.getMessage()
    from app.util import install_redaction
    install_redaction()
    root = logging.getLogger()
    assert all(any(isinstance(f, RedactSecrets) for f in h.filters) for h in root.handlers)


# ---- speed and size ---------------------------------------------------------------------------------
def test_station_and_line_lists_are_built_once_and_revalidated(tracker, tmp_path, monkeypatch):  # noqa: F811
    main, c = _app_with_fixture(tracker, tmp_path, monkeypatch)
    builds = []
    real = type(main.tracker)._build_stations
    monkeypatch.setattr(type(main.tracker), "_build_stations", lambda self: builds.append(1) or real(self))
    r1 = c.get("/api/stations")
    etag = r1.headers["etag"]
    r2 = c.get("/api/stations", headers={"If-None-Match": etag})
    assert r1.status_code == 200 and r2.status_code == 304 and r2.content == b""
    assert c.get("/api/metra/stations").json() == r1.json() and builds == [1]
    lines = c.get("/api/lines")
    assert c.get("/api/lines", headers={"If-None-Match": lines.headers["etag"]}).status_code == 304
    main.tracker.static = main.tracker.static          # a new schedule: everything is rebuilt
    c.get("/api/stations")
    assert builds == [1, 1]


def test_unchanged_predictions_are_not_written_again(tracker, tmp_path):  # noqa: F811
    from tests.test_tracker import _apply, _tu
    h = History(tmp_path / "h.db", 30)
    h.open()
    tracker.history = h
    written = []
    real = h.record_observations
    h.record_observations = lambda rows: written.append(len(rows)) or real(rows)
    _apply(tracker, [_tu("t-0800", [("A", 600)])])
    now = datetime(2026, 11, 9, 7, 50, tzinfo=TZ).timestamp()
    for _ in range(3):
        rows = tracker._observation_rows(now)
        h.record_observations(tracker._changed_rows(rows))
        tracker._recorded = {r[:3]: r[3:] for r in rows}
    assert written[0] > 0 and written[1:] == [0, 0]
    _apply(tracker, [_tu("t-0800", [("A", 900)])])                   # the prediction changes
    assert len(tracker._changed_rows(tracker._observation_rows(now))) > 0
    h.close()


def test_cta_board_history_is_one_query_per_station(tmp_path, schedule, monkeypatch):  # noqa: F811
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, json=LIVE))
    t.history = CtaHistory(tmp_path / "c.db", 30)
    t.history.open()
    queries = []
    real = t.history.board_stats
    monkeypatch.setattr(t.history, "board_stats", lambda *a, **k: queries.append(a[0]) or real(*a, **k))
    d = run(t.arrivals("40100", now=MON_0755))
    assert len(d["groups"]) >= 2 and queries == ["40100"]
    t.history.close()


def test_settings_file_is_read_again_only_when_it_changes(tmp_path, monkeypatch):
    from app.user_settings import UserSettings
    s = UserSettings(tmp_path / "s.json")
    s.update({"home_station": "A"})
    reads = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: reads.append(self.name) or real(self, *a, **k))
    for _ in range(5):
        assert s.load()["home_station"] == "A"
    assert reads.count("s.json") <= 1
    (tmp_path / "s.json").write_text(json.dumps({"home_station": "B", "pad": 1}))   # edited by hand
    assert s.load()["home_station"] == "B"


def test_health_is_ok_when_any_system_can_answer(tmp_path, monkeypatch, schedule):  # noqa: F811
    import importlib
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("METRA_API_TOKEN", "")
    import app.main as main
    main = importlib.reload(main)
    c = TestClient(main.app)
    assert c.get("/healthz").status_code == 503                      # nothing loaded yet
    main.cta.schedule = schedule                                      # CTA loaded, Metra still down
    h = c.get("/healthz")
    assert h.status_code == 200 and {s["id"]: s["loaded"] for s in h.json()["systems"]} == {"metra": False, "cta": True}


def test_cta_schedule_download_is_streamed_and_size_capped(tmp_path, schedule, monkeypatch):  # noqa: F811
    from app import cta as cta_mod
    monkeypatch.setattr(cta_mod, "MAX_DOWNLOAD", 1000)
    t = _tracker(tmp_path, schedule, lambda r: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(ValueError, match="larger than"):
        run(t.refresh_static(force=True))
    assert t.schedule is schedule and not (tmp_path / "cta" / "download.zip").exists()


# ---- one layout for every system -----------------------------------------------------------------
def test_every_system_is_listed_and_served_under_its_own_path(tracker, tmp_path, monkeypatch):  # noqa: F811
    main, c = _app_with_fixture(tracker, tmp_path, monkeypatch)
    systems = {s["id"]: s for s in c.get("/api/systems").json()}
    assert set(systems) == {"metra", "cta"} and systems["metra"]["api"] == "/api/metra"
    # Same answer at both addresses (apart from when it was made, which can tick over a second).
    same = lambda url: {k: v for k, v in c.get(url).json().items() if k != "generated_at"}
    assert same("/api/metra/next?station=A") == same("/api/next?station=A")
    assert c.get("/api/cta/status").status_code == 200
    paths = c.get("/openapi.json").json()["paths"]
    assert "/api/metra/next" in paths and "/api/cta/arrivals" in paths and "/api/next" not in paths
    assert paths["/api/metra/next"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("NextTrains")
