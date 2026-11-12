"""Switching the product mode (``APP_MODE``): personal VPN <-> shop.

Personal -> shop: a readiness checklist (plans, payment provider, support and legal links, a permanent HTTPS
domain), then a decision for every member with free access: keep it complimentary, or convert it to a normal
subscription that ends on a grace date (after which they buy a plan like everyone else).

Shop -> personal: sales close (payment providers off), paid time is preserved, auto-renewal is turned off for
everyone (the affected users should be notified), wallets stay as they are. Native Telegram Stars subscriptions
are charged by Telegram until the bot cancels them, so they are canceled after the commit (``cancel_stars``).

Always two steps: ``preview_*`` computes a :class:`SwitchPlan` without writing anything, :func:`apply` executes it,
audits it and keeps a snapshot so :func:`rollback` can undo it. Runtime settings are changed through a callback
(the settings provider) after the database transaction is committed.

Closing sales is blocked (unless forced) while invoices created recently are still open: once the providers are
off nobody receives their webhooks, so a customer paying such an invoice would never be credited.
"""

from __future__ import annotations

import enum
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, PaymentStatus, Plan, Subscription, SubscriptionStatus
from app.db.session import read_session
from app.services import audit, kv
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory

log = logging.getLogger(__name__)

SNAPSHOT_KEY = "mode_switch:last"
TEMPORARY_DOMAINS = ("trycloudflare.com", "ngrok.io", "ngrok-free.app", "loca.lt", "localhost.run")
# Settings toggles of the payment providers (turned off when sales close).
PROVIDER_TOGGLES = {
    "stars": "stars_enabled",
    "cryptobot": "cryptobot_enabled",
    "platega": "platega_enabled",
    "yookassa": "yookassa_enabled",
    "yoomoney": "yoomoney_enabled",
    "freekassa": "freekassa_enabled",
}

SettingSetter = Callable[[str, Any], Awaitable[Any]]
# Cancels the user's native Stars subscription; False if Telegram could not be told (to be retried).
StarsCanceler = Callable[[int], Awaitable[bool]]


class Mode(enum.StrEnum):
    SHOP = "shop"
    PERSONAL = "personal"


class MemberPolicy(enum.StrEnum):
    KEEP = "keep"  # stays complimentary
    CONVERT = "convert"  # becomes a normal subscription ending on the grace date


class ModeSwitchError(Exception):
    pass


class NotReady(ModeSwitchError):
    """Required checklist items are not done."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__("not ready: " + ", ".join(missing))
        self.missing = missing


@dataclass(frozen=True, slots=True)
class CheckItem:
    key: str  # "plans", "payment_provider", "test_payment", "support", "terms", "privacy", "domain", ...
    ok: bool
    required: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Readiness:
    items: list[CheckItem]

    @property
    def ready(self) -> bool:
        return all(item.ok for item in self.items if item.required)

    @property
    def missing(self) -> list[str]:
        return [item.key for item in self.items if item.required and not item.ok]


@dataclass(frozen=True, slots=True)
class MemberChange:
    user_id: int
    subscription_id: int
    policy: MemberPolicy
    expires_before: datetime
    expires_after: datetime


@dataclass(slots=True)
class SwitchPlan:
    target: Mode
    readiness: Readiness
    settings_changes: dict[str, Any]
    members: list[MemberChange] = field(default_factory=list)  # personal -> shop
    auto_renew_off: list[int] = field(default_factory=list)  # subscription ids (shop -> personal)
    notify_user_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SwitchResult:
    target: Mode
    converted: int
    auto_renew_disabled: int
    notify_user_ids: list[int]
    settings_failed: list[str]  # settings the callback could not change (logged; the admin should retry)
    # Users whose native Stars subscription could not be canceled: Telegram keeps charging them until it is.
    stars_cancel_failed: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------------------------
def is_permanent_domain(url: str | None) -> bool:
    """HTTPS on a domain the owner controls (not a temporary tunnel or a bare IP-less localhost)."""
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or host in ("localhost", "127.0.0.1"):
        return False
    return not any(host == d or host.endswith("." + d) for d in TEMPORARY_DOMAINS)


def enabled_providers(settings: Settings) -> list[str]:
    return [name for name, toggle in PROVIDER_TOGGLES.items() if getattr(settings, toggle, False)]


async def readiness_for_shop(session: AsyncSession, settings: Settings) -> Readiness:
    """Checklist for opening sales."""
    plans = int(
        await session.scalar(
            select(func.count(Plan.id)).where(Plan.is_active.is_(True), Plan.is_hidden.is_(False), Plan.price_kop > 0)
        )
        or 0
    )
    providers = enabled_providers(settings)
    tested: list[str] = []
    for provider in providers:
        succeeded = await session.scalar(
            select(Payment.id).where(Payment.provider == provider, Payment.status == PaymentStatus.SUCCEEDED).limit(1)
        )
        if succeeded is not None:
            tested.append(provider)
    untested = [p for p in providers if p not in tested]
    items = [
        CheckItem("plans", plans > 0, True, str(plans)),
        CheckItem("payment_provider", bool(providers), True, ",".join(providers)),
        CheckItem("test_payment", bool(providers) and not untested, False, ",".join(untested)),
        CheckItem("support", bool(settings.support_url), True),
        CheckItem("terms", bool(settings.terms_url), True),
        CheckItem("privacy", bool(settings.privacy_url), True),
        CheckItem("domain", is_permanent_domain(settings.public_base_url), True, settings.public_base_url or ""),
        CheckItem("trial", settings.trial_days > 0, False, str(settings.trial_days)),
        CheckItem("referral", settings.referral_enabled, False),
        CheckItem("channel", bool(settings.channel_url or settings.required_channel_id), False),
    ]
    return Readiness(items)


# ---------------------------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------------------------
async def preview_to_shop(
    session: AsyncSession,
    settings: Settings,
    *,
    default_policy: MemberPolicy = MemberPolicy.KEEP,
    per_user: Mapping[int, MemberPolicy] | None = None,
    grace_until: datetime | None = None,
    now: datetime | None = None,
) -> SwitchPlan:
    """Plan for personal -> shop. Members converted to paid access keep it until ``grace_until`` at most."""
    now = now or utcnow()
    per_user = per_user or {}
    wants_convert = default_policy == MemberPolicy.CONVERT or MemberPolicy.CONVERT in per_user.values()
    if wants_convert and (grace_until is None or grace_until.tzinfo is None or grace_until <= now):
        raise ModeSwitchError("converting members needs an aware grace date in the future")
    # Paused members are included: converting changes only the expiry, so they stay paused, but once resumed
    # they must not keep free access in the shop.
    subs = (
        await session.scalars(
            select(Subscription).where(Subscription.is_complimentary.is_(True)).order_by(Subscription.user_id)
        )
    ).all()
    members = []
    notify = []
    for sub in subs:
        policy = per_user.get(sub.user_id, default_policy)
        after = sub.expires_at
        if policy == MemberPolicy.CONVERT:
            assert grace_until is not None
            after = min(sub.expires_at, grace_until.replace(microsecond=0))
            notify.append(sub.user_id)
        members.append(MemberChange(sub.user_id, sub.id, policy, sub.expires_at, after))
    return SwitchPlan(
        target=Mode.SHOP,
        readiness=await readiness_for_shop(session, settings),
        settings_changes={"app_mode": Mode.SHOP.value},
        members=members,
        notify_user_ids=notify,
    )


async def open_payments(session: AsyncSession, settings: Settings, now: datetime | None = None) -> int:
    """Invoices that may still be paid: created or pending, younger than ``PAYMENT_MAX_PENDING_HOURS``."""
    since = (now or utcnow()) - timedelta(hours=settings.payment_max_pending_hours)
    count = await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.status.in_((PaymentStatus.CREATED, PaymentStatus.PENDING)), Payment.created_at >= since
        )
    )
    return int(count or 0)


async def preview_to_personal(session: AsyncSession, settings: Settings, *, now: datetime | None = None) -> SwitchPlan:
    """Plan for shop -> personal: close sales and turn auto-renewal off (paid time stays).

    Not ready while invoices are still open (see :func:`open_payments`): wait until they are paid or expire.
    """
    pending = await open_payments(session, settings, now)
    rows = (
        await session.execute(
            select(Subscription.id, Subscription.user_id)
            .where(Subscription.auto_renew.is_(True))
            .order_by(Subscription.user_id)
        )
    ).all()
    changes: dict[str, Any] = {"app_mode": Mode.PERSONAL.value}
    for provider in enabled_providers(settings):
        changes[PROVIDER_TOGGLES[provider]] = False
    return SwitchPlan(
        target=Mode.PERSONAL,
        readiness=Readiness([CheckItem("pending_payments", pending == 0, True, str(pending))]),
        settings_changes=changes,
        auto_renew_off=[sub_id for sub_id, _ in rows],
        notify_user_ids=[user_id for _, user_id in rows],
    )


# ---------------------------------------------------------------------------------------------
# Apply / rollback
# ---------------------------------------------------------------------------------------------
def _sub_state(sub: Subscription) -> dict[str, Any]:
    return {
        "expires_at": sub.expires_at.isoformat(),
        "is_complimentary": sub.is_complimentary,
        "auto_renew": sub.auto_renew,
    }


def _same_state(sub: Subscription, state: Mapping[str, Any]) -> bool:
    return (
        sub.expires_at == datetime.fromisoformat(state["expires_at"])
        and sub.is_complimentary == bool(state["is_complimentary"])
        and sub.auto_renew == bool(state["auto_renew"])
    )


async def _locked(session: AsyncSession, ids: list[int]) -> dict[int, Subscription]:
    if not ids:
        return {}
    stmt = (
        select(Subscription)
        .where(Subscription.id.in_(ids))
        .order_by(Subscription.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return {sub.id: sub for sub in (await session.scalars(stmt)).all()}


async def _apply_settings(changes: Mapping[str, Any], set_setting: SettingSetter | None) -> list[str]:
    failed: list[str] = []
    if set_setting is None:
        return list(changes) if changes else []
    for key, value in changes.items():
        try:
            await set_setting(key, value)
        except Exception:
            log.exception("mode switch: could not change setting %s", key)
            failed.append(key)
    return failed


async def apply(
    session_factory: SessionFactory,
    settings: Settings,
    plan: SwitchPlan,
    actor_id: int,
    *,
    set_setting: SettingSetter | None,
    cancel_stars: StarsCanceler | None = None,
    force: bool = False,
    source: str = "bot",
    ip: str | None = None,
    now: datetime | None = None,
) -> SwitchResult:
    """Execute a plan from ``preview_*``. Raises :class:`NotReady` if required items are missing, unless ``force``.

    Member changes are applied to the current rows (a member whose access ended meanwhile is not extended).
    ``set_setting(key, value)`` is awaited for every settings change after the commit. Switching to personal,
    ``cancel_stars(user_id)`` is awaited for every user with a linked native Stars subscription; the users it
    could not cancel (all of them without ``cancel_stars``) are listed in ``SwitchResult.stars_cancel_failed``.
    """
    if not plan.readiness.ready and not force:
        raise NotReady(plan.readiness.missing)
    now = now or utcnow()
    snapshot: dict[str, Any] = {
        "target": plan.target.value,
        "at": now.isoformat(),
        "actor_id": actor_id,
        "settings": {key: _jsonable(getattr(settings, key, None)) for key in plan.settings_changes},
        "subscriptions": {},
    }
    converted = renew_off = 0
    stars_users: list[int] = []
    async with session_factory.begin() as session:
        if plan.target == Mode.SHOP:
            converts = {m.subscription_id: m for m in plan.members if m.policy == MemberPolicy.CONVERT}
            for sub_id, sub in (await _locked(session, list(converts))).items():
                if not sub.is_complimentary:
                    continue
                before = _sub_state(sub)
                sub.is_complimentary = False
                sub.auto_renew = False
                sub.expires_at = min(sub.expires_at, converts[sub_id].expires_after)
                sub.last_reminder_days = None
                sub.revision += 1
                snapshot["subscriptions"][str(sub_id)] = {"before": before, "after": _sub_state(sub)}
                converted += 1
        else:
            for sub_id, sub in (await _locked(session, plan.auto_renew_off)).items():
                if not sub.auto_renew:
                    continue
                before = _sub_state(sub)
                sub.auto_renew = False
                snapshot["subscriptions"][str(sub_id)] = {"before": before, "after": _sub_state(sub)}
                renew_off += 1
            stars_users = list(
                (
                    await session.scalars(
                        select(Subscription.user_id)
                        .where(Subscription.stars_charge_id.is_not(None))
                        .order_by(Subscription.user_id)
                    )
                ).all()
            )
        await kv.set(session, SNAPSHOT_KEY, json.dumps(snapshot, ensure_ascii=False))
        await audit.log(
            session,
            actor_id,
            "mode.switch",
            "system",
            plan.target.value,
            source,
            ip,
            settings=plan.settings_changes,
            converted=converted,
            auto_renew_disabled=renew_off,
            kept=sum(1 for m in plan.members if m.policy == MemberPolicy.KEEP),
        )
    failed = await _apply_settings(plan.settings_changes, set_setting)
    stars_failed = await _cancel_stars(stars_users, cancel_stars)
    log.info("mode switched to %s by %s (converted=%d, auto-renew off=%d)", plan.target, actor_id, converted, renew_off)
    return SwitchResult(plan.target, converted, renew_off, list(plan.notify_user_ids), failed, stars_failed)


async def _cancel_stars(user_ids: list[int], cancel_stars: StarsCanceler | None) -> list[int]:
    """Cancel the native Stars subscriptions of ``user_ids``; the users that could not be canceled."""
    if not user_ids:
        return []
    if cancel_stars is None:
        log.warning("mode switch: Stars subscriptions of users %s were not canceled", user_ids)
        return list(user_ids)
    failed: list[int] = []
    for user_id in user_ids:
        try:
            ok = await cancel_stars(user_id)
        except Exception:
            log.exception("mode switch: canceling the Stars subscription of user %s failed", user_id)
            ok = False
        if not ok:
            failed.append(user_id)
    if failed:
        log.warning("mode switch: Stars subscriptions of users %s are still active", failed)
    return failed


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return str(value)


async def last_switch(session_factory: SessionFactory) -> dict[str, Any] | None:
    """The snapshot of the last switch (``target``, ``at``, ``actor_id``, ...), if it can be rolled back."""
    async with read_session(session_factory) as session:
        raw = await kv.get(session, SNAPSHOT_KEY)
    return json.loads(raw) if raw else None


async def rollback(
    session_factory: SessionFactory,
    actor_id: int,
    *,
    set_setting: SettingSetter | None,
    source: str = "bot",
    ip: str | None = None,
) -> bool:
    """Undo the last :func:`apply`: restore member subscriptions and the changed settings. False if none.

    A subscription is restored only while it still holds exactly what the switch wrote. One changed since then
    (extended by an admin or an invite, a plan bought, auto-renewal toggled) is left as it is and listed as
    ``skipped`` in the audit entry, so access granted after the switch is never taken back.
    """
    skipped: list[int] = []
    restored = 0
    async with session_factory.begin() as session:
        raw = await kv.get(session, SNAPSHOT_KEY)
        if not raw:
            return False
        snapshot = json.loads(raw)
        saved: dict[str, dict[str, Any]] = snapshot.get("subscriptions") or {}
        subs = await _locked(session, [int(k) for k in saved])
        for sub_id, sub in subs.items():
            entry = saved[str(sub_id)]
            if "before" not in entry or not _same_state(sub, entry["after"]):
                skipped.append(sub_id)
                continue
            state = entry["before"]
            restored += 1
            sub.is_complimentary = bool(state["is_complimentary"])
            sub.auto_renew = bool(state["auto_renew"])
            expires = datetime.fromisoformat(state["expires_at"])
            if expires != sub.expires_at:
                sub.expires_at = expires
                sub.revision += 1
            if sub.status == SubscriptionStatus.EXPIRED and expires > utcnow():
                sub.status = SubscriptionStatus.ACTIVE
                sub.revision += 1
        await kv.delete(session, SNAPSHOT_KEY)
        await audit.log(
            session,
            actor_id,
            "mode.rollback",
            "system",
            snapshot.get("target"),
            source,
            ip,
            restored=restored,
            skipped=skipped,
            settings=snapshot.get("settings"),
        )
    if skipped:
        log.warning("mode switch rollback: subscriptions changed since the switch were left as they are: %s", skipped)
    await _apply_settings(snapshot.get("settings") or {}, set_setting)
    return True
