"""/start with deep links, the main menu, language, profile, help, legal commands and the fallback handlers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import SendMessage
from sqlalchemy import select

from app.db.models import Invite, PromoCode, PromoKind, User
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.core_user import start as deeplinks
from app.modules.subscription.callbacks import PayCb, PlanCb
from app.services import invites
from app.services import plans as plans_service
from tests.test_mod_user_support import (
    OWNER,
    USER,
    Harness,
    add_plan,
    add_subscription,
    get_sub,
    get_user,
    install,
    make_harness,
    top_up,
)

Make = Callable[..., Harness]


@pytest.fixture
async def make(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Make]:
    harnesses: list[Harness] = []

    def factory(**settings: Any) -> Harness:
        harness = make_harness(session_factory, tmp_path / str(len(harnesses)), **settings)
        harnesses.append(harness)
        return harness

    yield factory
    for harness in harnesses:
        await harness.close()


# --- payload parsing --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("payload", "kind", "value"),
    [
        (None, deeplinks.Kind.NONE, ""),
        ("ref_ABCD2345", deeplinks.Kind.REFERRAL, "ABCD2345"),
        ("gift_XYZ", deeplinks.Kind.GIFT, "XYZ"),
        ("promo_SALE", deeplinks.Kind.PROMO, "SALE"),
        ("inv_CODE", deeplinks.Kind.INVITE, "CODE"),
        ("c_summer", deeplinks.Kind.CAMPAIGN, "summer"),
        ("plan_7", deeplinks.Kind.PLAN, "7"),
        ("RENEW", deeplinks.Kind.BUY, ""),
        ("connect", deeplinks.Kind.CONNECT, ""),
        ("proxy", deeplinks.Kind.PROXY, ""),
        ("ABCD2345", deeplinks.Kind.UNKNOWN, "ABCD2345"),
        ("ref_", deeplinks.Kind.UNKNOWN, "ref_"),
    ],
)
def test_parse_payload(payload: str | None, kind: deeplinks.Kind, value: str) -> None:
    parsed = deeplinks.parse(payload)
    assert (parsed.kind, parsed.value) == (kind, value)


def test_plan_id_only_for_digits() -> None:
    assert deeplinks.parse("plan_12").plan_id == 12
    assert deeplinks.parse("plan_x").plan_id is None
    parsed = deeplinks.parse("plan_12-0a1b2c3d4e5f")
    assert (parsed.plan_id, parsed.plan_token) == (12, "0a1b2c3d4e5f")


# --- /start and the menu ----------------------------------------------------------------------------------------------
async def test_first_start_greets_and_offers_the_trial(make: Make) -> None:
    h = make()
    await h.send("/start", first_name="Ann")
    text = h.last_text()
    assert "Привет, Ann" in text and "TestVPN" in text
    assert "3 дня" in text  # trial length in the welcome
    # offered once: the welcome mentions the trial, so the status line does not repeat it
    assert h.t("core_user.menu.status_trial", trial="3 дня") not in text
    assert text.count("3 дня") == 1
    buttons = h.button_texts()
    assert buttons[0] == h.t("menu.trial")
    assert h.t("menu.my_subscription") in buttons and h.t("menu.buy") in buttons
    assert h.t("menu.admin") not in buttons
    assert await get_user(h.sf) is not None


async def test_returning_user_gets_the_menu_with_status(make: Make) -> None:
    h = make()
    await h.send("/start")
    await add_subscription(h.sf, days=10)
    await h.press(Nav(to="menu"))
    text = h.last_text()
    assert "Подписка активна до" in text and "Привет" not in text
    assert h.t("menu.trial") not in h.button_texts()


async def test_admin_sees_the_admin_entry(make: Make) -> None:
    h = make()
    await h.send("/start", uid=OWNER)
    assert h.t("menu.admin") in h.button_texts()


async def test_personal_mode_menu_has_no_shop_entries(make: Make, session_factory: SessionFactory) -> None:
    h = make(app_mode="personal")
    await add_subscription_for_new_user(session_factory, USER)
    await h.send("/start")
    buttons = h.button_texts()
    assert h.t("menu.buy") not in buttons and h.t("menu.trial") not in buttons
    assert h.t("menu.connect") in buttons


async def add_subscription_for_new_user(sf: SessionFactory, uid: int) -> None:
    async with sf.begin() as session:
        session.add(User(tg_id=uid, referral_code=f"R{uid}", sub_token=f"tok{uid}", language="ru"))
    await add_subscription(sf, uid)


async def test_start_with_referral_link_links_and_rewards(make: Make) -> None:
    h = make(referral_invitee_bonus_rub=50, referral_inviter_requires_payment=False, referral_inviter_bonus_rub=30)
    await h.send("/start", uid=7001)
    inviter = await get_user(h.sf, 7001)
    await h.send(f"/start ref_{inviter.referral_code}")
    user = await get_user(h.sf)
    assert user.referred_by == 7001
    assert user.balance_kop == 5_000
    invitee_messages = [c for c in h.api.of(SendMessage) if int(c.chat_id) == USER]
    assert invitee_messages and "50 ₽" in invitee_messages[-1].text
    told = [c for c in h.api.of(SendMessage) if int(c.chat_id) == 7001]
    assert told and "30 ₽" in told[-1].text


async def test_referral_link_is_ignored_for_old_users(make: Make) -> None:
    h = make()
    await h.send("/start", uid=7001)
    inviter = await get_user(h.sf, 7001)
    await h.send("/start")
    async with h.sf.begin() as session:
        user = await session.get(User, USER)
        assert user is not None
        user.created_at = user.created_at.replace(year=user.created_at.year - 1)
    await h.send(f"/start ref_{inviter.referral_code}")
    assert (await get_user(h.sf)).referred_by is None


async def test_gift_link_extends_the_subscription(make: Make) -> None:
    h = make()
    await h.send("/start", uid=7001)
    plan_id = await add_plan(h.sf, title="Год", duration_days=365, price_kop=140_000)
    async with h.sf.begin() as session:
        session.add(PromoCode(code="GIFT1", kind=PromoKind.GIFT, bonus_days=365, plan_id=plan_id, max_uses=1))
    await h.send("/start gift_gift1")
    sub = await get_sub(h.sf)
    assert sub is not None and sub.plan_id == plan_id
    assert "Подарок активирован" in h.last_text_to() and "Год" in h.last_text_to()


async def test_promo_link_credits_money(make: Make) -> None:
    h = make()
    async with h.sf.begin() as session:
        session.add(PromoCode(code="SALE", kind=PromoKind.PROMO, bonus_kop=10_000, max_uses=0))
    await h.send("/start promo_sale")
    assert (await get_user(h.sf)).balance_kop == 10_000
    assert "100 ₽" in h.last_text_to()
    await h.send("/start promo_sale")
    assert h.t("core_user.code.already_used") in h.last_text_to()


async def test_unknown_promo_code(make: Make) -> None:
    h = make()
    await h.send("/start promo_nope")
    assert h.t("core_user.code.not_found") in h.last_text_to()


async def test_invite_link_in_personal_mode(make: Make) -> None:
    h = make(app_mode="personal")
    async with h.sf.begin() as session:
        invite = await invites.create(session, OWNER, days=None, device_limit=2, note="мама")
        code = invite.code
    await h.send(f"/start inv_{code}")
    sub = await get_sub(h.sf)
    assert sub is not None and sub.is_complimentary and sub.device_limit == 2
    assert h.t("core_user.invite.accepted_forever") in h.last_text()
    async with h.sf() as session:
        stored = await session.scalar(select(Invite).where(Invite.code == code))
        assert stored is not None and stored.uses == 1


async def test_plan_link_opens_a_hidden_plan(make: Make) -> None:
    h = make()
    plan_id = await add_plan(h.sf, title="Секретный", is_hidden=True)
    payload = plans_service.link_payload(h.ctx.secret_box, plan_id)
    await h.send(f"/start {payload}")
    assert "Секретный" in h.last_text()


async def test_hidden_plan_is_not_opened_by_its_number(make: Make) -> None:
    h = make()
    plan_id = await add_plan(h.sf, title="Секретный", is_hidden=True)
    await h.send(f"/start plan_{plan_id}")
    assert "Секретный" not in h.last_text()
    await h.send(f"/start plan_{plan_id}-000000000000")
    assert "Секретный" not in h.last_text()
    await h.press(PlanCb(id=plan_id))
    assert "Секретный" not in h.last_text()
    assert h.t("subscription.plans.gone") in h.popups()


async def test_hidden_plan_cannot_be_bought_by_its_number(make: Make) -> None:
    h = make()
    plan_id = await add_plan(h.sf, title="Секретный", is_hidden=True, price_kop=4_900)
    await h.send("/start")
    await top_up(h.sf, USER, 10_000)
    await h.press(PayCb(plan=plan_id, via="bal", n="crafted"))
    assert await get_sub(h.sf) is None
    assert (await get_user(h.sf)).balance_kop == 10_000


async def test_hidden_plan_bought_through_its_link(make: Make) -> None:
    h = make()
    plan_id = await add_plan(h.sf, title="Секретный", is_hidden=True, price_kop=4_900)
    await h.send("/start")
    await top_up(h.sf, USER, 10_000)
    await h.send(f"/start {plans_service.link_payload(h.ctx.secret_box, plan_id)}")
    pay = PayCb.unpack(h.button("Оплатить с баланса").callback_data)
    await h.press(pay)
    sub = await get_sub(h.sf)
    assert sub is not None and sub.plan_id == plan_id
    assert (await get_user(h.sf)).balance_kop == 5_100


async def test_connect_link_without_subscription_offers_the_trial(make: Make) -> None:
    h = make()
    await h.send("/start connect")
    assert h.t("subscription.connect.need_sub_trial", trial="3 дня") in h.last_text()


async def test_hidden_plans_are_not_listed(make: Make) -> None:
    h = make()
    await add_plan(h.sf, title="Обычный")
    await add_plan(h.sf, title="Секретный", is_hidden=True)
    await h.send("/start renew")
    text = h.last_text()
    assert "Обычный" in text and "Секретный" not in text


# --- language, profile, help ------------------------------------------------------------------------------------------
async def test_language_switch(make: Make) -> None:
    h = make()
    await h.send("/start")
    await h.press(Nav(to="lang"))
    assert h.t("lang.title") in h.last_text()
    await h.press(Nav(to="lang", arg="en"))
    assert (await get_user(h.sf)).language == "en"
    assert h.t("menu.my_subscription", "en") in h.button_texts()
    assert h.t("lang.changed", "en") in h.popups()


async def test_profile(make: Make) -> None:
    h = make()
    await h.send("/start")
    await h.press(Nav(to="profile"))
    assert f"<code>{USER}</code>" in h.last_text()
    assert "Баланс" in h.last_text()


async def test_help_and_legal_commands(make: Make) -> None:
    h = make(terms_url="https://example.com/terms", support_url="https://t.me/support")
    await h.send("/start")
    await h.send("/help")
    assert "Как подключиться" in h.last_text()
    await h.send("/terms")
    assert h.button(h.t("consent.terms")).url == "https://example.com/terms"
    await h.send("/privacy")
    assert h.t("core_user.legal.missing") in h.last_text()
    await h.send("/paysupport")
    assert h.button(h.t("menu.support")).url == "https://t.me/support"


# --- fallback ---------------------------------------------------------------------------------------------------------
async def test_unknown_messages_and_stale_buttons(make: Make) -> None:
    h = make()
    await install(h)
    await h.send("/start")
    await h.send("hello?")
    assert h.t("error.unknown_message") in h.last_text()
    await h.press("zz:old")
    assert h.t("error.expired_button") in h.popups()
    await h.press(Nav(to="wallet"))  # a feature that is not installed
    assert h.t("error.unavailable") in h.popups()
    assert h.t("menu.my_subscription") in h.button_texts()


async def test_plan_callback_of_an_archived_plan(make: Make) -> None:
    h = make()
    plan_id = await add_plan(h.sf, is_active=False)
    await h.send("/start")
    await h.press(PlanCb(id=plan_id))
    assert h.t("subscription.plans.gone") in h.popups()
