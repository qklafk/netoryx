"""Injects the application context and the current settings into every update."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

if TYPE_CHECKING:
    from app.core.context import AppContext


class ContextMiddleware(BaseMiddleware):
    """Outermost application middleware: ``ctx`` and ``settings`` (a snapshot taken once per update)."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["ctx"] = self._ctx
        data["settings"] = self._ctx.settings
        return await handler(event, data)
