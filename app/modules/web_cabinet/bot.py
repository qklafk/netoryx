"""The bot side of the web cabinet: a button and ``/cabinet`` that hand out a one-time login link."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import Screen, render
from app.keyboards import Button, Nav, kb, to_menu
from app.services import cabinet

log = logging.getLogger(__name__)

CABINET = "cabinet"  # Nav target

router = Router(name="web_cabinet")
router.message.filter(IsPrivate())
router.callback_query.filter(IsPrivate())


@router.message(Command("cabinet"))
@router.callback_query(Nav.filter(F.to == CABINET))
async def open_cabinet(event: Message | CallbackQuery, ctx: AppContext, _: Translator) -> None:
    settings = ctx.settings
    base = (settings.public_base_url or "").rstrip("/")
    if event.from_user is None:
        return
    if not (settings.cabinet_enabled and settings.cabinet_login_bot and base):
        await render(event, Screen(_("web_cabinet.unavailable"), kb([[to_menu(_)]])))
        return
    token = await cabinet.issue_token(
        ctx.session_factory,
        cabinet.LOGIN_PREFIX,
        {"user_id": event.from_user.id, "method": "bot"},
        cabinet.LOGIN_TTL,
    )
    minutes = int(cabinet.LOGIN_TTL.total_seconds() // 60)
    text = _("web_cabinet.link", minutes=_.plural(minutes, "plural.minutes"))
    markup = kb([[Button.url(_("web_cabinet.open"), f"{base}/cabinet/login/{token}")], [to_menu(_)]])
    await render(event, Screen(text, markup))
