"""Personal Telegram MTProto proxy for users.

Every user who may use the proxy gets their own account (own secret, so a leaked link can be revoked alone) on up to
``max_targets`` proxy *targets*: a telemt panel (:class:`~app.db.models.PanelKind.TELEMT`) or a 3x-ui ``mtproto``
inbound selected for the proxy (see :func:`app.services.panels.inventory.tg_proxy_enabled`). One
:class:`~app.db.models.TgProxyAccess` row per (user, panel).

Access policy (``TG_PROXY_ACCESS``)
-----------------------------------
* owners (``ADMIN_IDS``) and admins (``Admin`` rows with role owner/admin) always get a proxy, without limits;
* ``everyone``    - every user; users without an active subscription that includes the proxy get the free limits
  (``TG_PROXY_FREE_RATE_LIMIT_MBPS`` speed cap, ``TG_PROXY_FREE_AD_TAG`` sponsored channel);
* ``subscribers`` - users with an active subscription whose plan includes it (``Plan.includes_tg_proxy``; trials and
  plan-less grants included). After the subscription ends the account is disabled, and deleted once the VPN
  retention period (``EXPIRED_CLIENT_RETENTION_DAYS``) is over; the server also gets the expiry as a safety net;
* ``off``         - nobody else (existing accounts are disabled, links survive switching the mode back on).

In ``APP_MODE=personal`` only members get a proxy (besides owners and admins): users without a subscription and
members being removed (subscription disabled and already past the retention period) have their accounts deleted.

Banned users are always disabled. ``TG_PROXY_MAX_UNIQUE_IPS`` caps simultaneous source IPs per account.

Lifecycle
---------
* :func:`ensure_access` (no network) lazily creates rows on the best healthy targets when the user opens the proxy
  screen; :func:`sync_user` pushes them right away; :func:`open_proxy` does both and returns :func:`user_view`.
* :func:`reconcile` (periodic) converges every row: desired state and the full account spec are computed from the
  policy at run time and fingerprinted, so subscription changes, bans and settings edits are picked up without
  callers having to remember anything (:func:`mark_user_dirty` merely makes it immediate). Accounts on targets that
  were deactivated are deleted. Failures back off exponentially.
* :func:`rotate` gives the user a new secret (old links stop working after the next push).
* :func:`scan` (periodic) reads every target once: traffic counters, accounts deleted/changed on the server
  (re-pushed), orphans (deleted with ``DELETE_ORPHAN_CLIENTS``) and link sharing (unique IPs stuck at the limit
  for several scans -> auto-rotation with ``TG_PROXY_AUTO_ROTATE_ON_ABUSE`` + ``on_abuse`` callback).

The secret key is generated here and stored with the cached links in ``TgProxyAccess.links_cache`` (encrypted JSON),
so the database stays the source of truth: a secret changed on the server is reverted by the next push. No database
transaction is ever held across network I/O: rows are read, the transaction closed, the server called, and the
outcome stored compare-and-set on ``TgProxyAccess.revision``.

Panel options used here: telemt ``location_id``, ``web_host`` (WEB proxy hostname: adds ``tg://webproxy`` links),
``username_prefix`` (default ``"tg"``); 3x-ui ``tg_proxy_host`` / ``tg_proxy_port`` (address clients connect to;
default: the panel URL host and the inbound port).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    Admin,
    AdminRole,
    Inbound,
    Location,
    Panel,
    PanelKind,
    Plan,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    TgProxyAccess,
    User,
)
from app.db.session import SessionFactory, read_session
from app.services.panels.base import PanelBackend, PanelError
from app.services.panels.inventory import TG_PROXY_FLAG, set_tg_proxy_flag
from app.services.panels.mtproto import (
    MtprotoProxyBackend,
    ProxyAccount,
    ProxyLink,
    ProxyUserSpec,
    is_ad_tag,
    is_key,
    new_key,
    web_proxy_url,
)
from app.services.panels.xui import MTPROTO
from app.utils.time import utcnow

log = logging.getLogger(__name__)

DEFAULT_MAX_TARGETS: Final = 2  # primary + backup server
DEFAULT_TELEMT_PREFIX: Final = "tg"
XUI_EMAIL_RE: Final = re.compile(r"tg[0-9]+-[0-9]+-tg")  # f"tg{user_id}-{inbound.id}-tg"
BACKOFF_BASE: Final = timedelta(seconds=30)
BACKOFF_MAX: Final = timedelta(minutes=30)
RECONCILE_BATCH_SIZE: Final = 500
PANEL_ABORT_AFTER_FAILURES: Final = 3
MAX_ERROR_LENGTH: Final = 500
# Defaults of the TG_PROXY_ABUSE_* settings.
ABUSE_DEFAULT_UNIQUE_IPS: Final = 10  # threshold when accounts have no unique-IP limit
ABUSE_STRIKES: Final = 3  # consecutive scans at the threshold before an account counts as shared
ABUSE_ROTATE_COOLDOWN: Final = timedelta(hours=24)
_CACHE_VERSION: Final = 1
_STAFF_ROLES: Final = (AdminRole.OWNER, AdminRole.ADMIN)


class SecretCodec(Protocol):
    """The part of :class:`app.utils.crypto.SecretBox` used for ``links_cache``."""

    def encrypt(self, plaintext: str) -> str: ...

    def decrypt(self, value: str) -> str: ...


class PanelSource(Protocol):
    """What this module needs from :class:`app.services.panels.registry.PanelRegistry`."""

    async def get(self, panel_id: int) -> PanelBackend: ...


class RotationTooSoonError(Exception):
    """The user's link was rotated too recently."""

    def __init__(self, retry_at: datetime) -> None:
        self.retry_at = retry_at
        super().__init__(f"link rotated recently; retry after {retry_at.isoformat()}")


# ---------------------------------------------------------------------------------------------
# Policy (pure)
# ---------------------------------------------------------------------------------------------
class AccessReason(StrEnum):
    STAFF = "staff"  # owner/admin: always, unlimited
    SUBSCRIBER = "subscriber"  # active subscription that includes the proxy: unlimited
    FREE = "free"  # everyone mode, no qualifying subscription: free limits apply
    OFF = "off"  # the feature is off
    NO_SUBSCRIPTION = "no_subscription"  # subscribers mode without a qualifying subscription
    BANNED = "banned"


@dataclass(frozen=True, slots=True)
class UserFacts:
    """Everything the policy looks at."""

    user_id: int
    is_banned: bool
    is_staff: bool
    sub_status: SubscriptionStatus | None = None
    sub_expires_at: datetime | None = None
    plan_includes_proxy: bool = True  # Plan.includes_tg_proxy; True for plan-less subscriptions (trial, grants)


@dataclass(frozen=True, slots=True)
class Policy:
    state: ProvisionState  # desired state of the user's accounts
    reason: AccessReason
    limited: bool = False  # free limits (speed cap, ad tag) apply
    expires_at: datetime | None = None  # server-side expiry

    @property
    def allowed(self) -> bool:
        return self.state == ProvisionState.ENABLED


def evaluate(settings: Settings, facts: UserFacts, now: datetime) -> Policy:
    """The access policy for one user at ``now`` (see the module docstring)."""
    if facts.is_banned:
        return Policy(ProvisionState.DISABLED, AccessReason.BANNED)
    if facts.is_staff:
        return Policy(ProvisionState.ENABLED, AccessReason.STAFF)
    mode = settings.tg_proxy_access
    if mode == "off":
        return Policy(ProvisionState.DISABLED, AccessReason.OFF)
    retention = timedelta(days=max(0, settings.expired_client_retention_days))
    if settings.app_mode == "personal" and (
        facts.sub_status is None
        or (
            facts.sub_status == SubscriptionStatus.DISABLED
            and facts.sub_expires_at is not None
            and now - facts.sub_expires_at > retention
        )
    ):
        return Policy(ProvisionState.ABSENT, AccessReason.NO_SUBSCRIPTION)
    subscribed = (
        facts.sub_status == SubscriptionStatus.ACTIVE
        and facts.sub_expires_at is not None
        and facts.sub_expires_at > now
        and facts.plan_includes_proxy
    )
    if subscribed:
        expires_at = facts.sub_expires_at if mode == "subscribers" else None
        return Policy(ProvisionState.ENABLED, AccessReason.SUBSCRIBER, expires_at=expires_at)
    if mode == "everyone":
        return Policy(ProvisionState.ENABLED, AccessReason.FREE, limited=True)
    if facts.sub_expires_at is not None and now - facts.sub_expires_at > retention:
        return Policy(ProvisionState.ABSENT, AccessReason.NO_SUBSCRIPTION)
    return Policy(ProvisionState.DISABLED, AccessReason.NO_SUBSCRIPTION)


def free_ad_tag(settings: Settings) -> str | None:
    tag = (settings.tg_proxy_free_ad_tag or "").strip()
    if not tag:
        return None
    if not is_ad_tag(tag):
        log.warning("TG_PROXY_FREE_AD_TAG must be 32 hex chars (from @MTProxybot); ignoring it")
        return None
    return tag.lower()


def backoff_delay(attempts: int) -> timedelta:
    """Delay before retry number ``attempts`` (1-based): 30 s doubling up to 30 min."""
    exponent = min(max(attempts, 1) - 1, 16)
    return min(BACKOFF_BASE * (2**exponent), BACKOFF_MAX)


def _describe(exc: BaseException) -> str:
    return (str(exc) or type(exc).__name__)[:MAX_ERROR_LENGTH]


# ---------------------------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------------------------
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


@dataclass(frozen=True, slots=True)
class Target:
    """A place where proxy accounts live: a telemt panel or a 3x-ui mtproto inbound."""

    panel_id: int
    inbound_id: int | None
    kind: PanelKind
    remote_target: int | None  # 3x-ui inbound remote id; None for telemt
    title: str  # "<flag> <location>" or the panel name
    sort_order: int  # location order (lower first)
    active: bool  # selected for the proxy (otherwise its accounts are drained)
    serving: bool  # usable for new accounts right now (active and enabled on the panel)
    healthy: bool
    proxy_capable: bool = True  # False: a 3x-ui inbound that is no longer mtproto (its accounts are drained)
    prefix: str = DEFAULT_TELEMT_PREFIX
    tls_domain: str = ""  # 3x-ui inbound Fake-TLS domain
    host: str = ""  # 3x-ui: address clients connect to
    port: int | None = None
    web_host: str | None = None

    @property
    def key(self) -> tuple[int, int | None]:
        return self.panel_id, self.inbound_id

    def account_name(self, user_id: int) -> str:
        if self.kind == PanelKind.TELEMT:
            return f"{self.prefix}{user_id}"
        return f"tg{user_id}-{self.inbound_id}-tg"

    def is_managed_name(self, name: str) -> bool:
        if self.kind == PanelKind.TELEMT:
            rest = name[len(self.prefix) :]
            return name.startswith(self.prefix) and rest.isascii() and rest.isdigit()
        return XUI_EMAIL_RE.fullmatch(name) is not None and name.endswith(f"-{self.inbound_id}-tg")

    @property
    def link_fingerprint(self) -> list[Any]:
        return [self.host, self.port, self.web_host, self.tls_domain]

    def links_for(self, account: ProxyAccount) -> list[ProxyLink]:
        if self.kind == PanelKind.TELEMT:
            return list(account.links)
        if account.tls_secret and self.host and self.port:
            return [ProxyLink(self.host, self.port, account.tls_secret)]
        return []


def _location_title(location: Location | None, fallback: str) -> tuple[str, int]:
    if location is None:
        return fallback, 1_000_000
    title = f"{location.flag} {location.name}".strip() if location.flag else location.name
    return title, location.sort_order


location_title = _location_title


def build_target(
    panel: Panel, inbound: Inbound | None, locations: dict[int, Location], health_threshold: int
) -> Target | None:
    """The proxy target of a panel/inbound pair; None if it is not one."""
    options = _json_object(panel.options)
    healthy = panel.consecutive_failures < max(1, health_threshold)
    if panel.kind == PanelKind.TELEMT and inbound is None:
        location_id = options.get("location_id")
        location = locations.get(location_id) if isinstance(location_id, int) else None
        title, order = _location_title(location, panel.name)
        prefix = options.get("username_prefix")
        web_host = options.get("web_host")
        return Target(
            panel_id=panel.id,
            inbound_id=None,
            kind=PanelKind.TELEMT,
            remote_target=None,
            title=title,
            sort_order=order,
            active=panel.is_active,
            serving=panel.is_active,
            healthy=healthy,
            prefix=prefix if isinstance(prefix, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,16}", prefix) else "tg",
            host=str(options.get("public_host") or ""),
            port=_port(options.get("public_port")),
            web_host=web_host if isinstance(web_host, str) and web_host else None,
        )
    if panel.kind == PanelKind.XUI and inbound is not None:
        params = _json_object(inbound.params)
        capable = params.get("protocol") == MTPROTO
        selected = capable and params.get(TG_PROXY_FLAG) is True and params.get("local", True) is not False
        location = locations.get(inbound.location_id) if inbound.location_id is not None else None
        title, order = _location_title(location, panel.name)
        host = str(options.get("tg_proxy_host") or "") or (urlsplit(panel.url).hostname or "")
        return Target(
            panel_id=panel.id,
            inbound_id=inbound.id,
            kind=PanelKind.XUI,
            remote_target=inbound.remote_id,
            title=title,
            sort_order=order,
            active=panel.is_active and selected,
            serving=panel.is_active and selected and params.get("panel_enabled", True) is not False,
            healthy=healthy,
            proxy_capable=capable,
            tls_domain=str(params.get("fake_tls_domain") or ""),
            host=host,
            port=_port(options.get("tg_proxy_port")) or _port(params.get("port")),
        )
    return None


async def _locations(session: AsyncSession) -> dict[int, Location]:
    return {loc.id: loc for loc in await session.scalars(select(Location))}


async def list_targets(session: AsyncSession, settings: Settings) -> list[Target]:
    """Every proxy target (telemt panels and selected-or-not 3x-ui mtproto inbounds), in preference order."""
    locations = await _locations(session)
    threshold = settings.health_failures_before_alert
    targets: list[Target] = []
    for panel in await session.scalars(select(Panel).where(Panel.kind == PanelKind.TELEMT).order_by(Panel.id)):
        if (target := build_target(panel, None, locations, threshold)) is not None:
            targets.append(target)
    stmt = (
        select(Panel, Inbound)
        .join(Inbound, Inbound.panel_id == Panel.id)
        .where(Panel.kind == PanelKind.XUI)
        .order_by(Inbound.sort_order, Inbound.id)
    )
    for panel, inbound in (await session.execute(stmt)).all():
        if (target := build_target(panel, inbound, locations, threshold)) is not None and target.proxy_capable:
            targets.append(target)
    targets.sort(key=lambda t: (t.sort_order, t.panel_id, t.inbound_id or 0))
    return targets


async def set_xui_inbound(session: AsyncSession, inbound_id: int, enabled: bool) -> bool:
    """Select/deselect a 3x-ui mtproto inbound for the proxy (caller's transaction). False if not mtproto.

    Deselecting drains its accounts on the next reconcile; users get another target on their next visit.
    """
    inbound = await session.get(Inbound, inbound_id)
    return inbound is not None and set_tg_proxy_flag(inbound, enabled)


# ---------------------------------------------------------------------------------------------
# Cached secret + links (encrypted JSON in TgProxyAccess.links_cache)
# ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class _Cache:
    key: str | None = None
    spec: str = ""  # fingerprint of the last pushed spec
    links: list[ProxyLink] = field(default_factory=list)

    def dump(self, codec: SecretCodec) -> str:
        data = {"v": _CACHE_VERSION, "key": self.key, "spec": self.spec, "links": [x.to_json() for x in self.links]}
        return codec.encrypt(json.dumps(data, separators=(",", ":")))


def _load_cache(raw: str | None, codec: SecretCodec, access_id: int | None = None) -> _Cache:
    if not raw:
        return _Cache()
    try:
        data = json.loads(codec.decrypt(raw))
    except ValueError:  # DecryptionError is a ValueError: the key changed; a new secret is issued
        log.warning("tg proxy access %s: links cache cannot be decrypted; issuing a new secret", access_id)
        return _Cache()
    if not isinstance(data, dict):
        return _Cache()
    key = data.get("key")
    links = [link for item in data.get("links") or [] if (link := ProxyLink.from_json(item)) is not None]
    spec = data.get("spec")
    return _Cache(
        key=key.lower() if isinstance(key, str) and is_key(key) else None,
        spec=spec if isinstance(spec, str) else "",
        links=links,
    )


# ---------------------------------------------------------------------------------------------
# User facts
# ---------------------------------------------------------------------------------------------
async def _staff_ids(session: AsyncSession, settings: Settings) -> set[int]:
    rows = await session.scalars(select(Admin.tg_id).where(Admin.role.in_(_STAFF_ROLES)))
    return set(settings.admin_ids) | set(rows)


def _facts(user: User, sub: Subscription | None, plan: Plan | None, staff: set[int]) -> UserFacts:
    return UserFacts(
        user_id=user.tg_id,
        is_banned=user.is_banned,
        is_staff=user.tg_id in staff,
        sub_status=sub.status if sub is not None else None,
        sub_expires_at=sub.expires_at if sub is not None else None,
        plan_includes_proxy=plan.includes_tg_proxy if plan is not None else True,
    )


user_facts = _facts  # shared with :mod:`app.services.tg_socks`
staff_ids = _staff_ids


def _policy_subscription_id() -> Any:
    eligible = and_(
        Subscription.status == SubscriptionStatus.ACTIVE,
        Subscription.expires_at > utcnow(),
        or_(Plan.id.is_(None), Plan.includes_tg_proxy.is_(True)),
    )
    return (
        select(Subscription.id)
        .outerjoin(Plan, Subscription.plan_id == Plan.id)
        .where(Subscription.user_id == User.tg_id)
        .correlate(User)
        .order_by(case((eligible, 0), else_=1), Subscription.expires_at.desc(), Subscription.id)
        .limit(1)
        .scalar_subquery()
    )


policy_subscription_id = _policy_subscription_id


async def load_facts(session: AsyncSession, settings: Settings, user_id: int) -> UserFacts | None:
    stmt = (
        select(User, Subscription, Plan)
        .select_from(User)
        .outerjoin_from(User, Subscription, Subscription.id == _policy_subscription_id())
        .outerjoin(Plan, Subscription.plan_id == Plan.id)
        .where(User.tg_id == user_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    user, sub, plan = row
    return _facts(user, sub, plan, await _staff_ids(session, settings))


async def user_policy(
    session: AsyncSession, settings: Settings, user_id: int, *, now: datetime | None = None
) -> Policy:
    """The user's current proxy policy (unknown users: disabled as "off")."""
    facts = await load_facts(session, settings, user_id)
    if facts is None:
        return Policy(ProvisionState.DISABLED, AccessReason.OFF)
    return evaluate(settings, facts, now or utcnow())


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class EnsureResult:
    policy: Policy
    created: list[int] = field(default_factory=list)  # new TgProxyAccess ids
    no_targets: bool = False  # allowed, but no healthy proxy target is available


async def _target_loads(session: AsyncSession) -> dict[tuple[int, int | None], int]:
    stmt = select(TgProxyAccess.panel_id, TgProxyAccess.inbound_id, func.count()).group_by(
        TgProxyAccess.panel_id, TgProxyAccess.inbound_id
    )
    return {(panel_id, inbound_id): count for panel_id, inbound_id, count in (await session.execute(stmt)).all()}


async def _insert_access(session: AsyncSession, row: TgProxyAccess) -> bool:
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
    """Create the user's proxy accounts (database only) if the policy allows it.

    Keeps ``max_targets`` usable rows: targets are taken in location order, the least loaded first, never two on
    one panel. Raises LookupError for an unknown user.
    """
    now = now or utcnow()
    async with session_factory.begin() as session:
        facts = await load_facts(session, settings, user_id)
        if facts is None:
            raise LookupError(f"user {user_id} not found")
        policy = evaluate(settings, facts, now)
        result = EnsureResult(policy)
        if not policy.allowed:
            return result
        targets = await list_targets(session, settings)
        by_key = {t.key: t for t in targets}
        rows = list(await session.scalars(select(TgProxyAccess).where(TgProxyAccess.user_id == user_id)))
        usable = [r for r in rows if (t := by_key.get((r.panel_id, r.inbound_id))) and t.serving and t.healthy]
        used_panels = {r.panel_id for r in rows}
        need = max(1, max_targets) - len(usable)
        if need <= 0:
            return result
        loads = await _target_loads(session)
        candidates = [t for t in targets if t.serving and t.healthy and t.panel_id not in used_panels]
        candidates.sort(key=lambda t: (t.sort_order, loads.get(t.key, 0), t.panel_id, t.inbound_id or 0))
        for target in candidates:
            if need <= 0:
                break
            if target.panel_id in used_panels:
                continue
            row = TgProxyAccess(
                user_id=user_id,
                panel_id=target.panel_id,
                inbound_id=target.inbound_id,
                remote_name=target.account_name(user_id),
                links_cache=_Cache(key=new_key()).dump(codec),
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
        log.info("tg proxy: user %s got accounts %s", user_id, result.created)
    return result


async def mark_user_dirty(session: AsyncSession, user_id: int) -> int:
    """Make the next sync push the user's accounts now (ban/unban, subscription change). Caller's transaction."""
    result = await session.execute(
        update(TgProxyAccess)
        .where(TgProxyAccess.user_id == user_id)
        .values(revision=TgProxyAccess.revision + 1, next_attempt_at=None)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def rotate(
    session_factory: SessionFactory,
    codec: SecretCodec,
    user_id: int,
    *,
    min_interval: timedelta | None = None,
    now: datetime | None = None,
) -> int:
    """Issue new secrets for all the user's accounts (pushed by the next sync). Returns the rows rotated.

    With ``min_interval``, raises :class:`RotationTooSoonError` if any account was rotated more recently.
    """
    now = now or utcnow()
    async with session_factory.begin() as session:
        rows = list(
            await session.scalars(
                select(TgProxyAccess)
                .where(TgProxyAccess.user_id == user_id)
                .with_for_update()
                .order_by(TgProxyAccess.id)
            )
        )
        if min_interval is not None:
            last = max((r.rotated_at for r in rows if r.rotated_at is not None), default=None)
            if last is not None and now - last < min_interval:
                raise RotationTooSoonError(last + min_interval)
        for row in rows:
            _rotate_row(row, codec, now)
    if rows:
        log.info("tg proxy: user %s rotated %d secrets", user_id, len(rows))
    return len(rows)


def _rotate_row(row: TgProxyAccess, codec: SecretCodec, now: datetime) -> None:
    row.links_cache = _Cache(key=new_key()).dump(codec)  # old links are gone; new ones arrive with the push
    row.revision += 1
    row.rotated_at = now
    row.next_attempt_at = None


# ---------------------------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProxyEntry:
    """One proxy server of a user, ready to render."""

    access_id: int
    panel_id: int
    inbound_id: int | None
    title: str
    state: ProvisionState  # actual state on the server
    links: tuple[ProxyLink, ...]  # empty until the account is pushed
    web_url: str | None = None
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.state == ProvisionState.ENABLED and bool(self.links)


@dataclass(frozen=True, slots=True)
class UserProxyView:
    policy: Policy
    entries: tuple[ProxyEntry, ...]

    @property
    def ready(self) -> tuple[ProxyEntry, ...]:
        return tuple(e for e in self.entries if e.ready)


async def user_view(
    session_factory: SessionFactory,
    settings: Settings,
    codec: SecretCodec,
    user_id: int,
    *,
    now: datetime | None = None,
) -> UserProxyView:
    """The user's policy and proxy entries (links from the cache; no network I/O)."""
    async with read_session(session_factory) as session:
        policy = await user_policy(session, settings, user_id, now=now)
        stmt = (
            select(TgProxyAccess, Panel, Inbound)
            .join(Panel, TgProxyAccess.panel_id == Panel.id)
            .outerjoin(Inbound, TgProxyAccess.inbound_id == Inbound.id)
            .where(TgProxyAccess.user_id == user_id)
        )
        rows = (await session.execute(stmt)).all()
        locations = await _locations(session) if rows else {}
    entries: list[tuple[int, ProxyEntry]] = []
    for access, panel, inbound in rows:
        target = build_target(panel, inbound, locations, settings.health_failures_before_alert)
        if target is None or not target.active:
            continue
        cache = _load_cache(access.links_cache, codec, access.id)
        links = tuple(cache.links) if policy.allowed else ()  # a rotation empties the cached links until pushed
        web = web_proxy_url(target.web_host, cache.key) if target.web_host and cache.key and links else None
        entry = ProxyEntry(
            access_id=access.id,
            panel_id=access.panel_id,
            inbound_id=access.inbound_id,
            title=target.title,
            state=access.actual_state,
            links=links,
            web_url=web,
            error=access.last_error,
        )
        entries.append((target.sort_order, entry))
    entries.sort(key=lambda pair: (pair[0], pair[1].access_id))
    return UserProxyView(policy, tuple(entry for _, entry in entries))


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
    spec: ProxyUserSpec
    spec_hash: str
    key: str


@dataclass(slots=True)
class _Outcome:
    job: _Job
    account: ProxyAccount | None = None
    deleted: bool = False
    noop: bool = False  # nothing to do on the server (e.g. disabling an account that was never created)
    skipped: bool = False  # stale snapshot or panel gave up: retried later
    error: str | None = None


def build_spec(settings: Settings, target: Target, name: str, user_id: int, policy: Policy, key: str) -> ProxyUserSpec:
    """The full account for one row under ``policy``."""
    limited = policy.limited
    staff = policy.reason == AccessReason.STAFF
    rate = max(0, settings.tg_proxy_free_rate_limit_mbps) * 1_000_000 if limited else 0
    return ProxyUserSpec(
        name=name,
        enabled=policy.state == ProvisionState.ENABLED,
        key=key,
        expires_at=policy.expires_at,
        max_unique_ips=0 if staff else max(0, settings.tg_proxy_max_unique_ips),
        rate_limit_bps=rate,
        ad_tag=free_ad_tag(settings) if limited else None,
        tls_domain=target.tls_domain,
        tg_id=user_id,
        comment=f"tg:{user_id} proxy",
    )


def spec_fingerprint(spec: ProxyUserSpec, target: Target) -> str:
    material = [
        spec.name,
        spec.enabled,
        spec.key,
        spec.expires_at.isoformat() if spec.expires_at else None,
        spec.max_unique_ips,
        spec.traffic_limit_bytes,
        spec.rate_limit_bps,
        spec.ad_tag,
        spec.tls_domain,
        target.link_fingerprint,
    ]
    return hashlib.sha256(json.dumps(material, default=str).encode()).hexdigest()[:32]


_WorkRow = tuple[TgProxyAccess, User, Subscription | None, Plan | None, Panel, Inbound | None]


def _work_stmt() -> Any:
    return (
        select(TgProxyAccess, User, Subscription, Plan, Panel, Inbound)
        .select_from(TgProxyAccess)
        .join(User, TgProxyAccess.user_id == User.tg_id)
        .outerjoin_from(User, Subscription, Subscription.id == _policy_subscription_id())
        .outerjoin(Plan, Subscription.plan_id == Plan.id)
        .join(Panel, TgProxyAccess.panel_id == Panel.id)
        .outerjoin(Inbound, TgProxyAccess.inbound_id == Inbound.id)
        .order_by(TgProxyAccess.id)
    )


def _make_job(
    row: _WorkRow,
    settings: Settings,
    codec: SecretCodec,
    staff: set[int],
    locations: dict[int, Location],
    now: datetime,
) -> tuple[_Job, bool] | None:
    """(job, needs_work); None if the row's target cannot be resolved (e.g. 3x-ui row whose inbound is gone)."""
    access, user, sub, plan, panel, inbound = row
    target = build_target(panel, inbound, locations, settings.health_failures_before_alert)
    if target is None:
        return None
    policy = evaluate(settings, _facts(user, sub, plan, staff), now)
    desired = policy.state if target.active else ProvisionState.ABSENT
    cache = _load_cache(access.links_cache, codec, access.id)
    key = cache.key or new_key()
    spec = build_spec(settings, target, access.remote_name, user.tg_id, policy, key)
    spec_hash = spec_fingerprint(spec, target)
    job = _Job(
        access_id=access.id,
        user_id=user.tg_id,
        target=target,
        name=access.remote_name,
        revision=access.revision,
        desired=desired,
        actual=access.actual_state,
        spec=spec,
        spec_hash=spec_hash,
        key=key,
    )
    if desired == ProvisionState.ABSENT:
        needs = True  # delete (idempotent) and drop the row; a failed earlier push may have left an account
    elif desired == ProvisionState.DISABLED and access.actual_state == ProvisionState.ABSENT:
        needs = access.synced_revision != access.revision or access.desired_state != desired
    else:
        needs = (
            access.synced_revision != access.revision
            or access.actual_state != desired
            or access.last_error is not None
            or cache.spec != spec_hash
            or cache.key is None
        )
    return job, needs


async def _orphan_targets(session: AsyncSession) -> list[int]:
    """Rows whose 3x-ui inbound row is gone (cascade deletes them; this is a safety net)."""
    stmt = select(TgProxyAccess.id).where(
        TgProxyAccess.inbound_id.is_not(None), TgProxyAccess.inbound_id.not_in(select(Inbound.id))
    )
    return list(await session.scalars(stmt))


async def _load_jobs(
    session_factory: SessionFactory,
    settings: Settings,
    codec: SecretCodec,
    now: datetime,
    *,
    user_id: int | None = None,
    access_ids: Sequence[int] | None = None,
    respect_backoff: bool = True,
    limit: int | None = None,
) -> tuple[list[_Job], int]:
    """Due jobs and the number of due rows left out because their panel is down."""
    stmt = _work_stmt()
    if user_id is not None:
        stmt = stmt.where(TgProxyAccess.user_id == user_id)
    if access_ids is not None:
        stmt = stmt.where(TgProxyAccess.id.in_(list(access_ids)))
    if respect_backoff:
        stmt = stmt.where((TgProxyAccess.next_attempt_at.is_(None)) | (TgProxyAccess.next_attempt_at <= now))
    async with read_session(session_factory) as session:
        rows = (await session.execute(stmt)).all()
        staff = await _staff_ids(session, settings)
        locations = await _locations(session)
    jobs: list[_Job] = []
    down = 0
    for index, raw in enumerate(rows):
        if index and index % 1000 == 0:
            await asyncio.sleep(0)  # decrypting caches: let other tasks run
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
class _KeyedLocks:
    """In-process locks per row: two pushes of one account never overlap.

    A lock is dropped only when nobody holds or waits for it: a released lock still has a queued waiter until
    that waiter runs, so ``locked()`` alone cannot tell whether the entry is still in use.
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


_locks = _KeyedLocks()


async def _proxy_backend(registry: PanelSource, panel_id: int) -> MtprotoProxyBackend:
    backend = await registry.get(panel_id)
    if not isinstance(backend, MtprotoProxyBackend):
        raise PanelError(f"panel {panel_id} cannot host Telegram proxy accounts")
    return backend


async def _is_stale(session_factory: SessionFactory, job: _Job) -> bool:
    async with read_session(session_factory) as session:
        revision = await session.scalar(select(TgProxyAccess.revision).where(TgProxyAccess.id == job.access_id))
    return revision != job.revision


async def _execute(backend: MtprotoProxyBackend, job: _Job) -> _Outcome:
    target = job.target.remote_target
    if job.desired == ProvisionState.ABSENT:
        await backend.delete_proxy_user(target, job.name)
        return _Outcome(job, deleted=True)
    if job.desired == ProvisionState.DISABLED and job.actual == ProvisionState.ABSENT:
        return _Outcome(job, noop=True)
    return _Outcome(job, account=await backend.upsert_proxy_user(target, job.spec))


async def _run_panel(
    session_factory: SessionFactory, registry: PanelSource, panel_id: int, jobs: list[_Job]
) -> list[_Outcome]:
    try:
        backend = await _proxy_backend(registry, panel_id)
    except (PanelError, LookupError, ValueError) as exc:
        return [_Outcome(job, error=_describe(exc)) for job in jobs]
    outcomes: list[_Outcome] = []
    failures = 0
    for job in jobs:
        if failures >= PANEL_ABORT_AFTER_FAILURES:
            outcomes.append(_Outcome(job, skipped=True))
            continue
        async with _locks.hold(job.access_id):
            if await _is_stale(session_factory, job):
                outcomes.append(_Outcome(job, skipped=True))
                continue
            try:
                outcome = await _execute(backend, job)
                failures = 0
            except Exception as exc:  # one bad account must not stop the batch
                failures += 1
                log.warning("tg proxy access %s on panel %s: %s", job.access_id, panel_id, _describe(exc))
                outcome = _Outcome(job, error=_describe(exc))
        outcomes.append(outcome)
    return outcomes


async def _store(
    session_factory: SessionFactory, codec: SecretCodec, outcomes: Sequence[_Outcome], now: datetime
) -> None:
    async with session_factory.begin() as session:
        for outcome in outcomes:
            job = outcome.job
            current = TgProxyAccess.id == job.access_id
            same_revision = TgProxyAccess.revision == job.revision
            if outcome.skipped:
                continue
            if outcome.error is not None:
                row = await session.get(TgProxyAccess, job.access_id)
                if row is not None:
                    row.attempts = (row.attempts or 0) + 1
                    row.last_error = outcome.error
                    row.next_attempt_at = now + backoff_delay(row.attempts)
                    row.desired_state = job.desired
                continue
            if outcome.deleted:
                await session.execute(delete(TgProxyAccess).where(current, same_revision))
                continue
            values: dict[str, Any] = {
                "desired_state": job.desired,
                "synced_revision": job.revision,
                "attempts": 0,
                "last_error": None,
                "next_attempt_at": None,
                "updated_at": now,
            }
            if outcome.noop:
                values["actual_state"] = ProvisionState.ABSENT
                values["links_cache"] = _Cache(key=job.key).dump(codec)
            else:
                account = outcome.account
                assert account is not None
                key = account.key if account.key and account.key == job.key else job.key
                links = job.target.links_for(account)
                values["actual_state"] = ProvisionState.ENABLED if account.enabled else ProvisionState.DISABLED
                values["links_cache"] = _Cache(key=key, spec=job.spec_hash, links=links).dump(codec)
                values["used_bytes"] = max(0, account.used_bytes)
            await session.execute(update(TgProxyAccess).where(current, same_revision).values(**values))


@dataclass(slots=True)
class SyncReport:
    synced: int = 0
    failed: int = 0
    skipped: int = 0  # stale snapshot, panel down or gave up mid-batch: retried later
    deleted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.skipped == 0


async def _run_jobs(
    session_factory: SessionFactory, registry: PanelSource, codec: SecretCodec, jobs: list[_Job], now: datetime
) -> SyncReport:
    by_panel: dict[int, list[_Job]] = defaultdict(list)
    for job in jobs:
        by_panel[job.target.panel_id].append(job)
    results = await asyncio.gather(
        *(_run_panel(session_factory, registry, panel_id, panel_jobs) for panel_id, panel_jobs in by_panel.items())
    )
    outcomes = [outcome for panel_outcomes in results for outcome in panel_outcomes]
    await _store(session_factory, codec, outcomes, now)
    report = SyncReport()
    for outcome in outcomes:
        if outcome.skipped:
            report.skipped += 1
        elif outcome.error is not None:
            report.failed += 1
            report.errors.append(f"access {outcome.job.access_id}: {outcome.error}")
        elif outcome.deleted:
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


async def open_proxy(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    codec: SecretCodec,
    user_id: int,
    *,
    max_targets: int = DEFAULT_MAX_TARGETS,
    now: datetime | None = None,
) -> UserProxyView:
    """What the proxy screen calls: create accounts if needed, push them, and return the view."""
    await ensure_access(session_factory, settings, codec, user_id, max_targets=max_targets, now=now)
    await sync_user(session_factory, registry, settings, codec, user_id, now=now)
    return await user_view(session_factory, settings, codec, user_id, now=now)


async def remove_user(session_factory: SessionFactory, registry: PanelSource, user_id: int) -> SyncReport:
    """Delete the user's proxy accounts on the servers and their rows (e.g. a member removed from a private VPN).

    Rows whose server could not be reached are kept (``failed``) so the removal can be retried.
    """
    async with read_session(session_factory) as session:
        stmt = (
            select(TgProxyAccess, Panel, Inbound)
            .join(Panel, TgProxyAccess.panel_id == Panel.id)
            .outerjoin(Inbound, TgProxyAccess.inbound_id == Inbound.id)
            .where(TgProxyAccess.user_id == user_id)
        )
        rows = [
            (access.id, access.remote_name, panel, inbound) for access, panel, inbound in await session.execute(stmt)
        ]
        locations = await _locations(session) if rows else {}
    report = SyncReport()
    removed: list[int] = []
    for access_id, name, panel, inbound in rows:
        target = build_target(panel, inbound, locations, 1)
        try:
            backend = await _proxy_backend(registry, panel.id)
            await backend.delete_proxy_user(target.remote_target if target is not None else None, name)
        except Exception as exc:
            report.failed += 1
            report.errors.append(f"access {access_id}: {_describe(exc)}")
            continue
        removed.append(access_id)
    if removed:
        async with session_factory.begin() as session:
            await session.execute(delete(TgProxyAccess).where(TgProxyAccess.id.in_(removed)))
        report.deleted = len(removed)
        log.info("tg proxy: removed %d accounts of user %s", len(removed), user_id)
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
    """Converge due accounts (periodic job): policy changes, rotations, retries, drains of deactivated targets."""
    now = now or utcnow()
    async with session_factory.begin() as session:
        orphan_rows = await _orphan_targets(session)
        if orphan_rows:
            await session.execute(delete(TgProxyAccess).where(TgProxyAccess.id.in_(orphan_rows)))
    jobs, down = await _load_jobs(session_factory, settings, codec, now, limit=max(1, batch_size))
    report = await _run_jobs(session_factory, registry, codec, jobs, now) if jobs else SyncReport()
    report.skipped += down
    if report.synced or report.failed or report.deleted:
        log.info(
            "tg proxy reconcile: %d synced, %d deleted, %d failed, %d skipped",
            report.synced,
            report.deleted,
            report.failed,
            report.skipped,
        )
    return report


# ---------------------------------------------------------------------------------------------
# Scan: usage, drift, orphans, link sharing
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AbuseEvent:
    """An account whose unique source IPs stayed at the threshold for several scans (the link is shared)."""

    user_id: int
    access_id: int
    panel_id: int
    title: str
    unique_ips: int
    rotated: bool  # a new secret was issued (old links stop working after the push)


AbuseCallback = Callable[[AbuseEvent], Awaitable[None]]


class AbuseDetector:
    """Counts consecutive scans in which an account sat at the unique-IP threshold (process memory)."""

    def __init__(self, strikes: int = ABUSE_STRIKES) -> None:
        self.strikes = max(1, strikes)
        self._counts: dict[int, int] = {}

    def observe(self, access_id: int, flagged: bool) -> bool:
        """Record one scan; True when the account reaches the strike count (the counter then restarts)."""
        if not flagged:
            self._counts.pop(access_id, None)
            return False
        count = self._counts.get(access_id, 0) + 1
        if count >= self.strikes:
            self._counts.pop(access_id, None)
            return True
        self._counts[access_id] = count
        return False

    def forget(self, keep: Iterable[int]) -> None:
        keep_set = set(keep)
        for access_id in [a for a in self._counts if a not in keep_set]:
            del self._counts[access_id]


def abuse_threshold(settings: Settings) -> int:
    """Unique IPs at which an account looks shared: the per-account limit, else ``TG_PROXY_ABUSE_IP_THRESHOLD``."""
    if settings.tg_proxy_max_unique_ips > 0:
        return settings.tg_proxy_max_unique_ips
    return max(1, settings.tg_proxy_abuse_ip_threshold or ABUSE_DEFAULT_UNIQUE_IPS)


def abuse_rotate_cooldown(settings: Settings) -> timedelta:
    """Minimum time between automatic rotations of one account (``TG_PROXY_ABUSE_ROTATE_COOLDOWN_HOURS``)."""
    return timedelta(hours=max(0, settings.tg_proxy_abuse_rotate_cooldown_hours))


@dataclass(slots=True)
class ScanReport:
    accounts: int = 0
    drifted: int = 0  # missing/changed on the server: re-pushed by the next reconcile
    orphans: list[str] = field(default_factory=list)  # "<panel>:<name>" accounts without a row
    orphans_deleted: int = 0
    abuse: list[AbuseEvent] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _RowInfo:
    access_id: int
    user_id: int
    name: str
    actual: ProvisionState
    synced: bool
    rotated_at: datetime | None
    staff: bool


async def _target_rows(session: AsyncSession, target: Target, staff: set[int]) -> list[_RowInfo]:
    stmt = select(TgProxyAccess).where(TgProxyAccess.panel_id == target.panel_id)
    if target.inbound_id is not None:
        stmt = stmt.where(TgProxyAccess.inbound_id == target.inbound_id)
    return [
        _RowInfo(
            r.id,
            r.user_id,
            r.remote_name,
            r.actual_state,
            r.synced_revision == r.revision,
            r.rotated_at,
            r.user_id in staff,
        )
        for r in await session.scalars(stmt)
    ]


def _drifted(row: _RowInfo, account: ProxyAccount | None) -> bool:
    if not row.synced or row.actual == ProvisionState.ABSENT:
        return False  # a push is pending anyway / nothing expected on the server
    if account is None:
        return True
    return account.enabled != (row.actual == ProvisionState.ENABLED)


async def _scan_target(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    codec: SecretCodec,
    target: Target,
    staff: set[int],
    detector: AbuseDetector | None,
    now: datetime,
    report: ScanReport,
) -> None:
    backend = await _proxy_backend(registry, target.panel_id)
    accounts = await backend.list_proxy_users(target.remote_target)
    remote = {a.name: a for a in accounts if target.is_managed_name(a.name)}
    report.accounts += len(remote)
    async with read_session(session_factory) as session:
        rows = await _target_rows(session, target, staff)  # read after listing: rows precede their accounts
    known = {row.name for row in rows}
    threshold = abuse_threshold(settings)
    drifted: list[int] = []
    usage: dict[int, int] = {}
    shared: list[_RowInfo] = []
    for row in rows:
        account = remote.get(row.name)
        if account is not None:
            usage[row.access_id] = max(0, account.used_bytes)
        if _drifted(row, account):
            drifted.append(row.access_id)
        if detector is None or row.staff or target.kind != PanelKind.TELEMT:
            continue
        flagged = account is not None and account.enabled and account.recent_unique_ips >= threshold
        if detector.observe(row.access_id, flagged):
            shared.append(row)
    rotate_ids: set[int] = set()
    async with session_factory.begin() as session:
        for access_id, used in usage.items():
            await session.execute(update(TgProxyAccess).where(TgProxyAccess.id == access_id).values(used_bytes=used))
        if drifted:
            await session.execute(
                update(TgProxyAccess)
                .where(TgProxyAccess.id.in_(drifted))
                .values(synced_revision=0, next_attempt_at=None)
            )
        for row in shared:
            can_rotate = settings.tg_proxy_auto_rotate_on_abuse and (
                row.rotated_at is None or now - row.rotated_at >= abuse_rotate_cooldown(settings)
            )
            if can_rotate:
                locked = await session.get(TgProxyAccess, row.access_id, with_for_update=True)
                if locked is not None:
                    _rotate_row(locked, codec, now)
                    rotate_ids.add(row.access_id)
    report.drifted += len(drifted)
    for row in shared:
        account = remote.get(row.name)
        ips = account.recent_unique_ips if account is not None else 0
        event = AbuseEvent(row.user_id, row.access_id, target.panel_id, target.title, ips, row.access_id in rotate_ids)
        report.abuse.append(event)
        log.warning(
            "tg proxy access %s (user %s): %d unique IPs, link looks shared%s",
            row.access_id,
            row.user_id,
            ips,
            "; secret rotated" if event.rotated else "",
        )
    orphans = [name for name in remote if name not in known]
    report.orphans += [f"{target.panel_id}:{name}" for name in orphans]
    if orphans and settings.delete_orphan_clients:
        if not rows:
            log.warning(
                "tg proxy panel %s: no accounts in the database, not deleting %d", target.panel_id, len(orphans)
            )
            return
        for name in orphans:
            try:
                await backend.delete_proxy_user(target.remote_target, name)
                report.orphans_deleted += 1
            except Exception as exc:
                report.errors.append(f"panel {target.panel_id} orphan {name}: {_describe(exc)}")


async def scan(
    session_factory: SessionFactory,
    registry: PanelSource,
    settings: Settings,
    codec: SecretCodec,
    *,
    detector: AbuseDetector | None = None,
    on_abuse: AbuseCallback | None = None,
    now: datetime | None = None,
) -> ScanReport:
    """Read every active healthy target once (periodic job). Pass a long-lived ``detector`` to detect sharing."""
    now = now or utcnow()
    async with read_session(session_factory) as session:
        targets = [t for t in await list_targets(session, settings) if t.active and t.healthy]
        staff = await _staff_ids(session, settings)
    report = ScanReport()
    for target in targets:
        try:
            await _scan_target(session_factory, registry, settings, codec, target, staff, detector, now, report)
        except Exception as exc:
            log.warning("tg proxy scan of panel %s failed: %s", target.panel_id, _describe(exc))
            report.errors.append(f"panel {target.panel_id}: {_describe(exc)}")
    if detector is not None:
        async with read_session(session_factory) as session:
            detector.forget(await session.scalars(select(TgProxyAccess.id)))
    if on_abuse is not None:
        for event in report.abuse:
            try:
                await on_abuse(event)
            except Exception:  # a failing notification must not break the job
                log.exception("tg proxy abuse callback failed for access %s", event.access_id)
    return report
