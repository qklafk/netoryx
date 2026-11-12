"""Core services (wallet, users, referrals, promo, kv, stats) against real PostgreSQL and MySQL.

Skipped unless ``CORE_DB_INTEGRATION=1``. Requires Docker; containers are started with testcontainers:

    CORE_DB_INTEGRATION=1 .venv/Scripts/python -m pytest -q tests/integration/core

Optional environment:
* ``CORE_IT_DATABASES``       comma-separated subset of ``postgres,mysql`` (default both)
* ``CORE_IT_POSTGRES_IMAGE``  default ``postgres:18.4``
* ``CORE_IT_MYSQL_IMAGE``     default ``mysql:8.4``

These tests exercise what SQLite cannot: real row locks, concurrent unique-index races, REPEATABLE READ
snapshots (MySQL), InnoDB deadlock-prone lock orders and dialect differences in aggregates.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import BalanceTransaction, Base, Plan, PromoActivation, PromoCode, TxKind, User
from app.db.session import SessionFactory, create_engine, create_session_factory
from app.services import kv, promo, referrals, stats, users, wallet
from app.services.promo import PromoOutcome
from app.services.referrals import ReferralOutcome
from app.services.wallet import InsufficientFunds
from tests.conftest import make_settings
from tests.test_core_stats import NOW, SETTINGS, seed_dashboard_data

pytestmark = pytest.mark.skipif(
    os.environ.get("CORE_DB_INTEGRATION") != "1", reason="set CORE_DB_INTEGRATION=1 to run (needs Docker)"
)

_DATABASES = [d.strip() for d in os.environ.get("CORE_IT_DATABASES", "postgres,mysql").split(",") if d.strip()]


@pytest.fixture(scope="module", params=_DATABASES)
def database_url(request: pytest.FixtureRequest) -> Iterator[str]:
    container: Any
    if request.param == "postgres":
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer(os.environ.get("CORE_IT_POSTGRES_IMAGE", "postgres:18.4"), driver="asyncpg")
    elif request.param == "mysql":
        from testcontainers.community.mysql import MySqlContainer

        container = MySqlContainer(os.environ.get("CORE_IT_MYSQL_IMAGE", "mysql:8.4"), dialect="aiomysql")
    else:  # pragma: no cover - configuration error
        raise ValueError(f"unknown database {request.param!r}")
    with container:
        yield container.get_connection_url()


@pytest.fixture
async def sf(database_url: str) -> AsyncIterator[SessionFactory]:
    engine = create_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def _users(sf: SessionFactory, *ids: int, balance: int = 0) -> None:
    async with sf.begin() as s:
        for tg_id in ids:
            s.add(User(tg_id=tg_id, referral_code=f"R{tg_id}", sub_token=f"t{tg_id}", balance_kop=balance))


async def _balance(sf: SessionFactory, tg_id: int) -> int:
    async with sf() as s:
        return await wallet.balance(s, tg_id)


async def _count(sf: SessionFactory, model: type) -> int:
    async with sf() as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


# --- wallet ---------------------------------------------------------------------------------------------
async def _apply(sf: SessionFactory, amount: int, kind: TxKind, ref: str, *, read_first: bool) -> object:
    try:
        async with sf.begin() as s:
            if read_first:  # typical handler: load the user, then charge
                await s.get(User, 1)
            return await wallet.apply(s, 1, amount, kind, ref)
    except InsufficientFunds:
        return "insufficient"


@pytest.mark.parametrize("read_first", [False, True])
async def test_wallet_concurrent_charges_never_overdraw(sf: SessionFactory, read_first: bool) -> None:
    await _users(sf, 1, balance=1_000)
    results = await asyncio.gather(
        *(_apply(sf, -100, TxKind.PURCHASE, f"b{i}", read_first=read_first) for i in range(30))
    )
    assert results.count(-100) == 10
    assert results.count("insufficient") == 20
    assert await _balance(sf, 1) == 0
    assert await _count(sf, BalanceTransaction) == 10


@pytest.mark.parametrize("read_first", [False, True])
async def test_wallet_concurrent_duplicates_apply_once(sf: SessionFactory, read_first: bool) -> None:
    await _users(sf, 1)
    results = await asyncio.gather(*(_apply(sf, 500, TxKind.PAYMENT, "pay", read_first=read_first) for _ in range(20)))
    assert results.count(500) == 1 and results.count(None) == 19
    assert await _balance(sf, 1) == 500


async def test_wallet_concurrent_duplicate_charges_draining_balance(sf: SessionFactory) -> None:
    """Replays of a charge that emptied the wallet must report "already applied", not "insufficient"."""
    await _users(sf, 1, balance=300)
    results = await asyncio.gather(*(_apply(sf, -300, TxKind.PURCHASE, "b", read_first=True) for _ in range(10)))
    assert results.count(-300) == 1 and results.count(None) == 9
    assert await _balance(sf, 1) == 0


async def test_wallet_zero_amount_and_unknown_user(sf: SessionFactory) -> None:
    await _users(sf, 1, balance=10)
    async with sf.begin() as s:
        assert await wallet.apply(s, 1, 0, TxKind.PURCHASE, "free") == 0
        with pytest.raises(LookupError):
            await wallet.apply(s, 404, 5, TxKind.PAYMENT, "p")
    assert await _balance(sf, 1) == 10


async def test_wallet_rolls_back_with_caller(sf: SessionFactory) -> None:
    await _users(sf, 1, balance=1_000)
    with pytest.raises(RuntimeError):
        async with sf.begin() as s:
            await wallet.apply(s, 1, -400, TxKind.PURCHASE, "b")
            raise RuntimeError
    assert await _balance(sf, 1) == 1_000
    assert await _count(sf, BalanceTransaction) == 0


# --- users & referrals ------------------------------------------------------------------------------------
async def _register(sf: SessionFactory, settings: Settings, tg_id: int, code: str | None = None) -> object:
    async with sf.begin() as s:
        user, created = await users.get_or_create(s, settings, tg_id, f"u{tg_id}", "N", "ru")
        if code is None:
            return created
        return (await referrals.apply_on_registration(s, settings, user, code, is_new=created)).outcome


async def test_concurrent_registration_of_one_user(sf: SessionFactory) -> None:
    settings = make_settings(welcome_bonus_rub=10)
    results = await asyncio.gather(*(_register(sf, settings, 42) for _ in range(10)))
    assert results.count(True) == 1
    assert await _count(sf, User) == 1
    assert await _balance(sf, 42) == 1_000


async def test_concurrent_signups_through_one_link(sf: SessionFactory) -> None:
    settings = make_settings(referral_inviter_requires_payment=False, referral_inviter_bonus_rub=10)
    async with sf.begin() as s:
        inviter, _ = await users.get_or_create(s, settings, 1, "inviter", "I", "ru")
    invitees = range(100, 120)
    results = await asyncio.gather(*(_register(sf, settings, tg_id, inviter.referral_code) for tg_id in invitees))
    assert results == [ReferralOutcome.APPLIED] * len(invitees)
    assert await _balance(sf, 1) == 1_000 * len(invitees)
    async with sf() as s:
        assert await referrals.stats(s, 1) == (len(invitees), 1_000 * len(invitees))
        assert [(u.tg_id, n) for u, n in await referrals.top(s)] == [(1, len(invitees))]


# --- promo & gifts --------------------------------------------------------------------------------------
async def _redeem(sf: SessionFactory, user_id: int, code: str) -> PromoOutcome:
    async with sf.begin() as s:
        user = await s.get(User, user_id)
        assert user is not None
        outcome, _ = await promo.redeem(s, make_settings(), user, code)
        return outcome


async def test_concurrent_promo_redemptions(sf: SessionFactory) -> None:
    ids = list(range(1, 16))
    await _users(sf, *ids)
    async with sf.begin() as s:
        await promo.create_promo(s, code="RACE", bonus_kop=100, max_uses=4)
    outcomes = await asyncio.gather(*(_redeem(sf, tg_id, "race") for tg_id in ids))
    assert outcomes.count(PromoOutcome.OK) == 4
    assert outcomes.count(PromoOutcome.EXHAUSTED) == 11
    assert await _count(sf, PromoActivation) == 4
    async with sf() as s:
        assert (await promo.get_by_code(s, "RACE")).uses == 4  # type: ignore[union-attr]


async def test_concurrent_double_tap_promo(sf: SessionFactory) -> None:
    await _users(sf, 1)
    async with sf.begin() as s:
        await promo.create_promo(s, code="TAP", bonus_kop=100, max_uses=0)
    outcomes = await asyncio.gather(*(_redeem(sf, 1, "TAP") for _ in range(8)))
    assert outcomes.count(PromoOutcome.OK) == 1
    assert await _balance(sf, 1) == 100


async def test_concurrent_gift_creation_same_ref(sf: SessionFactory) -> None:
    await _users(sf, 1, balance=100_000)
    async with sf.begin() as s:
        plan = Plan(title="1m", duration_days=30, price_kop=15_000)
        s.add(plan)

    async def gift() -> int:
        async with sf.begin() as s:
            buyer = await s.get(User, 1)
            assert buyer is not None
            return (await promo.create_gift(s, make_settings(), buyer, plan, "pay-1")).id

    ids = await asyncio.gather(*(gift() for _ in range(6)))
    assert len(set(ids)) == 1
    assert await _balance(sf, 1) == 85_000
    assert await _count(sf, PromoCode) == 1


# --- kv & stats -----------------------------------------------------------------------------------------
async def test_kv_secret_and_upsert(sf: SessionFactory) -> None:
    secrets = await asyncio.gather(*(kv.get_or_create_secret(sf) for _ in range(5)))
    assert len(set(secrets)) == 1
    async with sf.begin() as s:
        await kv.set(s, "k", "v")
        await kv.set(s, "k", "v")  # unchanged value: MySQL reports 0 affected rows without FOUND_ROWS
        await kv.set(s, "k", "w")
        assert await kv.get(s, "k") == "w"


async def test_stats_dashboard_and_series(sf: SessionFactory) -> None:
    await seed_dashboard_data(sf)
    async with sf() as s:
        d = await stats.dashboard(s, SETTINGS, NOW)
        series = await stats.daily(s, SETTINGS, days=3, now=NOW)
    assert (d.users_total, d.users_new_today, d.subscriptions_active, d.paying_users) == (6, 2, 3, 4)
    assert (d.revenue_today_kop, d.revenue_total_kop, d.donations_total_kop) == (15_000, 105_000, 10_000)
    assert (d.mrr_kop, d.churned_30d, d.trial_converted, d.wallet_total_kop) == (28_333, 1, 3, 1_500)
    assert all(isinstance(v, int) for v in (d.revenue_total_kop, d.wallet_total_kop, d.mrr_kop))
    assert [(p.provider, p.amount_kop) for p in d.providers_30d] == [
        ("platega", 40_000),
        ("yookassa", 30_000),
        ("cryptobot", 20_000),
    ]
    assert [(x.registrations, x.revenue_kop, x.active_subscriptions) for x in series] == [
        (0, 20_000, 3),
        (1, 0, 2),
        (2, 15_000, 3),
    ]
