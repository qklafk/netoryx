"""Registers / loads the Telegram user and injects ``db_user``, ``user_created``, ``lang``, ``_`` and ``admin_role``.

* Private-chat updates register new users (:func:`app.services.users.get_or_create`, short transaction). Updates from
  group chats only look an existing user up, so members of the admin chat are not registered as customers.
* The profile and ``last_seen_at`` are written at most once per :data:`~app.services.users.LAST_SEEN_GRANULARITY`
  per user (unless the username or name changed); other updates use a read-only session.
* Users on the external blacklist (:mod:`app.services.blacklist`) are banned or flagged on their first update;
  the check is a set lookup, administrators are never affected.
* Banned users get a short notice (at most once per :data:`BANNED_NOTICE_TTL`) and their updates are dropped -
  except payment confirmations, which must always be processed (pre-checkout queries are declined).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from aiogram.types import User as TgUser

from app.db.session import read_session
from app.middlewares.common import TTLCache, answer_silently, from_user, is_payment_update, is_private, respond
from app.services import admins, blacklist, users

if TYPE_CHECKING:
    from app.core.context import AppContext
    from app.db.models import AdminRole, User

log = logging.getLogger(__name__)

BANNED_NOTICE_TTL = 3600.0
_PROFILE_CACHE_SIZE = 50_000


class UserMiddleware(BaseMiddleware):
    """Outer ``update`` middleware; runs right after :class:`~app.middlewares.context.ContextMiddleware`."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        # tg_id -> (username, first_name) last written; entries expire after LAST_SEEN_GRANULARITY
        self._fresh: TTLCache[int, tuple[str | None, str | None]] = TTLCache(_PROFILE_CACHE_SIZE)
        self._banned_noticed: TTLCache[int, bool] = TTLCache(_PROFILE_CACHE_SIZE)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings = data.get("settings") or self._ctx.settings
        tg_user = from_user(data)
        db_user: User | None = None
        created = False
        role: AdminRole | None = None
        if isinstance(event, Update) and tg_user is not None and not tg_user.is_bot:
            if is_private(event, data):
                db_user, created, role = await self._load_or_register(tg_user)
            else:
                db_user, role = await self._lookup(tg_user.id)

        lang = self._ctx.i18n.normalize(
            db_user.language
            if db_user is not None
            else users.pick_language(tg_user.language_code if tg_user is not None else None, settings)
        )
        data["db_user"] = db_user
        data["user_created"] = created
        data["admin_role"] = role
        data["lang"] = lang
        data["_"] = self._ctx.i18n.translator(lang, settings.zone)

        banned = db_user is not None and db_user.is_banned
        if settings.blacklist_enabled:
            blacklist.CACHE.schedule_reload(self._ctx.session_factory)
            if (
                db_user is not None
                and not banned
                and role is None
                and isinstance(event, Update)
                and blacklist.CACHE.listed(db_user.tg_id, settings)
                and not _is_payment_confirmation(event)
            ):
                banned = await _on_listed_user(self._ctx, db_user.tg_id)
        if (
            db_user is not None
            and banned
            and role is None
            and isinstance(event, Update)
            and not _is_payment_confirmation(event)
        ):
            await self._drop_banned(event, data, db_user)
            return None
        return await handler(event, data)

    async def _load_or_register(self, tg_user: TgUser) -> tuple[User | None, bool, AdminRole | None]:
        profile = (users.normalize_username(tg_user.username), tg_user.first_name[:256] if tg_user.first_name else None)
        if self._fresh.get(tg_user.id) == profile:
            user, role = await self._lookup(tg_user.id)
            if user is not None and not user.bot_blocked:
                return user, False, role
        settings = self._ctx.settings
        async with self._ctx.session_factory.begin() as session:
            user, created = await users.get_or_create(
                session, settings, tg_user.id, tg_user.username, tg_user.first_name, tg_user.language_code
            )
            role = await admins.resolve_role(session, settings, tg_user.id)
        self._fresh.set(tg_user.id, profile, users.LAST_SEEN_GRANULARITY.total_seconds())
        return user, created, role

    async def _lookup(self, tg_id: int) -> tuple[User | None, AdminRole | None]:
        async with read_session(self._ctx.session_factory) as session, session.begin():
            user = await users.get(session, tg_id)
            role = await admins.resolve_role(session, self._ctx.settings, tg_id)
        return user, role

    async def _drop_banned(self, event: Update, data: dict[str, Any], user: User) -> None:
        bot = data["bot"]
        _ = data["_"]
        if event.pre_checkout_query is not None:  # decline: a banned user cannot buy
            await respond(event, bot, _("access.banned"))
        elif user.tg_id not in self._banned_noticed:
            self._banned_noticed.set(user.tg_id, True, BANNED_NOTICE_TTL)
            await respond(event, bot, _("access.banned"), alert=True)
        else:
            await answer_silently(event, bot)
        log.debug("dropped update %s from banned user %s", event.update_id, user.tg_id)


async def _on_listed_user(ctx: AppContext, tg_id: int) -> bool:
    """Ban or flag a user found on the external blacklist; True if the update must be dropped."""
    from app.modules.blacklist.service import on_listed_user

    return await on_listed_user(ctx, tg_id)


def _is_payment_confirmation(update: Update) -> bool:
    """Money already moved (successful / refunded payment): always let it through."""
    return is_payment_update(update) and update.pre_checkout_query is None
