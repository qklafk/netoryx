from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.db.session import SessionFactory
from app.services import plans
from tests.test_mod_miniapp_shop import set_balance
from tests.test_mod_miniapp_support import Env, add_user, miniapp_env


@pytest.fixture
async def env(tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with miniapp_env(tmp_path, session_factory, monkeypatch) as environment:
        await add_user(environment.ctx.session_factory, environment.settings)
        yield environment


async def _hidden_plan(env: Env) -> int:
    async with env.ctx.session_factory.begin() as session:
        plan = await plans.create(session, title="Friends", duration_days=30, price_kop=100_00)
        plan.is_hidden = True
        return plan.id


async def test_hidden_plan_is_not_listed_by_its_id_alone(env: Env) -> None:
    plan_id = await _hidden_plan(env)
    resp = await env.get("/plans", params={"plan_id": plan_id})
    assert [p["id"] for p in resp.json()["plans"]] == []


async def test_hidden_plan_is_listed_with_its_link_token(env: Env) -> None:
    plan_id = await _hidden_plan(env)
    token = plans.link_token(env.ctx.secret_box, plan_id)
    resp = await env.get("/plans", params={"plan_id": plan_id, "plan_token": token})
    assert [p["id"] for p in resp.json()["plans"]] == [plan_id]


async def test_hidden_plan_cannot_be_bought_without_its_link_token(env: Env) -> None:
    plan_id = await _hidden_plan(env)
    await set_balance(env, 500_00)
    resp = await env.post(f"/plans/{plan_id}/buy", {})
    assert resp.status_code == 404
    token = plans.link_token(env.ctx.secret_box, plan_id)
    resp = await env.post(f"/plans/{plan_id}/buy", {"plan_token": token})
    assert resp.status_code == 200


async def test_hidden_plan_cannot_be_gifted_from_the_mini_app(env: Env) -> None:
    plan_id = await _hidden_plan(env)
    await set_balance(env, 500_00)
    token = plans.link_token(env.ctx.secret_box, plan_id)
    resp = await env.post(f"/plans/{plan_id}/buy", {"gift": True, "plan_token": token})
    assert resp.status_code == 404
