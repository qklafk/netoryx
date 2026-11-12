"""The bot admin menu (/admin), the web admin login link (/web) and leaving an abandoned input."""

from __future__ import annotations

import logging

from aiogram import F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.ui import Screen, render
from app.db.models import AdminRole
from app.keyboards import Btn, Button, Nav, grid, kb, to_menu
from app.modules.admin.common import ADMIN_NAV, InAdminInput, admin_router
from app.web.auth import LOGIN_TOKEN_TTL, create_login_link

log = logging.getLogger(__name__)

router = admin_router("admin")

WEB_NAV = "admin_web"


# --- leaving an input ------------------------------------------------------------------------------------------------


@router.callback_query(InAdminInput(), ~F.data.startswith("admi:"))
async def leave_input_on_button(callback: CallbackQuery, state: FSMContext) -> None:
    """Any other button abandons the input in progress; the button itself is then handled as usual."""
    await state.clear()
    raise SkipHandler


@router.message(InAdminInput(), F.text.startswith("/"))
async def leave_input_on_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    raise SkipHandler


# --- menu ------------------------------------------------------------------------------------------------------------


def menu_screen(ctx: AppContext, _: Translator, role: AdminRole) -> Screen:
    settings = ctx.settings
    buttons: list[Btn] = []
    for _module, item in ctx.modules.admin_menu(settings, role):
        if item.bot_callback:
            buttons.append(Button.cb(f"{item.icon} {_(item.title_key)}", item.bot_callback))
    rows: list[list[Btn]] = grid(buttons, 2)
    if settings.web_admin_enabled:
        rows.append([Button.cb(_("admin.web.button"), Nav(to=WEB_NAV))])
    rows.append([to_menu(_)])
    text = _(
        "admin.menu.title",
        brand=_.esc(settings.brand_name),
        role=_(f"admin.role.{role.value}"),
    )
    if not buttons:
        text += "\n\n" + _("admin.menu.empty")
    return Screen(text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("admin"))
async def admin_command(
    message: Message, ctx: AppContext, _: Translator, admin_role: AdminRole, state: FSMContext
) -> None:
    await state.clear()
    await render(message, menu_screen(ctx, _, admin_role))


@router.callback_query(Nav.filter(F.to == ADMIN_NAV))
async def admin_menu(callback: CallbackQuery, ctx: AppContext, _: Translator, admin_role: AdminRole) -> None:
    await render(callback, menu_screen(ctx, _, admin_role))


# --- web admin login link ------------------------------------------------------------------------------------------


async def web_login_screen(ctx: AppContext, _: Translator, admin_id: int) -> Screen:
    settings = ctx.settings
    if not settings.web_admin_enabled:
        return Screen(_("admin.web.disabled"), kb([[Button.cb(_("nav.back"), Nav(to=ADMIN_NAV))]]))
    link = await create_login_link(ctx.session_factory, settings, admin_id)
    minutes = int(LOGIN_TOKEN_TTL.total_seconds() // 60)
    ttl = _.plural(minutes, "plural.minutes")
    log.info("admin %s requested a web admin login link", admin_id)
    if link.startswith(("https://", "http://")):
        text = _("admin.web.link", ttl=ttl)
        markup = kb([[Button.url(_("admin.web.open"), link)], [Button.cb(_("nav.back"), Nav(to=ADMIN_NAV))]])
    else:
        text = _("admin.web.relative", ttl=ttl, path=_.esc(link))
        markup = kb([[Button.cb(_("nav.back"), Nav(to=ADMIN_NAV))]])
    return Screen(text, markup)


@router.message(Command("web"))
async def web_command(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    assert message.from_user is not None
    await render(message, await web_login_screen(ctx, _, message.from_user.id))


@router.callback_query(Nav.filter(F.to == WEB_NAV))
async def web_button(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await web_login_screen(ctx, _, callback.from_user.id))
