"""Admin screens: invitations list, the "new invitation" wizard, card with share link / QR, revoke, who joined."""

from __future__ import annotations

import logging

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, answer_callback, render
from app.db.models import Invite, User
from app.db.session import read_session
from app.keyboards import Nav
from app.modules.admin.common import admin_router
from app.modules.invites.callbacks import NAV, UNLIMITED, InvCb, InviteInput, InvNew
from app.modules.invites.views import (
    card_screen,
    invite_link,
    list_screen,
    note_screen,
    qr_screen,
    revoke_screen,
    users_screen,
    wizard_screen,
)
from app.services import invites
from app.services.qr import make_qr_png_async
from app.utils.time import utcnow

log = logging.getLogger(__name__)

PAGE_SIZE = 8
MAX_JOINED_SHOWN = 50

router = admin_router("invites_admin")
for _observer in (router.message, router.callback_query):
    _observer.filter(HasPermission(Permission.USERS_EDIT))


async def _list(ctx: AppContext, _: Translator, page: int) -> Screen:
    page = max(0, page)
    async with read_session(ctx.session_factory) as session:
        items = await invites.list_invites(session, limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE)
    return list_screen(_, items[:PAGE_SIZE], page, len(items) > PAGE_SIZE, utcnow())


async def _get(ctx: AppContext, invite_id: int) -> Invite | None:
    async with read_session(ctx.session_factory) as session:
        return await session.get(Invite, invite_id)


def _card(ctx: AppContext, _: Translator, invite: Invite, page: int) -> Screen:
    return card_screen(_, invite, invite_link(ctx.bot_username, invite), utcnow(), page)


@router.callback_query(Nav.filter(F.to == NAV))
async def open_list(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, await _list(ctx, _, 0))


@router.callback_query(InvCb.filter(F.action == "list"))
async def list_page(callback: CallbackQuery, callback_data: InvCb, ctx: AppContext, _: Translator) -> None:
    await render(callback, await _list(ctx, _, callback_data.page))


@router.callback_query(InvCb.filter(F.action.in_({"card", "revoke", "users", "qr"})))
async def invite_screens(callback: CallbackQuery, callback_data: InvCb, ctx: AppContext, _: Translator) -> None:
    invite = await _get(ctx, callback_data.id)
    page = callback_data.page
    if invite is None:
        await render(callback, await _list(ctx, _, page), answer=_("error.not_found"))
        return
    action = callback_data.action
    if action == "revoke":
        await render(callback, revoke_screen(_, invite, page))
    elif action == "users":
        async with read_session(ctx.session_factory) as session:
            ids = (await invites.redeemed_by(session, invite.id))[:MAX_JOINED_SHOWN]
            members = list(await session.scalars(select(User).where(User.tg_id.in_(ids)))) if ids else []
        await render(callback, users_screen(_, invite, members, page))
    elif action == "qr":
        png = await make_qr_png_async(invite_link(ctx.bot_username, invite))
        await render(callback, qr_screen(_, invite, png, page))
    else:
        await render(callback, _card(ctx, _, invite, page))


@router.callback_query(InvCb.filter(F.action == "revoke_ok"))
async def revoke(callback: CallbackQuery, callback_data: InvCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        done = await invites.revoke(session, callback_data.id, callback.from_user.id)
    invite = await _get(ctx, callback_data.id)
    if invite is None:
        await render(callback, await _list(ctx, _, callback_data.page))
        return
    await render(callback, _card(ctx, _, invite, callback_data.page), answer=_("invites.revoked") if done else None)


# --- wizard --------------------------------------------------------------------------------------------------------


@router.callback_query(InvNew.filter(F.step.in_({"days", "devices", "uses"})))
async def wizard_step(callback: CallbackQuery, callback_data: InvNew, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, wizard_screen(_, callback_data))


@router.callback_query(InvNew.filter(F.step == "note"))
async def wizard_note(callback: CallbackQuery, callback_data: InvNew, _: Translator, state: FSMContext) -> None:
    await state.set_state(InviteInput.note)
    await state.update_data(days=callback_data.days, devices=callback_data.devices, uses=callback_data.uses)
    await render(callback, note_screen(_, callback_data))


async def _create(ctx: AppContext, actor_id: int, days: int, devices: int, uses: int, note: str | None) -> Invite:
    async with ctx.session_factory.begin() as session:
        return await invites.create(
            session,
            actor_id,
            days=None if days == UNLIMITED else days,
            device_limit=0 if devices == UNLIMITED else devices,
            max_uses=0 if uses == UNLIMITED else uses,
            note=note,
        )


@router.callback_query(InvNew.filter(F.step == "create"))
async def wizard_create(
    callback: CallbackQuery, callback_data: InvNew, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    try:
        invite = await _create(
            ctx, callback.from_user.id, callback_data.days, callback_data.devices, callback_data.uses, None
        )
    except ValueError:
        await answer_callback(callback, _("error.invalid_input"), alert=True)
        return
    await render(callback, _card(ctx, _, invite, 0), answer=_("invites.created"))


@router.message(InviteInput.note, F.text)
async def wizard_note_text(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    note = (message.text or "").strip()[: invites.MAX_NOTE_LENGTH] or None
    try:
        actor = message.from_user.id if message.from_user else message.chat.id
        invite = await _create(ctx, actor, int(data["days"]), int(data["devices"]), int(data["uses"]), note)
    except (KeyError, ValueError):
        await render(message, await _list(ctx, _, 0))
        return
    await render(message, _card(ctx, _, invite, 0))
