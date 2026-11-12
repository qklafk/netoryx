"""Endpoints of an inbound: how users reach it (direct, L4 relay, CDN; regular or LTE), presets, live link preview,
probe, overrides."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from sqlalchemy import func, select

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, answer_callback, render
from app.db.models import Endpoint, EndpointKind, EndpointProfile, Inbound, Location
from app.db.session import read_session
from app.keyboards import Button, kb
from app.keyboards.builders import MAX_COPY_TEXT
from app.modules.admin.common import admin_input_group, admin_router
from app.modules.admin_servers import service
from app.modules.admin_servers.callbacks import SrvCb
from app.services import audit, links

log = logging.getLogger(__name__)

router = admin_router("servers_endpoints")
router.message.filter(HasPermission(Permission.PANELS))
router.callback_query.filter(HasPermission(Permission.PANELS))

MAX_REMARK = 64
MAX_OVERRIDES = 3000
KIND_ICONS = {EndpointKind.DIRECT: "🎯", EndpointKind.RELAY: "🔀", EndpointKind.CDN: "☁️"}


@admin_input_group
class EndpointStates(StatesGroup):
    sni = State()
    address = State()
    edit_address = State()
    remark = State()
    overrides = State()


def _cb(action: str, id_: int = 0, value: str = "") -> SrvCb:
    return SrvCb(a=action, id=id_, v=value)


def _label(endpoint: Endpoint) -> str:
    state = "⏸" if not endpoint.is_active else ("🙈" if endpoint.auto_hidden else "🟢")
    lte = " · LTE" if endpoint.profile == EndpointProfile.WHITELIST else ""
    port = f":{endpoint.port}" if endpoint.port else ""
    return f"{state} {KIND_ICONS.get(endpoint.kind, '')} {endpoint.address}{port}{lte}"[:60]


async def list_screen(ctx: AppContext, _: Translator, inbound_id: int, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session:
        inbound = await session.get(Inbound, inbound_id)
        endpoints = (
            await session.scalars(
                select(Endpoint).where(Endpoint.inbound_id == inbound_id).order_by(Endpoint.sort_order, Endpoint.id)
            )
        ).all()
    if inbound is None:
        return Screen(_("admin_servers.not_found"), kb([[Button.cb(_("nav.back"), _cb("home"))]]))
    rows: list[Sequence[InlineKeyboardButton | None]] = [[Button.cb(_label(e), _cb("ep", e.id))] for e in endpoints]
    rows.append([Button.cb(_("admin_servers.btn.add_endpoint"), _cb("epnew", inbound_id), style="primary")])
    rows.append([Button.cb(_("nav.back"), _cb("inb", inbound_id))])
    text = _("admin_servers.endpoints" if endpoints else "admin_servers.endpoints_empty", inbound=_.esc(inbound.name))
    if note:
        text += "\n\n" + note
    return Screen(text, kb(rows))


async def endpoint_screen(ctx: AppContext, _: Translator, endpoint_id: int, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session:
        endpoint = await session.get(Endpoint, endpoint_id)
        if endpoint is None:
            return Screen(_("admin_servers.not_found"), kb([[Button.cb(_("nav.back"), _cb("home"))]]))
        inbound = await session.get(Inbound, endpoint.inbound_id)
        location_id = endpoint.location_id or (inbound.location_id if inbound else None)
        location = await session.get(Location, location_id) if location_id else None
    link: str | None = None
    link_error: str | None = None
    if inbound is not None:
        try:
            link = service.preview_link(inbound, endpoint, location, ctx.settings)
        except ValueError as exc:
            link_error = str(exc)
    overrides = endpoint.overrides or "—"
    text = _(
        "admin_servers.endpoint",
        kind=_(f"admin_servers.kind.{endpoint.kind.value}"),
        profile=_(f"admin_servers.profile.{endpoint.profile.value}"),
        address=_.esc(endpoint.address),
        port=endpoint.port or _("admin_servers.inbound_port"),
        remark=_.esc(endpoint.remark or "—"),
        state=_("admin_servers.endpoint_hidden" if endpoint.auto_hidden else "admin_servers.inbound_on")
        if endpoint.is_active
        else _("admin_servers.inbound_off"),
        overrides=_.esc(overrides[:800]),
        error=_.esc(endpoint.last_error or "—"),
    )
    if link is not None:
        text += "\n\n" + _("admin_servers.link_preview", link=_.esc(link))
    else:
        text += "\n\n" + _("admin_servers.link_error", error=_.esc(link_error or "?"))
    if note:
        text += "\n\n" + note
    eid = endpoint.id
    profile_key = (
        "admin_servers.btn.make_lte"
        if endpoint.profile == EndpointProfile.STANDARD
        else "admin_servers.btn.make_regular"
    )
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.copy(_("admin_servers.btn.copy_link"), link) if link and len(link) <= MAX_COPY_TEXT else None],
        [Button.cb(_(profile_key), _cb("epprof", eid)), Button.cb(_("admin_servers.btn.probe"), _cb("epprobe", eid))],
        [
            Button.cb(_("admin_servers.btn.edit_address"), _cb("epaddr", eid)),
            Button.cb(_("admin_servers.btn.edit_remark"), _cb("eprem", eid)),
        ],
        [Button.cb(_("admin_servers.btn.overrides"), _cb("epovr", eid))],
        [
            Button.cb(
                _("admin_servers.btn.disable" if endpoint.is_active else "admin_servers.btn.enable"), _cb("eptog", eid)
            )
        ],
        [Button.cb(_("admin_servers.btn.delete"), _cb("epdel", eid), style="danger")],
        [Button.cb(_("nav.back"), _cb("eps", endpoint.inbound_id))],
    ]
    return Screen(text, kb(rows))


# --- list / create ----------------------------------------------------------------------------------------------------
@router.callback_query(SrvCb.filter(F.a == "eps"))
async def open_list(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await list_screen(ctx, _, callback_data.id))


@router.callback_query(SrvCb.filter(F.a == "ep"))
async def open_endpoint(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await endpoint_screen(ctx, _, callback_data.id))


@router.callback_query(SrvCb.filter(F.a == "epnew"))
async def choose_preset(callback: CallbackQuery, callback_data: SrvCb, _: Translator, state: FSMContext) -> None:
    await state.clear()
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_(f"admin_servers.preset.{key}"), _cb("eppre", callback_data.id, key))] for key in service.PRESETS
    ]
    rows.append([Button.cb(_("nav.back"), _cb("eps", callback_data.id))])
    await render(callback, Screen(_("admin_servers.presets"), kb(rows)))


@router.callback_query(SrvCb.filter(F.a == "eppre"))
async def preset_chosen(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    preset = service.PRESETS.get(callback_data.v)
    if preset is None:
        await answer_callback(callback, _("error.expired_button"))
        return
    async with read_session(ctx.session_factory) as session:
        inbound = await session.get(Inbound, callback_data.id)
    if inbound is None:
        await answer_callback(callback, _("admin_servers.not_found"), alert=True)
        return
    params = service.inbound_params(inbound)
    if preset.key == "cdn" and params.get("type") != "xhttp":
        await answer_callback(callback, _("admin_servers.preset_cdn_needs_xhttp"), alert=True)
        return
    if preset.key == "ru_sni" and params.get("security") != "reality":
        await answer_callback(callback, _("admin_servers.preset_needs_reality"), alert=True)
        return
    await state.update_data(iid=inbound.id, preset=preset.key)
    back_kb = kb([[Button.cb(_("nav.back"), _cb("epnew", inbound.id))]])
    if preset.needs_sni:
        await state.set_state(EndpointStates.sni)
        await render(callback, Screen(_("admin_servers.ask.endpoint_sni"), back_kb))
        return
    await state.set_state(EndpointStates.address)
    await render(callback, Screen(_(f"admin_servers.ask.endpoint_address.{preset.key}"), back_kb))


@router.message(EndpointStates.sni, F.text)
async def got_sni(message: Message, _: Translator, state: FSMContext) -> None:
    sni = (message.text or "").strip().lower()
    if not sni or " " in sni or "." not in sni or len(sni) > 253:
        await render(message, Screen(_("admin_servers.reality.bad_sni")))
        return
    await state.update_data(sni=sni)
    await state.set_state(EndpointStates.address)
    await render(message, Screen(_("admin_servers.ask.endpoint_address.ru_sni")))


@router.message(EndpointStates.address, F.text)
async def got_address(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    preset = service.PRESETS.get(str(data.get("preset") or ""))
    inbound_id = int(data.get("iid") or 0)
    parsed = service.parse_host_port(message.text or "")
    if preset is None or not inbound_id:
        await state.clear()
        await render(message, Screen(_("admin_servers.expired")))
        return
    if parsed is None:
        await render(message, Screen(_("admin_servers.error.address")))
        return
    host, port = parsed
    overrides = dict(preset.overrides)
    if preset.needs_sni and data.get("sni"):
        overrides["sni"] = str(data["sni"])
    endpoint_id: int | None = None
    async with ctx.session_factory.begin() as session:
        inbound = await session.get(Inbound, inbound_id)
        if inbound is not None:
            position = await session.scalar(
                select(func.max(Endpoint.sort_order)).where(Endpoint.inbound_id == inbound_id)
            )
            endpoint = Endpoint(
                inbound_id=inbound_id,
                location_id=inbound.location_id,
                kind=preset.kind,
                profile=preset.profile,
                address=host[:255],
                port=port,
                overrides=json.dumps(overrides, ensure_ascii=False) if overrides else None,
                sort_order=int(position or 0) + 1,
                is_active=True,
            )
            session.add(endpoint)
            await session.flush()
            await audit.log(
                session,
                message.chat.id,
                "endpoint.create",
                "endpoint",
                endpoint.id,
                inbound_id=inbound_id,
                preset=preset.key,
            )
            endpoint_id = endpoint.id
    await state.clear()
    if endpoint_id is None:
        await render(message, Screen(_("admin_servers.not_found")))
        return
    await render(message, await endpoint_screen(ctx, _, endpoint_id, _("admin_servers.saved")))


# --- edit -------------------------------------------------------------------------------------------------------------
async def _update(ctx: AppContext, endpoint_id: int, actor_id: int, **values: object) -> bool:
    async with ctx.session_factory.begin() as session:
        endpoint = await session.get(Endpoint, endpoint_id, with_for_update=True)
        if endpoint is None:
            return False
        for key, value in values.items():
            setattr(endpoint, key, value)
        await audit.log(session, actor_id, "endpoint.update", "endpoint", endpoint_id, fields=sorted(values))
    return True


@router.callback_query(SrvCb.filter(F.a == "epprof"))
async def toggle_profile(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with read_session(ctx.session_factory) as session:
        endpoint = await session.get(Endpoint, callback_data.id)
    if endpoint is None:
        await answer_callback(callback, _("admin_servers.not_found"), alert=True)
        return
    profile = EndpointProfile.STANDARD if endpoint.profile == EndpointProfile.WHITELIST else EndpointProfile.WHITELIST
    await _update(ctx, endpoint.id, callback.from_user.id, profile=profile)
    await render(callback, await endpoint_screen(ctx, _, endpoint.id, _("admin_servers.saved")))


@router.callback_query(SrvCb.filter(F.a == "eptog"))
async def toggle_active(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with read_session(ctx.session_factory) as session:
        endpoint = await session.get(Endpoint, callback_data.id)
    if endpoint is None:
        await answer_callback(callback, _("admin_servers.not_found"), alert=True)
        return
    await _update(ctx, endpoint.id, callback.from_user.id, is_active=not endpoint.is_active)
    await render(callback, await endpoint_screen(ctx, _, endpoint.id, _("admin_servers.saved")))


@router.callback_query(SrvCb.filter(F.a == "epprobe"))
async def probe(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with read_session(ctx.session_factory) as session:
        endpoint = await session.get(Endpoint, callback_data.id)
        inbound = await session.get(Inbound, endpoint.inbound_id) if endpoint else None
    if endpoint is None or inbound is None:
        await answer_callback(callback, _("admin_servers.not_found"), alert=True)
        return
    await answer_callback(callback, _("common.loading"))
    error = await service.probe_endpoint(inbound, endpoint)
    note = _("admin_servers.probe.ok") if error is None else _("admin_servers.probe.failed", error=_.esc(error))
    await render(callback, await endpoint_screen(ctx, _, endpoint.id, note))


_INPUTS = {
    "epaddr": (EndpointStates.edit_address, "admin_servers.ask.endpoint_address.edit"),
    "eprem": (EndpointStates.remark, "admin_servers.ask.endpoint_remark"),
    "epovr": (EndpointStates.overrides, "admin_servers.ask.endpoint_overrides"),
}


@router.callback_query(SrvCb.filter(F.a.in_(set(_INPUTS))))
async def ask_input(callback: CallbackQuery, callback_data: SrvCb, _: Translator, state: FSMContext) -> None:
    target_state, key = _INPUTS[callback_data.a]
    await state.set_state(target_state)
    await state.update_data(eid=callback_data.id)
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_servers.btn.clear"), _cb("epclr", callback_data.id, callback_data.a))]
        if callback_data.a in ("eprem", "epovr")
        else [],
        [Button.cb(_("nav.back"), _cb("ep", callback_data.id))],
    ]
    await render(callback, Screen(_(key), kb(rows)))


@router.callback_query(SrvCb.filter(F.a == "epclr"))
async def clear_field(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    field = "remark" if callback_data.v == "eprem" else "overrides"
    await _update(ctx, callback_data.id, callback.from_user.id, **{field: None})
    await render(callback, await endpoint_screen(ctx, _, callback_data.id, _("admin_servers.saved")))


@router.message(EndpointStates.edit_address, F.text)
async def got_new_address(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    endpoint_id = int((await state.get_data()).get("eid") or 0)
    parsed = service.parse_host_port(message.text or "")
    if parsed is None:
        await render(message, Screen(_("admin_servers.error.address")))
        return
    await state.clear()
    await _update(ctx, endpoint_id, message.chat.id, address=parsed[0][:255], port=parsed[1])
    await render(message, await endpoint_screen(ctx, _, endpoint_id, _("admin_servers.saved")))


@router.message(EndpointStates.remark, F.text)
async def got_remark(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    endpoint_id = int((await state.get_data()).get("eid") or 0)
    remark = " ".join((message.text or "").split())
    if not remark or len(remark) > MAX_REMARK:
        await render(message, Screen(_("admin_servers.error.name", limit=MAX_REMARK)))
        return
    await state.clear()
    await _update(ctx, endpoint_id, message.chat.id, remark=remark)
    await render(message, await endpoint_screen(ctx, _, endpoint_id, _("admin_servers.saved")))


@router.message(EndpointStates.overrides, F.text)
async def got_overrides(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    endpoint_id = int((await state.get_data()).get("eid") or 0)
    raw = (message.text or "").strip()
    try:
        if len(raw) > MAX_OVERRIDES:
            raise ValueError(f"longer than {MAX_OVERRIDES} characters")
        parsed = links.parse_overrides(raw)
    except ValueError as exc:
        await render(message, Screen(_("admin_servers.error.overrides", error=_.esc(exc))))
        return
    async with read_session(ctx.session_factory) as session:
        endpoint = await session.get(Endpoint, endpoint_id)
        inbound = await session.get(Inbound, endpoint.inbound_id) if endpoint else None
    if endpoint is None or inbound is None:
        await state.clear()
        await render(message, Screen(_("admin_servers.not_found")))
        return
    endpoint.overrides = json.dumps(parsed, ensure_ascii=False)
    try:
        links.effective_params(service.inbound_params(inbound), endpoint)
    except ValueError as exc:
        await render(message, Screen(_("admin_servers.error.overrides", error=_.esc(exc))))
        return
    await state.clear()
    await _update(ctx, endpoint_id, message.chat.id, overrides=endpoint.overrides if parsed else None)
    await render(message, await endpoint_screen(ctx, _, endpoint_id, _("admin_servers.saved")))


# --- delete -----------------------------------------------------------------------------------------------------------
@router.callback_query(SrvCb.filter(F.a == "epdel"))
async def ask_delete(callback: CallbackQuery, callback_data: SrvCb, _: Translator) -> None:
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_servers.btn.delete_yes"), _cb("epdely", callback_data.id), style="danger")],
        [Button.cb(_("nav.back"), _cb("ep", callback_data.id))],
    ]
    await render(callback, Screen(_("admin_servers.endpoint_delete"), kb(rows)))


@router.callback_query(SrvCb.filter(F.a == "epdely"))
async def delete(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        endpoint = await session.get(Endpoint, callback_data.id, with_for_update=True)
        if endpoint is None:
            await answer_callback(callback, _("admin_servers.not_found"), alert=True)
            return
        inbound_id = endpoint.inbound_id
        await session.delete(endpoint)
        await audit.log(
            session, callback.from_user.id, "endpoint.delete", "endpoint", callback_data.id, inbound_id=inbound_id
        )
    await render(callback, await list_screen(ctx, _, inbound_id, _("admin_servers.deleted")))
