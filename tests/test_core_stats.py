from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

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
from app.db.session import SessionFactory
from app.services import stats
from app.services.stats import DayStats, ProviderStat
from tests.conftest import make_settings

# 15:00 in Moscow (UTC+3). Local "today" starts at 2026-06-14 21:00 UTC.
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
SETTINGS = make_settings(tz="Europe/Moscow")


def _ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def _ahead(**kw: float) -> datetime:
    return NOW + timedelta(**kw)


def _user(tg_id: int, created_at: datetime, **kw: object) -> User:
    return User(tg_id=tg_id, referral_code=f"R{tg_id}", sub_token=f"t{tg_id}", created_at=created_at, **kw)


def _payment(pid: str, user_id: int, amount: int, provider: str, credited_at: datetime | None, **kw: object) -> Payment:
    kw.setdefault("status", PaymentStatus.SUCCEEDED)
    return Payment(id=pid, user_id=user_id, amount_kop=amount, provider=provider, credited_at=credited_at, **kw)


def _purchase(user_id: int, kind: PurchaseKind, created_at: datetime, expires_after: datetime) -> Purchase:
    return Purchase(user_id=user_id, kind=kind, days=30, expires_after=expires_after, created_at=created_at)


def _sub(sid: int, user_id: int, plan_id: int | None, expires_at: datetime, **kw: object) -> Subscription:
    kw.setdefault("status", SubscriptionStatus.ACTIVE if expires_at > NOW else SubscriptionStatus.EXPIRED)
    return Subscription(
        id=sid, user_id=user_id, plan_id=plan_id, client_uuid=f"uuid-{sid}", expires_at=expires_at, **kw
    )


async def seed_dashboard_data(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        s.add_all(
            [
                _user(1, _ago(hours=1), trial_used=True, balance_kop=500),
                _user(2, datetime(2026, 6, 14, 22, 0, tzinfo=UTC), trial_used=True, referred_by=1),  # 15th local
                _user(3, datetime(2026, 6, 14, 20, 0, tzinfo=UTC), balance_kop=1_000, referred_by=1),  # 14th local
                _user(4, _ago(days=10), trial_used=True),
                _user(5, _ago(days=40), trial_used=True),
                _user(6, _ago(days=100), bot_blocked=True),
                Plan(id=1, title="1m", duration_days=30, price_kop=15_000),
                Plan(id=2, title="3m", duration_days=90, price_kop=40_000),
            ]
        )
        await s.flush()
        s.add_all(
            [
                _payment("p1", 1, 15_000, "yookassa", _ago(minutes=30)),
                _payment("p2", 3, 40_000, "platega", _ago(days=3)),
                _payment("p3", 4, 15_000, "yookassa", _ago(days=20)),
                _payment("p4", 5, 15_000, "stars", _ago(days=60)),
                _payment("p5", 1, 10_000, "stars", _ago(hours=2), is_donation=True),
                _payment("p6", 2, 99_999, "yookassa", None, status=PaymentStatus.CANCELED),
                _payment("p7", 6, 5_000, "cryptobot", _ago(days=5), status=PaymentStatus.REFUNDED),
                _payment("p8", 3, 20_000, "cryptobot", datetime(2026, 6, 13, 20, 59, tzinfo=UTC)),  # 13th local
                _sub(1, 1, 1, _ahead(days=20)),
                _sub(2, 2, None, _ahead(days=2), is_trial=True),
                _sub(3, 3, 2, _ahead(days=80)),
                _sub(4, 4, 1, _ago(days=5)),  # churned
                _sub(5, 5, 1, _ago(days=45)),  # expired before the window
                _sub(6, 6, None, datetime(2026, 6, 14, 0, 0, tzinfo=UTC), is_trial=True),  # trial: not churn
                _purchase(1, PurchaseKind.TRIAL, _ago(days=25), _ago(days=22)),
                _purchase(1, PurchaseKind.PURCHASE, _ago(days=10), _ahead(days=20)),
                _purchase(1, PurchaseKind.PURCHASE, _ago(days=2), _ahead(days=50)),  # overlapping
                _purchase(2, PurchaseKind.TRIAL, datetime(2026, 6, 14, 22, 5, tzinfo=UTC), _ahead(days=2)),
                _purchase(3, PurchaseKind.PURCHASE, _ago(days=3), _ahead(days=80)),
                _purchase(4, PurchaseKind.TRIAL, _ago(days=40), _ago(days=37)),
                _purchase(4, PurchaseKind.AUTO_RENEW, _ago(days=35), _ago(days=5)),
                _purchase(5, PurchaseKind.PURCHASE, _ago(days=75), _ago(days=45)),
                _purchase(6, PurchaseKind.TRIAL, _ago(days=6), datetime(2026, 6, 14, 0, 0, tzinfo=UTC)),
            ]
        )


async def test_dashboard_on_seeded_data(session_factory: SessionFactory) -> None:
    await seed_dashboard_data(session_factory)
    async with session_factory() as s:
        d = await stats.dashboard(s, SETTINGS, NOW)
    assert d.generated_at == NOW
    assert (d.users_total, d.users_new_today, d.users_new_7d, d.users_new_30d, d.users_blocked_bot) == (6, 2, 3, 4, 1)
    assert (d.subscriptions_active, d.subscriptions_trial, d.subscriptions_paid) == (3, 1, 2)
    assert d.paying_users == 4
    assert (d.revenue_today_kop, d.revenue_7d_kop, d.revenue_30d_kop, d.revenue_total_kop) == (
        15_000,
        75_000,
        90_000,
        105_000,
    )
    assert (d.donations_30d_kop, d.donations_total_kop) == (10_000, 10_000)
    assert d.arppu_kop == 26_250
    assert d.mrr_kop == 28_333  # 15000 + 40000 * 30 / 90
    assert (d.churned_30d, d.churn_rate_30d) == (1, 0.3333)
    assert (d.trial_users, d.trial_converted, d.trial_conversion) == (4, 3, 0.75)  # users 1, 4, 5
    assert d.wallet_total_kop == 1_500
    assert d.providers_30d == [
        ProviderStat("platega", 1, 40_000),
        ProviderStat("yookassa", 2, 30_000),
        ProviderStat("cryptobot", 1, 20_000),
    ]
    assert [(u.tg_id, n) for u, n in d.top_referrers] == [(1, 2)]


async def test_dashboard_on_empty_database(session_factory: SessionFactory) -> None:
    async with session_factory() as s:
        d = await stats.dashboard(s, SETTINGS, NOW)
    assert d.users_total == d.revenue_total_kop == d.mrr_kop == d.arppu_kop == d.paying_users == 0
    assert d.churn_rate_30d == d.trial_conversion == 0.0
    assert d.providers_30d == [] and d.top_referrers == []


async def test_payments_by_provider_all_time(session_factory: SessionFactory) -> None:
    await seed_dashboard_data(session_factory)
    async with session_factory() as s:
        rows = await stats.payments_by_provider(s)
    assert rows == [
        ProviderStat("platega", 1, 40_000),
        ProviderStat("yookassa", 2, 30_000),
        ProviderStat("cryptobot", 1, 20_000),
        ProviderStat("stars", 1, 15_000),  # the Stars donation is excluded
    ]


async def test_daily_series_buckets_by_local_day(session_factory: SessionFactory) -> None:
    await seed_dashboard_data(session_factory)
    async with session_factory() as s:
        series = await stats.daily(s, SETTINGS, days=3, now=NOW)
    assert series == [
        DayStats(date(2026, 6, 13), registrations=0, payments=1, revenue_kop=20_000, active_subscriptions=3),
        DayStats(date(2026, 6, 14), registrations=1, payments=0, revenue_kop=0, active_subscriptions=2),
        DayStats(date(2026, 6, 15), registrations=2, payments=1, revenue_kop=15_000, active_subscriptions=3),
    ]


async def test_daily_series_in_utc_moves_boundaries(session_factory: SessionFactory) -> None:
    await seed_dashboard_data(session_factory)
    async with session_factory() as s:
        series = await stats.daily(s, make_settings(tz="UTC"), days=3, now=NOW)
    by_day = {d.day: d for d in series}
    assert by_day[date(2026, 6, 14)].registrations == 2  # users 2 and 3 are both on the 14th in UTC
    assert by_day[date(2026, 6, 13)].revenue_kop == 20_000


async def test_daily_series_counts_independent_subscriptions_of_one_user(session_factory: SessionFactory) -> None:
    await seed_dashboard_data(session_factory)
    async with session_factory.begin() as session:
        second = _sub(7, 1, 1, _ahead(days=20))
        session.add(second)
        await session.flush()
        second_purchase = _purchase(1, PurchaseKind.PURCHASE, _ago(days=10), _ahead(days=20))
        second_purchase.subscription_id = second.id
        session.add(second_purchase)
    async with session_factory() as session:
        series = await stats.daily(session, SETTINGS, days=3, now=NOW)
    assert [item.active_subscriptions for item in series] == [4, 3, 4]


async def test_daily_series_length_and_empty_db(session_factory: SessionFactory) -> None:
    async with session_factory() as s:
        series = await stats.daily(s, SETTINGS, days=30, now=NOW)
        assert len(series) == 30
        assert series[-1].day == date(2026, 6, 15) and series[0].day == date(2026, 5, 17)
        assert all(d == DayStats(d.day) for d in series)
        assert len(await stats.daily(s, SETTINGS, days=1, now=NOW)) == 1


@pytest.mark.parametrize("days", [0, -1, stats.MAX_SERIES_DAYS + 1])
async def test_daily_series_rejects_bad_ranges(session_factory: SessionFactory, days: int) -> None:
    async with session_factory() as s:
        with pytest.raises(ValueError):
            await stats.daily(s, SETTINGS, days=days, now=NOW)


class _RowsSession:
    """Stand-in session for ``_active_by_day``: returns canned purchase rows, ignores the statement."""

    def __init__(self, rows: list[tuple[int, datetime, datetime]]) -> None:
        self.rows = rows

    async def execute(self, _stmt: object) -> _RowsSession:
        return self

    def all(self) -> list[tuple[int, datetime, datetime]]:
        return self.rows


def _brute_force_active(
    rows: list[tuple[int, datetime, datetime]], day_list: list[date], now: datetime
) -> dict[date, int]:
    result = {}
    for d in day_list:
        instant = min(stats.local_day_start(d + timedelta(days=1), SETTINGS), now)
        result[d] = len({uid for uid, start, end in rows if start <= instant < end})
    return result


async def test_active_by_day_matches_brute_force() -> None:
    import random

    rng = random.Random(1234)
    day_list = [NOW.date() - timedelta(days=offset) for offset in range(39, -1, -1)]
    boundaries = [stats.local_day_start(d, SETTINGS) for d in day_list] + [NOW]
    rows: list[tuple[int, datetime, datetime]] = []
    for _ in range(600):
        uid = rng.randint(1, 40)
        # Starts/ends exactly on checkpoints are frequent, to exercise the half-open interval edges.
        start = rng.choice(boundaries) if rng.random() < 0.3 else _ago(days=rng.uniform(0, 60))
        end = rng.choice(boundaries) if rng.random() < 0.3 else start + timedelta(days=rng.uniform(0, 35))
        if start <= NOW and end > start:
            rows.append((uid, start, end))
    active = await stats._active_by_day(_RowsSession(rows), SETTINGS, day_list, NOW)  # type: ignore[arg-type]
    expected = _brute_force_active(rows, day_list, NOW)
    assert {d: active.get(d, 0) for d in day_list} == expected
    assert any(expected.values())


async def test_active_by_day_scales_linearly() -> None:
    """300k purchases over a 366-day window: must not be O(purchases x days) Python work on the event loop."""
    import time

    day_list = [NOW.date() - timedelta(days=offset) for offset in range(stats.MAX_SERIES_DAYS - 1, -1, -1)]
    rows = [(i % 100_000, _ago(days=i % 400), _ago(days=i % 400 - 30)) for i in range(300_000)]
    started = time.perf_counter()
    active = await stats._active_by_day(_RowsSession(rows), SETTINGS, day_list, NOW)  # type: ignore[arg-type]
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"_active_by_day took {elapsed:.1f}s"
    assert 0 < max(active.values()) <= 100_000


async def test_wallet_liability_includes_partner_balances(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        s.add_all(
            [
                _user(1, _ago(days=1), balance_kop=3_000_000, referral_balance_kop=12_000_000),
                _user(2, _ago(days=1), balance_kop=500),
            ]
        )
    async with session_factory() as s:
        d = await stats.dashboard(s, make_settings(referral_reward_mode="partner"), NOW)
    assert d.partner_balance_total_kop == 12_000_000
    assert d.wallet_total_kop == 3_000_500 + 12_000_000
