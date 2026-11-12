"""Bulk subscription operations: compensation days for everyone active and a full re-push to the panels."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import KeyValue, Panel, PurchaseKind, Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory, read_session
from app.services import audit, kv, provisioning, subscriptions
from app.services.provisioning import PanelSource, ReconcileReport
from app.utils.security import random_token
from app.utils.time import MAX_PERIOD_DAYS, utcnow

log = logging.getLogger(__name__)

BATCH_SIZE = 100
RUN_KEY = "subscriptions:compensation"
Progress = Callable[[int, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Overview:
    active: int
    trial: int
    complimentary: int
    expiring_3d: int
    expiring_7d: int
    expired_30d: int
    disabled: int


def _active(now: datetime) -> tuple[object, ...]:
    return (Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at > now)


async def overview(session_factory: SessionFactory, now: datetime | None = None) -> Overview:
    now = now or utcnow()

    async def count(*where: object) -> int:
        return int(await session.scalar(select(func.count(Subscription.id)).where(*where)) or 0)  # type: ignore[arg-type]

    async with read_session(session_factory) as session, session.begin():
        return Overview(
            active=await count(*_active(now)),
            trial=await count(*_active(now), Subscription.is_trial.is_(True)),
            complimentary=await count(*_active(now), Subscription.is_complimentary.is_(True)),
            expiring_3d=await count(*_active(now), Subscription.expires_at <= now + timedelta(days=3)),
            expiring_7d=await count(*_active(now), Subscription.expires_at <= now + timedelta(days=7)),
            expired_30d=await count(
                Subscription.status != SubscriptionStatus.DISABLED,
                Subscription.expires_at <= now,
                Subscription.expires_at > now - timedelta(days=30),
            ),
            disabled=await count(Subscription.status == SubscriptionStatus.DISABLED),
        )


async def expiring(
    session_factory: SessionFactory, days: int, *, offset: int, limit: int, now: datetime | None = None
) -> list[tuple[Subscription, User]]:
    """Active subscriptions ending within ``days``, soonest first."""
    now = now or utcnow()
    stmt = (
        select(Subscription, User)
        .join(User, User.tg_id == Subscription.user_id)
        .where(*_active(now), Subscription.expires_at <= now + timedelta(days=days))  # type: ignore[arg-type]
        .order_by(Subscription.expires_at, Subscription.id)
        .offset(offset)
        .limit(limit)
    )
    async with read_session(session_factory) as session, session.begin():
        return [(sub, user) for sub, user in (await session.execute(stmt)).all()]


async def active_ids(session_factory: SessionFactory, now: datetime | None = None) -> list[int]:
    now = now or utcnow()
    async with read_session(session_factory) as session, session.begin():
        stmt = select(Subscription.id).where(*_active(now)).order_by(Subscription.id)  # type: ignore[arg-type]
        return list((await session.scalars(stmt)).all())


@dataclass(frozen=True, slots=True)
class CompensationRun:
    """A compensation in progress, kept in ``kv`` so an interrupted one is resumed exactly where it stopped.

    Subscriptions are processed in id order; the cursor (``last_id``) moves in the same transaction as the days
    granted, so every subscription gets the days once even if the run fails or the process stops midway.
    """

    run_id: str
    days: int
    actor_id: int
    max_id: int  # subscriptions created after the start are not part of the run
    last_id: int = 0
    done: int = 0
    total: int = 0
    started_at: str = ""

    def dump(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def load(cls, raw: str | None) -> CompensationRun | None:
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return cls(**{name: data[name] for name in cls.__dataclass_fields__ if name in data})
        except (ValueError, TypeError, KeyError):
            log.warning("ignoring a malformed compensation state")
            return None


class CompensationPending(Exception):
    """A previous compensation is unfinished: resume it instead of starting another one (days would double)."""

    def __init__(self, run: CompensationRun) -> None:
        super().__init__(f"compensation {run.run_id} is not finished ({run.done} of {run.total})")
        self.run = run


async def _read_run(session: AsyncSession, *, lock: bool = False) -> CompensationRun | None:
    stmt = select(KeyValue.value).where(KeyValue.key == RUN_KEY)
    if lock:
        stmt = stmt.with_for_update()
    return CompensationRun.load(await session.scalar(stmt))


async def pending_compensation(session_factory: SessionFactory) -> CompensationRun | None:
    """The unfinished compensation, if any."""
    async with read_session(session_factory) as session, session.begin():
        return await _read_run(session)


async def start_compensation(
    session_factory: SessionFactory, days: int, actor_id: int, now: datetime | None = None
) -> CompensationRun:
    """Record a new compensation for every subscription active right now (nothing is granted yet).

    Raises :class:`CompensationPending` while a previous one is unfinished.
    """
    if not 0 < days <= MAX_PERIOD_DAYS:
        raise ValueError(f"days must be 1..{MAX_PERIOD_DAYS}")
    now = now or utcnow()
    async with session_factory.begin() as session:
        existing = await _read_run(session, lock=True)
        if existing is not None:
            raise CompensationPending(existing)
        where = _active(now)
        total = int(await session.scalar(select(func.count(Subscription.id)).where(*where)) or 0)  # type: ignore[arg-type]
        max_id = int(await session.scalar(select(func.max(Subscription.id)).where(*where)) or 0)  # type: ignore[arg-type]
        run = CompensationRun(
            run_id=random_token(8),
            days=days,
            actor_id=actor_id,
            max_id=max_id,
            total=total,
            started_at=now.isoformat(),
        )
        if not await kv.add(session, RUN_KEY, run.dump()):  # another admin started one at the same moment
            winner = CompensationRun.load(await kv.get(session, RUN_KEY, fresh=True))
            raise CompensationPending(winner or run)
        await audit.log(
            session, actor_id, "subscriptions.compensate", "subscription", None, days=days, count=total, run=run.run_id
        )
    return run


async def resume_compensation(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    progress: Progress | None = None,
    batch_size: int | None = None,
) -> int:
    """Continue the unfinished compensation until it is done; returns how many subscriptions got the days in total.

    Each batch commits the days together with the cursor, so an error or a stop loses at most the current batch
    (rolled back as a whole) and calling this again picks up from there. Returns 0 if nothing is pending.
    Subscriptions that are no longer active when their turn comes are skipped.
    """
    size = max(1, batch_size or BATCH_SIZE)
    run: CompensationRun | None = None
    while True:
        async with session_factory.begin() as session:
            current = await _read_run(session, lock=True)
            if current is None or (run is not None and current.run_id != run.run_id):
                break  # finished (or replaced) elsewhere
            run = current
            stmt = (
                select(Subscription.id, User)
                .join(User, User.tg_id == Subscription.user_id)
                .where(*_active(utcnow()), Subscription.id > run.last_id, Subscription.id <= run.max_id)  # type: ignore[arg-type]
                .order_by(Subscription.id)
                .limit(size)
            )
            rows = [(int(sub_id), user) for sub_id, user in (await session.execute(stmt)).all()]
            if not rows:
                await kv.delete(session, RUN_KEY)
                break
            for _sub_id, user in rows:
                await subscriptions.grant_days(session, settings, user, run.days, kind=PurchaseKind.ADMIN_GRANT)
            run = replace(run, last_id=rows[-1][0], done=run.done + len(rows))
            await kv.set(session, RUN_KEY, run.dump())
        if progress is not None:
            await progress(run.done, max(run.total, run.done))
    if run is None:
        return 0
    log.info("admin %s: compensation of %d days for %d subscriptions", run.actor_id, run.days, run.done)
    return run.done


async def compensate(
    session_factory: SessionFactory,
    settings: Settings,
    days: int,
    actor_id: int,
    *,
    progress: Progress | None = None,
    batch_size: int | None = None,
) -> int:
    """Add ``days`` to every subscription active right now (e.g. after an outage); returns how many got them.

    Works in small transactions; the panels pick the new expiry dates up with the next reconcile. If it stops
    midway, :func:`resume_compensation` finishes it; a new one is refused (:class:`CompensationPending`) until then.
    """
    await start_compensation(session_factory, days, actor_id)
    return await resume_compensation(session_factory, settings, progress=progress, batch_size=batch_size)


async def resync_all(
    session_factory: SessionFactory, registry: PanelSource, settings: Settings, actor_id: int
) -> tuple[int, ReconcileReport]:
    """Mark every account on every active panel for a re-push and run one reconcile pass now.

    Returns the number of accounts marked and the report of the pass (the rest follows in the background job).
    """
    async with session_factory.begin() as session:
        panel_ids = list(await session.scalars(select(Panel.id).where(Panel.is_active.is_(True))))
        marked = 0
        for panel_id in panel_ids:
            marked += await provisioning.mark_dirty(session, panel_id=panel_id)
        await audit.log(
            session, actor_id, "subscriptions.resync_all", "panel", None, panels=len(panel_ids), marked=marked
        )
    report = await provisioning.reconcile(session_factory, registry, settings, scan_orphans=False)
    return marked, report
