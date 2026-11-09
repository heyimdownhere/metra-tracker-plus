"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(value, minimum)


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
    history_days: int = 365

    @property
    def realtime_enabled(self) -> bool:
        return bool(self.api_token)


def load_settings() -> Settings:
    token = os.environ.get("METRA_API_TOKEN", "").strip() or None
    return Settings(
        api_token=token,
        config_dir=Path(os.environ.get("CONFIG_DIR", "/config")),
        static_url=os.environ.get(
            "METRA_STATIC_URL", "https://schedules.metrarail.com/gtfs/schedule.zip"
        ),
        published_url=os.environ.get(
            "METRA_PUBLISHED_URL", "https://schedules.metrarail.com/gtfs/published.txt"
        ),
        realtime_base=os.environ.get(
            "METRA_REALTIME_BASE", "https://gtfspublic.metrarr.com/gtfs/public"
        ).rstrip("/"),
        # Metra asks clients not to poll faster than every 30 seconds.
        realtime_interval=_int_env("REALTIME_INTERVAL", 30, 30),
        static_check_interval=_int_env("STATIC_CHECK_INTERVAL", 300, 60),
        port=_int_env("PORT", 8080, 1),
        # Days of realtime history to keep in /config/history.db (0 disables).
        history_days=_int_env("HISTORY_DAYS", 365, 0),
    )
