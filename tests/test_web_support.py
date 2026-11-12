"""Test helpers for the web layer: a minimal in-process ASGI client with a cookie jar and a fake app context."""

from __future__ import annotations

import asyncio
import json as jsonlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message

from app.config import Settings
from app.core.modules import FeatureModule, ModuleRegistry
from app.db.session import SessionFactory
from app.web.app import create_web_app


@dataclass
class Resp:
    status: int
    headers: Headers
    body: bytes

    def json(self) -> Any:
        return jsonlib.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode()

    def set_cookies(self) -> dict[str, str]:
        """``name -> raw Set-Cookie header`` of this response."""
        out: dict[str, str] = {}
        for raw in self.headers.getlist("set-cookie"):
            name = raw.split("=", 1)[0]
            out[name] = raw
        return out


class AsgiClient:
    """Drives an ASGI app directly (no network, no extra dependencies)."""

    def __init__(self, app: ASGIApp, *, client_ip: str = "203.0.113.5", host: str = "testserver") -> None:
        self.app = app
        self.client_ip = client_ip
        self.host = host
        self.cookies: dict[str, str] = {}

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        body: bytes | None = None,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
        client_ip: str | None = None,
        host: str | None = None,
        scheme: str = "http",
        send_cookies: bool = True,
    ) -> Resp:
        parts = urlsplit(url)
        hdrs: dict[str, str] = {"host": host or self.host}
        if json is not None:
            body = jsonlib.dumps(json).encode()
            hdrs["content-type"] = "application/json"
        if chunks is None:
            payload = body or b""
            if method not in ("GET", "HEAD") or payload:
                hdrs["content-length"] = str(len(payload))
            chunks = [payload]
        if send_cookies and self.cookies:
            hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        for k, v in (headers or {}).items():
            hdrs[k.lower()] = v
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": scheme,
            "path": parts.path,
            "raw_path": parts.path.encode(),
            "query_string": parts.query.encode(),
            "root_path": "",
            "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in hdrs.items()],
            "client": (client_ip or self.client_ip, 50000),
            "server": (host or self.host, 80),
        }
        pending = list(chunks)
        status = 0
        resp_headers: list[tuple[bytes, bytes]] = []
        out = bytearray()
        done = False

        async def receive() -> Message:
            if pending:
                chunk = pending.pop(0)
                return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
            await asyncio.Event().wait()  # the client stays connected until the response is complete
            return {"type": "http.disconnect"}  # pragma: no cover

        async def send(message: Message) -> None:
            nonlocal status, resp_headers, done
            if message["type"] == "http.response.start":
                status = message["status"]
                resp_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                out.extend(message.get("body", b""))
                if not message.get("more_body"):
                    done = True

        try:
            await self.app(scope, receive, send)
        except Exception:
            if not done:  # the server error middleware re-raises after answering 500
                raise
        resp = Resp(status, Headers(raw=resp_headers), bytes(out))
        self._store_cookies(resp)
        return resp

    def _store_cookies(self, resp: Resp) -> None:
        for raw in resp.headers.getlist("set-cookie"):
            cookie: SimpleCookie = SimpleCookie()
            cookie.load(raw)
            for name, morsel in cookie.items():
                if morsel["max-age"] == "0" or (morsel["expires"] and "1970" in morsel["expires"]):
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value

    async def get(self, url: str, **kw: Any) -> Resp:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> Resp:
        return await self.request("POST", url, **kw)


@dataclass
class FakeDispatcher:
    fed: list[Any] = field(default_factory=list)
    fail: bool = False

    async def feed_update(self, bot: Any, update: Any) -> None:
        if self.fail:
            raise RuntimeError("handler failed")
        self.fed.append((bot, update))


@dataclass
class FakeCtx:
    """Just the attributes of the application context the web layer uses."""

    settings: Settings
    session_factory: SessionFactory
    modules: ModuleRegistry = field(default_factory=lambda: ModuleRegistry([]))
    bot: Any = None
    dispatcher: FakeDispatcher = field(default_factory=FakeDispatcher)
    redis: Any = None
    bot_username: str = "test_vpn_bot"
    webhook_secret: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    credited: list[Any] = field(default_factory=list)
    on_credit: Callable[[Any], Awaitable[None]] | None = None

    async def on_payment_credited(self, result: Any) -> None:
        self.credited.append(result)
        if self.on_credit is not None:
            await self.on_credit(result)


def web_app(ctx: FakeCtx, **kw: Any) -> FastAPI:
    """``create_web_app`` for a fake context."""
    return create_web_app(cast(Any, ctx), **kw)


def make_ctx(settings: Settings, session_factory: SessionFactory, *modules: FeatureModule, **kw: Any) -> FakeCtx:
    return FakeCtx(settings=settings, session_factory=session_factory, modules=ModuleRegistry(modules), **kw)
