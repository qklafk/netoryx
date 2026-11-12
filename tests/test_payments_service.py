"""Payment lifecycle tests: create, credit exactly once, webhooks, reconciler, Telegram Stars.

The wallet / referral / event services are replaced by small in-file stubs with the same semantics
(ledger row with UNIQUE(kind, ref, user_id) inside a SAVEPOINT), so these tests exercise only the
payment logic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    BalanceTransaction,
    Payment,
    PaymentStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
    TxKind,
    User,
)
from app.db.session import SessionFactory
from app.services import subscriptions
from app.services.payments import freekassa, service, stars, yoomoney
from app.services.payments.base import (
    GatewayError,
    Invoice,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
)
from app.services.payments.freekassa import FreeKassaGateway
from app.services.payments.service import CreditResult
from app.services.payments.yoomoney import YooMoneyGateway
from tests.conftest import make_settings

USER_ID = 5512345678
INVITER_ID = 777


# ---------------------------------------------------------------------------------------------
# Stubs for CORE services
# ---------------------------------------------------------------------------------------------
@dataclass
class Core:
    events: list[tuple[str, int | None, int | None, dict[str, Any]]] = field(default_factory=list)
    referral_calls: list[tuple[int, str, int]] = field(default_factory=list)

    async def apply(
        self, session: AsyncSession, user_id: int, amount_kop: int, kind: TxKind, ref: str, *, actor_id: Any = None
    ) -> int | None:
        try:
            async with session.begin_nested():
                await session.execute(
                    insert(BalanceTransaction).values(user_id=user_id, amount_kop=amount_kop, kind=kind, ref=ref)
                )
                await session.execute(
                    update(User).where(User.tg_id == user_id).values(balance_kop=User.balance_kop + amount_kop)
                )
        except IntegrityError:
            return None
        return amount_kop

    async def on_invitee_payment(
        self,
        session: AsyncSession,
        settings: Settings,
        invitee_id: int,
        payment_id: str,
        amount_kop: int,
        *,
        expected_kop: int | None = None,
    ) -> tuple[int, int] | None:
        self.referral_calls.append((invitee_id, payment_id, amount_kop))
        referred_by = await session.scalar(select(User.referred_by).where(User.tg_id == invitee_id))
        return (referred_by, amount_kop // 10) if referred_by else None

    async def track(
        self, session: AsyncSession, type: str, user_id: int | None = None, amount_kop: int | None = None, **data: Any
    ) -> None:
        self.events.append((type, user_id, amount_kop, data))

    def event_types(self) -> list[str]:
        return [e[0] for e in self.events]


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> Core:
    stub = Core()
    monkeypatch.setattr(service, "wallet", SimpleNamespace(apply=stub.apply))
    monkeypatch.setattr(service, "referrals", SimpleNamespace(on_invitee_payment=stub.on_invitee_payment))
    monkeypatch.setattr(service, "events", SimpleNamespace(track=stub.track))
    return stub


@pytest.fixture
async def db(session_factory: SessionFactory, core: Core) -> SessionFactory:
    async with session_factory.begin() as s:
        s.add(User(tg_id=INVITER_ID, referral_code="INV", sub_token="tok-inv"))
        await s.flush()
        s.add(User(tg_id=USER_ID, referral_code="USR", sub_token="tok-usr", referred_by=INVITER_ID))
        s.add(Plan(id=1, title="1 месяц", duration_days=30, price_kop=15000, price_stars=120))
    return session_factory


# ---------------------------------------------------------------------------------------------
# Fake gateway
# ---------------------------------------------------------------------------------------------
@dataclass
class FakeGateway:
    key: str = "fake"
    title: str = "Fake"
    supports_polling: bool = True
    min_amount_kop: int = 100
    status: ProviderStatus = field(default_factory=lambda: ProviderStatus(PaymentStatus.PENDING))
    create_error: BaseException | None = None
    fetch_error: BaseException | None = None
    webhook: WebhookResult = field(default_factory=WebhookResult)
    barrier: asyncio.Barrier | None = None
    seen_on_create: list[Any] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    session_factory: SessionFactory | None = None

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        if self.session_factory is not None:  # observe what is committed when the provider is called
            async with self.session_factory() as s:
                row = await s.get(Payment, payment.id)
                self.seen_on_create.append((row.status if row else None, description, user.tg_id))
        if self.create_error is not None:
            raise self.create_error
        return Invoice(f"prov-{payment.id[:8]}", f"https://pay.example/{payment.id}", "150.00", "RUB")

    async def fetch(self, payment: Payment) -> ProviderStatus:
        self.fetched.append(payment.id)
        if self.barrier is not None:
            await self.barrier.wait()
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.status

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if raw == b"forged":
            raise WebhookRejected("bad signature", 401)
        return self.webhook

    async def close(self) -> None:
        return None


SETTINGS = make_settings()


async def _add_payment(
    sf: SessionFactory,
    *,
    pid: str = "pay-1",
    provider: str = "fake",
    status: PaymentStatus = PaymentStatus.PENDING,
    amount_kop: int = 15000,
    age: timedelta = timedelta(0),
    provider_payment_id: str | None = "prov-1",
    **kw: Any,
) -> Payment:
    payment = Payment(
        id=pid,
        provider=provider,
        provider_payment_id=provider_payment_id,
        user_id=USER_ID,
        amount_kop=amount_kop,
        status=status,
        created_at=datetime.now(UTC) - age,
        **kw,
    )
    async with sf.begin() as s:
        s.add(payment)
    return payment


async def _payment(sf: SessionFactory, pid: str) -> Payment:
    async with sf() as s:
        row = await s.get(Payment, pid)
    assert row is not None
    return row


async def _balance(sf: SessionFactory, user_id: int = USER_ID) -> int:
    async with sf() as s:
        return int(await s.scalar(select(User.balance_kop).where(User.tg_id == user_id)) or 0)


async def _ledger(sf: SessionFactory) -> list[tuple[int, int, TxKind, str]]:
    async with sf() as s:
        rows = await s.execute(
            select(
                BalanceTransaction.user_id,
                BalanceTransaction.amount_kop,
                BalanceTransaction.kind,
                BalanceTransaction.ref,
            )
        )
        return [tuple(r) for r in rows.all()]  # type: ignore[misc]


class Hook:
    def __init__(self, fail: bool = False) -> None:
        self.results: list[CreditResult] = []
        self.fail = fail

    async def __call__(self, result: CreditResult) -> None:
        self.results.append(result)
        if self.fail:
            raise RuntimeError("bot is down")


# ---------------------------------------------------------------------------------------------
# create_payment
# ---------------------------------------------------------------------------------------------
async def test_create_persists_before_calling_gateway(db: SessionFactory) -> None:
    gw = FakeGateway(session_factory=db)
    payment = await service.create_payment(db, SETTINGS, gw, USER_ID, 15000, plan_id=1)
    assert gw.seen_on_create == [(PaymentStatus.CREATED, SETTINGS.payment_description, USER_ID)]
    assert payment.status is PaymentStatus.PENDING
    assert payment.provider == "fake" and payment.plan_id == 1
    assert payment.provider_payment_id == f"prov-{payment.id[:8]}"
    assert payment.confirmation_url == f"https://pay.example/{payment.id}"
    assert (payment.provider_amount, payment.provider_currency) == ("150.00", "RUB")
    stored = await _payment(db, payment.id)
    assert stored.status is PaymentStatus.PENDING and stored.amount_kop == 15000


async def test_create_custom_description_and_flags(db: SessionFactory) -> None:
    gw = FakeGateway(session_factory=db)
    payment = await service.create_payment(
        db, SETTINGS, gw, USER_ID, 500, is_donation=True, is_gift=True, is_recurring=True, description="Донат"
    )
    assert gw.seen_on_create[0][1] == "Донат"
    assert (payment.is_donation, payment.is_gift, payment.is_recurring) == (True, True, True)


@pytest.mark.parametrize("error", [GatewayError("HTTP 500"), RuntimeError("boom"), TimeoutError()])
async def test_create_marks_canceled_on_gateway_error(db: SessionFactory, error: BaseException) -> None:
    gw = FakeGateway(create_error=error)
    with pytest.raises(GatewayError):
        await service.create_payment(db, SETTINGS, gw, USER_ID, 15000)
    async with db() as s:
        rows = (await s.scalars(select(Payment))).all()
    assert [r.status for r in rows] == [PaymentStatus.CANCELED]
    assert rows[0].provider_payment_id is None


@pytest.mark.parametrize("amount", [0, -100, 99, True, 150.0])
async def test_create_rejects_invalid_amounts(db: SessionFactory, amount: Any) -> None:
    with pytest.raises(ValueError):
        await service.create_payment(db, SETTINGS, FakeGateway(), USER_ID, amount)
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(Payment)) == 0


async def test_create_unknown_user(db: SessionFactory) -> None:
    gw = FakeGateway()
    with pytest.raises(LookupError):
        await service.create_payment(db, SETTINGS, gw, 424242, 15000)
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(Payment)) == 0


async def test_create_does_not_downgrade_a_payment_credited_meanwhile(db: SessionFactory) -> None:
    """A webhook may credit the payment before create() stores the provider's answer."""

    class RacingGateway(FakeGateway):
        async def create(self, payment: Payment, user: User, description: str) -> Invoice:
            await service.credit(db, SETTINGS, payment.id)
            return await super().create(payment, user, description)

    payment = await service.create_payment(db, SETTINGS, RacingGateway(), USER_ID, 15000)
    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.provider_payment_id == f"prov-{payment.id[:8]}"
    assert await _balance(db) == 15000


# ---------------------------------------------------------------------------------------------
# credit
# ---------------------------------------------------------------------------------------------
async def test_credit_credits_wallet_once(db: SessionFactory, core: Core) -> None:
    await _add_payment(db, plan_id=1, is_gift=True, provider_payment_id=None)
    result = await service.credit(db, SETTINGS, "pay-1", provider_payment_id="prov-x")
    assert result == CreditResult(
        payment_id="pay-1",
        user_id=USER_ID,
        amount_kop=15000,
        new_balance_kop=15000,
        plan_id=1,
        is_gift=True,
        is_donation=False,
        inviter=(INVITER_ID, 1500),
        provider="fake",
    )
    assert not result.is_partial
    stored = await _payment(db, "pay-1")
    assert stored.status is PaymentStatus.SUCCEEDED
    assert stored.credited_at is not None
    assert stored.provider_payment_id == "prov-x"
    assert await _ledger(db) == [(USER_ID, 15000, TxKind.PAYMENT, "pay-1")]
    assert core.referral_calls == [(USER_ID, "pay-1", 15000)]
    assert core.event_types() == ["payment.succeeded"]

    assert await service.credit(db, SETTINGS, "pay-1") is None
    assert await _balance(db) == 15000
    assert len(await _ledger(db)) == 1
    assert len(core.referral_calls) == 1


async def test_credit_keeps_existing_provider_id(db: SessionFactory) -> None:
    await _add_payment(db, provider_payment_id="original")
    await service.credit(db, SETTINGS, "pay-1", provider_payment_id="other")
    assert (await _payment(db, "pay-1")).provider_payment_id == "original"


async def test_credit_unknown_payment(db: SessionFactory) -> None:
    assert await service.credit(db, SETTINGS, "nope") is None


async def test_concurrent_credits_apply_exactly_once(db: SessionFactory, core: Core) -> None:
    await _add_payment(db)
    results = await asyncio.gather(*(service.credit(db, SETTINGS, "pay-1") for _ in range(8)))
    assert sum(r is not None for r in results) == 1
    assert await _ledger(db) == [(USER_ID, 15000, TxKind.PAYMENT, "pay-1")]
    assert await _balance(db) == 15000
    assert core.event_types() == ["payment.succeeded"]


async def test_partial_payment_credits_paid_amount(db: SessionFactory, core: Core) -> None:
    await _add_payment(db)
    result = await service.credit(db, SETTINGS, "pay-1", paid_amount_kop=9000)
    assert result is not None
    assert (result.amount_kop, result.expected_amount_kop, result.is_partial) == (9000, 15000, True)
    assert result.new_balance_kop == 9000
    assert (await _payment(db, "pay-1")).amount_kop == 9000
    assert await _ledger(db) == [(USER_ID, 9000, TxKind.PAYMENT, "pay-1")]
    assert core.referral_calls == [(USER_ID, "pay-1", 9000)]
    mismatch = next(e for e in core.events if e[0] == "payment.amount_mismatch")
    assert mismatch[3]["expected_kop"] == 15000 and mismatch[3]["paid_kop"] == 9000


async def test_overpayment_credits_invoice_amount(db: SessionFactory, core: Core) -> None:
    await _add_payment(db)
    result = await service.credit(db, SETTINGS, "pay-1", paid_amount_kop=20000)
    assert result is not None
    assert (result.amount_kop, result.expected_amount_kop, result.is_partial) == (15000, 15000, False)
    assert "payment.amount_mismatch" in core.event_types()
    assert await _balance(db) == 15000


async def test_exact_paid_amount_is_not_a_mismatch(db: SessionFactory, core: Core) -> None:
    await _add_payment(db)
    result = await service.credit(db, SETTINGS, "pay-1", paid_amount_kop=15000)
    assert result is not None and result.expected_amount_kop is None
    assert core.event_types() == ["payment.succeeded"]


async def test_zero_paid_marks_succeeded_without_credit(db: SessionFactory, core: Core) -> None:
    await _add_payment(db)
    result = await service.credit(db, SETTINGS, "pay-1", paid_amount_kop=0)
    assert result is not None and result.amount_kop == 0
    assert await _ledger(db) == []
    assert core.referral_calls == []


async def test_donation_is_not_credited_to_wallet(db: SessionFactory, core: Core) -> None:
    await _add_payment(db, is_donation=True)
    result = await service.credit(db, SETTINGS, "pay-1")
    assert result is not None
    assert result.is_donation and result.inviter is None
    assert result.new_balance_kop == 0
    assert await _ledger(db) == []
    assert core.referral_calls == []
    assert core.event_types() == ["donation.received"]
    assert (await _payment(db, "pay-1")).status is PaymentStatus.SUCCEEDED


@pytest.mark.parametrize("status", [PaymentStatus.CREATED, PaymentStatus.EXPIRED, PaymentStatus.CANCELED])
async def test_late_payment_is_still_credited(db: SessionFactory, status: PaymentStatus) -> None:
    await _add_payment(db, status=status)
    assert await service.credit(db, SETTINGS, "pay-1") is not None
    assert await _balance(db) == 15000


@pytest.mark.parametrize("status", [PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED])
async def test_final_payments_are_not_credited(db: SessionFactory, status: PaymentStatus) -> None:
    await _add_payment(db, status=status)
    assert await service.credit(db, SETTINGS, "pay-1") is None
    assert await _ledger(db) == []


# ---------------------------------------------------------------------------------------------
# mark_status
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("current", "target", "changed"),
    [
        (PaymentStatus.CREATED, PaymentStatus.PENDING, True),
        (PaymentStatus.PENDING, PaymentStatus.PENDING, False),
        (PaymentStatus.CREATED, PaymentStatus.CANCELED, True),
        (PaymentStatus.PENDING, PaymentStatus.EXPIRED, True),
        (PaymentStatus.SUCCEEDED, PaymentStatus.CANCELED, False),
        (PaymentStatus.SUCCEEDED, PaymentStatus.EXPIRED, False),
        (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED, True),
        (PaymentStatus.PENDING, PaymentStatus.REFUNDED, False),
        (PaymentStatus.EXPIRED, PaymentStatus.CANCELED, False),
    ],
)
async def test_mark_status_transitions(
    db: SessionFactory, current: PaymentStatus, target: PaymentStatus, changed: bool
) -> None:
    await _add_payment(db, status=current)
    assert await service.mark_status(db, "pay-1", target) is changed
    assert (await _payment(db, "pay-1")).status is (target if changed else current)


async def test_mark_status_refund_tracks_event_and_rejects_succeeded(db: SessionFactory, core: Core) -> None:
    await _add_payment(db, status=PaymentStatus.SUCCEEDED)
    await service.mark_status(db, "pay-1", PaymentStatus.REFUNDED)
    assert core.event_types() == ["payment.refunded"]
    with pytest.raises(ValueError):
        await service.mark_status(db, "pay-1", PaymentStatus.SUCCEEDED)
    assert await service.mark_status(db, "missing", PaymentStatus.CANCELED) is False


# ---------------------------------------------------------------------------------------------
# handle_webhook
# ---------------------------------------------------------------------------------------------
async def test_webhook_fetches_status_and_credits(db: SessionFactory) -> None:
    await _add_payment(db)
    gw = FakeGateway(
        status=ProviderStatus(PaymentStatus.SUCCEEDED, 15000, "prov-1"),
        webhook=WebhookResult(payment_ids=["pay-1"], provider_ids=["prov-1"]),
    )
    hook = Hook()
    result = await service.handle_webhook(db, SETTINGS, gw, b"{}", {}, "1.2.3.4", hook)
    assert (result.status_code, result.response_body) == (200, "OK")
    assert gw.fetched == ["pay-1"]  # resolved once although both ids match
    assert [r.payment_id for r in hook.results] == ["pay-1"]
    # a redelivery changes nothing
    await service.handle_webhook(db, SETTINGS, gw, b"{}", {}, None, hook)
    assert len(hook.results) == 1
    assert await _balance(db) == 15000


async def test_webhook_resolves_by_provider_id_only(db: SessionFactory) -> None:
    await _add_payment(db)
    gw = FakeGateway(status=ProviderStatus(PaymentStatus.SUCCEEDED), webhook=WebhookResult(provider_ids=["prov-1"]))
    await service.handle_webhook(db, SETTINGS, gw, b"{}", {}, None)
    assert await _balance(db) == 15000


async def test_webhook_never_trusts_body_without_trusted_status(db: SessionFactory) -> None:
    """The provider says PENDING although the webhook claims success: nothing is credited."""
    await _add_payment(db)
    gw = FakeGateway(webhook=WebhookResult(payment_ids=["pay-1"]))
    await service.handle_webhook(db, SETTINGS, gw, b"{}", {}, None)
    assert await _balance(db) == 0
    assert (await _payment(db, "pay-1")).status is PaymentStatus.PENDING


async def test_webhook_trusted_status_skips_fetch(db: SessionFactory) -> None:
    await _add_payment(db, provider_payment_id=None)
    trusted = ProviderStatus(PaymentStatus.SUCCEEDED, 10000, "op-1")
    gw = FakeGateway(
        supports_polling=False,
        webhook=WebhookResult(payment_ids=["pay-1"], response_body="YES", trusted_status=trusted),
    )
    result = await service.handle_webhook(db, SETTINGS, gw, b"x", {}, None)
    assert result.response_body == "YES"
    assert gw.fetched == []
    stored = await _payment(db, "pay-1")
    assert (stored.status, stored.amount_kop, stored.provider_payment_id) == (PaymentStatus.SUCCEEDED, 10000, "op-1")


async def test_webhook_unpollable_without_trusted_status_does_nothing(db: SessionFactory) -> None:
    await _add_payment(db)
    gw = FakeGateway(supports_polling=False, webhook=WebhookResult(payment_ids=["pay-1"]))
    result = await service.handle_webhook(db, SETTINGS, gw, b"x", {}, None)
    assert result.status_code == 200
    assert (await _payment(db, "pay-1")).status is PaymentStatus.PENDING


async def test_webhook_fetch_failure_asks_for_redelivery(db: SessionFactory) -> None:
    await _add_payment(db)
    gw = FakeGateway(fetch_error=GatewayError("timeout"), webhook=WebhookResult(payment_ids=["pay-1"]))
    result = await service.handle_webhook(db, SETTINGS, gw, b"{}", {}, None)
    assert result.status_code == 503
    assert await _balance(db) == 0


async def test_webhook_canceled_status_marks_payment(db: SessionFactory) -> None:
    await _add_payment(db)
    gw = FakeGateway(status=ProviderStatus(PaymentStatus.CANCELED), webhook=WebhookResult(payment_ids=["pay-1"]))
    await service.handle_webhook(db, SETTINGS, gw, b"{}", {}, None)
    assert (await _payment(db, "pay-1")).status is PaymentStatus.CANCELED


async def test_webhook_refund_after_success(db: SessionFactory, core: Core) -> None:
    await _add_payment(db, status=PaymentStatus.SUCCEEDED)
    gw = FakeGateway(status=ProviderStatus(PaymentStatus.REFUNDED), webhook=WebhookResult(payment_ids=["pay-1"]))
    await service.handle_webhook(db, SETTINGS, gw, b"{}", {}, None)
    assert (await _payment(db, "pay-1")).status is PaymentStatus.REFUNDED
    assert "payment.refunded" in core.event_types()


async def test_webhook_ignores_unknown_and_foreign_payments(db: SessionFactory) -> None:
    await _add_payment(db, provider="other")
    gw = FakeGateway(
        status=ProviderStatus(PaymentStatus.SUCCEEDED),
        webhook=WebhookResult(payment_ids=["pay-1", "missing", "x" * 300], provider_ids=["prov-1"]),
    )
    result = await service.handle_webhook(db, SETTINGS, gw, b"{}", {}, None)
    assert result.status_code == 200
    assert gw.fetched == []
    assert await _balance(db) == 0


async def test_webhook_rejection_propagates(db: SessionFactory) -> None:
    with pytest.raises(WebhookRejected):
        await service.handle_webhook(db, SETTINGS, FakeGateway(), b"forged", {}, None)


async def test_webhook_hook_failure_does_not_undo_credit(db: SessionFactory) -> None:
    await _add_payment(db)
    gw = FakeGateway(status=ProviderStatus(PaymentStatus.SUCCEEDED), webhook=WebhookResult(payment_ids=["pay-1"]))
    hook = Hook(fail=True)
    result = await service.handle_webhook(db, SETTINGS, gw, b"{}", {}, None, hook)
    assert result.status_code == 200
    assert len(hook.results) == 1
    assert await _balance(db) == 15000


async def test_webhook_and_reconcile_race_credit_once(db: SessionFactory, core: Core) -> None:
    await _add_payment(db)
    gw = FakeGateway(
        status=ProviderStatus(PaymentStatus.SUCCEEDED, 15000, "prov-1"),
        webhook=WebhookResult(payment_ids=["pay-1"]),
        barrier=asyncio.Barrier(2),  # both learn "succeeded" at the same moment
    )
    hook = Hook()
    _, credited = await asyncio.gather(
        service.handle_webhook(db, SETTINGS, gw, b"{}", {}, None, hook),
        service.reconcile(db, SETTINGS, {"fake": gw}, hook),
    )
    assert len(gw.fetched) == 2
    assert len(hook.results) == 1
    assert credited in (0, 1)
    assert await _ledger(db) == [(USER_ID, 15000, TxKind.PAYMENT, "pay-1")]
    assert await _balance(db) == 15000
    assert core.event_types().count("payment.succeeded") == 1


# ---------------------------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------------------------
async def test_reconcile_credits_and_cancels(db: SessionFactory) -> None:
    await _add_payment(db, pid="paid")
    await _add_payment(db, pid="dead", provider_payment_id="prov-2")

    class PerPayment(FakeGateway):
        async def fetch(self, payment: Payment) -> ProviderStatus:
            self.fetched.append(payment.id)
            if payment.id == "paid":
                return ProviderStatus(PaymentStatus.SUCCEEDED)
            return ProviderStatus(PaymentStatus.CANCELED)

    gw = PerPayment()
    hook = Hook()
    assert await service.reconcile(db, SETTINGS, {"fake": gw}, hook) == 1
    assert sorted(gw.fetched) == ["dead", "paid"]
    assert (await _payment(db, "paid")).status is PaymentStatus.SUCCEEDED
    assert (await _payment(db, "dead")).status is PaymentStatus.CANCELED
    assert [r.payment_id for r in hook.results] == ["paid"]
    assert await service.reconcile(db, SETTINGS, {"fake": gw}, hook) == 0  # nothing pending anymore


async def test_reconcile_expires_stale_payments(db: SessionFactory) -> None:
    old = timedelta(hours=SETTINGS.payment_max_pending_hours, minutes=1)
    await _add_payment(db, pid="old-polled", age=old)  # final check says pending -> expired
    await _add_payment(db, pid="old-unpollable", provider="yoomoney", provider_payment_id=None, age=old)
    await _add_payment(db, pid="old-created", status=PaymentStatus.CREATED, provider_payment_id=None, age=old)
    await _add_payment(db, pid="young", provider="yoomoney", provider_payment_id=None)
    await _add_payment(db, pid="old-done", status=PaymentStatus.SUCCEEDED, provider_payment_id="p9", age=old)
    gw = FakeGateway()
    assert await service.reconcile(db, SETTINGS, {"fake": gw}) == 0
    assert gw.fetched == ["old-polled"]
    for pid in ("old-polled", "old-unpollable", "old-created"):
        assert (await _payment(db, pid)).status is PaymentStatus.EXPIRED, pid
    assert (await _payment(db, "young")).status is PaymentStatus.PENDING
    assert (await _payment(db, "old-done")).status is PaymentStatus.SUCCEEDED


async def test_reconcile_old_payment_paid_at_the_last_moment(db: SessionFactory) -> None:
    await _add_payment(db, age=timedelta(days=3))
    gw = FakeGateway(status=ProviderStatus(PaymentStatus.SUCCEEDED))
    assert await service.reconcile(db, SETTINGS, {"fake": gw}) == 1
    assert (await _payment(db, "pay-1")).status is PaymentStatus.SUCCEEDED


async def test_reconcile_keeps_payments_whose_check_failed(db: SessionFactory) -> None:
    await _add_payment(db, age=timedelta(days=3))
    gw = FakeGateway(fetch_error=GatewayError("down"))
    assert await service.reconcile(db, SETTINGS, {"fake": gw}) == 0
    assert (await _payment(db, "pay-1")).status is PaymentStatus.PENDING


async def test_reconcile_without_pollable_gateways(db: SessionFactory) -> None:
    await _add_payment(db)
    gw = FakeGateway(supports_polling=False)
    assert await service.reconcile(db, SETTINGS, {"fake": gw}) == 0
    assert gw.fetched == []
    assert await service.reconcile(db, SETTINGS, {}) == 0


# ---------------------------------------------------------------------------------------------
# Telegram Stars
# ---------------------------------------------------------------------------------------------
STARS_SETTINGS = make_settings(stars_enabled=True, stars_rub_per_star=1.5)


@pytest.mark.parametrize(
    ("rate", "stars_count", "kop"),
    [(1.5, 100, 15000), (1.37, 3, 411), (1.333, 3, 399), (2, 1, 200), (0.01, 7, 7)],
)
def test_stars_conversions(rate: float, stars_count: int, kop: int) -> None:
    settings = make_settings(stars_rub_per_star=rate)
    assert stars.stars_to_kop(stars_count, settings) == kop
    assert stars.kop_to_stars(stars.stars_to_kop(stars_count, settings), settings) == stars_count


def test_kop_to_stars_rounds_up() -> None:
    assert stars.kop_to_stars(15001, STARS_SETTINGS) == 101
    assert stars.kop_to_stars(1, STARS_SETTINGS) == 1


async def test_stars_gateway() -> None:
    gw = stars.StarsGateway(STARS_SETTINGS)
    assert (gw.key, gw.supports_polling, gw.min_amount_kop) == ("stars", False, 150)
    preset = Payment(id="p", amount_kop=15000, provider_amount="120")
    assert (await gw.create(preset, User(tg_id=1), "x")) == Invoice(None, None, "120", "XTR")
    derived = Payment(id="p", amount_kop=15000, provider_amount=None)
    assert (await gw.create(derived, User(tg_id=1), "x")).provider_amount == "100"
    with pytest.raises(GatewayError):
        await gw.fetch(preset)
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(b"{}", {}, None)
    assert exc.value.status_code == 404
    await gw.close()


async def _stars_payment(sf: SessionFactory, **kw: Any) -> Payment:
    gw = stars.StarsGateway(STARS_SETTINGS)
    return await service.create_payment(sf, STARS_SETTINGS, gw, USER_ID, kw.pop("amount_kop", 15000), **kw)


async def test_stars_plan_payment_uses_explicit_stars_price(db: SessionFactory) -> None:
    payment = await _stars_payment(db, plan_id=1, provider_amount="120")
    assert payment.status is PaymentStatus.PENDING
    assert (payment.provider_amount, payment.provider_currency, payment.confirmation_url) == ("120", "XTR", None)


@pytest.mark.parametrize(
    ("payload", "amount", "user_id", "currency", "ok"),
    [
        (None, 100, USER_ID, "XTR", True),
        (None, 99, USER_ID, "XTR", False),  # amount must match
        (None, 101, USER_ID, "XTR", False),
        (None, 100, 42, "XTR", False),  # user must match
        (None, 100, USER_ID, "RUB", False),
        ("unknown-payload", 100, USER_ID, "XTR", False),
        ("", 100, USER_ID, "XTR", False),
        ("x" * 200, 100, USER_ID, "XTR", False),
    ],
)
async def test_validate_pre_checkout(
    db: SessionFactory, payload: str | None, amount: int, user_id: int, currency: str, ok: bool
) -> None:
    payment = await _stars_payment(db)  # 150 RUB at 1.5 RUB/Star -> 100 Stars
    result = await stars.validate_pre_checkout(
        db, payment.id if payload is None else payload, amount, user_id, currency=currency
    )
    assert result is ok


async def test_validate_pre_checkout_rejects_non_stars_and_closed(db: SessionFactory) -> None:
    await _add_payment(db, pid="card", provider="fake", provider_amount="100")
    assert not await stars.validate_pre_checkout(db, "card", 100, USER_ID)
    payment = await _stars_payment(db)
    await service.credit(db, STARS_SETTINGS, payment.id)
    assert not await stars.validate_pre_checkout(db, payment.id, 100, USER_ID)
    expired = await _stars_payment(db)
    await service.mark_status(db, expired.id, PaymentStatus.EXPIRED)
    assert not await stars.validate_pre_checkout(db, expired.id, 100, USER_ID)


async def test_validate_pre_checkout_without_stored_stars(db: SessionFactory) -> None:
    await _add_payment(db, pid="raw", provider="stars", provider_amount=None, amount_kop=15000)
    assert not await stars.validate_pre_checkout(db, "raw", 100, USER_ID)
    assert await stars.validate_pre_checkout(db, "raw", 100, USER_ID, settings=STARS_SETTINGS)


async def test_stars_successful_payment_is_deduplicated(db: SessionFactory) -> None:
    payment = await _stars_payment(db)
    kwargs: dict[str, Any] = {"payload": payment.id, "user_id": USER_ID, "total_amount": 100, "charge_id": "ch-1"}
    result = await stars.credit_successful_payment(db, STARS_SETTINGS, **kwargs)
    assert result is not None and result.amount_kop == 15000
    assert (await _payment(db, payment.id)).provider_payment_id == "ch-1"
    assert await stars.credit_successful_payment(db, STARS_SETTINGS, **kwargs) is None
    assert await _balance(db) == 15000


async def test_stars_successful_payment_rejections(db: SessionFactory) -> None:
    payment = await _stars_payment(db)
    base: dict[str, Any] = {"payload": payment.id, "user_id": USER_ID, "total_amount": 100, "charge_id": "ch"}
    assert await stars.credit_successful_payment(db, STARS_SETTINGS, **{**base, "user_id": 42}) is None
    assert await stars.credit_successful_payment(db, STARS_SETTINGS, **{**base, "currency": "RUB"}) is None
    assert await stars.credit_successful_payment(db, STARS_SETTINGS, **{**base, "charge_id": ""}) is None
    assert await stars.credit_successful_payment(db, STARS_SETTINGS, **{**base, "payload": "nope"}) is None
    await _add_payment(db, pid="card", provider="fake")
    assert await stars.credit_successful_payment(db, STARS_SETTINGS, **{**base, "payload": "card"}) is None
    assert await _balance(db) == 0


async def test_stars_amount_mismatch_credits_paid_stars(db: SessionFactory) -> None:
    payment = await _stars_payment(db)  # 100 Stars expected
    result = await stars.credit_successful_payment(
        db, STARS_SETTINGS, payload=payment.id, user_id=USER_ID, total_amount=50, charge_id="ch"
    )
    assert result is not None and result.is_partial and result.amount_kop == 7500


async def test_stars_subscription_renewals(db: SessionFactory) -> None:
    payment = await _stars_payment(db, plan_id=1, provider_amount="120", is_recurring=True)
    common: dict[str, Any] = {"payload": payment.id, "user_id": USER_ID, "total_amount": 120, "is_recurring": True}
    first = await stars.credit_successful_payment(
        db, STARS_SETTINGS, **common, charge_id="ch-1", is_first_recurring=True
    )
    assert first is not None and first.payment_id == payment.id and first.plan_id == 1

    renewal = await stars.credit_successful_payment(db, STARS_SETTINGS, **common, charge_id="ch-2")
    assert renewal is not None
    assert renewal.payment_id != payment.id and renewal.plan_id == 1 and renewal.amount_kop == 15000
    stored = await _payment(db, renewal.payment_id)
    assert (stored.provider, stored.provider_payment_id, stored.is_recurring) == ("stars", "ch-2", True)

    # redelivered renewal: no second credit, no second row
    assert await stars.credit_successful_payment(db, STARS_SETTINGS, **common, charge_id="ch-2") is None
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(Payment)) == 2
    assert await _balance(db) == 30000


async def test_stars_renewal_recorded_but_not_credited_is_recovered(db: SessionFactory) -> None:
    payment = await _stars_payment(db, is_recurring=True)
    await _add_payment(db, pid="renewal", provider="stars", provider_payment_id="ch-9", is_recurring=True)
    result = await stars.credit_successful_payment(
        db,
        STARS_SETTINGS,
        payload=payment.id,
        user_id=USER_ID,
        total_amount=100,
        charge_id="ch-9",
        is_recurring=True,
    )
    assert result is not None and result.payment_id == "renewal"


def _plan() -> Plan:
    return Plan(id=1, title="1 месяц", duration_days=30, price_kop=15000)


@pytest.mark.parametrize(
    ("flags", "with_plan", "title", "in_description"),
    [
        ({}, False, "Пополнение баланса", "150 ₽"),
        ({}, True, "TestVPN: 1 месяц", "30 дн."),
        ({"is_gift": True}, True, "Подарок: 1 месяц", "Подарочный код"),
        ({"is_donation": True}, False, "Поддержать TestVPN", "пожертвование"),
    ],
)
def test_build_invoice_params(flags: dict[str, bool], with_plan: bool, title: str, in_description: str) -> None:
    payment = Payment(id="pid-1", amount_kop=15000, provider_amount="100", is_recurring=False, **flags)
    params = stars.build_invoice_params(payment, _plan() if with_plan else None, STARS_SETTINGS)
    assert params["title"] == title
    assert in_description in params["description"]
    assert (params["payload"], params["currency"], params["provider_token"]) == ("pid-1", "XTR", "")
    assert [(p.label, p.amount) for p in params["prices"]] == [(title, 100)]
    assert "subscription_period" not in params


def test_build_invoice_params_limits() -> None:
    long_plan = Plan(id=1, title="Очень длинное название тарифа на целый год", duration_days=365, price_kop=1)
    payment = Payment(id="p", amount_kop=15000, provider_amount="100", is_recurring=True, is_gift=False)
    params = stars.build_invoice_params(payment, long_plan, STARS_SETTINGS)
    assert len(params["title"]) <= 32 and 1 <= len(params["description"]) <= 255
    assert params["subscription_period"] == 2_592_000
    with pytest.raises(ValueError):
        stars.build_invoice_params(
            Payment(id="p", amount_kop=1, provider_amount="10001", is_recurring=True), None, STARS_SETTINGS
        )


# ---------------------------------------------------------------------------------------------
# Regressions
# ---------------------------------------------------------------------------------------------
class _PaidFreeKassa(FreeKassaGateway):
    """The real gateway with the API call replaced: every order is paid."""

    def __init__(self, **kw: Any) -> None:
        super().__init__("7012", "s1", "s2", **kw)
        self.fetched: list[str] = []

    async def fetch(self, payment: Payment) -> ProviderStatus:
        self.fetched.append(payment.id)
        return ProviderStatus(PaymentStatus.SUCCEEDED, payment.amount_kop, "fk-555")


async def test_reconcile_polls_freekassa_payments_without_provider_id(db: SessionFactory) -> None:
    gw = _PaidFreeKassa(api_key="k")
    payment = await service.create_payment(db, SETTINGS, gw, USER_ID, 15000)
    assert payment.provider_payment_id is None
    assert await service.reconcile(db, SETTINGS, {gw.key: gw}) == 1
    assert gw.fetched == [payment.id]
    stored = await _payment(db, payment.id)
    assert (stored.status, stored.provider_payment_id) == (PaymentStatus.SUCCEEDED, "fk-555")
    assert await _balance(db) == 15000
    await gw.close()


async def test_reconcile_still_skips_unidentified_payments_of_other_providers(db: SessionFactory) -> None:
    await _add_payment(db, pid="no-id", provider_payment_id=None)
    gw = FakeGateway()
    assert await service.reconcile(db, SETTINGS, {"fake": gw}) == 0
    assert gw.fetched == []


async def _subscription(sf: SessionFactory, **kw: Any) -> None:
    async with sf.begin() as s:
        s.add(
            Subscription(
                user_id=USER_ID,
                plan_id=1,
                client_uuid="11111111-1111-1111-1111-111111111111",
                status=SubscriptionStatus.ACTIVE,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
                **kw,
            )
        )


async def _stars_charge_id(sf: SessionFactory) -> str | None:
    async with sf() as s:
        return await s.scalar(select(Subscription.stars_charge_id).where(Subscription.user_id == USER_ID))


async def test_stars_recurring_payment_links_subscription_created_by_hook(db: SessionFactory) -> None:
    payment = await _stars_payment(db, plan_id=1, provider_amount="120", is_recurring=True)
    seen: list[CreditResult] = []

    async def buy_plan(result: CreditResult) -> None:  # the post-payment hook buys the plan
        seen.append(result)
        await _subscription(db)

    common: dict[str, Any] = {"payload": payment.id, "user_id": USER_ID, "total_amount": 120, "is_recurring": True}
    first = await stars.credit_successful_payment(
        db, STARS_SETTINGS, **common, charge_id="ch-1", is_first_recurring=True, on_credited=buy_plan
    )
    assert first is not None and first.recurring_charge_id == "ch-1"
    assert seen == [first]
    assert await _stars_charge_id(db) == "ch-1"
    async with db() as s:
        due = await subscriptions.due_for_auto_renew(s, STARS_SETTINGS, datetime.now(UTC))
    assert due == []

    # a renewal keeps the subscription linked to the original charge
    renewal = await stars.credit_successful_payment(db, STARS_SETTINGS, **common, charge_id="ch-2")
    assert renewal is not None and renewal.recurring_charge_id == "ch-1"
    assert await _stars_charge_id(db) == "ch-1"

    await stars.unlink_recurring(db, USER_ID)
    assert await _stars_charge_id(db) is None


async def test_stars_renewal_relinks_existing_subscription(db: SessionFactory) -> None:
    payment = await _stars_payment(db, plan_id=1, provider_amount="120", is_recurring=True)
    await _subscription(db)
    result = await stars.credit_successful_payment(
        db,
        STARS_SETTINGS,
        payload=payment.id,
        user_id=USER_ID,
        total_amount=120,
        charge_id="ch-1",
        is_recurring=True,
        is_first_recurring=True,
    )
    assert result is not None
    assert await _stars_charge_id(db) == "ch-1"


async def test_stars_second_subscription_declined_while_one_is_linked(db: SessionFactory) -> None:
    """A second Stars subscription would keep charging after the linked one is canceled."""
    await _subscription(db, stars_charge_id="ch-old")
    recurring = await _stars_payment(db, plan_id=1, provider_amount="120", is_recurring=True)
    assert not await stars.validate_pre_checkout(db, recurring.id, 120, USER_ID)
    one_off = await _stars_payment(db, plan_id=1, provider_amount="120")
    assert await stars.validate_pre_checkout(db, one_off.id, 120, USER_ID)


async def test_stars_new_subscription_allowed_once_the_linked_one_lapsed(db: SessionFactory) -> None:
    await _subscription(db, stars_charge_id="ch-old")
    async with db.begin() as s:
        await s.execute(update(Subscription).values(expires_at=datetime.now(UTC) - timedelta(days=1)))
    recurring = await _stars_payment(db, plan_id=1, provider_amount="120", is_recurring=True)
    assert await stars.validate_pre_checkout(db, recurring.id, 120, USER_ID)


async def test_stars_new_subscription_replaces_and_cancels_the_linked_one(db: SessionFactory) -> None:
    await _subscription(db, stars_charge_id="ch-old")
    payment = await _stars_payment(db, plan_id=1, provider_amount="120", is_recurring=True)
    canceled: list[tuple[int, str]] = []

    async def cancel(user_id: int, charge_id: str) -> None:
        canceled.append((user_id, charge_id))

    common: dict[str, Any] = {"payload": payment.id, "user_id": USER_ID, "total_amount": 120, "is_recurring": True}
    result = await stars.credit_successful_payment(
        db, STARS_SETTINGS, **common, charge_id="ch-new", is_first_recurring=True, cancel_charge=cancel
    )
    assert result is not None
    assert await _stars_charge_id(db) == "ch-new"
    assert canceled == [(USER_ID, "ch-old")]


async def test_stars_stray_renewal_does_not_steal_the_link(db: SessionFactory) -> None:
    """A renewal of a subscription other than the linked one is credited, then canceled, never re-linked."""
    old = await _stars_payment(db, plan_id=1, provider_amount="120", is_recurring=True)
    common: dict[str, Any] = {"payload": old.id, "user_id": USER_ID, "total_amount": 120, "is_recurring": True}
    await _subscription(db)
    first = await stars.credit_successful_payment(
        db, STARS_SETTINGS, **common, charge_id="ch-1", is_first_recurring=True
    )
    assert first is not None
    async with db.begin() as s:
        await s.execute(update(Subscription).values(stars_charge_id="ch-other"))
    canceled: list[tuple[int, str]] = []

    async def cancel(user_id: int, charge_id: str) -> None:
        canceled.append((user_id, charge_id))

    renewal = await stars.credit_successful_payment(
        db, STARS_SETTINGS, **common, charge_id="ch-2", cancel_charge=cancel
    )
    assert renewal is not None and renewal.recurring_charge_id == "ch-1"
    assert await _stars_charge_id(db) == "ch-other"
    assert canceled == [(USER_ID, "ch-1")]


async def test_stars_one_off_payment_does_not_link(db: SessionFactory) -> None:
    payment = await _stars_payment(db, plan_id=1, provider_amount="120")
    await _subscription(db)
    result = await stars.credit_successful_payment(
        db, STARS_SETTINGS, payload=payment.id, user_id=USER_ID, total_amount=120, charge_id="ch-1"
    )
    assert result is not None and result.recurring_charge_id is None
    assert await _stars_charge_id(db) is None


# ---------------------------------------------------------------------------------------------
# Repeat payments through a reusable link (YooMoney quickpay / FreeKassa SCI) or Stars invoice
# ---------------------------------------------------------------------------------------------
YM_SECRET = "ym-secret"


def _ym_notification(label: str, operation_id: str, amount: str = "500.00", *, unaccepted: bool = False) -> bytes:
    fields = {
        "notification_type": "card-incoming",
        "operation_id": operation_id,
        "amount": amount,
        "withdraw_amount": amount,
        "currency": "643",
        "datetime": "2026-11-04T10:00:00Z",
        "sender": "",
        "codepro": "false",
        "label": label,
        "unaccepted": "true" if unaccepted else "false",
    }
    fields["sign"] = yoomoney.notification_sign(fields, YM_SECRET)
    return urlencode(fields).encode()


async def _payments_of(sf: SessionFactory, provider: str) -> list[Payment]:
    async with sf() as s:
        return list((await s.scalars(select(Payment).where(Payment.provider == provider))).all())


async def test_yoomoney_second_payment_through_same_link_is_recorded_and_credited(
    db: SessionFactory, core: Core
) -> None:
    gw = YooMoneyGateway("4100111", YM_SECRET)
    payment = await service.create_payment(db, SETTINGS, gw, USER_ID, 50000, plan_id=1)
    hook = Hook()

    first = await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-1"), {}, None, hook)
    assert first.status_code == 200
    assert await _balance(db) == 50000

    # the user taps the old "Pay" button again: a new transfer with the same label
    second = await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-2"), {}, None, hook)
    assert second.status_code == 200
    assert await _balance(db) == 100000
    rows = {p.provider_payment_id: p for p in await _payments_of(db, gw.key)}
    assert set(rows) == {"op-1", "op-2"}
    repeat = rows["op-2"]
    assert repeat.id != payment.id and repeat.status is PaymentStatus.SUCCEEDED
    # a repeat transfer is a wallet top-up: the plan is not bought a second time behind the user's back
    assert (repeat.user_id, repeat.amount_kop, repeat.plan_id, repeat.is_gift) == (USER_ID, 50000, None, False)
    assert [r.payment_id for r in hook.results] == [payment.id, repeat.id]
    assert hook.results[1].plan_id is None
    assert "payment.repeat" in core.event_types()

    # redeliveries of either notification change nothing
    for op in ("op-1", "op-2"):
        await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, op), {}, None, hook)
    assert await _balance(db) == 100000
    assert len(await _payments_of(db, gw.key)) == 2
    assert len(hook.results) == 2


async def test_yoomoney_concurrent_repeat_notifications_credit_once(db: SessionFactory) -> None:
    gw = YooMoneyGateway("4100111", YM_SECRET)
    payment = await service.create_payment(db, SETTINGS, gw, USER_ID, 50000)
    await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-1"), {}, None)
    raw = _ym_notification(payment.id, "op-2")
    await asyncio.gather(*(service.handle_webhook(db, SETTINGS, gw, raw, {}, None) for _ in range(3)))
    assert await _balance(db) == 100000
    assert len(await _payments_of(db, gw.key)) == 2


async def test_freekassa_second_payment_for_same_order_is_flagged(db: SessionFactory, core: Core) -> None:
    gw = FreeKassaGateway("7012", "s1", "s2", check_ip=False)
    payment = await service.create_payment(db, SETTINGS, gw, USER_ID, 15000)

    def notification(intid: str) -> bytes:
        fields = {
            "MERCHANT_ID": "7012",
            "AMOUNT": "150.00",
            "intid": intid,
            "MERCHANT_ORDER_ID": payment.id,
            "SIGN": freekassa.notification_sign("7012", "150.00", "s2", payment.id),
        }
        return urlencode(fields).encode()

    await service.handle_webhook(db, SETTINGS, gw, notification("111"), {}, None)
    await service.handle_webhook(db, SETTINGS, gw, notification("111"), {}, None)  # redelivery: silent
    assert "payment.repeat_unrecorded" not in core.event_types()
    result = await service.handle_webhook(db, SETTINGS, gw, notification("222"), {}, None)
    assert result.response_body == "YES"
    # FreeKassa's polled order id may differ from the notification's intid, so a repeat is not auto-credited
    # (that could double-credit) but it is surfaced to the admin instead of being dropped silently.
    unrecorded = [e for e in core.events if e[0] == "payment.repeat_unrecorded"]
    assert len(unrecorded) == 1
    assert unrecorded[0][1:3] == (USER_ID, 15000)
    assert unrecorded[0][3]["provider_payment_id"] == "222"
    assert await _balance(db) == 15000
    await gw.close()


async def test_stars_second_charge_on_same_invoice_is_recorded(db: SessionFactory, core: Core) -> None:
    """Two clients confirm the same invoice: both pre-checkouts pass, both charges must be accounted for."""
    payment = await _stars_payment(db, plan_id=1, provider_amount="120")
    assert await stars.validate_pre_checkout(db, payment.id, 120, USER_ID)
    assert await stars.validate_pre_checkout(db, payment.id, 120, USER_ID)
    common: dict[str, Any] = {"payload": payment.id, "user_id": USER_ID, "total_amount": 120}

    first = await stars.credit_successful_payment(db, STARS_SETTINGS, **common, charge_id="ch-A")
    assert first is not None and first.payment_id == payment.id and first.plan_id == 1

    second = await stars.credit_successful_payment(db, STARS_SETTINGS, **common, charge_id="ch-B")
    assert second is not None
    assert second.payment_id != payment.id
    assert (second.plan_id, second.is_gift, second.amount_kop) == (None, False, 15000)  # wallet top-up
    stored = await _payment(db, second.payment_id)
    assert (stored.provider_payment_id, stored.status) == ("ch-B", PaymentStatus.SUCCEEDED)
    assert "payment.repeat" in core.event_types()
    assert await _balance(db) == 30000

    # redeliveries of either charge are still no-ops
    assert await stars.credit_successful_payment(db, STARS_SETTINGS, **common, charge_id="ch-A") is None
    assert await stars.credit_successful_payment(db, STARS_SETTINGS, **common, charge_id="ch-B") is None
    assert len(await _payments_of(db, "stars")) == 2
    assert await _balance(db) == 30000


async def test_stars_concurrent_charges_on_same_invoice_are_both_credited(db: SessionFactory) -> None:
    payment = await _stars_payment(db)
    common: dict[str, Any] = {"payload": payment.id, "user_id": USER_ID, "total_amount": 100}
    results = await asyncio.gather(
        stars.credit_successful_payment(db, STARS_SETTINGS, **common, charge_id="ch-A"),
        stars.credit_successful_payment(db, STARS_SETTINGS, **common, charge_id="ch-B"),
    )
    assert all(r is not None for r in results)
    assert await _balance(db) == 30000
    assert {p.provider_payment_id for p in await _payments_of(db, "stars")} == {"ch-A", "ch-B"}


# ---------------------------------------------------------------------------------------------
# Reconciler under a backlog of open invoices
# ---------------------------------------------------------------------------------------------
async def _bulk_pending(sf: SessionFactory, count: int, *, age: timedelta, prefix: str) -> None:
    """``count`` pending payments; ``{prefix}-0000`` is the youngest."""
    now = datetime.now(UTC)
    async with sf.begin() as s:
        for i in range(count):
            s.add(
                Payment(
                    id=f"{prefix}-{i:04d}",
                    provider="fake",
                    provider_payment_id=f"{prefix}-prov-{i:04d}",
                    user_id=USER_ID,
                    amount_kop=15000,
                    status=PaymentStatus.PENDING,
                    created_at=now - age - timedelta(seconds=i),
                )
            )


@dataclass
class _PaidOnly(FakeGateway):
    """Reports ``paid_ids`` as paid and everything else as still pending."""

    paid_ids: frozenset[str] = frozenset()

    async def fetch(self, payment: Payment) -> ProviderStatus:
        self.fetched.append(payment.id)
        if payment.id in self.paid_ids:
            return ProviderStatus(PaymentStatus.SUCCEEDED)
        return ProviderStatus(PaymentStatus.PENDING)


async def test_reconcile_polls_fresh_payments_despite_a_backlog(
    db: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "RECONCILE_BATCH", 10)
    await _bulk_pending(db, 30, age=timedelta(hours=10), prefix="abandoned")
    await _add_payment(db, pid="fresh", provider_payment_id="prov-fresh", age=timedelta(minutes=1))
    gw = _PaidOnly(paid_ids=frozenset({"fresh"}))
    assert await service.reconcile(db, SETTINGS, {"fake": gw}) == 1
    assert "fresh" in gw.fetched
    assert len(gw.fetched) <= 10
    assert (await _payment(db, "fresh")).status is PaymentStatus.SUCCEEDED


async def test_reconcile_never_expires_payments_it_did_not_check(
    db: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "RECONCILE_BATCH", 10)
    overdue = timedelta(hours=SETTINGS.payment_max_pending_hours, minutes=5)
    await _bulk_pending(db, 25, age=overdue, prefix="old")
    paid = "old-0000"  # the youngest of the overdue ones: polled last
    gw = _PaidOnly(paid_ids=frozenset({paid}))

    for _ in range(10):
        before = len(set(gw.fetched))
        await service.reconcile(db, SETTINGS, {"fake": gw})
        async with db() as s:
            rows = list((await s.scalars(select(Payment))).all())
        for row in rows:
            if row.status is PaymentStatus.EXPIRED:
                assert row.id in gw.fetched, f"{row.id} expired without a final check"
        if all(r.status is not PaymentStatus.PENDING for r in rows):
            break
        assert len(set(gw.fetched)) > before  # the backlog drains every round
    else:
        pytest.fail("the overdue backlog never drained")
    assert set(gw.fetched) == {f"old-{i:04d}" for i in range(25)}
    assert (await _payment(db, paid)).status is PaymentStatus.SUCCEEDED


# ---------------------------------------------------------------------------------------------
# YooMoney held ("unaccepted") transfers
# ---------------------------------------------------------------------------------------------
def _held_events(core: Core) -> list[tuple[str, int | None, int | None, dict[str, Any]]]:
    return [e for e in core.events if e[0] == "payment.held"]


async def _age(sf: SessionFactory, pid: str, age: timedelta) -> None:
    async with sf.begin() as s:
        await s.execute(update(Payment).where(Payment.id == pid).values(created_at=datetime.now(UTC) - age))


async def test_yoomoney_held_transfer_is_recorded_and_never_expires_unseen(db: SessionFactory, core: Core) -> None:
    """A signed "unaccepted" notification means the money arrived but is held: it must leave a durable trace."""
    gw = YooMoneyGateway("4100111", YM_SECRET)
    payment = await service.create_payment(db, SETTINGS, gw, USER_ID, 50000, plan_id=1)
    held = _ym_notification(payment.id, "op-held", unaccepted=True)

    result = await service.handle_webhook(db, SETTINGS, gw, held, {}, None)
    assert result.status_code == 200
    stored = await _payment(db, payment.id)
    assert (stored.status, stored.provider_payment_id) == (PaymentStatus.PENDING, "op-held")
    events_ = _held_events(core)
    assert len(events_) == 1
    assert events_[0][1:3] == (USER_ID, 50000)
    assert events_[0][3]["payment_id"] == payment.id and events_[0][3]["provider_payment_id"] == "op-held"
    assert await _balance(db) == 0

    # YooMoney retries the notification (+10 min, +1 h): no duplicate trace
    await service.handle_webhook(db, SETTINGS, gw, held, {}, None)
    assert len(_held_events(core)) == 1

    # the reconciler cannot poll YooMoney, but must not silently expire a payment whose money is held
    await _age(db, payment.id, timedelta(hours=SETTINGS.payment_max_pending_hours, minutes=5))
    await service.reconcile(db, SETTINGS, {gw.key: gw})
    assert (await _payment(db, payment.id)).status is PaymentStatus.PENDING

    # the receiver accepts the transfer: a notification for the same operation credits it once
    hook = Hook()
    await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-held"), {}, None, hook)
    assert (await _payment(db, payment.id)).status is PaymentStatus.SUCCEEDED
    assert await _balance(db) == 50000
    assert [r.payment_id for r in hook.results] == [payment.id] and hook.results[0].plan_id == 1


async def test_yoomoney_held_transfer_can_be_credited_by_an_admin(db: SessionFactory, core: Core) -> None:
    gw = YooMoneyGateway("4100111", YM_SECRET)
    payment = await service.create_payment(db, SETTINGS, gw, USER_ID, 50000)
    await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-h", unaccepted=True), {}, None)
    assert await service.credit(db, SETTINGS, payment.id) is not None
    assert await _balance(db) == 50000
    # a later "accepted" notification for the same operation is a redelivery
    await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-h"), {}, None)
    assert await _balance(db) == 50000
    assert len(await _payments_of(db, gw.key)) == 1


async def test_yoomoney_held_second_transfer_on_paid_link_is_recorded(db: SessionFactory, core: Core) -> None:
    gw = YooMoneyGateway("4100111", YM_SECRET)
    payment = await service.create_payment(db, SETTINGS, gw, USER_ID, 50000)
    await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-1"), {}, None)
    assert await _balance(db) == 50000

    held = _ym_notification(payment.id, "op-2", unaccepted=True)
    await service.handle_webhook(db, SETTINGS, gw, held, {}, None)
    await service.handle_webhook(db, SETTINGS, gw, held, {}, None)  # retry
    rows = {p.provider_payment_id: p for p in await _payments_of(db, gw.key)}
    assert set(rows) == {"op-1", "op-2"}
    repeat = rows["op-2"]
    assert repeat.status is PaymentStatus.PENDING and repeat.amount_kop == 50000
    events_ = _held_events(core)
    assert len(events_) == 1 and events_[0][3]["payment_id"] == repeat.id
    assert events_[0][3]["provider_payment_id"] == "op-2"
    assert await _balance(db) == 50000

    await _age(db, repeat.id, timedelta(hours=SETTINGS.payment_max_pending_hours, minutes=5))
    await service.reconcile(db, SETTINGS, {gw.key: gw})
    assert (await _payment(db, repeat.id)).status is PaymentStatus.PENDING

    await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-2"), {}, None)  # accepted
    assert (await _payment(db, repeat.id)).status is PaymentStatus.SUCCEEDED
    assert await _balance(db) == 100000


async def test_yoomoney_paid_transfer_after_a_held_one_keeps_both(db: SessionFactory, core: Core) -> None:
    """A second, unheld transfer must not be credited under the held one's row (that would hide the held one)."""
    gw = YooMoneyGateway("4100111", YM_SECRET)
    payment = await service.create_payment(db, SETTINGS, gw, USER_ID, 50000)
    await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-h", unaccepted=True), {}, None)
    await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-2"), {}, None)
    assert await _balance(db) == 50000
    await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-2"), {}, None)  # retry
    assert await _balance(db) == 50000
    await service.handle_webhook(db, SETTINGS, gw, _ym_notification(payment.id, "op-h"), {}, None)  # accepted
    assert await _balance(db) == 100000
    rows = {p.provider_payment_id: p.status for p in await _payments_of(db, gw.key)}
    assert rows == {"op-h": PaymentStatus.SUCCEEDED, "op-2": PaymentStatus.SUCCEEDED}


async def test_unpollable_payments_without_held_transfer_still_expire(db: SessionFactory) -> None:
    gw = YooMoneyGateway("4100111", YM_SECRET)
    old = timedelta(hours=SETTINGS.payment_max_pending_hours, minutes=1)
    await _add_payment(db, pid="plain", provider=gw.key, provider_payment_id=None, age=old)
    await service.reconcile(db, SETTINGS, {gw.key: gw})
    assert (await _payment(db, "plain")).status is PaymentStatus.EXPIRED


# ---------------------------------------------------------------------------------------------
# FreeKassa GET notifications
# ---------------------------------------------------------------------------------------------
async def test_freekassa_get_notification_is_credited(db: SessionFactory) -> None:
    gw = FreeKassaGateway("7012", "s1", "s2", check_ip=False)
    payment = await service.create_payment(db, SETTINGS, gw, USER_ID, 15000)
    query = urlencode(
        {
            "MERCHANT_ID": "7012",
            "AMOUNT": "150.00",
            "intid": "111",
            "MERCHANT_ORDER_ID": payment.id,
            "SIGN": freekassa.notification_sign("7012", "150.00", "s2", payment.id),
        }
    )
    result = await service.handle_webhook(db, SETTINGS, gw, b"", {}, None, query=query)
    assert (result.status_code, result.response_body) == (200, "YES")
    assert await _balance(db) == 15000
    await gw.close()


async def test_webhook_query_is_ignored_for_post_only_gateways(db: SessionFactory) -> None:
    seen: list[bytes] = []

    class Spy(FakeGateway):
        async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
            seen.append(raw)
            return self.webhook

    await service.handle_webhook(db, SETTINGS, Spy(), b"", {}, None, query="a=1")
    assert seen == [b""]


# ---------------------------------------------------------------------------------------------
# Reconciler: one payment's failure does not abort the run
# ---------------------------------------------------------------------------------------------
async def test_reconcile_isolates_a_failing_payment(db: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    old = timedelta(hours=SETTINGS.payment_max_pending_hours, minutes=1)
    await _add_payment(db, pid="locked", provider_payment_id="prov-locked")
    await _add_payment(db, pid="garbled", provider_payment_id="prov-garbled")
    await _add_payment(db, pid="paid", provider_payment_id="prov-paid")
    await _add_payment(db, pid="stale", provider="yoomoney", provider_payment_id=None, age=old)

    class Mixed(FakeGateway):
        async def fetch(self, payment: Payment) -> ProviderStatus:
            self.fetched.append(payment.id)
            if payment.id == "garbled":
                raise UnicodeDecodeError("utf-8", b"\xc4", 0, 1, "invalid continuation byte")
            return ProviderStatus(PaymentStatus.SUCCEEDED)

    real_credit = service.credit

    async def flaky_credit(sf: SessionFactory, settings: Settings, payment_id: str, **kw: Any) -> CreditResult | None:
        if payment_id == "locked":
            raise OperationalError("UPDATE payments", {}, Exception("database is locked"))
        return await real_credit(sf, settings, payment_id, **kw)

    monkeypatch.setattr(service, "credit", flaky_credit)
    gw = Mixed()
    assert await service.reconcile(db, SETTINGS, {"fake": gw}) == 1
    assert sorted(gw.fetched) == ["garbled", "locked", "paid"]
    assert (await _payment(db, "paid")).status is PaymentStatus.SUCCEEDED
    assert (await _payment(db, "stale")).status is PaymentStatus.EXPIRED  # expiry still ran
    for pid in ("locked", "garbled"):  # retried on the next run
        assert (await _payment(db, pid)).status is PaymentStatus.PENDING


async def test_reconcile_failed_final_check_does_not_expire(db: SessionFactory) -> None:
    old = timedelta(hours=SETTINGS.payment_max_pending_hours, minutes=1)
    await _add_payment(db, pid="old", age=old)
    gw = FakeGateway(fetch_error=RuntimeError("bug"))
    assert await service.reconcile(db, SETTINGS, {"fake": gw}) == 0
    assert (await _payment(db, "old")).status is PaymentStatus.PENDING


async def test_bot_cancels_unlinked_stars_subscription() -> None:
    from types import SimpleNamespace

    from aiogram.exceptions import TelegramBadRequest

    from app.modules.payments import confirm

    calls: list[dict[str, Any]] = []

    async def edit_user_star_subscription(**params: Any) -> bool:
        calls.append(params)
        if params["telegram_payment_charge_id"] == "gone":
            raise TelegramBadRequest(method=None, message="SUBSCRIPTION_NOT_FOUND")  # type: ignore[arg-type]
        return True

    ctx: Any = SimpleNamespace(bot=SimpleNamespace(edit_user_star_subscription=edit_user_star_subscription))
    cancel = confirm._charge_canceler(ctx)
    await cancel(USER_ID, "ch-old")
    await cancel(USER_ID, "gone")
    assert calls == [
        {"user_id": USER_ID, "telegram_payment_charge_id": "ch-old", "is_canceled": True},
        {"user_id": USER_ID, "telegram_payment_charge_id": "gone", "is_canceled": True},
    ]
