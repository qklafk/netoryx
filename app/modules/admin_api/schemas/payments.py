"""Payments, purchases, ledger rows and payment providers."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.db.models import LedgerAccount, PaymentStatus, PurchaseKind, TxKind
from app.modules.admin_api.schemas.common import OrmModel


class PaymentOut(OrmModel):
    id: str
    provider: str
    provider_payment_id: str | None
    user_id: int
    amount_kop: int
    plan_id: int | None
    subscription_id: int | None
    new_subscription: bool
    subscription_label: str | None
    is_gift: bool
    is_donation: bool
    is_recurring: bool
    provider_amount: str | None
    provider_currency: str | None
    status: PaymentStatus
    created_at: datetime
    updated_at: datetime
    credited_at: datetime | None


class PurchaseOut(OrmModel):
    id: int
    user_id: int
    subscription_id: int | None
    plan_id: int | None
    kind: PurchaseKind
    price_kop: int
    days: int
    expires_before: datetime | None
    expires_after: datetime
    payment_id: str | None
    actor_id: int | None
    created_at: datetime


class LedgerOut(OrmModel):
    id: int
    user_id: int
    account: LedgerAccount
    amount_kop: int
    kind: TxKind
    ref: str
    actor_id: int | None
    created_at: datetime


class PaymentDetail(BaseModel):
    payment: PaymentOut
    confirmation_url: str | None
    username: str | None
    plan_title: str | None
    ledger: list[LedgerOut]
    purchases: list[PurchaseOut]


class MarkPaidIn(BaseModel):
    paid_amount_kop: int | None = Field(default=None, gt=0, description="What the user actually paid, if different")
    reason: str = Field(min_length=3, max_length=500)


class CreditOut(BaseModel):
    credited: bool
    amount_kop: int = 0
    new_balance_kop: int | None = None
    hook_ran: bool = False


class RefundIn(BaseModel):
    debit_wallet: bool = Field(
        default=True, description="Take the refunded amount back from the wallet (as much as is left)"
    )
    reason: str = Field(min_length=3, max_length=500)


class RefundOut(BaseModel):
    refunded: bool
    debited_kop: int


class StatusIn(BaseModel):
    status: Literal["canceled", "expired"]
    reason: str | None = Field(default=None, max_length=500)


class ProviderOut(BaseModel):
    key: str
    title: str
    enabled: bool  # switched on in the settings
    active: bool  # a gateway is running with the current settings
    settings_group: str | None
    webhook_url: str | None
    supports_polling: bool | None
    min_amount_kop: int | None
    payments_30d: int
    amount_30d_kop: int
    last_success_at: datetime | None


class ProviderTestOut(BaseModel):
    ok: bool | None
    supported: bool
    detail: str | None = None
