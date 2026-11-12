"""Web cabinet API: logging in (link, Telegram widget, e-mail), the session, and the account's e-mail address.

``/api/cabinet/auth/*`` is public (rate limited, same-site origin only). ``/api/app/cabinet/*`` is part of the
Mini App API: it accepts Mini App initData as well as a cabinet session.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import User
from app.db.session import read_session
from app.modules.miniapp_api.deps import maintenance_guard
from app.services import cabinet, mailer, users
from app.utils.privacy import client_ip_for_record
from app.web import auth
from app.web.auth import MiniAppUser
from app.web.deps import Ctx
from app.web.errors import ApiError
from app.web.security import client_ip

log = logging.getLogger(__name__)

TEXT = "web_cabinet."
LOGIN_ATTEMPTS = (10, 15 * 60)  # per client network and window
EMAIL_REQUESTS_PER_IP = (5, 15 * 60)
EMAIL_REQUESTS_PER_ADDRESS = (3, 15 * 60)
VERIFY_REQUESTS_PER_USER = (5, 3600)
LOGIN_LINKS_PER_USER = (10, 3600)
IPV6_PREFIX = 64

auth_router = APIRouter(prefix="/api/cabinet/auth", tags=["Web cabinet: login"])
account_router = APIRouter(
    prefix="/api/app/cabinet",
    tags=["Web cabinet: account"],
    dependencies=[Depends(auth.miniapp_init_data), Depends(maintenance_guard)],
)


class Ok(BaseModel):
    ok: bool = True


class ConfigOut(BaseModel):
    brand: str
    link: bool
    telegram: bool
    email: bool
    bot_username: str | None = None


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class EmailIn(BaseModel):
    email: str = Field(min_length=1, max_length=320)


class SessionOut(BaseModel):
    user_id: int
    csrf_token: str
    expires_at: datetime


class EmailOut(BaseModel):
    email: str | None
    available: bool


class LinkOut(BaseModel):
    url: str


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def methods(ctx: Any) -> dict[str, bool]:
    """Which login methods are usable right now."""
    settings: Settings = ctx.settings
    return {
        "link": settings.cabinet_login_bot,
        "telegram": settings.cabinet_login_telegram and settings.bot_token is not None and bool(ctx.bot_username),
        "email": settings.cabinet_login_email and mailer.smtp_configured(settings),
    }


def _bucket(ip: str) -> str:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((address, IPV6_PREFIX), strict=False))
    return ip


async def _hit(request: Request, key: str, limit: tuple[int, int]) -> None:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    result = await limiter.hit(key, *limit)
    if not result.allowed:
        raise ApiError(
            429,
            "too_many_requests",
            "Too many attempts, try again later",
            headers={"Retry-After": str(result.retry_after)},
        )


def _public_base(settings: Settings) -> str | None:
    return (settings.public_base_url or "").rstrip("/") or None


def cabinet_url(settings: Settings, path: str) -> str | None:
    base = _public_base(settings)
    return f"{base}/cabinet/{path}" if base else None


def _address_key(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()[:32]


async def _account_error(session: AsyncSession, settings: Settings, user: User | None) -> ApiError | None:
    """Why this account may not use the cabinet (the same rules as the Mini App), or None."""
    if user is None:
        return ApiError(403, "not_registered", "Open the bot first")
    if user.is_banned:
        return ApiError(403, "banned", "Access denied")
    if settings.app_mode == "personal" and not await auth.personal_access(session, settings, user):
        return ApiError(403, "invite_only", "Access is by invitation only")
    return None


async def _start_session(
    request: Request, response: Response, ctx: Any, user_id: int, method: str, session: AsyncSession
) -> SessionOut:
    settings: Settings = ctx.settings
    ip = client_ip(request, settings)
    token, row = await cabinet.create_session(
        session, settings, user_id, method, ip=ip, user_agent=request.headers.get("user-agent")
    )
    log.info("cabinet: user %s logged in (%s)", user_id, method)
    auth.set_cabinet_cookie(response, request, settings, token)
    return SessionOut(user_id=user_id, csrf_token=cabinet.csrf_token_for(token), expires_at=row.expires_at)


def _mail_text(ctx: Any, lang: str, key: str, **kw: Any) -> str:
    return ctx.i18n.t(TEXT + key, lang, **kw)


async def _deliver(settings: Settings, to: str, subject: str, body: str) -> None:
    """Background delivery: a failure is logged by the mailer and never reaches the requester."""
    try:
        await mailer.send_mail(settings, to, subject, body)
    except mailer.MailError:
        log.info("cabinet: e-mail to a customer was not delivered")


# ---------------------------------------------------------------------------------------------
# Public login API
# ---------------------------------------------------------------------------------------------
@auth_router.get("/config", response_model=ConfigOut)
async def login_config(ctx: Ctx) -> ConfigOut:
    """Login methods for the cabinet login page."""
    enabled = methods(ctx)
    settings: Settings = ctx.settings
    return ConfigOut(
        brand=settings.brand_name,
        link=enabled["link"],
        telegram=enabled["telegram"],
        email=enabled["email"],
        bot_username=ctx.bot_username or None,
    )


@auth_router.post("/redeem", response_model=SessionOut)
async def redeem(body: TokenIn, request: Request, response: Response, ctx: Ctx) -> SessionOut:
    """Exchange a one-time login link (from the bot or an e-mail) for a session cookie."""
    settings: Settings = ctx.settings
    auth.check_origin(request, settings)
    bucket = f"cabinet:login:{_bucket(client_ip(request, settings))}"
    await _hit(request, bucket, LOGIN_ATTEMPTS)
    enabled = methods(ctx)
    async with ctx.session_factory.begin() as session:
        data = await cabinet.redeem_token(session, cabinet.LOGIN_PREFIX, body.token)
        if data is None:
            raise ApiError(401, "invalid_token", "The login link is invalid or expired; request a new one")
        method = "email" if data.get("method") == "email" else "bot"
        if not enabled["email" if method == "email" else "link"]:
            raise ApiError(403, "method_disabled", "This login method is turned off")
        try:
            user_id = int(data["user_id"])
        except (KeyError, TypeError, ValueError):
            raise ApiError(401, "invalid_token", "The login link is invalid or expired; request a new one") from None
        problem = await _account_error(session, settings, await users.get(session, user_id))
        if problem is not None:
            raise problem
        out = await _start_session(request, response, ctx, user_id, method, session)
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None:
        await limiter.reset(bucket)
    return out


@auth_router.post("/telegram", response_model=SessionOut)
async def telegram_login(
    request: Request, response: Response, ctx: Ctx, payload: Annotated[dict[str, str], Body()]
) -> SessionOut:
    """Log in with the data of the Telegram Login Widget (signature and freshness are checked)."""
    settings: Settings = ctx.settings
    auth.check_origin(request, settings)
    if not methods(ctx)["telegram"] or settings.bot_token is None:
        raise ApiError(403, "method_disabled", "This login method is turned off")
    await _hit(request, f"cabinet:login:{_bucket(client_ip(request, settings))}", LOGIN_ATTEMPTS)
    try:
        who = cabinet.verify_widget_login(payload, settings.bot_token.get_secret_value())
    except cabinet.WidgetError as exc:
        source = client_ip_for_record(client_ip(request, settings), settings.client_ip_logging) or "-"
        log.info("cabinet: rejected Telegram login from %s: %s", source, exc)
        raise ApiError(401, "invalid_login", "Telegram login data is invalid or expired; try again") from None
    async with ctx.session_factory.begin() as session:
        if not await cabinet.claim_widget_payload(session, who.digest):
            raise ApiError(401, "invalid_login", "Telegram login data is invalid or expired; try again")
        user = await users.get(session, who.id)
        if user is None and settings.app_mode != "personal":
            user, _ = await users.get_or_create(session, settings, who.id, who.username, who.first_name, None)
        problem = await _account_error(session, settings, user)
        if problem is not None:
            raise problem
        return await _start_session(request, response, ctx, who.id, "telegram", session)


@auth_router.post("/email/request", response_model=Ok)
async def email_login_request(body: EmailIn, request: Request, ctx: Ctx, background: BackgroundTasks) -> Ok:
    """Mail a login link to a confirmed address. The answer is the same whether or not the address is known."""
    settings: Settings = ctx.settings
    auth.check_origin(request, settings)
    if not methods(ctx)["email"]:
        raise ApiError(403, "method_disabled", "This login method is turned off")
    email = cabinet.normalize_email(body.email)
    if email is None:
        raise ApiError(400, "bad_email", "Enter a valid e-mail address")
    await _hit(request, f"cabinet:mail:ip:{_bucket(client_ip(request, settings))}", EMAIL_REQUESTS_PER_IP)
    await _hit(request, f"cabinet:mail:to:{_address_key(email)}", EMAIL_REQUESTS_PER_ADDRESS)
    async with read_session(ctx.session_factory) as session:
        user_id = await cabinet.user_id_for_email(session, email)
        user = await users.get(session, user_id) if user_id is not None else None
        problem = await _account_error(session, settings, user)
    if user is not None and problem is None:
        token = await cabinet.issue_token(
            ctx.session_factory, cabinet.LOGIN_PREFIX, {"user_id": user.tg_id, "method": "email"}, cabinet.LOGIN_TTL
        )
        link = cabinet_url(settings, f"login/{token}")
        if link is not None:
            minutes = int(cabinet.LOGIN_TTL.total_seconds() // 60)
            subject = _mail_text(ctx, user.language, "mail.login_subject", brand=settings.brand_name)
            text = _mail_text(
                ctx, user.language, "mail.login_body", brand=settings.brand_name, link=link, minutes=minutes
            )
            background.add_task(_deliver, settings, email, subject, text)
    return Ok()


@auth_router.post("/verify-email", response_model=EmailOut)
async def verify_email(body: TokenIn, request: Request, ctx: Ctx) -> EmailOut:
    """Confirm an address from the link mailed by ``POST /api/app/cabinet/email``."""
    settings: Settings = ctx.settings
    auth.check_origin(request, settings)
    await _hit(request, f"cabinet:login:{_bucket(client_ip(request, settings))}", LOGIN_ATTEMPTS)
    async with ctx.session_factory.begin() as session:
        data = await cabinet.redeem_token(session, cabinet.VERIFY_PREFIX, body.token)
        if data is None:
            raise ApiError(401, "invalid_token", "The confirmation link is invalid or expired")
        try:
            user_id, email = int(data["user_id"]), str(data["email"])
        except (KeyError, TypeError, ValueError):
            raise ApiError(401, "invalid_token", "The confirmation link is invalid or expired") from None
        if not await cabinet.bind_email(session, user_id, email):
            raise ApiError(409, "email_taken", "This address is already used by another account")
        log.info("cabinet: user %s confirmed an e-mail address", user_id)
    return EmailOut(email=email, available=True)


@auth_router.get("/session", response_model=SessionOut)
async def current_session(request: Request, ctx: Ctx) -> SessionOut:
    """The logged-in customer and the CSRF token for mutating requests (401 when not logged in)."""
    settings: Settings = ctx.settings
    token = auth.cabinet_token_from(request, settings)
    principal = await auth.cabinet_principal(request, settings, ctx)
    if principal is None or not token:
        raise ApiError(401, "unauthorized", "Not logged in")
    return SessionOut(
        user_id=principal.user_id, csrf_token=cabinet.csrf_token_for(token), expires_at=principal.expires_at
    )


@auth_router.post("/logout", response_model=Ok)
async def logout(request: Request, response: Response, ctx: Ctx) -> Ok:
    """End the current session."""
    settings: Settings = ctx.settings
    principal = await auth.cabinet_principal(request, settings, ctx)
    if principal is not None:
        async with ctx.session_factory.begin() as session:
            await cabinet.revoke_session(session, principal.session_id, principal.user_id)
    auth.clear_cabinet_cookie(response, request, settings)
    return Ok()


@auth_router.post("/logout-all", response_model=Ok)
async def logout_everywhere(request: Request, response: Response, ctx: Ctx) -> Ok:
    """End every session of the customer, on all devices."""
    settings: Settings = ctx.settings
    principal = await auth.cabinet_principal(request, settings, ctx)
    if principal is not None:
        async with ctx.session_factory.begin() as session:
            count = await cabinet.revoke_all(session, principal.user_id)
            log.info("cabinet: user %s ended %d sessions", principal.user_id, count)
    auth.clear_cabinet_cookie(response, request, settings)
    return Ok()


# ---------------------------------------------------------------------------------------------
# Account API (initData or cabinet session)
# ---------------------------------------------------------------------------------------------
@account_router.get("/email", response_model=EmailOut)
async def get_email(ctx: Ctx, user: MiniAppUser) -> EmailOut:
    async with read_session(ctx.session_factory) as session:
        row = await cabinet.get_email(session, user.tg_id)
    return EmailOut(email=row.email if row else None, available=methods(ctx)["email"])


@account_router.post("/email", response_model=Ok)
async def start_email_verification(
    body: EmailIn, request: Request, ctx: Ctx, user: MiniAppUser, background: BackgroundTasks
) -> Ok:
    """Mail a confirmation link to a new address (it is attached to the account once the link is opened)."""
    settings: Settings = ctx.settings
    if not methods(ctx)["email"]:
        raise ApiError(503, "email_unavailable", "E-mail login is not available")
    email = cabinet.normalize_email(body.email)
    if email is None:
        raise ApiError(400, "bad_email", "Enter a valid e-mail address")
    await _hit(request, f"cabinet:verify:{user.tg_id}", VERIFY_REQUESTS_PER_USER)
    await _hit(request, f"cabinet:mail:to:{_address_key(email)}", EMAIL_REQUESTS_PER_ADDRESS)
    async with read_session(ctx.session_factory) as session:
        owner = await cabinet.user_id_for_email(session, email)
    if owner is None or owner == user.tg_id:  # an address of another account gets no mail, and no hint either
        token = await cabinet.issue_token(
            ctx.session_factory,
            cabinet.VERIFY_PREFIX,
            {"user_id": user.tg_id, "email": email},
            cabinet.VERIFY_TTL,
        )
        link = cabinet_url(settings, f"verify/{token}")
        if link is not None:
            subject = _mail_text(ctx, user.language, "mail.verify_subject", brand=settings.brand_name)
            text = _mail_text(ctx, user.language, "mail.verify_body", brand=settings.brand_name, link=link)
            background.add_task(_deliver, settings, email, subject, text)
    return Ok()


@account_router.delete("/email", response_model=Ok)
async def delete_email(ctx: Ctx, user: MiniAppUser) -> Ok:
    async with ctx.session_factory.begin() as session:
        await cabinet.remove_email(session, user.tg_id)
    return Ok()


@account_router.post("/login-link", response_model=LinkOut)
async def login_link(request: Request, ctx: Ctx, user: MiniAppUser) -> LinkOut:
    """A one-time link that opens the cabinet in a regular browser (for Mini App users)."""
    settings: Settings = ctx.settings
    if not methods(ctx)["link"]:
        raise ApiError(404, "method_disabled", "This login method is turned off")
    await _hit(request, f"cabinet:link:{user.tg_id}", LOGIN_LINKS_PER_USER)
    token = await cabinet.issue_token(
        ctx.session_factory, cabinet.LOGIN_PREFIX, {"user_id": user.tg_id, "method": "bot"}, cabinet.LOGIN_TTL
    )
    url = cabinet_url(settings, f"login/{token}")
    if url is None:
        raise ApiError(503, "web_unavailable", "PUBLIC_BASE_URL is not configured")
    return LinkOut(url=url)
