"""Self-hosted map background.

The server downloads a Chicago-area extract of the Protomaps OpenStreetMap
basemap once (plus the label fonts and icon sprites it needs) into
CONFIG_DIR/map, and serves tiles and assets itself. Browsers never contact
a map server for the default background.

The extract is made with HTTP range requests against a remote PMTiles
archive (by default the latest Protomaps daily planet build): only the
directories and tiles that cover the bounding box are read.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import mmap
import os
import re
import tempfile
import threading
import time
from bisect import bisect_left
from datetime import date, timedelta
from pathlib import Path

import httpx
from pmtiles.tile import Compression, deserialize_directory, deserialize_header, find_tile, zxy_to_tileid
from pmtiles.writer import Writer

from .config import DEFAULT_BBOX
from .util import TTLCache, atomic_write

log = logging.getLogger(__name__)

DEFAULT_MAX_ZOOM = 15
BUILDS_JSON = "https://build-metadata.protomaps.dev/builds.json"
BUILD_URL = "https://build.protomaps.com/{name}"
ASSETS_BASE = "https://protomaps.github.io/basemaps-assets"
USER_AGENT = "next-stop/1.0 (self-hosted map extract)"

# Asset paths the browser may ask for (fonts and sprites for the bundled styles).
ASSET_RE = re.compile(
    r"fonts/Noto Sans (?:Regular|Medium|Italic)/(\d{1,5})-(\d{1,5})\.pbf"
    r"|sprites/v4/(?:light|dark)(?:@2x)?\.(?:json|png)"
)
# Fetching a missing font or sprite from Protomaps: give up quickly, and don't
# ask again for a while after a failure.
ASSET_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
ASSET_RETRY_SECONDS = 600


def valid_asset(path: str) -> bool:
    """A sprite, or a font file for a real block of 256 characters (0-255,
    256-511, ... 65280-65535). Nothing else is ever fetched or served."""
    m = ASSET_RE.fullmatch(path)
    if not m:
        return False
    if m.group(1) is None:
        return True
    start, end = int(m.group(1)), int(m.group(2))
    return start % 256 == 0 and end == start + 255 and end <= 65535

PREFETCH_ASSETS = [
    *(f"fonts/Noto Sans {w}/{r}.pbf" for w in ("Regular", "Medium", "Italic")
      for r in ("0-255", "256-511", "8192-8447")),
    *(f"sprites/v4/{f}{s}.{e}" for f in ("light", "dark") for s in ("", "@2x") for e in ("json", "png")),
]

GAP_MERGE = 256 * 1024        # merge tile reads separated by less than this
MAX_BATCH = 16 * 1024 * 1024  # bytes per range request


def tiles_in_bbox(bbox: tuple[float, float, float, float], z: int):
    """(x, y) of every tile at zoom z touching bbox (min_lon, min_lat, max_lon, max_lat)."""
    def tx(lon):
        return int((lon + 180.0) / 360.0 * (1 << z))

    def ty(lat):
        lat = max(min(lat, 85.0511), -85.0511)
        r = math.radians(lat)
        return int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * (1 << z))

    n = (1 << z) - 1
    x0, x1 = max(0, tx(bbox[0])), min(n, tx(bbox[2]))
    y0, y1 = max(0, ty(bbox[3])), min(n, ty(bbox[1]))
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            yield x, y


def wanted_tile_ids(bbox, min_zoom: int, max_zoom: int) -> list[int]:
    ids = [zxy_to_tileid(z, x, y) for z in range(min_zoom, max_zoom + 1) for x, y in tiles_in_bbox(bbox, z)]
    return sorted(set(ids))


def extract(get_bytes, out_path: Path, bbox, max_zoom: int, progress=None) -> dict:
    """Copy the tiles inside bbox (zoom 0..max_zoom) from a PMTiles archive.

    get_bytes(offset, length) reads from the source (local or HTTP range).
    Returns a summary dict. Writes atomically to out_path.
    """
    header = deserialize_header(get_bytes(0, 127))
    max_zoom = min(max_zoom, header["max_zoom"])
    min_zoom = header["min_zoom"]
    wanted = wanted_tile_ids(bbox, min_zoom, max_zoom)
    if progress:
        progress(stage="directories", done=0, total=len(wanted))

    # 1. Walk the directory tree, descending only into leaves that cover wanted tiles.
    found: dict[int, tuple[int, int]] = {}  # tile_id -> (offset, length) in tile data

    def any_wanted(lo: int, hi: float) -> bool:
        i = bisect_left(wanted, lo)
        return i < len(wanted) and wanted[i] < hi

    def walk(offset: int, length: int, upper: float):
        entries = deserialize_directory(get_bytes(offset, length))
        for i, e in enumerate(entries):
            nxt = entries[i + 1].tile_id if i + 1 < len(entries) else upper
            if e.run_length == 0:
                if any_wanted(e.tile_id, nxt):
                    walk(header["leaf_directory_offset"] + e.offset, e.length, nxt)
            else:
                end = e.tile_id + e.run_length
                j = bisect_left(wanted, e.tile_id)
                while j < len(wanted) and wanted[j] < end:
                    found[wanted[j]] = (e.offset, e.length)
                    j += 1

    walk(header["root_offset"], header["root_length"], float("inf"))
    if not found:
        raise RuntimeError("no tiles found in the bounding box")

    # 2. Fetch tile data in a few large range requests, spooled to a temp file.
    unique = sorted(set(found.values()))
    batches: list[list[tuple[int, int]]] = []
    for off, ln in unique:
        if batches:
            first_off = batches[-1][0][0]
            last_off, last_len = batches[-1][-1]
            if off - (last_off + last_len) <= GAP_MERGE and (off + ln) - first_off <= MAX_BATCH:
                batches[-1].append((off, ln))
                continue
        batches.append([(off, ln)])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    spool = tempfile.TemporaryFile(dir=out_path.parent)
    where: dict[tuple[int, int], int] = {}
    fetched = 0
    base = header["tile_data_offset"]
    for n, batch in enumerate(batches):
        start = batch[0][0]
        end = batch[-1][0] + batch[-1][1]
        blob = get_bytes(base + start, end - start)
        if len(blob) != end - start:
            raise RuntimeError(f"short read: wanted {end - start} bytes, got {len(blob)}")
        for off, ln in batch:
            where[(off, ln)] = spool.tell()
            spool.write(blob[off - start: off - start + ln])
        fetched += end - start
        if progress:
            progress(stage="tiles", done=n + 1, total=len(batches), bytes=fetched)

    # 3. Write the new archive in tile-id order.
    meta_raw = get_bytes(header["metadata_offset"], header["metadata_length"])
    if header["internal_compression"] == Compression.GZIP:
        meta_raw = gzip.decompress(meta_raw)
    metadata = json.loads(meta_raw or b"{}")
    tmp = out_path.with_suffix(".part")
    with open(tmp, "wb") as f:
        w = Writer(f)
        for tid in sorted(found):
            off, ln = found[tid]
            spool.seek(where[(off, ln)])
            w.write_tile(tid, spool.read(ln))
        new_header = dict(header)
        new_header.update(
            min_lon_e7=int(bbox[0] * 1e7), min_lat_e7=int(bbox[1] * 1e7),
            max_lon_e7=int(bbox[2] * 1e7), max_lat_e7=int(bbox[3] * 1e7),
            center_lon_e7=int((bbox[0] + bbox[2]) / 2 * 1e7), center_lat_e7=int((bbox[1] + bbox[3]) / 2 * 1e7),
            center_zoom=min(10, max_zoom),
        )
        w.finalize(new_header, metadata)
    spool.close()
    os.replace(tmp, out_path)
    return {"tiles": len(found), "bytes_fetched": fetched, "requests": len(batches), "max_zoom": max_zoom,
            "size": out_path.stat().st_size}


class HttpRangeSource:
    """get_bytes() over HTTP range requests, with a small cache for directories."""

    def __init__(self, url: str, client: httpx.Client):
        self.url = url
        self.client = client
        self.requests = 0

    def __call__(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        for attempt in range(4):
            try:
                r = self.client.get(self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
                self.requests += 1
                if r.status_code == 206:
                    return r.content
                if r.status_code == 200:  # server ignored Range: refuse (would be the whole planet)
                    raise RuntimeError("server does not support range requests")
                r.raise_for_status()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == 3:
                    raise
                log.warning("Map download: retrying after %s", exc)
                time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")


def find_latest_build(client: httpx.Client, today: date | None = None) -> str:
    """URL of the newest Protomaps daily planet build."""
    try:
        r = client.get(BUILDS_JSON, timeout=20)
        r.raise_for_status()
        names = re.findall(r"(\d{8})\.pmtiles", r.text)
        if names:
            return BUILD_URL.format(name=max(names) + ".pmtiles")
    except Exception as exc:
        log.info("Map: build list unavailable (%s); probing recent dates", exc)
    today = today or date.today()
    for back in range(0, 21):
        name = (today - timedelta(days=back)).strftime("%Y%m%d") + ".pmtiles"
        url = BUILD_URL.format(name=name)
        try:
            r = client.get(url, headers={"Range": "bytes=0-6"}, timeout=20)
            if r.status_code == 206 and r.content[:7] == b"PMTiles":
                return url
        except httpx.HTTPError:
            continue
    raise RuntimeError("couldn't find a Protomaps build (set MAP_SOURCE to a .pmtiles URL)")


class _Archive:
    """An open map file: its bytes (memory-mapped), header, and the index
    pages already decoded.

    Decoding an index page is pure Python and takes several milliseconds, and
    every tile lookup needs two of them. Keeping the decoded pages turns a
    15-30 ms lookup into a few microseconds. The Chicago extract has a few
    dozen pages (a few MB decoded).
    """

    MAX_PAGES = 256

    def __init__(self, path: Path, stamp: int):
        self.stamp = stamp
        with open(path, "rb") as fh:
            self.mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        self.header = deserialize_header(self.mm[0:127])
        self.gzip = self.header["tile_compression"] == Compression.GZIP
        self._pages: dict[tuple[int, int], list] = {}

    def _page(self, offset: int, length: int) -> list:
        page = self._pages.get((offset, length))
        if page is None:
            page = deserialize_directory(self.mm[offset: offset + length])
            if len(self._pages) >= self.MAX_PAGES:
                self._pages.clear()
            self._pages[(offset, length)] = page
        return page

    def tile(self, z: int, x: int, y: int) -> bytes | None:
        tile_id = zxy_to_tileid(z, x, y)
        h = self.header
        offset, length = h["root_offset"], h["root_length"]
        for _ in range(4):  # the format allows at most this many levels
            entry = find_tile(self._page(offset, length), tile_id)
            if entry is None:
                return None
            if entry.run_length == 0:  # points at a deeper index page
                offset, length = h["leaf_directory_offset"] + entry.offset, entry.length
            else:
                start = h["tile_data_offset"] + entry.offset
                return self.mm[start: start + entry.length]
        return None


class BaseMap:
    def __init__(self, config_dir: Path, enabled: bool = True, source: str | None = None,
                 bbox=DEFAULT_BBOX, max_zoom: int = DEFAULT_MAX_ZOOM, refresh_days: int = 30):
        self.dir = config_dir / "map"
        self.path = self.dir / "chicago.pmtiles"
        self.state_path = self.dir / "map.json"
        self.assets_dir = self.dir / "assets"
        self.enabled = enabled
        self.source = source
        self.bbox = bbox
        self.max_zoom = max_zoom
        self.refresh_days = refresh_days
        self.progress: dict = {}
        self.error: str | None = None
        self.downloading = False
        self._lock = threading.Lock()
        self._archive: _Archive | None = None
        self._archive_lock = threading.Lock()
        self._info: dict | None = None
        self._asset_failed = TTLCache(ASSET_RETRY_SECONDS, max_items=1024)

    # ---- status ------------------------------------------------------------------
    def info(self) -> dict:
        """What map.json says about the downloaded map (read once, then remembered)."""
        if self._info is None:
            try:
                self._info = json.loads(self.state_path.read_text())
            except (OSError, ValueError):
                return {}
        return self._info

    @property
    def available(self) -> bool:
        return self.path.exists()

    def status(self) -> dict:
        info = self.info()
        state = ("disabled" if not self.enabled and not self.available
                 else "downloading" if self.downloading
                 else "ready" if self.available
                 else "error" if self.error else "missing")
        return {
            "state": state,
            "available": self.available,
            "max_zoom": info.get("max_zoom"),
            "size": self.path.stat().st_size if self.available else None,
            "downloaded_at": info.get("downloaded_at"),
            "source": info.get("source"),
            "progress": self.progress if self.downloading else None,
            "error": self.error,
        }

    def needs_download(self) -> bool:
        if not self.enabled:
            return False
        if not self.available:
            return True
        age_days = (time.time() - (self.info().get("downloaded_at") or 0)) / 86400
        return self.refresh_days > 0 and age_days >= self.refresh_days

    # ---- download (blocking; run in a thread) -------------------------------------
    def download(self) -> None:
        if not self._lock.acquire(blocking=False):
            return
        self.downloading, self.error, self.progress = True, None, {"stage": "starting"}
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True,
                              headers={"User-Agent": USER_AGENT}) as client:
                url = self.source or find_latest_build(client)
                log.info("Map: extracting Chicago area (zoom 0-%d) from %s", self.max_zoom, url)
                src = HttpRangeSource(url, client)
                started = time.monotonic()
                summary = extract(src, self.path, self.bbox, self.max_zoom,
                                  progress=lambda **p: self.progress.update(p))
                self.dir.mkdir(parents=True, exist_ok=True)
                atomic_write(self.state_path, json.dumps({
                    "source": url, "downloaded_at": time.time(), "bbox": self.bbox, **summary,
                }, indent=2).encode())
                self._info = None
                log.info("Map: saved %.1f MB (%d tiles, %d requests) in %.0fs",
                         summary["size"] / 1e6, summary["tiles"], src.requests, time.monotonic() - started)
                self._prefetch_assets(client)
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
            log.warning("Map download failed: %s (the app keeps working with the Simple background)", self.error)
        finally:
            self.downloading = False
            self._lock.release()

    def _prefetch_assets(self, client: httpx.Client) -> None:
        for path in PREFETCH_ASSETS:
            try:
                self._fetch_asset(path, client)
            except Exception as exc:
                log.info("Map: couldn't prefetch %s (%s)", path, exc)

    # ---- serving -----------------------------------------------------------------
    def _open(self) -> _Archive | None:
        """The map file, opened once and reopened when a new download replaces it.

        A request that is reading the old file while that happens keeps its own
        reference and finishes normally; the old file is closed when the last
        such request is done.
        """
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            return None
        archive = self._archive
        if archive is None or archive.stamp != stamp:
            with self._archive_lock:
                archive = self._archive
                if archive is None or archive.stamp != stamp:
                    archive = self._archive = _Archive(self.path, stamp)
        return archive

    def tile(self, z: int, x: int, y: int) -> tuple[bytes | None, str | None]:
        """(tile bytes, content-encoding) or (None, None) if not in the extract."""
        if z < 0 or z > 30 or not (0 <= x < (1 << z)) or not (0 <= y < (1 << z)):
            return None, None
        archive = self._open()
        data = archive.tile(z, x, y) if archive else None
        if data is None:
            return None, None
        return data, ("gzip" if archive.gzip else None)

    def _fetch_asset(self, path: str, client: httpx.Client | None = None) -> Path:
        target = self.assets_dir / path
        if target.exists():
            return target
        own = client is None
        client = client or httpx.Client(timeout=ASSET_TIMEOUT, follow_redirects=True,
                                        headers={"User-Agent": USER_AGENT})
        try:
            r = client.get(f"{ASSETS_BASE}/{path}", timeout=ASSET_TIMEOUT)
            r.raise_for_status()
        finally:
            if own:
                client.close()
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, r.content)
        return target

    def cached_asset(self, path: str) -> Path | None:
        """The font/sprite file if it is already on disk. Never uses the network."""
        if not valid_asset(path):
            return None
        target = self.assets_dir / path
        return target if target.is_file() else None

    def asset(self, path: str) -> Path | None:
        """Font/sprite file, downloaded once from Protomaps' asset site and cached.
        Blocking: call it from a worker thread. A failed download isn't tried
        again for ten minutes."""
        if not valid_asset(path) or self._asset_failed.get(path):
            return None
        try:
            return self._fetch_asset(path)
        except Exception as exc:
            self._asset_failed.put(path, True)
            log.info("Map: asset %s unavailable (%s)", path, exc)
            return None
