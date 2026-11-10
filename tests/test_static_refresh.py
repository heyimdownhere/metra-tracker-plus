"""Schedule downloads: weekly full re-check, validation, and the backup copy."""

from __future__ import annotations

import asyncio
import io
import os
import time
import zipfile

import httpx
import pytest

from app.config import Settings
from app.static_gtfs import StaticCache
from app.tracker import Tracker
from tests.test_tracker import FILES

# Same feed as test_tracker, valid far into the future so "trains this week" holds.
GOOD = {**FILES, "calendar.txt": FILES["calendar.txt"].replace("20261130", "20991231").replace("20261101", "20200101")}
EXPIRED = {**FILES, "calendar.txt": FILES["calendar.txt"].replace("20261130", "20200131").replace("20261101", "20200101")}


def _zip(files: dict, extra: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
        if extra:
            zf.writestr("feed_info.txt", extra)
    return buf.getvalue()


class FakeClient:
    """Stands in for httpx: serves published.txt and the zip, and counts zip downloads."""

    def __init__(self, published: str, content: bytes):
        self.published, self.content, self.zip_gets = published, content, 0

    async def get(self, url, **kw):
        if url == "pub":
            return httpx.Response(200, text=self.published, request=httpx.Request("GET", url))
        self.zip_gets += 1
        return httpx.Response(200, content=self.content, request=httpx.Request("GET", url))


def _tracker(tmp_path, client, days=7) -> Tracker:
    s = Settings(api_token=None, config_dir=tmp_path, static_url="zip", published_url="pub",
                 realtime_base="", realtime_interval=30, static_check_interval=300, port=8080,
                 static_full_refresh_days=days)
    t = Tracker(s)
    t._client = client
    t.cache.ensure_dir()
    return t


def run(coro):
    return asyncio.run(coro)


def test_first_download_and_unchanged_checks(tmp_path):
    client = FakeClient("v1", _zip(GOOD))
    t = _tracker(tmp_path, client)
    assert run(t.refresh_static()) is True and t.static.version == "v1"
    # Normal 5-minute check: published.txt unchanged -> no download.
    assert run(t.refresh_static()) is False and client.zip_gets == 1
    # Weekly check: downloads, sees the same file, just records the check time.
    os.utime(t.cache.zip_path, (time.time() - 8 * 86400,) * 2)
    assert t._full_refresh_due()
    assert run(t.refresh_static(full=True)) is False and client.zip_gets == 2
    assert not t._full_refresh_due() and t.static.version == "v1"


def test_weekly_check_catches_silent_change_and_keeps_backup(tmp_path):
    client = FakeClient("v1", _zip(GOOD))
    t = _tracker(tmp_path, client)
    run(t.refresh_static())
    first = t.cache.zip_path.read_bytes()
    client.content = _zip(GOOD, extra="feed_publisher_name\nMetra\n")  # same published.txt, new file
    assert run(t.refresh_static(full=True)) is True
    assert t.static.version.startswith("v1 (sha256:")
    assert t.cache.backup_zip.read_bytes() == first
    assert t.cache.backup_version.read_text().strip() == "v1"


@pytest.mark.parametrize("bad", [b"not a zip", _zip(EXPIRED), _zip({"agency.txt": FILES["agency.txt"]})])
def test_bad_download_never_replaces_working_schedule(tmp_path, bad):
    client = FakeClient("v1", _zip(GOOD))
    t = _tracker(tmp_path, client)
    run(t.refresh_static())
    good = t.cache.zip_path.read_bytes()
    client.published, client.content = "v2", bad
    assert run(t.refresh_static()) is False
    assert t.static.version == "v1" and "not used" in t.static_error and "keeping v1" in t.static_error
    assert t.cache.zip_path.read_bytes() == good and not t.cache.new_zip.exists()
    # A restart still loads the good schedule.
    t.cache.pickle_path.unlink()
    assert StaticCache(tmp_path).load().version == "v1"


def test_falls_back_to_backup_when_current_file_is_broken(tmp_path):
    client = FakeClient("v1", _zip(GOOD))
    t = _tracker(tmp_path, client)
    run(t.refresh_static())
    client.published, client.content = "v2", _zip(GOOD, extra="x\n1\n")
    run(t.refresh_static())
    assert t.static.version == "v2"
    t.cache.pickle_path.unlink()
    t.cache.zip_path.write_bytes(b"corrupted on disk")
    assert StaticCache(tmp_path).load().version == "v1"


def test_full_refresh_can_be_turned_off(tmp_path):
    t = _tracker(tmp_path, FakeClient("v1", _zip(GOOD)), days=0)
    run(t.refresh_static())
    os.utime(t.cache.zip_path, (0, 0))
    assert not t._full_refresh_due()
