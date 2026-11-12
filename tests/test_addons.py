"""Paid traffic and device add-ons."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import BalanceTransaction, Subscription, SubscriptionAddonPurchase
from app.services import addons, provisioning
from app.utils.time import utcnow
from tests.conftest import make_settings
from tests.test_subs_support import add_sub, add_user, days


async def test_traffic_pack_extends_current_exact_quota_and_is_ledgered(session_factory) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(traffic_pack_gb=20, traffic_pack_price_kop=500)
    async with session_factory.begin() as session:
        user = await add_user(session, 1, balance_kop=1_000)
        sub = await add_sub(session, user.tg_id, expires_at=utcnow() + days(10), traffic_gb=50)
        result = await addons.purchase(session, settings, user, sub.id, addons.AddonKind.TRAFFIC_PACK, ref="pack-1")
        assert result is not None and result.quantity == 20
        assert sub.traffic_limit_bytes == 70 * 1024**3
        assert sub.revision == 2

    async with session_factory() as session:
        row = await session.scalar(select(SubscriptionAddonPurchase))
        tx = await session.scalar(select(BalanceTransaction))
        sub = await session.get(Subscription, 1)
    assert row is not None and row.kind == "traffic_pack" and row.price_kop == 500
    assert tx is not None and tx.amount_kop == -500
    assert sub is not None and provisioning.traffic_limit_bytes(sub) == 70 * 1024**3


async def test_reset_clears_exhaustion_and_bumps_usage_epoch(session_factory) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(traffic_reset_price_kop=200)
    async with session_factory.begin() as session:
        user = await add_user(session, 1, balance_kop=500)
        sub = await add_sub(
            session,
            user.tg_id,
            expires_at=utcnow() + days(10),
            traffic_gb=5,
            traffic_epoch=3,
            traffic_exhausted_epoch=3,
        )
        assert provisioning.is_traffic_exhausted(sub)
        result = await addons.purchase(session, settings, user, sub.id, addons.AddonKind.TRAFFIC_RESET, ref="reset-1")
        assert result is not None
        assert (sub.traffic_epoch, sub.traffic_exhausted_epoch, sub.revision) == (4, None, 2)


def test_exact_paid_traffic_quota_is_detected_as_exhausted() -> None:
    """The scanner receives a byte quota after a traffic pack, not a rounded GB quota."""
    assert provisioning._exhausted(0, 4, 4, 70 * 1024**3)
    assert not provisioning._exhausted(0, 4, 3, 70 * 1024**3)


async def test_device_addon_and_unlimited_offer_rules(session_factory) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(device_addon_count=2, device_addon_price_kop=300)
    async with session_factory.begin() as session:
        user = await add_user(session, 1, balance_kop=500)
        sub = await add_sub(session, user.tg_id, expires_at=utcnow() + days(10), device_limit=3)
        result = await addons.purchase(session, settings, user, sub.id, addons.AddonKind.DEVICE_ADDON, ref="device-1")
        assert result is not None and sub.device_limit == 5 and sub.revision == 2
        unlimited = await add_sub(session, user.tg_id, expires_at=utcnow() + days(10), device_limit=0)
        assert addons.offers_for(settings, unlimited) == []
        with pytest.raises(addons.AddonError, match="unavailable"):
            await addons.purchase(session, settings, user, unlimited.id, addons.AddonKind.DEVICE_ADDON)
