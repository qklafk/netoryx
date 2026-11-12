"""User screens stay behind the gates: group chats, switched-off modules, input steps and Stars renewals."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.methods import EditMessageText, EditUserStarSubscription, SendInvoice, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update
from sqlalchemy import func, select, update

from app.core.context import DevGateway
from app.core.modules import FeatureModule
from app.db.models import Payment, PromoCode, PromoKind, Subscription, User
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.core_user import module as core_user_module
from app.modules.payments import helpers
from app.modules.subscription import module as subscription_module
from app.modules.subscription.callbacks import PayCb, PlanCb, SubAct
from app.modules.tickets.callbacks import TicketAdminCb
from app.services import invites
from app.services.payments.stars import StarsGateway
from tests.test_middlewares_support import tg_user
from tests.test_mod_miniapp_support import USER_ID as MINIAPP_USER
from tests.test_mod_miniapp_support import miniapp_env
from tests.test_mod_user_support import (
    OWNER,
    USER,
    Harness,
    add_plan,
    add_subscription,
    get_sub,
    install,
    make_harness,
    top_up,
)

Make = Callable[..., Harness]
GROUP = -100_555
_ids = count(900_000)

# Stand-ins for optional modules: only their on/off state matters here.
PAYMENTS = FeatureModule(name="payments", title_ru="Платежи", title_en="Payments", shop_only=True)
INVITES = FeatureModule(name="invites", title_ru="Приглашения", title_en="Invitations")
NOTICE = TicketAdminCb(a="nview", id=5).pack()  # a "new ticket" notice in the admin group


@pytest.fixture
async def make(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Make]:
    harnesses: list[Harness] = []

    def factory(*extra: FeatureModule, **settings: Any) -> Harness:
        modules = [core_user_module, subscription_module, *extra]
        harness = make_harness(session_factory, tmp_path / str(len(harnesses)), modules=modules, **settings)
        harnesses.append(harness)
        return harness

    yield factory
    for harness in harnesses:
        await harness.close()


def group_message(uid: int, text: str) -> Update:
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=GROUP, type="supergroup"),
        from_user=tg_user(uid),
        text=text,
    )
    return Update(update_id=next(_ids), message=msg)


def group_callback(uid: int, data: str) -> Update:
    msg = Message(message_id=77, date=datetime.now(UTC), chat=Chat(id=GROUP, type="supergroup"), text="notice")
    query = CallbackQuery(id=f"g{next(_ids)}", from_user=tg_user(uid), chat_instance="g", data=data, message=msg)
    return Update(update_id=next(_ids), callback_query=query)


def _to_group(h: Harness) -> list[Any]:
    return [
        call
        for call in h.api.calls
        if isinstance(call, SendMessage | EditMessageText) and str(call.chat_id) == str(GROUP)
    ]


async def _payments(sf: SessionFactory) -> int:
    async with sf() as session:
        return int(await session.scalar(select(func.count()).select_from(Payment)) or 0)


# --- group chats ------------------------------------------------------------------------------------------------------
async def test_user_screens_do_not_open_in_group_chats(make: Make) -> None:
    h = make(required_channel_id=-100_777, required_channel_url="https://t.me/chan", throttle_rate_limit=100)
    await install(h)
    await h.send("/start")  # registered, then stopped by the channel gate
    h.api.calls.clear()
    for text in ("/menu", "/sub", "/buy", "/profile", "/start"):
        await h.feed(group_message(USER, text))
    await h.feed(group_callback(USER, Nav(to="trial").pack()))
    assert _to_group(h) == []
    assert await get_sub(h.sf) is None  # no trial past the gate


async def test_group_purchase_buttons_create_nothing(make: Make) -> None:
    h = make(PAYMENTS)
    h.ctx.gateways.items["test"] = DevGateway("https://bot.example.com")
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await h.feed(group_callback(USER, PayCb(plan=plan_id, via="test", n="x").pack()))
    await h.feed(group_callback(USER, PayCb(plan=plan_id, via="balance", n="y").pack()))
    assert await _payments(h.sf) == 0
    assert await get_sub(h.sf) is None


async def test_private_chats_still_work(make: Make) -> None:
    h = make()
    await install(h)
    await h.send("/start")
    await h.send("/menu")
    assert h.t("menu.my_subscription") in h.button_texts()


# --- buttons nobody accepts -------------------------------------------------------------------------------------------
async def test_stale_button_in_a_group_only_answers(make: Make) -> None:
    h = make()
    await install(h)
    await h.send("/start")
    await h.send("/start", uid=OWNER)
    h.api.calls.clear()
    await h.feed(group_callback(OWNER, NOTICE))
    await h.feed(group_callback(USER, NOTICE))
    await h.feed(group_callback(4242, NOTICE))  # not registered at all
    assert _to_group(h) == []
    assert len(h.popups()) == 3


async def test_stale_button_in_private_shows_the_menu(make: Make) -> None:
    h = make()
    await install(h)
    await h.send("/start")
    await h.press("old:button")
    assert h.t("menu.my_subscription") in h.button_texts()
    assert h.t("error.expired_button") in h.popups()


# --- checkout while payments are off ----------------------------------------------------------------------------------
async def test_plan_card_hides_providers_while_payments_are_off(make: Make) -> None:
    h = make(PAYMENTS, disabled_modules=["payments"])
    h.ctx.gateways.items["test"] = DevGateway("https://bot.example.com")
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await h.press(PlanCb(id=plan_id))
    assert h.t("subscription.gw.test") not in " ".join(h.button_texts())
    await h.press(PayCb(plan=plan_id, via="test", n="old"))  # a button from before the switch
    assert await _payments(h.sf) == 0
    assert h.t("subscription.pay.unavailable") in h.popups()


async def test_stars_invoice_is_not_sent_while_payments_are_off(make: Make) -> None:
    h = make(PAYMENTS, disabled_modules=["payments"], stars_enabled=True)
    h.ctx.gateways.items["stars"] = StarsGateway(h.ctx.settings)
    plan_id = await add_plan(h.sf, price_stars=99)
    await h.send("/start")
    await h.press(PayCb(plan=plan_id, via="stars", n="old"))
    assert h.api.of(SendInvoice) == []
    assert await _payments(h.sf) == 0


async def test_plan_card_offers_providers_while_payments_are_on(make: Make) -> None:
    h = make(PAYMENTS)
    h.ctx.gateways.items["test"] = DevGateway("https://bot.example.com")
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await h.press(PlanCb(id=plan_id))
    await h.press(PayCb.unpack(h.button(h.t("subscription.gw.test")).callback_data))
    assert await _payments(h.sf) == 1


# --- invitations while the module is off ------------------------------------------------------------------------------
async def test_invite_link_is_ignored_while_invites_are_off(make: Make) -> None:
    h = make(INVITES, app_mode="personal", disabled_modules=["invites"])
    async with h.sf.begin() as session:
        invite = await invites.create(session, OWNER, days=None, device_limit=2, note="old")
        code = invite.code
    await h.send(f"/start inv_{code}")
    assert await get_sub(h.sf) is None


async def test_invite_link_works_while_invites_are_on(make: Make) -> None:
    h = make(INVITES, app_mode="personal")
    async with h.sf.begin() as session:
        invite = await invites.create(session, OWNER, days=None, device_limit=2, note="new")
        code = invite.code
    await h.send(f"/start inv_{code}")
    sub = await get_sub(h.sf)
    assert sub is not None and sub.is_complimentary


# --- e-mail step of the checkout --------------------------------------------------------------------------------------
def _yookassa(h: Harness) -> None:
    class Yoo(DevGateway):
        key = "yookassa"

    h.ctx.gateways.items["yookassa"] = Yoo("https://bot.example.com")


YOOKASSA = {
    "yookassa_enabled": True,
    "yookassa_shop_id": "1",
    "yookassa_secret_key": "k",
    "yookassa_receipt_enabled": True,
}


async def test_commands_leave_the_email_step(make: Make) -> None:
    later = Router(name="later_module")  # a module ordered after the subscription one, like wallet or tickets

    @later.message(Command("balance"))
    async def balance(message: Message) -> None:
        await message.answer("balance screen")

    h = make(PAYMENTS, FeatureModule(name="later", title_ru="-", title_en="-", bot_routers=[later]), **YOOKASSA)
    _yookassa(h)
    await install(h)
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await h.press(PlanCb(id=plan_id))
    await h.press(PayCb.unpack(h.button(h.t("subscription.gw.yookassa")).callback_data))
    assert h.t("subscription.email.ask") in h.last_text()
    await h.send("/balance")
    assert h.last_text() == "balance screen"


async def test_email_step_expires(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    h = make(PAYMENTS, **YOOKASSA)
    _yookassa(h)
    await install(h)
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await h.press(PlanCb(id=plan_id))
    await h.press(PayCb.unpack(h.button(h.t("subscription.gw.yookassa")).callback_data))
    monkeypatch.setattr(helpers, "INPUT_TTL_SEC", -1.0)  # the step is long abandoned
    await h.send("ann@example.com")
    assert await _payments(h.sf) == 0
    assert h.t("error.unknown_message") in h.last_text()


async def test_email_step_accepts_an_address(make: Make) -> None:
    h = make(PAYMENTS, **YOOKASSA)
    _yookassa(h)
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await h.press(PlanCb(id=plan_id))
    await h.press(PayCb.unpack(h.button(h.t("subscription.gw.yookassa")).callback_data))
    await h.send("bad")
    assert h.t("subscription.email.invalid") in h.last_text()
    await h.send("ann@example.com")
    assert await _payments(h.sf) == 1


# --- Telegram Stars renewals ------------------------------------------------------------------------------------------
async def test_turning_auto_renew_off_cancels_the_stars_subscription(make: Make) -> None:
    h = make()
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await add_subscription(h.sf, plan_id=plan_id, auto_renew=True, stars_charge_id="charge-1")
    await h.press(SubAct(act="ar0"))
    edits = h.api.of(EditUserStarSubscription)
    assert len(edits) == 1
    assert edits[0].user_id == USER and edits[0].telegram_payment_charge_id == "charge-1" and edits[0].is_canceled
    sub = await get_sub(h.sf)
    assert sub is not None and sub.auto_renew is False and sub.stars_charge_id is None
    assert h.t("subscription.auto_renew.disabled") in h.popups()


async def test_failed_stars_cancel_keeps_auto_renew_on(make: Make) -> None:
    h = make()
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await add_subscription(h.sf, plan_id=plan_id, auto_renew=True, stars_charge_id="charge-1")
    h.api.fail(EditUserStarSubscription, RuntimeError("telegram is down"))
    await h.press(SubAct(act="ar0"))
    sub = await get_sub(h.sf)
    assert sub is not None and sub.auto_renew is True and sub.stars_charge_id == "charge-1"
    assert h.t("subscription.auto_renew.stars_failed") in h.popups()


async def test_turning_auto_renew_off_without_stars_calls_nothing(make: Make) -> None:
    h = make()
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await add_subscription(h.sf, plan_id=plan_id, auto_renew=True)
    await top_up(h.sf, USER, 1)
    await h.press(SubAct(act="ar0"))
    assert h.api.of(EditUserStarSubscription) == []
    sub = await get_sub(h.sf)
    assert sub is not None and sub.auto_renew is False


async def test_stars_subscription_unknown_to_telegram_is_just_unlinked(make: Make) -> None:
    h = make()
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await add_subscription(h.sf, plan_id=plan_id, auto_renew=True, stars_charge_id="charge-1")
    h.api.fail(
        EditUserStarSubscription,
        TelegramBadRequest(
            EditUserStarSubscription(user_id=USER, telegram_payment_charge_id="charge-1", is_canceled=True),
            "SUBSCRIPTION_NOT_ACTIVE",
        ),
    )
    await h.press(SubAct(act="ar0"))
    sub = await get_sub(h.sf)
    assert sub is not None and sub.auto_renew is False and sub.stars_charge_id is None


async def test_mini_app_auto_renew_off_cancels_the_stars_subscription(
    tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with miniapp_env(tmp_path, session_factory, monkeypatch) as env:
        assert (await env.post("/subscription/trial")).status_code == 200
        async with session_factory.begin() as session:
            await session.execute(update(Subscription).values(stars_charge_id="charge-7", auto_renew=True))
        canceled: list[dict[str, Any]] = []
        fail = [True]

        async def edit_user_star_subscription(**params: Any) -> bool:
            if fail[0]:
                raise RuntimeError("telegram is down")
            canceled.append(params)
            return True

        env.ctx.bot.edit_user_star_subscription = edit_user_star_subscription  # type: ignore[attr-defined]
        resp = await env.post("/subscription/auto-renew", {"enabled": False})
        assert resp.status_code == 503 and resp.json()["error"] == "stars_cancel_failed"
        assert (await env.get("/subscription")).json()["auto_renew"] is True
        fail[0] = False
        resp = await env.post("/subscription/auto-renew", {"enabled": False})
        assert resp.status_code == 200 and resp.json() == {"auto_renew": False}
        assert canceled == [{"user_id": MINIAPP_USER, "telegram_payment_charge_id": "charge-7", "is_canceled": True}]
        async with session_factory() as session:
            sub = await session.scalar(select(Subscription))
        assert sub is not None and sub.stars_charge_id is None


# --- partial maintenance of the features linked from always-on screens ------------------------------------------------
PROMO = FeatureModule(name="promo", title_ru="Промокоды", title_en="Promo codes", shop_only=True)
GIFTS = FeatureModule(name="gifts", title_ru="Подарки", title_en="Gifts", shop_only=True)
TG_PROXY = FeatureModule(name="tg_proxy", title_ru="Прокси", title_en="Proxy")


async def _partial(h: Harness, *names: str) -> None:
    await h.ctx.maintenance.update(lambda s: s.disabled_modules.extend(names), actor_id=None, action="test")


async def test_plan_card_hides_providers_during_partial_maintenance_of_payments(make: Make) -> None:
    h = make(PAYMENTS)
    h.ctx.gateways.items["test"] = DevGateway("https://bot.example.com")
    plan_id = await add_plan(h.sf)
    await h.send("/start")
    await h.press(PlanCb(id=plan_id))
    button = PayCb.unpack(h.button(h.t("subscription.gw.test")).callback_data)
    await _partial(h, "payments")
    await h.press(PlanCb(id=plan_id))
    assert h.t("subscription.gw.test") not in " ".join(h.button_texts())
    await h.press(button)  # rendered before the maintenance began
    assert await _payments(h.sf) == 0
    assert h.t("subscription.pay.unavailable") in h.popups()


async def test_admins_keep_providers_during_partial_maintenance_of_payments(make: Make) -> None:
    h = make(PAYMENTS)
    h.ctx.gateways.items["test"] = DevGateway("https://bot.example.com")
    plan_id = await add_plan(h.sf)
    await _partial(h, "payments")
    await h.send("/start", uid=OWNER)
    await h.press(PlanCb(id=plan_id), uid=OWNER)
    await h.press(PayCb.unpack(h.button(h.t("subscription.gw.test")).callback_data), uid=OWNER)
    assert await _payments(h.sf) == 1


async def test_invite_link_is_ignored_during_partial_maintenance_of_invites(make: Make) -> None:
    h = make(INVITES, app_mode="personal")
    async with h.sf.begin() as session:
        invite = await invites.create(session, OWNER, days=None, device_limit=2, note="maint")
        code = invite.code
    await _partial(h, "invites")
    await h.send(f"/start inv_{code}")
    assert await get_sub(h.sf) is None


async def test_promo_link_is_ignored_during_partial_maintenance_of_codes(make: Make) -> None:
    h = make(PROMO, GIFTS)
    async with h.sf.begin() as session:
        session.add(PromoCode(code="SALE", kind=PromoKind.PROMO, bonus_kop=10_000, max_uses=0))
    await _partial(h, "promo", "gifts")
    await h.send("/start promo_sale")
    async with h.sf() as session:
        user = await session.get(User, USER)
    assert user is not None and user.balance_kop == 0


async def test_proxy_link_falls_back_to_the_menu_during_partial_maintenance(make: Make) -> None:
    h = make(TG_PROXY)
    await _partial(h, "tg_proxy")
    await h.send("/start proxy")
    assert h.t("core_user.proxy.text") not in h.last_text()
