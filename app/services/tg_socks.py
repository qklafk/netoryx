"""Personal SOCKS5 proxy for Telegram users, next to the MTProto proxy of :mod:`app.services.tg_proxy`.

Every user who may use the Telegram proxy also gets a SOCKS5 account (``tg<user id>`` + random password) on up to
``max_targets`` 3x-ui ``mixed`` inbounds that an admin selected for it (see
:func:`app.services.panels.inventory.tg_socks_enabled`). One :class:`~app.db.models.TgSocksAccess` row per
(user, panel); the password is stored encrypted and shown to the user in the bot, the Mini App and the
``tg://socks`` / ``https://t.me/socks`` links.

The access policy is exactly the MTProto one (:func:`app.services.tg_proxy.evaluate`: ``TG_PROXY_ACCESS``, staff,
subscription state, personal mode, bans). SOCKS5 accounts have no per-account switch, so a *disabled* account
(expired subscription, ban) is removed from the inbound's account list while its row keeps the credentials; an
*absent* one (retention over, member removed, inbound deselected) loses the row as well.

Lifecycle and consistency follow the MTProto service: :func:`ensure_access` (database only) creates rows,
:func:`sync_user` / :func:`reconcile` converge them, :func:`scan` repairs drift and removes orphans, :func:`rotate`
issues new passwords. The database is the source of truth; no transaction is held across panel calls and outcomes
are stored compare-and-set on ``TgSocksAccess.revision``. All changes of one inbound go out in a single
read-modify-write of its account list (:class:`~app.services.panels.socks.SocksProxyBackend`).

Panel options used here (3x-ui): ``tg_socks_host`` / ``tg_socks_port`` - the public address of the proxy when the
server sits behind NAT or a relay (default: ``tg_proxy_host``, else the panel URL host, and the inbound port).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    Inbound,
    Location,
    Panel,
    PanelKind,
    Plan,
    ProvisionState,
    Subscription,
    TgSocksAccess,
    User,
)
from app.db.session import SessionFactory, read_session
from app.services import tg_proxy
from app.services.panels.base import PanelBackend, PanelError
from app.services.panels.inventory import TG_SOCKS_FLAG, set_tg_socks_flag
from app.services.panels.socks import (
    SOCKS_PROTOCOLS,
    SocksLink,
    SocksProxyBackend,
    is_managed_username,
    new_password,
    username_for,
)
from app.services.tg_proxy import (
    DEFAULT_MAX_TARGETS,
    PanelSource,
    Policy,
    RotationTooSoonError,
    SecretCodec,
    backoff_delay,
    evaluate,
)
from app.utils.time import utcnow

log = logging.getLogger(__name__)

RECONCILE_BATCH_SIZE: Final = 500
MAX_ERROR_LENGTH: Final = 500


def _describe(exc: BaseException) -> str:
    return (str(exc) or type(exc).__name__)[:MAX_ERROR_LENGTH]


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _port(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536 else None


# ---------------------------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Target:
    """A 3x-ui ``mixed`` inbound that may hold SOCKS5 accounts."""

    panel_id: int
    inbound_id: int
    remote_id: int  # the inbound's id on the panel
    title: str
    sort_order: int
    active: bool  # selected for the proxy (otherwise its accounts are drained)
    serving: bool  # usable for new accounts right now
    healthy: bool
    host: str = ""
    port: int | None = None

    @property
    def key(self) -> tuple[int, int]:
        return self.panel_id, self.inbound_id


def build_target(
    settings: Settings, panel: Panel, inbound: Inbound, locations: dict[int, Location], health_threshold: int
) -> Target | None:
    """The SOCKS5 target of a panel/inbound pair; None if the inbound is not a mixed one."""
    if panel.kind != PanelKind.XUI:
        return None
    params = _json_object(inbound.params)
    if params.get("protocol") not in SOCKS_PROTOCOLS:
        return None
    options = _json_object(panel.options)
    selected = (
        settings.tg_socks_enabled and params.get(TG_SOCKS_FLAG) is True and params.get("local", True) is not False
    )
    location = locations.get(inbound.location_id) if inbound.location_id is not None else None
    title, order = tg_proxy.location_title(location, panel.name)
    host = str(options.get("tg_socks_host") or options.get("tg_proxy_host") or "") or (
        urlsplit(panel.url).hostname or ""
    )
    return Target(
        panel_id=panel.id,
        inbound_id=inbound.id,
        remote_id=inbound.remote_id,
        title=title,
        sort_order=order,
        active=panel.is_active and selected,
        serving=panel.is_active and selected and params.get("panel_enabled", True) is not False,
        healthy=panel.consecutive_failures < max(1, health_threshold),
        host=host,
        port=_port(options.get("tg_socks_port")) or _port(params.get("port")),
    )


async def _locations(session: AsyncSession) -> dict[int, Location]:
    return {loc.id: loc for loc in await session.scalars(select(Location))}


async def list_targets(session: AsyncSession, settings: Settings) -> list[Target]:
    """Every mixed inbound known to the bot (selected or not), in preference order."""
    locations = await _locations(session)
    threshold = settings.health_failures_before_alert
    stmt = (
        select(Panel, Inbound)
        .join(Inbound, Inbound.panel_id == Panel.id)
        .where(Panel.kind == PanelKind.XUI)
        .order_by(Inbound.sort_order, Inbound.id)
    )
    targets = [
        target
        for panel, inbound in (await session.execute(stmt)).all()
        if (target := build_target(settings, panel, inbound, locations, threshold)) is not None
    ]
    targets.sort(key=lambda t: (t.sort_order, t.panel_id, t.inbound_id))
    return targets


async def set_inbound(session: AsyncSession, inbound_id: int, enabled: bool) -> bool:
    """Select/deselect a mixed inbound for the SOCKS5 proxy (caller's transaction). False if it is not one."""
    inbound = await session.get(Inbound, inbound_id)
    return inbound is not None and set_tg_socks_flag(inbound, enabled)


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class EnsureResult:
    policy: Policy
    created: list[int] = field(default_factory=list)  # new TgSocksAccess ids
    no_targets: bool = False  # allowed, but no healthy SOCKS5 inbound is available


async def _target_loads(session: AsyncSession) -> dict[tuple[int, int], int]:
    stmt = select(TgSocksAccess.panel_id, TgSocksAccess.inbound_id, func.count()).group_by(
        TgSocksAccess.panel_id, TgSocksAccess.inbound_id
    )
    return {(panel_id, inbound_id): count for panel_id, inbound_id, count in (await session.execute(stmt)).all()}


async def _insert_access(session: AsyncSession, row: TgSocksAccess) -> bool:
    """Insert-if-absent (SAVEPOINT + IntegrityError): False if the user already has a row on that panel."""
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return False
    return True


async def ensure_access(
    session_factory: SessionFactory,
    settings: Settings,
    codec: SecretCodec,
    user_id: int,
    *,
    max_targets: int = DEFAULT_MAX_TARGETS,
    now: datetime | None = None,
) -> EnsureResult:
    """Create the user's SOCKS5 accounts (database only) if the policy allows it.

    Keeps ``max_targets`` usable rows, never two on one panel. Raises LookupError for an unknown user.
    """
    now = now or utcnow()
    async with session_factory.begin() as session:
        facts = await tg_proxy.load_facts(session, settings, user_id)
        if facts is None:
            raise LookupError(f"user {user_id} not found")
        policy = evaluate(settings, facts, now)
        result = EnsureResult(policy)
        if not policy.allowed:
            return result
        targets = await list_targets(session, settings)
        by_key = {t.key: t for t in targets}
        rows = list(await session.scalars(select(TgSocksAccess).where(TgSocksAccess.user_id == user_id)))
        usable = [r for r in rows if (t := by_key.get((r.panel_id, r.inbound_id))) and t.serving and t.healthy]
        used_panels = {r.panel_id for r in rows}
        need = max(1, max_targets) - len(usable)
        if need <= 0:
            return result
        loads = await _target_loads(session)
        candidates = [t for t in targets if t.serving and t.healthy and t.panel_id not in used_panels]
        candidates.sort(key=lambda t: (t.sort_order, loads.get(t.key, 0), t.panel_id, t.inbound_id))
        for target in candidates:
            if need <= 0:
                break
            if target.panel_id in used_panels:
                continue
            row = TgSocksAccess(
                user_id=user_id,
                panel_id=target.panel_id,
                inbound_id=target.inbound_id,
                username=username_for(user_id),
                password_enc=codec.encrypt(new_password()),
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ABSENT,
                revision=1,
                synced_revision=0,
                created_at=now,
                updated_at=now,
            )
            if await _insert_access(session, row):
                result.created.append(row.id)
                used_panels.add(target.panel_id)
                need -= 1
        result.no_targets = not usable and not result.created
    if result.created:
        log.info("tg socks: user %s got accounts %s", user_id, result.created)
    return result


async def mark_user_dirty(session: AsyncSession, user_id: int) -> int:
    """Make the next sync push the user's accounts now (ban/unban, subscription change). Caller's transaction."""
    result = await session.execute(
        update(TgSocksAccess)
        .where(TgSocksAccess.user_id == user_id)
        .values(revision=TgSocksAccess.revision + 1, next_attempt_at=None)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _rotate_row(row: TgSocksAccess, codec: SecretCodec, now: datetime) -> None:
    row.password_enc = codec.encrypt(new_password())
    row.revision += 1
    row.rotated_at = now
    row.next_attempt_at = None


async def rotate(
    session_factory: SessionFactory,
    codec: SecretCodec,
    user_id: int,
    *,
    min_interval: timedelta | None = None,
    now: datetime | None = None,
) -> int:
    """Issue new passwords for all the user's accounts (pushed by the next sync). Returns the rows rotated.

    With ``min_interval``, raises :class:`RotationTooSoonError` if any account was rotated more recently.
    """
    now = now or utcnow()
    async with session_factory.begin() as session:
        rows = list(
            await session.scalars(
                select(TgSocksAccess)
                .where(TgSocksAccess.user_id == user_id)
                .with_for_update()
                .order_by(TgSocksAccess.id)
            )
        )
        if min_interval is not None:
            last = max((r.rotated_at for r in rows if r.rotated_at is not None), default=None)
            if last is not None and now - last < min_interval:
                raise RotationTooSoonError(last + min_interval)
        for row in rows:
            _rotate_row(row, codec, now)
    if rows:
        log.info("tg socks: user %s rotated %d passwords", user_id, len(rows))
    return len(rows)


# ---------------------------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SocksEntry:
    """One SOCKS5 server of a user, ready to render."""

    access_id: int
    panel_id: int
    inbound_id: int
    title: str
    state: ProvisionState  # actual state on the server
    link: SocksLink | None  # None until the account is pushed (and while a rotation is being pushed)
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.link is not None


@dataclass(frozen=True, slots=True)
class SocksView:
    policy: Policy
    entries: tuple[SocksEntry, ...]

    @property
    def ready(self) -> tuple[SocksEntry, ...]:
        return tuple(e for e in self.entries if e.ready)


def _decrypt(codec: SecretCodec, value: str, access_id: int) -> str | None:
    try:
        return codec.decrypt(value)
    except ValueError:  # DecryptionError is a ValueError: the key changed; a new password is issued on the next sync
        log.warning("tg socks access %s: password cannot be decrypted; issuing a new one", access_id)
        return None


async def user_view(
    session_factory: SessionFactory,
    settings: Settings,
    codec: SecretCodec,
    user_id: int,
    *,
    now: datetime | None = None,
) -> SocksView:
    """The user's policy and SOCKS5 entries (credentials from the database; no network I/O)."""
    async with read_session(session_factory) as session:
        policy = await tg_proxy.user_policy(session, settings, user_id, now=now)
        stmt = (
            select(TgSocksAccess, Panel, Inbound)
            .join(Panel, TgSocksAccess.panel_id == Panel.id)
            .join(Inbound, TgSocksAccess.inbound_id == Inbound.id)
            .where(TgSocksAccess.user_id == user_id)
        )
        rows = (await session.execute(stmt)).all()
        locations = await _locations(session) if rows else {}
    entries: list[tuple[int, SocksEntry]] = []
    for access, panel, inbound in rows:
        target = build_target(settings, panel, inbound, locations, settings.health_failures_before_alert)
        if target is None or not target.active:
            continue
        link: SocksLink | None = None
        pushed = access.actual_state == ProvisionState.ENABLED and access.synced_revision == access.revision
        if policy.allowed and pushed and target.host and target.port and not access.last_error:
            password = _decrypt(codec, access.password_enc, access.id)
            if password:
                link = SocksLink(target.host, target.port, access.username, password)
        entry = SocksEntry(
            access_id=access.id,
            panel_id=access.panel_id,
            inbound_id=access.inbound_id,
            title=target.title,
            state=access.actual_state,
            link=link,
            error=access.last_error,
        )
        entries.append((target.sort_order, entry))
    entries.sort(key=lambda pair: (pair[0], pair[1].access_id))
    return SocksView(policy, tuple(entry for _, entry in entries))


# ---------------------------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class _Job:
    access_id: int
    user_id: int
    target: Target
    name: str
    revision: int
    desired: ProvisionState
    actual: ProvisionState
    password: str
    reissued: bool  # the stored password was unreadable: a new one is stored with the outcome
    noop: bool  # nothing to do on the server


@dataclass(slots=True)
class _Outcome:
    job: _Job
    error: str | None = None
    skipped: bool = False  # stale snapshot: retried later


_WorkRow = tuple[TgSocksAccess, User, Subscription | None, Plan | None, Panel, Inbound]


def _work_stmt() -> Any:
    return (
        select(TgSocksAccess, User, Subscription, Plan, Panel, Inbound)
        .select_from(TgSocksAccess)
        .join(User, TgSocksAccess.user_id == User.tg_id)
        .outerjoin_from(User, Subscription, Subscription.id == tg_proxy.policy_subscription_id())
        .outerjoin(Plan, Subscription.plan_id == Plan.id)
        .join(Panel, TgSocksAccess.panel_id == Panel.id)
        .join(Inbound, TgSocksAccess.inbound_id == Inbound.id)
        .order_by(TgSocksAccess.id)
    )


def _make_job(
    row: _WorkRow,
    settings: Settings,
    codec: SecretCodec,
    staff: set[int],
    locations: dict[int, Location],
    now: datetime,
) -> tuple[_Job, bool] | None:
    """(job, needs_work); None if the row's target cannot be resolved."""
    access, user, sub, plan, panel, inbound = row
    target = build_target(settings, panel, inbound, locations, settings.health_failures_before_alert)
    if target is None:
        return None
    policy = evaluate(settings, tg_proxy.user_facts(user, sub, plan, staff), now)
    desired = policy.state if target.active else ProvisionState.ABSENT
    password = _decrypt(codec, access.password_enc, access.id)
    reissued = password is None
    if password is None:
        password = new_password()
    if desired == ProvisionState.ABSENT:
        needs = True  # delete (idempotent) and drop the row; a failed earlier push may have left an account
        noop = False
    elif desired == ProvisionState.DISABLED:
        noop = access.actual_state != ProvisionState.ENABLED and access.last_error is None
        needs = (
            access.actual_state == ProvisionState.ENABLED
            or access.synced_revision != access.revision
            or access.desired_state != desired
            or access.last_error is not None
        )
    else:
        noop = False
        needs = (
            access.synced_revision != access.revision
            or access.actual_state != ProvisionState.ENABLED
            or access.last_error is not None
            or reissued
        )
    job = _Job(
        access_id=access.id,
        user_id=user.tg_id,
        target=target,
        name=access.username,
        revision=access.revision,
        desired=desired,
        actual=access.actual_state,
        password=password,
        reissued=reissued,
        noop=noop,
    )
    return job, needs


async def _load_jobs(
    session_factory: SessionFactory,
    settings: Settings,
    codec: SecretCodec,
    now: datetime,
    *,
    user_id: int | None = None,
    respect_backoff: bool = True,
    limit: int | None = None,
) -> tuple[list[_Job], int]:
    """Due jobs and the number of due rows left out because their panel is down."""
    stmt = _work_stmt()
    if user_id is not None:
        stmt = stmt.where(TgSocksAccess.user_id == user_id)
    if respect_backoff:
        stmt = stmt.where((TgSocksAccess.next_attempt_at.is_(None)) | (TgSocksAccess.next_attempt_at <= now))
    async with read_session(session_factory) as session:
        rows = (await session.execute(stmt)).all()
        staff = await tg_proxy.staff_ids(session, settings)
        locations = await _locations(session)
    jobs: list[_Job] = []
    down = 0
    for index, raw in enumerate(rows):
        if index and index % 1000 == 0:
            await asyncio.sleep(0)  # decrypting: let other tasks run
        made = _make_job(tuple(raw), settings, codec, staff, locations, now)
        if made is None:
            continue
        job, needs = made
        if not needs:
            continue
        if not job.target.healthy and job.target.active:
            down += 1
            continue
        jobs.append(job)
        if limit is not None and len(jobs) >= limit:
            break
    return jobs, down


# ---------------------------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------------------------
async def _socks_backend(registry: PanelSource, panel_id: int) -> SocksProxyBackend:
    backend: PanelBackend = await registry.get(panel_id)
    if not isinstance(backend, SocksProxyBackend):
        raise PanelError(f"panel {panel_id} cannot host SOCKS5 accounts")
    return backend


async def _is_stale(session_factory: SessionFactory, job: _Job) -> bool:
    async with read_session(session_factory) as session:
        revision = await session.scalar(select(TgSocksAccess.revision).where(TgSocksAccess.id == job.access_id))
    return revision != job.revision


async def _run_inbound(
    session_factory: SessionFactory, registry: PanelSource, target: Target, jobs: list[_Job]
) -> list[_Outcome]:
    """All changes of one inbound in a single account-list update."""
    live: list[_Job] = []
    outcomes: list[_Outcome] = []
    for job in jobs:
        if await _is_stale(session_factory, job):
            outcomes.append(_Outcome(job, skipped=True))
        else:
            live.append(job)
    work = [job for job in live if not job.noop]
    outcomes += [_Outcome(job) for job in live if job.noop]
    if not work:
        return outcomes
    put = {job.name: job.password for job in work if job.desired == ProvisionState.ENABLED}
    remove = [job.name for job in work if job.desired != ProvisionState.ENABLED]
    try:
        backend = await _socks_backend(registry, target.panel_id)
        await backend.apply_socks_users(target.remote_id, put, remove)
    except Exception as exc:  # the whole batch is retried with back-off
        log.warning("tg socks inbound %s on panel %s: %s", target.inbound_id, target.panel_id, _describe(exc))
        outcomes += [_Outcome(job, error=_describe(exc)) for job in work]
    else:
        outcomes += [_Outcome(job) for job in work]
    return outcomes


async def _store(
    session_factory: SessionFactory, codec: SecretCodec, outcomes: Sequence[_Outcome], now: datetime
) -> None:
    async with session_factory.begin() as session:
        for outcome in outcomes:
            job = outcome.job
            current = TgSocksAccess.id == job.access_id
            same_revision = TgSocksAccess.revision == job.revision
            if outcome.skipped:
                continue
            if outcome.error is not None:
                row = await session.get(TgSocksAccess, job.access_id)
                if row is not None:
                    row.attempts = (row.attempts or 0) + 1
                    row.last_error = outcome.error
                    row.next_attempt_at = now + backoff_delay(row.attempts)
                    row.desired_state = job.desired
                continue
            if job.desired == ProvisionState.ABSENT:
                await session.execute(delete(TgSocksAccess).where(current, same_revision))
                continue
            values: dict[str, Any] = {
                "desired_state": job.desired,
                "actual_state": ProvisionState.ENABLED if job.desired == ProvisionState.ENABLED else job.desired,
                "synced_revision": job.revision,
                "attempts": 0,
                "last_error": None,
                "next_attempt_at": None,
                "updated_at": now,
            }
            if job.desired == ProvisionState.DISABLED and job.actual == ProvisionState.ABSENT:
                values["actual_state"] = ProvisionState.ABSENT  # never was on the server
            if job.reissued:
                values["password_enc"] = codec.encrypt(job.password)
            await session.execute(update(TgSocksAccess).where(current, same_revision).values(**values))


@dataclass(slots=True)
class SyncReport:
    synced: int = 0
    failed: int = 0
    skipped: int = 0  # stale snapshot or panel down: retried later
    deleted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.skipped == 0


async def _run_jobs(
    session_factory: SessionFactory, registry: PanelSource, codec: SecretCodec, jobs: list[_Job], now: datetime
) -> SyncReport:
    by_inbound: dict[tuple[int, int], list[_Job]] = defaultdict(list)
    for job in jobs:
        by_inbound[job.target.key].append(job)
    # Inbounds of one panel go one after another (shared connection); different panels run in parallel.
    by_panel: dict[int, list[list[_Job]]] = defaultdict(list)
    for batch in by_inbound.values():
        by_panel[batch[0].target.panel_id].append(batch)

    async def run_panel(batches: list[list[_Job]]) -> list[_Outcome]:
        collected: list[_Outcome] = []
        for batch in batches:
            collected += await _run_inbound(session_factory, registry, batch[0].target, batch)
        return collected

    results = await asyncio.gather(*(run_panel(batches) for batches in by_panel.values()))
    outcomes = [outcome for panel_outcomes in results for outcome in panel_outcomes]
    await _store(session_factory, codec, outcomes, now)
    report = SyncReport()
    for outcome in outcomes:
        if outcome.skipped:
            report.skipped += 1
        elif outcome.error is not None:
            report.failed += 1
            report.errors.append(f"access {outcome.job.access_id}: {outcome.error}")
        elif outcome.job.desired == ProvisionState.ABSENT:
            report.deleted += 1
        else:
            report.synced += 1
    return report


async def sync_user(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    codec: SecretCodec,
    user_id: int,
    *,
    now: datetime | None = None,
) -> SyncReport:
    """Push the user's accounts now (ignores back-off; skips panels that health checks marked down)."""
    now = now or utcnow()
    jobs, down = await _load_jobs(session_factory, settings, codec, now, user_id=user_id, respect_backoff=False)
    report = await _run_jobs(session_factory, registry, codec, jobs, now) if jobs else SyncReport()
    report.skipped += down
    return report


async def open_socks(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    codec: SecretCodec,
    user_id: int,
    *,
    max_targets: int = DEFAULT_MAX_TARGETS,
    now: datetime | None = None,
) -> SocksView:
    """What the proxy screen calls: create accounts if needed, push them, and return the view."""
    await ensure_access(session_factory, settings, codec, user_id, max_targets=max_targets, now=now)
    await sync_user(session_factory, registry, settings, codec, user_id, now=now)
    return await user_view(session_factory, settings, codec, user_id, now=now)


async def remove_user(session_factory: SessionFactory, registry: PanelSource, user_id: int) -> SyncReport:
    """Delete the user's SOCKS5 accounts on the servers and their rows (a member removed, a user deleted).

    Rows whose server could not be reached are kept (``failed``) so the removal can be retried.
    """
    async with read_session(session_factory) as session:
        stmt = (
            select(TgSocksAccess.id, TgSocksAccess.username, TgSocksAccess.panel_id, Inbound.remote_id)
            .join(Inbound, TgSocksAccess.inbound_id == Inbound.id)
            .where(TgSocksAccess.user_id == user_id)
        )
        rows = [(r[0], r[1], r[2], r[3]) for r in await session.execute(stmt)]
    report = SyncReport()
    removed: list[int] = []
    for access_id, name, panel_id, remote_id in rows:
        try:
            backend = await _socks_backend(registry, panel_id)
            await backend.delete_socks_user(remote_id, name)
        except Exception as exc:
            report.failed += 1
            report.errors.append(f"access {access_id}: {_describe(exc)}")
            continue
        removed.append(access_id)
    if removed:
        async with session_factory.begin() as session:
            await session.execute(delete(TgSocksAccess).where(TgSocksAccess.id.in_(removed)))
        report.deleted = len(removed)
        log.info("tg socks: removed %d accounts of user %s", len(removed), user_id)
    return report


async def reconcile(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    codec: SecretCodec,
    *,
    batch_size: int = RECONCILE_BATCH_SIZE,
    now: datetime | None = None,
) -> SyncReport:
    """Converge due accounts (periodic job): policy changes, rotations, retries, drains of deselected inbounds."""
    now = now or utcnow()
    jobs, down = await _load_jobs(session_factory, settings, codec, now, limit=max(1, batch_size))
    report = await _run_jobs(session_factory, registry, codec, jobs, now) if jobs else SyncReport()
    report.skipped += down
    if report.synced or report.failed or report.deleted:
        log.info(
            "tg socks reconcile: %d synced, %d deleted, %d failed, %d skipped",
            report.synced,
            report.deleted,
            report.failed,
            report.skipped,
        )
    return report


# ---------------------------------------------------------------------------------------------
# Scan: drift and orphans
# ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class ScanReport:
    accounts: int = 0
    drifted: int = 0  # missing/changed on the server: re-pushed by the next reconcile
    orphans: list[str] = field(default_factory=list)  # "<panel>:<name>" accounts without a row
    orphans_deleted: int = 0
    errors: list[str] = field(default_factory=list)


async def _scan_target(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    codec: SecretCodec,
    target: Target,
    report: ScanReport,
) -> None:
    backend = await _socks_backend(registry, target.panel_id)
    remote = {a.user: a for a in await backend.list_socks_users(target.remote_id) if is_managed_username(a.user)}
    report.accounts += len(remote)
    async with read_session(session_factory) as session:  # read after listing: rows precede their accounts
        rows = list(
            await session.scalars(
                select(TgSocksAccess).where(
                    TgSocksAccess.panel_id == target.panel_id, TgSocksAccess.inbound_id == target.inbound_id
                )
            )
        )
    known = {row.username for row in rows}
    drifted: list[int] = []
    unexpected: list[int] = []  # present on the server although the row says it is not
    for row in rows:
        if row.synced_revision != row.revision:
            continue  # a push is pending anyway
        account = remote.get(row.username)
        if row.actual_state == ProvisionState.ENABLED:
            password = _decrypt(codec, row.password_enc, row.id)
            if account is None or account.password != password:
                drifted.append(row.id)
        elif account is not None:
            unexpected.append(row.id)
    if drifted or unexpected:
        async with session_factory.begin() as session:
            for ids, extra in ((drifted, {}), (unexpected, {"actual_state": ProvisionState.ENABLED})):
                if ids:  # "enabled" makes the next push remove the account instead of skipping it
                    await session.execute(
                        update(TgSocksAccess)
                        .where(TgSocksAccess.id.in_(ids))
                        .values(synced_revision=0, next_attempt_at=None, **extra)
                    )
    report.drifted += len(drifted) + len(unexpected)
    orphans = [name for name in remote if name not in known]
    report.orphans += [f"{target.panel_id}:{name}" for name in orphans]
    if orphans and settings.delete_orphan_clients:
        if not rows:
            log.warning(
                "tg socks panel %s: no accounts in the database, not deleting %d", target.panel_id, len(orphans)
            )
            return
        try:
            await backend.apply_socks_users(target.remote_id, {}, orphans)
            report.orphans_deleted += len(orphans)
        except Exception as exc:
            report.errors.append(f"panel {target.panel_id} orphans: {_describe(exc)}")


async def scan(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    codec: SecretCodec,
) -> ScanReport:
    """Read every selected healthy inbound once (periodic job): repair drift, remove orphans."""
    async with read_session(session_factory) as session:
        targets = [t for t in await list_targets(session, settings) if t.active and t.healthy]
    report = ScanReport()
    for target in targets:
        try:
            await _scan_target(session_factory, registry, settings, codec, target, report)
        except Exception as exc:
            log.warning("tg socks scan of panel %s failed: %s", target.panel_id, _describe(exc))
            report.errors.append(f"panel {target.panel_id}: {_describe(exc)}")
    return report
