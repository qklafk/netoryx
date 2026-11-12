"""Provisioning: keep every subscription's panel accounts in line with the database.

Model
-----
A :class:`~app.db.models.Provision` is one account of one subscription on one *target*:
``(panel, inbound)`` for 3x-ui and ``(panel)`` for Remnawave. Business code never talks to panels: it
changes the subscription, bumps ``Subscription.revision`` and calls :func:`ensure_provisions`.
:func:`reconcile` (periodic job) and :func:`sync_subscription` ("push now") then converge the panels.

A provision needs work when its synced revision is stale, its actual state differs from the desired one,
its last attempt failed, a new traffic period started (usage is reset on the panel), the subscription crossed the
retention period (accounts are then deleted), or the user was banned/unbanned (a banned user's accounts are
disabled). Accounts on deactivated panels/inbounds are drained (deleted) - also on panels that health checks had
marked down before they were deactivated - and recreated when the target is activated again. The periodic panel
scan compares every settled account with the panel's copy: an account deleted or changed on the panel is marked
dirty and re-pushed. Settings that shape the pushed account but do not live on the subscription (inbound flow,
Remnawave squads and options) are fingerprinted on every reconcile; a change marks the target's provisions dirty
(:func:`mark_dirty`). Failures back off exponentially via ``next_attempt_at`` (30 s doubling up to 30 min).

The plan's traffic quota belongs to the subscription, not to one account: the scan also sums the current period's
usage of every account of a subscription (one per 3x-ui inbound, one per Remnawave panel) and, once the sum reaches
the quota, flags the subscription (``traffic_exhausted_epoch``) so all its accounts are disabled until a new period
starts. The flag is lifted again when a complete scan sees the sum below the quota (e.g. the quota was raised).

Every panel round-trip is *read -> close transaction -> call panel -> open transaction -> write*: no
database transaction (and no row lock) is ever held across network I/O. Instead, pushes of one provision are
serialized in-process, ``Subscription.revision`` is re-checked right before each call (a stale snapshot is
skipped, never pushed over a newer one), and outcomes are stored compare-and-set on ``synced_revision``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import ColumnElement, CursorResult, Row, and_, exists, func, insert, not_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.config import Settings
from app.db.models import (
    Inbound,
    Panel,
    PanelKind,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory, read_session
from app.services import kv, links
from app.services.panels import shadowsocks
from app.services.panels.base import AccountSpec, PanelBackend, PanelError, RemoteAccount
from app.utils.bytes import gb_to_bytes
from app.utils.time import utcnow

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30  # mirrors Settings.expired_client_retention_days
DEFAULT_USERNAME_PREFIX = "tg_"  # Remnawave usernames: f"{prefix}{tg_id}"
XUI_EMAIL_RE = re.compile(r"tg[0-9]+(?:-s[0-9]+)?-[0-9]+")
CONFIG_KEY_PREFIX = "prov:cfg:"  # kv: fingerprint of a target's account-shaping settings

BACKOFF_BASE = timedelta(seconds=30)
BACKOFF_MAX = timedelta(minutes=30)
RECONCILE_BATCH_SIZE = 500  # provisions pushed per reconcile run
PANEL_ABORT_AFTER_FAILURES = 3  # consecutive failures that stop a panel's batch (it is probably down)
MAX_ERROR_LENGTH = 500
INSERT_CHUNK_SIZE = 500
SCAN_CHUNK_SIZE = 2000  # provisions per partition of the panel scan (the event loop gets a turn between them)
EXPIRY_DRIFT_TOLERANCE = timedelta(minutes=1)  # panels store expiry in ms / ISO: tiny differences are not drift

_ABSENT = ProvisionState.ABSENT


class PanelSource(Protocol):
    """What provisioning needs from :class:`app.services.panels.registry.PanelRegistry`."""

    async def get(self, panel_id: int) -> PanelBackend: ...


# ---------------------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OrphanAccount:
    """A panel account that looks like ours (email pattern) but has no live provision."""

    panel_id: int
    target: int | None
    email: str
    uuid: str
    deleted: bool = False
    error: str | None = None


@dataclass(slots=True)
class SyncResult:
    """Outcome of :func:`sync_subscription`."""

    subscription_id: int
    found: bool = True
    synced: int = 0
    failed: int = 0
    # Targets not attempted (panel down, gave up mid-batch, or the subscription changed meanwhile and the
    # newer revision is pushed by its own sync); reconcile retries them.
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Every target of the subscription is in its desired state."""
        return self.found and self.failed == 0 and self.skipped == 0


@dataclass(slots=True)
class ReconcileReport:
    """Outcome of :func:`reconcile`."""

    provisions_created: int = 0
    synced: int = 0
    failed: int = 0
    skipped: int = 0  # due provisions not attempted: panel stopped responding mid-batch, or snapshot went stale
    panels_down: list[int] = field(default_factory=list)  # active panels skipped by health
    orphans: list[OrphanAccount] = field(default_factory=list)
    # Accounts found missing or changed on their panel by the scan; marked dirty, re-pushed by the next reconcile.
    drifted: int = 0
    # Subscriptions whose summed traffic reached the plan's quota in this scan (their accounts get disabled).
    quota_exhausted: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------------------------
def desired_state(
    sub: Subscription, now: datetime, *, retention_days: int = DEFAULT_RETENTION_DAYS, banned: bool = False
) -> ProvisionState:
    """Which state the subscription's panel accounts should be in at ``now``.

    ACTIVE and not expired -> ENABLED (DISABLED if the user is ``banned`` or the period's traffic quota is used
    up, see :func:`is_traffic_exhausted`); DISABLED (admin block) -> DISABLED; expired (EXPIRED, or ACTIVE past
    ``expires_at`` before :func:`subscriptions.expire_due` ran) -> DISABLED during the retention period and ABSENT
    (deleted from panels) once it is over; the same retention applies to an admin-blocked subscription.
    """
    return _desired(sub.status, sub.expires_at, now, retention_days, banned, is_traffic_exhausted(sub))


def is_traffic_exhausted(sub: Subscription) -> bool:
    """The traffic used in the current period, summed over all panel accounts, reached the plan's quota."""
    epoch = sub.traffic_epoch or 0
    return (
        traffic_limit_bytes(sub) > 0
        and sub.traffic_exhausted_epoch is not None
        and sub.traffic_exhausted_epoch == epoch
    )


def traffic_limit_bytes(sub: Subscription) -> int:
    """Effective period quota, including an exact imported limit or a paid traffic pack."""
    if sub.traffic_limit_bytes is not None:
        return sub.traffic_limit_bytes
    return gb_to_bytes(max(0, sub.traffic_limit_gb or 0))


def _exhausted(
    limit_gb: int | None, epoch: int | None, exhausted_epoch: int | None, limit_bytes: int | None = None
) -> bool:
    quota = limit_bytes if limit_bytes is not None else gb_to_bytes(max(0, limit_gb or 0))
    return quota > 0 and exhausted_epoch is not None and exhausted_epoch == (epoch or 0)


def _exhausted_sql() -> ColumnElement[bool]:
    """SQL mirror of :func:`is_traffic_exhausted`. Needs Subscription joined."""
    return and_(
        or_(
            Subscription.traffic_limit_bytes > 0,
            and_(Subscription.traffic_limit_bytes.is_(None), Subscription.traffic_limit_gb > 0),
        ),
        Subscription.traffic_exhausted_epoch.is_not(None),
        Subscription.traffic_exhausted_epoch == Subscription.traffic_epoch,
    )


def _not_exhausted_sql() -> ColumnElement[bool]:
    """SQL: the negation of :func:`_exhausted_sql`, NULL-safe."""
    return or_(
        and_(Subscription.traffic_limit_bytes.is_(None), Subscription.traffic_limit_gb <= 0),
        Subscription.traffic_exhausted_epoch.is_(None),
        Subscription.traffic_exhausted_epoch != Subscription.traffic_epoch,
    )


def _desired(
    status: SubscriptionStatus,
    expires_at: datetime,
    now: datetime,
    retention_days: int,
    banned: bool = False,
    exhausted: bool = False,
) -> ProvisionState:
    if status == SubscriptionStatus.ACTIVE and expires_at > now:
        return ProvisionState.DISABLED if banned or exhausted else ProvisionState.ENABLED
    # Retention applies to admin blocks too: a long-expired blocked subscription needs no accounts at all.
    if now - expires_at > timedelta(days=max(0, retention_days)):
        return ProvisionState.ABSENT
    return ProvisionState.DISABLED


def backoff_delay(attempts: int) -> timedelta:
    """Delay before retry number ``attempts`` (1-based): 30 s, 1 min, 2 min, ... capped at 30 min."""
    exponent = min(max(attempts, 1) - 1, 16)
    return min(BACKOFF_BASE * (2**exponent), BACKOFF_MAX)


def panel_options(panel: Panel) -> dict[str, Any]:
    """``Panel.options`` parsed as a JSON object; ``{}`` when empty or malformed."""
    if not panel.options:
        return {}
    try:
        data = json.loads(panel.options)
    except ValueError:
        log.warning("panel %s: options are not valid JSON, ignoring them", panel.id)
        return {}
    return data if isinstance(data, dict) else {}


def username_prefix(panel: Panel) -> str:
    """Remnawave username prefix from panel options (``"username_prefix"``), default ``"tg_"``."""
    prefix = panel_options(panel).get("username_prefix")
    return prefix if isinstance(prefix, str) and prefix else DEFAULT_USERNAME_PREFIX


def account_email(panel: Panel, user_id: int, inbound_id: int | None, subscription_id: int | None = None) -> str:
    """Our account name on a panel: ``f"{prefix}{user_id}"`` (Remnawave), ``f"tg{user_id}-{inbound_id}"`` (3x-ui).

    ``inbound_id`` is the database id of the inbound: 3x-ui emails must be unique across a whole panel.
    """
    if panel.kind in (
        PanelKind.REMNAWAVE,
        PanelKind.MARZBAN,
        PanelKind.HIDDIFY,
        PanelKind.PASARGUARD,
        PanelKind.MARZNESHIN,
    ):
        suffix = f"_s{subscription_id}" if subscription_id is not None else ""
        value = f"{username_prefix(panel)}{user_id}{suffix}"
        if panel.kind == PanelKind.MARZBAN and len(value) > 32:
            return f"tg_s{subscription_id}_{hashlib.sha256(value.encode()).hexdigest()[:8]}"[:32]
        return value
    if inbound_id is None:
        raise ValueError("3x-ui accounts are per inbound; inbound_id is required")
    suffix = f"-s{subscription_id}" if subscription_id is not None else ""
    return f"tg{user_id}{suffix}-{inbound_id}"


def is_managed_email(panel: Panel, email: str) -> bool:
    """True if ``email`` follows the naming scheme this bot uses on ``panel`` (orphan candidates)."""
    if panel.kind in (
        PanelKind.REMNAWAVE,
        PanelKind.MARZBAN,
        PanelKind.HIDDIFY,
        PanelKind.PASARGUARD,
        PanelKind.MARZNESHIN,
    ):
        prefix = username_prefix(panel)
        rest = email[len(prefix) :]
        return email.startswith(prefix) and re.fullmatch(r"[0-9]+(?:_s[0-9]+)?", rest) is not None
    return XUI_EMAIL_RE.fullmatch(email) is not None


def derive_sub_id(client_uuid: str, email: str) -> str:
    """Deterministic 3x-ui ``subId`` (16 chars of [0-9a-f]) of one account that does not reveal the client UUID.

    Unique per account: 3x-ui v3 rejects a ``subId`` already used by a client with another email, and every
    inbound gets its own client.
    """
    return hashlib.sha256(f"vpn-bot:sub-id:{client_uuid}:{email}".encode()).hexdigest()[:16]


def parse_params(raw: str | None) -> dict[str, Any]:
    """Cached inbound params parsed as a JSON object; ``{}`` when empty or malformed."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def inbound_params(inbound: Inbound) -> dict[str, Any]:
    """``Inbound.params`` parsed as a JSON object; ``{}`` when empty or malformed."""
    return parse_params(inbound.params)


def account_flow(inbound: Inbound | None) -> str:
    """The inbound's flow (e.g. ``xtls-rprx-vision``) where Vision can run, else ``""``.

    Same rule as the share links (:func:`app.services.links.flow_allowed` plus the panel's ``flow_allowed``
    hint): Xray rejects a client whose requested flow differs from its account's, so they must agree.
    """
    if inbound is None or not inbound.flow:
        return ""
    params = inbound_params(inbound)
    if params.get("flow_allowed", True) is not False and links.flow_allowed(params):
        return inbound.flow
    return ""


def account_password(inbound: Inbound | None, client_uuid: str) -> str:
    """The Shadowsocks client key of a subscription on ``inbound``; ``""`` for any other kind of inbound."""
    if inbound is None:
        return ""
    return shadowsocks.client_identity(inbound_params(inbound), client_uuid, inbound.id) or ""


def _comment(user: User) -> str:
    return f"tg:{user.tg_id} @{user.username}" if user.username else f"tg:{user.tg_id}"


def panel_device_limit(kind: PanelKind, device_limit: int, settings: Settings) -> int:
    """The device limit pushed to a panel account.

    Panels without a native limit get 0 (the bot counts devices, see :mod:`app.services.hwid`); so does 3x-ui
    unless ``XUI_DEVICE_LIMIT=ip`` asks for ``limitIp``.
    """
    if kind in (PanelKind.MARZBAN, PanelKind.HIDDIFY, PanelKind.MARZNESHIN):
        return 0
    if kind == PanelKind.XUI and settings.xui_device_limit != "ip":
        return 0
    return max(0, device_limit)


def build_spec(
    sub: Subscription,
    user: User,
    provision: Provision,
    panel: Panel,
    inbound: Inbound | None,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> AccountSpec:
    """The full desired account for one provision.

    The stored ``provision.remote_email`` wins over the computed name so that changing the Remnawave
    prefix never orphans existing accounts.

    Limits: the traffic quota is enforced per subscription (the scan sums the usage of every account and disables
    them all once it reaches the plan's quota); each account also carries the whole quota so the panel stops it
    without delay when a single account uses it up. The device limit applies per account (3x-ui ``limitIp`` per
    inbound client, Remnawave HWID limit per user): panels cannot count devices across accounts, and splitting a
    small limit between targets would lock users out of some of them.
    """
    state = desired_state(
        sub, now or utcnow(), retention_days=settings.expired_client_retention_days, banned=user.is_banned
    )
    email = provision.remote_email or account_email(
        panel, user.tg_id, inbound.id if inbound else None, sub.id if sub.is_legacy is False else None
    )
    return AccountSpec(
        email=email,
        uuid=sub.client_uuid,
        enabled=state == ProvisionState.ENABLED,
        expires_at=sub.expires_at,
        device_limit=panel_device_limit(panel.kind, sub.device_limit, settings),
        traffic_limit_bytes=sub.traffic_limit_bytes
        if sub.traffic_limit_bytes is not None
        else gb_to_bytes(max(0, sub.traffic_limit_gb)),
        tg_id=user.tg_id,
        sub_id=derive_sub_id(sub.client_uuid, email),
        comment=_comment(user),
        flow=account_flow(inbound) if panel.kind == PanelKind.XUI else "",
        password=account_password(inbound, sub.client_uuid) if panel.kind == PanelKind.XUI else "",
    )


def _health_threshold(settings: Settings) -> int:
    return max(1, settings.health_failures_before_alert)


def is_panel_down(panel: Panel, settings: Settings) -> bool:
    """Health checks marked the panel unreachable: provisioning leaves it alone until it recovers."""
    return panel.consecutive_failures >= _health_threshold(settings)


def _describe(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:MAX_ERROR_LENGTH]


# ---------------------------------------------------------------------------------------------
# Provision rows
# ---------------------------------------------------------------------------------------------
async def _active_targets(session: AsyncSession) -> list[tuple[int, int | None]]:
    """``(panel_id, inbound_id)`` of every active 3x-ui inbound and ``(panel_id, None)`` per Remnawave panel."""
    xui = await session.execute(
        select(Inbound.panel_id, Inbound.id)
        .join(Panel, Inbound.panel_id == Panel.id)
        .where(Panel.is_active.is_(True), Panel.kind == PanelKind.XUI, Inbound.is_active.is_(True))
        .order_by(Inbound.panel_id, Inbound.id)
    )
    remnawave = await session.scalars(
        select(Panel.id)
        .where(
            Panel.is_active.is_(True),
            Panel.kind.in_(
                (PanelKind.REMNAWAVE, PanelKind.MARZBAN, PanelKind.HIDDIFY, PanelKind.PASARGUARD, PanelKind.MARZNESHIN)
            ),
        )
        .order_by(Panel.id)
    )
    targets: list[tuple[int, int | None]] = [(panel_id, inbound_id) for panel_id, inbound_id in xui]
    targets.extend((panel_id, None) for panel_id in remnawave)
    return targets


def _new_provision(sub_id: int, panel_id: int, inbound_id: int | None, desired: ProvisionState) -> dict[str, Any]:
    return {
        "subscription_id": sub_id,
        "panel_id": panel_id,
        "inbound_id": inbound_id,
        "inbound_key": inbound_id or 0,
        "desired_state": desired,
        "actual_state": _ABSENT,
        "synced_revision": 0,
        "attempts": 0,
    }


async def _insert_provisions(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> int:
    """Insert-if-absent in chunks; a chunk that races with another writer is retried row by row."""
    inserted = 0
    for start in range(0, len(rows), INSERT_CHUNK_SIZE):
        chunk = rows[start : start + INSERT_CHUNK_SIZE]
        try:
            async with session.begin_nested():
                await session.execute(insert(Provision), list(chunk))
            inserted += len(chunk)
        except IntegrityError:
            for row in chunk:
                try:
                    async with session.begin_nested():
                        await session.execute(insert(Provision), [row])
                    inserted += 1
                except IntegrityError:
                    pass  # created concurrently: exactly what we wanted
    return inserted


async def ensure_provisions(session: AsyncSession, sub: Subscription) -> None:
    """Create the missing Provision rows of ``sub`` for every active target. Removes nothing.

    Runs in the caller's transaction; the reconciler pushes the new rows to the panels.
    """
    if sub.id is None:
        await session.flush()
    existing = set(
        (
            await session.execute(
                select(Provision.panel_id, Provision.inbound_key).where(Provision.subscription_id == sub.id)
            )
        ).all()
    )
    banned = bool(await session.scalar(select(User.is_banned).where(User.tg_id == sub.user_id)))
    desired = desired_state(sub, utcnow(), banned=banned)
    rows = [
        _new_provision(sub.id, panel_id, inbound_id, desired)
        for panel_id, inbound_id in await _active_targets(session)
        if (panel_id, inbound_id or 0) not in existing
    ]
    if rows:
        await _insert_provisions(session, rows)


async def backfill_provisions(session: AsyncSession, settings: Settings, now: datetime) -> int:
    """Create provisions on newly added targets for every subscription that still needs accounts.

    Subscriptions expired beyond the retention period are skipped. Returns the number of rows created.
    """
    retention = settings.expired_client_retention_days
    cutoff = now - timedelta(days=max(0, retention))
    created = 0
    for panel_id, inbound_id in await _active_targets(session):
        has_provision = exists().where(
            Provision.subscription_id == Subscription.id,
            Provision.panel_id == panel_id,
            Provision.inbound_key == (inbound_id or 0),
        )
        missing = await session.execute(
            select(
                Subscription.id,
                Subscription.status,
                Subscription.expires_at,
                User.is_banned,
                Subscription.traffic_limit_gb,
                Subscription.traffic_epoch,
                Subscription.traffic_exhausted_epoch,
            )
            .join(User, Subscription.user_id == User.tg_id)
            .where(
                ~has_provision,
                Subscription.expires_at >= cutoff,
            )
        )
        rows = [
            _new_provision(
                sub_id,
                panel_id,
                inbound_id,
                _desired(status, expires_at, now, retention, banned, _exhausted(limit_gb, epoch, exhausted_epoch)),
            )
            for sub_id, status, expires_at, banned, limit_gb, epoch, exhausted_epoch in missing
        ]
        if rows:
            created += await _insert_provisions(session, rows)
    return created


# ---------------------------------------------------------------------------------------------
# Jobs: read phase
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Job:
    provision_id: int
    subscription_id: int
    panel_id: int
    target: int | None
    desired: ProvisionState
    actual: ProvisionState
    spec: AccountSpec
    revision: int
    attempts: int
    base_revision: int = 0  # Provision.synced_revision when read: the outcome is stored compare-and-set on it
    traffic_epoch: int = 0
    reset_traffic: bool = False  # zero the panel's usage counters before the push (new traffic period)

    @property
    def needs_call(self) -> bool:
        """An account that is absent and should stay absent needs no panel call."""
        return not (self.desired == _ABSENT and self.actual == _ABSENT)


_WorkRow = Row[Provision, Subscription, User, Panel, Inbound]  # Inbound is None for Remnawave


def _target_active() -> ColumnElement[bool]:
    """SQL: the provision's panel (and, for 3x-ui, its inbound) is active. Needs Panel and Inbound joined."""
    return and_(Panel.is_active.is_(True), or_(Provision.inbound_id.is_(None), Inbound.is_active.is_(True)))


def _is_target_active(panel: Panel, inbound: Inbound | None) -> bool:
    if inbound is not None and shadowsocks.blocked_reason(inbound_params(inbound)):
        return False  # a Shadowsocks inbound that cannot serve subscriptions (deprecated method, ...) is drained
    return panel.is_active and (inbound is None or inbound.is_active)


def _work_query() -> Select[Provision, Subscription, User, Panel, Inbound]:
    """Provisions with everything needed to build a spec.

    Deactivated panels/inbounds only contribute provisions whose account may still exist: they are drained.
    """
    return (
        select(Provision, Subscription, User, Panel, Inbound)
        .join(Subscription, Provision.subscription_id == Subscription.id)
        .join(User, Subscription.user_id == User.tg_id)
        .join(Panel, Provision.panel_id == Panel.id)
        .outerjoin(Inbound, Provision.inbound_id == Inbound.id)
        .where(or_(_target_active(), Provision.actual_state != _ABSENT))
    )


def _make_job(row: _WorkRow, settings: Settings, now: datetime) -> _Job | None:
    provision, sub, user, panel, inbound = row
    if panel.kind == PanelKind.XUI and inbound is None:
        log.warning("provision %s: 3x-ui provision without an inbound, skipping", provision.id)
        return None
    spec = build_spec(sub, user, provision, panel, inbound, settings, now=now)
    if _is_target_active(panel, inbound):
        desired = desired_state(sub, now, retention_days=settings.expired_client_retention_days, banned=user.is_banned)
    else:
        desired = _ABSENT  # target retired: its accounts must not keep working (blocks, retention...)
    reset = (
        desired != _ABSENT
        and provision.actual_state != _ABSENT  # a new account starts with zero usage
        and provision.traffic_epoch != sub.traffic_epoch
        and spec.traffic_limit_bytes > 0
    )
    return _Job(
        provision_id=provision.id,
        subscription_id=sub.id,
        panel_id=panel.id,
        target=inbound.remote_id if inbound is not None and panel.kind == PanelKind.XUI else None,
        desired=desired,
        actual=provision.actual_state,
        spec=spec,
        revision=sub.revision,
        attempts=provision.attempts,
        base_revision=provision.synced_revision,
        traffic_epoch=sub.traffic_epoch,
        reset_traffic=reset,
    )


def _needs_work(job: _Job, row: _WorkRow) -> bool:
    provision, sub = row[0], row[1]
    return (
        job.desired != job.actual
        or provision.synced_revision != sub.revision
        or provision.last_error is not None
        or provision.traffic_epoch != sub.traffic_epoch
    )


def _due_query(settings: Settings, now: datetime) -> Select[Provision, Subscription, User, Panel, Inbound]:
    retention_cutoff = now - timedelta(days=max(0, settings.expired_client_retention_days))
    # Exactly the subscriptions whose desired_state() is ABSENT (see _desired).
    live_absent = Subscription.expires_at < retention_cutoff
    live_active = and_(
        Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at > now, _not_exhausted_sql()
    )
    return _work_query().where(
        or_(Provision.next_attempt_at.is_(None), Provision.next_attempt_at <= now),
        or_(
            Provision.synced_revision != Subscription.revision,
            Provision.actual_state != Provision.desired_state,
            Provision.last_error.is_not(None),
            Provision.traffic_epoch != Subscription.traffic_epoch,
            # Retention elapsed without a revision bump: the account must be deleted.
            and_(live_absent, Provision.actual_state != _ABSENT),
            # Deleted on purpose, but the subscription lives again or its target was reactivated.
            and_(Provision.desired_state == _ABSENT, not_(live_absent)),
            # Target deactivated: drain the account (_work_query keeps only rows whose account may exist).
            not_(_target_active()),
            # The user was banned / unbanned (a flag on users, no revision bump): disable / re-enable the account.
            # The traffic quota was used up (also bumps the revision; kept here so the rule is self-contained).
            and_(or_(User.is_banned.is_(True), _exhausted_sql()), Provision.actual_state == ProvisionState.ENABLED),
            and_(User.is_banned.is_(False), live_active, Provision.actual_state == ProvisionState.DISABLED),
        ),
    )


async def _load_due_jobs(session: AsyncSession, settings: Settings, now: datetime, limit: int) -> list[_Job]:
    """Due jobs of every healthy panel, at most ``limit`` per panel: one panel's backlog never starves another.

    Within a panel, rows that failed fewer times come first, so permanently failing rows cannot pin the head of
    the queue while fresh work waits behind them. Deactivated panels are drained whatever their health record says:
    health checks no longer ping them, so a panel retired while marked down would otherwise keep its accounts
    forever; the per-panel failure streak and per-row backoff keep a really dead one cheap.
    """
    panel_ids = await session.scalars(
        select(Panel.id)
        .where(or_(Panel.consecutive_failures < _health_threshold(settings), Panel.is_active.is_(False)))
        .order_by(Panel.id)
    )
    base = _due_query(settings, now)
    jobs: list[_Job] = []
    for panel_id in list(panel_ids):
        stmt = base.where(Provision.panel_id == panel_id).order_by(Provision.attempts, Provision.id).limit(limit)
        for row in (await session.execute(stmt)).all():
            job = _make_job(row, settings, now)
            if job is not None and _needs_work(job, row):
                jobs.append(job)
    return jobs


async def _down_panel_ids(session: AsyncSession, settings: Settings) -> list[int]:
    stmt = select(Panel.id).where(Panel.is_active.is_(True), Panel.consecutive_failures >= _health_threshold(settings))
    return list((await session.scalars(stmt.order_by(Panel.id))).all())


# ---------------------------------------------------------------------------------------------
# Jobs: panel phase (no DB transaction is open here)
# ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class _Outcome:
    job: _Job
    account: RemoteAccount | None = None
    error: str | None = None


@dataclass(slots=True)
class _PanelRun:
    panel_id: int
    outcomes: list[_Outcome] = field(default_factory=list)
    skipped: int = 0
    aborted: bool = False


class _PushLocks:
    """In-process lock per provision, shared by :func:`reconcile` and :func:`sync_subscription`.

    Created on demand and dropped once unused. Held across the revision re-check and the panel call, it keeps
    the pushes of one account in revision order.
    """

    def __init__(self) -> None:
        self._locks: dict[int, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: int) -> AsyncIterator[None]:
        lock, users = self._locks.get(key) or (asyncio.Lock(), 0)
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


_PUSH_LOCKS = _PushLocks()


async def _is_stale(session_factory: SessionFactory, job: _Job) -> bool:
    """The subscription changed since the job was read: its snapshot could undo a newer push.

    A pure read (no SQLite write lock): it runs once per job while other panels' pushes are in flight.
    """
    async with read_session(session_factory) as session:
        revision = await session.scalar(select(Subscription.revision).where(Subscription.id == job.subscription_id))
    return revision != job.revision


async def _execute(backend: PanelBackend, job: _Job) -> _Outcome:
    try:
        if job.desired == _ABSENT:
            await backend.delete_account(job.target, job.spec.email, job.spec.uuid)
            return _Outcome(job)
        if job.reset_traffic:
            # Before the upsert: the panel would otherwise re-disable the account for last period's usage.
            await backend.reset_traffic(job.target, job.spec.email)
        return _Outcome(job, account=await backend.upsert_account(job.target, job.spec))
    except PanelError as exc:
        log.warning("panel %s: %s of %s failed: %s", job.panel_id, job.desired, job.spec.email, exc)
        return _Outcome(job, error=_describe(exc))
    except Exception as exc:  # a backend bug must not stop the whole reconcile
        log.exception("panel %s: unexpected error syncing %s", job.panel_id, job.spec.email)
        return _Outcome(job, error=_describe(exc))


async def _run_panel(
    session_factory: SessionFactory, registry: PanelSource, panel_id: int, jobs: list[_Job]
) -> _PanelRun:
    """Push one panel's jobs sequentially; give up after a streak of failures (panel probably down).

    Jobs whose subscription changed since they were read are skipped (counted in ``skipped``): whoever bumped
    the revision pushes the newer state, and the row stays due for the next reconcile anyway. So are jobs whose
    revision could not be checked (database error). Never raises (except on cancellation): an unexpected error
    postpones the rest of this panel's jobs and keeps the outcomes collected so far, so they are still stored.
    """
    run = _PanelRun(panel_id)
    calls = [job for job in jobs if job.needs_call]
    run.outcomes.extend(_Outcome(job) for job in jobs if not job.needs_call)
    if not calls:
        return run
    try:
        backend = await registry.get(panel_id)
    except Exception as exc:
        log.warning("panel %s: backend unavailable: %s", panel_id, exc)
        run.outcomes.extend(_Outcome(job, error=f"panel unavailable: {_describe(exc)}") for job in calls)
        run.aborted = True
        return run
    streak = index = 0
    try:
        for index, job in enumerate(calls):
            async with _PUSH_LOCKS.hold(job.provision_id):
                try:
                    stale = await _is_stale(session_factory, job)
                except Exception as exc:
                    log.warning("provision %s: cannot check its revision, not pushing it: %s", job.provision_id, exc)
                    stale = True
                if stale:
                    log.debug("provision %s: revision %s is stale, not pushing it", job.provision_id, job.revision)
                    run.skipped += 1
                    continue
                outcome = await _execute(backend, job)
            run.outcomes.append(outcome)
            streak = streak + 1 if outcome.error else 0
            if streak >= PANEL_ABORT_AFTER_FAILURES:
                postponed = len(calls) - index - 1
                run.skipped += postponed
                run.aborted = True
                log.warning("panel %s: %d failures in a row, postponing %d jobs", panel_id, streak, postponed)
                break
    except Exception:
        postponed = len(calls) - index  # the job that failed and every one after it
        log.exception("panel %s: provisioning run failed, postponing %d jobs", panel_id, postponed)
        run.skipped += postponed
        run.aborted = True
    return run


async def _run_jobs(session_factory: SessionFactory, registry: PanelSource, jobs: list[_Job]) -> list[_PanelRun]:
    """Panels are processed concurrently, each panel's jobs sequentially."""
    by_panel: dict[int, list[_Job]] = defaultdict(list)
    for job in jobs:
        by_panel[job.panel_id].append(job)
    runs = (_run_panel(session_factory, registry, pid, panel_jobs) for pid, panel_jobs in by_panel.items())
    results = await asyncio.gather(*runs, return_exceptions=True)
    collected: list[_PanelRun] = []
    for (panel_id, panel_jobs), result in zip(by_panel.items(), results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result  # cancellation
            log.error("panel %s: provisioning run failed: %s", panel_id, _describe(result))
            result = _PanelRun(panel_id, skipped=len(panel_jobs), aborted=True)
        collected.append(result)
    return collected


# ---------------------------------------------------------------------------------------------
# Jobs: write phase
# ---------------------------------------------------------------------------------------------
def _outcome_values(outcome: _Outcome, now: datetime) -> dict[str, Any]:
    job = outcome.job
    if outcome.error is not None:
        attempts = job.attempts + 1
        return {
            "desired_state": job.desired,
            "last_error": outcome.error,
            "attempts": attempts,
            "next_attempt_at": now + backoff_delay(attempts),
        }
    values: dict[str, Any] = {
        "desired_state": job.desired,
        "actual_state": job.desired,
        "synced_revision": job.revision,
        "traffic_epoch": job.traffic_epoch,
        "remote_email": job.spec.email,
        "last_error": None,
        "attempts": 0,
        "next_attempt_at": None,
    }
    if outcome.account is not None:
        values["remote_id"] = outcome.account.remote_id
        values["remote_short_id"] = outcome.account.short_id
    elif job.desired == _ABSENT:
        values["remote_id"] = None
        values["remote_short_id"] = None
    return values


def _rowcount(result: Any) -> int:
    return int(cast(CursorResult[Any], result).rowcount or 0)


async def _store_outcomes(session_factory: SessionFactory, runs: Sequence[_PanelRun], now: datetime) -> None:
    """Write outcomes compare-and-set on ``synced_revision``.

    If another writer stored an outcome (or :func:`mark_dirty` marked the row) since the job was read, this
    outcome is older information and is dropped; the row is re-evaluated by the next reconcile.
    """
    outcomes = [outcome for run in runs for outcome in run.outcomes]
    if not outcomes:
        return
    async with session_factory.begin() as session:
        for outcome in outcomes:
            job = outcome.job
            result = await session.execute(
                update(Provision)
                .where(Provision.id == job.provision_id, Provision.synced_revision == job.base_revision)
                .values(**_outcome_values(outcome, now))
                .execution_options(synchronize_session=False)
            )
            if not _rowcount(result):
                log.debug(
                    "provision %s changed meanwhile; outcome of revision %s dropped", job.provision_id, job.revision
                )


def _tally(runs: Sequence[_PanelRun]) -> tuple[int, int, int, list[str]]:
    synced = failed = skipped = 0
    errors: list[str] = []
    for run in runs:
        skipped += run.skipped
        for outcome in run.outcomes:
            if outcome.error is None:
                synced += 1
            else:
                failed += 1
                errors.append(f"panel {run.panel_id} / {outcome.job.spec.email}: {outcome.error}")
    return synced, failed, skipped, errors


# ---------------------------------------------------------------------------------------------
# Configuration drift: settings that shape accounts but do not live on the subscription
# ---------------------------------------------------------------------------------------------
async def mark_dirty(session: AsyncSession, *, panel_id: int, inbound_id: int | None = None) -> int:
    """Make the provisions of a panel (or of one of its inbounds) due again: their accounts are re-pushed.

    For changes that do not bump ``Subscription.revision`` (inbound flow/transport, panel options, Remnawave
    squads). ``synced_revision`` is decremented, so it no longer matches the subscription and an in-flight push
    (stored compare-and-set on the old value) cannot clear the mark. Accounts deleted on purpose are left alone.
    Runs in the caller's transaction; returns the number of provisions marked.
    """
    stmt = update(Provision).where(
        Provision.panel_id == panel_id,
        or_(Provision.desired_state != _ABSENT, Provision.actual_state != _ABSENT),
    )
    if inbound_id is not None:
        stmt = stmt.where(Provision.inbound_id == inbound_id)
    stmt = stmt.values(synced_revision=Provision.synced_revision - 1).execution_options(synchronize_session=False)
    return _rowcount(await session.execute(stmt))


def _config_key(panel_id: int, inbound_id: int | None) -> str:
    return f"{CONFIG_KEY_PREFIX}{panel_id}:{inbound_id or 0}"


def _fingerprint(material: Any) -> str:
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _configured_squads(panel: Panel) -> list[str]:
    squads = panel_options(panel).get("squad_uuids")
    return sorted(str(s) for s in squads) if isinstance(squads, list) else []


async def _panel_squads(registry: PanelSource, panel_id: int) -> tuple[Any, list[str]] | None:
    """``(backend, squad uuids)`` of a Remnawave panel in "all squads" mode; None if they cannot be listed."""
    try:
        backend = await registry.get(panel_id)
        list_squads = getattr(backend, "list_squads", None)
        if list_squads is None:
            return None
        return backend, sorted(str(squad.uuid) for squad in await list_squads())
    except Exception as exc:
        log.warning("panel %s: cannot list squads: %s", panel_id, _describe(exc))
        return None


def _ss_fingerprint(inbound: Inbound) -> list[str]:
    """Shadowsocks inbounds: the method (it sets the size of every client key); nothing for other inbounds."""
    params = inbound_params(inbound)
    return [str(params.get(shadowsocks.PARAM_METHOD) or "")] if shadowsocks.is_shadowsocks(params) else []


async def _target_fingerprints(
    session_factory: SessionFactory, registry: PanelSource, settings: Settings
) -> tuple[dict[tuple[int, int | None], str], dict[int, Any]]:
    """Fingerprint of the account-shaping settings of every healthy active target, plus the Remnawave backends
    whose squads were listed.

    3x-ui: the inbound's account flow and the device limit mode.
    Remnawave: the squads (configured ones, else all of the panel's) and the
    traffic strategy. Squad lists are fetched outside any transaction.
    """
    healthy = and_(Panel.is_active.is_(True), Panel.consecutive_failures < _health_threshold(settings))
    async with session_factory.begin() as session:
        panels = list((await session.scalars(select(Panel).where(healthy).order_by(Panel.id))).all())
        inbound_stmt = (
            select(Inbound)
            .join(Panel, Inbound.panel_id == Panel.id)
            .where(healthy, Panel.kind == PanelKind.XUI, Inbound.is_active.is_(True))
            .order_by(Inbound.id)
        )
        inbounds = list((await session.scalars(inbound_stmt)).all())
    fingerprints: dict[tuple[int, int | None], str] = {
        (inbound.panel_id, inbound.id): _fingerprint(
            ["xui", account_flow(inbound), settings.xui_device_limit, *_ss_fingerprint(inbound)]
        )
        for inbound in inbounds
    }
    backends: dict[int, Any] = {}
    for panel in panels:
        if panel.kind == PanelKind.MARZBAN:
            fingerprints[(panel.id, None)] = _fingerprint(["marzban", panel_options(panel).get("inbound_tags")])
            continue
        if panel.kind in (PanelKind.HIDDIFY, PanelKind.PASARGUARD, PanelKind.MARZNESHIN):
            fingerprints[(panel.id, None)] = _fingerprint([str(panel.kind)])
            continue
        if panel.kind != PanelKind.REMNAWAVE:
            continue
        squads = _configured_squads(panel)
        if not squads:
            listed = await _panel_squads(registry, panel.id)
            if listed is None:
                continue  # unknown this time: keep the stored fingerprint
            backends[panel.id], squads = listed
        strategy = panel_options(panel).get("traffic_limit_strategy")
        fingerprints[(panel.id, None)] = _fingerprint(["remnawave", squads, strategy])
    return fingerprints, backends


async def _mark_config_changes(session_factory: SessionFactory, registry: PanelSource, settings: Settings) -> int:
    """Mark provisions dirty whose target's account-shaping settings changed since the last reconcile.

    A target's first fingerprint is only recorded. Returns the number of provisions marked.
    """
    fingerprints, backends = await _target_fingerprints(session_factory, registry, settings)
    changed: list[tuple[int, int | None]] = []
    marked = 0
    async with session_factory.begin() as session:
        for (panel_id, inbound_id), fingerprint in fingerprints.items():
            key = _config_key(panel_id, inbound_id)
            previous = await kv.get(session, key)
            if previous == fingerprint:
                continue
            await kv.set(session, key, fingerprint)
            if previous is not None:
                marked += await mark_dirty(session, panel_id=panel_id, inbound_id=inbound_id)
                changed.append((panel_id, inbound_id))
    for panel_id, inbound_id in changed:
        log.info("panel %s inbound %s: account settings changed, re-pushing accounts", panel_id, inbound_id)
        invalidate = getattr(backends.get(panel_id), "invalidate_squads", None)
        if inbound_id is None and invalidate is not None:
            invalidate()  # the backend caches "all squads": push the new list, not the cached one
    return marked


# ---------------------------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------------------------
async def sync_subscription(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    sub_id: int,
    *,
    now: datetime | None = None,
) -> SyncResult:
    """Push one subscription to all its targets now, ignoring retry backoff (e.g. right after a purchase).

    Panels marked down by health checks are skipped (counted in ``skipped``); the reconciler retries them.
    Accounts left on deactivated targets are drained.
    """
    now = now or utcnow()
    result = SyncResult(subscription_id=sub_id)
    jobs: list[_Job] = []
    async with session_factory.begin() as session:
        sub = await session.get(Subscription, sub_id)
        if sub is None:
            result.found = False
            return result
        await ensure_provisions(session, sub)
        stmt = _work_query().where(Provision.subscription_id == sub_id).order_by(Provision.panel_id, Provision.id)
        for row in (await session.execute(stmt)).all():
            if is_panel_down(row[3], settings):
                result.skipped += 1
                continue
            job = _make_job(row, settings, now)
            if job is not None:
                jobs.append(job)
    runs = await _run_jobs(session_factory, registry, jobs)
    await _store_outcomes(session_factory, runs, now)
    synced, failed, skipped, errors = _tally(runs)
    result.synced, result.failed, result.errors = synced, failed, errors
    result.skipped += skipped
    return result


async def reconcile(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    *,
    now: datetime | None = None,
    scan_orphans: bool = True,
    batch_size: int = RECONCILE_BATCH_SIZE,
) -> ReconcileReport:
    """Converge panels to the database; never raises because of a panel.

    1. mark provisions dirty whose target settings changed; 2. create provisions for new targets; 3. push due
    provisions (stale revision, state drift, retry due, new traffic period, retention elapsed, deactivated
    target, ban flag changed) of healthy panels and of deactivated ones, at most ``batch_size`` per panel;
    4. optionally scan healthy panels: report orphan accounts (deleting them when ``DELETE_ORPHAN_CLIENTS`` is on)
    and mark accounts that were deleted or changed on the panel dirty, so the next run re-pushes them; flag
    subscriptions whose usage summed over all their accounts reached the traffic quota (the next run disables them).
    """
    now = now or utcnow()
    report = ReconcileReport()
    await _mark_config_changes(session_factory, registry, settings)
    async with session_factory.begin() as session:
        report.provisions_created = await backfill_provisions(session, settings, now)
        report.panels_down = await _down_panel_ids(session, settings)
        jobs = await _load_due_jobs(session, settings, now, batch_size)
    runs = await _run_jobs(session_factory, registry, jobs)
    await _store_outcomes(session_factory, runs, now)
    report.synced, report.failed, report.skipped, report.errors = _tally(runs)
    if scan_orphans:
        aborted = {run.panel_id for run in runs if run.aborted}
        scan = await _scan_panels(
            session_factory, registry, settings, delete=settings.delete_orphan_clients, exclude=aborted, now=now
        )
        report.orphans, report.drifted, report.quota_exhausted = scan.orphans, scan.drifted, scan.quota_exhausted
        report.errors.extend(scan.errors)
    if report.failed or report.orphans or report.drifted:
        log.info(
            "reconcile: synced=%d failed=%d skipped=%d orphans=%d drifted=%d",
            report.synced,
            report.failed,
            report.skipped,
            len(report.orphans),
            report.drifted,
        )
    return report


# ---------------------------------------------------------------------------------------------
# Orphans
# ---------------------------------------------------------------------------------------------
async def _scan_targets(session: AsyncSession, panel: Panel) -> list[int | None]:
    if panel.kind in (
        PanelKind.REMNAWAVE,
        PanelKind.MARZBAN,
        PanelKind.HIDDIFY,
        PanelKind.PASARGUARD,
        PanelKind.MARZNESHIN,
    ):
        return [None]
    stmt = select(Inbound.remote_id).where(Inbound.panel_id == panel.id, Inbound.is_active.is_(True))
    return [*(await session.scalars(stmt.order_by(Inbound.id)))]


@dataclass(slots=True)
class _SubUsage:
    """One subscription's traffic as seen by a scan (only subscriptions with a quota are tracked)."""

    limit_gb: int
    epoch: int
    exhausted_epoch: int | None
    used: int = 0  # bytes used in the current period by the accounts read below
    seen: int = 0  # accounts whose counter was read (listed, and already reset for the current period)
    limit_bytes: int | None = None


_Usage = dict[int, _SubUsage]  # subscription id -> usage


@dataclass(slots=True)
class _KnownSet:
    """What the database says about one panel's accounts, read for :func:`_panel_orphans`."""

    accounts: set[tuple[int | None, str]] = field(default_factory=set)  # may legitimately exist on the panel
    has_any: bool = False  # the panel has at least one provision
    drifted: dict[int, list[int]] = field(default_factory=dict)  # synced_revision read -> provision ids


def _account_drifted(
    account: RemoteAccount | None,
    desired: ProvisionState,
    *,
    uuid: str,
    expires_at: datetime,
    device_limit: int,
    traffic_limit_bytes: int,
    now: datetime,
) -> bool:
    """The panel's copy of a settled account differs from what was pushed (deleted, re-keyed, toggled, edited)."""
    if account is None:
        return True
    if account.uuid.lower() != uuid.lower():
        return True
    if desired == ProvisionState.DISABLED:
        # Only the switch matters: Remnawave keeps a past expiry it cannot set, limits are moot.
        return account.enabled
    if not account.enabled:
        # 3x-ui disables a client itself once it used up its quota or expired: that is not drift.
        depleted = account.traffic_limit_bytes > 0 and account.used_bytes >= account.traffic_limit_bytes
        lapsed = account.expires_at is not None and account.expires_at <= now
        if not (depleted or lapsed):
            return True
    if account.expires_at is None and expires_at.year >= 2099:
        return account.device_limit != device_limit or account.traffic_limit_bytes != traffic_limit_bytes
    if account.expires_at is None or abs(account.expires_at - expires_at) > EXPIRY_DRIFT_TOLERANCE:
        return True
    return account.device_limit != device_limit or account.traffic_limit_bytes != traffic_limit_bytes


def _expected_identity(cache: dict[int, str], inbound_id: int | None, params_json: str | None, client_uuid: str) -> str:
    """What the panel's copy of the account must be identified by: the VLESS uuid, or the Shadowsocks client key."""
    if inbound_id is None or not params_json:
        return client_uuid
    if inbound_id not in cache:
        params = parse_params(params_json)
        method = str(params.get(shadowsocks.PARAM_METHOD) or "")
        cache[inbound_id] = method if shadowsocks.is_shadowsocks(params) else ""
    method = cache[inbound_id]
    if not shadowsocks.key_bytes(method):
        return client_uuid
    return shadowsocks.derive_client_key(client_uuid, inbound_id, method)


async def _known_accounts(
    session: AsyncSession,
    panel: Panel,
    settings: Settings,
    now: datetime,
    remote: dict[tuple[int | None, str], RemoteAccount] | None = None,
    usage: _Usage | None = None,
) -> _KnownSet:
    """Every account that may legitimately exist on ``panel``, and the settled ones that drifted from ``remote``.

    A provision is dead only if its account was deleted on purpose (actual ABSENT) *and* the subscription's live
    desired state is ABSENT. The stored desired state is not enough: a renewal does not touch it, and a
    concurrent sync may already have re-created the account without having stored its outcome yet.

    With ``remote`` (the panel's managed accounts by ``(target, email)``), provisions that are settled (pushed at
    the current revision, no error, no pending traffic reset, stored state equal to the live desired one, target
    active) are compared with the panel's copy, including imported accounts that retain the operator's own names.
    With ``usage``, the current period's traffic of every listed account is added to its subscription's tally.
    Rows are streamed in partitions so a large panel neither buffers every row nor blocks the event loop for the
    whole scan.
    """
    retention = settings.expired_client_retention_days
    is_xui = panel.kind == PanelKind.XUI
    known = _KnownSet()
    identities: dict[int, str] = {}  # inbound id -> Shadowsocks method ("" for other inbounds)
    stmt = (
        select(
            Provision.id,
            Provision.remote_email,
            Provision.actual_state,
            Provision.inbound_id,
            Provision.synced_revision,
            Provision.last_error.is_not(None),
            Provision.traffic_epoch,
            Inbound.remote_id,
            Inbound.is_active,
            Subscription.id,
            Subscription.user_id,
            Subscription.status,
            Subscription.expires_at,
            Subscription.revision,
            Subscription.traffic_epoch,
            Subscription.client_uuid,
            Subscription.device_limit,
            Subscription.traffic_limit_gb,
            Subscription.traffic_exhausted_epoch,
            Subscription.traffic_limit_bytes,
            Subscription.is_legacy,
            User.is_banned,
            Inbound.params,
        )
        .join(Subscription, Provision.subscription_id == Subscription.id)
        .join(User, Subscription.user_id == User.tg_id)
        .outerjoin(Inbound, Provision.inbound_id == Inbound.id)
        .where(Provision.panel_id == panel.id)
        .execution_options(yield_per=SCAN_CHUNK_SIZE)
    )
    result = await session.stream(stmt)
    async for partition in result.partitions():
        for row in partition:
            (
                provision_id,
                remote_email,
                actual,
                inbound_id,
                synced_revision,
                has_error,
                provision_epoch,
                remote_inbound_id,
                inbound_active,
                sub_id,
                user_id,
                status,
                expires_at,
                revision,
                sub_epoch,
                client_uuid,
                device_limit,
                traffic_gb,
                exhausted_epoch,
                exact_limit_bytes,
                is_legacy,
                banned,
                inbound_params_json,
            ) = row
            known.has_any = True
            exhausted = _exhausted(traffic_gb, sub_epoch, exhausted_epoch)
            desired = _desired(status, expires_at, now, retention, banned, exhausted)
            if actual == _ABSENT and desired == _ABSENT:
                continue  # deleted on purpose and still dead: if it exists remotely, it is an orphan
            target = remote_inbound_id if is_xui else None
            if remote_email:
                known.accounts.add((target, remote_email))
            if not is_xui or inbound_id is not None:
                known.accounts.add(
                    (target, account_email(panel, user_id, inbound_id, sub_id if is_legacy is False else None))
                )
            if remote is None or not remote_email:
                continue  # the listing only holds accounts named after the current scheme
            # Remnawave lists only usernames with its *current* configured prefix.
            # A prefix change deliberately leaves prior accounts untouched: their
            # persisted remote name is not returned by that filtered listing, so
            # treating it as a deletion would re-queue it on every reconcile.
            # Imported/non-standard retained names follow the same rule.
            if panel.kind == PanelKind.REMNAWAVE and remote_email != account_email(
                panel, user_id, inbound_id, sub_id if is_legacy is False else None
            ):
                continue
            account = remote.get((target, remote_email))
            if usage is not None and traffic_gb > 0 and actual != _ABSENT:
                tally = usage.setdefault(
                    sub_id, _SubUsage(traffic_gb, sub_epoch, exhausted_epoch, limit_bytes=exact_limit_bytes)
                )
                if account is not None and provision_epoch == sub_epoch:  # else: the counter is the old period's
                    tally.used += max(0, account.used_bytes)
                    tally.seen += 1
            settled = (
                actual != _ABSENT
                and actual == desired
                and synced_revision == revision
                and not has_error
                and provision_epoch == sub_epoch
                and (not is_xui or bool(inbound_active))
            )
            if settled and _account_drifted(
                account,
                desired,
                uuid=_expected_identity(identities, inbound_id, inbound_params_json, client_uuid),
                expires_at=expires_at,
                device_limit=panel_device_limit(panel.kind, device_limit, settings),
                traffic_limit_bytes=exact_limit_bytes
                if exact_limit_bytes is not None
                else gb_to_bytes(max(0, traffic_gb)),
                now=now,
            ):
                known.drifted.setdefault(synced_revision, []).append(provision_id)
        await asyncio.sleep(0)
    return known


async def _mark_drifted(session: AsyncSession, drifted: dict[int, list[int]]) -> int:
    """Make drifted provisions due (``synced_revision - 1``, like :func:`mark_dirty`), compare-and-set on the
    revision read: a row pushed meanwhile is left alone. Returns the number of rows marked."""
    marked = 0
    for revision, ids in drifted.items():
        for start in range(0, len(ids), INSERT_CHUNK_SIZE):
            stmt = (
                update(Provision)
                .where(Provision.id.in_(ids[start : start + INSERT_CHUNK_SIZE]), Provision.synced_revision == revision)
                .values(synced_revision=revision - 1)
                .execution_options(synchronize_session=False)
            )
            marked += _rowcount(await session.execute(stmt))
    return marked


async def _list_panel(backend: PanelBackend, targets: list[int | None]) -> list[tuple[int | None, RemoteAccount]]:
    """Every account of the given targets, with one panel request when the backend can list several at once
    (3x-ui returns the whole panel for any per-inbound listing)."""
    by_target = getattr(backend, "list_accounts_by_target", None)
    if by_target is not None and len(targets) > 1 and None not in targets:
        listed = await by_target([t for t in targets if t is not None])
        return [(target, acc) for target in targets for acc in listed.get(target, [])]
    accounts: list[tuple[int | None, RemoteAccount]] = []
    for target in targets:
        accounts.extend((target, acc) for acc in await backend.list_accounts(target))
    return accounts


@dataclass(slots=True)
class _Scan:
    orphans: list[OrphanAccount] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    drifted: int = 0
    quota_exhausted: int = 0
    usage: _Usage = field(default_factory=dict)


async def _panel_orphans(
    session_factory: SessionFactory,
    registry: PanelSource,
    panel: Panel,
    settings: Settings,
    now: datetime,
    *,
    delete: bool,
    usage: _Usage | None = None,
) -> tuple[list[OrphanAccount], int]:
    """Orphans of one panel, and the number of provisions marked dirty because their account drifted.

    The traffic of the panel's accounts is added to ``usage``.
    """
    async with read_session(session_factory) as session:
        targets = await _scan_targets(session, panel)
    backend = await registry.get(panel.id)
    remote: dict[tuple[int | None, str], RemoteAccount] = {}
    for target, acc in await _list_panel(backend, targets):
        remote.setdefault((target, acc.email), acc)
    # Read the known set only after listing: a provision exists, and is live, before its account is created.
    async with read_session(session_factory) as session:
        known = await _known_accounts(session, panel, settings, now, remote, usage)
        from app.services.imports import protected_accounts

        protected = await protected_accounts(session, panel.id)
    drifted = 0
    if known.drifted:
        async with session_factory.begin() as session:
            drifted = await _mark_drifted(session, known.drifted)
        log.warning("panel %s: %d accounts missing or changed on the panel, re-pushing them", panel.id, drifted)
    candidates = [
        key_acc
        for key_acc in remote.items()
        if key_acc[0] not in known.accounts
        and is_managed_email(panel, key_acc[1].email)
        and key_acc[1].email not in protected
    ]
    if delete and candidates and not known.has_any:
        log.warning(
            "panel %s: no provisions in the database, refusing to delete %d accounts", panel.id, len(candidates)
        )
        delete = False
    orphans = [await _handle_orphan(backend, panel.id, target, acc, delete=delete) for (target, _), acc in candidates]
    return orphans, drifted


async def _handle_orphan(
    backend: PanelBackend, panel_id: int, target: int | None, account: RemoteAccount, *, delete: bool
) -> OrphanAccount:
    orphan = OrphanAccount(panel_id=panel_id, target=target, email=account.email, uuid=account.uuid)
    if not delete:
        return orphan
    try:
        await backend.delete_account(target, account.email, account.uuid)
    except Exception as exc:
        log.warning("panel %s: deleting orphan %s failed: %s", panel_id, account.email, exc)
        return OrphanAccount(panel_id, target, account.email, account.uuid, error=_describe(exc))
    log.info("panel %s: deleted orphan account %s", panel_id, account.email)
    return OrphanAccount(panel_id, target, account.email, account.uuid, deleted=True)


async def find_orphans(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    *,
    delete: bool = False,
    exclude: set[int] | None = None,
    now: datetime | None = None,
) -> tuple[list[OrphanAccount], list[str]]:
    """Accounts on healthy active panels that match our naming scheme but have no live provision.

    With ``delete=True`` they are removed, except on a panel without any provision in the database (a fresh
    database connected to a populated panel must never wipe it). Accounts that drifted on the panel are marked
    dirty and the subscriptions' traffic quota is checked on the way (see :func:`reconcile`). Returns
    ``(orphans, errors)``.
    """
    scan = await _scan_panels(session_factory, registry, settings, delete=delete, exclude=exclude, now=now)
    return scan.orphans, scan.errors


async def _scan_panels(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    *,
    delete: bool,
    exclude: set[int] | None,
    now: datetime | None,
) -> _Scan:
    now = now or utcnow()
    async with read_session(session_factory) as session:
        panels = list((await session.scalars(select(Panel).where(Panel.is_active.is_(True)).order_by(Panel.id))).all())
    scan = _Scan()
    for panel in panels:
        if is_panel_down(panel, settings) or panel.id in (exclude or set()):
            continue
        panel_usage: _Usage = {}
        try:
            orphans, drifted = await _panel_orphans(
                session_factory, registry, panel, settings, now, delete=delete, usage=panel_usage
            )
        except Exception as exc:
            log.warning("panel %s: orphan scan failed: %s", panel.id, exc)
            scan.errors.append(f"panel {panel.id} orphan scan: {_describe(exc)}")
            continue
        scan.orphans.extend(orphans)
        scan.drifted += drifted
        _merge_usage(scan.usage, panel_usage)
    try:
        scan.quota_exhausted = await _apply_traffic_quota(session_factory, scan.usage)
    except Exception as exc:
        log.warning("traffic quota check failed: %s", exc)
        scan.errors.append(f"traffic quota check: {_describe(exc)}")
    return scan


def _merge_usage(total: _Usage, part: _Usage) -> None:
    for sub_id, tally in part.items():
        merged = total.setdefault(
            sub_id, _SubUsage(tally.limit_gb, tally.epoch, tally.exhausted_epoch, limit_bytes=tally.limit_bytes)
        )
        merged.used += tally.used
        merged.seen += tally.seen


async def _expected_accounts(session: AsyncSession, sub_ids: list[int]) -> dict[int, int]:
    """Number of accounts that should exist on active targets, per subscription."""
    counts: dict[int, int] = {}
    for start in range(0, len(sub_ids), INSERT_CHUNK_SIZE):
        stmt = (
            select(Provision.subscription_id, func.count())
            .join(Panel, Provision.panel_id == Panel.id)
            .outerjoin(Inbound, Provision.inbound_id == Inbound.id)
            .where(
                Provision.subscription_id.in_(sub_ids[start : start + INSERT_CHUNK_SIZE]),
                Provision.actual_state != _ABSENT,
                _target_active(),
            )
            .group_by(Provision.subscription_id)
        )
        counts.update({sub_id: count for sub_id, count in await session.execute(stmt)})
    return counts


async def _apply_traffic_quota(session_factory: SessionFactory, usage: _Usage) -> int:
    """Flag subscriptions whose summed usage reached their quota; lift the flag when it is below it again.

    The sum is a lower bound when some accounts could not be read (panel down, account missing, counter not reset
    yet), which is enough to block but not to unblock: the flag is lifted only when every account that should
    exist was read. Both writes bump the revision so every account is re-pushed, compare-and-set on the period and
    quota read (a renewal or plan change meanwhile wins). Returns the number of subscriptions newly flagged.
    """
    block: list[tuple[int, _SubUsage]] = []
    unblock: list[tuple[int, _SubUsage]] = []
    for sub_id, tally in usage.items():
        over = tally.used >= (tally.limit_bytes if tally.limit_bytes is not None else gb_to_bytes(tally.limit_gb))
        exhausted = _exhausted(tally.limit_gb, tally.epoch, tally.exhausted_epoch, tally.limit_bytes)
        if over and not exhausted:
            block.append((sub_id, tally))
        elif exhausted and not over:
            unblock.append((sub_id, tally))
    if not block and not unblock:
        return 0
    blocked = 0
    async with session_factory.begin() as session:
        expected = await _expected_accounts(session, [sub_id for sub_id, _ in unblock]) if unblock else {}
        for sub_id, tally in block:
            stmt = (
                update(Subscription)
                .where(
                    Subscription.id == sub_id,
                    Subscription.traffic_epoch == tally.epoch,
                    Subscription.traffic_limit_gb == tally.limit_gb,
                    Subscription.traffic_limit_bytes == tally.limit_bytes,
                    or_(
                        Subscription.traffic_exhausted_epoch.is_(None),
                        Subscription.traffic_exhausted_epoch != tally.epoch,
                    ),
                )
                .values(traffic_exhausted_epoch=tally.epoch, revision=Subscription.revision + 1)
                .execution_options(synchronize_session=False)
            )
            if _rowcount(await session.execute(stmt)):
                blocked += 1
                log.info(
                    "subscription %s: traffic quota (%d GB) used up, disabling its accounts", sub_id, tally.limit_gb
                )
        for sub_id, tally in unblock:
            if tally.seen < expected.get(sub_id, 0):
                continue  # some accounts were not read: the sum may be too low
            stmt = (
                update(Subscription)
                .where(
                    Subscription.id == sub_id,
                    Subscription.traffic_epoch == tally.epoch,
                    Subscription.traffic_limit_gb == tally.limit_gb,
                    Subscription.traffic_limit_bytes == tally.limit_bytes,
                    Subscription.traffic_exhausted_epoch == tally.epoch,
                )
                .values(traffic_exhausted_epoch=None, revision=Subscription.revision + 1)
                .execution_options(synchronize_session=False)
            )
            if _rowcount(await session.execute(stmt)):
                log.info("subscription %s: traffic is below the quota again, re-enabling its accounts", sub_id)
    return blocked
