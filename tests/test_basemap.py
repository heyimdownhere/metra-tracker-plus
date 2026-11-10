"""Tests for the self-hosted map: extracting a region from a PMTiles archive and serving it."""

from __future__ import annotations

import gzip
from datetime import date
from pathlib import Path

import httpx
import pytest
from pmtiles.reader import MmapSource, Reader
from pmtiles.tile import Compression, TileType, tileid_to_zxy, zxy_to_tileid
from pmtiles.writer import Writer

from app.basemap import BaseMap, extract, find_latest_build, tiles_in_bbox, wanted_tile_ids

BBOX = (-88.75, 41.30, -87.40, 42.70)
WORLD_MAX_Z = 8


def _payload(z, x, y):
    # Varied sizes so the directory doesn't compress away (real builds need leaf directories).
    import random
    pad = random.Random(z * 1_000_003 + x * 7919 + y).randbytes((x * 7919 + y * 104729 + z) % 400)
    return gzip.compress(f"tile {z}/{x}/{y}|".encode() + pad, mtime=0)


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Path:
    """A whole-world archive (zoom 0-8, every tile distinct) shaped like a Protomaps build."""
    path = tmp_path_factory.mktemp("world") / "world.pmtiles"
    ids = sorted(zxy_to_tileid(z, x, y) for z in range(WORLD_MAX_Z + 1) for x in range(1 << z) for y in range(1 << z))
    with open(path, "wb") as f:
        w = Writer(f)
        for tid in ids:
            w.write_tile(tid, _payload(*tileid_to_zxy(tid)))
        w.finalize({
            "tile_type": TileType.MVT, "tile_compression": Compression.GZIP,
            "min_lon_e7": -1800000000, "min_lat_e7": -850000000, "max_lon_e7": 1800000000, "max_lat_e7": 850000000,
            "center_zoom": 0, "center_lon_e7": 0, "center_lat_e7": 0,
        }, {"name": "test world", "attribution": "© OpenStreetMap"})
    return path


def test_world_fixture_uses_leaf_directories(world):
    from pmtiles.tile import deserialize_directory, deserialize_header
    with open(world, "rb") as f:
        get = MmapSource(f)
        h = deserialize_header(get(0, 127))
        root = deserialize_directory(get(h["root_offset"], h["root_length"]))
        assert h["leaf_directory_length"] > 0 and any(e.run_length == 0 for e in root)


def test_extract_copies_exactly_the_bbox_tiles(world, tmp_path):
    calls = []
    with open(world, "rb") as f:
        src = MmapSource(f)

        def get_bytes(off, ln):
            calls.append(ln)
            return src(off, ln)

        out = tmp_path / "chicago.pmtiles"
        summary = extract(get_bytes, out, BBOX, max_zoom=15)   # capped at the archive's zoom 7
    wanted = wanted_tile_ids(BBOX, 0, WORLD_MAX_Z)
    assert summary["tiles"] == len(wanted) and summary["max_zoom"] == WORLD_MAX_Z
    assert len(calls) < 20, calls                     # a handful of range reads, not one per tile
    with open(out, "rb") as f:
        r = Reader(MmapSource(f))
        h = r.header()
        assert h["min_zoom"] == 0 and h["max_zoom"] == WORLD_MAX_Z
        assert h["min_lon_e7"] == int(BBOX[0] * 1e7) and h["max_lat_e7"] == int(BBOX[3] * 1e7)
        assert h["tile_compression"] == Compression.GZIP
        assert r.metadata()["attribution"] == "© OpenStreetMap"
        for tid in wanted:
            z, x, y = tileid_to_zxy(tid)
            assert r.get(z, x, y) == _payload(z, x, y)
        # Chicago at zoom 7 is tile (32, 47); a tile on the other side of the world is absent.
        assert (32, 47) in set(tiles_in_bbox(BBOX, 7))
        assert r.get(7, 100, 100) is None


def _ready_basemap(world, tmp_path) -> BaseMap:
    bm = BaseMap(tmp_path / "cfg", enabled=False)
    with open(world, "rb") as f:
        extract(MmapSource(f), bm.path, BBOX, max_zoom=7)
    return bm


def test_tiles_and_assets_served_by_the_app(world, tmp_path, monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("MAP_DOWNLOAD", "false")
    import app.main as main
    main = importlib.reload(main)
    with open(world, "rb") as f:
        extract(MmapSource(f), main.basemap.path, BBOX, max_zoom=7)
    c = TestClient(main.app)
    r = c.get("/map/tiles/7/32/47.mvt")
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert r.content == gzip.decompress(_payload(7, 32, 47))   # client undoes the gzip
    assert c.get("/map/tiles/7/100/100.mvt").status_code == 204
    assert c.get("/map/tiles/7/999/1.mvt").status_code == 204
    st = c.get("/api/map").json()
    assert st["state"] == "ready" and st["available"]
    # Assets: only fonts/sprites, cached after the first fetch; no path tricks.
    fetched = []
    def fake_fetch(path, client=None):
        fetched.append(path)
        target = main.basemap.assets_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        return target
    monkeypatch.setattr(main.basemap, "_fetch_asset", fake_fetch)
    assert c.get("/map/assets/fonts/Noto Sans Regular/0-255.pbf").status_code == 200
    assert c.get("/map/assets/sprites/v4/dark@2x.png").status_code == 200
    assert c.get("/map/assets/../../settings.json").status_code == 404
    assert c.get("/map/assets/fonts/Evil/0-255.pbf").status_code == 404
    assert fetched == ["fonts/Noto Sans Regular/0-255.pbf", "sprites/v4/dark@2x.png"]


def test_find_latest_build_from_list_or_by_probing():
    def handler(request: httpx.Request):
        if "builds.json" in str(request.url):
            return httpx.Response(200, json=[{"key": "20261101.pmtiles"}, {"key": "20261108.pmtiles"}])
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert find_latest_build(c) == "https://build.protomaps.com/20261108.pmtiles"

    def handler2(request: httpx.Request):
        if "builds.json" in str(request.url):
            return httpx.Response(500)
        if request.url.path == "/20261107.pmtiles":
            return httpx.Response(206, content=b"PMTiles")
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handler2)) as c:
        assert find_latest_build(c, today=date(2026, 11, 10)) == "https://build.protomaps.com/20261107.pmtiles"


def test_download_over_http_ranges(world, tmp_path):
    """The whole download path, with the 'remote' archive served by a mock HTTP server."""
    data = world.read_bytes()
    def handler(request: httpx.Request):
        rng = request.headers.get("range", "")
        start, end = (int(v) for v in rng.removeprefix("bytes=").split("-"))
        return httpx.Response(206, content=data[start:end + 1])
    bm = BaseMap(tmp_path / "cfg", enabled=True, source="https://example.test/world.pmtiles", max_zoom=7)
    real_client = httpx.Client
    import app.basemap as mod
    mod.httpx.Client = lambda **kw: real_client(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"})
    try:
        bm._prefetch_assets = lambda client: None
        assert bm.needs_download()
        bm.download()
    finally:
        mod.httpx.Client = real_client
    assert bm.error is None and bm.available and not bm.needs_download()
    assert bm.status()["state"] == "ready" and bm.info()["source"] == "https://example.test/world.pmtiles"
    assert bm.tile(7, 32, 47) == (_payload(7, 32, 47), "gzip")


def test_index_links_are_versioned_and_cached():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    page = c.get("/")
    assert page.headers["cache-control"] == "no-cache"
    import re
    links = re.findall(r'(?:src|href)="([^"]+\.(?:js|css)\?v=[0-9a-f]{10})"', page.text)
    assert any(l.startswith("app.js?v=") for l in links) and any("maplibre-gl.js?v=" in l for l in links)
    for link in links:
        r = c.get("/" + link)
        assert r.status_code == 200
        assert "immutable" in r.headers["cache-control"]
    assert c.get("/app.js").headers["cache-control"] == "no-cache"
