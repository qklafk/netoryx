from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import CreateInvoiceLink
from sqlalchemy import select

from app.db.models import Payment, PaymentStatus, PromoCode, PromoKind, TxKind, User
from app.db.session import SessionFactory
from app.services import plans, wallet
from tests.test_mod_miniapp_support import USER_ID, Env, FakeGateway, add_member, add_user, miniapp_env


@pytest.fixture
async def env(tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with miniapp_env(tmp_path, session_factory, monkeypatch) as environment:
        await add_user(environment.ctx.session_factory, environment.settings)
        yield environment


async def make_plans(env: Env) -> dict[str, int]:
    async with env.ctx.session_factory.begin() as session:
        month = await plans.create(session, title="1 месяц", duration_days=30, price_kop=200_00)
        year = await plans.create(session, title="12 месяцев", duration_days=360, price_kop=1800_00, price_stars=999)
        hidden = await plans.create(session, title="Секретный", duration_days=30, price_kop=100_00)
        hidden.is_hidden = True
        archived = await plans.create(session, title="Старый", duration_days=30, price_kop=50_00, is_active=False)
        return {"month": month.id, "year": year.id, "hidden": hidden.id, "archived": archived.id}


async def set_balance(env: Env, amount_kop: int, user_id: int = USER_ID) -> None:
    async with env.ctx.session_factory.begin() as session:
        await wallet.apply(session, user_id, amount_kop, TxKind.ADMIN_ADJUST, f"test:{amount_kop}")


def gateway(env: Env) -> FakeGateway:
    found = env.ctx.gateways.get("cryptobot")
    assert isinstance(found, FakeGateway)
    return found


async def load_payment(env: Env, payment_id: str) -> Payment:
    async with env.ctx.session_factory() as session:
        payment = await session.get(Payment, payment_id)
        assert payment is not None
        return payment


# ---------------------------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------------------------
async def test_plans_list(env: Env) -> None:
    ids = await make_plans(env)
    await set_balance(env, 50_00)
    body = (await env.get("/plans")).json()
    assert [p["id"] for p in body["plans"]] == [ids["month"], ids["year"]]
    month, year = body["plans"]
    assert month["monthly_price_kop"] == 200_00 and month["discount_percent"] == 0
    assert year["monthly_price_kop"] == 150_00 and year["discount_percent"] == 25
    assert year["price_stars"] == 999 and month["price_stars"] == 134  # 200 RUB / 1.5 RUB per Star, rounded up
    assert body["balance_kop"] == 50_00 and body["gifts_available"] is True
    providers = {p["key"]: p for p in body["providers"]}
    assert providers["stars"]["title"] == "⭐ Звёзды Telegram" and providers["stars"]["recurring"] is True
    assert providers["cryptobot"]["recurring"] is False
    assert body["topup"] == {
        "min_kop": 50_00,
        "max_kop": 100_000_00,
        "quick_amounts_kop": [150_00, 300_00, 500_00, 1000_00],
    }

    with_hidden = (await env.get("/plans", params={"plan_id": ids["hidden"]})).json()
    assert ids["hidden"] in [p["id"] for p in with_hidden["plans"]]
    assert next(p for p in with_hidden["plans"] if p["id"] == ids["hidden"])["hidden"] is True
    archived = (await env.get("/plans", params={"plan_id": ids["archived"]})).json()
    assert ids["archived"] not in [p["id"] for p in archived["plans"]]


# ---------------------------------------------------------------------------------------------
# Buying from the balance
# ---------------------------------------------------------------------------------------------
async def test_buy_from_balance(env: Env) -> None:
    ids = await make_plans(env)
    poor = await env.post(f"/plans/{ids['month']}/buy", {})
    assert poor.status_code == 402
    body = poor.json()
    assert body["error"] == "insufficient_funds" and body["missing_kop"] == 200_00 and body["balance_kop"] == 0
    assert "200 ₽" in body["message"]

    await set_balance(env, 250_00)
    resp = await env.post(f"/plans/{ids['month']}/buy", {})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "purchased" and body["balance_kop"] == 50_00 and body["expires_at"]
    card = (await env.get("/subscription")).json()
    assert card["state"] == "active" and card["plan"]["id"] == ids["month"] and card["days_left"] == 30


async def test_buy_unknown_or_archived_plan(env: Env) -> None:
    ids = await make_plans(env)
    for plan_id in (ids["archived"], 9999):
        resp = await env.post(f"/plans/{plan_id}/buy", {})
        assert resp.status_code == 404 and resp.json()["error"] == "plan_not_found"


async def test_buy_gift_from_balance(env: Env) -> None:
    ids = await make_plans(env)
    await set_balance(env, 200_00)
    resp = await env.post(f"/plans/{ids['month']}/buy", {"gift": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "gift" and body["balance_kop"] == 0
    gift = body["gift"]
    assert gift["plan"]["id"] == ids["month"]
    assert gift["link"] == f"https://t.me/test_vpn_bot?start=gift_{gift['code']}"
    async with env.ctx.session_factory() as session:
        row = await session.get(PromoCode, 1)
        assert row is not None and row.kind is PromoKind.GIFT and row.code == gift["code"]

    env.ctx.box.update(gifts_enabled=False)
    off = await env.post(f"/plans/{ids['month']}/buy", {"gift": True})
    assert off.status_code == 404 and off.json()["error"] == "feature_disabled"


async def test_buy_from_balance_retry_charges_once(env: Env) -> None:
    ids = await make_plans(env)
    await set_balance(env, 600_00)
    first = await env.post(f"/plans/{ids['month']}/buy", {"request_id": "tap-0001abcd"})
    assert first.status_code == 200, first.text
    retry = await env.post(f"/plans/{ids['month']}/buy", {"request_id": "tap-0001abcd"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["balance_kop"] == 400_00 and retry.json()["expires_at"] == first.json()["expires_at"]
    card = (await env.get("/subscription")).json()
    assert card["days_left"] == 30

    again = await env.post(f"/plans/{ids['month']}/buy", {"request_id": "tap-0002abcd"})
    assert again.json()["balance_kop"] == 200_00
    bad = await env.post(f"/plans/{ids['month']}/buy", {"request_id": "x" * 33})
    assert bad.status_code == 422


async def test_gift_from_balance_retry_issues_one_code(env: Env) -> None:
    ids = await make_plans(env)
    await set_balance(env, 400_00)
    first = await env.post(f"/plans/{ids['month']}/buy", {"gift": True, "request_id": "gift-req-1"})
    retry = await env.post(f"/plans/{ids['month']}/buy", {"gift": True, "request_id": "gift-req-1"})
    assert first.status_code == retry.status_code == 200, retry.text
    assert retry.json()["gift"]["code"] == first.json()["gift"]["code"] and retry.json()["balance_kop"] == 200_00

    # The same request id of another user is a different request.
    await add_user(env.ctx.session_factory, env.settings, USER_ID + 1)
    await set_balance(env, 200_00, USER_ID + 1)
    other = await env.post(
        f"/plans/{ids['month']}/buy", {"gift": True, "request_id": "gift-req-1"}, user_id=USER_ID + 1
    )
    assert other.status_code == 200 and other.json()["gift"]["code"] != first.json()["gift"]["code"]
    assert other.json()["balance_kop"] == 0


# ---------------------------------------------------------------------------------------------
# Buying through a provider
# ---------------------------------------------------------------------------------------------
async def test_buy_with_provider_pays_the_missing_amount(env: Env) -> None:
    ids = await make_plans(env)
    await set_balance(env, 150_00)
    resp = await env.post(f"/plans/{ids['month']}/buy", {"provider": "cryptobot"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "payment" and body["balance_kop"] == 150_00
    payment = body["payment"]
    assert payment["amount_kop"] == 50_00 and payment["plan_id"] == ids["month"] and payment["status"] == "pending"
    assert payment["url"] == f"https://pay.example/{payment['id']}" and payment["invoice_link"] is None
    assert gateway(env).created == ["TestVPN: подписка «1 месяц»"]

    # Below the provider minimum: the minimum is charged, the rest stays on the balance.
    await set_balance(env, 49_50)
    small = (await env.post(f"/plans/{ids['month']}/buy", {"provider": "cryptobot"})).json()
    assert small["payment"]["amount_kop"] == 100


async def test_buy_with_stars_returns_an_invoice_link(env: Env) -> None:
    ids = await make_plans(env)
    resp = await env.post(f"/plans/{ids['month']}/buy", {"provider": "stars", "recurring": True})
    assert resp.status_code == 200, resp.text
    payment = resp.json()["payment"]
    assert payment["invoice_link"] == "https://t.me/$invoice1"
    assert payment["amount_kop"] == 200_00 and payment["provider_amount"] == "134" and payment["is_recurring"]
    params = env.ctx.bot.invoice_links[0]
    assert params["payload"] == payment["id"] and params["currency"] == "XTR"
    assert params["subscription_period"] == 2_592_000

    not_monthly = await env.post(f"/plans/{ids['year']}/buy", {"provider": "stars", "recurring": True})
    assert not_monthly.status_code == 400 and not_monthly.json()["error"] == "recurring_unavailable"
    not_stars = await env.post(f"/plans/{ids['month']}/buy", {"provider": "cryptobot", "recurring": True})
    assert not_stars.status_code == 400


async def test_stars_invoice_failure_cancels_the_payment(env: Env) -> None:
    ids = await make_plans(env)

    async def broken(**_: Any) -> str:
        raise TelegramBadRequest(
            CreateInvoiceLink(title="t", description="d", payload="p", currency="XTR", prices=[]), "bad"
        )

    env.ctx.bot.create_invoice_link = broken  # type: ignore[method-assign]
    resp = await env.post(f"/plans/{ids['year']}/buy", {"provider": "stars"})
    assert resp.status_code == 502 and resp.json()["error"] == "provider_unavailable"
    async with env.ctx.session_factory() as session:
        rows = list(await session.scalars(select(Payment)))
    assert [row.status for row in rows] == [PaymentStatus.CANCELED]


async def test_provider_errors(env: Env) -> None:
    ids = await make_plans(env)
    unknown = await env.post(f"/plans/{ids['month']}/buy", {"provider": "paypal"})
    assert unknown.status_code == 400 and unknown.json()["error"] == "unknown_provider"

    gateway(env).fail_create = True
    down = await env.post(f"/plans/{ids['month']}/buy", {"provider": "cryptobot"})
    assert down.status_code == 502 and down.json()["error"] == "provider_unavailable"
    async with env.ctx.session_factory() as session:
        rows = list(await session.scalars(select(Payment)))
    assert [row.status for row in rows] == [PaymentStatus.CANCELED]


async def test_payment_creation_is_rate_limited(env: Env) -> None:
    ids = await make_plans(env)
    for _ in range(10):
        assert (await env.post(f"/plans/{ids['month']}/buy", {"provider": "cryptobot"})).status_code == 200
    limited = await env.post("/wallet/topup", {"amount_kop": 100_00, "provider": "cryptobot"})
    assert limited.status_code == 429 and limited.json()["retry_after"] > 0


# ---------------------------------------------------------------------------------------------
# Top-up, payment status, wallet
# ---------------------------------------------------------------------------------------------
async def test_topup_limits(env: Env) -> None:
    low = await env.post("/wallet/topup", {"amount_kop": 10_00, "provider": "cryptobot"})
    assert low.status_code == 400
    body = low.json()
    assert body["error"] == "topup_range" and body["min_kop"] == 50_00
    assert body["message"] == "Сумма пополнения — от 50 ₽ до 100 000 ₽."
    assert (await env.post("/wallet/topup", {"amount_kop": 0, "provider": "cryptobot"})).status_code == 422

    ok = await env.post("/wallet/topup", {"amount_kop": 300_00, "provider": "cryptobot"})
    assert ok.status_code == 200 and ok.json()["amount_kop"] == 300_00 and ok.json()["plan_id"] is None
    assert gateway(env).created == ["Пополнение баланса TestVPN"]


async def test_payment_status_polls_the_provider(env: Env) -> None:
    created = (await env.post("/wallet/topup", {"amount_kop": 300_00, "provider": "cryptobot"})).json()
    pending = await env.get(f"/payments/{created['id']}")
    assert pending.status_code == 200 and pending.json()["status"] == "pending"
    assert gateway(env).fetched == [created["id"]]

    # Checked at most once per 5 seconds per payment.
    gateway(env).status = PaymentStatus.SUCCEEDED
    assert (await env.get(f"/payments/{created['id']}")).json()["status"] == "pending"
    assert len(gateway(env).fetched) == 1

    await env.app.state.rate_limiter.reset(f"miniapp:paycheck:{created['id']}")
    paid = (await env.get(f"/payments/{created['id']}")).json()
    assert paid["status"] == "succeeded" and paid["credited_at"]
    assert [r.payment_id for r in env.ctx.credited] == [created["id"]]
    async with env.ctx.session_factory() as session:
        user = await session.get(User, USER_ID)
        assert user is not None and user.balance_kop == 300_00


async def test_payment_of_another_user_is_hidden(env: Env) -> None:
    created = (await env.post("/wallet/topup", {"amount_kop": 300_00, "provider": "cryptobot"})).json()
    await add_user(env.ctx.session_factory, env.settings, 5555)
    other = await env.get(f"/payments/{created['id']}", user_id=5555)
    assert other.status_code == 404 and other.json()["error"] == "payment_not_found"
    assert (await env.get("/payments/" + "x" * 80)).status_code == 404


async def test_wallet_history_pages(env: Env) -> None:
    for amount in (100_00, 200_00, 300_00):
        await set_balance(env, amount)
    first = (await env.get("/wallet", params={"limit": 2})).json()
    assert first["balance_kop"] == 600_00 and first["partner_balance_kop"] == 0
    assert [i["amount_kop"] for i in first["items"]] == [300_00, 200_00]
    assert first["items"][0]["title"] == "Корректировка" and first["items"][0]["kind"] == "admin_adjust"
    second = (await env.get("/wallet", params={"limit": 2, "before_id": first["next_before_id"]})).json()
    assert [i["amount_kop"] for i in second["items"]] == [100_00] and second["next_before_id"] is None


async def test_selling_is_off_in_personal_mode(env: Env) -> None:
    env.ctx.box.update(app_mode="personal")
    await add_member(env.ctx.session_factory)
    for path in ("/plans", "/wallet"):
        assert (await env.get(path)).status_code == 404
    assert (await env.post("/wallet/topup", {"amount_kop": 100_00, "provider": "cryptobot"})).status_code == 404
