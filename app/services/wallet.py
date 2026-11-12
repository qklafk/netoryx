"""Internal wallet: the ONLY code path that changes user balances.

A user has two balances (ledger accounts): MAIN (``users.balance_kop``, spendable) and REFERRAL
(``users.referral_balance_kop``, partner earnings). Every change updates one balance with a single conditional
``UPDATE`` (so concurrent charges can never drive it below zero) and writes exactly one ledger row
(``balance_transactions``) whose ``UNIQUE(account, kind, ref, user_id)`` makes it idempotent. Both happen inside
one SAVEPOINT: either both are written or neither is.

Lock order matters on InnoDB: the ``UPDATE`` runs first and takes the user's row lock before the ledger insert's
foreign-key check would take a shared lock on the same row (two concurrent charges would otherwise deadlock).
After a conflict, reads use ``FOR SHARE`` so they see rows committed after a REPEATABLE READ snapshot.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from sqlalchemy import CursorResult, exists, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, identity
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.db.models import BalanceTransaction, LedgerAccount, TxKind, User

log = logging.getLogger(__name__)

MAX_REF_LENGTH = 128
_BALANCE_COLUMNS: dict[LedgerAccount, InstrumentedAttribute[int]] = {
    LedgerAccount.MAIN: User.balance_kop,
    LedgerAccount.REFERRAL: User.referral_balance_kop,
}


class InsufficientFunds(Exception):
    """The charge would make the balance negative; nothing was written."""

    def __init__(
        self, user_id: int, amount_kop: int, balance_kop: int, account: LedgerAccount = LedgerAccount.MAIN
    ) -> None:
        super().__init__(f"user {user_id}: {account} balance {balance_kop} kop, charge {-amount_kop} kop")
        self.user_id = user_id
        self.amount_kop = amount_kop
        self.balance_kop = balance_kop
        self.account = account

    @property
    def missing_kop(self) -> int:
        """How much the user has to top up for the charge to succeed."""
        return max(0, -self.amount_kop - self.balance_kop)


class _Rejected(Exception):
    """Internal: the conditional UPDATE matched no row (rolls the SAVEPOINT back)."""


def _validate(amount_kop: int, ref: str) -> None:
    if isinstance(amount_kop, bool) or not isinstance(amount_kop, int):
        raise TypeError("amount_kop must be an int (kopecks)")
    if not ref or len(ref) > MAX_REF_LENGTH:
        raise ValueError(f"ref must be 1..{MAX_REF_LENGTH} characters")


async def apply(
    session: AsyncSession,
    user_id: int,
    amount_kop: int,
    kind: TxKind,
    ref: str,
    *,
    actor_id: int | None = None,
    account: LedgerAccount = LedgerAccount.MAIN,
) -> int | None:
    """Credit (``amount_kop > 0``) or charge (``< 0``) a balance exactly once per ``(account, kind, ref)``.

    Runs inside the caller's transaction (never commits). Returns ``amount_kop`` when applied, ``None`` if
    this ``(account, kind, ref, user_id)`` was applied before. Raises :class:`InsufficientFunds` if the balance
    would go negative and ``LookupError`` if the user does not exist; in both cases nothing is written.
    A zero amount records the ledger row (an idempotency marker) without touching the balance.
    """
    _validate(amount_kop, ref)
    if await _ledger_row_exists(session, user_id, account, kind, ref):  # fast path for replays
        return None
    try:
        async with session.begin_nested():
            await _update_balance(session, user_id, amount_kop, account)
            await session.execute(
                insert(BalanceTransaction).values(
                    user_id=user_id, account=account, amount_kop=amount_kop, kind=kind, ref=ref, actor_id=actor_id
                )
            )
    except IntegrityError:
        # A concurrent transaction committed the same key first; our balance change was rolled back.
        if await _ledger_row_exists(session, user_id, account, kind, ref, fresh=True):
            log.debug("wallet: %s/%s/%s for user %s already applied", account, kind, ref, user_id)
            return None
        raise
    except _Rejected:
        await _raise_if_missing(session, user_id)
        # A charge replayed after a concurrent first attempt used up the balance is still a replay.
        if await _ledger_row_exists(session, user_id, account, kind, ref, fresh=True):
            return None
        current = await balance(session, user_id, account)
        raise InsufficientFunds(user_id, amount_kop, current, account) from None
    await _sync_loaded_user(session, user_id, account)
    log.info("wallet: user %s %+d kop %s (%s/%s)", user_id, amount_kop, account, kind, ref)
    return amount_kop


async def _update_balance(session: AsyncSession, user_id: int, amount_kop: int, account: LedgerAccount) -> None:
    column = _BALANCE_COLUMNS[account]
    stmt = (
        update(User)
        .where(User.tg_id == user_id, column + amount_kop >= 0)
        .values({column: column + amount_kop})
        .execution_options(synchronize_session=False)
    )
    result = cast(CursorResult[Any], await session.execute(stmt))
    if result.rowcount != 1:
        raise _Rejected


async def _ledger_row_exists(
    session: AsyncSession, user_id: int, account: LedgerAccount, kind: TxKind, ref: str, *, fresh: bool = False
) -> bool:
    """``fresh=True`` uses a shared locking read that sees rows committed after a REPEATABLE READ snapshot."""
    stmt = select(BalanceTransaction.id).where(
        BalanceTransaction.user_id == user_id,
        BalanceTransaction.account == account,
        BalanceTransaction.kind == kind,
        BalanceTransaction.ref == ref,
    )
    if fresh:
        stmt = stmt.with_for_update(read=True)
    return await session.scalar(stmt) is not None


async def _raise_if_missing(session: AsyncSession, user_id: int) -> None:
    if not await session.scalar(select(exists().where(User.tg_id == user_id))):
        raise LookupError(f"user {user_id} not found")


async def _sync_loaded_user(session: AsyncSession, user_id: int, account: LedgerAccount) -> None:
    """Refresh the balance of a ``User`` already loaded in this session (the UPDATE bypassed the ORM)."""
    identity_map: identity.IdentityMap = session.sync_session.identity_map
    user = identity_map.get(identity_key(User, user_id))
    if user is not None:
        set_committed_value(user, _BALANCE_COLUMNS[account].key, await balance(session, user_id, account))


async def balance(session: AsyncSession, user_id: int, account: LedgerAccount = LedgerAccount.MAIN) -> int:
    """Current balance in kopecks as seen by this transaction (0 for unknown users)."""
    value = await session.scalar(select(_BALANCE_COLUMNS[account]).where(User.tg_id == user_id))
    return int(value or 0)


async def has_transaction(
    session: AsyncSession, user_id: int, kind: TxKind, ref: str, *, account: LedgerAccount = LedgerAccount.MAIN
) -> bool:
    """True if ``(account, kind, ref)`` was already applied to this user."""
    return await _ledger_row_exists(session, user_id, account, kind, ref)


async def history(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int = 20,
    before_id: int | None = None,
    account: LedgerAccount | None = None,
) -> list[BalanceTransaction]:
    """Newest-first ledger page for a user (all accounts unless ``account`` is given). Paginate with
    ``before_id``."""
    stmt = (
        select(BalanceTransaction)
        .where(BalanceTransaction.user_id == user_id)
        .order_by(BalanceTransaction.id.desc())
        .limit(max(1, min(limit, 200)))
    )
    if account is not None:
        stmt = stmt.where(BalanceTransaction.account == account)
    if before_id is not None:
        stmt = stmt.where(BalanceTransaction.id < before_id)
    return list((await session.scalars(stmt)).all())
