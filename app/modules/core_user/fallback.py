"""Last-resort handlers, included after every module's routers: outdated buttons and messages nobody expects.

Without them a tap on a button of a switched-off feature (or an old message) would leave the client spinning and a
random message would get no answer at all.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import AdminRole
from app.keyboards import Nav, kb, to_menu
from app.modules.core_user.bot import show_menu

# Messages that deserve an "I did not understand" answer (service messages such as payments never do).
_CHATTY = frozenset(
    {
        ContentType.TEXT,
        ContentType.PHOTO,
        ContentType.STICKER,
        ContentType.VOICE,
        ContentType.VIDEO,
        ContentType.VIDEO_NOTE,
        ContentType.DOCUMENT,
        ContentType.AUDIO,
        ContentType.ANIMATION,
        ContentType.LOCATION,
        ContentType.CONTACT,
    }
)


def build_fallback_router() -> Router:
    """A fresh router (a router can be attached to one dispatcher only)."""
    router = Router(name="core_user_fallback")
    router.callback_query.register(stale_button)
    router.message.register(unknown_message, IsPrivate(), F.content_type.in_(_CHATTY))
    return router


async def stale_button(
    callback: CallbackQuery,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    admin_role: AdminRole | None = None,
) -> None:
    """A button no handler accepted: the feature is off or the message is old. Show the menu again."""
    data = callback.data or ""
    key = "error.unavailable" if data.startswith(f"{Nav.__prefix__}:") else "error.expired_button"
    chat = callback.message.chat if callback.message is not None else None
    if chat is None or chat.type != "private":
        # A shared message (an admin group notice, say) belongs to everyone there: never replace it with a menu.
        await answer_callback(callback, _(key), alert=True)
        return
    await show_menu(callback, ctx, settings, _, admin_role, answer=_(key))


async def unknown_message(message: Message, _: Translator) -> None:
    await render(message, Screen(_("error.unknown_message"), kb([[to_menu(_)]])))


async def install_fallback(ctx: AppContext) -> None:
    """Startup hook: append the fallback router after all module routers."""
    ctx.dispatcher.include_router(build_fallback_router())
