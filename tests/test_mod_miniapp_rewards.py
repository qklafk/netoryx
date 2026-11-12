from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.db.models import LedgerAccount, TxKind, User
from app.db.session import SessionFactory
from app.services import plans, promo, wallet
from app.services.notifications import AdminEvent
from tests.test_mod_miniapp_support import USER_ID, Env, add_member, add_user, miniapp_env

OTHER_ID = 5151


@pytest.fixture
async def env(tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with miniapp_env(tmp_path, session_factory, monkeypatch) as environment:
        await add_user(environment.ctx.session_factory, environment.settings)
        yield environment


async def referral_code(env: Env, user_id: int = USER_ID) -> str:
    async with env.ctx.session_factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        return user.referral_code


async def credit_partner(env: Env, amount_kop: int) -> None:
    async with env.ctx.session_factory.begin() as session:
        await wallet.apply(
            session,
            USER_ID,
            amount_kop,
            TxKind.REFERRAL_COMMISSION,
            f"L1:test{amount_kop}",
            account=LedgerAccount.REFERRAL,
        )


# ---------------------------------------------------------------------------------------------
# Referral
# ---------------------------------------------------------------------------------------------
async def test_referral_balance_mode(env: Env) -> None:
    code = await referral_code(env)
    async with env.ctx.session_factory.begin() as session:
        other = User(tg_id=OTHER_ID, referral_code="OTHERCODE", sub_token="other-token", referred_by=USER_ID)
        session.add(other)
    body = (await env.get("/referral", params={"qr": True})).json()
    assert body["mode"] == "balance" and body["code"] == code
    assert body["link"] == f"https://t.me/test_vpn_bot?start=ref_{code}"
    assert body["link"] in body["share_text"] and "TestVPN" in body["share_text"]
    assert body["qr"].startswith("data:image/png;base64,")
    assert body["invited"] == 1 and body["earned_kop"] == 0
    assert body["invitee_bonus_kop"] == 50_00 and body["invitee_bonus_days"] == 0
    assert body["levels"] == [
        {"level": 1, "percent": 10, "users": 1, "paid_users": 0, "earned_kop": 0, "earned_days": 0}
    ]
    assert body["partner"] is None
    assert (await env.get("/referral")).json()["qr"] is None


async def test_referral_days_mode_and_switch_off(env: Env) -> None:
    env.ctx.box.update(referral_reward_mode="days")
    body = (await env.get("/referral")).json()
    assert body["mode"] == "days" and body["invitee_bonus_days"] == 3 and body["invitee_bonus_kop"] == 0
    assert "бонусные дни" in body["share_text"]
    env.ctx.box.update(referral_enabled=False)
    off = await env.get("/referral")
    assert off.status_code == 404 and off.json()["error"] == "feature_disabled"


async def test_partner_transfer(env: Env) -> None:
    assert (await env.post("/referral/transfer", {"amount_kop": 100})).status_code == 404  # not partner mode
    env.ctx.box.update(referral_reward_mode="partner")
    poor = await env.post("/referral/transfer", {"amount_kop": 100_00})
    assert poor.status_code == 402 and poor.json()["error"] == "insufficient_funds"
    assert poor.json()["message"] == "На партнёрском балансе недостаточно средств."

    await credit_partner(env, 300_00)
    payload = {"amount_kop": 100_00, "request_id": "retry-safe-1"}
    first = (await env.post("/referral/transfer", payload)).json()
    assert first == {"amount_kop": 100_00, "applied": True, "balance_kop": 100_00, "partner_balance_kop": 200_00}
    again = (await env.post("/referral/transfer", payload)).json()
    assert again["applied"] is False and again["partner_balance_kop"] == 200_00
    bad = await env.post("/referral/transfer", {"amount_kop": 100, "request_id": "no spaces!"})
    assert bad.status_code == 422


async def test_partner_withdrawals(env: Env) -> None:
    env.ctx.box.update(referral_reward_mode="partner")
    request = {"amount_kop": 600_00, "method": "card", "details": "2200 0000 0000 0000"}
    off = await env.post("/referral/withdrawals", request)
    assert off.status_code == 404 and off.json()["error"] == "disabled"

    env.ctx.box.update(withdrawals_enabled=True)
    await credit_partner(env, 1000_00)
    small = await env.post("/referral/withdrawals", {**request, "amount_kop": 100_00})
    assert small.status_code == 400 and small.json()["message"] == "Минимальная сумма вывода — 500 ₽."

    resp = await env.post("/referral/withdrawals", request)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending" and body["amount_kop"] == 600_00 and body["method"] == "card"
    event, text = env.notified[-1]
    assert event is AdminEvent.WITHDRAWALS and f"#{body['id']}" in text and str(USER_ID) in text
    assert "2200" not in text  # payout details stay out of notifications

    dup = await env.post("/referral/withdrawals", request)
    assert dup.status_code == 409 and dup.json()["error"] == "pending_exists"

    info = (await env.get("/referral")).json()["partner"]
    assert info["balance_kop"] == 400_00 and info["pending_withdrawal_kop"] == 600_00
    assert info["withdrawals_available"] is True and info["methods"] == ["card", "sbp", "usdt_trc20"]
    assert [w["id"] for w in info["withdrawals"]] == [body["id"]]


# ---------------------------------------------------------------------------------------------
# Promo & gift codes
# ---------------------------------------------------------------------------------------------
async def test_promo_redeem(env: Env) -> None:
    async with env.ctx.session_factory.begin() as session:
        await promo.create_promo(session, code="WELCOME", bonus_kop=100_00, bonus_days=7)
    resp = await env.post("/promo/redeem", {"code": " welcome "})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "promo" and body["bonus_kop"] == 100_00 and body["bonus_days"] == 7
    assert body["balance_kop"] == 100_00 and body["expires_at"]
    assert body["message"] == "Промокод активирован 🎉 Вы получили: 100 ₽ на баланс, 7 дней к подписке."
    card = (await env.get("/subscription")).json()
    assert card["state"] == "active" and card["days_left"] == 7

    again = await env.post("/promo/redeem", {"code": "WELCOME"})
    assert again.status_code == 409 and again.json()["error"] == "promo_already_used"
    unknown = await env.post("/promo/redeem", {"code": "NOPE"})
    assert unknown.status_code == 404 and unknown.json()["message"].startswith("Такого кода нет")


async def test_promo_redeem_is_rate_limited(env: Env) -> None:
    for _ in range(10):
        assert (await env.post("/promo/redeem", {"code": "GUESS"})).status_code == 404
    assert (await env.post("/promo/redeem", {"code": "GUESS"})).status_code == 429


async def test_gift_redeem(env: Env) -> None:
    await add_user(env.ctx.session_factory, env.settings, OTHER_ID)
    async with env.ctx.session_factory.begin() as session:
        plan = await plans.create(session, title="3 месяца", duration_days=90, price_kop=500_00, device_limit=5)
        await wallet.apply(session, OTHER_ID, 500_00, TxKind.ADMIN_ADJUST, "gift-money")
        buyer = await session.get(User, OTHER_ID)
        assert buyer is not None
        gift = await promo.create_gift(session, env.settings, buyer, plan, "gift-ref")
        code = gift.code

    env.ctx.box.update(gifts_enabled=False)
    hidden = await env.post("/promo/redeem", {"code": code})
    assert hidden.status_code == 404 and hidden.json()["error"] == "promo_not_found"

    env.ctx.box.update(gifts_enabled=True)
    resp = await env.post("/promo/redeem", {"code": code})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "gift" and body["bonus_days"] == 90
    assert body["message"].startswith("Подарок активирован 🎁")
    card = (await env.get("/subscription")).json()
    assert card["device_limit"] == 5 and card["plan"]["id"] == plan.id


async def test_redeem_when_codes_are_off(env: Env) -> None:
    env.ctx.box.update(app_mode="personal")
    await add_user(env.ctx.session_factory, env.settings)
    await add_member(env.ctx.session_factory)
    resp = await env.post("/promo/redeem", {"code": "ANY"})
    assert resp.status_code == 404 and resp.json()["error"] == "feature_disabled"
