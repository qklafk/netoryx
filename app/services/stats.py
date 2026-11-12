"""Dashboard aggregates for the bot admin panel and the web admin.

Portable by design (SQLite / PostgreSQL / MySQL): only COUNT/SUM/GROUP BY over plain columns in SQL; anything
calendar-related (local "today", per-day buckets) is computed in Python with ``settings.zone``.

Definitions
* revenue   - ``amount_kop`` of SUCCEEDED, non-donation payments by ``credited_at`` (refunded ones excluded);
              donations are reported separately.
* MRR       - active, non-trial subscriptions priced at their plan normalized to 30 days.
* churn     - non-trial subscriptions whose ``expires_at`` fell within the last 30 days and was not extended
              (a renewal moves ``expires_at`` into the future); rate = churned / (churned + active paid).
* trial conversion - users who used the trial and later bought or auto-renewed a plan.
* daily active subscriptions - reconstructed from purchase history: a user is active at the end of a day if
  some purchase/trial/grant made before that instant still covered it (admin blocks are not reflected).

Everything here only reads: callers should pass a ``app.db.session.read_session`` session, which on SQLite does
not hold the write lock while the dashboard's queries run.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import accumulate
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Payment,
    PaymentStatus,
    Plan,
    Purchase,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.services import referrals
from app.utils.time import local, local_day_start, utcnow

if TYPE_CHECKING:
    from app.config import Settings

PAID_PURCHASE_KINDS = (PurchaseKind.PURCHASE, PurchaseKind.AUTO_RENEW)
MAX_SERIES_DAYS = 366
_MONTH_DAYS = 30


@dataclass(frozen=True, slots=True)
class ProviderStat:
    provider: str
    payments: int
    amount_kop: int


@dataclass(frozen=True, slots=True)
class Dashboard:
    generated_at: datetime
    users_total: int
    users_new_today: int
    users_new_7d: int
    users_new_30d: int
    users_blocked_bot: int
    subscriptions_active: int
    subscriptions_trial: int  # active trials (included in subscriptions_active)
    paying_users: int
    revenue_today_kop: int
    revenue_7d_kop: int
    revenue_30d_kop: int
    revenue_total_kop: int
    donations_30d_kop: int
    donations_total_kop: int
    arppu_kop: int  # all-time revenue per paying user
    mrr_kop: int
    churned_30d: int
    churn_rate_30d: float
    trial_users: int
    trial_converted: int
    trial_conversion: float
    wallet_total_kop: int  # sum of all balances, main + partner (liability)
    partner_balance_total_kop: int  # of which partner (REFERRAL) balances; withdrawable in partner mode
    providers_30d: list[ProviderStat] = field(default_factory=list)
    top_referrers: list[tuple[User, int]] = field(default_factory=list)

    @property
    def subscriptions_paid(self) -> int:
        return self.subscriptions_active - self.subscriptions_trial


@dataclass(frozen=True, slots=True)
class DayStats:
    day: date
    registrations: int = 0
    payments: int = 0
    revenue_kop: int = 0
    active_subscriptions: int = 0


# ---------------------------------------------------------------------------------------------
# Small portable query helpers
# ---------------------------------------------------------------------------------------------
async def _count(session: AsyncSession, column: Any, *where: ColumnElement[bool]) -> int:
    return int(await session.scalar(select(func.count(column)).where(*where)) or 0)


async def _sum(session: AsyncSession, column: Any, *where: ColumnElement[bool]) -> int:
    # SUM over BIGINT is NUMERIC/Decimal on PostgreSQL and MySQL: normalize to int.
    return int(await session.scalar(select(func.coalesce(func.sum(column), 0)).where(*where)) or 0)


def _credited(donation: bool) -> list[ColumnElement[bool]]:
    return [
        Payment.status == PaymentStatus.SUCCEEDED,
        Payment.is_donation.is_(donation),
        Payment.credited_at.is_not(None),
    ]


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


# ---------------------------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------------------------
async def dashboard(
    session: AsyncSession, settings: Settings, now: datetime | None = None, *, top_limit: int = 5
) -> Dashboard:
    """All headline numbers for the admin dashboard."""
    now = now or utcnow()
    today_start = local_day_start(local(now, settings).date(), settings)
    d7, d30 = now - timedelta(days=7), now - timedelta(days=30)

    paid = _credited(False)
    revenue_total = await _sum(session, Payment.amount_kop, *paid)
    paying = await _count(session, distinct(Payment.user_id), *paid)
    active = [Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at > now]
    subs_active = await _count(session, Subscription.id, *active)
    subs_trial = await _count(session, Subscription.id, *active, Subscription.is_trial.is_(True))
    churned = await _count(
        session,
        Subscription.id,
        Subscription.is_trial.is_(False),
        Subscription.expires_at > d30,
        Subscription.expires_at <= now,
    )
    trial_users, trial_converted = await _trial_conversion(session)
    partner_balances = await _sum(session, User.referral_balance_kop)

    return Dashboard(
        generated_at=now,
        users_total=await _count(session, User.tg_id),
        users_new_today=await _count(session, User.tg_id, User.created_at >= today_start),
        users_new_7d=await _count(session, User.tg_id, User.created_at >= d7),
        users_new_30d=await _count(session, User.tg_id, User.created_at >= d30),
        users_blocked_bot=await _count(session, User.tg_id, User.bot_blocked.is_(True)),
        subscriptions_active=subs_active,
        subscriptions_trial=subs_trial,
        paying_users=paying,
        revenue_today_kop=await _sum(session, Payment.amount_kop, *paid, Payment.credited_at >= today_start),
        revenue_7d_kop=await _sum(session, Payment.amount_kop, *paid, Payment.credited_at >= d7),
        revenue_30d_kop=await _sum(session, Payment.amount_kop, *paid, Payment.credited_at >= d30),
        revenue_total_kop=revenue_total,
        donations_30d_kop=await _sum(session, Payment.amount_kop, *_credited(True), Payment.credited_at >= d30),
        donations_total_kop=await _sum(session, Payment.amount_kop, *_credited(True)),
        arppu_kop=revenue_total // paying if paying else 0,
        mrr_kop=await mrr(session, now),
        churned_30d=churned,
        churn_rate_30d=_ratio(churned, churned + subs_active - subs_trial),
        trial_users=trial_users,
        trial_converted=trial_converted,
        trial_conversion=_ratio(trial_converted, trial_users),
        wallet_total_kop=await _sum(session, User.balance_kop) + partner_balances,
        partner_balance_total_kop=partner_balances,
        providers_30d=await payments_by_provider(session, since=d30),
        top_referrers=await referrals.top(session, limit=top_limit),
    )


async def mrr(session: AsyncSession, now: datetime | None = None) -> int:
    """Monthly recurring revenue estimate in kopecks (see module docstring)."""
    now = now or utcnow()
    stmt = (
        select(Plan.price_kop, Plan.duration_days, func.count(Subscription.id))
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at > now,
            Subscription.is_trial.is_(False),
        )
        .group_by(Plan.id, Plan.price_kop, Plan.duration_days)
    )
    total = sum(price * _MONTH_DAYS * count / days for price, days, count in (await session.execute(stmt)).all())
    return round(total)


async def payments_by_provider(session: AsyncSession, *, since: datetime | None = None) -> list[ProviderStat]:
    """Successful non-donation payments per provider (optionally since a moment), largest first."""
    amount = func.coalesce(func.sum(Payment.amount_kop), 0)
    stmt = select(Payment.provider, func.count(Payment.id), amount).where(*_credited(False)).group_by(Payment.provider)
    if since is not None:
        stmt = stmt.where(Payment.credited_at >= since)
    rows = [ProviderStat(p, int(n), int(s)) for p, n, s in (await session.execute(stmt)).all()]
    return sorted(rows, key=lambda r: (-r.amount_kop, r.provider))


async def _trial_conversion(session: AsyncSession) -> tuple[int, int]:
    trial_users = await _count(session, User.tg_id, User.trial_used.is_(True))
    converted = int(
        await session.scalar(
            select(func.count(distinct(Purchase.user_id)))
            .join(User, User.tg_id == Purchase.user_id)
            .where(User.trial_used.is_(True), Purchase.kind.in_(PAID_PURCHASE_KINDS))
        )
        or 0
    )
    return trial_users, converted


# ---------------------------------------------------------------------------------------------
# Daily series for charts
# ---------------------------------------------------------------------------------------------
async def daily(
    session: AsyncSession, settings: Settings, days: int = 30, now: datetime | None = None
) -> list[DayStats]:
    """Per local day (oldest first, today last): registrations, payments, revenue, active subscriptions."""
    if not 1 <= days <= MAX_SERIES_DAYS:
        raise ValueError(f"days must be 1..{MAX_SERIES_DAYS}")
    now = now or utcnow()
    today = local(now, settings).date()
    day_list = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    start = local_day_start(day_list[0], settings)

    users = await _rows(session, select(User.created_at), User.created_at, start, now)
    registrations = _bucket(settings, (created_at for (created_at,) in users))
    payments: dict[date, int] = defaultdict(int)
    revenue: dict[date, int] = defaultdict(int)
    stmt = select(Payment.credited_at, Payment.amount_kop).where(*_credited(False))
    for credited_at, amount in await _rows(session, stmt, Payment.credited_at, start, now):
        day = local(credited_at, settings).date()
        payments[day] += 1
        revenue[day] += int(amount)
    active = await _active_by_day(session, settings, day_list, now)

    return [
        DayStats(
            day=d,
            registrations=registrations.get(d, 0),
            payments=payments.get(d, 0),
            revenue_kop=revenue.get(d, 0),
            active_subscriptions=active.get(d, 0),
        )
        for d in day_list
    ]


async def _rows(session: AsyncSession, stmt: Any, column: Any, start: datetime, end: datetime) -> list[Any]:
    """Rows of ``stmt`` whose ``column`` lies within ``[start, end]``."""
    return list((await session.execute(stmt.where(column >= start, column <= end))).all())


def _bucket(settings: Settings, timestamps: Iterable[datetime]) -> dict[date, int]:
    counts: dict[date, int] = defaultdict(int)
    for ts in timestamps:
        counts[local(ts, settings).date()] += 1
    return counts


async def _active_by_day(
    session: AsyncSession, settings: Settings, day_list: list[date], now: datetime
) -> dict[date, int]:
    """Distinct subscriptions covered by a purchase interval ``[created_at, expires_after)`` at each day's end.

    Sweep instead of rows x days: each subscription's intervals are merged (so it counts once per checkpoint), every
    merged interval adds +1/-1 at the first checkpoint it covers / the first one past its end, and a prefix sum
    gives the counts - O(rows log rows) however long the window is.
    """
    instants = [min(local_day_start(d + timedelta(days=1), settings), now) for d in day_list]
    # Pre-migration purchases have no target. A negative user id preserves their former one-subscription-per-user
    # meaning without colliding with a real subscription id.
    identifier = func.coalesce(Purchase.subscription_id, -Purchase.user_id)
    stmt = select(identifier, Purchase.created_at, Purchase.expires_after).where(
        Purchase.expires_after > instants[0], Purchase.created_at <= now
    )
    rows = (await session.execute(stmt)).all()
    counts = _sweep_counts(rows, instants)
    return dict(zip(day_list, counts, strict=True))


def _sweep_counts(rows: Iterable[tuple[int, datetime, datetime]], instants: list[datetime]) -> list[int]:
    """How many distinct identifiers have an interval ``[start, end)`` at each non-decreasing instant."""
    deltas = [0] * (len(instants) + 1)

    def add(start: datetime, end: datetime) -> None:
        first, past = bisect_left(instants, start), bisect_left(instants, end)
        if first < past:
            deltas[first] += 1
            deltas[past] -= 1

    span: tuple[int, datetime, datetime] | None = None  # the current subscription's merged interval
    for identifier, start, end in sorted(rows, key=lambda row: (row[0], row[1])):
        if span is not None and span[0] == identifier and start <= span[2]:
            span = (identifier, span[1], max(span[2], end))
            continue
        if span is not None:
            add(span[1], span[2])
        span = (identifier, start, end)
    if span is not None:
        add(span[1], span[2])
    return list(accumulate(deltas[:-1]))
