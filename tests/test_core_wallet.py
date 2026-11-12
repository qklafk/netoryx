from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from app.db.models import BalanceTransaction, LedgerAccount, TxKind, User
from app.db.session import SessionFactory
from app.services import wallet
from app.services.wallet import InsufficientFunds


async def _user(session_factory: SessionFactory, tg_id: int = 1, balance: int = 0) -> None:
    async with session_factory.begin() as s:
        s.add(User(tg_id=tg_id, referral_code=f"C{tg_id}", sub_token=f"t{tg_id}", balance_kop=balance))


async def _balance(session_factory: SessionFactory, tg_id: int = 1) -> int:
    async with session_factory() as s:
        return await wallet.balance(s, tg_id)


async def _ledger(session_factory: SessionFactory, tg_id: int = 1) -> list[BalanceTransaction]:
    async with session_factory() as s:
        rows = await s.scalars(select(BalanceTransaction).where(BalanceTransaction.user_id == tg_id))
        return list(rows.all())


async def test_credit_and_charge(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    async with session_factory.begin() as s:
        assert await wallet.apply(s, 1, 10_000, TxKind.PAYMENT, "p1") == 10_000
        assert await wallet.apply(s, 1, -3_000, TxKind.PURCHASE, "buy1") == -3_000
    assert await _balance(session_factory) == 7_000
    ledger = await _ledger(session_factory)
    assert sorted(t.amount_kop for t in ledger) == [-3_000, 10_000]
    assert sum(t.amount_kop for t in ledger) == 7_000


async def test_same_kind_and_ref_applies_once(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    async with session_factory.begin() as s:
        assert await wallet.apply(s, 1, 500, TxKind.PAYMENT, "p1") == 500
        assert await wallet.apply(s, 1, 500, TxKind.PAYMENT, "p1") is None
        assert await wallet.has_transaction(s, 1, TxKind.PAYMENT, "p1")
    async with session_factory.begin() as s:  # also across transactions
        assert await wallet.apply(s, 1, 500, TxKind.PAYMENT, "p1") is None
    assert await _balance(session_factory) == 500
    assert len(await _ledger(session_factory)) == 1


async def test_same_ref_different_kind_or_user_is_independent(session_factory: SessionFactory) -> None:
    await _user(session_factory, 1)
    await _user(session_factory, 2)
    async with session_factory.begin() as s:
        assert await wallet.apply(s, 1, 100, TxKind.PAYMENT, "x") == 100
        assert await wallet.apply(s, 1, 100, TxKind.PROMO, "x") == 100
        assert await wallet.apply(s, 2, 100, TxKind.PROMO, "x") == 100
    assert await _balance(session_factory, 1) == 200
    assert await _balance(session_factory, 2) == 100


async def test_insufficient_funds_writes_nothing(session_factory: SessionFactory) -> None:
    await _user(session_factory, balance=1_000)
    async with session_factory.begin() as s:
        with pytest.raises(InsufficientFunds) as exc:
            await wallet.apply(s, 1, -1_001, TxKind.PURCHASE, "buy")
        assert exc.value.balance_kop == 1_000
        assert exc.value.missing_kop == 1
        assert exc.value.user_id == 1
        # The transaction stays usable and the ledger row was rolled back with the savepoint,
        # so the same ref can be retried after a top-up.
        assert not await wallet.has_transaction(s, 1, TxKind.PURCHASE, "buy")
        await wallet.apply(s, 1, 1, TxKind.PAYMENT, "topup")
        assert await wallet.apply(s, 1, -1_001, TxKind.PURCHASE, "buy") == -1_001
    assert await _balance(session_factory) == 0


async def test_replayed_charge_after_balance_is_spent_is_not_insufficient(session_factory: SessionFactory) -> None:
    await _user(session_factory, balance=100)
    async with session_factory.begin() as s:
        assert await wallet.apply(s, 1, -100, TxKind.PURCHASE, "b") == -100
    async with session_factory.begin() as s:
        assert await wallet.apply(s, 1, -100, TxKind.PURCHASE, "b") is None


async def test_charge_to_exactly_zero_is_allowed(session_factory: SessionFactory) -> None:
    await _user(session_factory, balance=1_500)
    async with session_factory.begin() as s:
        assert await wallet.apply(s, 1, -1_500, TxKind.PURCHASE, "b") == -1_500
    assert await _balance(session_factory) == 0


async def test_unknown_user_raises_lookup_error(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        with pytest.raises(LookupError):
            await wallet.apply(s, 404, 100, TxKind.PAYMENT, "p")
        with pytest.raises(LookupError):
            await wallet.apply(s, 404, -100, TxKind.PURCHASE, "p")
        assert await s.scalar(select(func.count()).select_from(BalanceTransaction)) == 0


async def test_zero_amount_records_marker_without_balance_change(session_factory: SessionFactory) -> None:
    await _user(session_factory, balance=10)
    async with session_factory.begin() as s:
        assert await wallet.apply(s, 1, 0, TxKind.PURCHASE, "free") == 0
        assert await wallet.apply(s, 1, 0, TxKind.PURCHASE, "free") is None
    assert await _balance(session_factory) == 10


@pytest.mark.parametrize(("amount", "ref", "error"), [(1.5, "r", TypeError), (True, "r", TypeError)])
async def test_invalid_amount_types(session_factory: SessionFactory, amount: object, ref: str, error: type) -> None:
    await _user(session_factory)
    async with session_factory.begin() as s:
        with pytest.raises(error):
            await wallet.apply(s, 1, amount, TxKind.PAYMENT, ref)  # type: ignore[arg-type]


@pytest.mark.parametrize("ref", ["", "x" * 129])
async def test_invalid_refs(session_factory: SessionFactory, ref: str) -> None:
    await _user(session_factory)
    async with session_factory.begin() as s:
        with pytest.raises(ValueError):
            await wallet.apply(s, 1, 1, TxKind.PAYMENT, ref)


async def test_loaded_user_object_sees_new_balance(session_factory: SessionFactory) -> None:
    await _user(session_factory, balance=100)
    async with session_factory.begin() as s:
        user = await s.get(User, 1)
        assert user is not None
        user.first_name = "pending change"  # unflushed ORM change must survive
        await wallet.apply(s, 1, 250, TxKind.PAYMENT, "p")
        assert user.balance_kop == 350
        await wallet.apply(s, 1, -50, TxKind.PURCHASE, "b")
        assert user.balance_kop == 300
    async with session_factory() as s:
        fresh = await s.get(User, 1)
        assert fresh is not None
        assert (fresh.balance_kop, fresh.first_name) == (300, "pending change")


async def test_caller_rollback_discards_everything(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    async with session_factory() as s:
        await wallet.apply(s, 1, 100, TxKind.PAYMENT, "p")
        await s.rollback()
    assert await _balance(session_factory) == 0
    assert await _ledger(session_factory) == []


async def test_apply_as_first_statement_still_rolls_back_with_caller(session_factory: SessionFactory) -> None:
    # Regression: on pysqlite a SAVEPOINT opening the transaction made RELEASE commit it.
    await _user(session_factory, balance=1_000)
    with pytest.raises(RuntimeError):
        async with session_factory.begin() as s:
            await wallet.apply(s, 1, -400, TxKind.PURCHASE, "b")
            raise RuntimeError("provisioning failed after the charge")
    assert await _balance(session_factory) == 1_000
    assert await _ledger(session_factory) == []


async def test_history_is_newest_first_and_paginates(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    async with session_factory.begin() as s:
        for i in range(5):
            await wallet.apply(s, 1, 10 + i, TxKind.PAYMENT, f"p{i}")
    async with session_factory() as s:
        page1 = await wallet.history(s, 1, limit=3)
        assert [t.ref for t in page1] == ["p4", "p3", "p2"]
        page2 = await wallet.history(s, 1, limit=3, before_id=page1[-1].id)
        assert [t.ref for t in page2] == ["p1", "p0"]
        assert await wallet.balance(s, 999) == 0


# --- concurrency (SQLite file DB, one session per task) ---------------------------------------------
async def _apply_in_own_txn(session_factory: SessionFactory, amount: int, kind: TxKind, ref: str) -> int | str | None:
    try:
        async with session_factory.begin() as s:
            return await wallet.apply(s, 1, amount, kind, ref)
    except InsufficientFunds:
        return "insufficient"


async def test_concurrent_charges_never_overdraw(session_factory: SessionFactory) -> None:
    await _user(session_factory, balance=1_000)
    results = await asyncio.gather(
        *(_apply_in_own_txn(session_factory, -100, TxKind.PURCHASE, f"buy{i}") for i in range(40))
    )
    assert results.count(-100) == 10
    assert results.count("insufficient") == 30
    assert await _balance(session_factory) == 0
    ledger = await _ledger(session_factory)
    assert len(ledger) == 10 and sum(t.amount_kop for t in ledger) == -1_000


async def test_concurrent_duplicates_apply_once(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    results = await asyncio.gather(
        *(_apply_in_own_txn(session_factory, 700, TxKind.PAYMENT, "same-payment") for _ in range(30))
    )
    assert results.count(700) == 1
    assert results.count(None) == 29
    assert await _balance(session_factory) == 700
    assert len(await _ledger(session_factory)) == 1


async def test_concurrent_mixed_credits_and_charges_keep_ledger_consistent(session_factory: SessionFactory) -> None:
    await _user(session_factory, balance=0)
    jobs = []
    for i in range(25):
        jobs.append(_apply_in_own_txn(session_factory, 300, TxKind.PAYMENT, f"p{i}"))
        jobs.append(_apply_in_own_txn(session_factory, -500, TxKind.PURCHASE, f"b{i}"))
        jobs.append(_apply_in_own_txn(session_factory, 300, TxKind.PAYMENT, f"p{i}"))  # duplicate credit
    results = await asyncio.gather(*jobs)
    credits = results.count(300)
    charges = results.count(-500)
    assert credits == 25  # every payment exactly once
    balance = await _balance(session_factory)
    assert balance == 25 * 300 - charges * 500 >= 0
    assert sum(t.amount_kop for t in await _ledger(session_factory)) == balance


async def test_concurrent_read_then_charge_transactions(session_factory: SessionFactory) -> None:
    """Typical handler shape: read the user, then charge. Must neither fail with 'database is locked'
    nor overdraw."""
    await _user(session_factory, balance=2_000)

    async def job(i: int) -> str:
        try:
            async with session_factory.begin() as s:
                user = await s.get(User, 1)
                assert user is not None
                await asyncio.sleep(0)
                await wallet.apply(s, 1, -300, TxKind.PURCHASE, f"b{i}")
                return "ok"
        except InsufficientFunds:
            return "insufficient"

    results = await asyncio.gather(*(job(i) for i in range(20)))
    assert results.count("ok") == 6
    assert results.count("insufficient") == 14
    assert await _balance(session_factory) == 200


# --- ledger accounts (MAIN / REFERRAL) ------------------------------------------------------------------
async def test_referral_account_is_a_separate_balance(session_factory: SessionFactory) -> None:
    await _user(session_factory, balance=100)
    async with session_factory.begin() as s:
        user = await s.get(User, 1)
        assert user is not None
        assert await wallet.apply(s, 1, 700, TxKind.REFERRAL_COMMISSION, "L1:p1", account=LedgerAccount.REFERRAL)
        assert (user.balance_kop, user.referral_balance_kop) == (100, 700)
        # a transfer writes the same (kind, ref) once per account
        assert await wallet.apply(s, 1, -500, TxKind.REFERRAL_TRANSFER, "t1", account=LedgerAccount.REFERRAL)
        assert await wallet.apply(s, 1, 500, TxKind.REFERRAL_TRANSFER, "t1") == 500
        assert await wallet.apply(s, 1, 500, TxKind.REFERRAL_TRANSFER, "t1") is None
        assert (user.balance_kop, user.referral_balance_kop) == (600, 200)
        assert await wallet.has_transaction(s, 1, TxKind.REFERRAL_TRANSFER, "t1", account=LedgerAccount.REFERRAL)
        assert not await wallet.has_transaction(s, 1, TxKind.REFERRAL_COMMISSION, "L1:p1")
    async with session_factory() as s:
        assert await wallet.balance(s, 1, LedgerAccount.REFERRAL) == 200
        assert len(await wallet.history(s, 1)) == 3
        referral_rows = await wallet.history(s, 1, account=LedgerAccount.REFERRAL)
        assert [t.ref for t in referral_rows] == ["t1", "L1:p1"]


async def test_referral_account_cannot_go_negative(session_factory: SessionFactory) -> None:
    await _user(session_factory, balance=10_000)
    async with session_factory.begin() as s:
        with pytest.raises(InsufficientFunds) as exc:
            await wallet.apply(s, 1, -1, TxKind.WITHDRAWAL, "w1", account=LedgerAccount.REFERRAL)
    assert (exc.value.account, exc.value.balance_kop, exc.value.missing_kop) == (LedgerAccount.REFERRAL, 0, 1)
    assert await _balance(session_factory) == 10_000
