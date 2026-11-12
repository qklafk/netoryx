"""Single-page apps: the web admin at ``/admin`` and the Telegram Mini App at ``/app``.

Built files are served from ``<static dir>/admin`` and ``<static dir>/app`` with client-side routing fallback to
``index.html``. Without a build a short placeholder page explains how to get one.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings
from app.web.pages import page

DEFAULT_STATIC_DIR = Path(__file__).resolve().parents[3] / "web" / "dist"
IMMUTABLE = "public, max-age=31536000, immutable"


class SpaStaticFiles(StaticFiles):
    """Static files with an ``index.html`` fallback for client-side routes and cache headers for hashed assets."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or Path(path).suffix:
                raise  # a missing asset is a real 404; extension-less paths are app routes
            response = await super().get_response("index.html", scope)
        if Path(path).parts[:1] == ("assets",) and response.status_code == 200:
            response.headers["Cache-Control"] = IMMUTABLE
        else:
            response.headers.setdefault("Cache-Control", "no-cache")
        return response


def _placeholder(title: str, what: str) -> Response:
    body = (
        f"<p>The {what} is not built yet.</p>"
        "<p>Use the Docker image, or run <code>npm ci &amp;&amp; npm run build</code> in the <code>web</code> "
        "directory and restart the bot.</p>"
    )
    return page(title, body, status_code=503)


class SpaMount:
    """ASGI app for one SPA: switched on/off at runtime by a settings flag."""

    def __init__(
        self,
        directory: Path,
        *,
        title: str,
        what: str,
        enabled: Callable[[Settings], bool],
        settings_getter: Callable[[], Settings],
    ) -> None:
        self.directory = directory
        self.title = title
        self.what = what
        self.enabled = enabled
        self.settings_getter = settings_getter
        self.files: ASGIApp | None = (
            SpaStaticFiles(directory=directory, html=True) if (directory / "index.html").is_file() else None
        )

    @property
    def built(self) -> bool:
        return self.files is not None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - mounts only receive http here
            return
        if not self.enabled(self.settings_getter()):
            await page("Not found", "<p>This page is disabled.</p>", status_code=404)(scope, receive, send)
            return
        if self.files is None:
            await _placeholder(self.title, self.what)(scope, receive, send)
            return
        await self.files(scope, receive, send)


class CabinetMount(SpaMount):
    """The web cabinet: the Mini App build served under ``/cabinet``.

    The page is the Mini App ``index.html`` with its asset URLs moved under ``/cabinet`` and without the Telegram
    WebApp script; the app notices it runs in a browser by its URL. Hashed assets come from the same build.
    """

    TELEGRAM_SCRIPT = re.compile(r"<script[^>]*telegram-web-app\.js[^>]*></script>")

    def __init__(
        self,
        directory: Path,
        *,
        enabled: Callable[[Settings], bool],
        settings_getter: Callable[[], Settings],
    ) -> None:
        super().__init__(
            directory, title="Web cabinet", what="web cabinet", enabled=enabled, settings_getter=settings_getter
        )
        self.index_html: bytes | None = None
        index = directory / "index.html"
        if index.is_file():
            html = index.read_text(encoding="utf-8").replace('"/app/', '"/cabinet/')
            self.index_html = self.TELEGRAM_SCRIPT.sub("", html).encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.index_html is not None and self.enabled(self.settings_getter()):
            path = scope["path"]
            root = scope.get("root_path", "")
            if root and path.startswith(root):
                path = path[len(root) :]
            if not Path(path).suffix:  # application routes (login, profile, ...) all answer with the page
                if scope["method"] not in ("GET", "HEAD"):
                    await Response(status_code=405, headers={"Allow": "GET, HEAD"})(scope, receive, send)
                    return
                response = Response(
                    self.index_html if scope["method"] == "GET" else b"",
                    media_type="text/html",
                    headers={"Cache-Control": "no-cache"},
                )
                await response(scope, receive, send)
                return
        await super().__call__(scope, receive, send)
