"""Redeeming promo and gift codes from the bot, the Mini App and ``/start promo_<code>`` / ``gift_<code>`` links.

One transaction redeems the code (money bonus on the wallet) and adds its days to the subscription (a gift applies
its plan's limits); the subscription is pushed to the panels afterwards, and the gift's buyer gets a thank-you.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db.models import Plan, PromoKind, PurchaseKind, Subscription, User
from app.db.session import read_session
from app.services import promo, provisioning, subscriptions, wallet
from app.services.notify import safe_send
from app.services.promo import PromoOutcome

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

MAX_CODE_LENGTH = 64

__all__ = ["PromoOutcome", "RedeemResult", "redeem_code"]


@dataclass(frozen=True, slots=True)
class RedeemResult:
    outcome: PromoOutcome
    is_gift: bool = False
    bonus_kop: int = 0
    days: int = 0
    plan_title: str | None = None
    expires_at: datetime | None = None
    balance_kop: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome is PromoOutcome.OK


def clean_code(text: str | None) -> str:
    """The code part of user input (``/promo CODE``, a pasted ``promo_`` / ``gift_`` link payload, extra spaces)."""
    words = (text or "").split()
    value = words[0] if words else ""
    for prefix in ("promo_", "gift_"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
            break
    return value[:MAX_CODE_LENGTH]


async def redeem_code(ctx: AppContext, user_id: int, code: str) -> RedeemResult:
    """Redeem ``code`` for the user; see the module docstring. Never raises for business refusals."""
    settings = ctx.settings
    code = clean_code(code)
    if not code:
        return RedeemResult(PromoOutcome.NOT_FOUND)
    buyer_id: int | None = None
    sub_id: int | None = None
    async with ctx.session_factory.begin() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedeemResult(PromoOutcome.NOT_FOUND)
        outcome, row = await promo.redeem(session, settings, user, code)
        if outcome is not PromoOutcome.OK or row is None:
            return RedeemResult(outcome)
        is_gift = row.kind is PromoKind.GIFT
        plan = await session.get(Plan, row.plan_id) if row.plan_id is not None else None
        expires_at = None
        if row.bonus_days > 0:
            purchase = await subscriptions.grant_days(
                session,
                settings,
                user,
                row.bonus_days,
                kind=PurchaseKind.GIFT_REDEEM if is_gift else PurchaseKind.PROMO,
                plan=plan,
            )
            expires_at = purchase.expires_after
            sub_id = await session.scalar(select(Subscription.id).where(Subscription.user_id == user_id))
        balance = await wallet.balance(session, user_id)
        if is_gift and row.created_by is not None and row.created_by != user_id:
            buyer_id = row.created_by
        result = RedeemResult(
            PromoOutcome.OK,
            is_gift=is_gift,
            bonus_kop=row.bonus_kop,
            days=row.bonus_days,
            plan_title=plan.title if plan is not None else None,
            expires_at=expires_at,
            balance_kop=balance,
        )
    if sub_id is not None:
        try:
            await provisioning.sync_subscription(ctx.session_factory, ctx.panels, settings, sub_id)
        except Exception:
            log.exception("promo: syncing subscription %s failed; the reconciler will retry", sub_id)
    if buyer_id is not None:
        await _thank_buyer(ctx, buyer_id, result.plan_title or "")
    return result


async def _thank_buyer(ctx: AppContext, buyer_id: int, plan_title: str) -> None:
    async with read_session(ctx.session_factory) as session:
        lang = await session.scalar(select(User.language).where(User.tg_id == buyer_id))
    if lang is None:
        return
    _ = ctx.i18n.translator(lang, ctx.settings.zone)
    text = _("promo.gift_redeemed_buyer", plan=_.esc(plan_title), brand=_.esc(ctx.settings.brand_name))
    await safe_send(ctx.bot, buyer_id, text, session_factory=ctx.session_factory)
