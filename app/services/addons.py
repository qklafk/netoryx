"""Paid traffic and device extras for an existing active subscription.

All charges go through the wallet ledger.  Traffic packs are exact byte quotas for the
current period; the next plan renewal deliberately restores the plan's base quota.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Subscription, SubscriptionAddonPurchase, SubscriptionStatus, TxKind, User
from app.services import provisioning, subscriptions, wallet
from app.utils.bytes import gb_to_bytes
from app.utils.time import utcnow


class AddonKind(StrEnum):
    TRAFFIC_PACK = "traffic_pack"
    TRAFFIC_RESET = "traffic_reset"
    DEVICE_ADDON = "device_addon"


class AddonError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Offer:
    kind: AddonKind
    quantity: int
    price_kop: int


def offer(settings: Settings, kind: AddonKind) -> Offer | None:
    if kind is AddonKind.TRAFFIC_PACK and settings.traffic_pack_gb > 0 and settings.traffic_pack_price_kop > 0:
        return Offer(kind, settings.traffic_pack_gb, settings.traffic_pack_price_kop)
    if kind is AddonKind.TRAFFIC_RESET and settings.traffic_reset_price_kop > 0:
        return Offer(kind, 1, settings.traffic_reset_price_kop)
    if kind is AddonKind.DEVICE_ADDON and settings.device_addon_price_kop > 0:
        return Offer(kind, settings.device_addon_count, settings.device_addon_price_kop)
    return None


def offers_for(settings: Settings, sub: Subscription) -> list[Offer]:
    result: list[Offer] = []
    for kind in AddonKind:
        item = offer(settings, kind)
        if item is None:
            continue
        if kind in (AddonKind.TRAFFIC_PACK, AddonKind.TRAFFIC_RESET) and provisioning.traffic_limit_bytes(sub) <= 0:
            continue
        if kind is AddonKind.DEVICE_ADDON and sub.device_limit <= 0:
            continue
        result.append(item)
    return result


async def purchase(
    session: AsyncSession,
    settings: Settings,
    user: User,
    subscription_id: int,
    kind: AddonKind,
    *,
    ref: str | None = None,
) -> SubscriptionAddonPurchase | None:
    """Charge once and immediately apply an enabled add-on; returns ``None`` for a replay."""
    if user.is_banned:
        raise AddonError("banned")
    sub = await subscriptions.lock_for_user(session, user.tg_id, subscription_id)
    if sub is None:
        raise AddonError("subscription_not_found")
    if sub.status is not SubscriptionStatus.ACTIVE or sub.expires_at <= utcnow():
        raise AddonError("subscription_inactive")
    item = offer(settings, kind)
    if item is None or item not in offers_for(settings, sub):
        raise AddonError("unavailable")
    charge_ref = ref or f"addon:{kind.value}:{uuid4().hex}"
    try:
        applied = await wallet.apply(session, user.tg_id, -item.price_kop, TxKind.PURCHASE, charge_ref)
    except wallet.InsufficientFunds as exc:
        raise AddonError("insufficient_funds") from exc
    if applied is None:
        return None
    if kind is AddonKind.TRAFFIC_PACK:
        sub.traffic_limit_bytes = provisioning.traffic_limit_bytes(sub) + gb_to_bytes(item.quantity)
    elif kind is AddonKind.DEVICE_ADDON:
        sub.device_limit += item.quantity
    else:
        # A reset begins a new usage epoch without changing the plan's quota.
        sub.traffic_epoch += 1
    if kind is not AddonKind.DEVICE_ADDON:
        sub.traffic_exhausted_epoch = None
    sub.revision += 1
    row = SubscriptionAddonPurchase(
        user_id=user.tg_id,
        subscription_id=sub.id,
        kind=kind.value,
        quantity=item.quantity,
        price_kop=item.price_kop,
        ref=charge_ref,
    )
    session.add(row)
    await provisioning.ensure_provisions(session, sub)
    return row
