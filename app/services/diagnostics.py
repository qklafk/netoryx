"""Self-diagnosis of a subscription for "the VPN does not work" (read-only, database only, no network I/O).

It looks for the usual reasons on the service side, so the customer gets a concrete fix before writing to support:
the subscription ended or was suspended, its traffic is used up, the device limit is full, the panels have not got
the current settings yet, or the servers it uses are unreachable. When nothing is found, the customer gets a short
checklist and a support ticket that already carries the result (:meth:`Diagnosis.summary`).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from app.config import Settings
from app.db.models import Panel, Provision, ProvisionState, Subscription, SubscriptionDevice, SubscriptionStatus, User
from app.db.session import SessionFactory, read_session
from app.services.provisioning import is_traffic_exhausted
from app.utils.time import utcnow

# A panel that failed this many health checks in a row counts as unreachable.
DOWN_AFTER_FAILURES = 2


class Problem(enum.StrEnum):
    NO_SUBSCRIPTION = "no_subscription"
    BANNED = "banned"
    EXPIRED = "expired"
    DISABLED = "disabled"
    TRAFFIC = "traffic"  # the period's traffic is used up
    DEVICES = "devices"  # every device slot is taken (bot-side HWID limit)
    NOT_SYNCED = "not_synced"  # a panel does not have the current settings yet
    NO_SERVERS = "no_servers"  # the subscription is on no server at all
    SERVERS_DOWN = "servers_down"  # some of its servers do not answer
    ALL_SERVERS_DOWN = "all_servers_down"


@dataclass(frozen=True, slots=True)
class Diagnosis:
    problems: tuple[Problem, ...] = ()
    devices: int = 0
    device_limit: int = 0
    servers: int = 0
    servers_down: int = 0
    unsynced: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def has(self, problem: Problem) -> bool:
        return problem in self.problems

    def summary(self) -> str:
        """One technical line for support (language-neutral)."""
        found = ",".join(p.value for p in self.problems) or "none"
        limit = self.device_limit or "-"
        return (
            f"problems={found}; devices={self.devices}/{limit}; servers={self.servers}; "
            f"down={self.servers_down}; unsynced={self.unsynced}"
        )


async def diagnose(
    session_factory: SessionFactory,
    settings: Settings,
    user_id: int,
    subscription_id: int,
    *,
    now: datetime | None = None,
) -> Diagnosis:
    """Check one subscription of ``user_id``; a subscription of somebody else is reported as missing."""
    now = now or utcnow()
    async with read_session(session_factory) as session:
        sub = await session.scalar(
            select(Subscription).where(Subscription.id == subscription_id, Subscription.user_id == user_id)
        )
        if sub is None:
            return Diagnosis(problems=(Problem.NO_SUBSCRIPTION,))
        banned = bool(await session.scalar(select(User.is_banned).where(User.tg_id == user_id)))
        devices = int(
            await session.scalar(
                select(func.count()).select_from(SubscriptionDevice).where(SubscriptionDevice.subscription_id == sub.id)
            )
            or 0
        )
        rows = (
            await session.execute(
                select(Provision, Panel)
                .join(Panel, Panel.id == Provision.panel_id)
                .where(Provision.subscription_id == sub.id)
            )
        ).all()

    problems: list[Problem] = []
    if banned:
        problems.append(Problem.BANNED)
    if sub.status == SubscriptionStatus.DISABLED:
        problems.append(Problem.DISABLED)
    elif sub.status == SubscriptionStatus.EXPIRED or sub.expires_at <= now:
        problems.append(Problem.EXPIRED)
    if problems:  # nothing else matters until the subscription itself is usable
        return Diagnosis(problems=tuple(problems), devices=devices, device_limit=sub.device_limit)

    if is_traffic_exhausted(sub):
        problems.append(Problem.TRAFFIC)
    hwid_limit = sub.device_limit if settings.xui_device_limit == "hwid" else 0
    if hwid_limit > 0 and devices >= hwid_limit:
        problems.append(Problem.DEVICES)

    panels: dict[int, Panel] = {}
    unsynced = 0
    for provision, panel in rows:
        if not panel.is_active:
            continue
        panels[panel.id] = panel
        if provision.desired_state == ProvisionState.ENABLED and (
            provision.actual_state != ProvisionState.ENABLED
            or provision.synced_revision < sub.revision
            or provision.last_error
        ):
            unsynced += 1
    down = sum(1 for panel in panels.values() if panel.consecutive_failures >= DOWN_AFTER_FAILURES)
    if not panels:
        problems.append(Problem.NO_SERVERS)
    elif down == len(panels):
        problems.append(Problem.ALL_SERVERS_DOWN)
    elif down:
        problems.append(Problem.SERVERS_DOWN)
    if unsynced:
        problems.append(Problem.NOT_SYNCED)
    return Diagnosis(
        problems=tuple(problems),
        devices=devices,
        device_limit=hwid_limit,
        servers=len(panels),
        servers_down=down,
        unsynced=unsynced,
    )
