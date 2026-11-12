"""plans, kv, events and audit services."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import pytest
from sqlalchemy import select

from app.config import PlanSeed
from app.db.models import AdminAuditLog, Event, KeyValue, Plan, PurchaseKind, User
from app.db.session import SessionFactory
from app.services import audit, events, kv, plans
from app.services import subscriptions as subs
from app.utils.time import MAX_PERIOD_DAYS, utcnow
from tests.conftest import make_settings


# --- plans ------------------------------------------------------------------------------------------
async def test_seed_defaults_only_once(session_factory: SessionFactory) -> None:
    settings = make_settings()
    async with session_factory.begin() as s:
        assert await plans.seed_defaults(s, settings) == 4
    async with session_factory.begin() as s:
        assert await plans.seed_defaults(s, settings) == 0
        active = await plans.list_active(s)
    assert [(p.title, p.duration_days, p.price_kop) for p in active] == [
        ("1 месяц", 30, 15_000),
        ("3 месяца", 90, 40_000),
        ("6 месяцев", 180, 75_000),
        ("12 месяцев", 365, 140_000),
    ]
    assert [p.sort_order for p in active] == [0, 1, 2, 3]


async def test_seed_defaults_custom_plans(session_factory: SessionFactory) -> None:
    settings = make_settings(default_plans=[PlanSeed(title="Week", days=7, price_rub=50, devices=1, traffic_gb=100)])
    async with session_factory.begin() as s:
        assert await plans.seed_defaults(s, settings) == 1
        (plan,) = await plans.list_all(s)
    assert (plan.device_limit, plan.traffic_limit_gb, plan.price_kop) == (1, 100, 5_000)


async def test_create_update_archive(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        a = await plans.create(s, title=" Month ", duration_days=30, price_kop=15_000)
        b = await plans.create(s, title="Year", duration_days=365, price_kop=140_000, price_stars=900)
        assert (a.title, a.sort_order, b.sort_order) == ("Month", 0, 1)
        await plans.update(s, a, price_kop=17_000, title="Month+")
        await plans.archive(s, b)
    async with session_factory() as s:
        assert [p.title for p in await plans.list_active(s)] == ["Month+"]
        assert len(await plans.list_all(s)) == 2
        archived = await plans.get(s, b.id)
        assert archived is not None and not archived.is_active
        assert await plans.get(s, 999) is None


@pytest.mark.parametrize(
    "fields",
    [
        {"title": ""},
        {"title": "x" * 65},
        {"duration_days": 0},
        {"price_kop": -1},
        {"device_limit": -1},
        {"traffic_limit_gb": -1},
        {"price_stars": 0},
        {"duration_days": 1.5},
        {"duration_days": MAX_PERIOD_DAYS + 1},
        {"duration_days": 3_650_000},
        {"price_kop": True},
    ],
)
async def test_invalid_plan_fields(session_factory: SessionFactory, fields: dict[str, Any]) -> None:
    async with session_factory.begin() as s:
        plan = await plans.create(s, title="Ok", duration_days=30, price_kop=100)
        with pytest.raises(ValueError):
            await plans.update(s, plan, **fields)
        with pytest.raises(ValueError):
            await plans.create(s, **{"title": "T", "duration_days": 30, "price_kop": 100, **fields})


async def test_plan_duration_upper_bound_is_accepted(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        plan = await plans.create(s, title="Max", duration_days=MAX_PERIOD_DAYS, price_kop=100)
        assert plan.duration_days == MAX_PERIOD_DAYS


def test_period_cap_keeps_far_future_expiry_in_range() -> None:
    """Even several maximal periods stacked on top of each other still fit in a datetime (no OverflowError)."""
    assert (utcnow() + timedelta(days=MAX_PERIOD_DAYS) * 50).year < 9999


async def test_grant_days_rejects_oversized_period(session_factory: SessionFactory) -> None:
    """An admin typo (an extra zero) is a ValueError up front, not an OverflowError deep inside _extend."""
    settings = make_settings()
    async with session_factory.begin() as s:
        user = User(tg_id=1, referral_code="R1", sub_token="t1")
        s.add(user)
        await s.flush()
        with pytest.raises(ValueError, match="days"):
            await subs.grant_days(s, settings, user, 3_650_000, kind=PurchaseKind.ADMIN_GRANT)
        purchase = await subs.grant_days(s, settings, user, MAX_PERIOD_DAYS, kind=PurchaseKind.ADMIN_GRANT)
        assert purchase.days == MAX_PERIOD_DAYS


async def test_update_rejects_unknown_fields(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        plan = await plans.create(s, title="Ok", duration_days=30, price_kop=100)
        with pytest.raises(ValueError, match="id"):
            await plans.update(s, plan, id=5)


def _plan(price_kop: int, price_stars: int | None = None, days: int = 30) -> Plan:
    return Plan(title="p", duration_days=days, price_kop=price_kop, price_stars=price_stars)


@pytest.mark.parametrize(
    ("price_kop", "price_stars", "rate", "stars"),
    [
        (15_000, None, 1.5, 100),
        (15_000, 77, 1.5, 77),  # explicit price wins
        (10_000, None, 1.5, 67),  # 66.67 -> rounded up
        (1, None, 1.5, 1),
        (0, None, 1.5, 0),  # free plan
        (40_000, None, 1.3, 308),  # 307.69 -> 308; float rate handled exactly
        (13_000, None, 1.3, 100),  # exact: 130 / 1.3 = 100 (a float division would give 100.00000000000001)
    ],
)
def test_stars_price(price_kop: int, price_stars: int | None, rate: float, stars: int) -> None:
    settings = make_settings(stars_rub_per_star=rate)
    assert plans.stars_price(_plan(price_kop, price_stars), settings) == stars


def test_monthly_price() -> None:
    assert plans.monthly_price_kop(_plan(15_000)) == 15_000
    assert plans.monthly_price_kop(_plan(140_000, days=365)) == 11_507
    assert plans.monthly_price_kop(_plan(40_000, days=90)) == 13_333


# --- kv ---------------------------------------------------------------------------------------------
async def test_kv_set_get_overwrite_delete(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        assert await kv.get(s, "k") is None
        await kv.set(s, "k", "v1")
        await kv.set(s, "k", "v2")
        assert await kv.get(s, "k") == "v2"
        await kv.set(s, "k", "v2")  # same value again
        assert await kv.delete(s, "k")
        assert not await kv.delete(s, "k")
        assert await kv.get(s, "k") is None
    with pytest.raises(ValueError):
        async with session_factory.begin() as s:
            await kv.set(s, "x" * 65, "v")


async def test_kv_concurrent_set_same_key(session_factory: SessionFactory) -> None:
    async def write(i: int) -> None:
        async with session_factory.begin() as s:
            await kv.set(s, "shared", str(i))

    await asyncio.gather(*(write(i) for i in range(10)))
    async with session_factory() as s:
        rows = (await s.scalars(select(KeyValue).where(KeyValue.key == "shared"))).all()
    assert len(rows) == 1 and rows[0].value in {str(i) for i in range(10)}


async def test_kv_add_inserts_only_when_absent(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        assert await kv.add(s, "new", "v1")
        assert not await kv.add(s, "new", "v2")  # the transaction stays usable after the conflict
        assert await kv.get(s, "new") == "v1"
    with pytest.raises(ValueError):
        async with session_factory.begin() as s:
            await kv.add(s, "", "v")


async def test_maintenance_flag(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        assert not await kv.is_maintenance(s)
        await kv.set_maintenance(s, True)
        assert await kv.is_maintenance(s)
        await kv.set_maintenance(s, False)
        assert not await kv.is_maintenance(s)


async def test_secret_prefers_configured_key(session_factory: SessionFactory) -> None:
    settings = make_settings(secret_key="configured-secret")
    assert await kv.get_or_create_secret(session_factory, settings) == "configured-secret"
    async with session_factory() as s:
        assert await kv.get(s, kv.SECRET_KEY) is None


async def test_secret_is_generated_once_and_persisted(session_factory: SessionFactory) -> None:
    settings = make_settings(secret_key=None)
    results = await asyncio.gather(*(kv.get_or_create_secret(session_factory, settings) for _ in range(5)))
    assert len(set(results)) == 1 and len(results[0]) >= 40
    assert await kv.get_or_create_secret(session_factory) == results[0]


# --- events -----------------------------------------------------------------------------------------
class _Color(StrEnum):
    RED = "red"


async def test_track_serializes_data(session_factory: SessionFactory) -> None:
    when = datetime(2026, 1, 2, tzinfo=UTC)
    async with session_factory.begin() as s:
        await events.track(s, "purchase", user_id=5, amount_kop=15_000, plan_id=1, at=when, color=_Color.RED)
        await events.track(s, "bare")
    async with session_factory() as s:
        rows = (await s.scalars(select(Event).order_by(Event.id))).all()
    assert (rows[0].type, rows[0].user_id, rows[0].amount_kop) == ("purchase", 5, 15_000)
    assert json.loads(rows[0].data or "") == {"at": "2026-01-02 00:00:00+00:00", "color": "red", "plan_id": 1}
    assert (rows[1].user_id, rows[1].amount_kop, rows[1].data) == (None, None, None)


@pytest.mark.parametrize("event_type", ["", "x" * 49])
async def test_track_validates_type(session_factory: SessionFactory, event_type: str) -> None:
    async with session_factory.begin() as s:
        with pytest.raises(ValueError):
            await events.track(s, event_type)


# --- audit ------------------------------------------------------------------------------------------
async def test_audit_log_redacts_secrets(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await audit.log(
            s,
            1000,
            "panel.update",
            "panel",
            7,
            source="web",
            ip="10.0.0.1",
            url="https://p",
            password="hunter2",
            api_token="tok",
            nested={"X-Api-Key": "k", "Cookie": "c", "ok": 1},
            empty_secret="",
        )
    async with session_factory() as s:
        entry = await s.scalar(select(AdminAuditLog))
    assert entry is not None
    assert (entry.actor_id, entry.action, entry.target_type, entry.target_id) == (1000, "panel.update", "panel", "7")
    assert (entry.source, entry.ip) == ("web", "10.0.0.1")
    details = json.loads(entry.details or "")
    assert details["password"] == details["api_token"] == "***"
    assert details["nested"] == {"X-Api-Key": "***", "Cookie": "***", "ok": 1}
    assert details["empty_secret"] == "" and details["url"] == "https://p"
    assert "hunter2" not in (entry.details or "")


def test_audit_redact_covers_lists_and_key_styles() -> None:
    details: dict[str, Any] = {
        "realitySettings": {"privateKey": "abc", "publicKey": "pub"},
        "panels": [{"name": "de", "password": "p"}, ("x", {"Passphrase": "pp"}), "plain"],
        "db_pwd": "pw",
        "apiKey": "k",
    }
    clean = audit.redact(details)
    assert clean["realitySettings"] == {"privateKey": "***", "publicKey": "pub"}
    assert clean["panels"] == [{"name": "de", "password": "***"}, ["x", {"Passphrase": "***"}], "plain"]
    assert clean["db_pwd"] == clean["apiKey"] == "***"
    assert details["panels"][0]["password"] == "p"  # the input is not modified


async def test_audit_recent_filters_and_paginates(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        for i in range(5):
            await audit.log(s, 1000 + i % 2, f"user.action{i}", "user", 42 if i < 3 else 43)
        with pytest.raises(ValueError):
            await audit.log(s, 1000, "")
    async with session_factory() as s:
        page = await audit.recent(s, limit=2)
        assert [e.action for e in page] == ["user.action4", "user.action3"]
        older = await audit.recent(s, limit=10, before_id=page[-1].id)
        assert [e.action for e in older] == ["user.action2", "user.action1", "user.action0"]
        assert len(await audit.recent(s, actor_id=1001)) == 2
        assert len(await audit.recent(s, target_type="user", target_id=42)) == 3
        entry = (await audit.recent(s, limit=1))[0]
        assert entry.details is None
