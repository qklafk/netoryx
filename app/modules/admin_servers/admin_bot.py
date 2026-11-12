"""Servers: panels (3x-ui / Remnawave / telemt) - add with a live connection test, edit, enable/disable, delete with
or without removing our accounts, sync, inbounds, Reality inbound creation and a health overview.

Adding a panel and changing its URL, credentials or TLS checking is owner-only: those changes decide where the stored
credentials and every subscriber's account are sent."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from sqlalchemy import func, select

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission, has_permission
from app.core.ui import Screen, answer_callback, delete_message, render
from app.db.models import AdminRole, Endpoint, Inbound, Location, Panel, PanelKind
from app.db.session import read_session
from app.keyboards import Button, back, grid, kb
from app.modules.admin.common import admin_input_group, admin_router, parse_int
from app.modules.admin_servers import service
from app.modules.admin_servers.callbacks import SrvCb
from app.modules.admin_servers.service import PanelDraft
from app.services import audit
from app.services.panels import inventory
from app.services.panels.base import PanelError
from app.services.panels.registry import panel_options

log = logging.getLogger(__name__)

router = admin_router("servers_admin")
router.message.filter(HasPermission(Permission.PANELS))
router.callback_query.filter(HasPermission(Permission.PANELS))

KIND_TITLES = {
    PanelKind.XUI: "3x-ui",
    PanelKind.REMNAWAVE: "Remnawave",
    PanelKind.TELEMT: "telemt",
    PanelKind.MARZBAN: "Marzban",
    PanelKind.HIDDIFY: "Hiddify",
    PanelKind.PASARGUARD: "PasarGuard",
    PanelKind.MARZNESHIN: "Marzneshin",
}
MAX_NAME = 64
DEFAULT_REALITY_PORT = 443
# Actions that add a panel or change where the bot connects to it (and sends its credentials).
OWNER_ACTIONS = frozenset({"add", "kind", "save", "eurl", "ecreds", "etls"})
OWNER_PERMISSION = Permission.SETTINGS_SECRETS


@admin_input_group
class PanelStates(StatesGroup):
    name = State()
    url = State()
    creds = State()
    edit_name = State()
    edit_url = State()
    edit_creds = State()


@admin_input_group
class RealityStates(StatesGroup):
    port = State()
    sni = State()
    address = State()


# Registered first so these checks run before the handlers below.
@router.callback_query(SrvCb.filter(F.a.in_(OWNER_ACTIONS)), ~HasPermission(OWNER_PERMISSION))
async def owner_only(callback: CallbackQuery, _: Translator) -> None:
    await answer_callback(callback, _("admin_servers.owner_only"), alert=True)


@router.message(StateFilter(PanelStates.edit_url, PanelStates.edit_creds), ~HasPermission(OWNER_PERMISSION))
async def owner_only_input(message: Message, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await delete_message(message)  # it may hold credentials
    await render(message, Screen(_("admin_servers.owner_only")))


def _cb(action: str, id_: int = 0, value: str = "") -> SrvCb:
    return SrvCb(a=action, id=id_, v=value)


def _back(_: Translator, action: str, id_: int = 0) -> InlineKeyboardButton:
    return Button.cb(_("nav.back"), _cb(action, id_))


def status_icon(panel: Panel, threshold: int) -> str:
    if not panel.is_active:
        return "⏸"
    if (panel.consecutive_failures or 0) >= threshold:
        return "🔴"
    if panel.consecutive_failures:
        return "🟡"
    return "🟢" if panel.last_ok_at else "⚪️"


# --- screens ----------------------------------------------------------------------------------------------------------
async def home_screen(ctx: AppContext, _: Translator, role: AdminRole | None = None) -> Screen:
    threshold = max(1, ctx.settings.health_failures_before_alert)
    async with read_session(ctx.session_factory) as session:
        panels = (await session.scalars(select(Panel).order_by(Panel.id))).all()
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(f"{status_icon(p, threshold)} {p.name} · {KIND_TITLES.get(p.kind, p.kind)}", _cb("card", p.id))]
        for p in panels
    ]
    if has_permission(role, OWNER_PERMISSION):
        rows.append([Button.cb(_("admin_servers.btn.add"), _cb("add"), style="primary")])
    rows += [
        [
            Button.cb(_("admin_servers.btn.locations"), _cb("locs")),
            Button.cb(_("admin_servers.btn.health"), _cb("health")),
        ],
        [back("admin", _)],
    ]
    key = "admin_servers.home" if panels else "admin_servers.home_empty"
    return Screen(_(key, count=_.number(len(panels))), kb(rows))


async def panel_screen(ctx: AppContext, _: Translator, panel_id: int, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session:
        panel = await session.get(Panel, panel_id)
        if panel is None:
            return Screen(_("admin_servers.not_found"), kb([[_back(_, "home")]]))
        total, active = (
            await session.execute(
                select(func.count(Inbound.id), func.count(Inbound.id).filter(Inbound.is_active.is_(True))).where(
                    Inbound.panel_id == panel_id
                )
            )
        ).one()
        location_id = panel_options(panel).get("location_id")
        location = await session.get(Location, location_id) if isinstance(location_id, int) else None
    threshold = max(1, ctx.settings.health_failures_before_alert)
    if not panel.is_active:
        status = _("admin_servers.state.disabled")
    elif (panel.consecutive_failures or 0) >= threshold:
        status = _("admin_servers.state.down", error=_.esc(panel.last_error or "—"))
    elif panel.consecutive_failures:
        status = _("admin_servers.state.flaky", count=panel.consecutive_failures, error=_.esc(panel.last_error or "—"))
    elif panel.last_ok_at:
        status = _("admin_servers.state.ok", when=_.datetime(panel.last_ok_at))
    else:
        status = _("admin_servers.state.unknown")
    lines = [
        _(
            "admin_servers.card",
            name=_.esc(panel.name),
            kind=KIND_TITLES.get(panel.kind, panel.kind),
            url=_.esc(panel.url),
            version=_.esc(panel.version or "—"),
            status=status,
            tls=_("common.on") if panel.tls_verify else _("common.off"),
        )
    ]
    if panel.kind == PanelKind.XUI:
        lines.append(_("admin_servers.card_inbounds", active=active, total=total))
    if panel.kind != PanelKind.XUI:
        loc = f"{location.flag} {_.esc(location.name)}".strip() if location else _("admin_servers.no_location")
        lines.append(_("admin_servers.card_location", location=loc))
    if note:
        lines.append(note)
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [
            Button.cb(_("admin_servers.btn.ping"), _cb("ping", panel.id)),
            Button.cb(_("admin_servers.btn.sync"), _cb("sync", panel.id)),
        ],
    ]
    if panel.kind == PanelKind.XUI:
        rows.append(
            [
                Button.cb(_("admin_servers.btn.inbounds"), _cb("inbs", panel.id)),
                Button.cb(_("admin_servers.btn.refresh"), _cb("refr", panel.id)),
            ]
        )
        rows.append([Button.cb(_("admin_servers.btn.reality"), _cb("rnew", panel.id))])
    else:
        rows.append([Button.cb(_("admin_servers.btn.location"), _cb("ploc", panel.id))])
    rows.append(
        [
            Button.cb(_("admin_servers.btn.edit"), _cb("edit", panel.id)),
            Button.cb(
                _("admin_servers.btn.disable" if panel.is_active else "admin_servers.btn.enable"),
                _cb("toggle", panel.id),
            ),
        ]
    )
    rows.append([Button.cb(_("admin_servers.btn.delete"), _cb("del", panel.id), style="danger")])
    rows.append([_back(_, "home")])
    return Screen("\n\n".join(lines), kb(rows))


def _draft_from(data: dict[str, object]) -> PanelDraft | None:
    raw = data.get("draft")
    if not isinstance(raw, dict):
        return None
    try:
        return PanelDraft.from_dict(raw)
    except (KeyError, ValueError):
        return None


def test_screen(_: Translator, draft: PanelDraft, result: service.ConnectionResult) -> Screen:
    if result.ok:
        details = _("admin_servers.test.ok", version=_.esc(result.version or "?"))
        if result.inbounds is not None:
            details += "\n" + _("admin_servers.test.inbounds", count=result.inbounds)
    else:
        details = _("admin_servers.test.failed", error=_.esc(result.error or "?"))
    text = _(
        "admin_servers.test.title",
        name=_.esc(draft.name),
        kind=KIND_TITLES[draft.kind],
        url=_.esc(draft.url),
        result=details,
    )
    tls_label = _("admin_servers.btn.tls_on" if draft.tls_verify else "admin_servers.btn.tls_off")
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [
            Button.cb(
                _("admin_servers.btn.save" if result.ok else "admin_servers.btn.save_anyway"),
                _cb("save"),
                style="success" if result.ok else None,
            )
        ],
        [Button.cb(_("admin_servers.btn.retest"), _cb("retest")), Button.cb(tls_label, _cb("tls"))],
        [Button.cb(_("nav.cancel"), _cb("home"))],
    ]
    return Screen(text, kb(rows))


# --- list / card ------------------------------------------------------------------------------------------------------
@router.callback_query(SrvCb.filter(F.a == "home"))
async def open_home(
    callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext, admin_role: AdminRole | None = None
) -> None:
    await state.clear()
    await render(callback, await home_screen(ctx, _, admin_role))


@router.callback_query(SrvCb.filter(F.a == "card"))
async def open_card(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await panel_screen(ctx, _, callback_data.id))


@router.callback_query(SrvCb.filter(F.a == "ping"))
async def ping(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    await answer_callback(callback, _("common.loading"))
    result = await service.ping_panel(ctx, callback_data.id)
    if result.ok:
        note = _("admin_servers.ping.ok", version=_.esc(result.version or "?"))
    else:
        note = _("admin_servers.ping.failed", error=_.esc(result.error or "?"))
    await render(callback, await panel_screen(ctx, _, callback_data.id, note))


@router.callback_query(SrvCb.filter(F.a == "sync"))
async def sync(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    marked, started = await service.sync_now(ctx, callback_data.id, callback.from_user.id)
    key = "admin_servers.sync.started" if started else "admin_servers.sync.queued"
    note = _(key, count=marked, minutes=ctx.settings.reconcile_interval_min)
    await render(callback, await panel_screen(ctx, _, callback_data.id, note))


@router.callback_query(SrvCb.filter(F.a == "toggle"))
async def toggle(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        panel = await session.get(Panel, callback_data.id, with_for_update=True)
        if panel is None:
            await answer_callback(callback, _("admin_servers.not_found"), alert=True)
            return
        panel.is_active = not panel.is_active
        action = "panel.enable" if panel.is_active else "panel.disable"
        await audit.log(session, callback.from_user.id, action, "panel", panel.id, name=panel.name)
        enabled = panel.is_active
    service.kick_job(ctx, service.RECONCILE_JOB)
    note = _("admin_servers.enabled" if enabled else "admin_servers.disabled")
    await render(callback, await panel_screen(ctx, _, callback_data.id, note))


# --- add --------------------------------------------------------------------------------------------------------------
@router.callback_query(SrvCb.filter(F.a == "add"))
async def add_panel(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.clear()
    rows = [[Button.cb(title, _cb("kind", value=kind.value))] for kind, title in KIND_TITLES.items()]
    rows.append([_back(_, "home")])
    await render(callback, Screen(_("admin_servers.add.kind"), kb(rows)))


@router.callback_query(SrvCb.filter(F.a == "kind"))
async def add_kind(callback: CallbackQuery, callback_data: SrvCb, _: Translator, state: FSMContext) -> None:
    try:
        kind = PanelKind(callback_data.v)
    except ValueError:
        await answer_callback(callback, _("error.expired_button"))
        return
    await state.set_state(PanelStates.name)
    await state.update_data(draft=PanelDraft(kind=kind).to_dict())
    text = _("admin_servers.add.name", kind=KIND_TITLES[kind])
    await render(callback, Screen(text, kb([[Button.cb(_("nav.cancel"), _cb("home"))]])))


@router.message(PanelStates.name, F.text)
async def add_name(message: Message, _: Translator, state: FSMContext) -> None:
    draft = _draft_from(await state.get_data())
    name = (message.text or "").strip()
    if draft is None:
        await state.clear()
        await render(message, Screen(_("admin_servers.expired")))
        return
    if not name or len(name) > MAX_NAME:
        await render(message, Screen(_("admin_servers.error.name", limit=MAX_NAME)))
        return
    draft.name = name
    await state.update_data(draft=draft.to_dict())
    await state.set_state(PanelStates.url)
    await render(
        message, Screen(_(f"admin_servers.add.url.{draft.kind.value}"), kb([[Button.cb(_("nav.cancel"), _cb("home"))]]))
    )


@router.message(PanelStates.url, F.text)
async def add_url(message: Message, _: Translator, state: FSMContext) -> None:
    draft = _draft_from(await state.get_data())
    url = service.normalize_url(message.text or "")
    if draft is None:
        await state.clear()
        await render(message, Screen(_("admin_servers.expired")))
        return
    if url is None:
        await render(message, Screen(_("admin_servers.error.url")))
        return
    draft.url = url
    await state.update_data(draft=draft.to_dict())
    await state.set_state(PanelStates.creds)
    await render(
        message,
        Screen(_(f"admin_servers.add.creds.{draft.kind.value}"), kb([[Button.cb(_("nav.cancel"), _cb("home"))]])),
    )


def _apply_creds(ctx: AppContext, draft: PanelDraft, text: str) -> bool:
    """Credentials from the admin's message; False if they do not fit the panel kind."""
    parts = text.split()
    draft.username = draft.password = draft.api_token = None
    if draft.kind in (PanelKind.XUI, PanelKind.MARZBAN, PanelKind.MARZNESHIN):
        if len(parts) == 2:
            draft.username = parts[0][:256]
            draft.password = ctx.secret_box.encrypt(parts[1])
            return True
        if len(parts) == 1:
            draft.api_token = ctx.secret_box.encrypt(parts[0])
            return True
        return False
    if draft.kind == PanelKind.TELEMT and text.strip() == "-":
        return True
    value = text.strip()
    single_word = draft.kind in (
        PanelKind.REMNAWAVE,
        PanelKind.MARZBAN,
        PanelKind.HIDDIFY,
        PanelKind.PASARGUARD,
    )  # API tokens have no spaces
    if not value or "\n" in value or (single_word and len(parts) != 1):
        return False
    draft.api_token = ctx.secret_box.encrypt(value)
    return True


@router.message(PanelStates.creds, F.text)
async def add_creds(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    draft = _draft_from(await state.get_data())
    if draft is None:
        await state.clear()
        await render(message, Screen(_("admin_servers.expired")))
        return
    ok = _apply_creds(ctx, draft, message.text or "")
    await delete_message(message)  # credentials must not stay in the chat history
    if not ok:
        await render(message, Screen(_(f"admin_servers.error.creds.{draft.kind.value}")))
        return
    await state.update_data(draft=draft.to_dict())
    await state.set_state(None)
    progress = await message.answer(_("admin_servers.test.running"))
    result = await service.test_connection(ctx, draft)
    await state.update_data(version=result.version)
    await delete_message(progress)
    await render(message, test_screen(_, draft, result))


@router.callback_query(SrvCb.filter(F.a.in_({"retest", "tls"})))
async def retest(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    draft = _draft_from(await state.get_data())
    if draft is None:
        await render(callback, Screen(_("admin_servers.expired"), kb([[_back(_, "home")]])))
        return
    if callback_data.a == "tls":
        draft.tls_verify = not draft.tls_verify
        await state.update_data(draft=draft.to_dict())
    await answer_callback(callback, _("common.loading"))
    result = await service.test_connection(ctx, draft)
    await state.update_data(version=result.version)
    await render(callback, test_screen(_, draft, result))


@router.callback_query(SrvCb.filter(F.a == "save"), HasPermission(OWNER_PERMISSION))
async def save_panel(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    draft = _draft_from(data)
    if draft is None or not draft.url:
        await render(callback, Screen(_("admin_servers.expired"), kb([[_back(_, "home")]])))
        return
    await state.clear()
    version = data.get("version")
    panel_id = await service.create_panel(ctx, draft, callback.from_user.id, str(version) if version else None)
    note = _("admin_servers.saved")
    if draft.kind == PanelKind.XUI:
        try:
            result = await service.refresh(ctx, panel_id)
            note = _("admin_servers.saved_inbounds", count=len(result.created))
        except (PanelError, LookupError) as exc:
            note = _("admin_servers.refresh.failed", error=_.esc(service.short_error(exc)))
    await render(callback, await panel_screen(ctx, _, panel_id, note))


# --- edit -------------------------------------------------------------------------------------------------------------
@router.callback_query(SrvCb.filter(F.a == "edit"))
async def edit_menu(
    callback: CallbackQuery,
    callback_data: SrvCb,
    _: Translator,
    state: FSMContext,
    admin_role: AdminRole | None = None,
) -> None:
    await state.clear()
    pid = callback_data.id
    first: list[InlineKeyboardButton | None] = [Button.cb(_("admin_servers.btn.edit_name"), _cb("ename", pid))]
    rows: list[Sequence[InlineKeyboardButton | None]] = [first]
    if has_permission(admin_role, OWNER_PERMISSION):
        first.append(Button.cb(_("admin_servers.btn.edit_url"), _cb("eurl", pid)))
        rows.append(
            [
                Button.cb(_("admin_servers.btn.edit_creds"), _cb("ecreds", pid)),
                Button.cb(_("admin_servers.btn.edit_tls"), _cb("etls", pid)),
            ]
        )
    rows.append([_back(_, "card", pid)])
    await render(callback, Screen(_("admin_servers.edit"), kb(rows)))


_EDIT_STATES = {"ename": PanelStates.edit_name, "eurl": PanelStates.edit_url, "ecreds": PanelStates.edit_creds}


@router.callback_query(SrvCb.filter(F.a.in_(set(_EDIT_STATES))))
async def ask_edit(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    async with read_session(ctx.session_factory) as session:
        panel = await session.get(Panel, callback_data.id)
    if panel is None:
        await answer_callback(callback, _("admin_servers.not_found"), alert=True)
        return
    await state.set_state(_EDIT_STATES[callback_data.a])
    await state.update_data(pid=panel.id, kind=panel.kind.value)
    key = {
        "ename": "admin_servers.ask.name",
        "eurl": f"admin_servers.add.url.{panel.kind.value}",
        "ecreds": f"admin_servers.add.creds.{panel.kind.value}",
    }[callback_data.a]
    await render(callback, Screen(_(key, kind=KIND_TITLES[panel.kind]), kb([[_back(_, "edit", panel.id)]])))


async def _update_panel(ctx: AppContext, panel_id: int, actor_id: int, **values: object) -> bool:
    async with ctx.session_factory.begin() as session:
        panel = await session.get(Panel, panel_id, with_for_update=True)
        if panel is None:
            return False
        for key, value in values.items():
            setattr(panel, key, value)
        await audit.log(session, actor_id, "panel.update", "panel", panel_id, fields=sorted(values))
    return True


@router.message(PanelStates.edit_name, F.text)
async def edit_name(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    panel_id = int((await state.get_data()).get("pid") or 0)
    name = (message.text or "").strip()
    if not name or len(name) > MAX_NAME:
        await render(message, Screen(_("admin_servers.error.name", limit=MAX_NAME)))
        return
    await state.clear()
    await _update_panel(ctx, panel_id, message.chat.id, name=name)
    await render(message, await panel_screen(ctx, _, panel_id, _("admin_servers.saved")))


@router.message(PanelStates.edit_url, F.text, HasPermission(OWNER_PERMISSION))
async def edit_url(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    panel_id = int((await state.get_data()).get("pid") or 0)
    url = service.normalize_url(message.text or "")
    if url is None:
        await render(message, Screen(_("admin_servers.error.url")))
        return
    await state.clear()
    await _update_panel(ctx, panel_id, message.chat.id, url=url)
    await _after_connection_change(message, ctx, _, panel_id)


@router.message(PanelStates.edit_creds, F.text, HasPermission(OWNER_PERMISSION))
async def edit_creds(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    panel_id = int(data.get("pid") or 0)
    draft = PanelDraft(kind=PanelKind(str(data.get("kind") or PanelKind.XUI.value)))
    ok = _apply_creds(ctx, draft, message.text or "")
    await delete_message(message)
    if not ok:
        await render(message, Screen(_(f"admin_servers.error.creds.{draft.kind.value}")))
        return
    await state.clear()
    await _update_panel(
        ctx, panel_id, message.chat.id, username=draft.username, password=draft.password, api_token=draft.api_token
    )
    await _after_connection_change(message, ctx, _, panel_id)


@router.callback_query(SrvCb.filter(F.a == "etls"), HasPermission(OWNER_PERMISSION))
async def edit_tls(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with read_session(ctx.session_factory) as session:
        panel = await session.get(Panel, callback_data.id)
    if panel is None:
        await answer_callback(callback, _("admin_servers.not_found"), alert=True)
        return
    await _update_panel(ctx, panel.id, callback.from_user.id, tls_verify=not panel.tls_verify)
    await _after_connection_change(callback, ctx, _, panel.id)


async def _after_connection_change(
    event: Message | CallbackQuery, ctx: AppContext, _: Translator, panel_id: int
) -> None:
    result = await service.ping_panel(ctx, panel_id)
    if result.ok:
        note = _("admin_servers.ping.ok", version=_.esc(result.version or "?"))
    else:
        note = _("admin_servers.ping.failed", error=_.esc(result.error or "?"))
    await render(event, await panel_screen(ctx, _, panel_id, _("admin_servers.saved") + "\n" + note))


# --- delete -----------------------------------------------------------------------------------------------------------
@router.callback_query(SrvCb.filter(F.a == "del"))
async def ask_delete(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with read_session(ctx.session_factory) as session:
        panel = await session.get(Panel, callback_data.id)
    if panel is None:
        await answer_callback(callback, _("admin_servers.not_found"), alert=True)
        return
    count = await service.count_accounts(ctx, panel.id)
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_servers.btn.delete_cleanup", count=count), _cb("delc", panel.id, "1"), style="danger")],
        [Button.cb(_("admin_servers.btn.delete_keep"), _cb("delc", panel.id, "0"))],
        [_back(_, "card", panel.id)],
    ]
    await render(callback, Screen(_("admin_servers.delete.ask", name=_.esc(panel.name), count=count), kb(rows)))


@router.callback_query(SrvCb.filter(F.a == "delc"))
async def confirm_delete(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    key = "admin_servers.delete.confirm_cleanup" if callback_data.v == "1" else "admin_servers.delete.confirm_keep"
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_servers.btn.delete_yes"), _cb("dely", callback_data.id, callback_data.v), style="danger")],
        [_back(_, "card", callback_data.id)],
    ]
    await render(callback, Screen(_(key), kb(rows)))


@router.callback_query(SrvCb.filter(F.a == "dely"))
async def delete(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, admin_role: AdminRole | None = None
) -> None:
    await answer_callback(callback, _("common.loading"))
    cleanup = callback_data.v == "1"
    report = await service.delete_panel(ctx, callback_data.id, callback.from_user.id, cleanup=cleanup)
    if cleanup:
        note = _("admin_servers.delete.done_cleanup", removed=report.removed, failed=report.failed)
        if report.errors:
            note += "\n<code>" + _.esc("; ".join(report.errors)) + "</code>"
    else:
        note = _("admin_servers.delete.done")
    screen = await home_screen(ctx, _, admin_role)
    await render(callback, Screen(note + "\n\n" + screen.text, screen.kb))


# --- location of a Remnawave / telemt panel ---------------------------------------------------------------------------
@router.callback_query(SrvCb.filter(F.a == "ploc"))
async def pick_panel_location(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with read_session(ctx.session_factory) as session:
        locations = (await session.scalars(select(Location).order_by(Location.sort_order, Location.id))).all()
    buttons = [
        Button.cb(f"{loc.flag} {loc.name}".strip(), _cb("plocs", callback_data.id, str(loc.id))) for loc in locations
    ]
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        *grid(buttons, 2),
        [Button.cb(_("admin_servers.no_location"), _cb("plocs", callback_data.id, "0"))],
        [_back(_, "card", callback_data.id)],
    ]
    await render(callback, Screen(_("admin_servers.pick_location"), kb(rows)))


@router.callback_query(SrvCb.filter(F.a == "plocs"))
async def set_panel_location(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    location_id = int(callback_data.v) if callback_data.v.isdigit() else 0
    async with ctx.session_factory.begin() as session:
        panel = await session.get(Panel, callback_data.id, with_for_update=True)
        if panel is None:
            await answer_callback(callback, _("admin_servers.not_found"), alert=True)
            return
        options = panel_options(panel)
        if location_id:
            options["location_id"] = location_id
        else:
            options.pop("location_id", None)
        panel.options = json.dumps(options, ensure_ascii=False) if options else None
        await audit.log(session, callback.from_user.id, "panel.update", "panel", panel.id, fields=["location_id"])
    await render(callback, await panel_screen(ctx, _, callback_data.id, _("admin_servers.saved")))


# --- inbounds (3x-ui) -------------------------------------------------------------------------------------------------
@router.callback_query(SrvCb.filter(F.a == "refr"))
async def refresh_inbounds(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    await answer_callback(callback, _("common.loading"))
    try:
        result = await service.refresh(ctx, callback_data.id)
    except (PanelError, LookupError) as exc:
        note = _("admin_servers.refresh.failed", error=_.esc(service.short_error(exc)))
    else:
        note = _(
            "admin_servers.refresh.done",
            created=len(result.created),
            updated=len(result.updated),
            deactivated=len(result.deactivated),
        )
    await render(callback, await panel_screen(ctx, _, callback_data.id, note))


async def inbounds_screen(ctx: AppContext, _: Translator, panel_id: int) -> Screen:
    async with read_session(ctx.session_factory) as session:
        rows_db = (
            await session.execute(
                select(Inbound, Location)
                .outerjoin(Location, Location.id == Inbound.location_id)
                .where(Inbound.panel_id == panel_id)
                .order_by(Inbound.sort_order, Inbound.id)
            )
        ).all()
    rows: list[Sequence[InlineKeyboardButton | None]] = []
    for inbound, location in rows_db:
        if inventory.is_proxy_inbound(inbound):
            continue
        icon = "🟢" if inbound.is_active else "⚪️"
        port = inventory.inbound_port(inbound)
        flag = location.flag if location else ""
        label = f"{icon} {inbound.name}" + (f" :{port}" if port else "") + (f" {flag}" if flag else "")
        rows.append([Button.cb(label[:60], _cb("inb", inbound.id))])
    rows.append(
        [
            Button.cb(_("admin_servers.btn.refresh"), _cb("refr", panel_id)),
            Button.cb(_("admin_servers.btn.reality"), _cb("rnew", panel_id)),
        ]
    )
    rows.append([_back(_, "card", panel_id)])
    key = "admin_servers.inbounds" if len(rows) > 2 else "admin_servers.inbounds_empty"
    return Screen(_(key), kb(rows))


async def inbound_screen(ctx: AppContext, _: Translator, inbound_id: int, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session:
        inbound = await session.get(Inbound, inbound_id)
        if inbound is None:
            return Screen(_("admin_servers.not_found"), kb([[_back(_, "home")]]))
        location = await session.get(Location, inbound.location_id) if inbound.location_id else None
        endpoints = int(
            await session.scalar(select(func.count(Endpoint.id)).where(Endpoint.inbound_id == inbound.id)) or 0
        )
    params = service.inbound_params(inbound)
    text = _(
        "admin_servers.inbound",
        name=_.esc(inbound.name),
        remote_id=inbound.remote_id,
        port=params.get("port", "—"),
        transport=_.esc(f"{params.get('type', '?')} / {params.get('security', 'none')}"),
        flow=_.esc(inbound.flow or "—"),
        location=f"{location.flag} {_.esc(location.name)}".strip() if location else _("admin_servers.no_location"),
        state=_("admin_servers.inbound_on" if inbound.is_active else "admin_servers.inbound_off"),
        endpoints=endpoints,
    )
    if inbound.is_active and endpoints == 0:
        text += "\n\n" + _("admin_servers.inbound_no_endpoints")
    if note:
        text += "\n\n" + note
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [
            Button.cb(
                _("admin_servers.btn.inbound_off" if inbound.is_active else "admin_servers.btn.inbound_on"),
                _cb("inbt", inbound.id),
            )
        ],
        [Button.cb(_("admin_servers.btn.endpoints", count=endpoints), _cb("eps", inbound.id))],
        [Button.cb(_("admin_servers.btn.location"), _cb("inbl", inbound.id))],
        [_back(_, "inbs", inbound.panel_id)],
    ]
    return Screen(text, kb(rows))


@router.callback_query(SrvCb.filter(F.a == "inbs"))
async def open_inbounds(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await inbounds_screen(ctx, _, callback_data.id))


@router.callback_query(SrvCb.filter(F.a == "inb"))
async def open_inbound(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await inbound_screen(ctx, _, callback_data.id))


@router.callback_query(SrvCb.filter(F.a == "inbt"))
async def toggle_inbound(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        inbound = await session.get(Inbound, callback_data.id, with_for_update=True)
        if inbound is None or inventory.is_proxy_inbound(inbound):
            await answer_callback(callback, _("admin_servers.not_found"), alert=True)
            return
        inbound.is_active = not inbound.is_active
        await audit.log(
            session, callback.from_user.id, "inbound.update", "inbound", inbound.id, is_active=inbound.is_active
        )
    service.kick_job(ctx, service.RECONCILE_JOB)
    await render(callback, await inbound_screen(ctx, _, callback_data.id, _("admin_servers.saved")))


@router.callback_query(SrvCb.filter(F.a == "inbl"))
async def pick_inbound_location(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    async with read_session(ctx.session_factory) as session:
        locations = (await session.scalars(select(Location).order_by(Location.sort_order, Location.id))).all()
    buttons = [
        Button.cb(f"{loc.flag} {loc.name}".strip(), _cb("inbls", callback_data.id, str(loc.id))) for loc in locations
    ]
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        *grid(buttons, 2),
        [Button.cb(_("admin_servers.no_location"), _cb("inbls", callback_data.id, "0"))],
        [_back(_, "inb", callback_data.id)],
    ]
    await render(callback, Screen(_("admin_servers.pick_location"), kb(rows)))


@router.callback_query(SrvCb.filter(F.a == "inbls"))
async def set_inbound_location(callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator) -> None:
    location_id = int(callback_data.v) if callback_data.v.isdigit() and callback_data.v != "0" else None
    async with ctx.session_factory.begin() as session:
        inbound = await session.get(Inbound, callback_data.id, with_for_update=True)
        if inbound is None:
            await answer_callback(callback, _("admin_servers.not_found"), alert=True)
            return
        inbound.location_id = location_id
        await audit.log(
            session, callback.from_user.id, "inbound.update", "inbound", inbound.id, location_id=location_id
        )
    await render(callback, await inbound_screen(ctx, _, callback_data.id, _("admin_servers.saved")))


# --- Reality inbound wizard -------------------------------------------------------------------------------------------
@router.callback_query(SrvCb.filter(F.a == "rnew"))
async def reality_start(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await state.set_state(RealityStates.port)
    await state.update_data(pid=callback_data.id)
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(str(DEFAULT_REALITY_PORT), _cb("rport", callback_data.id, str(DEFAULT_REALITY_PORT)))],
        [_back(_, "card", callback_data.id)],
    ]
    await render(callback, Screen(_("admin_servers.reality.port"), kb(rows)))


async def _accept_port(
    event: Message | CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext, port: int
) -> None:
    panel_id = int((await state.get_data()).get("pid") or 0)
    try:
        taken = await service.used_ports(ctx, panel_id)
    except (PanelError, LookupError) as exc:
        await render(
            event,
            Screen(
                _("admin_servers.refresh.failed", error=_.esc(service.short_error(exc))),
                kb([[_back(_, "card", panel_id)]]),
            ),
        )
        return
    if port in taken:
        await render(
            event, Screen(_("admin_servers.reality.port_taken", port=port), kb([[_back(_, "card", panel_id)]]))
        )
        return
    await state.update_data(port=port)
    await state.set_state(None)
    await render(event, await sni_screen(_, panel_id, "global"))


@router.callback_query(SrvCb.filter(F.a == "rport"))
async def reality_port_button(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await answer_callback(callback, _("common.loading"))
    await state.update_data(pid=callback_data.id)
    await _accept_port(callback, ctx, _, state, int(callback_data.v))


@router.message(RealityStates.port, F.text)
async def reality_port_input(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    port = parse_int(message.text, minimum=1, maximum=65535)
    if port is None:
        await render(message, Screen(_("admin_servers.reality.bad_port")))
        return
    await _accept_port(message, ctx, _, state, port)


async def sni_screen(_: Translator, panel_id: int, group: str) -> Screen:
    hosts = service.SNI_RU if group == "ru" else service.SNI_GLOBAL
    results = await service.check_snis(hosts)
    rows: list[Sequence[InlineKeyboardButton | None]] = []
    for result in results:
        if result.ok:
            label = f"✅ {result.host} · {result.latency_ms} ms"
            rows.append([Button.cb(label, _cb("rsni", panel_id, result.host))])
        else:
            label = f"❌ {result.host} · " + _(f"admin_servers.reality.problem.{result.problem or 'unreachable'}")
            rows.append([Button.cb(label, _cb("rsni", panel_id, result.host))])
    other = "global" if group == "ru" else "ru"
    rows.append([Button.cb(_(f"admin_servers.reality.group.{other}"), _cb("rsnil", panel_id, other))])
    rows.append([Button.cb(_("admin_servers.reality.custom_sni"), _cb("rsnic", panel_id))])
    rows.append([_back(_, "card", panel_id)])
    return Screen(_("admin_servers.reality.sni"), kb(rows))


@router.callback_query(SrvCb.filter(F.a == "rsnil"))
async def reality_sni_list(callback: CallbackQuery, callback_data: SrvCb, _: Translator) -> None:
    await answer_callback(callback, _("admin_servers.reality.checking"))
    await render(callback, await sni_screen(_, callback_data.id, callback_data.v))


@router.callback_query(SrvCb.filter(F.a == "rsnic"))
async def reality_sni_custom(callback: CallbackQuery, callback_data: SrvCb, _: Translator, state: FSMContext) -> None:
    await state.set_state(RealityStates.sni)
    await render(callback, Screen(_("admin_servers.reality.ask_sni"), kb([[_back(_, "card", callback_data.id)]])))


@router.message(RealityStates.sni, F.text)
async def reality_sni_input(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    host = (message.text or "").strip().lower().removeprefix("https://").split("/")[0]
    if not host or " " in host or "." not in host:
        await render(message, Screen(_("admin_servers.reality.bad_sni")))
        return
    check = await service.check_sni(host)
    if not check.ok:
        problem = _(f"admin_servers.reality.problem.{check.problem or 'unreachable'}")
        await render(message, Screen(_("admin_servers.reality.sni_unfit", host=_.esc(host), problem=problem)))
        return
    await state.set_state(None)
    await _choose_location(message, ctx, _, state, host)


@router.callback_query(SrvCb.filter(F.a == "rsni"))
async def reality_sni_chosen(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await _choose_location(callback, ctx, _, state, callback_data.v)


async def _choose_location(
    event: Message | CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext, sni: str
) -> None:
    data = await state.get_data()
    panel_id = int(data.get("pid") or 0)
    if not data.get("port"):
        await render(event, Screen(_("admin_servers.expired"), kb([[_back(_, "home")]])))
        return
    await state.update_data(sni=sni)
    async with read_session(ctx.session_factory) as session:
        locations = (await session.scalars(select(Location).order_by(Location.sort_order, Location.id))).all()
    buttons = [Button.cb(f"{loc.flag} {loc.name}".strip(), _cb("rloc", panel_id, str(loc.id))) for loc in locations]
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        *grid(buttons, 2),
        [Button.cb(_("admin_servers.no_location"), _cb("rloc", panel_id, "0"))],
        [_back(_, "card", panel_id)],
    ]
    await render(event, Screen(_("admin_servers.reality.location", sni=_.esc(sni)), kb(rows)))


@router.callback_query(SrvCb.filter(F.a == "rloc"))
async def reality_location(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    if not data.get("port") or not data.get("sni"):
        await render(callback, Screen(_("admin_servers.expired"), kb([[_back(_, "home")]])))
        return
    address = await service.suggest_endpoint_address(ctx, callback_data.id)
    await state.update_data(loc=int(callback_data.v) if callback_data.v.isdigit() else 0, addr=address)
    await render(callback, await reality_confirm_screen(_, state, callback_data.id))


async def reality_confirm_screen(_: Translator, state: FSMContext, panel_id: int) -> Screen:
    data = await state.get_data()
    if not data.get("port") or not data.get("sni"):
        return Screen(_("admin_servers.expired"), kb([[_back(_, "home")]]))
    text = _(
        "admin_servers.reality.confirm",
        port=data["port"],
        sni=_.esc(str(data["sni"])),
        address=_.esc(str(data.get("addr") or "—")),
    )
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_servers.btn.create"), _cb("rgo", panel_id), style="success")],
        [Button.cb(_("admin_servers.btn.reality_address"), _cb("raddr", panel_id))],
        [_back(_, "card", panel_id)],
    ]
    return Screen(text, kb(rows))


@router.callback_query(SrvCb.filter(F.a == "raddr"))
async def reality_ask_address(callback: CallbackQuery, callback_data: SrvCb, _: Translator, state: FSMContext) -> None:
    await state.set_state(RealityStates.address)
    await state.update_data(pid=callback_data.id)
    await render(callback, Screen(_("admin_servers.reality.ask_address"), kb([[_back(_, "card", callback_data.id)]])))


@router.message(RealityStates.address, F.text)
async def reality_address_input(message: Message, _: Translator, state: FSMContext) -> None:
    parsed = service.parse_host_port(message.text or "")
    if parsed is None or parsed[1] is not None or ("." not in parsed[0] and ":" not in parsed[0]):
        await render(message, Screen(_("admin_servers.reality.bad_address")))
        return
    await state.set_state(None)
    await state.update_data(addr=parsed[0].lower())
    await render(message, await reality_confirm_screen(_, state, int((await state.get_data()).get("pid") or 0)))


@router.callback_query(SrvCb.filter(F.a == "rgo"))
async def reality_create(
    callback: CallbackQuery, callback_data: SrvCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    port, sni = data.get("port"), data.get("sni")
    if not port or not sni:
        await render(callback, Screen(_("admin_servers.expired"), kb([[_back(_, "home")]])))
        return
    await state.clear()
    await answer_callback(callback, _("common.loading"))
    location_id = int(data.get("loc") or 0) or None
    address = str(data.get("addr") or "") or None
    remark = f"{ctx.settings.brand_name} Reality {port}"[:64]
    try:
        result = await service.create_reality(
            ctx,
            callback_data.id,
            callback.from_user.id,
            port=int(port),
            sni=str(sni),
            remark=remark,
            location_id=location_id,
            endpoint_address=address,
        )
    except (PanelError, LookupError) as exc:
        note = _("admin_servers.reality.failed", error=_.esc(service.short_error(exc)))
        await render(callback, await panel_screen(ctx, _, callback_data.id, note))
        return
    service.kick_job(ctx, service.RECONCILE_JOB)
    if result.created:
        await render(callback, await inbound_screen(ctx, _, result.created[0], _("admin_servers.reality.created")))
    else:
        await render(callback, await panel_screen(ctx, _, callback_data.id, _("admin_servers.reality.created")))


# --- health -----------------------------------------------------------------------------------------------------------
async def health_screen(ctx: AppContext, _: Translator, note: str | None = None) -> Screen:
    threshold = max(1, ctx.settings.health_failures_before_alert)
    async with read_session(ctx.session_factory) as session:
        panels = (await session.scalars(select(Panel).order_by(Panel.id))).all()
        hidden = (
            await session.scalars(
                select(Endpoint).where(Endpoint.auto_hidden.is_(True), Endpoint.is_active.is_(True)).limit(20)
            )
        ).all()
    lines = []
    for panel in panels:
        if panel.is_active and panel.consecutive_failures:
            detail = _(
                "admin_servers.health.failing",
                count=panel.consecutive_failures,
                error=_.esc((panel.last_error or "—")[:120]),
            )
        elif panel.last_ok_at:
            detail = _("admin_servers.health.ok_since", when=_.datetime(panel.last_ok_at))
        else:
            detail = _("admin_servers.health.never")
        lines.append(f"{status_icon(panel, threshold)} <b>{_.esc(panel.name)}</b> — {detail}")
    endpoints = "\n".join(f"• {_.esc(e.address)}:{e.port or '—'} — {_.esc((e.last_error or '—')[:80])}" for e in hidden)
    text = _(
        "admin_servers.health.title",
        panels="\n".join(lines) or _("admin_servers.health.no_panels"),
        endpoints=endpoints or _("admin_servers.health.no_hidden"),
    )
    if note:
        text += "\n\n" + note
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_servers.btn.check_now"), _cb("hcheck"))],
        [_back(_, "home")],
    ]
    return Screen(text, kb(rows))


@router.callback_query(SrvCb.filter(F.a == "health"))
async def open_health(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await health_screen(ctx, _))


@router.callback_query(SrvCb.filter(F.a == "hcheck"))
async def check_health(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await answer_callback(callback, _("admin_servers.health.checking"))
    changes = await inventory.check_health(ctx.session_factory, ctx.panels, ctx.settings)
    note = _("admin_servers.health.checked", changes=len(changes))
    await render(callback, await health_screen(ctx, _, note))
