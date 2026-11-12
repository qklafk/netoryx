"""``/start inv_<code>``: redeem an invitation, push the new access to the servers and greet the newcomer.

:func:`redeem_invite` is also usable from other ``/start`` handlers (it returns the outcome and renders nothing).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.core.context import AppContext
from app.core.filters import IsPrivate
from app.core.i18n import Translator
from app.core.ui import Screen, render
from app.db.models import User
from app.db.session import read_session
from app.keyboards import Button, Nav, kb, to_menu
from app.modules.admin.common import user_short
from app.services import invites, provisioning
from app.services.invites import InviteOutcome, RedeemResult
from app.services.notify import safe_send

log = logging.getLogger(__name__)

CONNECT_NAV = "connect"  # the connection screen of the subscription module

router = Router(name="invites_redeem")
router.message.filter(IsPrivate())


@dataclass(frozen=True, slots=True)
class Redeemed:
    result: RedeemResult
    unlimited: bool = False


async def redeem_invite(ctx: AppContext, user_id: int, code: str) -> Redeemed:
    """Redeem ``code`` for the user (short transaction), then push the subscription and tell the inviter."""
    settings = ctx.settings
    async with ctx.session_factory.begin() as session:
        user = await session.get(User, user_id)
        if user is None:
            return Redeemed(RedeemResult(InviteOutcome.NOT_FOUND))
        result = await invites.redeem(session, settings, user, code)
        sub_id = result.subscription.id if result.ok and result.subscription is not None else None
        unlimited = bool(result.subscription is not None and invites.is_unlimited(result.subscription))
    if sub_id is None:
        return Redeemed(result, unlimited)
    try:
        await provisioning.sync_subscription(ctx.session_factory, ctx.panels, settings, sub_id)
    except Exception:  # the reconciler pushes it shortly anyway
        log.exception("invite: pushing subscription %s failed", sub_id)
    if result.invite is not None and result.invite.created_by != user_id:
        await _tell_inviter(ctx, result, user_id)
    return Redeemed(result, unlimited)


async def _tell_inviter(ctx: AppContext, result: RedeemResult, user_id: int) -> None:
    invite = result.invite
    assert invite is not None
    async with read_session(ctx.session_factory) as session:
        inviter = await session.get(User, invite.created_by)
        newcomer = await session.get(User, user_id)
    if inviter is None or newcomer is None or inviter.bot_blocked:
        return
    lang = ctx.i18n.normalize(inviter.language)
    key = "invites.joined_note" if invite.note else "invites.joined"
    text = ctx.i18n.t(key, lang, name=ctx.i18n.esc(user_short(newcomer)), note=ctx.i18n.esc(invite.note or ""))
    await safe_send(ctx.bot, inviter.tg_id, text, session_factory=ctx.session_factory)


def outcome_screen(_: Translator, redeemed: Redeemed) -> Screen:
    result = redeemed.result
    if result.ok and result.subscription is not None:
        expires = result.subscription.expires_at
        until = _("invites.until_forever") if redeemed.unlimited else _("invites.until_date", date=_.date(expires))
        text = _("invites.welcome", until=until)
        return Screen(
            text, kb([[Button.cb(_("invites.btn.connect"), Nav(to=CONNECT_NAV), style="success")], [to_menu(_)]])
        )
    return Screen(_(f"invites.outcome.{result.outcome.value}"), kb([[to_menu(_)]]))


@router.message(CommandStart(deep_link=True))
async def start_with_invite(
    message: Message, command: CommandObject, ctx: AppContext, db_user: User | None, _: Translator
) -> None:
    payload = command.args or ""
    if db_user is None or not payload.lower().startswith(invites.DEEP_LINK_PREFIX):
        raise SkipHandler
    redeemed = await redeem_invite(ctx, db_user.tg_id, payload)
    await render(message, outcome_screen(_, redeemed))
