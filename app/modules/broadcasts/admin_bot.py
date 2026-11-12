"""Admin broadcasts: compose any message, pick an audience, preview, send now or schedule, watch progress, cancel."""

from __future__ import annotations

import logging

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, answer_callback, render
from app.db.models import Broadcast, BroadcastStatus, Location, Panel, Plan
from app.db.session import read_session
from app.keyboards import Button, back, kb, paginate
from app.modules.admin.common import AdminInputCb, admin_input_group, admin_router, input_button
from app.modules.admin_maintenance.timeinput import parse_moment
from app.modules.broadcasts import progress
from app.modules.broadcasts.callbacks import BcCb
from app.services import broadcasts
from app.services.broadcasts import Audience, AudienceSpec, BroadcastError, BroadcastErrorCode
from app.utils.time import utcnow

log = logging.getLogger(__name__)

router = admin_router("broadcasts_admin")
router.message.filter(HasPermission(Permission.BROADCASTS))
router.callback_query.filter(HasPermission(Permission.BROADCASTS))

HISTORY_PAGE = 8
NO_BUTTONS = "bc_nobtn"  # input buttons keep the compose state (see app.modules.admin.common)
BACK_TO_AUDIENCE = "bc_audb"
EXPIRING_PRESETS = (1, 3, 7, 14)
# Segments offered in the bot; ``None`` = needs a parameter screen.
SIMPLE_AUDIENCES = (Audience.ALL, Audience.ACTIVE, Audience.TRIAL, Audience.EXPIRED, Audience.NEVER_PAID)
PARAM_AUDIENCES = (Audience.EXPIRING, Audience.LANGUAGE, Audience.PLAN, Audience.PANEL, Audience.LOCATION)
STATUS_ICONS = {
    BroadcastStatus.DRAFT: "📝",
    BroadcastStatus.RUNNING: "🚀",
    BroadcastStatus.DONE: "✅",
    BroadcastStatus.CANCELED: "⏹",
}


@admin_input_group
class BcStates(StatesGroup):
    content = State()
    buttons = State()
    custom_ids = State()
    schedule = State()


def _cb(action: str, id_: int = 0, value: str = "") -> BcCb:
    return BcCb(a=action, id=id_, v=value)


# --- audience ---------------------------------------------------------------------------------------------------------
def audience_from_value(value: str) -> AudienceSpec:
    """``"plan.5"`` / ``"expiring_in_days.3"`` / ``"all"`` -> spec (raises :class:`BroadcastError`)."""
    kind_text, _sep, param = value.partition(".")
    try:
        kind = Audience(kind_text)
    except ValueError:
        raise BroadcastError(BroadcastErrorCode.BAD_AUDIENCE) from None
    data: dict[str, object] = {"kind": kind.value}
    if param:
        field = {
            Audience.EXPIRING: "days",
            Audience.LANGUAGE: "language",
            Audience.PLAN: "plan_id",
            Audience.PANEL: "panel_id",
            Audience.LOCATION: "location_id",
        }.get(kind)
        if field is None:
            raise BroadcastError(BroadcastErrorCode.BAD_AUDIENCE)
        data[field] = param
    spec = AudienceSpec.from_dict(data)
    spec.validate()
    return spec


async def describe_audience(ctx: AppContext, _: Translator, spec: AudienceSpec) -> str:
    """Human-readable segment, e.g. "План «Месяц»" or "Подписка заканчивается в ближайшие 3 дня"."""
    name = ""
    async with read_session(ctx.session_factory) as session:
        if spec.kind == Audience.PLAN and spec.plan_id is not None:
            plan = await session.get(Plan, spec.plan_id)
            name = plan.title if plan else f"#{spec.plan_id}"
        elif spec.kind == Audience.PANEL and spec.panel_id is not None:
            panel = await session.get(Panel, spec.panel_id)
            name = panel.name if panel else f"#{spec.panel_id}"
        elif spec.kind == Audience.LOCATION and spec.location_id is not None:
            location = await session.get(Location, spec.location_id)
            name = f"{location.flag} {location.name}".strip() if location else f"#{spec.location_id}"
    return _(
        f"broadcasts.aud.{spec.kind.value}",
        days=_.plural(spec.days or 0, "plural.days"),
        language=_(f"lang.{spec.language}") if spec.language else "",
        name=_.esc(name),
        count=_.number(len(spec.user_ids)),
    )


async def _count(ctx: AppContext, spec: AudienceSpec) -> int:
    async with read_session(ctx.session_factory) as session:
        return await broadcasts.count_audience(session, spec)


# --- screens ----------------------------------------------------------------------------------------------------------
async def home_screen(ctx: AppContext, _: Translator) -> Screen:
    async with read_session(ctx.session_factory) as session:
        recent = await broadcasts.history(session, limit=50)
    running = sum(1 for b in recent if b.status == BroadcastStatus.RUNNING)
    drafts = sum(1 for b in recent if b.status == BroadcastStatus.DRAFT)
    text = _("broadcasts.home", running=_.number(running), drafts=_.number(drafts))
    return Screen(
        text,
        kb(
            [
                [Button.cb(_("broadcasts.btn.new"), _cb("new"), style="primary")],
                [Button.cb(_("broadcasts.btn.history"), _cb("hist"))],
                [back("admin", _)],
            ]
        ),
    )


def audience_screen(_: Translator) -> Screen:
    rows: list[list[InlineKeyboardButton]] = [
        [Button.cb(_(f"broadcasts.seg.{kind.value}"), _cb("aud", value=kind.value))] for kind in SIMPLE_AUDIENCES
    ]
    rows += [[Button.cb(_(f"broadcasts.seg.{kind.value}"), _cb("audp", value=kind.value))] for kind in PARAM_AUDIENCES]
    rows.append([Button.cb(_("broadcasts.seg.custom"), _cb("audc"))])
    rows.append([Button.cb(_("nav.cancel"), _cb("home"))])
    return Screen(_("broadcasts.audience"), kb(rows))


async def audience_param_screen(ctx: AppContext, _: Translator, kind: Audience) -> Screen:
    rows: list[list[InlineKeyboardButton]] = []
    if kind == Audience.EXPIRING:
        rows = [
            [
                Button.cb(_.plural(days, "plural.days"), _cb("aud", value=f"{kind.value}.{days}"))
                for days in EXPIRING_PRESETS
            ]
        ]
    elif kind == Audience.LANGUAGE:
        rows = [[Button.cb(_(f"lang.{code}"), _cb("aud", value=f"{kind.value}.{code}")) for code in ctx.i18n.languages]]
    else:
        async with read_session(ctx.session_factory) as session:
            if kind == Audience.PLAN:
                plans = (await session.scalars(select(Plan).order_by(Plan.sort_order, Plan.id))).all()
                items = [(p.id, p.title) for p in plans]
            elif kind == Audience.PANEL:
                panels = (await session.scalars(select(Panel).order_by(Panel.id))).all()
                items = [(p.id, p.name) for p in panels]
            else:
                locations = (await session.scalars(select(Location).order_by(Location.sort_order, Location.id))).all()
                items = [(loc.id, f"{loc.flag} {loc.name}".strip()) for loc in locations]
        rows = [[Button.cb(title[:60], _cb("aud", value=f"{kind.value}.{item_id}"))] for item_id, title in items[:40]]
        if not rows:
            return Screen(_("broadcasts.audience_empty"), kb([[Button.cb(_("nav.back"), _cb("audb"))]]))
    rows.append([Button.cb(_("nav.back"), _cb("audb"))])
    return Screen(_(f"broadcasts.audience_pick.{kind.value}"), kb(rows))


async def draft_screen(ctx: AppContext, _: Translator, broadcast_id: int) -> Screen:
    async with read_session(ctx.session_factory) as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast is None:
            return Screen(_("broadcasts.not_found"), kb([[Button.cb(_("nav.back"), _cb("home"))]]))
        options = await broadcasts.get_options(session, broadcast)
        count = await broadcasts.count_audience(session, options.audience)
    if broadcast.status != BroadcastStatus.DRAFT:
        return await view_screen(ctx, _, broadcast_id)
    buttons = broadcasts.keyboard(broadcast.buttons)
    button_count = sum(len(row) for row in buttons.inline_keyboard) if buttons else 0
    when = _.datetime(options.scheduled_at) if options.scheduled_at else _("broadcasts.manual")
    text = _(
        "broadcasts.draft",
        id=broadcast.id,
        audience=await describe_audience(ctx, _, options.audience),
        count=_.number(count),
        buttons=_.number(button_count),
        when=when,
    )
    scheduled = options.scheduled_at is not None
    rows: list[list[InlineKeyboardButton | None]] = [
        [Button.cb(_("broadcasts.btn.preview"), _cb("prev", broadcast.id))],
        [Button.cb(_("broadcasts.btn.send"), _cb("send", broadcast.id), style="success")],
        [
            Button.cb(
                _("broadcasts.btn.reschedule" if scheduled else "broadcasts.btn.schedule"), _cb("sch", broadcast.id)
            ),
            Button.cb(_("broadcasts.btn.unschedule"), _cb("unsch", broadcast.id)) if scheduled else None,
        ],
        [Button.cb(_("broadcasts.btn.discard"), _cb("disc", broadcast.id), style="danger")],
        [Button.cb(_("nav.back"), _cb("home"))],
    ]
    return Screen(text, kb(rows))


async def view_screen(ctx: AppContext, _: Translator, broadcast_id: int) -> Screen:
    async with read_session(ctx.session_factory) as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast is None:
            return Screen(_("broadcasts.not_found"), kb([[Button.cb(_("nav.back"), _cb("hist"))]]))
        options = await broadcasts.get_options(session, broadcast)
    if broadcast.status == BroadcastStatus.RUNNING:
        return progress.progress_screen(_, progress.snapshot(broadcast))
    text = _(
        "broadcasts.view",
        id=broadcast.id,
        status=_(f"broadcasts.status.{broadcast.status.value}"),
        audience=await describe_audience(ctx, _, options.audience),
        created=_.datetime(broadcast.created_at),
        started=_.datetime(broadcast.started_at) if broadcast.started_at else "—",
        finished=_.datetime(broadcast.finished_at) if broadcast.finished_at else "—",
        total=_.number(broadcast.total),
        sent=_.number(broadcast.sent),
        blocked=_.number(broadcast.blocked),
        failed=_.number(broadcast.failed),
    )
    rows = []
    if broadcast.status == BroadcastStatus.DRAFT:
        rows.append([Button.cb(_("broadcasts.btn.open_draft"), _cb("draft", broadcast.id))])
    rows.append([Button.cb(_("broadcasts.btn.preview"), _cb("prev", broadcast.id))])
    rows.append([Button.cb(_("nav.back"), _cb("hist"))])
    return Screen(text, kb(rows))


async def history_screen(ctx: AppContext, _: Translator, page: int) -> Screen:
    async with read_session(ctx.session_factory) as session:
        items = await broadcasts.history(session, limit=200)
    if not items:
        return Screen(_("broadcasts.history_empty"), kb([[Button.cb(_("nav.back"), _cb("home"))]]))

    def button(item: Broadcast) -> InlineKeyboardButton:
        icon = STATUS_ICONS.get(item.status, "•")
        date = item.created_at.astimezone(ctx.settings.zone).strftime("%d.%m %H:%M")
        label = f"{icon} #{item.id} · {date} · {item.sent}/{item.total}"
        return Button.cb(label, _cb("view", item.id))

    page_data = paginate(items, page, HISTORY_PAGE, button, lambda p: _cb("hist", value=str(p)))
    rows = [*page_data.rows, [Button.cb(_("nav.back"), _cb("home"))]]
    return Screen(_("broadcasts.history", total=_.number(page_data.total)), kb(rows))


def _error_text(_: Translator, exc: BroadcastError) -> str:
    return _(f"broadcasts.error.{exc.code.value}")


# --- entry ------------------------------------------------------------------------------------------------------------
@router.message(Command("broadcast"))
async def broadcast_command(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(message, await home_screen(ctx, _))


@router.callback_query(BcCb.filter(F.a == "home"))
async def open_home(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, await home_screen(ctx, _))


# --- compose ----------------------------------------------------------------------------------------------------------
@router.callback_query(BcCb.filter(F.a == "new"))
async def start_compose(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.set_state(BcStates.content)
    await render(callback, Screen(_("broadcasts.compose"), kb([[Button.cb(_("nav.cancel"), _cb("home"))]])))


@router.message(BcStates.content)
async def got_content(message: Message, _: Translator, state: FSMContext) -> None:
    if message.text is not None and message.text.startswith("/"):
        await state.clear()
        await render(message, Screen(_("common.canceled")))
        return
    await state.update_data(src_chat=message.chat.id, src_msg=message.message_id, buttons=None)
    await state.set_state(BcStates.buttons)
    await render(
        message,
        Screen(
            _("broadcasts.ask_buttons"),
            kb([[input_button(_("broadcasts.btn.no_buttons"), NO_BUTTONS)], [Button.cb(_("nav.cancel"), _cb("home"))]]),
        ),
        keep_input=True,  # this message is copied to the recipients later
    )


@router.message(BcStates.buttons, F.text)
async def got_buttons(message: Message, _: Translator, state: FSMContext) -> None:
    try:
        rows = broadcasts.parse_buttons(message.text or "")
    except BroadcastError:
        await render(
            message,
            Screen(_("broadcasts.error.bad_buttons"), kb([[input_button(_("broadcasts.btn.no_buttons"), NO_BUTTONS)]])),
        )
        return
    await state.update_data(buttons=rows)
    await state.set_state(None)
    await render(message, audience_screen(_))


@router.callback_query(AdminInputCb.filter(F.action == NO_BUTTONS))
async def skip_buttons(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    if not (await state.get_data()).get("src_msg"):
        await render(callback, Screen(_("broadcasts.expired"), kb([[Button.cb(_("nav.back"), _cb("home"))]])))
        return
    await state.update_data(buttons=None)
    await state.set_state(None)
    await render(callback, audience_screen(_))


@router.callback_query(BcCb.filter(F.a == "audb"))
@router.callback_query(AdminInputCb.filter(F.action == BACK_TO_AUDIENCE))
async def back_to_audience(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.set_state(None)
    await render(callback, audience_screen(_))


@router.callback_query(BcCb.filter(F.a == "audp"))
async def audience_param(callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator) -> None:
    try:
        kind = Audience(callback_data.v)
    except ValueError:
        await answer_callback(callback, _("error.expired_button"))
        return
    await render(callback, await audience_param_screen(ctx, _, kind))


@router.callback_query(BcCb.filter(F.a == "audc"))
async def audience_custom(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.set_state(BcStates.custom_ids)
    await render(callback, Screen(_("broadcasts.ask_ids"), kb([[input_button(_("nav.back"), BACK_TO_AUDIENCE)]])))


@router.message(BcStates.custom_ids, F.text)
async def got_custom_ids(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    ids = broadcasts.parse_user_ids(message.text or "")
    spec = AudienceSpec(kind=Audience.CUSTOM, user_ids=ids)
    try:
        spec.validate()
    except BroadcastError:
        await render(
            message, Screen(_("broadcasts.error.bad_ids"), kb([[input_button(_("nav.back"), BACK_TO_AUDIENCE)]]))
        )
        return
    await state.set_state(None)
    await _create_draft(message, ctx, _, state, spec)


@router.callback_query(BcCb.filter(F.a == "aud"))
async def audience_chosen(
    callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    try:
        spec = audience_from_value(callback_data.v)
    except BroadcastError:
        await answer_callback(callback, _("broadcasts.error.bad_audience"), alert=True)
        return
    await _create_draft(callback, ctx, _, state, spec)


async def _create_draft(
    event: Message | CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext, spec: AudienceSpec
) -> None:
    data = await state.get_data()
    src_chat, src_msg = data.get("src_chat"), data.get("src_msg")
    if not src_chat or not src_msg:
        await render(event, Screen(_("broadcasts.expired"), kb([[Button.cb(_("nav.back"), _cb("home"))]])))
        return
    actor = event.from_user.id if event.from_user else 0
    try:
        async with ctx.session_factory.begin() as session:
            draft = await broadcasts.create_draft(
                session,
                actor,
                spec,
                buttons=data.get("buttons"),
                source_chat_id=int(src_chat),
                source_message_id=int(src_msg),
            )
            draft_id = draft.id
    except BroadcastError as exc:
        await render(event, Screen(_error_text(_, exc), kb([[Button.cb(_("nav.back"), _cb("home"))]])))
        return
    await state.clear()
    await render(event, await draft_screen(ctx, _, draft_id))


# --- draft actions ----------------------------------------------------------------------------------------------------
@router.callback_query(BcCb.filter(F.a == "draft"))
async def open_draft(
    callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.clear()
    await render(callback, await draft_screen(ctx, _, callback_data.id))


@router.callback_query(BcCb.filter(F.a == "prev"))
async def preview(callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator) -> None:
    try:
        outcome = await broadcasts.send_preview(ctx.session_factory, ctx.bot, callback_data.id, callback.from_user.id)
    except BroadcastError:
        await answer_callback(callback, _("broadcasts.not_found"), alert=True)
        return
    ok = outcome == broadcasts.Delivery.SENT
    await answer_callback(callback, _("broadcasts.preview_sent" if ok else "broadcasts.preview_failed"), alert=not ok)


@router.callback_query(BcCb.filter(F.a == "send"))
async def confirm_send(callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator) -> None:
    count: int | None = None
    async with read_session(ctx.session_factory) as session:
        broadcast = await session.get(Broadcast, callback_data.id)
        if broadcast is not None and broadcast.status == BroadcastStatus.DRAFT:
            options = await broadcasts.get_options(session, broadcast)
            count = await broadcasts.count_audience(session, options.audience)
    if count is None:
        await render(callback, await view_screen(ctx, _, callback_data.id))
        return
    if count == 0:
        await answer_callback(callback, _("broadcasts.nobody"), alert=True)
        return
    text = _("broadcasts.confirm_send", id=callback_data.id, count=_.number(count))
    keyboard = kb(
        [
            [Button.cb(_("broadcasts.btn.send_confirm"), _cb("go", callback_data.id), style="success")],
            [Button.cb(_("nav.back"), _cb("draft", callback_data.id))],
        ]
    )
    await render(callback, Screen(text, keyboard))


@router.callback_query(BcCb.filter(F.a == "go"))
async def send_now(callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator) -> None:
    try:
        async with ctx.session_factory.begin() as session:
            broadcast = await broadcasts.start(session, callback_data.id, callback.from_user.id)
            snapshot = progress.snapshot(broadcast)
    except BroadcastError as exc:
        await answer_callback(callback, _error_text(_, exc), alert=True)
        return
    shown = await render(callback, progress.progress_screen(_, snapshot), answer=_("broadcasts.started"))
    if shown is not None:
        await progress.remember_message(ctx, callback_data.id, shown.chat.id, shown.message_id, _.lang)
    progress.kick_runner(ctx)


@router.callback_query(BcCb.filter(F.a == "sch"))
async def ask_schedule(
    callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    await state.set_state(BcStates.schedule)
    await state.update_data(bid=callback_data.id)
    now_local = utcnow().astimezone(ctx.settings.zone).strftime("%d.%m %H:%M")
    text = _("broadcasts.ask_schedule", now=now_local, tz=_.esc(ctx.settings.tz))
    await render(callback, Screen(text, kb([[Button.cb(_("nav.back"), _cb("draft", callback_data.id))]])))


@router.message(BcStates.schedule, F.text)
async def got_schedule(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    broadcast_id = int((await state.get_data()).get("bid") or 0)
    now = utcnow()
    when = parse_moment(message.text or "", ctx.settings.zone, now)
    back_kb = kb([[Button.cb(_("nav.back"), _cb("draft", broadcast_id))]])
    if when is None or when <= now:
        await render(message, Screen(_("broadcasts.error.bad_schedule"), back_kb))
        return
    try:
        async with ctx.session_factory.begin() as session:
            await broadcasts.schedule(session, broadcast_id, when, message.chat.id, now=now)
    except BroadcastError as exc:
        await render(message, Screen(_error_text(_, exc), back_kb))
        return
    await state.clear()
    await render(message, await draft_screen(ctx, _, broadcast_id))


@router.callback_query(BcCb.filter(F.a == "unsch"))
async def unschedule(callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator) -> None:
    try:
        async with ctx.session_factory.begin() as session:
            await broadcasts.schedule(session, callback_data.id, None, callback.from_user.id)
    except BroadcastError as exc:
        await answer_callback(callback, _error_text(_, exc), alert=True)
        return
    await render(callback, await draft_screen(ctx, _, callback_data.id), answer=_("broadcasts.unscheduled"))


@router.callback_query(BcCb.filter(F.a == "disc"))
async def discard(callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        await broadcasts.cancel(session, callback_data.id, callback.from_user.id)
    await render(callback, await home_screen(ctx, _), answer=_("broadcasts.discarded"))


# --- progress ---------------------------------------------------------------------------------------------------------
@router.callback_query(BcCb.filter(F.a == "prog"))
async def refresh_progress(callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator) -> None:
    await render(callback, await view_screen(ctx, _, callback_data.id))


@router.callback_query(BcCb.filter(F.a == "stop"))
async def ask_stop(callback: CallbackQuery, callback_data: BcCb, _: Translator) -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [Button.cb(_("broadcasts.btn.stop_confirm"), _cb("stopy", callback_data.id), style="danger")],
            [Button.cb(_("nav.back"), _cb("prog", callback_data.id))],
        ]
    )
    await render(callback, Screen(_("broadcasts.confirm_stop", id=callback_data.id), keyboard))


@router.callback_query(BcCb.filter(F.a == "stopy"))
async def stop(callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator) -> None:
    async with ctx.session_factory.begin() as session:
        stopped = await broadcasts.cancel(session, callback_data.id, callback.from_user.id)
    note = _("broadcasts.stopped" if stopped else "broadcasts.already_finished")
    await render(callback, await view_screen(ctx, _, callback_data.id), answer=note)


# --- history ----------------------------------------------------------------------------------------------------------
@router.callback_query(BcCb.filter(F.a == "hist"))
async def history(callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator) -> None:
    page = int(callback_data.v) if callback_data.v.isdigit() else 0
    await render(callback, await history_screen(ctx, _, page))


@router.callback_query(BcCb.filter(F.a == "view"))
async def view(callback: CallbackQuery, callback_data: BcCb, ctx: AppContext, _: Translator) -> None:
    await render(callback, await view_screen(ctx, _, callback_data.id))
