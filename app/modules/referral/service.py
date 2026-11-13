"""Referral program data for the screens: the user's link, program terms per reward mode and partner notices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db.models import LedgerAccount, User, Withdrawal
from app.db.session import read_session
from app.services import notifications, partner, wallet
from app.services.notify import safe_send

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.context import AppContext
    from app.core.i18n import Translator


@dataclass(frozen=True, slots=True)
class Overview:
    code: str
    link: str
    main_kop: int
    stats: partner.PartnerStats
    open_withdrawal: Withdrawal | None
    has_withdrawals: bool


def referral_link(bot_username: str | None, code: str) -> str:
    """``https://t.me/<bot>?start=ref_<code>`` (the bare code if the bot username is unknown)."""
    return f"https://t.me/{bot_username}?start=ref_{code}" if bot_username else code


async def overview(ctx: AppContext, user_id: int) -> Overview | None:
    async with read_session(ctx.session_factory) as session:
        user = await session.get(User, user_id)
        if user is None:
            return None
        stats = await partner.partner_stats(session, ctx.settings, user_id)
        open_request = await partner.open_withdrawal(session, user_id)
        has_any = open_request is not None or bool(await partner.list_for_user(session, user_id, limit=1))
        return Overview(
            code=user.referral_code,
            link=referral_link(ctx.bot_username, user.referral_code),
            main_kop=user.balance_kop,
            stats=stats,
            open_withdrawal=open_request,
            has_withdrawals=has_any,
        )


async def balances(ctx: AppContext, user_id: int) -> tuple[int, int]:
    """``(main_kop, partner_kop)``."""
    async with read_session(ctx.session_factory) as session:
        main = await wallet.balance(session, user_id)
        referral = await wallet.balance(session, user_id, LedgerAccount.REFERRAL)
    return main, referral


def levels_text(_: Translator, percents: list[int]) -> str:
    """``1st level — 10%, 2nd level — 5%``."""
    return ", ".join(_("referral.level_pct", level=i, pct=pct) for i, pct in enumerate(percents, start=1) if pct > 0)


def terms(_: Translator, settings: Settings, *, withdrawals: bool) -> list[str]:
    """The program's rules for the current reward mode, one line each."""
    mode = settings.referral_reward_mode
    percents = [p for p in settings.referral_levels_percent if p > 0]
    after = _("referral.terms.after_payment") if settings.referral_inviter_requires_payment else ""
    lines: list[str] = []
    if mode == "days":
        if settings.referral_invitee_bonus_days > 0:
            lines.append(_("referral.terms.invitee_days", days=_.duration(settings.referral_invitee_bonus_days)))
        if settings.referral_inviter_bonus_days > 0:
            lines.append(
                _("referral.terms.inviter_days", days=_.duration(settings.referral_inviter_bonus_days), after=after)
            )
    else:
        if settings.referral_invitee_bonus_kop > 0:
            key = "invitee_money_paid" if settings.referral_invitee_requires_payment else "invitee_money"
            lines.append(_(f"referral.terms.{key}", amount=_.money(settings.referral_invitee_bonus_kop)))
        if settings.referral_inviter_bonus_kop > 0:
            lines.append(
                _("referral.terms.inviter_money", amount=_.money(settings.referral_inviter_bonus_kop), after=after)
            )
    if percents:
        single = len(settings.referral_levels_percent) == 1
        if single:
            lines.append(_(f"referral.terms.{mode}_pct", pct=percents[0]))
        else:
            lines.append(_(f"referral.terms.{mode}_levels", levels=levels_text(_, settings.referral_levels_percent)))
    if mode == "partner":
        key = "referral.terms.partner_withdraw" if withdrawals else "referral.terms.partner_transfer"
        lines.append(_(key, min=_.money(settings.min_withdrawal_kop)))
    return lines


async def notify_withdrawal_requested(ctx: AppContext, withdrawal: Withdrawal, username: str | None) -> None:
    """Tell the admins about a new payout request (withdrawals topic)."""
    i18n = ctx.i18n
    lang = i18n.default_lang
    text = i18n.t(
        "referral.admin.withdrawal",
        lang,
        id=withdrawal.id,
        user_id=withdrawal.user_id,
        username=i18n.esc(f"@{username}" if username else "-"),
        amount=i18n.money(withdrawal.amount_kop, lang),
        method=i18n.esc(withdrawal.method),
        details=i18n.esc(withdrawal.details),
    )
    notifications.notify(ctx, notifications.AdminEvent.WITHDRAWALS, text)


async def notify_withdrawal_update(ctx: AppContext, withdrawal: Withdrawal) -> bool:
    """Tell the partner that an admin approved, paid or rejected their payout request; True if delivered."""
    async with read_session(ctx.session_factory) as session:
        lang = await session.scalar(select(User.language).where(User.tg_id == withdrawal.user_id))
    if lang is None:
        return False
    _ = ctx.i18n.translator(lang, ctx.settings.zone)
    text = _(
        f"referral.withdrawal.notify.{withdrawal.status.value}",
        id=withdrawal.id,
        amount=_.money(withdrawal.amount_kop),
    )
    if withdrawal.admin_comment:
        text += "\n\n" + _("referral.withdrawal.notify.comment", comment=_.esc(withdrawal.admin_comment))
    return await safe_send(ctx.bot, withdrawal.user_id, text, session_factory=ctx.session_factory)
