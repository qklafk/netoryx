"""Authentication for the web admin (one-time login links -> session cookie + CSRF) and the Mini App (initData).

Admin login flow: an admin sends ``/web`` to the bot, which calls :func:`create_login_token` and replies with
:func:`login_url`. The SPA reads the token from the URL fragment (never sent to the server or logged) and posts it
to ``POST /api/admin/auth/magic``, which sets an HttpOnly session cookie. Mutating requests must carry
``X-CSRF-Token`` (returned by ``/magic`` and ``/me``, also readable from a non-HttpOnly cookie).
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.permissions import Permission, permissions_of, resolve_role
from app.db.models import AdminRole, AdminSession, KeyValue, User
from app.db.session import SessionFactory, read_session
from app.services import audit, cabinet, invites, kv, users
from app.utils.security import const_eq, random_token, sha256_hex
from app.web.deps import Ctx
from app.web.errors import ApiError
from app.web.ratelimit import RateLimiter
from app.web.security import client_ip, is_local_host
from app.web.tma import InitDataError, WebAppInitData, WebAppUser, validate_init_data

log = logging.getLogger(__name__)

SESSION_COOKIE = "vpnbot_session"
CSRF_COOKIE = "vpnbot_csrf"
SECURE_PREFIX = "__Host-"  # browser-enforced: Secure, Path=/, no Domain -> cannot be planted by subdomains
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

LOGIN_TOKEN_BYTES = 32
LOGIN_TOKEN_TTL = timedelta(minutes=5)
LOGIN_KEY_PREFIX = "weblogin:"
SESSION_TOKEN_BYTES = 32
SESSION_TOUCH_INTERVAL = timedelta(minutes=1)  # sliding expiry is persisted at most this often
MAX_SESSION_AGE = timedelta(days=30)  # absolute lifetime regardless of activity

# Abuse protection of the login endpoint (on top of the generic per-IP limit). Login tokens are 256-bit random,
# so there is deliberately no shared counter: anonymous clients must not be able to lock the owner out.
LOGIN_ATTEMPTS_PER_IP = 10
LOGIN_ATTEMPTS_WINDOW_SEC = 15 * 60
LOGIN_IPV6_PREFIX = 64  # IPv6 clients usually get a whole /64, so attempts are counted per network

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------------------------
# One-time login tokens
# ---------------------------------------------------------------------------------------------
def _login_key(token: str) -> str:
    digest = hashlib.sha256(token.encode()).digest()
    return LOGIN_KEY_PREFIX + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def create_login_token(
    session_factory: SessionFactory,
    admin_id: int,
    *,
    ttl: timedelta = LOGIN_TOKEN_TTL,
    now: datetime | None = None,
) -> str:
    """Issue a single-use login token for ``admin_id`` (only its hash is stored, valid for ``ttl``)."""
    token = random_token(LOGIN_TOKEN_BYTES)
    expires = (now or _now()) + ttl
    async with session_factory.begin() as session:
        await kv.add(session, _login_key(token), json.dumps({"admin_id": admin_id, "exp": expires.timestamp()}))
    return token


def login_url(settings: Settings, token: str) -> str | None:
    """Link to open in a browser; the token travels in the fragment so it never reaches server logs."""
    if not settings.public_base_url:
        return None
    return f"{settings.public_base_url}/admin/login#token={token}"


async def create_login_link(session_factory: SessionFactory, settings: Settings, tg_id: int) -> str:
    """Issue a login token for ``tg_id`` and return the link to open (for the bot's ``/web`` command and the CLI).

    Without ``PUBLIC_BASE_URL`` the link is relative (``/admin/login#token=...``): append it to the server address.
    """
    token = await create_login_token(session_factory, tg_id)
    return login_url(settings, token) or f"/admin/login#token={token}"


async def redeem_login_token(session: AsyncSession, token: str, *, now: datetime | None = None) -> int | None:
    """Consume a login token in the caller's transaction; the admin id, or None if unknown/used/expired."""
    if not _TOKEN_RE.match(token):
        return None
    key = _login_key(token)
    raw = await kv.get(session, key)
    if raw is None or not await kv.delete(session, key):  # delete() is the single-use guard under concurrency
        return None
    try:
        data = json.loads(raw)
        admin_id = int(data["admin_id"])
        expires = float(data["exp"])
    except (ValueError, KeyError, TypeError):
        return None
    if (now or _now()).timestamp() >= expires:
        return None
    return admin_id


async def purge_expired_login_tokens(session_factory: SessionFactory, *, now: datetime | None = None) -> int:
    """Delete expired login tokens; returns how many were removed."""
    current = (now or _now()).timestamp()
    removed = 0
    async with session_factory.begin() as session:
        rows = (await session.execute(select(KeyValue.key, KeyValue.value).where(_login_keys()))).all()
        stale = [key for key, value in rows if _expired(value, current)]
        if stale:
            result = await session.execute(delete(KeyValue).where(KeyValue.key.in_(stale)))
            removed = int(getattr(result, "rowcount", 0) or 0)
    return removed


def _login_keys() -> Any:
    return KeyValue.key.startswith(LOGIN_KEY_PREFIX, autoescape=True)


def _expired(raw: str, now_ts: float) -> bool:
    try:
        return float(json.loads(raw)["exp"]) <= now_ts
    except (ValueError, KeyError, TypeError):
        return True


# ---------------------------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    """The authenticated web admin of a request."""

    tg_id: int
    role: AdminRole
    session_id: int
    ip: str
    permissions: frozenset[Permission]
    expires_at: datetime

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions


def csrf_token_for(session_token: str) -> str:
    """CSRF token bound to a session: only someone who can read the HttpOnly session cookie can derive it."""
    return sha256_hex("csrf:" + session_token)[:43]


def _session_expiry(settings: Settings, created_at: datetime, now: datetime) -> datetime:
    return min(now + timedelta(hours=max(1, settings.web_session_hours)), created_at + MAX_SESSION_AGE)


async def create_session(
    session: AsyncSession,
    settings: Settings,
    admin_id: int,
    *,
    ip: str | None,
    user_agent: str | None,
    now: datetime | None = None,
) -> tuple[str, AdminSession]:
    """Create a web session in the caller's transaction; returns ``(cookie token, row)``."""
    current = now or _now()
    token = random_token(SESSION_TOKEN_BYTES)
    row = AdminSession(
        admin_id=admin_id,
        token_hash=sha256_hex(token),
        ip=ip[:64] if ip else None,
        user_agent=user_agent[:256] if user_agent else None,
        created_at=current,
        expires_at=_session_expiry(settings, current, current),
        last_used_at=current,
        revoked=False,
    )
    session.add(row)
    await session.flush()
    return token, row


def _is_active(row: AdminSession, now: datetime) -> bool:
    return not row.revoked and row.expires_at > now and row.created_at + MAX_SESSION_AGE > now


async def authenticate_session(
    session_factory: SessionFactory,
    settings: Settings,
    token: str,
    *,
    ip: str,
    now: datetime | None = None,
) -> AdminPrincipal | None:
    """Resolve a session cookie to a principal (role re-checked every time) and slide its expiry."""
    if not _TOKEN_RE.match(token):
        return None
    current = now or _now()
    async with read_session(session_factory) as session:
        row = await session.scalar(select(AdminSession).where(AdminSession.token_hash == sha256_hex(token)))
        if row is None or not _is_active(row, current):
            return None
        role = await resolve_role(session, settings, row.admin_id)
    if role is None:  # demoted or removed since login
        async with session_factory.begin() as session:
            await revoke_session(session, row.id)
        return None
    expires_at = row.expires_at
    if row.last_used_at is None or current - row.last_used_at >= SESSION_TOUCH_INTERVAL:
        expires_at = _session_expiry(settings, row.created_at, current)
        async with session_factory.begin() as session:
            await session.execute(
                update(AdminSession)
                .where(AdminSession.id == row.id, AdminSession.revoked.is_(False))
                .values(last_used_at=current, expires_at=expires_at, ip=ip[:64])
            )
    return AdminPrincipal(
        tg_id=row.admin_id,
        role=role,
        session_id=row.id,
        ip=ip,
        permissions=permissions_of(role),
        expires_at=expires_at,
    )


async def revoke_session(session: AsyncSession, session_id: int, *, admin_id: int | None = None) -> bool:
    """Revoke one session (optionally only if it belongs to ``admin_id``); True if it was active."""
    stmt = update(AdminSession).where(AdminSession.id == session_id, AdminSession.revoked.is_(False))
    if admin_id is not None:
        stmt = stmt.where(AdminSession.admin_id == admin_id)
    result: CursorResult[Any] = await session.execute(stmt.values(revoked=True))  # type: ignore[assignment]
    return bool(result.rowcount)


async def revoke_all(session: AsyncSession, admin_id: int) -> int:
    """Revoke every session of ``admin_id`` (e.g. when the admin is removed); returns how many."""
    result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
        update(AdminSession)
        .where(AdminSession.admin_id == admin_id, AdminSession.revoked.is_(False))
        .values(revoked=True)
    )
    return int(result.rowcount or 0)


async def list_sessions(
    session: AsyncSession, *, admin_id: int | None = None, now: datetime | None = None
) -> list[AdminSession]:
    """Active sessions, newest first (all admins, or one)."""
    current = now or _now()
    stmt = select(AdminSession).where(
        AdminSession.revoked.is_(False),
        AdminSession.expires_at > current,
        AdminSession.created_at > current - MAX_SESSION_AGE,
    )
    if admin_id is not None:
        stmt = stmt.where(AdminSession.admin_id == admin_id)
    return list((await session.scalars(stmt.order_by(AdminSession.id.desc()))).all())


async def purge_sessions(
    session_factory: SessionFactory, *, keep: timedelta = timedelta(days=30), now: datetime | None = None
) -> int:
    """Delete sessions that expired or were revoked more than ``keep`` ago."""
    cutoff = (now or _now()) - keep
    async with session_factory.begin() as session:
        result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
            delete(AdminSession).where(AdminSession.expires_at < cutoff)
        )
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------------------------
# Cookies & request checks
# ---------------------------------------------------------------------------------------------
def cookies_secure(request: Request, settings: Settings) -> bool:
    """Cookies are Secure everywhere except plain-HTTP localhost in DEV_MODE."""
    return not (settings.dev_mode and is_local_host(request))


def _cookie_name(base: str, secure: bool) -> str:
    return SECURE_PREFIX + base if secure else base


def session_token_from(request: Request, settings: Settings) -> str | None:
    return request.cookies.get(_cookie_name(SESSION_COOKIE, cookies_secure(request, settings))) or None


def set_session_cookies(response: Response, request: Request, settings: Settings, token: str) -> None:
    secure = cookies_secure(request, settings)
    max_age = int(MAX_SESSION_AGE.total_seconds())
    response.set_cookie(
        _cookie_name(SESSION_COOKIE, secure),
        token,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        _cookie_name(CSRF_COOKIE, secure),
        csrf_token_for(token),
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )


def clear_session_cookies(response: Response, request: Request, settings: Settings) -> None:
    secure = cookies_secure(request, settings)
    for base in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(
            _cookie_name(base, secure), path="/", secure=secure, httponly=base == SESSION_COOKIE, samesite="strict"
        )


CABINET_COOKIE = "vpnbot_cabinet"


def cabinet_token_from(request: Request, settings: Settings) -> str | None:
    return request.cookies.get(_cookie_name(CABINET_COOKIE, cookies_secure(request, settings))) or None


def set_cabinet_cookie(response: Response, request: Request, settings: Settings, token: str) -> None:
    """Customer session cookie: HttpOnly, Lax (so returning from a payment page keeps the login), Secure over HTTPS."""
    secure = cookies_secure(request, settings)
    response.set_cookie(
        _cookie_name(CABINET_COOKIE, secure),
        token,
        max_age=max(1, settings.cabinet_session_days) * 86400,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def clear_cabinet_cookie(response: Response, request: Request, settings: Settings) -> None:
    secure = cookies_secure(request, settings)
    response.delete_cookie(_cookie_name(CABINET_COOKIE, secure), path="/", secure=secure, httponly=True, samesite="lax")


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def check_origin(request: Request, settings: Settings) -> None:
    """Reject cross-site mutations (defence in depth on top of SameSite=Strict and the CSRF token)."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise ApiError(403, "csrf_failed", "Cross-site request rejected")
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        if origin == "null":
            raise ApiError(403, "csrf_failed", "Cross-site request rejected")
        return
    allowed = {_origin_of(str(request.base_url))}
    if settings.public_base_url:
        allowed.add(_origin_of(settings.public_base_url))
    if origin.lower() not in allowed:
        raise ApiError(403, "csrf_failed", "Cross-site request rejected")


def _check_csrf(request: Request, settings: Settings, session_token: str) -> None:
    check_origin(request, settings)
    header = request.headers.get(CSRF_HEADER, "")
    if not header or not const_eq(header, csrf_token_for(session_token)):
        raise ApiError(403, "csrf_failed", "Missing or invalid CSRF token")


async def _authenticate_request(request: Request, ctx: Any) -> AdminPrincipal:
    settings: Settings = ctx.settings
    if not settings.web_admin_enabled:
        raise ApiError(404, "not_found", "Web admin is disabled")
    token = session_token_from(request, settings)
    if not token:
        raise ApiError(401, "unauthorized", "Not logged in")
    if request.method not in SAFE_METHODS:
        _check_csrf(request, settings, token)
    principal = await authenticate_session(ctx.session_factory, settings, token, ip=client_ip(request, settings))
    if principal is None:
        raise ApiError(401, "session_expired", "Session expired, log in again")
    return principal


AdminDependency = Callable[..., Awaitable[AdminPrincipal]]


@cache
def admin_user(permission: Permission | None = None) -> AdminDependency:
    """Dependency factory: the logged-in admin, optionally required to hold ``permission`` (403 otherwise).

    Authentication runs once per request even if several routers/routes declare it (cached on request state).
    Mutating methods require a valid ``X-CSRF-Token`` header.
    """

    async def dependency(request: Request, ctx: Ctx) -> AdminPrincipal:
        principal: AdminPrincipal | None = getattr(request.state, "admin_principal", None)
        if principal is None:
            principal = await _authenticate_request(request, ctx)
            request.state.admin_principal = principal
        if permission is not None and permission not in principal.permissions:
            raise ApiError(403, "forbidden", "Not enough permissions")
        return principal

    dependency.__name__ = f"admin_user_{permission or 'any'}"
    return dependency


CurrentAdmin = Annotated[AdminPrincipal, Depends(admin_user())]


# ---------------------------------------------------------------------------------------------
# Admin auth API
# ---------------------------------------------------------------------------------------------
class MagicLoginIn(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class AdminMe(BaseModel):
    tg_id: int
    role: str
    permissions: list[str]
    csrf_token: str
    session_id: int
    expires_at: datetime


class Ok(BaseModel):
    ok: bool = True


def _limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def _me(principal: AdminPrincipal, token: str) -> AdminMe:
    return AdminMe(
        tg_id=principal.tg_id,
        role=str(principal.role),
        permissions=sorted(str(p) for p in principal.permissions),
        csrf_token=csrf_token_for(token),
        session_id=principal.session_id,
        expires_at=principal.expires_at,
    )


def _login_bucket(ip: str) -> str:
    """The address login attempts are counted for: the IP itself, or its /64 network for IPv6."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((address, LOGIN_IPV6_PREFIX), strict=False))
    return ip


router = APIRouter(prefix="/api/admin/auth", tags=["admin-auth"])


@router.post("/magic", response_model=AdminMe)
async def magic_login(body: MagicLoginIn, request: Request, response: Response, ctx: Ctx) -> AdminMe:
    """Exchange a one-time login token from the bot for a session cookie."""
    settings: Settings = ctx.settings
    if not settings.web_admin_enabled:
        raise ApiError(404, "not_found", "Web admin is disabled")
    check_origin(request, settings)
    ip = client_ip(request, settings)
    limiter = _limiter(request)
    attempts_key = f"login:{_login_bucket(ip)}"
    result = await limiter.hit(attempts_key, LOGIN_ATTEMPTS_PER_IP, LOGIN_ATTEMPTS_WINDOW_SEC)
    if not result.allowed:
        log.warning("web login: too many attempts from %s", ip)
        raise ApiError(
            429, "too_many_requests", "Too many login attempts", headers={"Retry-After": str(result.retry_after)}
        )

    now = _now()
    async with ctx.session_factory.begin() as session:
        admin_id = await redeem_login_token(session, body.token, now=now)
        role = await resolve_role(session, settings, admin_id) if admin_id is not None else None
        if admin_id is not None and role is not None:
            token, row = await create_session(
                session, settings, admin_id, ip=ip, user_agent=request.headers.get("user-agent"), now=now
            )
            await audit.log(session, admin_id, "web.login", "admin_session", row.id, source="web", ip=ip)
    if admin_id is None:
        log.info("web login: invalid or expired token from %s", ip)
        raise ApiError(401, "invalid_token", "The login link is invalid or expired; request a new one in the bot")
    if role is None:
        log.warning("web login: user %s is not an admin anymore", admin_id)
        raise ApiError(403, "forbidden", "Not an admin")

    await limiter.reset(attempts_key)
    set_session_cookies(response, request, settings, token)
    principal = AdminPrincipal(
        tg_id=admin_id,
        role=role,
        session_id=row.id,
        ip=ip,
        permissions=permissions_of(role),
        expires_at=row.expires_at,
    )
    return _me(principal, token)


@router.get("/me", response_model=AdminMe)
async def me(principal: CurrentAdmin, request: Request, ctx: Ctx) -> AdminMe:
    """The current admin, their permissions and the CSRF token for mutating requests."""
    token = session_token_from(request, ctx.settings) or ""
    return _me(principal, token)


@router.post("/logout", response_model=Ok)
async def logout(principal: CurrentAdmin, request: Request, response: Response, ctx: Ctx) -> Ok:
    """Revoke the current session and clear the cookies."""
    async with ctx.session_factory.begin() as session:
        await revoke_session(session, principal.session_id)
        await audit.log(
            session, principal.tg_id, "web.logout", "admin_session", principal.session_id, source="web", ip=principal.ip
        )
    clear_session_cookies(response, request, ctx.settings)
    return Ok()


# ---------------------------------------------------------------------------------------------
# Mini App
# ---------------------------------------------------------------------------------------------
async def cabinet_principal(request: Request, settings: Settings, ctx: Any) -> cabinet.CabinetPrincipal | None:
    """The web cabinet session of the request (None: no cookie). Mutating requests need the CSRF header and origin."""
    token = cabinet_token_from(request, settings)
    if not token:
        return None
    if request.method not in SAFE_METHODS:
        check_origin(request, settings)
        header = request.headers.get(CSRF_HEADER, "")
        if not header or not const_eq(header, cabinet.csrf_token_for(token)):
            raise ApiError(403, "csrf_failed", "Missing or invalid CSRF token")
    principal = await cabinet.authenticate(ctx.session_factory, token)
    if principal is None:
        raise ApiError(401, "session_expired", "Session expired, log in again")
    return principal


async def _cabinet_init_data(request: Request, settings: Settings, ctx: Any) -> WebAppInitData | None:
    """The cabinet session shaped like Mini App data (None: no cookie)."""
    principal = await cabinet_principal(request, settings, ctx)
    if principal is None:
        return None
    request.state.cabinet_session_id = principal.session_id
    return WebAppInitData(
        user=WebAppUser(id=principal.user_id, first_name=None, last_name=None, username=None, language_code=None),
        auth_date=_now(),
        query_id=None,
        start_param=None,
        chat_type=None,
        fields={},
    )


def in_cabinet(request: Request | None) -> bool:
    """The request was authenticated by a web cabinet session (not by Mini App initData)."""
    return request is not None and getattr(request.state, "cabinet_session_id", None) is not None


async def miniapp_init_data(request: Request, ctx: Ctx) -> WebAppInitData:
    """Validated ``Authorization: tma <initData>`` of a Mini App request, or a web cabinet session cookie.

    Requests carrying an ``Authorization`` header are always judged as Mini App requests. Without one, the cabinet
    cookie is used when the web cabinet is on (mutating requests then need ``X-CSRF-Token`` and a same-site origin).
    """
    cached: WebAppInitData | None = getattr(request.state, "tma_init_data", None)
    if cached is not None:
        return cached
    settings: Settings = ctx.settings
    authorization = request.headers.get("authorization", "")
    if not authorization and settings.cabinet_enabled:
        data = await _cabinet_init_data(request, settings, ctx)
        if data is not None:
            request.state.tma_init_data = data
            return data
        if not settings.miniapp_enabled:
            raise ApiError(401, "unauthorized", "Not logged in")
    if not settings.miniapp_enabled:
        raise ApiError(404, "not_found", "Mini App is disabled")
    if settings.bot_token is None:
        raise ApiError(503, "unavailable", "Telegram Bot is not configured")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "tma" or not value.strip():
        raise ApiError(401, "unauthorized", "Mini App authorization is required")
    try:
        data = validate_init_data(value.strip(), settings.bot_token.get_secret_value())
    except InitDataError as exc:
        log.info("mini app: rejected initData from %s: %s", client_ip(request, settings), exc)
        raise ApiError(401, "invalid_init_data", "Mini App session is invalid or expired; reopen the app") from None
    request.state.tma_init_data = data
    return data


async def miniapp_user(request: Request, ctx: Ctx, init: Annotated[WebAppInitData, Depends(miniapp_init_data)]) -> User:
    """The bot user behind a Mini App request (registered on first use; banned users get 403).

    In personal mode users join by invitation through the bot, so unknown users are refused instead, and only
    members and admins get in (the same rule as the bot's access policy).
    """
    cached: User | None = getattr(request.state, "miniapp_user", None)
    if cached is not None:
        return cached
    settings: Settings = ctx.settings
    claiming = request.url.path == "/api/app/import/claim"
    async with read_session(ctx.session_factory) as session:
        user = await users.get(session, init.user.id)
        allowed = (
            claiming
            or settings.app_mode != "personal"
            or user is None
            or await personal_access(session, settings, user)
        )
    if user is None:
        if settings.app_mode == "personal" and not claiming:
            raise ApiError(403, "not_registered", "Open the bot first")
        async with ctx.session_factory.begin() as session:
            user, _ = await users.get_or_create(
                session,
                settings,
                init.user.id,
                init.user.username,
                init.user.first_name,
                init.user.language_code,
            )
    if user.is_banned:
        raise ApiError(403, "banned", "Access denied")
    if not allowed:
        raise ApiError(403, "invite_only", "Access is by invitation only")
    request.state.miniapp_user = user
    return user


async def personal_access(session: AsyncSession, settings: Settings, user: User) -> bool:
    """Personal mode: admins and members (a subscription that is not being removed) may use the Mini App."""
    if await resolve_role(session, settings, user.tg_id) is not None:
        return True
    return await invites.is_member(session, user.tg_id)


MiniAppUser = Annotated[User, Depends(miniapp_user)]
