"""Update middlewares and the global error handler.

Order (outer -> inner), all registered on ``dispatcher.update`` so they see every update type once:

1. :class:`ContextMiddleware` - ``ctx`` and ``settings``
2. :class:`UserMiddleware` - ``db_user``, ``user_created``, ``lang``, ``_``, ``admin_role``; drops banned users
3. :class:`ThrottlingMiddleware` - per-user token bucket (admins exempt)
4. :class:`AccessPolicyMiddleware` - ``APP_MODE=personal``: members and valid invites only
5. :class:`MaintenanceMiddleware` - full / partial maintenance
6. :class:`LegalConsentMiddleware` - one-time acceptance of the terms
7. :class:`ChannelGateMiddleware` - required channel subscription

Payment confirmations pass every gate. Errors raised by handlers reach :class:`ErrorHandler`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.middlewares.access import AccessPolicyMiddleware
from app.middlewares.channel_gate import ChannelGateMiddleware
from app.middlewares.consent import LegalConsentMiddleware
from app.middlewares.context import ContextMiddleware
from app.middlewares.errors import ErrorHandler
from app.middlewares.maintenance import MaintenanceMiddleware
from app.middlewares.replay import StartReplayer
from app.middlewares.throttling import ThrottlingMiddleware
from app.middlewares.user import UserMiddleware

if TYPE_CHECKING:
    from aiogram import Dispatcher

    from app.core.context import AppContext

__all__ = [
    "AccessPolicyMiddleware",
    "ChannelGateMiddleware",
    "ContextMiddleware",
    "ErrorHandler",
    "LegalConsentMiddleware",
    "MaintenanceMiddleware",
    "Middlewares",
    "StartReplayer",
    "ThrottlingMiddleware",
    "UserMiddleware",
    "setup_middlewares",
]


@dataclass(frozen=True, slots=True)
class Middlewares:
    """The registered instances (handlers use them to drop caches, e.g. ``channel_gate.forget(user_id)``)."""

    context: ContextMiddleware
    user: UserMiddleware
    throttling: ThrottlingMiddleware
    access: AccessPolicyMiddleware
    maintenance: MaintenanceMiddleware
    consent: LegalConsentMiddleware
    channel_gate: ChannelGateMiddleware
    errors: ErrorHandler
    replayer: StartReplayer


def setup_middlewares(dispatcher: Dispatcher, ctx: AppContext) -> Middlewares:
    """Register the middlewares in the documented order and the error handler; call once at startup."""
    replayer = StartReplayer()
    mw = Middlewares(
        context=ContextMiddleware(ctx),
        user=UserMiddleware(ctx),
        throttling=ThrottlingMiddleware(ctx),
        access=AccessPolicyMiddleware(ctx),
        maintenance=MaintenanceMiddleware(ctx),
        consent=LegalConsentMiddleware(ctx, replayer),
        channel_gate=ChannelGateMiddleware(ctx, replayer),
        errors=ErrorHandler(ctx),
        replayer=replayer,
    )
    for middleware in (mw.context, mw.user, mw.throttling, mw.access, mw.maintenance, mw.consent, mw.channel_gate):
        dispatcher.update.outer_middleware(middleware)
    dispatcher.errors.register(mw.errors.handle)
    return mw
