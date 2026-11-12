"""Telegram UI for wallet-funded subscription extras."""

from __future__ import annotations

import secrets

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.config import Settings
from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import answer_callback, render
from app.keyboards import Nav
from app.modules.subscription import views
from app.modules.subscription.bot import alert, require_state, show_card, sync_now
from app.modules.subscription.callbacks import ADDONS, AddonCb
from app.services import addons, users

router = Router(name="subscription_addons")
router.callback_query.filter(IsPrivate())


def _nonce() -> str:
    return secrets.token_urlsafe(6)


@router.callback_query(Nav.filter(F.to == ADDONS))
async def show_addons(
    callback: CallbackQuery, callback_data: Nav, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    sub_id = int(callback_data.arg) if callback_data.arg.isdecimal() else 0
    state = await require_state(callback, ctx, _, sub_id or None)
    if state is None or state.sub is None:
        return
    if settings.app_mode != "shop":
        await alert(callback, _("subscription.addons.unavailable"))
        return
    await render(callback, views.addons_screen(_, state, addons.offers_for(settings, state.sub), _nonce()))


@router.callback_query(AddonCb.filter())
async def buy_addon(
    callback: CallbackQuery, callback_data: AddonCb, ctx: AppContext, settings: Settings, _: Translator
) -> None:
    try:
        kind = addons.AddonKind(callback_data.k)
    except ValueError:
        await answer_callback(callback, _("error.expired_button"), alert=True)
        return
    try:
        async with ctx.session_factory.begin() as session:
            user = await users.get(session, callback.from_user.id)
            if user is None:
                raise addons.AddonError("subscription_not_found")
            result = await addons.purchase(
                session,
                settings,
                user,
                callback_data.s,
                kind,
                ref=f"tap-addon:{callback_data.n or _nonce()}",
            )
    except addons.AddonError as exc:
        key = (
            "subscription.addons.insufficient"
            if str(exc) == "insufficient_funds"
            else "subscription.addons.unavailable"
        )
        await alert(callback, _(key))
        await show_addons(callback, Nav(to=ADDONS, arg=str(callback_data.s)), ctx, settings, _)
        return
    if result is None:
        await answer_callback(callback, _("subscription.addons.done"))
        return
    await sync_now(ctx, settings, callback_data.s)
    await show_card(callback, ctx, settings, _, header=_("subscription.addons.done"), subscription_id=callback_data.s)
