"""Notices to users about what an admin did in the web admin (sent in the background, after the commit)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.db.models import User
from app.db.session import read_session
from app.modules.admin_api.deps import spawn
from app.modules.admin_api.texts import t
from app.services.notify import safe_send
from app.utils.money import rub

log = logging.getLogger(__name__)

BULK_SEND_INTERVAL_SEC = 0.05

TextBuilder = Callable[[str], str]  # language -> text


def money(ctx: Any, kop: int, lang: str) -> str:
    i18n = getattr(ctx, "i18n", None)
    if i18n is not None:
        try:
            return str(i18n.money(kop, lang))
        except Exception:
            log.debug("i18n money formatting failed", exc_info=True)
    return f"{rub(kop)} ₽"


def date(ctx: Any, value: datetime, lang: str) -> str:
    i18n = getattr(ctx, "i18n", None)
    zone = ctx.settings.zone
    if i18n is not None:
        try:
            return str(i18n.date(value, lang, zone))
        except Exception:
            log.debug("i18n date formatting failed", exc_info=True)
    return value.astimezone(zone).strftime("%d.%m.%Y")


async def _languages(ctx: Any, user_ids: Iterable[int]) -> dict[int, str]:
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return {}
    async with read_session(ctx.session_factory) as session:
        rows = await session.execute(
            select(User.tg_id, User.language).where(
                User.tg_id.in_(ids), User.is_banned.is_(False), User.bot_blocked.is_(False)
            )
        )
        return {int(tg_id): str(language) for tg_id, language in rows}


async def _send_many(ctx: Any, user_ids: list[int], build: TextBuilder) -> int:
    if getattr(ctx, "bot", None) is None:
        return 0
    sent = 0
    for start in range(0, len(user_ids), 500):
        languages = await _languages(ctx, user_ids[start : start + 500])
        for user_id, lang in languages.items():
            if await safe_send(ctx.bot, user_id, build(lang), session_factory=ctx.session_factory):
                sent += 1
            await asyncio.sleep(BULK_SEND_INTERVAL_SEC)
    return sent


def notify_user(ctx: Any, user_id: int, build: TextBuilder) -> None:
    """Send one user a notice in their language (skipped for banned users and those who blocked the bot)."""
    spawn(_send_many(ctx, [user_id], build), f"admin-notify-{user_id}")


def notify_users(ctx: Any, user_ids: Iterable[int], build: TextBuilder) -> None:
    """Paced notices to many users."""
    ids = list(dict.fromkeys(user_ids))
    if ids:
        spawn(_send_many(ctx, ids, build), f"admin-notify-{len(ids)}")


def text(ctx: Any, key: str, **params: Any) -> TextBuilder:
    """A builder rendering ``key`` with ``params`` in the recipient's language."""
    return lambda lang: t(ctx, key, lang, **params)
