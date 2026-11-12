from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.db.models import PromoGroup, PromoGroupMember, Subscription, SubscriptionStatus
from app.db.session import SessionFactory
from app.services import plans
from app.services.invites import UNLIMITED_EXPIRES_AT
from tests.test_mod_miniapp_shop import set_balance
from tests.test_mod_miniapp_support import USER_ID, Env, add_user, miniapp_env


@pytest.fixture
async def env(tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with miniapp_env(tmp_path, session_factory, monkeypatch) as environment:
        await add_user(environment.ctx.session_factory, environment.settings)
        yield environment


async def _plan(env: Env) -> int:
    async with env.ctx.session_factory.begin() as session:
        return (await plans.create(session, title="Month", duration_days=30, price_kop=200_00)).id


async def _half_price_group(env: Env) -> None:
    async with env.ctx.session_factory.begin() as session:
        group = PromoGroup(title="friends", discount_percent=50)
        session.add(group)
        await session.flush()
        session.add(PromoGroupMember(group_id=group.id, user_id=USER_ID))


async def test_plans_expose_the_full_price_charged_for_a_gift(env: Env) -> None:
    plan_id = await _plan(env)
    await _half_price_group(env)
    item = (await env.get("/plans")).json()["plans"][0]
    assert item["id"] == plan_id
    assert item["price_kop"] == 100_00
    assert item["gift_price_kop"] == 200_00
    assert item["gift_price_stars"] >= item["price_stars"]


async def test_gift_shortfall_is_computed_from_the_full_price(env: Env) -> None:
    plan_id = await _plan(env)
    await _half_price_group(env)
    await set_balance(env, 150_00)
    resp = await env.post(f"/plans/{plan_id}/buy", {"gift": True})
    assert resp.status_code == 402
    assert resp.json()["missing_kop"] == 50_00


async def test_unlimited_subscription_cannot_be_paid_for(env: Env) -> None:
    plan_id = await _plan(env)
    await set_balance(env, 500_00)
    async with env.ctx.session_factory.begin() as session:
        sub = Subscription(
            user_id=USER_ID,
            client_uuid="unlimited-member",
            status=SubscriptionStatus.ACTIVE,
            expires_at=UNLIMITED_EXPIRES_AT,
        )
        session.add(sub)
        await session.flush()
        sub_id = sub.id
    resp = await env.post(f"/plans/{plan_id}/buy", {"subscription_id": sub_id})
    assert resp.status_code == 409 and resp.json()["error"] == "renew_unavailable"
    resp = await env.post(f"/plans/{plan_id}/buy", {"subscription_id": sub_id, "provider": "cryptobot"})
    assert resp.status_code == 409
    assert (await env.get("/plans")).json()["balance_kop"] == 500_00
