"""Shared plumbing of the Mini App API: feature gates, maintenance, per-user rate limits, localized errors."""

from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Request

from app.config import Settings
from app.core.i18n import Translator
from app.core.modules import SHOP_ONLY_MODULES
from app.core.permissions import resolve_role
from app.db.models import User
from app.db.session import read_session
from app.services.notifications import AdminEvent, notify
from app.services.qr import make_qr_png_async
from app.web.auth import MiniAppUser
from app.web.deps import Ctx
from app.web.errors import ApiError

log = logging.getLogger(__name__)

MODULE = "miniapp_api"
TEXT_PREFIX = f"{MODULE}."
QR_BOX_SIZE = 6  # pixels per module: small enough for a data URL, sharp on phone screens

# Feature names are module names: a feature is off when its module is switched off (or, when the module is not
# installed, when the mode preset turns selling features off).
SUBSCRIPTION = "subscription"
PLANS = "plans"
PAYMENTS = "payments"
WALLET = "wallet"
REFERRAL = "referral"
PROMO = "promo"
GIFTS = "gifts"
TICKETS = "tickets"
TG_PROXY = "tg_proxy"
FEATURES: tuple[str, ...] = (SUBSCRIPTION, PLANS, PAYMENTS, WALLET, REFERRAL, PROMO, GIFTS, TICKETS, TG_PROXY)


def now_utc() -> datetime:
    return datetime.now(UTC)


def translator(ctx: Any, user: User) -> Translator:
    """Texts in the user's language."""
    settings: Settings = ctx.settings
    return ctx.i18n.translator(user.language, settings.zone)


def text(ctx: Any, user: User, key: str, /, **kw: Any) -> str:
    """A module text (``miniapp_api.<key>``) in the user's language."""
    return translator(ctx, user)(TEXT_PREFIX + key, **kw)


def optional_text(ctx: Any, user: User, key: str, /, **kw: Any) -> str | None:
    """Like :func:`text`, but None for keys without a text (no "missing key" warning)."""
    full = TEXT_PREFIX + key
    if ctx.i18n.template(full, user.language) is None:
        return None
    return text(ctx, user, key, **kw)


def error(
    ctx: Any,
    user: User,
    status_code: int,
    code: str,
    key: str | None = None,
    /,
    *,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    **kw: Any,
) -> ApiError:
    """An :class:`ApiError` whose message is the localized ``miniapp_api.error.<key or code>`` text."""
    message = text(ctx, user, f"error.{key or code}", **kw)
    return ApiError(status_code, code, message, headers=headers, extra=extra)


# ---------------------------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------------------------
def _setting_allows(settings: Settings, name: str) -> bool:
    if name == REFERRAL:
        return settings.referral_enabled
    if name == GIFTS:
        return settings.gifts_enabled
    return True


def feature_configured(ctx: Any, name: str) -> bool:
    """Whether a feature is on for everybody (module switch, mode preset, its own setting)."""
    settings: Settings = ctx.settings
    modules = getattr(ctx, "modules", None)
    if modules is not None and name in modules:
        if not modules.is_enabled(name, settings):
            return False
    else:
        if settings.app_mode == "personal" and name in SHOP_ONLY_MODULES:
            return False
        if name in settings.disabled_modules:
            return False
    return _setting_allows(settings, name)


async def is_staff(ctx: Any, user_id: int) -> bool:
    settings: Settings = ctx.settings
    if user_id in settings.admin_ids:
        return True
    async with read_session(ctx.session_factory) as session:
        return await resolve_role(session, settings, user_id) is not None


async def feature_on(ctx: Any, name: str, user_id: int) -> bool:
    """Whether ``name`` is available to this user right now (also honours a partial maintenance)."""
    if not feature_configured(ctx, name):
        return False
    if name == TG_PROXY and ctx.settings.tg_proxy_access == "off":
        return False  # the legacy proxy is hidden everywhere until the owner turns it on
    maintenance = getattr(ctx, "maintenance", None)
    if maintenance is None:
        return True
    if not await maintenance.module_blocked(name, user_id, is_admin=False):
        return True
    return await is_staff(ctx, user_id)


async def features_for(ctx: Any, user_id: int) -> dict[str, bool]:
    return {name: await feature_on(ctx, name, user_id) for name in FEATURES}


def require_feature(*names: str) -> Callable[..., Awaitable[None]]:
    """Dependency: 404 ``feature_disabled`` unless every feature in ``names`` is available to the user."""

    async def dependency(ctx: Ctx, user: MiniAppUser) -> None:
        for name in names:
            if not await feature_on(ctx, name, user.tg_id):
                raise error(ctx, user, 404, "feature_disabled")

    dependency.__name__ = "require_feature_" + "_".join(names)
    return dependency


def needs(*names: str) -> Any:
    return Depends(require_feature(*names))


# ---------------------------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------------------------
async def maintenance_guard(ctx: Ctx, user: MiniAppUser) -> None:
    """503 ``maintenance`` while a full maintenance is on (admins and the allowlist pass)."""
    service = getattr(ctx, "maintenance", None)
    if service is None:
        return
    now = now_utc()
    state = await service.state()
    if not state.is_active(now):
        return
    if await is_staff(ctx, user.tg_id):
        return
    if not await service.check(user.tg_id, is_admin=False, now=now):
        return
    eta = state.effective_eta(now)
    custom = state.message(user.language, now)
    message = custom or text(ctx, user, "error.maintenance")
    raise ApiError(503, "maintenance", message, extra={"eta": eta.isoformat() if eta else None})


# ---------------------------------------------------------------------------------------------
# Rate limits (per user, on top of the per-IP limit of the whole /api/app tree)
# ---------------------------------------------------------------------------------------------
async def rate_limit(request: Request, ctx: Any, user: User, action: str, limit: int, window_sec: int) -> None:
    """Count one ``action`` of the user; 429 with ``Retry-After`` once ``limit`` per ``window_sec`` is exceeded."""
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    result = await limiter.hit(f"miniapp:{action}:{user.tg_id}", limit, window_sec)
    if not result.allowed:
        raise error(
            ctx,
            user,
            429,
            "too_many_requests",
            headers={"Retry-After": str(result.retry_after)},
            extra={"retry_after": result.retry_after},
        )


# ---------------------------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------------------------
def public_base(ctx: Any, user: User) -> str:
    """``PUBLIC_BASE_URL`` without the trailing slash; 503 when it is not configured."""
    base = (ctx.settings.public_base_url or "").rstrip("/")
    if not base:
        raise error(ctx, user, 503, "web_unavailable")
    return base


def subscription_url(base: str, sub_token: str) -> str:
    return f"{base}/sub/{sub_token}"


async def qr_data_url(data: str) -> str | None:
    """PNG QR code as a ``data:`` URL (None when the data does not fit into a QR code)."""
    try:
        png = await make_qr_png_async(data, box_size=QR_BOX_SIZE)
    except ValueError:
        log.debug("QR code: %d characters do not fit", len(data))
        return None
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def notify_admins(ctx: Any, event: AdminEvent, key: str, /, **kw: Any) -> None:
    """Queue an admin notification (module text in the default language). Never raises."""
    try:
        message = ctx.i18n.t(TEXT_PREFIX + key, ctx.settings.default_language, **kw)
        notify(ctx, event, message)
    except Exception:
        log.exception("mini app: admin notification %s failed", key)
