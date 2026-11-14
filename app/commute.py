"""Home/work settings shared by every transit system: what a client may send,
checking it, and which leg of the commute to show right now."""

from __future__ import annotations

import time
from datetime import datetime, tzinfo
from typing import Callable

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .config import parse_switch_time, parse_work_days

STATION_KEYS = ("home_station", "home_dest", "work_station")


class SettingsChange(BaseModel):
    """What PUT .../settings accepts. Only the fields sent are changed; null
    (or "") clears one, falling back to the environment value if there is one."""

    model_config = ConfigDict(extra="ignore")

    home_station: str | None = Field(None, max_length=80, description="Station id")
    home_dest: str | None = Field(None, max_length=80, description="Usual destination from home (station id)")
    work_station: str | None = Field(None, max_length=80, description="Station id")
    switch_time: str | None = Field(None, max_length=8, description="HH:MM: home→work before it, work→home after")
    work_days: list[int] | str | None = Field(None, description="Weekdays, Monday = 0, or text like 'mon-fri'")


def clean_changes(body: SettingsChange, is_station: Callable[[str], bool], what: str = "station") -> dict:
    """The fields that were sent, checked and tidied. Raises HTTP 400 with a
    message a person can act on."""
    sent = body.model_fields_set
    out: dict = {}
    for key in STATION_KEYS:
        if key in sent:
            value = getattr(body, key) or None
            if value is not None and not is_station(value):
                raise HTTPException(400, f"Unknown {what} for {key}: {value!r}")
            out[key] = value
    if "switch_time" in sent:
        if body.switch_time in (None, ""):
            out["switch_time"] = None
        else:
            parsed = parse_switch_time(body.switch_time)
            if parsed is None:
                raise HTTPException(400, "switch_time must be a time like 12:00")
            out["switch_time"] = parsed
    if "work_days" in sent:
        if body.work_days is None:
            out["work_days"] = None
        else:
            if isinstance(body.work_days, str) and len(body.work_days) > 60:
                raise HTTPException(400, "work_days is too long")
            days = parse_work_days(body.work_days)
            if days is None:
                raise HTTPException(400, "work_days must be weekdays like [0,1,2,3,4] (Monday = 0) or 'mon-fri'")
            out["work_days"] = list(days)
    return out


def commute_leg(p: dict, now: float | None = None, tz: tzinfo | None = None) -> dict | None:
    """Which trip to show right now.

    On a workday with both home and work set: before the switch time it's
    home -> work, after it work -> home. Otherwise just the home board.
    """
    home, work = p["home_station"], p["work_station"]
    if not home:
        return None
    local = datetime.fromtimestamp(time.time() if now is None else now, tz)
    if work and work != home and local.weekday() in p["work_days"]:
        if local.strftime("%H:%M") < p["switch_time"]:
            return {"leg": "to_work", "from": home, "to": work}
        return {"leg": "to_home", "from": work, "to": home}
    return {"leg": "home", "from": home, "to": p["home_dest"]}
