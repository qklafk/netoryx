"""Promo and gifts modules: code entry and redemption, gifts from the balance or by payment, sharing, landing data."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.db.models import Payment, PaymentStatus, Plan, PromoCode, PromoKind, Purchase, PurchaseKind, Subscription
from app.keyboards.callbacks import Nav
from app.modules.gifts import service as gift_service
from app.modules.gifts.bot import GiftCb
from app.modules.payments import flow
from app.modules.payments.callbacks import PayCb
from app.modules.promo.bot import PromoCb
from app.modules.promo.service import clean_code, redeem_code
from app.services import plans, promo
from app.services.promo import PromoOutcome
from tests.test_mod_money_support import Harness, balance, buttons, callback_datas, money, register

__all__ = ["money"]

UID = 7001
FRIEND = 7002


async def _promo(h: Harness, code: str, **kwargs: Any) -> None:
    async with h.ctx.session_factory.begin() as session:
        await promo.create_promo(session, code=code, **kwargs)


async def _plan(h: Harness, price_rub: int = 150, days: int = 30) -> int:
    async with h.ctx.session_factory.begin() as session:
        return (await plans.create(session, title="Month", duration_days=days, price_kop=price_rub * 100)).id


async def _gifts(h: Harness) -> list[PromoCode]:
    async with h.ctx.session_factory() as session:
        return list((await session.scalars(select(PromoCode).where(PromoCode.kind == PromoKind.GIFT))).all())


# --- promo ------------------------------------------------------------------------------------------------------------
async def test_promo_command_with_code_gives_money_and_days(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await _plan(h)
    await _promo(h, "WELCOME", bonus_kop=10_000, bonus_days=7)
    await h.say(UID, "/promo welcome")
    text = h.api.screens()[-1]
    assert "Промокод активирован" in text and "+7 дней" in text and "+100 ₽" in text
    assert await balance(h.ctx, UID) == (10_000, 0)
    assert len(h.synced) == 1  # the new days were pushed to the panels

    await h.say(UID, "/promo WELCOME")
    assert "уже активировали" in h.api.screens()[-1]


async def test_promo_entry_allows_retries_after_typos(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await _promo(h, "SPRING", bonus_kop=5_000)
    await h.press(UID, Nav(to="promo").pack())
    assert "Отправьте код" in h.api.screens()[-1]
    await h.say(UID, "SPRNG")
    assert "Такого кода нет" in h.api.screens()[-1]
    await h.say(UID, "spring")
    assert "+50 ₽" in h.api.screens()[-1]
    await h.say(UID, "spring")  # the step is over
    assert len([t for t in h.api.screens() if "+50 ₽" in t]) == 1


async def test_promo_cancel_clears_the_step(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await _promo(h, "AUTUMN", bonus_kop=5_000)
    await h.press(UID, PromoCb(a="enter").pack())
    await h.press(UID, PromoCb(a="cancel").pack())
    await h.say(UID, "AUTUMN")
    assert await balance(h.ctx, UID) == (0, 0)


async def test_promo_only_for_new_customers(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await _promo(h, "NEWBIE", bonus_kop=5_000, only_new_users=True)
    result = await redeem_code(h.ctx, UID, "newbie")  # type: ignore[arg-type]
    assert result.ok
    assert (await redeem_code(h.ctx, UID, "nope")).outcome is PromoOutcome.NOT_FOUND  # type: ignore[arg-type]
    assert (await redeem_code(h.ctx, 999_999, "newbie")).outcome is PromoOutcome.NOT_FOUND  # type: ignore[arg-type]


def test_clean_code() -> None:
    assert clean_code("  promo_SPRING extra ") == "SPRING"
    assert clean_code("gift_ABC") == "ABC"
    assert clean_code("Promo_abc") == "abc"
    assert clean_code("") == ""
    assert len(clean_code("x" * 500)) == 64


# --- gifts ------------------------------------------------------------------------------------------------------------
async def test_gift_hub_and_plans(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    plan_id = await _plan(h)
    await h.press(UID, Nav(to="gift").pack())
    datas = callback_datas(h.api.last_markup())
    assert GiftCb(a="plans").pack() in datas and PromoCb(a="enter").pack() in datas
    await h.press(UID, GiftCb(a="plans").pack())
    assert GiftCb(a="plan", plan=plan_id).pack() in callback_datas(h.api.last_markup())


async def test_gift_from_balance_is_idempotent_per_button(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID, balance_kop=40_000)
    plan_id = await _plan(h)
    await h.press(UID, GiftCb(a="plan", plan=plan_id).pack())
    [buy] = [d for d in callback_datas(h.api.last_markup()) if d.startswith("gift:buy:")]
    await h.press(UID, buy)
    await h.press(UID, buy)  # double tap
    [gift] = await _gifts(h)
    assert await balance(h.ctx, UID) == (25_000, 0)
    text = h.api.screens()[-1]
    assert gift.code in text and f"https://t.me/test_bot?start=gift_{gift.code}" in text
    urls = [b.url for b in buttons(h.api.last_markup()) if b.url]
    assert urls and urls[0].startswith("https://t.me/share/url?url=")

    await h.press(UID, GiftCb(a="mine").pack())
    assert gift.code in h.api.screens()[-1] and "ждёт получателя" in h.api.screens()[-1]


async def test_gift_without_enough_money_goes_to_payment(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID, balance_kop=5_000)
    plan_id = await _plan(h)
    await h.press(UID, GiftCb(a="plan", plan=plan_id).pack())
    assert "100 ₽" in h.api.screens()[-1]
    pay = PayCb(a="m", k="g", plan=plan_id).pack()
    assert pay in callback_datas(h.api.last_markup())
    await h.press(UID, GiftCb(a="buy", plan=plan_id, n="abc").pack())  # forged button
    assert await _gifts(h) == []

    await h.press(UID, PayCb(a="go", k="g", plan=plan_id, prov="cryptobot").pack())
    async with h.ctx.session_factory() as session:
        payment = await session.scalar(select(Payment))
    assert payment is not None and payment.is_gift and payment.amount_kop == 10_000
    h.gateway.status = PaymentStatus.SUCCEEDED
    await flow.check(h.ctx, UID, payment.id)  # type: ignore[arg-type]
    [gift] = await _gifts(h)
    assert any(gift.code in text for text in h.api.texts())


async def test_gift_redeemed_by_a_friend(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID, balance_kop=15_000)
    await register(h.ctx, FRIEND)
    plan_id = await _plan(h)
    await h.press(UID, GiftCb(a="plan", plan=plan_id).pack())
    [buy] = [d for d in callback_datas(h.api.last_markup()) if d.startswith("gift:buy:")]
    await h.press(UID, buy)
    [gift] = await _gifts(h)

    async with h.ctx.session_factory() as session:
        info = await gift_service.gift_info(session, gift.code.lower())
    assert info is not None and info.plan_title == "Month" and not info.redeemed and info.days == 30

    await h.say(FRIEND, f"/promo {gift.code}")
    assert "Подарок активирован" in h.api.screens()[-1]
    async with h.ctx.session_factory() as session:
        purchase = await session.scalar(select(Purchase).where(Purchase.user_id == FRIEND))
        sub = await session.scalar(select(Subscription).where(Subscription.user_id == FRIEND))
        info = await gift_service.gift_info(session, gift.code)
    assert purchase is not None and purchase.kind is PurchaseKind.GIFT_REDEEM
    assert sub is not None and sub.device_limit == 3  # the gifted plan's limits
    assert info is not None and info.redeemed
    assert any("только что активировали" in text for text in h.api.texts())  # the buyer is thanked


async def test_gift_code_entry_without_promo_module(money: Any) -> None:
    h: Harness = money(disabled_modules=["promo"])
    await register(h.ctx, UID, balance_kop=15_000)
    await register(h.ctx, FRIEND)
    plan_id = await _plan(h)
    await h.press(FRIEND, Nav(to="gift").pack())
    assert GiftCb(a="code").pack() in callback_datas(h.api.last_markup())

    await h.press(UID, GiftCb(a="plan", plan=plan_id).pack())
    [buy] = [d for d in callback_datas(h.api.last_markup()) if d.startswith("gift:buy:")]
    await h.press(UID, buy)
    [gift] = await _gifts(h)
    await h.press(FRIEND, GiftCb(a="code").pack())
    await h.say(FRIEND, "WRONG")
    assert "Такого кода нет" in h.api.screens()[-1]
    await h.say(FRIEND, gift.code)
    assert "Подарок активирован" in h.api.screens()[-1]


async def test_hub_falls_back_to_promo_when_gifts_are_off(money: Any) -> None:
    h: Harness = money(gifts_enabled=False)
    await register(h.ctx, UID)
    await h.press(UID, Nav(to="gift").pack())
    assert "Промокод или код подарка" in h.api.screens()[-1]


async def test_hidden_and_free_plans_are_not_gifts(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID, balance_kop=100_000)
    hidden = await _plan(h)
    free = await _plan(h, price_rub=0)
    async with h.ctx.session_factory.begin() as session:
        plan = await session.get(Plan, hidden)
        assert plan is not None
        plan.is_hidden = True
    await h.press(UID, GiftCb(a="plans").pack())
    assert "Сейчас нет тарифов" in h.api.screens()[-1]
    for plan_id in (hidden, free):
        await h.press(UID, GiftCb(a="buy", plan=plan_id, n="x1").pack())
    async with h.ctx.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PromoCode)) == 0
