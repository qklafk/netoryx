"""Independent subscriptions of one user: targeting, naming, tokens and per-subscription renewal."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Panel, Provision, Subscription
from app.db.session import SessionFactory
from app.services import subscriptions as subs
from app.services.provisioning import account_email
from tests.conftest import make_settings
from tests.test_subs_support import NOW, add_plan, add_rw_panel, add_user, add_xui_panel, days


async def test_second_subscription_is_isolated_from_the_first(session_factory: SessionFactory) -> None:
    settings = make_settings()
    async with session_factory.begin() as s:
        user = await add_user(s, 100, balance_kop=1_000_000)
        plan = await add_plan(s)
        await add_xui_panel(s, inbounds=2)
        await add_rw_panel(s)
        first = await subs.purchase_plan(s, settings, user, plan, payment_id="p1", now=NOW)
        second = await subs.purchase_plan(
            s, settings, user, plan, payment_id="p2", now=NOW + days(1), new_subscription=True, label="Two"
        )
    assert first.subscription_id != second.subscription_id
    async with session_factory() as s:
        a = await s.get(Subscription, first.subscription_id)
        b = await s.get(Subscription, second.subscription_id)
        assert a is not None and b is not None
        assert a.client_uuid != b.client_uuid and a.sub_token != b.sub_token
        rows = (await s.execute(select(Provision.subscription_id, Provision.panel_id, Provision.inbound_id))).all()
        assert len(rows) == 6 and len(set(rows)) == 6
        panels = {p.id: p for p in await s.scalars(select(Panel))}
        names = [
            account_email(panels[panel_id], 100, inbound_id, sub_id if sub_id == b.id else None)
            for sub_id, panel_id, inbound_id in rows
        ]
        assert len(set(names)) == 6
        user_a = await subs.resolve_token(s, a.sub_token or "")
        user_b = await subs.resolve_token(s, b.sub_token or "")
        assert user_a is not None and user_a[1] is not None and user_a[1].id == a.id
        assert user_b is not None and user_b[1] is not None and user_b[1].id == b.id

    # Renewing the second one leaves the first untouched.
    async with session_factory.begin() as s:
        user = await s.get(type(user), 100)
        assert user is not None
        plan2 = await add_plan(s, days=90, device_limit=5, price_kop=1000)
        before = (await s.get(Subscription, first.subscription_id)).expires_at  # type: ignore[union-attr]
        await subs.purchase_plan(
            s, settings, user, plan2, payment_id="p3", now=NOW + days(2), subscription_id=second.subscription_id
        )
        after = await s.get(Subscription, first.subscription_id, populate_existing=True)
        assert after is not None and after.expires_at == before and after.device_limit == 3
        other = await s.get(Subscription, second.subscription_id, populate_existing=True)
        assert other is not None and other.device_limit == 5
