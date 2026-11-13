"""What happens after a payment was credited to the wallet (:class:`~app.services.payments.service.CreditResult`).

The credit itself (wallet top-up, referral commissions and bonuses) is already committed by
:func:`app.services.payments.service.credit`. This hook then:

1. buys the plan the payment was made for from the wallet (``Payment.plan_id``), or creates the gift code
   (``Payment.is_gift``); a partial payment that does not cover the plan stays on the balance;
2. pushes the subscription to the panels right away (the reconciler retries failures);
3. tells the user (top-up / activation / gift code / donation thanks), the referral upline about their rewards and
   the admins about the payment.

It is idempotent: purchases and gifts are keyed by the payment id, and notifications for a payment are claimed once
(kv ``hook:<payment_id>``), so running it twice - a webhook racing the reconciler, or an admin re-running it for a
stuck payment - never charges or notifies twice. If a first run could not buy the plan (insufficient balance) a
later run that succeeds still tells the user about the activation. No network I/O happens inside a transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, Plan, PurchaseKind, ReferralReward, TxKind, User
from app.db.session import read_session
from app.keyboards.callbacks import MENU, Nav
from app.services import kv, notifications, promo, provisioning, referrals, subscriptions, wallet, winback
from app.services.notify import safe_send
from app.services.subscriptions import SubErrorCode, SubscriptionError
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.context import AppContext
    from app.core.i18n import I18n, Translator
    from app.services.payments.service import CreditResult

log = logging.getLogger(__name__)

NOTICE_KEY_PREFIX = "hook:"


def notice_key(payment_id: str) -> str:
    """kv key claimed by the first run of the hook for a payment."""
    return f"{NOTICE_KEY_PREFIX}{payment_id}"


@dataclass(slots=True)
class HookOutcome:
    """What the hook did (returned for tests, logs and the admin "re-run" action)."""

    payment_id: str
    first_run: bool = False  # this run claimed the payment's notifications
    purchase_id: int | None = None
    purchase_new: bool = False  # the plan was bought by this run
    subscription_id: int | None = None
    expires_at: datetime | None = None
    gift_code: str | None = None
    gift_new: bool = False
    error: SubErrorCode | None = None  # the plan could not be bought / gifted (money stays on the balance)
    synced: bool = False
    notified_users: list[int] = field(default_factory=list)


@dataclass(slots=True)
class _Reward:
    user_id: int
    level: int
    amount_kop: int = 0
    days: int = 0
    partner: bool = False


def _is_stars_renewal(result: CreditResult, payment: Payment) -> bool:
    """A recurring Telegram Stars charge after the first one (renews the subscription)."""
    return bool(result.recurring_charge_id) and payment.provider_payment_id != result.recurring_charge_id


async def on_payment_credited(ctx: AppContext, result: CreditResult) -> HookOutcome:
    """Run the post-credit actions for ``result`` (see the module docstring). Never raises for business errors."""
    settings = ctx.settings
    outcome = HookOutcome(result.payment_id)
    async with ctx.session_factory.begin() as session:
        payment = await session.get(Payment, result.payment_id)
        user = await session.get(User, result.user_id)
        if payment is None or user is None:
            log.warning("hook: payment %s or its user is gone", result.payment_id)
            return outcome
        plan = (
            await session.get(Plan, result.plan_id) if result.plan_id is not None and not result.is_donation else None
        )
        if result.plan_id is not None and plan is None and not result.is_donation:
            outcome.error = SubErrorCode.NO_PLAN
        if plan is not None and payment.saved_method_id is not None and not result.is_gift:
            await _renew_autopay(session, ctx, payment, outcome)
        elif plan is not None:
            if result.is_gift:
                await _create_gift(session, ctx, user, plan, result, outcome)
            else:
                kind = PurchaseKind.AUTO_RENEW if _is_stars_renewal(result, payment) else PurchaseKind.PURCHASE
                await _buy(session, ctx, user, plan, kind, result, outcome)
        outcome.first_run = await kv.add(session, notice_key(result.payment_id), utcnow().isoformat())
        rewards = (
            await _rewards(session, settings, result, outcome) if outcome.first_run or outcome.purchase_new else []
        )
        lang = user.language
        username = user.username
        plan_title = plan.title if plan is not None else None

    if outcome.purchase_id is not None and outcome.subscription_id is not None:
        outcome.synced = await _sync(ctx, outcome.subscription_id)

    _ = ctx.i18n.translator(lang, settings.zone)
    text = _user_text(_, result, outcome, plan_title, ctx.bot_username)
    if text is not None and await _send(ctx, result.user_id, text, _main_menu(_)):
        outcome.notified_users.append(result.user_id)
    for reward in rewards:
        if await _notify_reward(ctx, reward):
            outcome.notified_users.append(reward.user_id)
    if outcome.first_run:
        _notify_admins(ctx, result, outcome, username, plan_title)
    return outcome


async def _buy(
    session: AsyncSession,
    ctx: AppContext,
    user: User,
    plan: Plan,
    kind: PurchaseKind,
    result: CreditResult,
    outcome: HookOutcome,
) -> None:
    ref = f"purchase:{result.payment_id}"
    done_before = await wallet.has_transaction(session, user.tg_id, TxKind.PURCHASE, ref)
    payment = await session.get(Payment, result.payment_id)
    if payment is None:
        return
    # The price locked on the invoice, but never above the plan's current price, and a single-use offer only once.
    price_kop = None
    if payment.plan_price_kop is not None:
        price_kop = await winback.invoice_price(
            session, ctx.settings, user.tg_id, plan, min(payment.plan_price_kop, plan.price_kop)
        )
    try:
        async with session.begin_nested():
            purchase = await subscriptions.purchase_plan(
                session,
                ctx.settings,
                user,
                plan,
                kind=kind,
                payment_id=result.payment_id,
                subscription_id=payment.subscription_id,
                new_subscription=payment.new_subscription,
                label=payment.subscription_label,
                price_kop=price_kop,
            )
    except SubscriptionError as exc:
        outcome.error = exc.code
        log.info("hook: payment %s could not buy plan %s: %s", result.payment_id, plan.id, exc.code)
        return
    outcome.purchase_id = purchase.id
    outcome.purchase_new = not done_before
    outcome.expires_at = purchase.expires_after
    outcome.subscription_id = purchase.subscription_id
    payment = await session.get(Payment, result.payment_id)
    if payment is not None:
        payment.subscription_id = purchase.subscription_id
        payment.new_subscription = False


async def _renew_autopay(session: AsyncSession, ctx: AppContext, payment: Payment, outcome: HookOutcome) -> None:
    """A saved-card charge for an auto-renewal: renew the period it was charged for from the wallet.

    The renewal uses the period's ledger ref, so it is a no-op when the wallet auto-renewal already renewed it; the
    money then simply stays on the balance.
    """
    from app.services import saved_cards

    sub_id = payment.subscription_id
    sub = await subscriptions.get_for_user(session, payment.user_id, sub_id) if sub_id is not None else None
    if sub is None or saved_cards.autopay_payment_id(sub) != payment.id:
        return  # the period was renewed or changed meanwhile
    try:
        async with session.begin_nested():
            purchase, state = await subscriptions.renew_from_wallet(session, ctx.settings, sub.id, utcnow())
    except Exception:
        log.exception("hook: payment %s could not renew subscription %s", payment.id, sub.id)
        return
    if purchase is None:
        log.info("hook: payment %s did not renew subscription %s: %s", payment.id, sub.id, state)
        if state.value in {code.value for code in SubErrorCode}:
            outcome.error = SubErrorCode(state.value)
        return
    outcome.purchase_id = purchase.id
    outcome.purchase_new = True
    outcome.expires_at = purchase.expires_after
    outcome.subscription_id = purchase.subscription_id


async def _create_gift(
    session: AsyncSession, ctx: AppContext, user: User, plan: Plan, result: CreditResult, outcome: HookOutcome
) -> None:
    done_before = await wallet.has_transaction(session, user.tg_id, TxKind.GIFT_PURCHASE, f"gift:{result.payment_id}")
    try:
        async with session.begin_nested():
            gift = await promo.create_gift(session, ctx.settings, user, plan, result.payment_id, allow_archived=True)
    except SubscriptionError as exc:
        outcome.error = exc.code
        log.info("hook: payment %s could not buy a gift of plan %s: %s", result.payment_id, plan.id, exc.code)
        return
    outcome.gift_code = gift.code
    outcome.gift_new = not done_before


async def _rewards(
    session: AsyncSession, settings: Settings, result: CreditResult, outcome: HookOutcome
) -> list[_Reward]:
    """Referral rewards caused by this payment (and by the purchase this run made) to tell the upline about."""
    partner = settings.referral_reward_mode == "partner"
    rewards: list[_Reward] = []
    if outcome.first_run:
        if result.inviter is not None:
            inviter_id, kop = result.inviter
            if kop > 0:
                rewards.append(_Reward(inviter_id, 1, amount_kop=kop, partner=partner))
            else:  # "days" mode: the one-time inviter bonus was unlocked by this payment
                bonus = await session.scalar(
                    select(ReferralReward).where(
                        ReferralReward.source_ref == referrals.bonus_source_ref("inviter", result.user_id),
                        ReferralReward.level == referrals.BONUS_LEVEL,
                    )
                )
                if bonus is not None and bonus.days > 0:
                    rewards.append(_Reward(bonus.beneficiary_id, 1, days=bonus.days))
        rows = await session.scalars(
            select(ReferralReward)
            .where(
                ReferralReward.source_ref == referrals.payment_source_ref(result.payment_id),
                ReferralReward.level >= 2,
            )
            .order_by(ReferralReward.level)
        )
        rewards.extend(
            _Reward(r.beneficiary_id, r.level, amount_kop=r.amount_kop, partner=r.account is not None and partner)
            for r in rows
        )
    if outcome.purchase_new and outcome.purchase_id is not None:
        rows = await session.scalars(
            select(ReferralReward)
            .where(ReferralReward.source_ref == referrals.purchase_source_ref(outcome.purchase_id))
            .order_by(ReferralReward.level)
        )
        rewards.extend(_Reward(r.beneficiary_id, r.level, days=r.days) for r in rows if r.days > 0)
    return rewards


async def _sync(ctx: AppContext, sub_id: int) -> bool:
    try:
        res = await provisioning.sync_subscription(ctx.session_factory, ctx.panels, ctx.settings, sub_id)
    except Exception:
        log.exception("hook: syncing subscription %s failed; the reconciler will retry", sub_id)
        return False
    return res.failed == 0


# --- texts ------------------------------------------------------------------------------------------------------------
def _gift_link(bot_username: str | None, code: str) -> str:
    return f"https://t.me/{bot_username}?start=gift_{code}" if bot_username else code


def _user_text(
    _: Translator, result: CreditResult, outcome: HookOutcome, plan_title: str | None, bot_username: str | None
) -> str | None:
    esc = _.esc
    if outcome.purchase_new:
        expires = _.date(outcome.expires_at) if outcome.expires_at is not None else ""
        return _("paid.plan_activated", plan=esc(plan_title or ""), expires=expires)
    if outcome.gift_new and outcome.gift_code is not None:
        return _(
            "paid.gift_ready",
            plan=esc(plan_title or ""),
            code=esc(outcome.gift_code),
            link=esc(_gift_link(bot_username, outcome.gift_code)),
        )
    if not outcome.first_run:
        return None
    if result.is_donation:
        return _("paid.donation_thanks", amount=_.money(result.amount_kop))
    if outcome.error is not None:
        return _(
            "paid.plan_failed",
            amount=_.money(result.amount_kop),
            balance=_.money(result.new_balance_kop),
            plan=esc(plan_title or ""),
        )
    if result.is_partial and result.expected_amount_kop is not None:
        return _(
            "paid.partial",
            amount=_.money(result.amount_kop),
            expected=_.money(result.expected_amount_kop),
            balance=_.money(result.new_balance_kop),
        )
    if outcome.purchase_id is not None or outcome.gift_code is not None:
        return None  # bought before by an earlier run that already told the user
    return _("paid.topped_up", amount=_.money(result.amount_kop), balance=_.money(result.new_balance_kop))


def _main_menu(_: Translator) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_("nav.menu"), callback_data=Nav(to=MENU).pack())]]
    )


async def _send(ctx: AppContext, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    return await safe_send(ctx.bot, chat_id, text, session_factory=ctx.session_factory, reply_markup=markup)


async def _notify_reward(ctx: AppContext, reward: _Reward) -> bool:
    async with read_session(ctx.session_factory) as session, session.begin():
        lang = await session.scalar(select(User.language).where(User.tg_id == reward.user_id))
    if lang is None:
        return False
    _ = ctx.i18n.translator(lang, ctx.settings.zone)
    if reward.days > 0:
        text = _("reward.days", days=_.duration(reward.days), level=reward.level)
    elif reward.amount_kop > 0:
        key = "reward.partner" if reward.partner else "reward.money"
        text = _(key, amount=_.money(reward.amount_kop), level=reward.level)
    else:
        return False
    return await _send(ctx, reward.user_id, text)


def _notify_admins(
    ctx: AppContext, result: CreditResult, outcome: HookOutcome, username: str | None, plan_title: str | None
) -> None:
    i18n: I18n = ctx.i18n
    lang = i18n.default_lang
    esc = i18n.esc
    if result.is_donation:
        what = i18n.t("admin_notify.what_donation", lang)
    elif result.is_gift:
        what = i18n.t("admin_notify.what_gift", lang, plan=esc(plan_title or ""))
    elif result.plan_id is not None:
        what = i18n.t("admin_notify.what_plan", lang, plan=esc(plan_title or ""))
    else:
        what = i18n.t("admin_notify.what_topup", lang)
    text = i18n.t(
        "admin_notify.payment",
        lang,
        amount=i18n.money(result.amount_kop, lang),
        provider=esc(result.provider),
        user_id=result.user_id,
        username=esc(f"@{username}" if username else "-"),
        what=what,
    )
    if outcome.error is not None:
        text += "\n" + i18n.t("admin_notify.plan_failed", lang, reason=esc(outcome.error.value))
    notifications.notify(ctx, notifications.AdminEvent.PAYMENTS, text)
