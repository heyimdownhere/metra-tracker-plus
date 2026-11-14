"""Background polling of a GTFS + GTFS-Realtime agency (Metra) and the queries
the web API serves.

The class is built in layers, one file each:
  tracker_core      the schedule in use, feed polling, predictions
  tracker_stations  station names and lookups
  tracker_map       trains for the map
  tracker_boards    boards, timetable, one train, Home Assistant JSON
"""

from __future__ import annotations

import asyncio  # noqa: F401  (tests patch app.tracker.asyncio / app.tracker.time)
import time  # noqa: F401

from .tracker_boards import BoardQueries
from .tracker_helpers import AmbiguousStation, is_schedule_change, normalize_direction  # noqa: F401


class Tracker(BoardQueries):
    """Everything about one agency: state, polling and queries."""
