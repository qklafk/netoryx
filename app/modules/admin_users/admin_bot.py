"""Users section of the bot admin: search, the user card and every per-user action."""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageOriginUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.context import AppContext
from app.core.filters import HasPermission, Permission, has_permission
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import (
    AdminRole,
    LedgerAccount,
    Payment,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    TxKind,
    User,
)
from app.db.session import read_session
from app.keyboards import Btn, Button, Nav, kb, nav
from app.keyboards.common import confirm_kb
from app.modules.admin.common import (
    PAGE_SIZE,
    admin_input_group,
    admin_router,
    first_time,
    money_signed,
    new_nonce,
    parse_amount_kop,
    parse_int,
    to_admin,
    user_short,
    user_title,
    when,
)
from app.modules.admin_users import ops
from app.services import audit, devices, plans, provisioning, subscriptions, users, wallet
from app.services.notify import mark_blocked
from app.utils.time import MAX_PERIOD_DAYS, days_left, utcnow

log = logging.getLogger(__name__)

router = admin_router("admin_users")

USERS_NAV = "adm_users"
USER_NAV = "adm_user"  # Nav(to=USER_NAV, arg=<tg_id>) opens a user card from any admin screen
FOREVER_YEAR = 2090  # expiry dates this far ahead are shown as "forever"
MAX_LIMIT = 1_000_000


class UserCb(CallbackData, prefix="admu"):
    a: str  # action
    uid: int
    arg: str = ""


class UserListCb(CallbackData, prefix="adml"):
    f: str  # filter: recent | active | banned
    page: int = 0


@admin_input_group
class UserInput(StatesGroup):
    query = State()
    value = State()  # data: uid, action
    message = State()  # data: uid


LIST_FILTERS = ("recent", "active", "banned")
VALUE_ACTIONS: dict[str, Permission] = {
    "bal": Permission.USERS_MONEY,
    "pbal": Permission.USERS_MONEY,
    "days": Permission.SUBS_GRANT,
    "dev": Permission.SUBS_GRANT,
    "traf": Permission.SUBS_GRANT,
}


def card_button(_: Translator, uid: int) -> Btn:
    return Button.cb(_("admin_users.back_to_card"), UserCb(a="card", uid=uid))


# --- search ----------------------------------------------------------------------------------------------------------


def search_screen(_: Translator, note: str | None = None) -> Screen:
    text = _("admin_users.search.prompt")
    if note:
        text = f"{note}\n\n{text}"
    rows = [
        [Button.cb(_(f"admin_users.list.{name}"), UserListCb(f=name)) for name in LIST_FILTERS],
        [to_admin(_)],
    ]
    return Screen(text, kb(rows))


@router.callback_query(Nav.filter(F.to == USERS_NAV), HasPermission(Permission.USERS_VIEW))
async def open_search(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.set_state(UserInput.query)
    await render(callback, search_screen(_))


@router.message(Command("user"), HasPermission(Permission.USERS_VIEW))
async def user_command(
    message: Message,
    command: CommandObject,
    ctx: AppContext,
    _: Translator,
    admin_role: AdminRole,
    state: FSMContext,
) -> None:
    if not command.args:
        await state.set_state(UserInput.query)
        await render(message, search_screen(_))
        return
    await _search_and_show(message, ctx, _, admin_role, state, command.args)


@router.message(UserInput.query, HasPermission(Permission.USERS_VIEW))
async def search_input(
    message: Message, ctx: AppContext, _: Translator, admin_role: AdminRole, state: FSMContext
) -> None:
    origin = message.forward_origin
    if origin is not None:
        if isinstance(origin, MessageOriginUser):
            await _search_and_show(message, ctx, _, admin_role, state, str(origin.sender_user.id))
        else:
            await render(message, search_screen(_, _("admin_users.search.hidden_forward")))
        return
    if not message.text:
        await render(message, search_screen(_, _("admin_users.search.text_only")))
        return
    await _search_and_show(message, ctx, _, admin_role, state, message.text)


async def _search_and_show(
    message: Message, ctx: AppContext, _: Translator, role: AdminRole, state: FSMContext, query: str
) -> None:
    async with read_session(ctx.session_factory) as session, session.begin():
        found = await ops.search(session, query)
    if not found:
        await state.set_state(UserInput.query)
        await render(message, search_screen(_, _("admin_users.search.nothing", query=_.esc(query[:64]))))
        return
    if len(found) == 1:
        await state.clear()
        await render(message, await card_screen(ctx, _, role, found[0].tg_id))
        return
    await state.set_state(UserInput.query)
    rows = [[Button.cb(user_short(user), UserCb(a="card", uid=user.tg_id))] for user in found]
    rows.append([nav(_("admin_users.search.again"), USERS_NAV), to_admin(_)])
    text = _("admin_users.search.found", count=len(found), query=_.esc(query[:64]))
    await render(message, Screen(text, kb(rows)))


@router.callback_query(UserListCb.filter(), HasPermission(Permission.USERS_VIEW))
async def user_list(
    callback: CallbackQuery, callback_data: UserListCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    name = callback_data.f if callback_data.f in LIST_FILTERS else "recent"
    page = max(0, callback_data.page)
    now = utcnow()
    stmt = select(User)
    if name == "active":
        stmt = stmt.join(Subscription, Subscription.user_id == User.tg_id).where(
            Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at > now
        )
    elif name == "banned":
        stmt = stmt.where(User.is_banned.is_(True))
    stmt = stmt.order_by(User.created_at.desc(), User.tg_id.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1)
    async with read_session(ctx.session_factory) as session, session.begin():
        found = list((await session.scalars(stmt)).all())
    has_next = len(found) > PAGE_SIZE
    rows: list[list[Btn]] = [
        [Button.cb(user_short(user), UserCb(a="card", uid=user.tg_id))] for user in found[:PAGE_SIZE]
    ]
    pager = [
        Button.cb("◀️", UserListCb(f=name, page=page - 1)) if page > 0 else None,
        Button.cb("▶️", UserListCb(f=name, page=page + 1)) if has_next else None,
    ]
    rows.append([b for b in pager if b is not None])
    rows.append([nav(_("admin_users.search.again"), USERS_NAV), to_admin(_)])
    title = _(f"admin_users.list.{name}")
    text = _("admin_users.list.title", title=title, page=page + 1)
    if not found:
        text += "\n\n" + _("admin_users.list.empty")
    await state.set_state(UserInput.query)
    await render(callback, Screen(text, kb(rows)))


# --- the card --------------------------------------------------------------------------------------------------------


def _expiry(_: Translator, value: datetime) -> str:
    return _("fmt.forever") if value.year >= FOREVER_YEAR else _.date(value)


def _limits(_: Translator, devices_limit: int, traffic_gb: int) -> tuple[str, str]:
    dev = _("fmt.unlimited") if devices_limit == 0 else _.plural(devices_limit, "plural.devices")
    traffic = _("fmt.unlimited") if traffic_gb == 0 else _.plural(traffic_gb, "plural.gb")
    return dev, traffic


def _subscription_lines(_: Translator, card: ops.UserCard, now: datetime) -> list[str]:
    sub = card.subscription
    if sub is None:
        return [_("admin_users.card.no_subscription")]
    expires = _expiry(_, sub.expires_at)
    if sub.status == SubscriptionStatus.DISABLED:
        status = _("admin_users.card.sub_disabled", expires=expires)
    elif sub.expires_at > now:
        left = (
            _("fmt.forever")
            if sub.expires_at.year >= FOREVER_YEAR
            else _.plural(days_left(sub.expires_at, now), "plural.days")
        )
        status = _("admin_users.card.sub_active", expires=expires, left=left)
    else:
        status = _("admin_users.card.sub_expired", expires=expires)
    dev, traffic = _limits(_, sub.device_limit, sub.traffic_limit_gb)
    plan = _.esc(card.plan.title) if card.plan is not None else _("admin_users.card.no_plan")
    lines = [status, _("admin_users.card.plan", plan=plan, devices=dev, traffic=traffic)]
    flags = [_("admin_users.card.auto_renew_on" if sub.auto_renew else "admin_users.card.auto_renew_off")]
    if sub.is_trial:
        flags.append(_("admin_users.card.trial"))
    if sub.is_complimentary:
        flags.append(_("admin_users.card.complimentary"))
    if sub.stars_charge_id:
        flags.append(_("admin_users.card.stars_subscription"))
    lines.append(" · ".join(flags))
    return lines


def card_text(_: Translator, card: ops.UserCard, now: datetime) -> str:
    user = card.user
    lines = [
        _("admin_users.card.title", user=user_title(user)),
        _(
            "admin_users.card.profile",
            language=_.esc(user.language),
            created=_.date(user.created_at),
            seen=when(_, user.last_seen_at),
        ),
    ]
    flags = []
    if user.is_banned:
        flags.append(_("admin_users.card.banned"))
    if user.bot_blocked:
        flags.append(_("admin_users.card.bot_blocked"))
    if card.role is not None:
        flags.append(_("admin_users.card.admin", role=_(f"admin.role.{card.role.value}")))
    if flags:
        lines.append(" · ".join(flags))
    referral = [_("admin_users.card.invited", count=_.number(card.invited))]
    if user.referred_by:
        referral.insert(0, _("admin_users.card.referred_by", user_id=user.referred_by))
    if card.campaign:
        referral.append(_("admin_users.card.campaign", code=_.esc(card.campaign)))
    lines.append(" · ".join(referral))
    lines.append("")
    lines += _subscription_lines(_, card, now)
    lines.append("")
    lines.append(
        _(
            "admin_users.card.balances",
            balance=_.money(user.balance_kop),
            partner=_.money(user.referral_balance_kop),
        )
    )
    if card.payments_count:
        lines.append(
            _(
                "admin_users.card.payments",
                count=_.number(card.payments_count),
                total=_.money(card.payments_sum_kop),
                last=when(_, card.last_payment_at),
            )
        )
    else:
        lines.append(_("admin_users.card.no_payments"))
    return "\n".join(lines)


def card_keyboard(_: Translator, settings: Settings, role: AdminRole, card: ops.UserCard) -> list[list[Btn | None]]:
    user = card.user
    uid = user.tg_id
    sub = card.subscription
    money = has_permission(role, Permission.USERS_MONEY)
    edit = has_permission(role, Permission.USERS_EDIT)
    grant = edit and has_permission(role, Permission.SUBS_GRANT)
    main_balance = money and (settings.app_mode == "shop" or user.balance_kop != 0)
    partner_balance = money and (settings.referral_reward_mode == "partner" or user.referral_balance_kop != 0)

    def btn(key: str, action: str, allowed: bool, *, style: str | None = None, **params: object) -> Btn | None:
        return Button.cb(_(key, **params), UserCb(a=action, uid=uid), style=style) if allowed else None

    has_sub = sub is not None
    rows: list[list[Btn | None]] = [
        [
            btn("admin_users.btn.balance", "bal", main_balance),
            btn("admin_users.btn.partner_balance", "pbal", partner_balance),
        ],
        [btn("admin_users.btn.days", "days", grant), btn("admin_users.btn.plan", "plan", grant and has_sub)],
        [
            btn("admin_users.btn.devices_limit", "dev", grant and has_sub),
            btn("admin_users.btn.traffic", "traf", grant and has_sub),
        ],
    ]
    if sub is not None:
        renew_key = "admin_users.btn.auto_renew_off" if sub.auto_renew else "admin_users.btn.auto_renew_on"
        comp_key = "admin_users.btn.comp_off" if sub.is_complimentary else "admin_users.btn.comp_on"
        block = sub.status == SubscriptionStatus.DISABLED
        rows += [
            [btn(renew_key, "renew", edit), btn(comp_key, "comp", money)],
            [btn("admin_users.btn.reset_devices", "devices", edit), btn("admin_users.btn.rotate", "rotate", edit)],
            [
                btn("admin_users.btn.sync", "sync", edit),
                btn(
                    "admin_users.btn.unblock" if block else "admin_users.btn.block",
                    "unblock" if block else "block",
                    edit,
                ),
            ],
        ]
    rows.append([btn("admin_users.btn.message", "msg", edit), btn("admin_users.btn.history", "hist", True)])
    protected = card.role is not None
    ban = card.user.is_banned
    rows.append(
        [
            btn(
                "admin_users.btn.unban" if ban else "admin_users.btn.ban",
                "unban" if ban else "ban",
                edit and not protected,
            ),
            btn("admin_users.btn.delete", "del", edit and money and not protected, style="danger"),
        ]
    )
    rows.append([btn("nav.refresh", "card", True), Button.cb(_("admin_users.search.again"), Nav(to=USERS_NAV))])
    rows.append([to_admin(_)])
    return rows


async def card_screen(ctx: AppContext, _: Translator, role: AdminRole, uid: int, *, note: str | None = None) -> Screen:
    async with read_session(ctx.session_factory) as session, session.begin():
        card = await ops.load_card(session, ctx.settings, uid)
    if card is None:
        return search_screen(_, _("admin_users.error.not_found"))
    text = card_text(_, card, utcnow())
    if note:
        text = f"{note}\n\n{text}"
    return Screen(text, kb(card_keyboard(_, ctx.settings, role, card)))


@router.callback_query(UserCb.filter(F.a == "card"), HasPermission(Permission.USERS_VIEW))
async def show_card(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    await render(callback, await card_screen(ctx, _, admin_role, callback_data.uid))


@router.callback_query(Nav.filter(F.to == USER_NAV), HasPermission(Permission.USERS_VIEW))
async def show_card_nav(
    callback: CallbackQuery, callback_data: Nav, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    uid = parse_int(callback_data.arg)
    if uid is None:
        await answer_callback(callback, _("error.expired_button"))
        return
    await render(callback, await card_screen(ctx, _, admin_role, uid))


async def _sync_note(ctx: AppContext, _: Translator, uid: int) -> str:
    """Push the user's subscription to the panels now; a short note about the result."""
    async with read_session(ctx.session_factory) as session, session.begin():
        sub_id = await session.scalar(select(Subscription.id).where(Subscription.user_id == uid))
    if sub_id is None:
        return _("admin.saved")
    try:
        result = await provisioning.sync_subscription(ctx.session_factory, ctx.panels, ctx.settings, sub_id)
    except Exception:
        log.exception("admin: sync of subscription %s failed", sub_id)
        return _("admin_users.sync.later")
    if result.ok:
        return _("admin_users.sync.done")
    return _("admin_users.sync.later")


# --- value inputs: balances, days, limits ----------------------------------------------------------------------------


@router.callback_query(UserCb.filter(F.a.in_(set(VALUE_ACTIONS))))
async def ask_value(
    callback: CallbackQuery,
    callback_data: UserCb,
    ctx: AppContext,
    _: Translator,
    admin_role: AdminRole,
    state: FSMContext,
) -> None:
    if not has_permission(admin_role, VALUE_ACTIONS[callback_data.a]):
        await answer_callback(callback, _("error.not_allowed"), alert=True)
        return
    await state.set_state(UserInput.value)
    await state.set_data({"uid": callback_data.uid, "action": callback_data.a})
    text = _(f"admin_users.ask.{callback_data.a}")
    await render(callback, Screen(text, kb([[card_button(_, callback_data.uid)]])))


@router.message(UserInput.value)
async def value_input(
    message: Message, ctx: AppContext, _: Translator, admin_role: AdminRole, state: FSMContext
) -> None:
    data = await state.get_data()
    uid, action = int(data.get("uid", 0)), str(data.get("action", ""))
    if action not in VALUE_ACTIONS or not has_permission(admin_role, VALUE_ACTIONS[action]):
        await state.clear()
        return
    back = kb([[card_button(_, uid)]])
    if action in ("bal", "pbal"):
        kop = parse_amount_kop(message.text, signed=True)
        if kop is None:
            await render(message, Screen(_("admin.invalid_amount"), back))
            return
        await state.clear()
        account = "main" if action == "bal" else "partner"
        text = _(f"admin_users.confirm.balance_{account}", amount=money_signed(_, kop))
        yes = UserCb(a=f"{action}_ok", uid=uid, arg=f"{kop}.{new_nonce()}")
        await render(message, Screen(text, confirm_kb(_, yes, UserCb(a="card", uid=uid), danger=kop < 0)))
        return
    if action == "days":
        days = parse_int(message.text, signed=True, minimum=-MAX_PERIOD_DAYS, maximum=MAX_PERIOD_DAYS)
        if not days:
            await render(message, Screen(_("admin.invalid_number"), back))
            return
        await state.clear()
        key = "admin_users.confirm.add_days" if days > 0 else "admin_users.confirm.remove_days"
        text = _(key, days=_.plural(abs(days), "plural.days"))
        yes = UserCb(a="days_ok", uid=uid, arg=f"{days}.{new_nonce()}")
        await render(message, Screen(text, confirm_kb(_, yes, UserCb(a="card", uid=uid), danger=days < 0)))
        return
    value = parse_int(message.text, minimum=0, maximum=MAX_LIMIT)
    if value is None:
        await render(message, Screen(_("admin.invalid_number"), back))
        return
    await state.clear()
    actor = message.from_user.id if message.from_user else message.chat.id
    try:
        async with ctx.session_factory.begin() as session:
            if action == "dev":
                await ops.set_limits(session, uid, actor, device_limit=value)
            else:
                await ops.set_limits(session, uid, actor, traffic_limit_gb=value)
    except ops.UserOpError as exc:
        await render(message, await card_screen(ctx, _, admin_role, uid, note=_(f"admin_users.error.{exc.code}")))
        return
    note = await _sync_note(ctx, _, uid)
    await render(message, await card_screen(ctx, _, admin_role, uid, note=note))


def _split_arg(arg: str) -> tuple[int, str] | None:
    value, _sep, nonce = arg.partition(".")
    number = parse_int(value, signed=True)
    return (number, nonce) if number is not None and nonce else None


@router.callback_query(UserCb.filter(F.a.in_({"bal_ok", "pbal_ok"})), HasPermission(Permission.USERS_MONEY))
async def apply_balance(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    parsed = _split_arg(callback_data.arg)
    if parsed is None:
        await answer_callback(callback, _("error.expired_button"))
        return
    kop, nonce = parsed
    uid = callback_data.uid
    account = LedgerAccount.MAIN if callback_data.a == "bal_ok" else LedgerAccount.REFERRAL
    ref = f"admin:{nonce}"
    try:
        async with ctx.session_factory.begin() as session:
            if await session.get(User, uid) is None:
                raise ops.UserOpError("not_found")
            applied = await wallet.apply(
                session, uid, kop, TxKind.ADMIN_ADJUST, ref, actor_id=callback.from_user.id, account=account
            )
            if applied is not None:
                await _audit(
                    session, callback.from_user.id, "user.balance_adjust", uid, amount_kop=kop, account=account.value
                )
    except wallet.InsufficientFunds:
        note = _("admin_users.error.insufficient")
    except ops.UserOpError as exc:
        note = _(f"admin_users.error.{exc.code}")
    else:
        note = _("admin_users.done.balance", amount=money_signed(_, kop))
    await render(callback, await card_screen(ctx, _, admin_role, uid, note=note))


async def _audit(session: AsyncSession, actor: int, action: str, uid: int, **details: object) -> None:
    await audit.log(session, actor, action, "user", uid, "bot", None, **details)


@router.callback_query(UserCb.filter(F.a == "days_ok"), HasPermission(Permission.SUBS_GRANT))
async def apply_days(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    parsed = _split_arg(callback_data.arg)
    if parsed is None or parsed[0] == 0:
        await answer_callback(callback, _("error.expired_button"))
        return
    days, nonce = parsed
    uid = callback_data.uid
    if not first_time(f"days:{uid}:{nonce}"):
        await render(callback, await card_screen(ctx, _, admin_role, uid))
        return
    try:
        async with ctx.session_factory.begin() as session:
            user = await session.get(User, uid)
            if user is None:
                raise ops.UserOpError("not_found")
            if days > 0:
                await subscriptions.grant_days(
                    session, ctx.settings, user, days, kind=PurchaseKind.ADMIN_GRANT, actor_id=callback.from_user.id
                )
            else:
                await ops.remove_days(session, uid, -days, callback.from_user.id)
    except ops.UserOpError as exc:
        await render(callback, await card_screen(ctx, _, admin_role, uid, note=_(f"admin_users.error.{exc.code}")))
        return
    await answer_callback(callback, _("admin.working"))
    note = await _sync_note(ctx, _, uid)
    await render(callback, await card_screen(ctx, _, admin_role, uid, note=note))


# --- plan ------------------------------------------------------------------------------------------------------------


@router.callback_query(UserCb.filter(F.a == "plan"), HasPermission(Permission.SUBS_GRANT))
async def choose_plan(callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator) -> None:
    uid = callback_data.uid
    async with read_session(ctx.session_factory) as session, session.begin():
        current = await session.scalar(select(Subscription.plan_id).where(Subscription.user_id == uid))
        available = await plans.list_all(session)
    rows: list[list[Btn]] = []
    for plan in available:
        mark = "✓ " if plan.id == current else ""
        archived = "" if plan.is_active else " 🗄"
        label = f"{mark}{plan.title} · {_.money(plan.price_kop)}{archived}"
        rows.append([Button.cb(label[:64], UserCb(a="setplan", uid=uid, arg=str(plan.id)))])
    rows.append([card_button(_, uid)])
    text = _("admin_users.plan.title") if available else _("admin_users.plan.empty")
    await render(callback, Screen(text, kb(rows)))


@router.callback_query(UserCb.filter(F.a == "setplan"), HasPermission(Permission.SUBS_GRANT))
async def apply_plan(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    uid = callback_data.uid
    plan_id = parse_int(callback_data.arg)
    try:
        async with ctx.session_factory.begin() as session:
            plan = await plans.get(session, plan_id) if plan_id is not None else None
            if plan is None:
                raise ops.UserOpError("plan_not_found")
            await ops.set_plan(session, uid, plan, callback.from_user.id)
    except ops.UserOpError as exc:
        await render(callback, await card_screen(ctx, _, admin_role, uid, note=_(f"admin_users.error.{exc.code}")))
        return
    await answer_callback(callback, _("admin.working"))
    note = await _sync_note(ctx, _, uid)
    await render(callback, await card_screen(ctx, _, admin_role, uid, note=note))


# --- toggles ---------------------------------------------------------------------------------------------------------


@router.callback_query(UserCb.filter(F.a == "renew"), HasPermission(Permission.USERS_EDIT))
async def toggle_renew(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    await _toggle(callback, callback_data, ctx, _, admin_role, "renew")


@router.callback_query(UserCb.filter(F.a == "comp"), HasPermission(Permission.USERS_MONEY))
async def toggle_complimentary(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    await _toggle(callback, callback_data, ctx, _, admin_role, "comp")


async def _toggle(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, role: AdminRole, what: str
) -> None:
    uid = callback_data.uid
    try:
        async with ctx.session_factory.begin() as session:
            sub = await session.scalar(select(Subscription).where(Subscription.user_id == uid))
            if sub is None:
                raise ops.UserOpError("no_subscription")
            if what == "renew":
                await ops.set_auto_renew(session, uid, not sub.auto_renew, callback.from_user.id)
            else:
                await ops.set_complimentary(session, uid, not sub.is_complimentary, callback.from_user.id)
    except ops.UserOpError as exc:
        await render(callback, await card_screen(ctx, _, role, uid, note=_(f"admin_users.error.{exc.code}")))
        return
    await render(callback, await card_screen(ctx, _, role, uid), answer=_("admin.saved"))


@router.callback_query(UserCb.filter(F.a.in_({"block", "unblock"})), HasPermission(Permission.USERS_EDIT))
async def block_subscription(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    uid = callback_data.uid
    target = SubscriptionStatus.DISABLED if callback_data.a == "block" else SubscriptionStatus.ACTIVE
    async with ctx.session_factory.begin() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.user_id == uid).with_for_update())
        if sub is not None:
            await subscriptions.set_status(session, sub, target, callback.from_user.id)
    if sub is None:
        note = _("admin_users.error.no_subscription")
    else:
        await answer_callback(callback, _("admin.working"))
        note = await _sync_note(ctx, _, uid)
    await render(callback, await card_screen(ctx, _, admin_role, uid, note=note))


# --- confirmed actions: devices, link, ban, delete -------------------------------------------------------------------

CONFIRMS: dict[str, tuple[str, Permission, bool]] = {
    # action: (text key, permission, dangerous)
    "devices": ("admin_users.confirm.reset_devices", Permission.USERS_EDIT, False),
    "rotate": ("admin_users.confirm.rotate", Permission.USERS_EDIT, True),
    "ban": ("admin_users.confirm.ban", Permission.USERS_EDIT, True),
    "del": ("admin_users.confirm.delete", Permission.USERS_MONEY, True),
}


@router.callback_query(UserCb.filter(F.a.in_(set(CONFIRMS))), HasPermission(Permission.USERS_EDIT))
async def ask_confirm(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    key, permission, danger = CONFIRMS[callback_data.a]
    if not has_permission(admin_role, permission):
        await answer_callback(callback, _("error.not_allowed"), alert=True)
        return
    uid = callback_data.uid
    async with read_session(ctx.session_factory) as session, session.begin():
        user = await session.get(User, uid)
    if user is None:
        await render(callback, search_screen(_, _("admin_users.error.not_found")))
        return
    yes = UserCb(a=f"{callback_data.a}_ok", uid=uid)
    await render(
        callback, Screen(_(key, user=user_title(user)), confirm_kb(_, yes, UserCb(a="card", uid=uid), danger=danger))
    )


@router.callback_query(UserCb.filter(F.a == "devices_ok"), HasPermission(Permission.USERS_EDIT))
async def reset_devices(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    uid = callback_data.uid
    async with read_session(ctx.session_factory) as session, session.begin():
        sub_id = await session.scalar(select(Subscription.id).where(Subscription.user_id == uid))
    if sub_id is None:
        note = _("admin_users.error.no_subscription")
    else:
        await answer_callback(callback, _("admin.working"))
        result = await devices.reset(ctx.session_factory, ctx.panels, sub_id, actor_id=callback.from_user.id)
        if result.failed == 0:
            note = _("admin_users.done.devices", count=result.reset)
        else:
            note = _("admin_users.done.devices_partial", count=result.reset, failed=result.failed)
    await render(callback, await card_screen(ctx, _, admin_role, uid, note=note))


@router.callback_query(UserCb.filter(F.a == "rotate_ok"), HasPermission(Permission.USERS_EDIT))
async def rotate_link(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    uid = callback_data.uid
    async with ctx.session_factory.begin() as session:
        user = await session.get(User, uid)
        if user is not None:
            await users.rotate_sub_token(session, user)
            await _audit(session, callback.from_user.id, "user.rotate_sub_token", uid)
    note = _("admin_users.done.rotated") if user is not None else _("admin_users.error.not_found")
    await render(callback, await card_screen(ctx, _, admin_role, uid, note=note))


@router.callback_query(UserCb.filter(F.a == "sync"), HasPermission(Permission.USERS_EDIT))
async def sync_now(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    await answer_callback(callback, _("admin.working"))
    note = await _sync_note(ctx, _, callback_data.uid)
    await render(callback, await card_screen(ctx, _, admin_role, callback_data.uid, note=note))


@router.callback_query(UserCb.filter(F.a.in_({"ban_ok", "unban"})), HasPermission(Permission.USERS_EDIT))
async def ban_user(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    uid = callback_data.uid
    banned = callback_data.a == "ban_ok"
    try:
        async with ctx.session_factory.begin() as session:
            await ops.set_banned(session, ctx.settings, uid, banned, callback.from_user.id)
    except ops.UserOpError as exc:
        await render(callback, await card_screen(ctx, _, admin_role, uid, note=_(f"admin_users.error.{exc.code}")))
        return
    await answer_callback(callback, _("admin.working"))
    await _sync_note(ctx, _, uid)
    note = _("admin_users.done.banned" if banned else "admin_users.done.unbanned")
    await render(callback, await card_screen(ctx, _, admin_role, uid, note=note))


@router.callback_query(
    UserCb.filter(F.a == "del_ok"), HasPermission(Permission.USERS_EDIT), HasPermission(Permission.USERS_MONEY)
)
async def delete_user(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    uid = callback_data.uid
    await answer_callback(callback, _("admin.working"))
    try:
        result = await ops.delete_user(ctx.session_factory, ctx.panels, ctx.settings, uid, callback.from_user.id)
    except ops.UserOpError as exc:
        await render(callback, await card_screen(ctx, _, admin_role, uid, note=_(f"admin_users.error.{exc.code}")))
        return
    if result.errors:
        note = _("admin_users.done.deleted_partial", user_id=uid, removed=result.removed, failed=len(result.errors))
    else:
        note = _("admin_users.done.deleted", user_id=uid, removed=result.removed)
    await render(callback, search_screen(_, note))


# --- message the user ------------------------------------------------------------------------------------------------


@router.callback_query(UserCb.filter(F.a == "msg"), HasPermission(Permission.USERS_EDIT))
async def ask_message(callback: CallbackQuery, callback_data: UserCb, _: Translator, state: FSMContext) -> None:
    await state.set_state(UserInput.message)
    await state.set_data({"uid": callback_data.uid})
    await render(callback, Screen(_("admin_users.ask.msg"), kb([[card_button(_, callback_data.uid)]])))


@router.message(UserInput.message, HasPermission(Permission.USERS_EDIT))
async def message_input(message: Message, _: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    uid = int(data.get("uid", 0))
    if not uid:
        return
    yes = UserCb(a="msg_ok", uid=uid, arg=str(message.message_id))
    await message.reply(
        _("admin_users.confirm.message", user_id=uid),
        reply_markup=confirm_kb(_, yes, UserCb(a="card", uid=uid)),
    )


@router.callback_query(UserCb.filter(F.a == "msg_ok"), HasPermission(Permission.USERS_EDIT))
async def send_message(
    callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator, admin_role: AdminRole
) -> None:
    uid = callback_data.uid
    message_id = parse_int(callback_data.arg)
    if message_id is None or callback.message is None:
        await answer_callback(callback, _("error.expired_button"))
        return
    if not first_time(f"msg:{callback.message.chat.id}:{message_id}:{uid}"):
        await answer_callback(callback)
        return
    try:
        await ctx.bot.copy_message(chat_id=uid, from_chat_id=callback.message.chat.id, message_id=message_id)
    except (TelegramForbiddenError, TelegramNotFound):
        await mark_blocked(ctx.session_factory, uid)
        note = _("admin_users.error.blocked_bot")
    except TelegramBadRequest as exc:
        log.info("admin message to %s failed: %s", uid, exc)
        note = _("admin_users.error.send_failed", error=_.esc(str(exc)[:200]))
    else:
        async with ctx.session_factory.begin() as session:
            await _audit(session, callback.from_user.id, "user.message", uid, message_id=message_id)
        note = _("admin_users.done.message")
    await render(callback, await card_screen(ctx, _, admin_role, uid, note=note))


# --- history ---------------------------------------------------------------------------------------------------------

HISTORY_ROWS = 10


@router.callback_query(UserCb.filter(F.a == "hist"), HasPermission(Permission.USERS_VIEW))
async def history(callback: CallbackQuery, callback_data: UserCb, ctx: AppContext, _: Translator) -> None:
    uid = callback_data.uid
    async with read_session(ctx.session_factory) as session, session.begin():
        payments = list(
            (
                await session.scalars(
                    select(Payment)
                    .where(Payment.user_id == uid)
                    .order_by(Payment.created_at.desc())
                    .limit(HISTORY_ROWS)
                )
            ).all()
        )
        ledger = await wallet.history(session, uid, limit=HISTORY_ROWS)
    lines = [_("admin_users.history.payments")]
    if payments:
        for payment in payments:
            lines.append(
                _(
                    "admin_users.history.payment_row",
                    date=_.datetime(payment.created_at),
                    amount=_.money(payment.amount_kop),
                    provider=_.esc(payment.provider),
                    status=_(f"admin.payment_status.{payment.status.value}"),
                )
            )
    else:
        lines.append(_("admin_users.history.none"))
    lines += ["", _("admin_users.history.ledger")]
    if ledger:
        for tx in ledger:
            account = _("admin_users.history.partner_mark") if tx.account == LedgerAccount.REFERRAL else ""
            lines.append(
                _(
                    "admin_users.history.ledger_row",
                    date=_.datetime(tx.created_at),
                    amount=money_signed(_, tx.amount_kop),
                    kind=_(f"admin.tx.{tx.kind.value}"),
                    account=account,
                )
            )
    else:
        lines.append(_("admin_users.history.none"))
    await render(callback, Screen("\n".join(lines), kb([[card_button(_, uid)]])))
