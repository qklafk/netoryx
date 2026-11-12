"""Advertising campaigns: definitions, attribution and the funnel report."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Event, Payment, PaymentStatus, Purchase, PurchaseKind
from app.db.session import SessionFactory
from app.services import campaigns
from app.services.campaigns import CampaignExists
from tests.test_subs_support import NOW, add_user

ADMIN = 1000


def _purchase(user_id: int, kind: PurchaseKind, price_kop: int = 0) -> Purchase:
    return Purchase(user_id=user_id, kind=kind, price_kop=price_kop, days=30, expires_after=NOW)


def _payment(pid: str, user_id: int, amount: int, *, donation: bool = False, credited: bool = True) -> Payment:
    return Payment(
        id=pid,
        provider="stars",
        user_id=user_id,
        amount_kop=amount,
        status=PaymentStatus.SUCCEEDED if credited else PaymentStatus.PENDING,
        credited_at=NOW if credited else None,
        is_donation=donation,
    )


async def test_create_list_and_toggle(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        first = await campaigns.create(s, "c_summer", "Summer ads", ADMIN)
        await campaigns.create(s, "TG-channel", "", ADMIN)
        with pytest.raises(CampaignExists):
            await campaigns.create(s, "SUMMER", "dup", ADMIN)
        for bad in ("x", "has space", "a" * 33):
            with pytest.raises(ValueError):
                await campaigns.create(s, bad, "bad", ADMIN)
    assert first.code == "SUMMER" and campaigns.deep_link_payload("summer") == "c_SUMMER"
    async with session_factory.begin() as s:
        listed = await campaigns.list_campaigns(s)
        assert {c.code for c in listed} == {"SUMMER", "TG-CHANNEL"}
        assert (await campaigns.get(s, "tg-channel")).name == "TG-CHANNEL"  # type: ignore[union-attr]
        paused = await campaigns.set_active(s, "summer", False, ADMIN)
        assert paused is not None and not paused.is_active
        assert await campaigns.set_active(s, "missing", False, ADMIN) is None
        assert await campaigns.get(s, "") is None


async def test_attribution_rules(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        for uid in (1, 2, 3):
            await add_user(s, uid)
        await campaigns.create(s, "ads", "Ads", ADMIN)
        await campaigns.create(s, "old", "Old", ADMIN)
        await campaigns.set_active(s, "old", False, ADMIN)
        assert await campaigns.attribute(s, 1, "c_ads", is_new=True)
        assert not await campaigns.attribute(s, 1, "ads", is_new=True)  # once per user
        assert not await campaigns.attribute(s, 2, "ads", is_new=False)  # only new users
        assert not await campaigns.attribute(s, 2, "old", is_new=True)  # paused
        assert not await campaigns.attribute(s, 3, "nope", is_new=True)
        assert not await campaigns.attribute(s, 3, None, is_new=True)
        assert await campaigns.campaign_of(s, 1) == "ADS"
        assert await campaigns.campaign_of(s, 2) is None
        data = await s.scalar(select(Event.data).where(Event.type == campaigns.JOIN_EVENT))
    assert data == campaigns._event_data("ADS")  # stored exactly the way the report matches it


async def test_funnel_report(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        for uid in range(1, 8):
            await add_user(s, uid)
        await campaigns.create(s, "ads", "Ads", ADMIN)
        await campaigns.create(s, "blog", "Blog", ADMIN)
        await campaigns.create(s, "empty", "Empty", ADMIN)
        for uid in (1, 2, 3, 4):
            await campaigns.attribute(s, uid, "ads", is_new=True)
        await campaigns.attribute(s, 5, "blog", is_new=True)
        s.add_all(
            [
                _purchase(1, PurchaseKind.TRIAL),
                _purchase(2, PurchaseKind.TRIAL),
                _purchase(2, PurchaseKind.PURCHASE, 15_000),
                _purchase(2, PurchaseKind.AUTO_RENEW, 15_000),
                _purchase(3, PurchaseKind.PURCHASE, 40_000),
                _purchase(4, PurchaseKind.PROMO),
                _purchase(5, PurchaseKind.PURCHASE, 0),  # free: not a paying user
                _purchase(6, PurchaseKind.PURCHASE, 15_000),  # organic
                _payment("a", 2, 30_000),
                _payment("b", 3, 40_000),
                _payment("c", 3, 5_000, donation=True),
                _payment("d", 4, 9_000, credited=False),
                _payment("e", 6, 15_000),
            ]
        )
    async with session_factory() as s:
        report = {item.code: item for item in await campaigns.stats(s)}
        only_ads = await campaigns.stats(s, code="c_ads")
    ads = report["ADS"]
    assert (ads.registrations, ads.trials, ads.paying_users, ads.purchases, ads.revenue_kop) == (4, 2, 2, 3, 70_000)
    assert ads.conversion == 0.5
    blog = report["BLOG"]
    assert (blog.registrations, blog.paying_users, blog.revenue_kop) == (1, 0, 0)
    assert report["EMPTY"].registrations == 0 and report["EMPTY"].conversion == 0.0
    assert [r.code for r in only_ads] == ["ADS"] and only_ads[0].revenue_kop == 70_000
    assert next(iter(report)) == "ADS"  # sorted by registrations
