"""FastAPI entry point: the JSON API for each transit system, and the web app.

The browser only ever talks to this server; all Metra and CTA requests are
made server-side (a Metra data-license requirement).

create_app() builds everything. Each transit system is registered as a
System (see app/systems.py) and gets its API under /api/<id>/. Metra's is
also served at /api/... where it has always been.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .basemap import BaseMap
from .commute import commute_leg
from .config import Settings, load_settings
from .cta import CtaTracker
from .cta_api import make_router as make_cta_router
from .metra_api import MetraCommute, make_router as make_metra_router
from .systems import System
from .tracker import Tracker
from .user_settings import UserSettings
from .util import install_redaction, json_response
from .web import WEB_DIR, BodyLimit, IndexPage, SecurityHeaders, WebFiles

log = logging.getLogger("metra")

# How many map fonts/sprites may be fetched from Protomaps at once. Each fetch
# waits on the network in a worker thread; capping them keeps threads free for
# the boards.
ASSET_FETCHES = 4


async def _map_loop(basemap: BaseMap):
    """Download the self-hosted map on first start, then refresh it now and then."""
    await asyncio.sleep(5)  # let the schedule download go first
    while True:
        if basemap.needs_download():
            await asyncio.to_thread(basemap.download)
            await asyncio.sleep(3600 if basemap.error else 6 * 3600)
        else:
            await asyncio.sleep(6 * 3600)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_redaction()

    tracker = Tracker(settings)
    commute = MetraCommute(tracker, UserSettings(settings.config_dir / "settings.json"), settings)
    # CTA is kept apart: its own tracker, schedule cache (/config/cta) and settings file.
    cta = CtaTracker(settings.config_dir, settings.cta_api_key, settings.cta_static_url,
                     check_hours=settings.cta_static_check_hours, enabled=settings.cta_enabled,
                     history_days=settings.cta_history_days)
    cta_settings = UserSettings(settings.config_dir / "cta-settings.json")
    basemap = BaseMap(settings.config_dir, enabled=settings.map_download, source=settings.map_source,
                      bbox=settings.map_bbox, max_zoom=settings.map_max_zoom, refresh_days=settings.map_refresh_days)

    metra_router = make_metra_router(tracker, commute, settings)
    systems = [
        System("metra", "Metra", tracker, metra_router, live=settings.realtime_enabled),
        System("cta", "CTA 'L'", cta, make_cta_router(cta, cta_settings, settings, commute_leg, prefix=""),
               enabled=settings.cta_enabled, live=bool(settings.cta_api_key)),
    ]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info(settings.summary())
        for warning in settings.warnings:
            log.warning("Config: %s", warning)
        for system in systems:
            if system.enabled:
                await system.tracker.start()
        map_task = asyncio.create_task(_map_loop(basemap), name="map")
        yield
        map_task.cancel()
        for system in reversed(systems):
            if system.enabled:
                await system.tracker.stop()

    app = FastAPI(title="Next Stop", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.tracker, app.state.cta, app.state.basemap = tracker, cta, basemap
    app.state.commute, app.state.systems = commute, systems

    # Outermost first: refuse unknown host names, cap request bodies, add the
    # safety headers, then compress (JSON and the JS/CSS bundles: the map
    # library alone is ~1 MB, which matters on phone data).
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(BodyLimit, max_bytes=64 * 1024)
    if settings.allowed_hosts:
        # The health check inside the container asks for 127.0.0.1.
        app.add_middleware(TrustedHostMiddleware,
                           allowed_hosts=[*settings.allowed_hosts, "localhost", "127.0.0.1"])

    # ---- health and the systems list --------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    @app.get("/api/health")
    def health():
        """200 once any system can answer; each system's state is listed."""
        states = [s.summary() for s in systems if s.enabled]
        ok = any(s["loaded"] for s in states)
        metra = tracker.static is not None
        return json_response({
            "status": "ok" if ok else "starting",
            "systems": states,
            # Kept for older clients: Metra's own state.
            "static_loaded": metra,
            "mode": "realtime" if settings.realtime_enabled else "schedule-only",
            "realtime_live": tracker.realtime_live,
        }, 200 if ok else 503)

    @app.get("/api/systems")
    def systems_list():
        """The transit systems this server serves, and where their API is."""
        return json_response([s.summary() for s in systems])

    @app.get("/api/status")
    def status():
        return json_response({**tracker.status(), "map": basemap.status()})

    for system in systems:
        app.include_router(system.router, prefix=f"/api/{system.id}")
    # Metra's endpoints where they have always been (Home Assistant sensors use them).
    app.include_router(metra_router, prefix="/api", include_in_schema=False)

    # ---- self-hosted map ---------------------------------------------------------------
    @app.get("/api/map")
    def map_status():
        """Self-hosted map: state (ready/downloading/missing/error/disabled), size, progress."""
        return json_response(basemap.status())

    tile_cache = {"Cache-Control": "public, max-age=86400"}

    @app.get("/map/tiles/{z}/{x}/{y}.mvt", include_in_schema=False)
    def map_tile(z: int, x: int, y: int):
        data, encoding = basemap.tile(z, x, y)
        if data is None:
            return Response(status_code=204, headers=tile_cache)
        headers = dict(tile_cache)
        if encoding:
            headers["Content-Encoding"] = encoding  # already compressed; GZipMiddleware leaves it alone
        return Response(data, media_type="application/vnd.mapbox-vector-tile", headers=headers)

    asset_fetches = asyncio.Semaphore(ASSET_FETCHES)

    @app.get("/map/assets/{path:path}", include_in_schema=False)
    async def map_asset(path: str):
        """Label fonts and icon sprites, fetched once by the server and cached in /config/map."""
        file = basemap.cached_asset(path)
        if file is None:
            async with asset_fetches:
                file = await asyncio.to_thread(basemap.asset, path)
        if file is None:
            raise HTTPException(404, "Not available")
        media = ("application/x-protobuf" if path.endswith(".pbf")
                 else "application/json" if path.endswith(".json") else "image/png")
        return FileResponse(file, media_type=media, headers={"Cache-Control": "public, max-age=604800"})

    # ---- API docs, served from this server (no outside script) ------------------------
    @app.get("/api/docs", include_in_schema=False)
    def docs():
        return get_swagger_ui_html(openapi_url=app.openapi_url or "/openapi.json", title="Next Stop API",
                                   swagger_js_url="/vendor/swagger/swagger-ui-bundle.js",
                                   swagger_css_url="/vendor/swagger/swagger-ui.css",
                                   swagger_favicon_url="/favicon.svg")

    # ---- web app ------------------------------------------------------------------------
    page = IndexPage(WEB_DIR)

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def index():
        return page.response()

    app.mount("/", WebFiles(directory=WEB_DIR), name="web")
    return app


app = create_app()

# Kept for tests and scripts that reach into the running app.
tracker: Tracker = app.state.tracker
cta: CtaTracker = app.state.cta
basemap: BaseMap = app.state.basemap
settings: Settings = app.state.settings


def profile(now: float | None = None) -> dict:
    return app.state.commute.profile(now)


def _commute_leg(p: dict, now: float | None = None, tz=None) -> dict | None:
    return commute_leg(p, now, tz or (tracker.static.tz if tracker.static else None))


__all__ = ["app", "create_app", "tracker", "cta", "basemap", "settings", "profile", "Path"]
