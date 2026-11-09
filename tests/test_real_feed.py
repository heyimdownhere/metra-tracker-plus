"""Integration test against Metra's live static GTFS. Run with METRA_LIVE_TESTS=1."""

import os
import time
import urllib.request

import pytest

from app.static_gtfs import parse_gtfs_zip

pytestmark = pytest.mark.skipif(not os.environ.get("METRA_LIVE_TESTS"), reason="set METRA_LIVE_TESTS=1")


def test_real_static_feed(tmp_path):
    zp = tmp_path / "schedule.zip"
    urllib.request.urlretrieve("https://schedules.metrarail.com/gtfs/schedule.zip", zp)
    s = parse_gtfs_zip(zp, "live")
    assert len(s.routes) >= 10 and len(s.trips) > 1000 and s.shapes
    now = time.time()
    active = set().union(*(s.active_services(d) for d in s.candidate_service_dates(now)))
    assert active, "no active service around today"
    assert all(r["color"].startswith("#") for r in s.routes.values())
