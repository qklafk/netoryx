"""Post-credit hook: plan purchase / gift / donation, notifications, referral messages, idempotency."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import SendMessage
from sqlalchemy import func, select

from app.db.models import (
    Payment,
    PaymentStatus,
    PromoCode,
    PromoKind,
    Purchase,
    PurchaseKind,
    Subscription,
    TxKind,
    User,
)
from app.db.session import SessionFactory
from app.services import hooks, notifications, plans, provisioning, referrals, users, wallet
from app.services.payments import service
from app.services.payments.service import CreditResult
from app.services.subscriptions import SubErrorCode
from tests.test_middlewares_support import FakeCtx, FakeSession, make_ctx

OWNER = 1000
BUYER = 7001


@pytest.fixture
def synced(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    async def fake_sync(session_factory: Any, registry: Any, settings: Any, sub_id: int, **kw: Any) -> Any:
        calls.append(sub_id)
        return provisioning.SyncResult(subscription_id=sub_id)

    monkeypatch.setattr(provisioning, "sync_subscription", fake_sync)
    return calls


def _ctx(sf: SessionFactory, tmp_path: Path, **settings: Any) -> tuple[FakeCtx, FakeSession]:
    base = {"referral_invitee_bonus_rub": 0, "referral_inviter_bonus_rub": 0, "referral_levels_percent": [10]}
    return make_ctx(sf, tmp_path, **(base | settings))


async def _register(ctx: FakeCtx, uid: int, inviter: int | None = None) -> None:
    async with ctx.session_factory.begin() as s:
        user, created = await users.get_or_create(s, ctx.settings, uid, f"user{uid}", "Name", "ru")
        if inviter is not None:
            inviter_user = await s.get(User, inviter)
            assert inviter_user is not None
            await referrals.apply_on_registration(s, ctx.settings, user, inviter_user.referral_code, is_new=created)


async def _plan(ctx: FakeCtx, price_rub: int = 150, days: int = 30) -> int:
    async with ctx.session_factory.begin() as s:
        plan = await plans.create(s, title="Month", duration_days=days, price_kop=price_rub * 100)
        return plan.id


async def _pay(
    ctx: FakeCtx, uid: int, rub: int, *, paid_rub: int | None = None, provider: str = "cryptobot", **flags: Any
) -> CreditResult:
    pid = str(uuid.uuid4())
    async with ctx.session_factory.begin() as s:
        s.add(
            Payment(
                id=pid,
                provider=provider,
                user_id=uid,
                amount_kop=rub * 100,
                status=PaymentStatus.PENDING,
                provider_payment_id=f"prov-{pid[:8]}",
                **flags,
            )
        )
    paid = paid_rub * 100 if paid_rub is not None else None
    result = await service.credit(ctx.session_factory, ctx.settings, pid, paid_amount_kop=paid)
    assert result is not None
    return result


async def _messages_to(ctx: FakeCtx, api: FakeSession, chat_id: int) -> list[str]:
    await notifications.get_notifier(ctx).join()  # type: ignore[arg-type]
    return [m.text for m in api.of(SendMessage) if m.chat_id == chat_id]


async def _balance(ctx: FakeCtx, uid: int) -> int:
    async with ctx.session_factory() as s:
        return await wallet.balance(s, uid)


# --- top-up -----------------------------------------------------------------------------------------------------------
async def test_top_up_notifies_user_and_admins_once(session_factory: SessionFactory, tmp_path: Path) -> None:
    ctx, api = _ctx(session_factory, tmp_path)
    await _register(ctx, BUYER)
    result = await _pay(ctx, BUYER, 300)

    outcome = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert outcome.first_run and outcome.notified_users == [BUYER] and outcome.purchase_id is None
    assert [t.split(" ")[0] for t in await _messages_to(ctx, api, BUYER)] == ["TOPPED"]
    admin = await _messages_to(ctx, api, OWNER)
    assert len(admin) == 1 and "via cryptobot" in admin[0] and "@user7001: top-up" in admin[0]

    again = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert not again.first_run and again.notified_users == []
    assert len(await _messages_to(ctx, api, BUYER)) == 1 and len(await _messages_to(ctx, api, OWNER)) == 1
    await notifications.close(ctx)  # type: ignore[arg-type]


# --- plan purchase ----------------------------------------------------------------------------------------------------
async def test_payment_for_a_plan_buys_it_and_syncs(
    session_factory: SessionFactory, tmp_path: Path, synced: list[int]
) -> None:
    ctx, api = _ctx(session_factory, tmp_path)
    await _register(ctx, BUYER)
    plan_id = await _plan(ctx)
    result = await _pay(ctx, BUYER, 150, plan_id=plan_id)

    outcome = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert outcome.purchase_new and outcome.subscription_id is not None and outcome.synced
    assert synced == [outcome.subscription_id]
    texts = await _messages_to(ctx, api, BUYER)
    assert len(texts) == 1 and texts[0].startswith("ACTIVATED Month until ")
    menu = api.of(SendMessage)[0].reply_markup.inline_keyboard[0][0]
    assert menu.callback_data == "nav:menu:"
    assert await _balance(ctx, BUYER) == 0
    assert "plan Month" in (await _messages_to(ctx, api, OWNER))[0]

    again = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert again.purchase_id == outcome.purchase_id and not again.purchase_new
    assert len(await _messages_to(ctx, api, BUYER)) == 1
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Purchase)) == 1
    await notifications.close(ctx)  # type: ignore[arg-type]


async def test_partial_payment_keeps_money_until_the_rest_arrives(
    session_factory: SessionFactory, tmp_path: Path, synced: list[int]
) -> None:
    ctx, api = _ctx(session_factory, tmp_path)
    await _register(ctx, BUYER)
    plan_id = await _plan(ctx)
    result = await _pay(ctx, BUYER, 150, paid_rub=100, plan_id=plan_id)
    assert result.is_partial

    outcome = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert outcome.error is SubErrorCode.INSUFFICIENT_FUNDS and outcome.purchase_id is None and not synced
    assert (await _messages_to(ctx, api, BUYER))[0].startswith("FAILED Month")
    assert "plan not bought: insufficient_funds" in (await _messages_to(ctx, api, OWNER))[0]
    assert await _balance(ctx, BUYER) == 10_000

    async with session_factory.begin() as s:  # an admin adds the missing part and re-runs the hook
        await wallet.apply(s, BUYER, 5_000, TxKind.ADMIN_ADJUST, "fix-1")
    rerun = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert not rerun.first_run and rerun.purchase_new and rerun.error is None
    texts = await _messages_to(ctx, api, BUYER)
    assert len(texts) == 2 and texts[1].startswith("ACTIVATED")
    assert len(await _messages_to(ctx, api, OWNER)) == 1
    await notifications.close(ctx)  # type: ignore[arg-type]


async def test_deleted_plan_leaves_money_on_the_balance(session_factory: SessionFactory, tmp_path: Path) -> None:
    ctx, api = _ctx(session_factory, tmp_path)
    await _register(ctx, BUYER)
    plan_id = await _plan(ctx)
    result = await _pay(ctx, BUYER, 150, plan_id=plan_id)
    result = replace(result, plan_id=plan_id + 100)
    outcome = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert outcome.error is SubErrorCode.NO_PLAN
    assert (await _messages_to(ctx, api, BUYER))[0].startswith("FAILED")
    await notifications.close(ctx)  # type: ignore[arg-type]


async def test_stars_renewal_is_recorded_as_auto_renew(
    session_factory: SessionFactory, tmp_path: Path, synced: list[int]
) -> None:
    ctx, _ = _ctx(session_factory, tmp_path)
    await _register(ctx, BUYER)
    plan_id = await _plan(ctx)
    result = await _pay(ctx, BUYER, 150, plan_id=plan_id, provider="stars")
    result = replace(result, recurring_charge_id="first-charge")  # not this payment's charge: a renewal
    outcome = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    async with session_factory() as s:
        purchase = await s.get(Purchase, outcome.purchase_id)
        assert purchase is not None and purchase.kind is PurchaseKind.AUTO_RENEW
    await notifications.close(ctx)  # type: ignore[arg-type]


# --- gifts & donations ------------------------------------------------------------------------------------------------
async def test_gift_payment_creates_a_code(session_factory: SessionFactory, tmp_path: Path, synced: list[int]) -> None:
    ctx, api = _ctx(session_factory, tmp_path)
    await _register(ctx, BUYER)
    plan_id = await _plan(ctx)
    result = await _pay(ctx, BUYER, 150, plan_id=plan_id, is_gift=True)

    outcome = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert outcome.gift_new and outcome.gift_code and not synced
    text = (await _messages_to(ctx, api, BUYER))[0]
    assert text == f"GIFT Month code {outcome.gift_code} link https://t.me/test_bot?start=gift_{outcome.gift_code}"
    assert "gift Month" in (await _messages_to(ctx, api, OWNER))[0]

    again = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert again.gift_code == outcome.gift_code and not again.gift_new
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(PromoCode).where(PromoCode.kind == PromoKind.GIFT)) == 1
        assert await s.scalar(select(func.count()).select_from(Subscription)) == 0
    assert len(await _messages_to(ctx, api, BUYER)) == 1
    await notifications.close(ctx)  # type: ignore[arg-type]


async def test_donation_thanks(session_factory: SessionFactory, tmp_path: Path) -> None:
    ctx, api = _ctx(session_factory, tmp_path)
    await _register(ctx, BUYER)
    result = await _pay(ctx, BUYER, 100, provider="stars", is_donation=True)
    await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert (await _messages_to(ctx, api, BUYER))[0].startswith("THANKS")
    assert "donation" in (await _messages_to(ctx, api, OWNER))[0]
    assert await _balance(ctx, BUYER) == 0
    await notifications.close(ctx)  # type: ignore[arg-type]


async def test_missing_payment_is_ignored(session_factory: SessionFactory, tmp_path: Path) -> None:
    ctx, api = _ctx(session_factory, tmp_path)
    result = CreditResult("nope", BUYER, 100, 100, None, False, False, None, "cryptobot")
    outcome = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert not outcome.first_run and not api.calls


# --- referral notifications -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("mode", "word"), [("balance", "REWARD"), ("partner", "PARTNER")])
async def test_upline_is_told_about_commissions(
    session_factory: SessionFactory, tmp_path: Path, mode: str, word: str
) -> None:
    ctx, api = _ctx(session_factory, tmp_path, referral_reward_mode=mode, referral_levels_percent=[10, 5])
    await _register(ctx, 1)
    await _register(ctx, 2, inviter=1)
    await _register(ctx, 3, inviter=2)
    result = await _pay(ctx, 3, 1000)
    outcome = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert sorted(outcome.notified_users) == [1, 2, 3]
    level1, level2 = (await _messages_to(ctx, api, 2)), (await _messages_to(ctx, api, 1))
    assert len(level1) == 1 and level1[0].startswith(word) and level1[0].endswith("level 1")
    assert len(level2) == 1 and level2[0].startswith(word) and level2[0].endswith("level 2")

    await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert len(await _messages_to(ctx, api, 2)) == 1
    await notifications.close(ctx)  # type: ignore[arg-type]


async def test_days_mode_rewards_are_announced_after_the_purchase(
    session_factory: SessionFactory, tmp_path: Path, synced: list[int]
) -> None:
    ctx, api = _ctx(session_factory, tmp_path, referral_reward_mode="days", referral_inviter_bonus_days=0)
    await _register(ctx, 1)
    await _register(ctx, 2, inviter=1)
    plan_id = await _plan(ctx, days=30)
    result = await _pay(ctx, 2, 150, plan_id=plan_id)
    outcome = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert outcome.purchase_new
    texts = await _messages_to(ctx, api, 1)
    assert len(texts) == 1 and texts[0].startswith("REWARD DAYS 3 ") and texts[0].endswith("level 1")
    await notifications.close(ctx)  # type: ignore[arg-type]


def test_notice_key_fits_kv() -> None:
    assert len(hooks.notice_key(str(uuid.uuid4()))) <= 64


async def test_days_mode_one_time_inviter_bonus_is_announced(session_factory: SessionFactory, tmp_path: Path) -> None:
    ctx, api = _ctx(
        session_factory,
        tmp_path,
        referral_reward_mode="days",
        referral_levels_percent=[],
        referral_inviter_requires_payment=True,
        referral_inviter_bonus_days=7,
        referral_invitee_bonus_days=0,
    )
    await _register(ctx, 1)
    await _register(ctx, 2, inviter=1)
    result = await _pay(ctx, 2, 150)
    assert result.inviter == (1, 0)
    await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    texts = await _messages_to(ctx, api, 1)
    assert len(texts) == 1 and texts[0].startswith("REWARD DAYS 7 ")
    await notifications.close(ctx)  # type: ignore[arg-type]
