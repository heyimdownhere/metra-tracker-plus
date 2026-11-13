"""Runtime configuration, read from environment variables.

Every setting has a sensible default, so a container runs with none set.
See README "Configuration" for the full list. Invalid values are reported in
the log and replaced by the default (or clamped to the allowed range) rather
than stopping the container.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PORT = 8080
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _int_env(name: str, default: int, minimum: int, maximum: int | None = None,
             warnings: list[str] | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        if warnings is not None:
            warnings.append(f"{name}={raw!r} is not a whole number; using {default}")
        return default
    if value < minimum:
        if warnings is not None:
            warnings.append(f"{name}={value} is below the minimum {minimum}; using {minimum}")
        return minimum
    if maximum is not None and value > maximum:
        if warnings is not None:
            warnings.append(f"{name}={value} is above the maximum {maximum}; using {maximum}")
        return maximum
    return value


def _str_env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_WORK_DAYS = (0, 1, 2, 3, 4)
DEFAULT_SWITCH_TIME = "12:00"


def parse_switch_time(value: str | None) -> str | None:
    """'7:30', '07:30', '13:05' -> 'HH:MM'; None if invalid."""
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def parse_work_days(value) -> tuple[int, ...] | None:
    """'mon-fri', 'mon,tue,wed', '0,1,2' or a list of 0-6 (Mon=0) -> sorted tuple; None if invalid."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        text = str(value).strip().lower()
        if not text:
            return None
        items = []
        for part in text.replace(" ", "").split(","):
            if "-" in part and part.split("-")[0] in DAY_NAMES:
                a, b = part.split("-", 1)
                if b not in DAY_NAMES:
                    return None
                i, j = DAY_NAMES.index(a), DAY_NAMES.index(b)
                items.extend(range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)])
            elif part[:3] in DAY_NAMES:
                items.append(DAY_NAMES.index(part[:3]))
            elif part.isdigit():
                items.append(int(part))
            else:
                return None
    try:
        days = sorted({int(d) for d in items})
    except (TypeError, ValueError):
        return None
    if not all(0 <= d <= 6 for d in days):
        return None
    return tuple(days)


def _read_token(warnings: list[str]) -> str | None:
    """METRA_API_TOKEN, or the contents of the file named by METRA_API_TOKEN_FILE
    (for Docker/compose secrets). The variable wins if both are set."""
    token = _str_env("METRA_API_TOKEN")
    if token:
        return token
    path = _str_env("METRA_API_TOKEN_FILE")
    if not path:
        return None
    try:
        return Path(path).read_text().strip() or None
    except OSError as exc:
        warnings.append(f"METRA_API_TOKEN_FILE={path!r} could not be read ({exc}); running schedule-only")
        return None


def _read_cta_key(warnings: list[str]) -> str | None:
    """CTA_TRAIN_TRACKER_KEY, or the contents of the file named by CTA_TRAIN_TRACKER_KEY_FILE."""
    key = _str_env("CTA_TRAIN_TRACKER_KEY")
    if key:
        return key
    path = _str_env("CTA_TRAIN_TRACKER_KEY_FILE")
    if not path:
        return None
    try:
        return Path(path).read_text().strip() or None
    except OSError as exc:
        warnings.append(f"CTA_TRAIN_TRACKER_KEY_FILE={path!r} could not be read ({exc}); CTA uses the schedule only")
        return None


def _bool_env(name: str, default: bool, warnings: list[str]) -> bool:
    raw = (_str_env(name) or "").lower()
    if not raw:
        return default
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    warnings.append(f"{name}={raw!r} should be true or false; using {'true' if default else 'false'}")
    return default


@dataclass(frozen=True)
class Settings:
    api_token: str | None
    config_dir: Path
    static_url: str
    published_url: str
    realtime_base: str
    realtime_interval: int
    static_check_interval: int
    port: int
    static_full_refresh_days: int = 7
    history_days: int = 365
    host: str = "0.0.0.0"
    home_station: str | None = None
    home_destination: str | None = None
    work_station: str | None = None
    commute_switch_time: str = DEFAULT_SWITCH_TIME
    work_days: tuple[int, ...] = DEFAULT_WORK_DAYS
    log_level: str = "INFO"
    map_download: bool = True
    map_source: str | None = None
    map_max_zoom: int = 15
    map_bbox: tuple[float, float, float, float] = (-88.75, 41.30, -87.40, 42.70)
    map_refresh_days: int = 30
    cta_enabled: bool = True
    cta_api_key: str | None = None
    cta_static_url: str = "https://www.transitchicago.com/downloads/sch_data/google_transit.zip"
    cta_static_check_hours: int = 24
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def realtime_enabled(self) -> bool:
        return bool(self.api_token)

    def summary(self) -> str:
        """Human-readable settings for the startup log. Never prints the token."""
        token = "not set (schedule-only mode)"
        if self.api_token:
            token = f"set ({len(self.api_token)} chars, ends …{self.api_token[-4:]})" if len(self.api_token) > 8 else "set"
        lines = [
            f"  METRA_API_TOKEN       {token}",
            f"  HOST:PORT             {self.host}:{self.port}",
            f"  CONFIG_DIR            {self.config_dir}",
            f"  TZ                    {os.environ.get('TZ', '(container default)')}",
            f"  REALTIME_INTERVAL     {self.realtime_interval}s",
            f"  STATIC_CHECK_INTERVAL {self.static_check_interval}s",
            "  STATIC_FULL_REFRESH   " + (f"every {self.static_full_refresh_days} days" if self.static_full_refresh_days else "off"),
            f"  HISTORY_DAYS          {self.history_days}" + (" (history off)" if self.history_days == 0 else ""),
            f"  HOME_STATION          {self.home_station or '(not set)'}",
            f"  HOME_DESTINATION      {self.home_destination or '(not set)'}",
            f"  WORK_STATION          {self.work_station or '(not set)'}",
            f"  COMMUTE_SWITCH_TIME   {self.commute_switch_time}",
            f"  WORK_DAYS             {','.join(DAY_NAMES[d] for d in self.work_days) or '(none)'}",
            f"  MAP_DOWNLOAD          {'on' if self.map_download else 'off'} (zoom 0-{self.map_max_zoom}, refresh every {self.map_refresh_days} days)",
            f"  CTA_ENABLED           {'on' if self.cta_enabled else 'off'}",
            f"  CTA_TRAIN_TRACKER_KEY {_mask(self.cta_api_key, 'not set (CTA schedule only)')}",
            f"  LOG_LEVEL             {self.log_level}",
        ]
        return "Settings:\n" + "\n".join(lines)


def _mask(secret: str | None, unset: str) -> str:
    if not secret:
        return unset
    return f"set ({len(secret)} chars, ends …{secret[-4:]})" if len(secret) > 8 else "set"


def load_settings() -> Settings:
    warnings: list[str] = []
    switch = _str_env("COMMUTE_SWITCH_TIME")
    switch_time = parse_switch_time(switch) if switch else DEFAULT_SWITCH_TIME
    if switch_time is None:
        warnings.append(f"COMMUTE_SWITCH_TIME={switch!r} isn't a time like 12:00; using {DEFAULT_SWITCH_TIME}")
        switch_time = DEFAULT_SWITCH_TIME
    days_raw = _str_env("WORK_DAYS")
    work_days = parse_work_days(days_raw) if days_raw else DEFAULT_WORK_DAYS
    if work_days is None:
        warnings.append(f"WORK_DAYS={days_raw!r} isn't like 'mon-fri' or 'mon,wed,fri'; using mon-fri")
        work_days = DEFAULT_WORK_DAYS
    bbox_raw = _str_env("MAP_BBOX")
    map_bbox = (-88.75, 41.30, -87.40, 42.70)
    if bbox_raw:
        try:
            parts = tuple(float(v) for v in bbox_raw.split(","))
            if len(parts) != 4 or not (parts[0] < parts[2] and parts[1] < parts[3]):
                raise ValueError
            map_bbox = parts
        except ValueError:
            warnings.append(f"MAP_BBOX={bbox_raw!r} isn't min_lon,min_lat,max_lon,max_lat; using the Metra area")
    map_dl = (_str_env("MAP_DOWNLOAD", "true") or "true").lower()
    if map_dl not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        warnings.append(f"MAP_DOWNLOAD={map_dl!r} should be true or false; using true")
        map_dl = "true"
    level = (_str_env("LOG_LEVEL", "INFO") or "INFO").upper()
    if level not in VALID_LOG_LEVELS:
        warnings.append(f"LOG_LEVEL={level!r} is not one of {', '.join(VALID_LOG_LEVELS)}; using INFO")
        level = "INFO"
    return Settings(
        api_token=_read_token(warnings),
        config_dir=Path(_str_env("CONFIG_DIR", "/config")),
        static_url=_str_env("METRA_STATIC_URL", "https://schedules.metrarail.com/gtfs/schedule.zip"),
        published_url=_str_env("METRA_PUBLISHED_URL", "https://schedules.metrarail.com/gtfs/published.txt"),
        realtime_base=_str_env("METRA_REALTIME_BASE", "https://gtfspublic.metrarr.com/gtfs/public").rstrip("/"),
        # Metra asks clients not to poll faster than every 30 seconds.
        realtime_interval=_int_env("REALTIME_INTERVAL", 30, 30, 3600, warnings),
        # How often to ask Metra whether a new schedule was published (realtime polling is separate).
        static_check_interval=_int_env("STATIC_CHECK_INTERVAL", 3600, 60, 86400, warnings),
        # Re-download the whole schedule this often even if Metra says it's unchanged (0 = never).
        static_full_refresh_days=_int_env("STATIC_FULL_REFRESH_DAYS", 7, 0, 365, warnings),
        port=_int_env("PORT", DEFAULT_PORT, 1, 65535, warnings),
        host=_str_env("HOST", "0.0.0.0"),
        # Days of realtime history to keep in /config/history.db (0 disables).
        history_days=_int_env("HISTORY_DAYS", 365, 0, 3650, warnings),
        home_station=_str_env("HOME_STATION"),
        home_destination=_str_env("HOME_DESTINATION"),
        work_station=_str_env("WORK_STATION"),
        commute_switch_time=switch_time,
        work_days=work_days,
        log_level=level,
        map_download=map_dl in ("true", "1", "yes", "on"),
        map_source=_str_env("MAP_SOURCE"),
        map_max_zoom=_int_env("MAP_MAX_ZOOM", 15, 8, 15, warnings),
        map_bbox=map_bbox,
        map_refresh_days=_int_env("MAP_REFRESH_DAYS", 30, 0, 3650, warnings),
        cta_enabled=_bool_env("CTA_ENABLED", True, warnings),
        cta_api_key=_read_cta_key(warnings),
        cta_static_url=_str_env("CTA_STATIC_URL", "https://www.transitchicago.com/downloads/sch_data/google_transit.zip"),
        # How often to ask CTA whether its schedule changed (a small request unless it did).
        cta_static_check_hours=_int_env("CTA_STATIC_CHECK_HOURS", 24, 1, 24 * 30, warnings),
        warnings=tuple(warnings),
    )
