"""Global error handler for bot updates.

Logs the exception with the update id, tells the user that something went wrong (localized; callback spinners are
stopped), alerts the admins at most once per minute per error signature and reports to Sentry when ``SENTRY_DSN`` is
configured. Users who blocked the bot are marked ``bot_blocked`` instead.
"""

from __future__ import annotations

import html
import logging
import re
import traceback
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ErrorEvent, Update

from app.middlewares.common import from_user, respond, translator_of
from app.services import notifications
from app.services.notify import mark_blocked

if TYPE_CHECKING:
    from aiogram import Bot

    from app.core.context import AppContext

log = logging.getLogger(__name__)

ALERT_INTERVAL_SEC = 60.0
MAX_ALERT_TRACE = 1500
# Bot tokens and credentials in URLs must not leak into admin chats.
_SECRETS = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}|(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")


def mask_secrets(text: str) -> str:
    """Replace bot tokens and ``user:password@`` URL credentials with ``***``."""
    return _SECRETS.sub("***", text)


def error_signature(exc: BaseException) -> str:
    """``Type@file:line`` of the innermost frame: identical bugs share it, so alerts can be rate-limited."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return type(exc).__name__
    last = frames[-1]
    filename = last.filename.replace("\\", "/").rsplit("/", 2)
    return f"{type(exc).__name__}@{'/'.join(filename[-2:])}:{last.lineno}"


def _short_trace(exc: BaseException) -> str:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=6))
    return text[-MAX_ALERT_TRACE:]


def capture_sentry(exc: BaseException, update: Update | None) -> None:
    """Send ``exc`` to Sentry if the SDK is installed and initialized (no-op otherwise)."""
    try:
        import sentry_sdk
    except ImportError:
        return
    try:
        if not sentry_sdk.is_initialized():
            return
        with sentry_sdk.new_scope() as scope:
            if update is not None:
                scope.set_tag("update_type", update.event_type)
                scope.set_extra("update_id", update.update_id)
            sentry_sdk.capture_exception(exc)
    except Exception:  # pragma: no cover - telemetry must never break error handling
        log.debug("sentry capture failed", exc_info=True)


def _is_harmless(exc: BaseException) -> bool:
    """Errors that need no reply and no alert."""
    if isinstance(exc, TelegramBadRequest):
        text = str(exc).lower()
        return "message is not modified" in text or "query is too old" in text or "query id is invalid" in text
    return False


class ErrorHandler:
    """Register with ``dispatcher.errors.register(ErrorHandler(ctx).handle)``."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def handle(self, event: ErrorEvent, **data: Any) -> bool:
        exc = event.exception
        update = event.update
        bot: Bot | None = data.get("bot")
        if _is_harmless(exc):
            log.debug("update %s: %s", update.update_id, exc)
            return True
        user = from_user(data)
        if isinstance(exc, TelegramForbiddenError):
            if user is not None:
                await self._mark_blocked(user.id)
            log.info("update %s: user %s blocked the bot", update.update_id, user.id if user else None)
            return True

        log.error(
            "update %s (%s) from user %s failed",
            update.update_id,
            update.event_type,
            user.id if user else None,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        capture_sentry(exc, update)
        if bot is not None:
            _ = translator_of(data, self._ctx)
            await respond(update, bot, _("error.generic"), popup=_("error.alert"), alert=True)
        self._alert_admins(exc, update, user.id if user else None)
        return True

    def _alert_admins(self, exc: BaseException, update: Update, user_id: int | None) -> None:
        i18n = self._ctx.i18n
        signature = error_signature(exc)
        text = i18n.t(
            "admin_notify.error",
            i18n.default_lang,
            signature=html.escape(signature),
            update_id=update.update_id,
            update_type=html.escape(str(update.event_type)),
            user_id=user_id if user_id is not None else "-",
            error=html.escape(mask_secrets(f"{type(exc).__name__}: {exc}")[:300]),
            trace=html.escape(mask_secrets(_short_trace(exc))),
        )
        notifications.notify(
            self._ctx,
            notifications.AdminEvent.ALERTS,
            text,
            throttle_key=f"error:{signature}",
            throttle_sec=ALERT_INTERVAL_SEC,
        )

    async def _mark_blocked(self, user_id: int) -> None:
        try:
            await mark_blocked(self._ctx.session_factory, user_id)
        except Exception:
            log.exception("could not mark user %s as blocked", user_id)
