"""/start with deep links, the main menu, language, profile, help and the legal commands."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import Screen, answer_callback, render
from app.db.models import AdminRole, User
from app.keyboards import MENU, NOOP, Button, Nav, back, kb, nav, to_menu
from app.keyboards.common import language_kb
from app.modules.core_user import start as deeplinks
from app.modules.core_user.menu import (
    CUSTOM_SCREEN,
    HELP,
    LANG,
    PROFILE,
    TG_PROXY,
    feature_on,
    main_menu,
    name_on,
    support_button,
)
from app.modules.subscription import bot as subscription_bot
from app.modules.subscription import checkout
from app.modules.subscription.bot import sync_now
from app.modules.subscription.callbacks import CONNECT
from app.modules.subscription.state import Status, SubState, load_state
from app.modules.subscription.views import Rows
from app.services import imports, menu_buttons, notifications, users
from app.services.panels.base import PanelError
from app.utils.time import utcnow

log = logging.getLogger(__name__)

router = Router(name="core_user")
# User screens live in the private chat only: the access gates do not look at group chats.
router.message.filter(IsPrivate())
router.callback_query.filter(IsPrivate())

Event = Message | CallbackQuery
INVITES = "invites"


async def show_menu(
    event: Event,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    admin_role: AdminRole | None,
    *,
    header: str | None = None,
    answer: str | None = None,
) -> None:
    if event.from_user is None:
        return
    state = await load_state(ctx.session_factory, event.from_user.id)
    if state is None:
        await render(event, Screen(_("error.unavailable")), answer=answer)
        return
    custom = await _custom_buttons(ctx)
    screen = main_menu(_, settings, state, ctx.modules, is_admin=admin_role is not None, header=header, custom=custom)
    await render(event, screen, answer=answer)


async def _custom_buttons(ctx: AppContext) -> tuple[menu_buttons.MenuItem, ...]:
    """The owner's custom menu buttons; the menu still opens if they cannot be read."""
    try:
        return await menu_buttons.cache_for(ctx.session_factory).items()
    except Exception:
        log.exception("could not load custom menu buttons")
        return ()


def welcome(_: Translator, settings: Settings, state: SubState, name: str | None) -> str:
    key = "core_user.welcome" if settings.app_mode == "shop" else "core_user.welcome_personal"
    text = _(key, name=_.esc(name or _("core_user.friend")), brand=_.esc(settings.brand_name))
    if state.trial_available(settings):
        text += "\n\n" + _("core_user.welcome_trial", trial=_.duration(settings.trial_days))
    return text


# --- /start -----------------------------------------------------------------------------------------------------------
@router.message(CommandStart())
async def start(
    message: Message,
    command: CommandObject,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    state: FSMContext,
    db_user: User | None = None,
    user_created: bool = False,
    admin_role: AdminRole | None = None,
) -> None:
    await state.clear()
    if message.from_user is None or db_user is None:
        await message.answer(_("error.unavailable"))
        return
    user_id = message.from_user.id
    payload = deeplinks.parse(command.args)
    fresh = deeplinks.is_fresh(db_user, user_created, utcnow())
    result = await deeplinks.apply_registration(ctx, settings, _, user_id, payload, fresh=fresh)
    if payload.kind in (deeplinks.Kind.GIFT, deeplinks.Kind.PROMO):
        if await _feature_open(ctx, settings, "promo", user_id) or await _feature_open(ctx, settings, "gifts", user_id):
            redeemed = await deeplinks.redeem_code(ctx, settings, _, user_id, payload.value)
            result.notices += redeemed.notices
            result.sync_sub_id = redeemed.sync_sub_id or result.sync_sub_id
    elif payload.kind == deeplinks.Kind.INVITE and await _invites_on(ctx, settings, user_id):
        redeemed = await deeplinks.redeem_invite(ctx, settings, _, user_id, payload.value)
        result.notices += redeemed.notices
        result.sync_sub_id = redeemed.sync_sub_id or result.sync_sub_id
    elif payload.kind == deeplinks.Kind.IMPORT:
        try:
            result.sync_sub_id = await imports.claim(
                ctx.session_factory, settings, ctx.secret_box, user_id, payload.value, registry=ctx.panels
            )
        except imports.ImportError:
            result.notices.append(_("core_user.import.unavailable"))
        except PanelError:
            result.notices.append(_("core_user.import.retry_later"))
        else:
            result.notices.append(_("core_user.import.claimed"))
    if result.sync_sub_id is not None:
        await sync_now(ctx, settings, result.sync_sub_id)
    if user_created:
        _notify_new_user(ctx, settings, message, result)

    header = "\n\n".join(result.notices) or None
    if await _open_target(message, ctx, settings, _, payload, header):
        return
    if fresh and header is None:
        loaded = await load_state(ctx.session_factory, user_id)
        if loaded is not None:
            header = welcome(_, settings, loaded, message.from_user.first_name)
    await show_menu(message, ctx, settings, _, admin_role, header=header)


async def _feature_open(ctx: AppContext, settings: Settings, name: str, user_id: int) -> bool:
    """The feature is on and not under a partial maintenance for this user."""
    if not feature_on(ctx.modules, settings, name):
        return False
    return not await checkout.under_maintenance(ctx, settings, name, user_id)


async def _invites_on(ctx: AppContext, settings: Settings, user_id: int) -> bool:
    """Invitation links are redeemed only while the invitations module is on (or not installed and not switched off)
    and not under a partial maintenance."""
    if INVITES in ctx.modules:
        enabled = name_on(ctx.modules, settings, INVITES)
    else:
        enabled = INVITES not in settings.disabled_modules
    return enabled and not await checkout.under_maintenance(ctx, settings, INVITES, user_id)


async def _open_target(
    message: Message,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    payload: deeplinks.Payload,
    header: str | None,
) -> bool:
    """Open the screen a link points to (after its notices); False if the link has none."""
    kind = payload.kind
    if kind not in (deeplinks.Kind.PLAN, deeplinks.Kind.BUY, deeplinks.Kind.CONNECT, deeplinks.Kind.SUB):
        user_id = message.from_user.id if message.from_user else None
        if (
            kind == deeplinks.Kind.PROXY
            and name_on(ctx.modules, settings, TG_PROXY)
            and not await checkout.under_maintenance(ctx, settings, TG_PROXY, user_id)
        ):
            screen = Screen(
                _("core_user.proxy.text"),
                kb([[nav(_("core_user.proxy.open"), TG_PROXY)], [to_menu(_)]]),
            )
            if header:
                await message.answer(header)
            await render(message, screen)
            return True
        return False
    if header:
        await message.answer(header)
    if kind == deeplinks.Kind.PLAN and payload.plan_id is not None:
        await checkout.show_plan(message, ctx, settings, _, payload.plan_id, payload.plan_token)
    elif kind == deeplinks.Kind.BUY or kind == deeplinks.Kind.PLAN:
        await checkout.show_plans(message, ctx, settings, _)
    elif kind == deeplinks.Kind.CONNECT:
        await subscription_bot.show_connect(message, ctx, settings, _)
    else:
        await subscription_bot.show_card(message, ctx, settings, _)
    return True


def _notify_new_user(ctx: AppContext, settings: Settings, message: Message, result: deeplinks.StartResult) -> None:
    tg_user = message.from_user
    if tg_user is None:
        return
    admin = ctx.i18n.translator(ctx.i18n.default_lang, settings.zone)
    who = f"@{tg_user.username}" if tg_user.username else admin.esc(tg_user.full_name)
    lines = [admin("core_user.admin.new_user", user=admin.esc(who), user_id=tg_user.id)]
    referral = result.referral
    if referral is not None and referral.inviter_id is not None:
        lines.append(admin("core_user.admin.new_user_ref", inviter_id=referral.inviter_id))
    if result.campaign:
        lines.append(admin("core_user.admin.new_user_campaign", campaign=admin.esc(result.campaign)))
    notifications.notify(ctx, notifications.AdminEvent.USERS, "\n".join(lines))


# --- main menu --------------------------------------------------------------------------------------------------------
@router.message(Command("menu"))
@router.callback_query(Nav.filter(F.to == MENU))
async def open_menu(
    event: Event,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    state: FSMContext,
    admin_role: AdminRole | None = None,
) -> None:
    await state.clear()
    await show_menu(event, ctx, settings, _, admin_role)


@router.callback_query(Nav.filter(F.to == CUSTOM_SCREEN))
async def custom_screen(
    callback: CallbackQuery,
    callback_data: Nav,
    ctx: AppContext,
    _: Translator,
    admin_role: AdminRole | None = None,
) -> None:
    """Text screen of a custom menu button (only for users the button is shown to)."""
    item = None
    if callback_data.arg.isdigit():
        button_id = int(callback_data.arg)
        item = next((i for i in await _custom_buttons(ctx) if i.id == button_id), None)
    if item is not None and item.kind == menu_buttons.KIND_SCREEN:
        state = await load_state(ctx.session_factory, callback.from_user.id)
        subscribed = state is not None and state.status == Status.ACTIVE
        if item.visible_to(subscribed=subscribed, is_admin=admin_role is not None):
            await render(callback, Screen(item.payload, kb([[back(MENU, _)]])))
            return
    await answer_callback(callback, _("core_user.menu.button_gone"), alert=True)


@router.callback_query(Nav.filter(F.to == NOOP))
async def noop(callback: CallbackQuery) -> None:
    await answer_callback(callback)


# --- language ---------------------------------------------------------------------------------------------------------
@router.message(Command("language", "lang"))
@router.callback_query(Nav.filter(F.to == LANG))
async def choose_language(
    event: Event,
    ctx: AppContext,
    settings: Settings,
    _: Translator,
    lang: str,
    callback_data: Nav | None = None,
    admin_role: AdminRole | None = None,
) -> None:
    code = callback_data.arg if callback_data is not None else ""
    offered = [c for c in users.enabled_languages(settings) if c in ctx.i18n.languages]
    if not code or code not in offered or event.from_user is None:
        await render(event, Screen(_("lang.title"), language_kb(_, offered, current=lang)))
        return
    async with ctx.session_factory.begin() as session:
        user = await users.get(session, event.from_user.id)
        if user is not None:
            await users.set_language(session, user, code)
    new = ctx.i18n.translator(code, settings.zone)
    await show_menu(event, ctx, settings, new, admin_role, answer=new("lang.changed"))


# --- profile ----------------------------------------------------------------------------------------------------------
@router.message(Command("profile"))
@router.callback_query(Nav.filter(F.to == PROFILE))
async def profile(event: Event, ctx: AppContext, settings: Settings, _: Translator) -> None:
    if event.from_user is None:
        return
    state = await load_state(ctx.session_factory, event.from_user.id)
    if state is None:
        await render(event, Screen(_("error.unavailable")))
        return
    user = state.user
    lines = [
        _("core_user.profile.title"),
        "",
        _("core_user.profile.id", id=user.tg_id),
        _("core_user.profile.name", name=_.esc(user.first_name or "-")),
        _("core_user.profile.language", language=_(f"lang.{user.language}")),
        _("core_user.profile.since", date=_.date(user.created_at)),
    ]
    if settings.app_mode == "shop":
        lines.append(_("core_user.profile.balance", balance=_.money(user.balance_kop)))
        if user.referral_balance_kop > 0 or settings.referral_reward_mode == "partner":
            lines.append(_("core_user.profile.partner_balance", balance=_.money(user.referral_balance_kop)))
    rows = [
        [nav(_("menu.my_subscription"), "sub"), nav(_("menu.language"), LANG)],
        [to_menu(_)],
    ]
    if settings.cabinet_enabled and settings.cabinet_login_bot and name_on(ctx.modules, settings, "web_cabinet"):
        rows.insert(1, [nav(_("web_cabinet.button"), "cabinet")])
    await render(event, Screen("\n".join(lines), kb(rows)))


# --- help and legal ---------------------------------------------------------------------------------------------------
@router.message(Command("help"))
@router.callback_query(Nav.filter(F.to == HELP))
async def help_screen(event: Event, ctx: AppContext, settings: Settings, _: Translator) -> None:
    key = "core_user.help.text" if settings.app_mode == "shop" else "core_user.help.text_personal"
    rows: Rows = [
        [nav(_("menu.connect"), CONNECT)],
        [support_button(_, ctx.modules, settings)],
        [
            Button.url(_("consent.terms"), settings.terms_url) if settings.terms_url else None,
            Button.url(_("consent.privacy"), settings.privacy_url) if settings.privacy_url else None,
        ],
        [to_menu(_)],
    ]
    await render(event, Screen(_(key, brand=_.esc(settings.brand_name)), kb(rows)))


@router.message(Command("terms"))
async def terms(message: Message, settings: Settings, _: Translator) -> None:
    await _legal(message, _, settings.terms_url, "core_user.legal.terms", _("consent.terms"))


@router.message(Command("privacy"))
async def privacy(message: Message, settings: Settings, _: Translator) -> None:
    await _legal(message, _, settings.privacy_url, "core_user.legal.privacy", _("consent.privacy"))


async def _legal(message: Message, _: Translator, url: str | None, key: str, button: str) -> None:
    if url:
        await render(message, Screen(_(key), kb([[Button.url(button, url)], [to_menu(_)]])))
    else:
        await render(message, Screen(_("core_user.legal.missing"), kb([[to_menu(_)]])))


@router.message(Command("paysupport"))
async def paysupport(message: Message, ctx: AppContext, settings: Settings, _: Translator) -> None:
    support = support_button(_, ctx.modules, settings)
    rows: Rows = [[support], [to_menu(_)]]
    text = _("core_user.paysupport.text" if support is not None else "core_user.paysupport.no_contact")
    await render(message, Screen(text, kb(rows)))
