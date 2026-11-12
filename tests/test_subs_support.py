"""Shared factories and doubles for the subscriptions/provisioning tests (no tests in this module)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Inbound,
    Panel,
    PanelKind,
    Plan,
    Provision,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory
from app.services.panels.base import PanelBackend, PanelError
from tests.fakes import FakePanel

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
REALITY_PARAMS = {"type": "tcp", "security": "reality", "pbk": "PUBKEY", "sni": "www.example.com", "sid": "ab"}


async def add_user(session: AsyncSession, tg_id: int = 100, *, balance_kop: int = 0, **kw: Any) -> User:
    user = User(
        tg_id=tg_id,
        username=kw.pop("username", f"user{tg_id}"),
        referral_code=f"R{tg_id}",
        sub_token=f"token{tg_id}",
        balance_kop=balance_kop,
        **kw,
    )
    session.add(user)
    await session.flush()
    return user


async def add_plan(
    session: AsyncSession,
    *,
    days: int = 30,
    price_kop: int = 15_000,
    device_limit: int = 3,
    traffic_gb: int = 0,
    is_active: bool = True,
    sort_order: int = 0,
    title: str = "1 месяц",
) -> Plan:
    plan = Plan(
        title=title,
        duration_days=days,
        price_kop=price_kop,
        device_limit=device_limit,
        traffic_limit_gb=traffic_gb,
        is_active=is_active,
        sort_order=sort_order,
    )
    session.add(plan)
    await session.flush()
    return plan


async def add_xui_panel(
    session: AsyncSession,
    *,
    inbounds: int = 1,
    params: dict[str, Any] | None = None,
    flow: str = "xtls-rprx-vision",
    is_active: bool = True,
    name: str = "xui",
) -> tuple[Panel, list[Inbound]]:
    panel = Panel(name=name, kind=PanelKind.XUI, url="https://xui.example.com", is_active=is_active)
    session.add(panel)
    await session.flush()
    created = []
    for n in range(inbounds):
        inbound = Inbound(
            panel_id=panel.id,
            remote_id=n + 1,
            name=f"in{n + 1}",
            params=json.dumps(params if params is not None else REALITY_PARAMS),
            flow=flow,
        )
        session.add(inbound)
        created.append(inbound)
    await session.flush()
    return panel, created


async def add_rw_panel(session: AsyncSession, *, options: dict[str, Any] | None = None, name: str = "rw") -> Panel:
    panel = Panel(
        name=name,
        kind=PanelKind.REMNAWAVE,
        url="https://rw.example.com",
        options=json.dumps(options) if options is not None else None,
    )
    session.add(panel)
    await session.flush()
    return panel


async def add_sub(
    session: AsyncSession,
    user_id: int,
    *,
    expires_at: datetime,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    plan: Plan | None = None,
    device_limit: int = 3,
    traffic_gb: int = 0,
    **kw: Any,
) -> Subscription:
    sub = Subscription(
        user_id=user_id,
        plan_id=plan.id if plan else None,
        client_uuid=str(uuid4()),
        status=status,
        expires_at=expires_at,
        device_limit=device_limit,
        traffic_limit_gb=traffic_gb,
        revision=kw.pop("revision", 1),
        **kw,
    )
    session.add(sub)
    await session.flush()
    return sub


async def provisions_of(session_factory: SessionFactory, sub_id: int | None = None) -> list[Provision]:
    async with session_factory() as session:
        stmt = select(Provision).order_by(Provision.id)
        if sub_id is not None:
            stmt = stmt.where(Provision.subscription_id == sub_id)
        return list((await session.scalars(stmt)).all())


def days(n: float) -> timedelta:
    return timedelta(days=n)


class FakeRegistry:
    """``PanelRegistry.get`` double: panel id -> FakePanel; ids in ``broken`` raise PanelError."""

    def __init__(self, panels: dict[int, FakePanel] | None = None) -> None:
        self.panels: dict[int, FakePanel] = panels or {}
        self.broken: set[int] = set()

    async def get(self, panel_id: int) -> PanelBackend:
        if panel_id in self.broken:
            raise PanelError("cannot build backend")
        return self.panels[panel_id]


class TxTracker:
    """Wraps a SessionFactory and counts open ``begin()`` transactions."""

    def __init__(self, inner: SessionFactory) -> None:
        self.inner = inner
        self.open = 0
        self.violations: list[str] = []

    @property
    def kw(self) -> dict[str, Any]:  # read_session() looks up the engine here
        return self.inner.kw

    def __call__(self, **kw: Any) -> AsyncSession:
        return self.inner(**kw)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        async with self.inner.begin() as session:
            self.open += 1
            try:
                yield session
            finally:
                self.open -= 1

    def guard(self, panel: FakePanel) -> FakePanel:
        """Make every network-like call of ``panel`` record a violation if a transaction is open."""
        for name in ("upsert_account", "delete_account", "list_accounts", "get_account"):
            original: Callable[..., Any] = getattr(panel, name)
            setattr(panel, name, self._wrap(name, original))
        return panel

    def _wrap(self, name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if self.open:
                self.violations.append(name)
            return await original(*args, **kwargs)

        return wrapper
