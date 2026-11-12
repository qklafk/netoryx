"""Database schema (SQLAlchemy 2.0, async). Portable across SQLite, PostgreSQL and MySQL/MariaDB.

Domain model
------------
Users & money
* ``User``               - Telegram user + internal wallet (``balance_kop``) for top-ups and bonuses.
* ``Plan``               - tariff: duration, device limit, traffic limit, price. Managed by admins.
* ``Subscription``       - one or more independent user subscriptions; ``expires_at`` is the source of truth for each.
                           One VLESS UUID shared by every panel/inbound.
* ``Payment``            - a provider payment. Success always credits the wallet; if ``plan_id`` is set the
                           plan is then bought from the wallet (one code path for "top up" and "buy now").
* ``Purchase``           - history of purchases / renewals / trials / gifts / admin grants.
* ``BalanceTransaction`` - ledger; every balance change writes exactly one row (idempotency key).
* ``PromoCode``          - promo and gift codes (money and/or days).

Infrastructure
* ``Panel``     - a 3x-ui or Remnawave panel (credentials, health).
* ``Inbound``   - (3x-ui only) an inbound on a panel; cached link parameters.
* ``Location``  - user-facing grouping ("🇩🇪 Германия"); orders subscription entries.
* ``Endpoint``  - (3x-ui only) one line in the subscription: where clients connect to reach an inbound.
                  Direct, L4 relay (address/port replaced) or CDN (sni/host/path/... overridden);
                  profile "standard" or "whitelist" (LTE / RU mobile whitelist mode).
* ``Provision`` - the subscription's account on one target (3x-ui: panel+inbound; Remnawave: panel).
                  Reconciler converges actual -> desired.

Invariants: money is integer kopecks; timestamps are aware UTC (``UTCDateTime``);
balance changes only via ``app.services.wallet``; no network I/O inside DB transactions.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as UTC and always returns aware UTC datetimes (SQLite/MySQL drop tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime")
        value = value.astimezone(UTC)
        if dialect.name in ("sqlite", "mysql", "mariadb"):
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _enum(e: type[enum.Enum], name: str) -> Enum:
    # native_enum=False -> VARCHAR + CHECK: portable and easy to extend with a migration.
    return Enum(e, name=name, native_enum=False, values_callable=lambda x: [m.value for m in x], length=32)


# ---------------------------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------------------------
class SubscriptionStatus(enum.StrEnum):
    ACTIVE = "active"  # expires_at in the future
    EXPIRED = "expired"  # past expires_at; accounts disabled on panels, removed after retention
    DISABLED = "disabled"  # blocked by an admin


class ProvisionState(enum.StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    ABSENT = "absent"


class PanelKind(enum.StrEnum):
    XUI = "xui"
    REMNAWAVE = "remnawave"
    MARZBAN = "marzban"
    HIDDIFY = "hiddify"
    PASARGUARD = "pasarguard"
    MARZNESHIN = "marzneshin"
    TELEMT = "telemt"  # MTProto proxy for Telegram (telemt Control API); one user per subscription


class EndpointKind(enum.StrEnum):
    DIRECT = "direct"  # the inbound's own address
    RELAY = "relay"  # L4 relay / port-forward: only address+port differ
    CDN = "cdn"  # TLS/XHTTP/WS via CDN: sni/host/path/alpn/... overridden


class EndpointProfile(enum.StrEnum):
    STANDARD = "standard"
    WHITELIST = "whitelist"  # "LTE" entries for RU mobile whitelist mode


class PaymentProvider(enum.StrEnum):
    """Known providers. Stored as plain strings so new gateways need no migration."""

    STARS = "stars"
    CRYPTOBOT = "cryptobot"
    PLATEGA = "platega"
    YOOKASSA = "yookassa"
    YOOMONEY = "yoomoney"
    FREEKASSA = "freekassa"
    ROBOKASSA = "robokassa"
    HELEKET = "heleket"
    NOWPAYMENTS = "nowpayments"
    PLISIO = "plisio"
    TON = "ton"  # direct Toncoin transfers to the owner's wallet
    USDT_TRC20 = "usdt_trc20"  # direct USDT (TRC-20) transfers to the owner's TRON address
    WATA = "wata"
    ROLLYPAY = "rollypay"
    CISPAY = "cispay"
    TABPAY = "tabpay"
    SEVERPAY = "severpay"
    CASHERA = "cashera"
    PARITYPAY = "paritypay"
    AURAPAY = "aurapay"
    ANTILOPAY = "antilopay"
    STRIPE = "stripe"
    PAYPAL = "paypal"
    TRIBUTE = "tribute"
    LAVATOP = "lavatop"
    CUSTOM = "custom"  # owner-configured HTTP gateway
    MANUAL = "manual"
    CLOUDPAYMENTS = "cloudpayments"


class PaymentStatus(enum.StrEnum):
    CREATED = "created"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REFUNDED = "refunded"


class PurchaseKind(enum.StrEnum):
    TRIAL = "trial"
    PURCHASE = "purchase"  # bought by the user from the wallet
    AUTO_RENEW = "auto_renew"
    GIFT_REDEEM = "gift_redeem"  # redeemed a gift code
    PROMO = "promo"  # promo code with bonus days
    ADMIN_GRANT = "admin_grant"
    INVITE = "invite"  # access granted through an invitation link (personal mode)
    REFERRAL_REWARD = "referral_reward"  # days granted by the referral program ("days" mode)


class PromoKind(enum.StrEnum):
    PROMO = "promo"  # created by admins
    GIFT = "gift"  # bought by a user for someone else


class TxKind(enum.StrEnum):
    PAYMENT = "payment"  # +, ref = payment.id
    PURCHASE = "purchase"  # -, ref = purchase.id
    GIFT_PURCHASE = "gift_purchase"  # -, ref = promo_code.id
    REFERRAL_INVITEE = "referral_invitee"  # +, ref = "invitee"
    REFERRAL_INVITER = "referral_inviter"  # +, ref = f"fixed:{invitee_id}" / f"pct:{payment_id}"
    WELCOME_BONUS = "welcome_bonus"  # +, ref = "welcome"
    PROMO = "promo"  # +, ref = promo_code.id
    ADMIN_ADJUST = "admin_adjust"  # +/-, ref = unique action id
    REFUND = "refund"  # +/-, ref = payment.id / purchase.id
    # Partner (multi-level referral) account - see LedgerAccount.REFERRAL
    REFERRAL_COMMISSION = "referral_commission"  # + referral account, ref = f"L{level}:{payment_id}"
    REFERRAL_TRANSFER = "referral_transfer"  # -referral / +main, ref = transfer uuid (two rows, one per account)
    WITHDRAWAL = "withdrawal"  # - referral account, ref = withdrawal.id ; + back on rejection (kind REFUND)


class LedgerAccount(enum.StrEnum):
    MAIN = "main"  # spendable wallet (users.balance_kop)
    REFERRAL = "referral"  # partner earnings (users.referral_balance_kop): transferable to main or withdrawable


class WithdrawalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"  # approved, payout in progress (manual)
    PAID = "paid"
    REJECTED = "rejected"  # amount returned to the referral account


# ---------------------------------------------------------------------------------------------
# Users & money
# ---------------------------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("balance_kop >= 0", name="balance_non_negative"),
        CheckConstraint("referral_balance_kop >= 0", name="referral_balance_non_negative"),
        Index("ix_users_created_at", "created_at"),
        Index("ix_users_last_seen_at", "last_seen_at"),
    )

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), index=True)  # lowercase, without "@"
    first_name: Mapped[str | None] = mapped_column(String(256))
    language: Mapped[str] = mapped_column(String(8), default="ru", server_default="ru")
    balance_kop: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    # Partner earnings from multi-level referral commissions (withdrawable / transferable to balance_kop).
    referral_balance_kop: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    referral_code: Mapped[str] = mapped_column(String(32), unique=True)
    referred_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.tg_id", ondelete="SET NULL"), index=True
    )
    # Secret token of the personal subscription URL: <PUBLIC_BASE_URL>/sub/<sub_token>. Rotatable.
    sub_token: Mapped[str] = mapped_column(String(64), unique=True)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    receipt_email: Mapped[str | None] = mapped_column(String(256))
    bot_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="user", lazy="raise")


class PublicApiKey(Base):
    """Opaque, revocable user-owned key for the public read API; only a SHA-256 hash is persisted."""

    __tablename__ = "public_api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    token_hint: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("price_kop >= 0", name="price_non_negative"),
        CheckConstraint("duration_days > 0", name="duration_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(64))  # e.g. "1 месяц"
    duration_days: Mapped[int] = mapped_column(Integer)
    price_kop: Mapped[int] = mapped_column(BigInteger)
    # Optional explicit Stars price (otherwise derived from price via STARS_RUB_PER_STAR).
    price_stars: Mapped[int | None] = mapped_column(Integer)
    device_limit: Mapped[int] = mapped_column(Integer, default=3)  # 0 = unlimited
    traffic_limit_gb: Mapped[int] = mapped_column(Integer, default=0)  # per period; 0 = unlimited
    # Used when TG_PROXY_ACCESS=subscribers: whether this plan grants the Telegram proxy.
    includes_tg_proxy: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Hidden plans are not listed; available via deep link /start plan_<id> or to specific audiences.
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # ``all`` | ``new`` | ``existing`` | ``invited`` | ``allowlist``. Hidden plans still require their signed link.
    availability_rule: Mapped[str] = mapped_column(String(16), default="all", server_default="all")
    allowed_user_ids: Mapped[list[int]] = mapped_column(JSON, default=list, server_default=text("('[]')"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subscriptions_status_expires", "status", "expires_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(64), default="Подписка", server_default="Подписка")
    sub_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Existing accounts retain their remote names when the schema is upgraded.
    is_legacy: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    client_uuid: Mapped[str] = mapped_column(String(36), unique=True)  # VLESS id on every panel
    status: Mapped[SubscriptionStatus] = mapped_column(_enum(SubscriptionStatus, "subscription_status"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    device_limit: Mapped[int] = mapped_column(Integer, default=0)  # snapshot from the plan; 0 = unlimited
    traffic_limit_gb: Mapped[int] = mapped_column(Integer, default=0)
    traffic_limit_bytes: Mapped[int | None] = mapped_column(BigInteger)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Free access granted by the owner (family/friends, personal mode, VIP): no payment reminders, no auto-renew,
    # excluded from sales analytics.
    is_complimentary: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Smallest "days left" reminder already sent for the current period (3, 1; 0 = "expired" notice).
    last_reminder_days: Mapped[int | None] = mapped_column(Integer)
    # Telegram Stars native recurring subscription (30 days), if the user subscribed that way.
    stars_charge_id: Mapped[str | None] = mapped_column(String(128))
    # Bumped on any change that must be pushed to panels; provisions store the version they synced.
    revision: Mapped[int] = mapped_column(Integer, default=1)
    # Bumped when a new paid traffic period starts (plan bought/renewed): panel usage counters are reset.
    traffic_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # traffic_epoch in which the usage summed over all panel accounts reached traffic_limit_gb: the accounts are
    # disabled until a new period starts (or the quota is raised). Set and cleared by the provisioning scan.
    traffic_exhausted_epoch: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="subscriptions", lazy="raise")
    plan: Mapped[Plan | None] = relationship(lazy="raise")
    provisions: Mapped[list[Provision]] = relationship(back_populates="subscription", lazy="raise")


class SubscriptionDevice(Base):
    """A client device seen on a subscription link (``x-hwid``), kept to enforce the device limit in the bot.

    Only a keyed hash of the hardware id is stored, never the id itself, and no network address.
    """

    __tablename__ = "subscription_devices"
    __table_args__ = (
        UniqueConstraint("subscription_id", "hwid_hash"),
        Index("ix_subscription_devices_last_seen_at", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True)
    hwid_hash: Mapped[str] = mapped_column(String(64))  # hex HMAC-SHA256 of the client's hardware id
    platform: Mapped[str | None] = mapped_column(String(32))
    os_version: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    app: Mapped[str | None] = mapped_column(String(64))  # client application, from its user agent
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id"),
        Index("ix_payments_status_created", "status", "created_at"),
        Index("ix_payments_user_created", "user_id", "created_at"),
        Index("ix_payments_credited_at", "credited_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4; idempotence key / order id
    provider: Mapped[str] = mapped_column(String(32))  # PaymentProvider value
    provider_payment_id: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), index=True)
    amount_kop: Mapped[int] = mapped_column(BigInteger)  # wallet credit on success
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))  # buy right after
    # Price locked when the invoice was created. It may be lower than the current tariff due to a personal discount.
    plan_price_kop: Mapped[int | None] = mapped_column(BigInteger)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id", ondelete="SET NULL"), index=True)
    new_subscription: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    subscription_label: Mapped[str | None] = mapped_column(String(64))
    is_gift: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # buy plan as a gift
    is_donation: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # no wallet credit
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # Stars sub
    # What the provider actually charged in its own unit (Stars, USDT...), informational.
    provider_amount: Mapped[str | None] = mapped_column(String(64))
    provider_currency: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[PaymentStatus] = mapped_column(_enum(PaymentStatus, "payment_status"))
    confirmation_url: Mapped[str | None] = mapped_column(Text)
    # Telegram receipt for a manual card/SBP transfer. The file stays on Telegram; no binary is stored locally.
    receipt_file_id: Mapped[str | None] = mapped_column(String(256))
    receipt_media_type: Mapped[str | None] = mapped_column(String(16))  # photo | document
    # The payer agreed to keep the card for auto-renewal (the provider is asked to save it).
    save_method: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Set on an auto-renewal charge of a saved card; the id is derived from the renewed period.
    saved_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("saved_payment_methods.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    credited_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SavedPaymentMethod(Base):
    """A card the user allowed to be charged for auto-renewal.

    ``token`` is the provider's reusable payment method id / card token, never a card number. Removing a card
    only deactivates the row so a late provider notification cannot bring it back.
    """

    __tablename__ = "saved_payment_methods"
    __table_args__ = (UniqueConstraint("provider", "token"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))  # PaymentProvider value
    token: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(64))  # e.g. "Visa •• 4242"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deactivated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Purchase(Base):
    __tablename__ = "purchases"
    __table_args__ = (Index("ix_purchases_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id", ondelete="SET NULL"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    kind: Mapped[PurchaseKind] = mapped_column(_enum(PurchaseKind, "purchase_kind"))
    price_kop: Mapped[int] = mapped_column(BigInteger, default=0)
    days: Mapped[int] = mapped_column(Integer)
    expires_before: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_after: Mapped[datetime] = mapped_column(UTCDateTime())
    payment_id: Mapped[str | None] = mapped_column(String(36))
    actor_id: Mapped[int | None] = mapped_column(BigInteger)  # admin for ADMIN_GRANT
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class SubscriptionAddonPurchase(Base):
    """An auditable paid extra applied to one subscription in its current period."""

    __tablename__ = "subscription_addon_purchases"
    __table_args__ = (Index("ix_addon_purchases_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True)
    # traffic_pack | traffic_reset | device_addon.  A string avoids a database enum migration for new offers.
    kind: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer)
    price_kop: Mapped[int] = mapped_column(BigInteger)
    ref: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class BalanceTransaction(Base):
    """Ledger. UNIQUE(account, kind, ref, user_id) makes every credit/charge idempotent."""

    __tablename__ = "balance_transactions"
    __table_args__ = (
        UniqueConstraint("account", "kind", "ref", "user_id"),
        Index("ix_balance_transactions_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    # Which balance the row affects: MAIN -> users.balance_kop, REFERRAL -> users.referral_balance_kop.
    account: Mapped[LedgerAccount] = mapped_column(
        _enum(LedgerAccount, "ledger_account"), default=LedgerAccount.MAIN, server_default="main"
    )
    amount_kop: Mapped[int] = mapped_column(BigInteger)  # signed
    kind: Mapped[TxKind] = mapped_column(_enum(TxKind, "tx_kind"))
    ref: Mapped[str] = mapped_column(String(128))
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)  # stored uppercase
    kind: Mapped[PromoKind] = mapped_column(_enum(PromoKind, "promo_kind"), default=PromoKind.PROMO)
    bonus_kop: Mapped[int] = mapped_column(BigInteger, default=0)  # wallet credit
    bonus_days: Mapped[int] = mapped_column(Integer, default=0)  # subscription extension
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))  # gift: limits
    max_uses: Mapped[int] = mapped_column(Integer, default=1)  # 0 = unlimited
    uses: Mapped[int] = mapped_column(Integer, default=0)
    only_new_users: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_by: Mapped[int | None] = mapped_column(BigInteger)  # admin or gift buyer tg_id
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PromoActivation(Base):
    __tablename__ = "promo_activations"
    __table_args__ = (UniqueConstraint("promo_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PromoGroup(Base):
    """A named audience with a percentage discount on every tariff purchase."""

    __tablename__ = "promo_groups"
    __table_args__ = (
        CheckConstraint("discount_percent > 0 AND discount_percent < 100", name="discount_percent_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(64), unique=True)
    discount_percent: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PromoGroupMember(Base):
    __tablename__ = "promo_group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id"),
        Index("ix_promo_group_members_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("promo_groups.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class WinbackOffer(Base):
    """A temporary automatic personal discount for a user who did not convert or churned."""

    __tablename__ = "winback_offers"
    __table_args__ = (
        UniqueConstraint("user_id", "kind"),
        Index("ix_winback_offers_active", "expires_at", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # trial | churned
    discount_percent: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---------------------------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------------------------
class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))  # "Германия"
    flag: Mapped[str] = mapped_column(String(16), default="")  # "🇩🇪"
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class Panel(Base):
    __tablename__ = "panels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[PanelKind] = mapped_column(_enum(PanelKind, "panel_kind"))
    url: Mapped[str] = mapped_column(String(512))  # base URL incl. web base path
    username: Mapped[str | None] = mapped_column(String(256))
    password: Mapped[str | None] = mapped_column(Text)  # encrypted ("enc:" prefix) when SECRET_KEY is set
    api_token: Mapped[str | None] = mapped_column(Text)  # encrypted; 3x-ui v3.1+ Bearer / Remnawave API token
    tls_verify: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # JSON object: extra HTTP headers/cookies (e.g. Remnawave caddy X-Api-Key) and kind-specific options
    # (remnawave: {"squad_uuids": [...], "username_prefix": "tg_", "direct": true, "location_id": 1}).
    options: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Health
    version: Mapped[str | None] = mapped_column(String(32))
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    inbounds: Mapped[list[Inbound]] = relationship(back_populates="panel", lazy="raise")


class Inbound(Base):
    """3x-ui inbound. Link params are cached from the panel (refreshed periodically)."""

    __tablename__ = "inbounds"
    __table_args__ = (UniqueConstraint("panel_id", "remote_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"))
    remote_id: Mapped[int] = mapped_column(Integer)  # inbound id inside 3x-ui
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(64))
    # Cached JSON of link-relevant params read from the panel (protocol, port, network, security, reality/tls
    # public fields, xhttp/ws/grpc fields). Endpoint overrides are applied on top.
    params: Mapped[str | None] = mapped_column(Text)
    params_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    flow: Mapped[str] = mapped_column(String(32), default="")  # e.g. xtls-rprx-vision (tcp+reality only)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    panel: Mapped[Panel] = relationship(back_populates="inbounds", lazy="raise")
    endpoints: Mapped[list[Endpoint]] = relationship(back_populates="inbound", lazy="raise")


class Endpoint(Base):
    """One subscription entry for an inbound (direct / relay / CDN; standard or whitelist profile)."""

    __tablename__ = "endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inbound_id: Mapped[int] = mapped_column(ForeignKey("inbounds.id", ondelete="CASCADE"), index=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))
    kind: Mapped[EndpointKind] = mapped_column(_enum(EndpointKind, "endpoint_kind"))
    profile: Mapped[EndpointProfile] = mapped_column(_enum(EndpointProfile, "endpoint_profile"))
    address: Mapped[str] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)  # None = inbound port
    # JSON object overriding link params: security, sni, host, path, alpn, fp, mode, extra, serviceName, ...
    overrides: Mapped[str | None] = mapped_column(Text)
    remark: Mapped[str | None] = mapped_column(String(64))  # defaults to "<flag> <location>[ · LTE]"
    description: Mapped[str | None] = mapped_column(String(64))  # Happ serverDescription
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Health (TCP/TLS probe); auto_hidden endpoints are skipped in subscriptions until they recover.
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    auto_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    inbound: Mapped[Inbound] = relationship(back_populates="endpoints", lazy="raise")


class Provision(Base):
    """A subscription's account on one target: (panel, inbound) for 3x-ui, (panel) for Remnawave."""

    __tablename__ = "provisions"
    __table_args__ = (
        UniqueConstraint("subscription_id", "panel_id", "inbound_key"),
        Index("ix_provisions_panel_next_attempt", "panel_id", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"))
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"), index=True)
    inbound_id: Mapped[int | None] = mapped_column(ForeignKey("inbounds.id", ondelete="CASCADE"))
    inbound_key: Mapped[int] = mapped_column(Integer, default=0)  # = inbound_id or 0 (unique-friendly)
    remote_email: Mapped[str | None] = mapped_column(String(128))  # 3x-ui client email / Remnawave username
    remote_id: Mapped[str | None] = mapped_column(String(64))  # Remnawave user id
    remote_short_id: Mapped[str | None] = mapped_column(String(64))  # Remnawave shortUuid
    # Encrypted JSON cache of share links returned by the backend (telemt tg://proxy links, ...).
    links_cache: Mapped[str | None] = mapped_column(Text)
    desired_state: Mapped[ProvisionState] = mapped_column(_enum(ProvisionState, "provision_desired"))
    actual_state: Mapped[ProvisionState] = mapped_column(_enum(ProvisionState, "provision_actual"))
    synced_revision: Mapped[int] = mapped_column(Integer, default=0)  # Subscription.revision last pushed
    traffic_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # Subscription's, applied
    last_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    subscription: Mapped[Subscription] = relationship(back_populates="provisions", lazy="raise")
    panel: Mapped[Panel] = relationship(lazy="raise")
    inbound: Mapped[Inbound | None] = relationship(lazy="raise")


class KeyValue(Base):
    """Runtime settings store (maintenance mode, generated secrets, bot-editable settings...)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# ---------------------------------------------------------------------------------------------
# Operations, analytics & support
# ---------------------------------------------------------------------------------------------
class BroadcastStatus(enum.StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    DONE = "done"
    CANCELED = "canceled"


class TicketStatus(enum.StrEnum):
    OPEN = "open"
    ANSWERED = "answered"  # waiting for the user
    CLOSED = "closed"


class AdminAuditLog(Base):
    """Every admin action (bot or web): who did what to whom."""

    __tablename__ = "admin_audit_log"
    __table_args__ = (Index("ix_admin_audit_log_actor_created", "actor_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)  # admin tg_id
    action: Mapped[str] = mapped_column(String(64))  # e.g. "user.balance_adjust", "plan.update"
    target_type: Mapped[str | None] = mapped_column(String(32))  # "user", "plan", "panel", ...
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)
    details: Mapped[str | None] = mapped_column(Text)  # JSON
    source: Mapped[str] = mapped_column(String(16), default="bot")  # "bot" | "web"
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class AdminSession(Base):
    """Web admin sessions (revocable). Only a hash of the session token is stored."""

    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class CabinetSession(Base):
    """Web cabinet sessions of customers (revocable). Only a hash of the session token is stored."""

    __tablename__ = "cabinet_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex
    method: Mapped[str] = mapped_column(String(16))  # bot | telegram | email
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class UserEmail(Base):
    """A confirmed e-mail address of a user (lower case); used for web cabinet login links."""

    __tablename__ = "user_emails"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    verified_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    # "all" | "active" | "expired" | "never_paid" | "trial" | "no_subscription"
    audience: Mapped[str] = mapped_column(String(32))
    # Message to copy (bot: source chat + message id) or HTML text (web).
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    text: Mapped[str | None] = mapped_column(Text)
    buttons: Mapped[str | None] = mapped_column(Text)  # JSON [[{"text","url"}]]
    status: Mapped[BroadcastStatus] = mapped_column(_enum(BroadcastStatus, "broadcast_status"))
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    last_user_id: Mapped[int | None] = mapped_column(BigInteger)  # resume cursor
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Event(Base):
    """Analytics event stream (funnel, conversions, churn). Append-only."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_type_created", "type", "created_at"),
        Index("ix_events_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    # "user.registered", "trial.started", "payment.succeeded", "purchase", "subscription.expired",
    # "referral.applied", "promo.redeemed", "gift.redeemed", ...
    type: Mapped[str] = mapped_column(String(48))
    amount_kop: Mapped[int | None] = mapped_column(BigInteger)
    data: Mapped[str | None] = mapped_column(Text)  # JSON
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class WebhookDeliveryStatus(enum.StrEnum):
    PENDING = "pending"  # waiting for the first attempt
    SENT = "sent"  # the receiver answered 2xx
    FAILED = "failed"  # the last attempt failed; another one is scheduled at ``next_attempt_at``
    DEAD = "dead"  # gave up after the last retry


class WebhookEndpoint(Base):
    """An external URL that receives signed copies of business events (CRMs, analytics, integrations)."""

    __tablename__ = "webhook_endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(1024))
    secret: Mapped[str] = mapped_column(Text)  # HMAC key, encrypted ("enc:" prefix)
    events: Mapped[str] = mapped_column(String(1024), default="*")  # comma-separated event types or "*"
    description: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)  # failed attempts in a row
    consecutive_dead: Mapped[int] = mapped_column(Integer, default=0)  # deliveries given up in a row
    disabled_reason: Mapped[str | None] = mapped_column(String(255))
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class WebhookDelivery(Base):
    """Outbox row: one event for one endpoint, written in the transaction that produced the event."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_status_next", "status", "next_attempt_at"),
        Index("ix_webhook_deliveries_endpoint_created", "endpoint_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(Integer, ForeignKey("webhook_endpoints.id", ondelete="CASCADE"))
    event_id: Mapped[str] = mapped_column(String(40))
    event_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[str] = mapped_column(Text)  # JSON body, sent byte for byte on every attempt
    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        _enum(WebhookDeliveryStatus, "webhook_delivery_status"), default=WebhookDeliveryStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class UsageSnapshot(Base):
    """Daily traffic usage per subscription (summed over panels), for charts and stats."""

    __tablename__ = "usage_snapshots"
    __table_args__ = (UniqueConstraint("subscription_id", "day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"))
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD in TZ
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)  # cumulative counter at snapshot time


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (Index("ix_tickets_status_updated", "status", "updated_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(128))
    status: Mapped[TicketStatus] = mapped_column(_enum(TicketStatus, "ticket_status"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    messages: Mapped[list[TicketMessage]] = relationship(back_populates="ticket", lazy="raise")


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int] = mapped_column(BigInteger)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    text: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[str | None] = mapped_column(String(256))  # Telegram photo/document file_id
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="messages", lazy="raise")


class Withdrawal(Base):
    """Partner payout request (paid manually by an admin outside the system)."""

    __tablename__ = "withdrawals"
    __table_args__ = (
        CheckConstraint("amount_kop > 0", name="amount_positive"),
        Index("ix_withdrawals_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), index=True)
    amount_kop: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(32))  # "card" | "sbp" | "usdt_trc20" | "ton" | ...
    details: Mapped[str] = mapped_column(String(512))  # card number / phone+bank / wallet address
    status: Mapped[WithdrawalStatus] = mapped_column(_enum(WithdrawalStatus, "withdrawal_status"))
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    admin_comment: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ReferralReward(Base):
    """One referral reward (money or days) for one level of one source purchase/payment. Basis for partner stats."""

    __tablename__ = "referral_rewards"
    __table_args__ = (
        UniqueConstraint("source_ref", "level"),
        Index("ix_referral_rewards_beneficiary_created", "beneficiary_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    beneficiary_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    source_user_id: Mapped[int] = mapped_column(BigInteger)  # the referral who paid
    level: Mapped[int] = mapped_column(Integer)  # 1 = direct referral; 0 = one-time bonus
    # "payment:<id>" | "purchase:<id>" | "bonus:inviter:<invitee_id>" | "bonus:invitee:<invitee_id>"
    source_ref: Mapped[str] = mapped_column(String(128))
    amount_kop: Mapped[int] = mapped_column(BigInteger, default=0)
    days: Mapped[int] = mapped_column(Integer, default=0)
    account: Mapped[LedgerAccount | None] = mapped_column(_enum(LedgerAccount, "ledger_account_rr"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AdminRole(enum.StrEnum):
    """Role-based access for bot & web admin. ADMIN_IDS from env are always OWNER."""

    OWNER = "owner"  # everything incl. admins, panels credentials, backups/restore, payment credentials
    ADMIN = "admin"  # everything except managing owners/admins and restoring backups
    SUPPORT = "support"  # users (view, message, grant days, reset devices), tickets
    VIEWER = "viewer"  # read-only dashboards


class Admin(Base):
    """Additional admins managed from the admin panel (owners come from ADMIN_IDS)."""

    __tablename__ = "admins"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    role: Mapped[AdminRole] = mapped_column(_enum(AdminRole, "admin_role"))
    note: Mapped[str | None] = mapped_column(String(128))
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class TextOverride(Base):
    """Admin-edited bot texts (white-label). Falls back to built-in locale strings when absent."""

    __tablename__ = "text_overrides"
    __table_args__ = (UniqueConstraint("key", "language"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128))  # i18n key, e.g. "start.welcome"
    language: Mapped[str] = mapped_column(String(8))
    value: Mapped[str] = mapped_column(Text)  # HTML; validated against the key's placeholders on save
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class MenuButton(Base):
    """A custom button of the bot's main menu, shown after the built-in entries.

    ``kind``: ``url`` | ``webapp`` | ``copy_text`` | ``text_screen``; ``payload`` is the link, the text to copy or
    the (Telegram HTML) text of the screen. Buttons sharing ``row`` are placed side by side, ordered by
    ``sort_order``. ``audience``: ``all`` | ``subscribers`` | ``non_subscribers`` | ``admins``.
    """

    __tablename__ = "menu_buttons"
    __table_args__ = (Index("ix_menu_buttons_row_order", "row", "sort_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(64))  # default language title
    titles: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, server_default=text("('{}')"))  # per language
    payload: Mapped[str] = mapped_column(Text)
    row: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    audience: Mapped[str] = mapped_column(String(16), default="all", server_default="all")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class TgProxyAccess(Base):
    """Per-user MTProto proxy account for Telegram (free for every user, independent of subscriptions).

    One row per (user, proxy panel). Created lazily when the user opens the proxy screen (and for owners right after
    install); disabled when the user is banned; reconciled like provisions (desired vs actual state).
    """

    __tablename__ = "tg_proxy_access"
    __table_args__ = (
        UniqueConstraint("user_id", "panel_id"),
        Index("ix_tg_proxy_access_panel_next_attempt", "panel_id", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), index=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"))  # TELEMT panel or 3x-ui
    inbound_id: Mapped[int | None] = mapped_column(ForeignKey("inbounds.id", ondelete="CASCADE"))  # 3x-ui mtproto
    remote_name: Mapped[str] = mapped_column(String(64))  # telemt username / 3x-ui client email
    links_cache: Mapped[str | None] = mapped_column(Text)  # encrypted JSON of tg://proxy / t.me/proxy links
    desired_state: Mapped[ProvisionState] = mapped_column(_enum(ProvisionState, "tgp_desired"))
    actual_state: Mapped[ProvisionState] = mapped_column(_enum(ProvisionState, "tgp_actual"))
    revision: Mapped[int] = mapped_column(Integer, default=1)  # bumped on rotate / limit change
    synced_revision: Mapped[int] = mapped_column(Integer, default=0)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class TgSocksAccess(Base):
    """Per-user SOCKS5 proxy account for Telegram on a 3x-ui ``mixed`` inbound (password auth).

    One row per (user, panel). The username is derived from the user id, the password is random and stored
    encrypted. Desired/actual state work as for :class:`TgProxyAccess`; a disabled account is simply absent from
    the inbound's account list on the server, the row keeps the credentials.
    """

    __tablename__ = "tg_socks_access"
    __table_args__ = (
        UniqueConstraint("user_id", "panel_id"),
        Index("ix_tg_socks_access_panel_next_attempt", "panel_id", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), index=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"))
    inbound_id: Mapped[int] = mapped_column(ForeignKey("inbounds.id", ondelete="CASCADE"))
    username: Mapped[str] = mapped_column(String(64))
    password_enc: Mapped[str] = mapped_column(Text)  # SecretBox-encrypted password
    desired_state: Mapped[ProvisionState] = mapped_column(_enum(ProvisionState, "tgs_desired"))
    actual_state: Mapped[ProvisionState] = mapped_column(_enum(ProvisionState, "tgs_actual"))
    revision: Mapped[int] = mapped_column(Integer, default=1)  # bumped on rotate / policy change
    synced_revision: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Invite(Base):
    """Invitation link (personal mode, or free access for friends in shop mode): /start inv_<code>."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(128))  # e.g. "мама", "Дима"
    max_uses: Mapped[int] = mapped_column(Integer, default=1)  # 0 = unlimited
    uses: Mapped[int] = mapped_column(Integer, default=0)
    # Access preset given to whoever redeems the invite.
    days: Mapped[int | None] = mapped_column(Integer)  # None = unlimited (complimentary, never expires)
    device_limit: Mapped[int] = mapped_column(Integer, default=3)  # 0 = unlimited
    traffic_limit_gb: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())  # link validity
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class TaxReceiptStatus(enum.StrEnum):
    PENDING = "pending"  # waiting to be registered in "Мой налог"
    REGISTERED = "registered"
    FAILED = "failed"  # gave up after NPD_MAX_ATTEMPTS; an admin may retry
    CANCEL_PENDING = "cancel_pending"  # the payment was refunded; the receipt is to be annulled
    CANCELLED = "cancelled"


class TaxReceipt(Base):
    """Income of a credited payment registered in "Мой налог" (self-employed tax, NPD); one per payment.

    Kept without a foreign key so the tax history outlives deleted users and payments.
    """

    __tablename__ = "tax_receipts"
    __table_args__ = (Index("ix_tax_receipts_status_next_attempt", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(36), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))  # TaxReceiptStatus value
    amount_kop: Mapped[int] = mapped_column(BigInteger)
    service_name: Mapped[str] = mapped_column(String(256))
    operation_time: Mapped[datetime] = mapped_column(UTCDateTime())  # when the money was received
    receipt_uuid: Mapped[str | None] = mapped_column(String(64))
    receipt_url: Mapped[str | None] = mapped_column(String(512))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(String(500))
    # An income request was sent at least once: look for the income in the cabinet before sending another.
    sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    user_notified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    registered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
