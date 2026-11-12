"""Closed bot for ``APP_MODE=personal``: only owners, admins and members get in.

A member is a user who has a subscription (given by an invite or an admin) that is not being removed; it is
checked on every update, so a removal takes effect at once. Anyone else may only open a valid
invitation link (``/start inv_<code>``: active, not expired, uses left); the ``/start`` handler redeems it. Everyone
else gets the "access by invitation only" screen. Payment confirmations, group chats and the legal commands are not
affected. In ``APP_MODE=shop`` this middleware does nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.db.models import Invite
from app.db.session import read_session
from app.middlewares.common import (
    command_of,
    from_user,
    is_admin,
    is_open_command,
    is_payment_update,
    is_private,
    respond,
    translator_of,
)
from app.services import invites
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)


def invite_usable(invite: Invite, now: datetime) -> bool:
    """The invite can still be redeemed by someone."""
    if not invite.is_active:
        return False
    if invite.expires_at is not None and invite.expires_at <= now:
        return False
    return invite.max_uses == 0 or invite.uses < invite.max_uses


def invite_payload(update: Update) -> str | None:
    """The ``inv_<code>`` payload of a ``/start`` message, if any."""
    cmd = command_of(update)
    if cmd is None or cmd[0] != "start":
        return None
    payload = cmd[1]
    return payload if payload.lower().startswith(invites.DEEP_LINK_PREFIX) else None


class AccessPolicyMiddleware(BaseMiddleware):
    """Outer ``update`` middleware; runs after the user and throttling middlewares."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if self._ctx.settings.app_mode != "personal" or not isinstance(event, Update):
            return await handler(event, data)
        user = from_user(data)
        if (
            user is None
            or is_admin(data)
            or is_payment_update(event)
            or is_open_command(event)
            or not is_private(event, data)
        ):
            return await handler(event, data)
        if await self._is_member(user.id):
            return await handler(event, data)
        payload = invite_payload(event)
        if payload is not None and await self._invite_valid(payload):
            return await handler(event, data)
        command = command_of(event)
        if command is not None and command[0] == "start" and command[1].startswith("import_"):
            from app.services.imports import re_token

            if re_token(command[1][len("import_") :]):
                return await handler(event, data)
        _ = translator_of(data, self._ctx)
        log.info("personal mode: refused update %s from user %s", event.update_id, user.id)
        key = "access.invite_invalid" if payload is not None else "access.invite_only"
        await respond(event, data["bot"], _(key), alert=True)
        return None

    async def _is_member(self, user_id: int) -> bool:
        async with read_session(self._ctx.session_factory) as session, session.begin():
            return await invites.is_member(session, user_id)

    async def _invite_valid(self, payload: str) -> bool:
        async with read_session(self._ctx.session_factory) as session, session.begin():
            invite = await invites.get_by_code(session, payload)
            return invite is not None and invite_usable(invite, utcnow())
