"""The web side that isn't data: the page itself, static files, and the small
safety layers every response goes through."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Outside servers the page may load map pictures and styles from. Only the
# optional online backgrounds use them (see web/map.js); the default map,
# its fonts and all train data come from this server.
ONLINE_MAP_HOSTS = "https://tiles.openfreemap.org https://server.arcgisonline.com https://*.tiles.openrailwaymap.org"

# index.html links each script/stylesheet as "file?v=<content hash>", so the browser
# can keep those files for a year and only fetches one again after it changes.
ASSET_REF = re.compile(r'(?P<attr>(?:src|href))="(?P<path>[^"?#:]+\.(?:js|css))"')
INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.DOTALL)


def content_security_policy(page: str) -> str:
    """What the page is allowed to load and run.

    Scripts: only this server's own files, plus the one small script written
    into index.html (allowed by its hash). So if text from a transit agency
    (an alert, a station name) ever slipped through unescaped, the browser
    still would not run it.
    """
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode() + "'"
        for m in INLINE_SCRIPT.finditer(page))
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' {hashes}".rstrip(),
        "style-src 'self' 'unsafe-inline'",
        f"img-src 'self' data: blob: {ONLINE_MAP_HOSTS}",
        f"connect-src 'self' {ONLINE_MAP_HOSTS}",
        "font-src 'self'",
        "worker-src 'self' blob:",
        "child-src blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ])


class IndexPage:
    """index.html with versioned asset links and its security policy, rebuilt
    only when a file changes."""

    def __init__(self, web_dir: Path = WEB_DIR):
        self.dir = web_dir
        self._key: tuple | None = None
        self._body = b""
        self._csp = ""

    def response(self) -> Response:
        page_path = self.dir / "index.html"
        page = page_path.read_text()
        files = [page_path] + [self.dir / m.group("path") for m in ASSET_REF.finditer(page)]
        key = tuple((f, f.stat().st_mtime_ns) for f in files if f.exists())
        if key != self._key:
            def stamp(m: re.Match) -> str:
                f = self.dir / m.group("path")
                if not f.is_file():
                    return m.group(0)
                digest = hashlib.sha1(f.read_bytes()).hexdigest()[:10]
                return f'{m.group("attr")}="{m.group("path")}?v={digest}"'
            self._body, self._csp, self._key = ASSET_REF.sub(stamp, page).encode(), content_security_policy(page), key
        return Response(self._body, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache", "Content-Security-Policy": self._csp})


class WebFiles(StaticFiles):
    """Static files: versioned links (?v=) are cached for a year, anything else is re-checked."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            versioned = b"v=" in scope.get("query_string", b"")
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-cache"
        return response


# ---- safety layers (plain ASGI: no per-request overhead to speak of) ---------------------
class SecurityHeaders:
    """Headers every response gets: browsers must not guess content types, and
    the app's address isn't passed on to other sites when a link is followed."""

    HEADERS = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin")]

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + self.HEADERS
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodyLimit:
    """Refuse request bodies over `max_bytes`. The only thing a client ever
    sends is a few settings, so anything large is a mistake or an attack."""

    def __init__(self, app: ASGIApp, max_bytes: int = 64 * 1024):
        self.app = app
        self.max_bytes = max_bytes

    async def _too_large(self, send: Send) -> None:
        body = json.dumps({"detail": f"Request body too large (limit {self.max_bytes // 1024} KB)"}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await self._too_large(send)
            return
        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    # No length was declared (chunked): stop reading. The route
                    # turns this into a 413 answer.
                    raise HTTPException(413, f"Request body too large (limit {self.max_bytes // 1024} KB)")
            return message

        await self.app(scope, limited_receive, send)
