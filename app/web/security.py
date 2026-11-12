"""HTTP hardening: client IP behind trusted proxies, security headers, request ids + access log without secrets,
request body size limit and per-IP rate limits (pure ASGI middlewares)."""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.utils.privacy import client_ip_for_record
from app.web.errors import error_response
from app.web.masking import mask_url
from app.web.ratelimit import RateLimiter

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)
access_log = logging.getLogger("app.web.access")

SettingsGetter = Callable[[], "Settings"]
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Request id of the request being handled (for log records); None outside requests.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


# ---------------------------------------------------------------------------------------------
# Client IP
# ---------------------------------------------------------------------------------------------
@lru_cache(maxsize=32)
def _parse_networks(items: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    networks: list[IPNetwork] = []
    for item in items:
        try:
            networks.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError:
            log.warning("ignoring invalid TRUSTED_PROXIES entry %r", item)
    return tuple(networks)


def trusted_networks(settings: Settings) -> tuple[IPNetwork, ...]:
    """Parsed ``TRUSTED_PROXIES`` (cached)."""
    return _parse_networks(tuple(settings.trusted_proxies))


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    value = value.strip().strip('"')
    if not value or value.lower() == "unknown":
        return None
    if value.startswith("["):  # "[2001:db8::1]:443"
        value = value[1 : value.find("]")] if "]" in value else value[1:]
    elif value.count(":") == 1:  # "1.2.3.4:5678"
        value = value.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_trusted(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, networks: Iterable[IPNetwork]) -> bool:
    return any(ip.version == net.version and ip in net for net in networks)


def resolve_client_ip(
    peer: str | None,
    forwarded_for: str | None,
    real_ip: str | None,
    networks: Sequence[IPNetwork],
) -> str:
    """The originating client address.

    Proxy headers are honoured only when the direct peer is a trusted proxy; ``X-Forwarded-For`` is walked
    right to left and the first address that is not a trusted proxy wins (left-most entries are client-controlled).

    Proxies differ in which header they set and pass the other one through from the client unchanged (nginx with
    only ``X-Real-IP $remote_addr`` forwards a client-sent ``X-Forwarded-For``). So when both headers name a
    client and disagree, neither is trusted and the peer address is used. An ``X-Real-IP`` naming a trusted proxy
    (set by an inner proxy in a chain) carries no client information and is ignored.
    """
    peer_ip = _parse_ip(peer or "")
    if peer_ip is None:
        return peer or "unknown"
    if not _is_trusted(peer_ip, networks):
        return str(peer_ip)
    from_xff = _forwarded_client(forwarded_for, networks)
    from_real_ip = _parse_ip(real_ip) if real_ip else None
    if from_real_ip is not None and _is_trusted(from_real_ip, networks):
        from_real_ip = None
    if from_xff is not None and from_real_ip is not None and from_xff != str(from_real_ip):
        return str(peer_ip)
    if from_xff is not None:
        return from_xff
    if from_real_ip is not None:
        return str(from_real_ip)
    return str(peer_ip)


def _forwarded_client(forwarded_for: str | None, networks: Sequence[IPNetwork]) -> str | None:
    """The client named by an ``X-Forwarded-For`` chain (right-most untrusted hop), None if there is none."""
    if not forwarded_for:
        return None
    candidate: str | None = None
    for raw in reversed(forwarded_for.split(",")):
        ip = _parse_ip(raw)
        if ip is None:
            break
        candidate = str(ip)
        if not _is_trusted(ip, networks):
            return candidate
    return candidate


def _scope_client_ip(scope: Scope, settings: Settings) -> str:
    cached = scope.get("state", {}).get("client_ip")
    if isinstance(cached, str):
        return cached
    client = scope.get("client")
    headers = Headers(scope=scope)
    ip = resolve_client_ip(
        client[0] if client else None,
        headers.get("x-forwarded-for"),
        headers.get("x-real-ip"),
        trusted_networks(settings),
    )
    scope.setdefault("state", {})["client_ip"] = ip
    return ip


def client_ip(request: Request, settings: Settings) -> str:
    """Client IP of ``request`` honouring ``TRUSTED_PROXIES``."""
    return _scope_client_ip(request.scope, settings)


def is_https(request: Request, settings: Settings) -> bool:
    """True if the client connected over HTTPS (``X-Forwarded-Proto`` is honoured from trusted proxies only)."""
    if request.url.scheme == "https":
        return True
    client = request.client
    peer = _parse_ip(client.host) if client else None
    if peer is not None and _is_trusted(peer, trusted_networks(settings)):
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        return proto == "https"
    return False


def is_local_host(request: Request) -> bool:
    """True for requests addressed to localhost (local development)."""
    host = (request.headers.get("host") or request.url.hostname or "").lower()
    if host.startswith("["):
        host = host[1 : host.find("]")] if "]" in host else host
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")


# ---------------------------------------------------------------------------------------------
# Request id + access log
# ---------------------------------------------------------------------------------------------
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
QUIET_PATHS = frozenset({"/healthz"})


class RequestContextMiddleware:
    """Assigns a request id (``X-Request-ID``) and writes one access log line per request with secrets masked."""

    def __init__(self, app: ASGIApp, settings_getter: SettingsGetter) -> None:
        self.app = app
        self.settings_getter = settings_getter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else secrets.token_hex(8)
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            path = scope.get("path", "")
            level = logging.DEBUG if path in QUIET_PATHS and status < 400 else logging.INFO
            if access_log.isEnabledFor(level):
                try:
                    settings = self.settings_getter()
                    ip = client_ip_for_record(_scope_client_ip(scope, settings), settings.client_ip_logging) or "-"
                except Exception:  # pragma: no cover - logging must never break a request
                    ip = "?"
                query = scope.get("query_string", b"").decode("latin-1")
                access_log.log(
                    level,
                    "%s %s %s %d %.1fms rid=%s",
                    ip,
                    scope.get("method", "?"),
                    mask_url(path, query),
                    status,
                    elapsed_ms,
                    request_id,
                )
            request_id_var.reset(token)


# ---------------------------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------------------------
MINIAPP_PREFIX = "/app"
ADMIN_PREFIX = "/admin"
CABINET_PREFIX = "/cabinet"
API_PREFIX = "/api/"
TELEGRAM_FRAME_ANCESTORS = "https://web.telegram.org https://*.telegram.org"


def _spa_csp(frame_ancestors: str, *, extra: tuple[str, ...] = ()) -> str:
    return "; ".join(
        (
            "default-src 'self'",
            "script-src 'self' https://telegram.org",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob: https:",
            "font-src 'self' data:",
            "connect-src 'self'",
            f"frame-ancestors {frame_ancestors}",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
            *extra,
        )
    )


ADMIN_CSP = _spa_csp("'none'")
MINIAPP_CSP = _spa_csp(f"'self' {TELEGRAM_FRAME_ANCESTORS}")
CABINET_CSP = _spa_csp("'none'", extra=("frame-src https://oauth.telegram.org",))  # the Telegram Login Widget iframe
API_CSP = "default-src 'none'; frame-ancestors 'none'"
DEFAULT_CSP = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


class SecurityHeadersMiddleware:
    """Adds conservative security headers; values a route already set are kept."""

    def __init__(self, app: ASGIApp, settings_getter: SettingsGetter) -> None:
        self.app = app
        self.settings_getter = settings_getter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        headers_to_add = self._headers_for(path)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in headers_to_add.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _headers_for(self, path: str) -> dict[str, str]:
        headers = {
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
            "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
        }
        miniapp = _under(path, MINIAPP_PREFIX)
        if miniapp:
            headers["content-security-policy"] = MINIAPP_CSP  # Telegram Web clients embed Mini Apps in an iframe
        else:
            headers["x-frame-options"] = "DENY"
            if _under(path, ADMIN_PREFIX):
                headers["content-security-policy"] = ADMIN_CSP
                headers["cross-origin-opener-policy"] = "same-origin"
            elif _under(path, CABINET_PREFIX):
                headers["content-security-policy"] = CABINET_CSP
            elif path.startswith(API_PREFIX):
                headers["content-security-policy"] = API_CSP
                headers["cache-control"] = "no-store"
            else:
                headers["content-security-policy"] = DEFAULT_CSP
        try:
            base_url = self.settings_getter().public_base_url or ""
        except Exception:  # pragma: no cover - settings are always available in practice
            base_url = ""
        if base_url.startswith("https://"):
            headers["strict-transport-security"] = "max-age=31536000"
        return headers


# ---------------------------------------------------------------------------------------------
# Body size limit
# ---------------------------------------------------------------------------------------------
DEFAULT_BODY_LIMIT = 1024 * 1024  # 1 MiB: JSON API, Telegram updates, payment webhooks


class _BodyTooLarge(StarletteHTTPException):
    """Raised from ``receive``; an HTTPException so that FastAPI's body parsing re-raises it unchanged."""

    def __init__(self) -> None:
        super().__init__(413, "Request body is too large")


class BodySizeLimitMiddleware:
    """Rejects request bodies above a limit with 413 (checks ``Content-Length`` and counts streamed chunks).

    ``overrides`` maps exact paths to their own limits, e.g. the backup upload. Exact matching keeps the large
    limits away from neighbouring routes that parse a JSON body before authentication runs.
    """

    def __init__(self, app: ASGIApp, default_limit: int = DEFAULT_BODY_LIMIT, overrides: dict[str, int] | None = None):
        self.app = app
        self.default_limit = default_limit
        self.overrides = {path.rstrip("/"): limit for path, limit in (overrides or {}).items()}

    def limit_for(self, path: str) -> int:
        return self.overrides.get(path.rstrip("/"), self.default_limit)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope.get("path", ""))
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                await error_response(400, "bad_request", "Invalid Content-Length")(scope, receive, send)
                return
            if too_big:
                await _too_large(scope, receive, send)
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await _too_large(scope, receive, send)


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    await error_response(413, "payload_too_large", "Request body is too large")(scope, receive, send)


# ---------------------------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RateRule:
    """``limit`` requests per ``window_sec`` per client IP for paths starting with ``prefix``."""

    name: str
    prefix: str
    limit: int
    window_sec: int
    methods: frozenset[str] | None = None  # None = every method


DEFAULT_RATE_RULES: tuple[RateRule, ...] = (
    RateRule("auth", "/api/admin/auth/", 30, 60),
    RateRule("admin", "/api/admin/", 600, 60),
    RateRule("cabinet_auth", "/api/cabinet/auth/", 30, 60),
    RateRule("miniapp", "/api/app/", 240, 60),
    RateRule("public_api", "/api/public/", 120, 60),
    RateRule("sub", "/sub/", 60, 60),
    RateRule("open", "/open/", 60, 60),
    RateRule("gift", "/gift/", 30, 60),
    RateRule("dev", "/dev/", 30, 60),
)


@dataclass
class RateLimitMiddleware:
    """Per-IP limits on public routes; the first matching rule applies. Telegram and payment webhooks are not
    listed, so providers retrying deliveries are never throttled."""

    app: ASGIApp
    limiter_getter: Callable[[], RateLimiter]
    settings_getter: SettingsGetter
    rules: Sequence[RateRule] = field(default_factory=lambda: DEFAULT_RATE_RULES)

    def match(self, path: str, method: str) -> RateRule | None:
        for rule in self.rules:
            if path.startswith(rule.prefix) and (rule.methods is None or method in rule.methods):
                return rule
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rule = self.match(scope.get("path", ""), scope.get("method", "GET"))
        if rule is not None:
            ip = _scope_client_ip(scope, self.settings_getter())
            result = await self.limiter_getter().hit(f"{rule.name}:{ip}", rule.limit, rule.window_sec)
            if not result.allowed:
                response = error_response(
                    429,
                    "too_many_requests",
                    "Too many requests, try again later",
                    headers={"Retry-After": str(result.retry_after)},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
