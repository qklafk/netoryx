"""Maintenance mode for bot updates (state and admin actions: :mod:`app.services.maintenance`).

* Full maintenance (manual or a scheduled window): users get the maintenance text (the admin's custom message or the
  default one, plus the expected end time) and are remembered, so the "we're back" notice reaches them later.
  Administrators and the allowlist keep full access.
* Partial maintenance (some features switched off): the modules' handlers are disabled by the ``ModuleEnabled``
  filter; an update that nothing handled while a partial maintenance is active gets a short "this feature is under
  maintenance" answer instead of silence.
* Payment confirmations (pre-checkout queries, successful payments) are never blocked: money must not get stuck.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import TelegramObject, Update

from app.middlewares.common import (
    from_user,
    is_admin,
    is_open_command,
    is_payment_update,
    is_private,
    respond,
    translator_of,
)
from app.utils.time import utcnow

if TYPE_CHECKING:
    from datetime import datetime

    from app.core.context import AppContext
    from app.core.i18n import Translator
    from app.services.maintenance import MaintenanceState

log = logging.getLogger(__name__)

ETA_LINE_KEY = "access.maintenance_until"  # optional "expected end: {eta}" line under a custom message


def maintenance_text(_: Translator, state: MaintenanceState, now: datetime) -> str:
    """The text shown to blocked users: the admin's custom message (escaped) or the default one, with the ETA."""
    eta = state.effective_eta(now)
    eta = eta if eta is not None and eta > now else None
    custom = state.message(_.lang, now)
    if custom:
        text = _.esc(custom)
        if eta is not None and _.i18n.has(ETA_LINE_KEY):
            text += "\n\n" + _(ETA_LINE_KEY, eta=_.datetime(eta))
        return text
    if eta is not None:
        return _("access.maintenance_eta", eta=_.datetime(eta))
    return _("access.maintenance")


class MaintenanceMiddleware(BaseMiddleware):
    """Outer ``update`` middleware; runs after the user, throttling and access middlewares."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update) or not self._applies(event, data):
            return await handler(event, data)
        user = from_user(data)
        admin = is_admin(data)
        state = await self._ctx.maintenance.state()
        now = utcnow()
        user_id = user.id if user is not None else None
        if state.blocks(user_id, is_admin=admin, now=now):
            if user_id is not None:
                await self._ctx.maintenance.record_hit(user_id, state.epoch(now))
            _ = translator_of(data, self._ctx)
            text = maintenance_text(_, state, now)
            await respond(event, data["bot"], text, popup=_("access.maintenance_alert"), alert=True)
            return None

        result = await handler(event, data)
        if (
            result is UNHANDLED
            and state.disabled_modules
            and not state.is_exempt(user_id, is_admin=admin)
            and (event.message is not None or event.callback_query is not None)
        ):
            _ = translator_of(data, self._ctx)
            await respond(event, data["bot"], _("error.unavailable"), alert=True)
            return None
        return result

    @staticmethod
    def _applies(event: Update, data: dict[str, Any]) -> bool:
        if event.message is None and event.callback_query is None and event.inline_query is None:
            return False
        if is_payment_update(event) or is_open_command(event):
            return False
        return is_private(event, data)
