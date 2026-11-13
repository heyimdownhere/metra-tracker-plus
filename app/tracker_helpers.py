"""Constants and small pure functions for the GTFS tracker: direction names,
reading what an alert means, and the status text for a train."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from . import realtime as rtp

USER_AGENT = "next-stop/1.0 (self-hosted)"

# A vehicle position older than this is dropped from the map (it stopped reporting).
STALE_VEHICLE_SECONDS = 180
SNAP_MAX_M = 150  # GPS points this close to the drawn track are moved onto it
# On-time history shown on the board: look back this many days, and only show
# stats once a departure has been seen on at least this many days.
BOARD_HISTORY_DAYS = 30
BOARD_HISTORY_MIN_OBS = 5

# How far back to look for departures, so very late trains still show.
DEPARTURE_LOOKBACK_SECONDS = 4 * 3600
# GPS delay estimates are only made when the train is this close to its route.
GPS_MAX_OFF_ROUTE_M = 2000
GPS_AT_STATION_M = 200
# A train with no trip update this long after its scheduled finish is treated
# as out of service (Metra keeps tagging parked/deadheading trains with their
# last trip for hours).
TRIP_OVER_AFTER_SECONDS = 45 * 60

# Downtown Chicago (the Loop). Metra's downtown terminals (Union Station,
# Ogilvie, LaSalle St., Millennium) are all within ~1.5 km of this point.
LOOP = (41.8819, -87.6278)

DIRECTION_ALIASES = {
    "inbound": "inbound", "in": "inbound", "to": "inbound", "to_downtown": "inbound", "downtown": "inbound",
    "outbound": "outbound", "out": "outbound", "from": "outbound", "from_downtown": "outbound",
}


def normalize_direction(value: str | None) -> str | None:
    """'inbound' (toward downtown), 'outbound', or None for both. Raises ValueError."""
    if value is None or value.strip() == "" or value.strip().lower() in ("both", "any", "all"):
        return None
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in DIRECTION_ALIASES:
        raise ValueError(value)
    return DIRECTION_ALIASES[key]


class AmbiguousStation(KeyError):
    """A station name that matches more than one station."""

    def __init__(self, query: str, options: list[dict]):
        super().__init__(query)
        self.query = query
        self.options = options


def _norm_name(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", x.lower())

DELAY_EFFECTS = {"SIGNIFICANT_DELAYS", "REDUCED_SERVICE", "NO_SERVICE", "DETOUR", "MODIFIED_SERVICE"}
NON_DELAY_EFFECTS = {"ACCESSIBILITY_ISSUE", "STOP_MOVED", "NO_EFFECT", "ADDITIONAL_SERVICE"}
DELAY_WORDS = re.compile(
    r"\b(delay(s|ed)?|late|behind schedule|standing|stopped|holding|held|congestion|"
    r"mechanical|signal (problem|issue)s?|police|pedestrian|trespasser|freight|weather)\b",
    re.IGNORECASE,
)


# Alerts saying trains won't run as the published timetable says.
SCHEDULE_EFFECTS = {"MODIFIED_SERVICE", "REDUCED_SERVICE", "ADDITIONAL_SERVICE", "NO_SERVICE", "DETOUR"}
SCHEDULE_WORDS = re.compile(
    r"\b(schedule (change|changes|adjustment)s?|(modified|special|revised|alternate|temporary|holiday|"
    r"weekend|saturday|sunday) (schedule|service|timetable)|timetable|"
    r"(extra|additional|added|special) (trains?|service)|cancel(l)?ed|annulled|"
    r"will not (run|operate|stop)|not (running|operating)|no service|"
    r"(bus(es)?|substitute bus(es)?) (will )?(replace|replacing|substitute)|bus substitution|"
    r"(depart|leave|leaving|arrive)s? (\d+ )?(minutes? )?(earlier|later) than|"
    r"line (will )?(close|closes|closed|closure)|line closure|express(es|ing)?\b(?! ?way)|"
    r"last (inbound|outbound) (\w+ )?trains?|service (is scheduled to be |will be )?restored|"
    r"skip(s|ping)? (stops|stations))\b",
    re.IGNORECASE,
)


def is_schedule_change(a: rtp.AlertInfo) -> bool:
    """Whether an alert says trains won't match the published timetable."""
    if a.effect in SCHEDULE_EFFECTS:
        return True
    if a.effect in ("ACCESSIBILITY_ISSUE", "STOP_MOVED"):
        return False
    return bool(SCHEDULE_WORDS.search(f"{a.header} {a.description}"))


def _status(r: dict) -> tuple[int | None, str]:
    """(delay in minutes, human status) for a departure row."""
    delay_min = round(r["delay"] / 60) if r["delay"] is not None else None
    if r["cancelled"]:
        return delay_min, "cancelled"
    if r.get("tracking"):
        return delay_min, (f"about {delay_min} min late" if delay_min and delay_min >= 1 else "on time (GPS)")
    if not r["live"]:
        return delay_min, "scheduled"
    if delay_min and delay_min >= 1:
        return delay_min, f"{delay_min} min late"
    if delay_min and delay_min <= -1:
        return delay_min, f"{-delay_min} min early"
    return delay_min, "on time"


def _is_delay_alert(a: rtp.AlertInfo) -> bool:
    """Whether a line-wide alert plausibly explains a late train."""
    if a.effect in DELAY_EFFECTS:
        return True
    if a.effect in NON_DELAY_EFFECTS:
        return False
    if a.cause not in ("UNKNOWN_CAUSE", "OTHER_CAUSE", "", None):
        return True
    return bool(DELAY_WORDS.search(f"{a.header} {a.description}"))


@dataclass
class _TripRT:
    """Realtime info for one static trip, indexed for fast lookup."""
    update: rtp.TripUpdateInfo
    service_date: date | None
