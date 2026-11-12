"""Request and response models of the Mini App API. Money is in kopecks, times are ISO 8601 UTC."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Ok(BaseModel):
    ok: bool = True


# ---------------------------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------------------------
class MeOut(BaseModel):
    id: int
    username: str | None
    first_name: str | None
    language: str
    languages: list[str]
    mode: Literal["shop", "personal"]
    brand: str
    brand_logo_url: str = ""
    bot_username: str | None
    support_url: str | None
    balance_kop: int
    partner_balance_kop: int
    has_subscription: bool
    trial_available: bool
    trial_days: int
    unread_tickets: int
    features: dict[str, bool]


class LanguageIn(_In):
    language: str = Field(min_length=2, max_length=8)


class LanguageOut(BaseModel):
    language: str


# ---------------------------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------------------------
class PlanBrief(BaseModel):
    id: int
    title: str
    duration_days: int


class SubscriptionOut(BaseModel):
    id: int | None = None
    label: str = "Подписка"
    exists: bool
    state: Literal["active", "expired", "disabled", "none"]
    expires_at: datetime | None = None
    days_left: int = 0
    unlimited: bool = False  # never expires (personal mode members)
    plan: PlanBrief | None = None
    device_limit: int = 0  # 0 = unlimited
    traffic_limit_gb: int = 0  # 0 = unlimited
    traffic_used_bytes: int | None = None  # None when not requested or unknown
    auto_renew: bool = False
    auto_renew_available: bool = False
    is_trial: bool = False
    is_complimentary: bool = False
    stars_recurring: bool = False  # renewed by a Telegram Stars subscription
    trial_available: bool = False


class UsageDay(BaseModel):
    day: str  # YYYY-MM-DD in the service time zone
    bytes: int


class UsageOut(BaseModel):
    days: list[UsageDay]
    total_bytes: int


class AutoRenewIn(_In):
    enabled: bool
    subscription_id: int | None = Field(default=None, gt=0)


class SelectionIn(_In):
    subscription_id: int | None = Field(default=None, gt=0)


class SubscriptionLabelIn(_In):
    label: str = Field(min_length=1, max_length=64)


class SubscriptionsOut(BaseModel):
    items: list[SubscriptionOut]


class AutoRenewOut(BaseModel):
    auto_renew: bool


class RotateLinkOut(BaseModel):
    sub_url: str


# ---------------------------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------------------------
class DownloadOut(BaseModel):
    platform: str
    label: str
    url: str


class AppOut(BaseModel):
    key: str
    title: str
    platforms: list[str]
    downloads: list[DownloadOut]
    deeplink: str  # custom-scheme link that imports the subscription
    open_url: str  # HTTPS page that opens the deeplink (for Telegram.WebApp.openLink)
    supports_crypto_link: bool


class ConnectOut(BaseModel):
    state: Literal["active", "expired", "disabled", "none"]
    sub_url: str
    whitelist_url: str | None
    qr: str | None  # data:image/png;base64,...
    happ_crypt_link: str | None
    incy_crypt_link: str | None
    platforms: list[str]
    platform: str | None  # the requested platform, when known
    apps: list[AppOut]
    servers: list[ServerLinksOut] = Field(default_factory=list)


class ProfileLinkOut(BaseModel):
    name: str
    uri: str
    qr: str | None = None


class ServerLinksOut(BaseModel):
    id: str
    name: str
    subscription_url: str
    profiles: list[ProfileLinkOut]


# ---------------------------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------------------------
class DeviceOut(BaseModel):
    id: str
    platform: str | None
    model: str | None
    os_version: str | None
    user_agent: str | None
    ip: str | None
    created_at: datetime | None
    last_seen_at: datetime | None = None


class DeviceServerOut(BaseModel):
    panel_id: int
    server: str
    supported: bool
    can_delete_one: bool
    unavailable: bool
    devices: list[DeviceOut]


class DevicesOut(BaseModel):
    device_limit: int
    servers: list[DeviceServerOut]
    last_reset_at: datetime | None
    next_reset_at: datetime | None  # when a reset is possible again (None: now)


class DeviceResetIn(_In):
    subscription_id: int | None = Field(default=None, gt=0)
    panel_id: int | None = None
    device_id: str | None = Field(default=None, min_length=1, max_length=256)


class DeviceResetOut(BaseModel):
    reset: int
    failed: int


# ---------------------------------------------------------------------------------------------
# Plans & payments
# ---------------------------------------------------------------------------------------------
class PlanOut(BaseModel):
    id: int
    title: str
    duration_days: int
    price_kop: int
    monthly_price_kop: int
    discount_percent: int
    price_stars: int
    gift_price_kop: int  # a gift is always paid at the full tariff price
    gift_price_stars: int
    device_limit: int
    traffic_limit_gb: int
    includes_tg_proxy: bool
    hidden: bool


class ProviderOut(BaseModel):
    key: str
    title: str
    min_amount_kop: int
    recurring: bool  # can renew a 30-day plan automatically (Telegram Stars)
    # How the invoice is paid: a provider page, a Telegram Stars invoice, a direct blockchain transfer or a
    # manual card/SBP transfer confirmed by a receipt.
    kind: Literal["redirect", "stars", "onchain", "manual"] = "redirect"
    save_card: bool = False  # can keep the card for auto-renewal (when the buyer agrees)


class TopupLimits(BaseModel):
    min_kop: int
    max_kop: int
    quick_amounts_kop: list[int]


class PlansOut(BaseModel):
    plans: list[PlanOut]
    balance_kop: int
    providers: list[ProviderOut]
    gifts_available: bool
    topup: TopupLimits


class BuyIn(_In):
    subscription_id: int | None = Field(default=None, gt=0)
    new_subscription: bool = False
    label: str | None = Field(default=None, min_length=1, max_length=64)
    provider: str | None = Field(default=None, max_length=32)  # None: pay from the balance
    gift: bool = False
    recurring: bool = False  # Telegram Stars 30-day subscription
    save_card: bool = False  # keep the card for auto-renewal (providers with saved cards only)
    # One id per button press makes a retried or doubled purchase from the balance charge once.
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,32}$")


class ChainTransferOut(BaseModel):
    """A direct blockchain transfer: send exactly ``amount`` ``currency`` to ``address`` (with ``comment``)."""

    network: str
    currency: str
    address: str
    amount: str
    comment: str | None
    wallet_url: str | None  # https link that opens a wallet app
    deep_link: str | None  # chain URI, e.g. ton://transfer/...
    notes: list[str]  # warnings to show next to the instructions


class ManualTransferOut(BaseModel):
    details: str  # the operator's card / phone / bank instructions
    receipt_sent: bool


class PaymentOut(BaseModel):
    id: str
    provider: str
    status: str
    amount_kop: int
    plan_id: int | None
    is_gift: bool
    is_recurring: bool
    provider_amount: str | None
    provider_currency: str | None
    url: str | None  # provider's payment page
    invoice_link: str | None  # Telegram Stars: open with Telegram.WebApp.openInvoice
    created_at: datetime
    credited_at: datetime | None
    subscription_id: int | None = None
    save_card: bool = False  # the card will be kept for auto-renewal once paid
    transfer: ChainTransferOut | None = None
    manual: ManualTransferOut | None = None


class GiftOut(BaseModel):
    code: str
    link: str | None
    plan: PlanBrief


class BuyOut(BaseModel):
    result: Literal["purchased", "gift", "payment"]
    expires_at: datetime | None = None
    gift: GiftOut | None = None
    payment: PaymentOut | None = None
    balance_kop: int
    subscription_id: int | None = None


class TopupIn(_In):
    amount_kop: int = Field(gt=0)
    provider: str = Field(min_length=1, max_length=32)
    save_card: bool = False


# ---------------------------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------------------------
class TransactionOut(BaseModel):
    id: int
    kind: str
    account: str
    amount_kop: int
    title: str
    created_at: datetime


class SavedCardOut(BaseModel):
    id: int
    provider: str
    title: str
    created_at: datetime
    last_used_at: datetime | None


class SavedCardsOut(BaseModel):
    enabled: bool  # the shop charges saved cards for auto-renewal
    items: list[SavedCardOut]


class WalletOut(BaseModel):
    balance_kop: int
    partner_balance_kop: int
    items: list[TransactionOut]
    next_before_id: int | None


# ---------------------------------------------------------------------------------------------
# Referral
# ---------------------------------------------------------------------------------------------
class LevelOut(BaseModel):
    level: int
    percent: int
    users: int
    paid_users: int
    earned_kop: int
    earned_days: int


class WithdrawalOut(BaseModel):
    id: int
    amount_kop: int
    method: str
    status: str
    created_at: datetime
    processed_at: datetime | None
    comment: str | None


class PartnerOut(BaseModel):
    balance_kop: int
    pending_withdrawal_kop: int
    withdrawn_kop: int
    withdrawals_available: bool
    min_withdrawal_kop: int
    methods: list[str]
    withdrawals: list[WithdrawalOut]


class ReferralOut(BaseModel):
    mode: Literal["days", "balance", "partner"]
    code: str
    link: str | None
    share_text: str
    qr: str | None
    invited: int
    paid_invited: int
    earned_kop: int
    earned_days: int
    invitee_bonus_kop: int
    inviter_bonus_kop: int
    invitee_bonus_days: int
    inviter_bonus_days: int
    levels: list[LevelOut]
    partner: PartnerOut | None


class TransferIn(_In):
    amount_kop: int = Field(gt=0)
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")  # makes retries safe


class TransferOut(BaseModel):
    amount_kop: int
    applied: bool
    balance_kop: int
    partner_balance_kop: int


class WithdrawalIn(_In):
    amount_kop: int = Field(gt=0)
    method: str = Field(min_length=1, max_length=32)
    details: str = Field(min_length=1, max_length=512)


# ---------------------------------------------------------------------------------------------
# Promo / gift codes
# ---------------------------------------------------------------------------------------------
class RedeemIn(_In):
    code: str = Field(min_length=1, max_length=64)
    subscription_id: int | None = Field(default=None, gt=0)


class RedeemOut(BaseModel):
    kind: Literal["promo", "gift"]
    bonus_kop: int
    bonus_days: int
    balance_kop: int
    expires_at: datetime | None
    message: str


# ---------------------------------------------------------------------------------------------
# Telegram proxy
# ---------------------------------------------------------------------------------------------
class ProxyLinkOut(BaseModel):
    server: str
    port: int
    tme_url: str  # https://t.me/proxy?... (Telegram.WebApp.openTelegramLink)
    tg_url: str  # tg://proxy?... (copy)


class ProxyServerOut(BaseModel):
    title: str
    ready: bool
    links: list[ProxyLinkOut]
    web_url: str | None
    qr: str | None


class SocksLinkOut(BaseModel):
    server: str
    port: int
    user: str
    password: str
    tme_url: str  # https://t.me/socks?... (Telegram.WebApp.openTelegramLink)
    tg_url: str  # tg://socks?... (copy)


class SocksServerOut(BaseModel):
    title: str
    ready: bool
    link: SocksLinkOut | None
    qr: str | None


class ProxyOut(BaseModel):
    allowed: bool
    reason: str
    limited: bool  # free access with limits (speed cap / sponsored channel)
    servers: list[ProxyServerOut]
    socks: list[SocksServerOut] = []  # personal SOCKS5 accounts next to the MTProto links


# ---------------------------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------------------------
class TicketOut(BaseModel):
    id: int
    subject: str
    status: str
    created_at: datetime
    updated_at: datetime


class TicketsOut(BaseModel):
    items: list[TicketOut]
    open_count: int
    max_open: int


class TicketMessageOut(BaseModel):
    id: int
    from_support: bool
    text: str | None
    has_attachment: bool
    created_at: datetime


class TicketViewOut(BaseModel):
    ticket: TicketOut
    messages: list[TicketMessageOut]


class TicketCreateIn(_In):
    subject: str | None = Field(default=None, max_length=128)
    text: str = Field(min_length=1, max_length=4000)


class TicketReplyIn(_In):
    text: str = Field(min_length=1, max_length=4000)
