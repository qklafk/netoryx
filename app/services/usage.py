"""Daily traffic snapshots per subscription (``usage_snapshots``) for charts and statistics.

:func:`snapshot` reads every panel's accounts in one listing per panel (3x-ui: all inbounds in one request where
the backend supports it), sums the counters of each subscription's accounts and stores the total for the current
local day (re-running the same day overwrites it). Panels are read with no DB transaction open. A subscription with
an account on a panel that could not be read is skipped for that day rather than stored with a partial total.

Counters are cumulative and reset when a new traffic period starts, so daily usage is the difference between
consecutive snapshots (or the snapshot itself after a reset), see :func:`daily_usage`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Inbound, Panel, PanelKind, Provision, ProvisionState, UsageSnapshot
from app.db.session import read_session
from app.services.panels.base import PanelBackend, PanelError, RemoteAccount
from app.services.provisioning import PanelSource, is_panel_down
from app.utils.time import today_local, utcnow

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory

log = logging.getLogger(__name__)

WRITE_CHUNK = 500
DEFAULT_KEEP_DAYS = 400

# (target, email) -> subscription id
_AccountMap = dict[tuple[int | None, str], int]


@dataclass(slots=True)
class UsageReport:
    day: str
    panels_ok: int = 0
    panels_failed: int = 0
    stored: int = 0  # subscriptions written
    skipped: int = 0  # subscriptions with an account on an unreadable panel
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _PanelPlan:
    panel_id: int
    kind: PanelKind
    accounts: _AccountMap = field(default_factory=dict)

    @property
    def targets(self) -> list[int | None]:
        return sorted({target for target, _ in self.accounts}, key=lambda t: -1 if t is None else t)


async def _plan(session: AsyncSession, settings: Settings) -> tuple[list[_PanelPlan], dict[int, set[int]]]:
    """Accounts to read per healthy panel, and every subscription's panels (for partial-read detection)."""
    stmt = (
        select(Provision.subscription_id, Provision.remote_email, Panel, Inbound.remote_id)
        .join(Panel, Panel.id == Provision.panel_id)
        .outerjoin(Inbound, Inbound.id == Provision.inbound_id)
        .where(
            Panel.is_active.is_(True),
            Panel.kind.in_(
                (
                    PanelKind.XUI,
                    PanelKind.REMNAWAVE,
                    PanelKind.MARZBAN,
                    PanelKind.HIDDIFY,
                    PanelKind.PASARGUARD,
                    PanelKind.MARZNESHIN,
                )
            ),
            Provision.actual_state != ProvisionState.ABSENT,
            Provision.remote_email.is_not(None),
        )
    )
    plans: dict[int, _PanelPlan] = {}
    panels_of_sub: dict[int, set[int]] = defaultdict(set)
    for sub_id, email, panel, remote_id in (await session.execute(stmt)).all():
        panels_of_sub[sub_id].add(panel.id)
        if is_panel_down(panel, settings):
            continue
        target = remote_id if panel.kind == PanelKind.XUI else None
        if email is None or (panel.kind == PanelKind.XUI and target is None):
            continue
        plan = plans.setdefault(panel.id, _PanelPlan(panel.id, panel.kind))
        plan.accounts[(target, email)] = sub_id
    return list(plans.values()), panels_of_sub


async def _list(backend: PanelBackend, targets: list[int | None]) -> list[tuple[int | None, RemoteAccount]]:
    many = getattr(backend, "list_accounts_by_target", None)
    int_targets = [t for t in targets if t is not None]
    if many is not None and len(int_targets) == len(targets) and int_targets:
        by_target: dict[int, list[RemoteAccount]] = await many(int_targets)
        return [(target, acc) for target, accounts in by_target.items() for acc in accounts]
    result: list[tuple[int | None, RemoteAccount]] = []
    for target in targets:
        result.extend((target, acc) for acc in await backend.list_accounts(target))
    return result


async def _read_panel(registry: PanelSource, plan: _PanelPlan) -> dict[int, int]:
    """Bytes used per subscription on one panel."""
    backend = await registry.get(plan.panel_id)
    used: dict[int, int] = defaultdict(int)
    for target, account in await _list(backend, plan.targets):
        sub_id = plan.accounts.get((target, account.email))
        if sub_id is not None:
            used[sub_id] += max(0, int(account.used_bytes or 0))
    return used


async def _store(session: AsyncSession, day: str, totals: dict[int, int]) -> None:
    ids = sorted(totals)
    for start in range(0, len(ids), WRITE_CHUNK):
        chunk = ids[start : start + WRITE_CHUNK]
        existing = set(
            (
                await session.scalars(
                    select(UsageSnapshot.subscription_id).where(
                        UsageSnapshot.day == day, UsageSnapshot.subscription_id.in_(chunk)
                    )
                )
            ).all()
        )
        for sub_id in chunk:
            if sub_id in existing:
                await _update(session, sub_id, day, totals[sub_id])
                continue
            try:
                async with session.begin_nested():
                    await session.execute(
                        insert(UsageSnapshot).values(subscription_id=sub_id, day=day, used_bytes=totals[sub_id])
                    )
            except IntegrityError:  # written concurrently (or the subscription was deleted)
                await _update(session, sub_id, day, totals[sub_id])


async def _update(session: AsyncSession, sub_id: int, day: str, used: int) -> None:
    await session.execute(
        update(UsageSnapshot)
        .where(UsageSnapshot.subscription_id == sub_id, UsageSnapshot.day == day)
        .values(used_bytes=used)
        .execution_options(synchronize_session=False)
    )


async def snapshot(
    session_factory: SessionFactory, registry: PanelSource, settings: Settings, *, now: datetime | None = None
) -> UsageReport:
    """Store today's traffic counter of every subscription (scheduler job, daily). Never raises for a panel."""
    day = today_local(settings, now or utcnow()).isoformat()
    report = UsageReport(day)
    async with read_session(session_factory) as session:
        plans, panels_of_sub = await _plan(session, settings)
    totals: dict[int, int] = defaultdict(int)
    read_ok: set[int] = set()
    for plan in plans:
        try:
            used = await _read_panel(registry, plan)
        except (PanelError, LookupError) as exc:
            report.panels_failed += 1
            report.errors.append(f"panel {plan.panel_id}: {exc}")
            log.warning("usage snapshot: panel %s failed: %s", plan.panel_id, exc)
            continue
        except Exception as exc:
            report.panels_failed += 1
            report.errors.append(f"panel {plan.panel_id}: {type(exc).__name__}")
            log.exception("usage snapshot: panel %s failed", plan.panel_id)
            continue
        report.panels_ok += 1
        read_ok.add(plan.panel_id)
        for sub_id, value in used.items():
            totals[sub_id] += value
    complete = {sub_id: totals.get(sub_id, 0) for sub_id, panels in panels_of_sub.items() if panels <= read_ok}
    report.skipped = len(panels_of_sub) - len(complete)
    if complete:
        async with session_factory.begin() as session:
            await _store(session, day, complete)
    report.stored = len(complete)
    return report


def _deltas(rows: list[tuple[str, int]]) -> dict[str, int]:
    """Per-day usage from ascending ``(day, cumulative)`` rows; a counter drop means a reset."""
    result: dict[str, int] = {}
    previous: tuple[date, int] | None = None
    for day_text, used in rows:
        day = date.fromisoformat(day_text)
        if previous is None or (day - previous[0]).days != 1:
            delta = 0 if previous is None else max(0, used - previous[1])
        else:
            delta = used - previous[1] if used >= previous[1] else used
        result[day_text] = delta
        previous = (day, used)
    return result


async def daily_usage(
    session: AsyncSession, subscription_id: int, settings: Settings, *, days: int = 30, now: datetime | None = None
) -> list[tuple[str, int]]:
    """``(YYYY-MM-DD, bytes)`` for the last ``days`` local days (0 for days without data)."""
    end = today_local(settings, now or utcnow())
    start = end - timedelta(days=max(1, days) - 1)
    rows = (
        await session.execute(
            select(UsageSnapshot.day, UsageSnapshot.used_bytes)
            .where(
                UsageSnapshot.subscription_id == subscription_id,
                UsageSnapshot.day >= (start - timedelta(days=1)).isoformat(),
                UsageSnapshot.day <= end.isoformat(),
            )
            .order_by(UsageSnapshot.day)
        )
    ).all()
    deltas = _deltas([(d, int(u)) for d, u in rows])
    return [
        ((start + timedelta(days=i)).isoformat(), deltas.get((start + timedelta(days=i)).isoformat(), 0))
        for i in range((end - start).days + 1)
    ]


async def total_daily_usage(
    session: AsyncSession, settings: Settings, *, days: int = 30, now: datetime | None = None
) -> list[tuple[str, int]]:
    """Traffic of all subscriptions per local day for the last ``days`` days (dashboard chart)."""
    end = today_local(settings, now or utcnow())
    start = end - timedelta(days=max(1, days) - 1)
    rows = (
        await session.execute(
            select(UsageSnapshot.subscription_id, UsageSnapshot.day, UsageSnapshot.used_bytes)
            .where(UsageSnapshot.day >= (start - timedelta(days=1)).isoformat(), UsageSnapshot.day <= end.isoformat())
            .order_by(UsageSnapshot.subscription_id, UsageSnapshot.day)
        )
    ).all()
    per_sub: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for sub_id, day, used in rows:
        per_sub[sub_id].append((day, int(used)))
    totals: dict[str, int] = defaultdict(int)
    for series in per_sub.values():
        for day, delta in _deltas(series).items():
            totals[day] += delta
    span = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    return [(day, totals.get(day, 0)) for day in span]


async def prune(
    session: AsyncSession, settings: Settings, *, keep_days: int = DEFAULT_KEEP_DAYS, now: datetime | None = None
) -> int:
    """Delete snapshots older than ``keep_days``; returns how many rows were removed."""
    cutoff = (today_local(settings, now or utcnow()) - timedelta(days=keep_days)).isoformat()
    result: Any = await session.execute(
        delete(UsageSnapshot).where(UsageSnapshot.day < cutoff).execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)
