from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, select

from app.config import Settings
from app.db.models import (
    BalanceTransaction,
    Event,
    Payment,
    PaymentStatus,
    Plan,
    PromoActivation,
    PromoCode,
    PromoKind,
    Purchase,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    TxKind,
    User,
)
from app.db.session import SessionFactory
from app.services import promo, wallet
from app.services.promo import PromoCodeExists, PromoOutcome
from app.services.subscriptions import SubErrorCode, SubscriptionError
from app.utils.time import MAX_PERIOD_DAYS, utcnow


async def _users(session_factory: SessionFactory, *ids: int, balance: int = 0) -> None:
    async with session_factory.begin() as s:
        for tg_id in ids:
            s.add(User(tg_id=tg_id, referral_code=f"R{tg_id}", sub_token=f"t{tg_id}", balance_kop=balance))


async def _promo(session_factory: SessionFactory, **kwargs: Any) -> PromoCode:
    kwargs.setdefault("bonus_kop", 1_000)
    async with session_factory.begin() as s:
        return await promo.create_promo(s, **kwargs)


async def _plan(session_factory: SessionFactory, price_kop: int = 15_000) -> Plan:
    async with session_factory.begin() as s:
        plan = Plan(title="1 месяц", duration_days=30, price_kop=price_kop, device_limit=3)
        s.add(plan)
    return plan


async def _redeem(
    session_factory: SessionFactory, settings: Settings, user_id: int, code: str
) -> tuple[PromoOutcome, PromoCode | None]:
    async with session_factory.begin() as s:
        user = await s.get(User, user_id)
        assert user is not None
        return await promo.redeem(s, settings, user, code)


async def _balance(session_factory: SessionFactory, user_id: int) -> int:
    async with session_factory() as s:
        return await wallet.balance(s, user_id)


async def _count(session_factory: SessionFactory, model: type, *where: Any) -> int:
    async with session_factory() as s:
        return int(await s.scalar(select(func.count()).select_from(model).where(*where)) or 0)


# --- creation ---------------------------------------------------------------------------------------
async def test_create_promo_normalizes_and_validates(session_factory: SessionFactory) -> None:
    created = await _promo(session_factory, code=" summer-2026 ", bonus_kop=500, max_uses=0)
    assert created.code == "SUMMER-2026"
    assert (created.kind, created.uses, created.max_uses, created.is_active) == (PromoKind.PROMO, 0, 0, True)
    cyrillic = await _promo(session_factory, code="лето", bonus_days=7)
    assert cyrillic.code == "ЛЕТО"
    generated = await _promo(session_factory)
    assert len(generated.code) == promo.GENERATED_CODE_LENGTH
    with pytest.raises(PromoCodeExists):
        await _promo(session_factory, code="Summer-2026")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bonus_kop": 0, "bonus_days": 0},
        {"bonus_kop": -1},
        {"bonus_days": -1, "bonus_kop": 0},
        {"bonus_days": MAX_PERIOD_DAYS + 1},
        {"max_uses": -1},
        {"code": "ab"},
        {"code": "has space"},
        {"code": "x" * 65},
        {"expires_at": utcnow().replace(tzinfo=None)},
    ],
)
async def test_create_promo_rejects_invalid(session_factory: SessionFactory, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await _promo(session_factory, **kwargs)


# --- redemption -------------------------------------------------------------------------------------
async def test_redeem_credits_wallet_once(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1)
    await _promo(session_factory, code="BONUS", bonus_kop=2_500, bonus_days=3, max_uses=10)
    outcome, code = await _redeem(session_factory, settings, 1, " bonus ")
    assert outcome is PromoOutcome.OK
    assert code is not None and code.uses == 1 and code.bonus_days == 3
    assert await _balance(session_factory, 1) == 2_500
    assert (await _redeem(session_factory, settings, 1, "BONUS"))[0] is PromoOutcome.ALREADY_USED
    assert await _balance(session_factory, 1) == 2_500
    assert await _count(session_factory, PromoActivation) == 1
    assert await _count(session_factory, Event, Event.type == "promo.redeemed") == 1
    async with session_factory() as s:
        tx = await s.scalar(select(BalanceTransaction))
        assert tx is not None and (tx.kind, tx.ref) == (TxKind.PROMO, str(code.id))


async def test_days_only_promo_writes_no_ledger_row(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1)
    await _promo(session_factory, code="DAYS7", bonus_kop=0, bonus_days=7)
    assert (await _redeem(session_factory, settings, 1, "days7"))[0] is PromoOutcome.OK
    assert await _count(session_factory, BalanceTransaction) == 0


@pytest.mark.parametrize("code", ["", "   ", "UNKNOWN"])
async def test_unknown_code(session_factory: SessionFactory, settings: Settings, code: str) -> None:
    await _users(session_factory, 1)
    assert await _redeem(session_factory, settings, 1, code) == (PromoOutcome.NOT_FOUND, None)


async def test_inactive_code_is_not_found(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1)
    created = await _promo(session_factory, code="OFF")
    async with session_factory.begin() as s:
        loaded = await s.get(PromoCode, created.id)
        assert loaded is not None
        await promo.deactivate(s, loaded)
    assert await _redeem(session_factory, settings, 1, "OFF") == (PromoOutcome.NOT_FOUND, None)


async def test_expired_code(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1)
    await _promo(session_factory, code="OLD", expires_at=utcnow() - timedelta(seconds=1))
    await _promo(session_factory, code="FRESH", expires_at=utcnow() + timedelta(days=1))
    outcome, code = await _redeem(session_factory, settings, 1, "OLD")
    assert outcome is PromoOutcome.EXPIRED and code is not None
    assert (await _redeem(session_factory, settings, 1, "FRESH"))[0] is PromoOutcome.OK


async def test_max_uses_is_enforced(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1, 2, 3)
    await _promo(session_factory, code="TWO", max_uses=2)
    assert (await _redeem(session_factory, settings, 1, "TWO"))[0] is PromoOutcome.OK
    assert (await _redeem(session_factory, settings, 2, "TWO"))[0] is PromoOutcome.OK
    assert (await _redeem(session_factory, settings, 3, "TWO"))[0] is PromoOutcome.EXHAUSTED
    # a user who already used it hears "already used" rather than "exhausted"
    assert (await _redeem(session_factory, settings, 1, "TWO"))[0] is PromoOutcome.ALREADY_USED
    assert await _balance(session_factory, 3) == 0


async def test_unlimited_code(session_factory: SessionFactory, settings: Settings) -> None:
    ids = list(range(1, 8))
    await _users(session_factory, *ids)
    await _promo(session_factory, code="ALL", max_uses=0)
    for tg_id in ids:
        assert (await _redeem(session_factory, settings, tg_id, "ALL"))[0] is PromoOutcome.OK
    async with session_factory() as s:
        assert (await promo.get_by_code(s, "all")).uses == len(ids)  # type: ignore[union-attr]


async def test_only_new_users(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1, 2, 3, 4)
    await _promo(session_factory, code="NEWBIE", only_new_users=True, max_uses=0)
    now = utcnow()
    async with session_factory.begin() as s:
        s.add(Payment(id="pay-2", provider="stars", user_id=2, amount_kop=100, status=PaymentStatus.SUCCEEDED))
        s.add(Purchase(user_id=3, kind=PurchaseKind.PURCHASE, price_kop=100, days=30, expires_after=now))
        # a trial or a canceled payment does not make a user a customer
        s.add(Purchase(user_id=4, kind=PurchaseKind.TRIAL, price_kop=0, days=3, expires_after=now))
        s.add(Payment(id="pay-4", provider="stars", user_id=4, amount_kop=100, status=PaymentStatus.CANCELED))
    assert (await _redeem(session_factory, settings, 1, "NEWBIE"))[0] is PromoOutcome.OK
    assert (await _redeem(session_factory, settings, 2, "NEWBIE"))[0] is PromoOutcome.ONLY_NEW
    assert (await _redeem(session_factory, settings, 3, "NEWBIE"))[0] is PromoOutcome.ONLY_NEW
    assert (await _redeem(session_factory, settings, 4, "NEWBIE"))[0] is PromoOutcome.OK


async def test_concurrent_redemptions_respect_max_uses(session_factory: SessionFactory, settings: Settings) -> None:
    ids = list(range(1, 13))
    await _users(session_factory, *ids)
    await _promo(session_factory, code="RACE", max_uses=3, bonus_kop=100)
    results = await asyncio.gather(*(_redeem(session_factory, settings, tg_id, "RACE") for tg_id in ids))
    outcomes = [o for o, _ in results]
    assert outcomes.count(PromoOutcome.OK) == 3
    assert outcomes.count(PromoOutcome.EXHAUSTED) == 9
    assert await _count(session_factory, PromoActivation) == 3
    assert await _count(session_factory, BalanceTransaction) == 3
    async with session_factory() as s:
        assert (await promo.get_by_code(s, "RACE")).uses == 3  # type: ignore[union-attr]


async def test_concurrent_double_tap_by_one_user(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1)
    await _promo(session_factory, code="TAP", max_uses=0, bonus_kop=100)
    results = await asyncio.gather(*(_redeem(session_factory, settings, 1, "TAP") for _ in range(6)))
    outcomes = [o for o, _ in results]
    assert outcomes.count(PromoOutcome.OK) == 1
    assert outcomes.count(PromoOutcome.ALREADY_USED) == 5
    assert await _balance(session_factory, 1) == 100
    async with session_factory() as s:
        assert (await promo.get_by_code(s, "TAP")).uses == 1  # type: ignore[union-attr]


# --- gifts ------------------------------------------------------------------------------------------
async def _gift(session_factory: SessionFactory, settings: Settings, buyer_id: int, plan: Plan, ref: str) -> PromoCode:
    async with session_factory.begin() as s:
        buyer = await s.get(User, buyer_id)
        assert buyer is not None
        return await promo.create_gift(s, settings, buyer, plan, ref)


async def test_create_gift_charges_wallet(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1, balance=20_000)
    plan = await _plan(session_factory)
    gift = await _gift(session_factory, settings, 1, plan, "pay-1")
    assert (gift.kind, gift.bonus_days, gift.plan_id, gift.max_uses, gift.bonus_kop) == (
        PromoKind.GIFT,
        30,
        plan.id,
        1,
        0,
    )
    assert gift.created_by == 1 and len(gift.code) == promo.GIFT_CODE_LENGTH
    assert await _balance(session_factory, 1) == 5_000
    async with session_factory() as s:
        tx = await s.scalar(select(BalanceTransaction))
        assert tx is not None and (tx.kind, tx.ref, tx.amount_kop) == (TxKind.GIFT_PURCHASE, "gift:pay-1", -15_000)
    assert await _count(session_factory, Event, Event.type == "gift.purchased") == 1


async def test_create_gift_is_idempotent_per_ref(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1, balance=50_000)
    plan = await _plan(session_factory)
    first = await _gift(session_factory, settings, 1, plan, "pay-1")
    again = await _gift(session_factory, settings, 1, plan, "pay-1")
    other = await _gift(session_factory, settings, 1, plan, "pay-2")
    assert again.id == first.id and again.code == first.code
    assert other.id != first.id
    assert await _balance(session_factory, 1) == 20_000
    assert await _count(session_factory, PromoCode) == 2


async def test_concurrent_gift_creation_with_same_ref(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1, balance=100_000)
    plan = await _plan(session_factory)
    gifts = await asyncio.gather(*(_gift(session_factory, settings, 1, plan, "pay-x") for _ in range(5)))
    assert len({g.id for g in gifts}) == 1
    assert await _balance(session_factory, 1) == 85_000
    assert await _count(session_factory, PromoCode) == 1


async def test_create_gift_insufficient_funds_writes_nothing(
    session_factory: SessionFactory, settings: Settings
) -> None:
    await _users(session_factory, 1, balance=14_999)
    plan = await _plan(session_factory)
    with pytest.raises(SubscriptionError) as err:
        await _gift(session_factory, settings, 1, plan, "pay-1")
    assert err.value.code is SubErrorCode.INSUFFICIENT_FUNDS  # same error as subscriptions.purchase_plan
    assert await _balance(session_factory, 1) == 14_999
    assert await _count(session_factory, PromoCode) == 0
    assert await _count(session_factory, BalanceTransaction) == 0


async def _archive(session_factory: SessionFactory, plan: Plan) -> None:
    async with session_factory.begin() as s:
        row = await s.get(Plan, plan.id)
        assert row is not None
        row.is_active = False
    plan.is_active = False


async def test_create_gift_refuses_archived_plan_from_wallet(
    session_factory: SessionFactory, settings: Settings
) -> None:
    """A stale 'gift:<id>' button must not sell a plan the admin archived (purchase_plan refuses it too)."""
    await _users(session_factory, 1, balance=100_000)
    plan = await _plan(session_factory)
    await _archive(session_factory, plan)
    with pytest.raises(SubscriptionError) as err:
        await _gift(session_factory, settings, 1, plan, "ui-ref-1")
    assert err.value.code is SubErrorCode.NO_PLAN
    assert await _balance(session_factory, 1) == 100_000
    assert await _count(session_factory, PromoCode) == 0
    assert await _count(session_factory, BalanceTransaction) == 0


async def test_create_gift_paid_by_payment_allows_archived_plan(
    session_factory: SessionFactory, settings: Settings
) -> None:
    """A gift payment made before the plan was archived still delivers its code (grandfathered, like _buy)."""
    await _users(session_factory, 1, balance=15_000)
    plan = await _plan(session_factory)
    await _archive(session_factory, plan)
    async with session_factory.begin() as s:
        buyer = await s.get(User, 1)
        assert buyer is not None
        gift = await promo.create_gift(s, settings, buyer, plan, "pay-1", allow_archived=True)
    assert gift.plan_id == plan.id
    assert await _balance(session_factory, 1) == 0


async def test_create_gift_refuses_banned_buyer(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1, balance=100_000)
    plan = await _plan(session_factory)
    async with session_factory.begin() as s:
        buyer = await s.get(User, 1)
        assert buyer is not None
        buyer.is_banned = True
    with pytest.raises(SubscriptionError) as err:
        await _gift(session_factory, settings, 1, plan, "ui-ref-1")
    assert err.value.code is SubErrorCode.BANNED
    assert await _balance(session_factory, 1) == 100_000
    assert await _count(session_factory, PromoCode) == 0


async def test_gift_redeemed_once(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1, balance=15_000)
    await _users(session_factory, 2, 3)
    plan = await _plan(session_factory)
    gift = await _gift(session_factory, settings, 1, plan, "pay-1")
    outcome, redeemed = await _redeem(session_factory, settings, 2, gift.code.lower())
    assert outcome is PromoOutcome.OK and redeemed is not None
    assert (redeemed.kind, redeemed.plan_id, redeemed.bonus_days) == (PromoKind.GIFT, plan.id, 30)
    assert (await _redeem(session_factory, settings, 3, gift.code))[0] is PromoOutcome.EXHAUSTED
    assert await _count(session_factory, Event, Event.type == "gift.redeemed") == 1
    assert await _count(session_factory, BalanceTransaction, BalanceTransaction.user_id == 2) == 0


async def test_list_codes(session_factory: SessionFactory, settings: Settings) -> None:
    await _users(session_factory, 1, balance=15_000)
    plan = await _plan(session_factory)
    await _promo(session_factory, code="AAA")
    await _promo(session_factory, code="BBB")
    await _gift(session_factory, settings, 1, plan, "g")
    async with session_factory.begin() as s:
        assert [p.code for p in await promo.list_codes(s)] == ["BBB", "AAA"]
        assert len(await promo.list_codes(s, kind=None)) == 3
        assert [p.kind for p in await promo.list_codes(s, kind=PromoKind.GIFT)] == [PromoKind.GIFT]
        aaa = await promo.get_by_code(s, "aaa")
        assert aaa is not None
        await promo.deactivate(s, aaa)
        assert [p.code for p in await promo.list_codes(s, active_only=True)] == ["BBB"]
        assert await promo.get_by_code(s, " ") is None


@contextmanager
def _capture_sql(session_factory: SessionFactory) -> Iterator[list[str]]:
    """Record the SQL statements executed through the factory's engine (normalized: upper-case, one line)."""
    engine = session_factory.kw["bind"].sync_engine
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(" ".join(statement.split()).upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _first_index(statements: list[str], prefix: str) -> int:
    return next(i for i, sql in enumerate(statements) if sql.startswith(prefix))


async def test_create_gift_inserts_ref_mapping_without_update(
    session_factory: SessionFactory, settings: Settings
) -> None:
    """The gift-ref key is always new: an UPDATE-first upsert takes InnoDB gap locks that deadlock two buyers."""
    await _users(session_factory, 1, balance=20_000)
    plan = await _plan(session_factory)
    with _capture_sql(session_factory) as statements:
        gift = await _gift(session_factory, settings, 1, plan, "pay-1")
    assert not [sql for sql in statements if sql.startswith("UPDATE KV")]
    assert [sql for sql in statements if sql.startswith("INSERT INTO KV")]
    assert (await _gift(session_factory, settings, 1, plan, "pay-1")).id == gift.id


@pytest.mark.parametrize(("bonus_kop", "bonus_days"), [(0, 7), (500, 7)])
async def test_redeem_with_days_locks_subscription_before_user_row(
    session_factory: SessionFactory, settings: Settings, bonus_kop: int, bonus_days: int
) -> None:
    """Lock order must match purchases (subscription, then users): the caller follows up with grant_days."""
    await _users(session_factory, 1)
    async with session_factory.begin() as s:
        s.add(Subscription(user_id=1, client_uuid="u-1", status=SubscriptionStatus.ACTIVE, expires_at=utcnow()))
    code = (await _promo(session_factory, bonus_kop=bonus_kop, bonus_days=bonus_days)).code
    with _capture_sql(session_factory) as statements:
        assert (await _redeem(session_factory, settings, 1, code))[0] is PromoOutcome.OK
    lock = _first_index(statements, "SELECT SUBSCRIPTIONS.")
    assert lock < _first_index(statements, "INSERT INTO PROMO_ACTIVATIONS")
    if bonus_kop:
        assert lock < _first_index(statements, "UPDATE USERS")


async def test_redeem_money_promo_updates_balance_before_activation_insert(
    session_factory: SessionFactory, settings: Settings
) -> None:
    """The wallet UPDATE takes the user's row lock before the activation's FK check would take a shared one."""
    await _users(session_factory, 1)
    code = (await _promo(session_factory, bonus_kop=500)).code
    with _capture_sql(session_factory) as statements:
        assert (await _redeem(session_factory, settings, 1, code))[0] is PromoOutcome.OK
    assert _first_index(statements, "UPDATE USERS") < _first_index(statements, "INSERT INTO PROMO_ACTIVATIONS")
    assert not [sql for sql in statements if sql.startswith("SELECT SUBSCRIPTIONS.")]  # money only: no lock
    assert await _balance(session_factory, 1) == 500
