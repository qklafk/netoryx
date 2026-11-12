"""Safe message delivery: handles blocked users, flood limits and bad requests."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from sqlalchemy import update

from app.config import Settings
from app.db.models import User
from app.db.session import SessionFactory

log = logging.getLogger(__name__)


async def mark_blocked(session_factory: SessionFactory, chat_id: int, blocked: bool = True) -> None:
    async with session_factory.begin() as session:
        await session.execute(update(User).where(User.tg_id == chat_id).values(bot_blocked=blocked))


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    session_factory: SessionFactory | None = None,
    **kwargs: Any,
) -> bool:
    """Send a message, never raising. Returns True on success.

    * TelegramForbiddenError -> user blocked the bot; marks `bot_blocked` if a session factory is given.
    * TelegramRetryAfter -> waits and retries (up to 3 times).
    """
    for _ in range(3):
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except (TelegramForbiddenError, TelegramNotFound):
            if session_factory is not None:
                try:
                    await mark_blocked(session_factory, chat_id)
                except Exception:  # pragma: no cover - best effort
                    log.exception("failed to mark user %s as blocked", chat_id)
            return False
        except TelegramBadRequest as e:
            log.warning("send_message to %s failed: %s", chat_id, e)
            return False
        except Exception:
            log.exception("send_message to %s failed", chat_id)
            return False
    return False


async def notify_admins(bot: Bot, settings: Settings, text: str, **kwargs: Any) -> None:
    for chat_id in settings.report_chat_ids:
        await safe_send(bot, chat_id, text, **kwargs)
