"""Locations: the countries / cities users see in their apps ("🇩🇪 Германия"); order, rename, hide, delete."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from sqlalchemy import func, select, update

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, answer_callback, render
from app.db.models import Endpoint, Inbound, Location
from app.db.session import read_session
from app.keyboards import Button, kb
from app.modules.admin.common import admin_input_group, admin_router
from app.modules.admin_servers.callbacks import SrvCb
from app.services import audit

log = logging.getLogger(__name__)

router = admin_router("servers_locations")
router.message.filter(HasPermission(Permission.PANELS))
router.callback_query.filter(HasPermission(Permission.PANELS))

MAX_NAME = 64
MAX_FLAG = 16


@admin_input_group
class LocationStates(StatesGroup):
    new = State()
    rename = State()


def _cb(action: str, id_: int = 0, value: str = "") -> SrvCb:
    return SrvCb(a=action, id=id_, v=value)


def parse_location(text: str) -> tuple[str, str] | None:
    """``"🇩🇪 Германия"`` -> (flag, name); the flag is optional (any leading non-letter token)."""
    value = " ".join(text.split())
    if not value:
        return None
    first, _sep, rest = value.partition(" ")
    if rest and not any(ch.isalnum() for ch in first):
        flag, name = first, rest
    else:
        flag, name = "", value
    if not name or len(name) > MAX_NAME or len(flag) > MAX_FLAG:
        return None
    return flag, name


async def list_screen(ctx: AppContext, _: Translator, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session:
        locations = (await session.scalars(select(Location).order_by(Location.sort_order, Location.id))).all()
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(("" if loc.is_active else "⏸ ") + f"{loc.flag} {loc.name}".strip(), _cb("loc", loc.id))]
        for loc in locations
    ]
    rows.append([Button.cb(_("admin_servers.btn.add_location"), _cb("locnew"), style="primary")])
    rows.append([Button.cb(_("nav.back"), _cb("home"))])
    text = _("admin_servers.locations" if locations else "admin_servers.locations_empty")
    if note:
        text += "\n\n" + note
    return Screen(text, kb(rows))


async def location_screen(ctx: AppContext, _: Translator, location_id: int, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session:
        location = await session.get(Location, location_id)
        if location is None:
            return Screen(_("admin_servers.not_found"), kb([[Button.cb(_("nav.back"), _cb("locs"))]]))
        inbounds = int(
            await session.scalar(select(func.count(Inbound.id)).where(Inbound.location_id == location_id)) or 0
        )
        endpoints = int(
            await session.scalar(select(func.count(Endpoint.id)).where(Endpoint.location_id == location_id)) or 0
        )
    text = _(
        "admin_servers.location",
        title=_.esc(f"{location.flag} {location.name}".strip()),
        state=_("admin_servers.location_on" if location.is_active else "admin_servers.location_off"),
        inbounds=inbounds,
        endpoints=endpoints,
        order=location.sort_order,
    )
    if note:
        text += "\n\n" + note
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_servers.btn.rename"), _cb("locren", location.id))],
        [Button.cb("⬆️", _cb("locmv", location.id, "up")), Button.cb("⬇️", _cb("locmv", location.id, "down"))],
        [
            Button.cb(
                _("admin_servers.btn.hide" if location.is_active else "admin_servers.btn.show"),
                _cb("loct", location.id),
            )
        ],
        [Button.cb(_("admin_servers.btn.delete"), _cb("locdel", location.id), style="danger")],
        [Button.cb(_("nav.back"), _cb("locs"))],
    ]
    return Screen(text, kb(rows))


@router.callback_query(SrvCb.filter(F.a == "locs"))
async def open_list(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, await list_screen(ctx, _))


@router.callback_query(SrvCb.filter(F.a == "loc"))
async def open_location(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await location_screen(ctx, _, callback_data.id))


@router.callback_query(SrvCb.filter(F.a == "locnew"))
async def ask_new(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.set_state(LocationStates.new)
    await render(callback, Screen(_("admin_servers.ask.location"), kb([[Button.cb(_("nav.cancel"), _cb("locs"))]])))


@router.message(LocationStates.new, F.text)
async def got_new(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    parsed = parse_location(message.text or "")
    if parsed is None:
        await render(message, Screen(_("admin_servers.error.location", limit=MAX_NAME)))
        return
    flag, name = parsed
    async with ctx.session_factory.begin() as session:
        last = await session.scalar(select(func.max(Location.sort_order)))
        location = Location(name=name, flag=flag, sort_order=int(last or 0) + 1, is_active=True)
        session.add(location)
        await session.flush()
        await audit.log(session, message.chat.id, "location.create", "location", location.id, name=name)
        location_id = location.id
    await state.clear()
    await render(message, await location_screen(ctx, _, location_id, _("admin_servers.saved")))


@router.callback_query(SrvCb.filter(F.a == "locren"))
async def ask_rename(callback: CallbackQuery, callback_data: SrvCb, _: Translator, state: FSMContext) -> None:
    await state.set_state(LocationStates.rename)
    await state.update_data(lid=callback_data.id)
    await render(
        callback,
        Screen(_("admin_servers.ask.location"), kb([[Button.cb(_("nav.back"), _cb("loc", callback_data.id))]])),
    )


@router.message(LocationStates.rename, F.text)
async def got_rename(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    location_id = int((await state.get_data()).get("lid") or 0)
    parsed = parse_location(message.text or "")
    if parsed is None:
        await render(message, Screen(_("admin_servers.error.location", limit=MAX_NAME)))
        return
    async with ctx.session_factory.begin() as session:
        location = await session.get(Location, location_id, with_for_update=True)
        if location is not None:
            location.flag, location.name = parsed
            await audit.log(session, message.chat.id, "location.update", "location", location_id, name=location.name)
    await state.clear()
    await render(message, await location_screen(ctx, _, location_id, _("admin_servers.saved")))


@router.callback_query(SrvCb.filter(F.a == "locmv"))
async def move(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        ordered = list(
            (await session.scalars(select(Location).order_by(Location.sort_order, Location.id).with_for_update())).all()
        )
        index = next((i for i, loc in enumerate(ordered) if loc.id == callback_data.id), None)
        if index is None:
            await answer_callback(callback, _("admin_servers.not_found"), alert=True)
            return
        target = index - 1 if callback_data.v == "up" else index + 1
        if 0 <= target < len(ordered):
            ordered[index], ordered[target] = ordered[target], ordered[index]
            for position, loc in enumerate(ordered):
                loc.sort_order = position
            await audit.log(session, callback.from_user.id, "location.reorder", "location", callback_data.id)
    await render(callback, await location_screen(ctx, _, callback_data.id))


@router.callback_query(SrvCb.filter(F.a == "loct"))
async def toggle(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        location = await session.get(Location, callback_data.id, with_for_update=True)
        if location is None:
            await answer_callback(callback, _("admin_servers.not_found"), alert=True)
            return
        location.is_active = not location.is_active
        await audit.log(
            session, callback.from_user.id, "location.update", "location", location.id, is_active=location.is_active
        )
    await render(callback, await location_screen(ctx, _, callback_data.id, _("admin_servers.saved")))


@router.callback_query(SrvCb.filter(F.a == "locdel"))
async def ask_delete(callback: CallbackQuery, callback_data: SrvCb, _: Translator) -> None:
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_servers.btn.delete_yes"), _cb("locdely", callback_data.id), style="danger")],
        [Button.cb(_("nav.back"), _cb("loc", callback_data.id))],
    ]
    await render(callback, Screen(_("admin_servers.location_delete"), kb(rows)))


@router.callback_query(SrvCb.filter(F.a == "locdely"))
async def delete(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        location = await session.get(Location, callback_data.id, with_for_update=True)
        if location is not None:
            name = location.name
            for model in (Inbound, Endpoint):
                await session.execute(
                    update(model)
                    .where(model.location_id == location.id)
                    .values(location_id=None)
                    .execution_options(synchronize_session=False)
                )
            await session.delete(location)
            await audit.log(session, callback.from_user.id, "location.delete", "location", callback_data.id, name=name)
    await render(callback, await list_screen(ctx, _, _("admin_servers.deleted")))
