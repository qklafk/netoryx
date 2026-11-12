"""CloudPayments gateway, saved cards and auto-renewal charged to a saved card."""
# ruff: noqa: F811  (pytest fixtures imported from the gateway tests)

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote_plus, urlencode

import pytest
from sqlalchemy import func, select

from app.db.models import (
    BalanceTransaction,
    Payment,
    PaymentStatus,
    Purchase,
    PurchaseKind,
    SavedPaymentMethod,
    Subscription,
    User,
)
from app.db.session import SessionFactory
from app.modules.payments import flow
from app.modules.payments.callbacks import Purpose
from app.services import hooks, notifications, provisioning, saved_cards, subscriptions
from app.services.payments import autopay, service
from app.services.payments.base import (
    ChargeResult,
    GatewayError,
    Invoice,
    PaymentGateway,
    ProviderStatus,
    SavedMethod,
    WebhookRejected,
)
from app.services.payments.cloudpayments import CloudPaymentsGateway, body_signature
from app.services.payments.registry import build_gateways
from app.services.payments.yookassa import YooKassaGateway
from tests.conftest import make_settings
from tests.test_middlewares_support import make_ctx
from tests.test_payments_gateways import (  # noqa: F401  (fixtures)
    PAYMENT_ID,
    FakeProvider,
    _no_retry_delay,
    closing,
    fake,
    make_payment,
    make_user,
)
from tests.test_subs_support import add_plan, add_sub, add_user

PUBLIC_ID = "pk_test_public"
SECRET = "api-secret-123"
TOKEN = "tk_0123456789abcdef"
USER_ID = 5512345678


def _cp(fake: FakeProvider, closing: list[PaymentGateway]) -> CloudPaymentsGateway:
    gw = _no_retry_delay(CloudPaymentsGateway(PUBLIC_ID, SECRET, return_url="https://t.me/bot", base_url=fake.base_url))
    closing.append(gw)
    return gw


def _completed(**kw: Any) -> dict[str, Any]:
    model = {
        "TransactionId": 504,
        "Amount": 199.5,
        "Currency": "RUB",
        "InvoiceId": PAYMENT_ID,
        "AccountId": str(USER_ID),
        "Status": "Completed",
        "StatusCode": 3,
        "Reason": "Approved",
        "ReasonCode": 0,
        "CardFirstSix": "424242",
        "CardLastFour": "4242",
        "CardType": "Visa",
        "Token": TOKEN,
        "OperationType": "Payment",
    }
    model.update(kw)
    return model


# ---------------------------------------------------------------------------------------------
# CloudPayments gateway
# ---------------------------------------------------------------------------------------------
async def test_cloudpayments_create_order(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply(
        "POST",
        "/orders/create",
        (200, {"Success": True, "Model": {"Id": "f2K8", "Url": "https://orders.cloudpayments.ru/d/f2K8"}}),
    )
    gw = _cp(fake, closing)
    invoice = await gw.create(make_payment(19950), make_user(receipt_email="a@b.ru"), "VPN: 1 month")

    assert invoice.url == "https://orders.cloudpayments.ru/d/f2K8"
    assert invoice.provider_payment_id is None and invoice.provider_amount == "199.50"
    req = fake.requests[0]
    expected_auth = "Basic " + base64.b64encode(f"{PUBLIC_ID}:{SECRET}".encode()).decode()
    assert req.headers["Authorization"] == expected_auth
    assert req.headers["X-Request-ID"] == f"order-{PAYMENT_ID}"
    assert req.json() == {
        "Amount": 199.5,
        "Currency": "RUB",
        "Description": "VPN: 1 month",
        "InvoiceId": PAYMENT_ID,
        "AccountId": str(USER_ID),
        "Email": "a@b.ru",
        "SuccessRedirectUrl": "https://t.me/bot",
        "FailRedirectUrl": "https://t.me/bot",
    }


async def test_cloudpayments_create_rejected(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    fake.reply("POST", "/orders/create", (200, {"Success": False, "Message": "Invalid Amount value"}))
    with pytest.raises(GatewayError, match="Invalid Amount"):
        await _cp(fake, closing).create(make_payment(), make_user(), "x")


async def test_cloudpayments_fetch_mapping(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _cp(fake, closing)
    fake.reply("POST", "/v2/payments/find", (200, {"Success": True, "Model": _completed()}))
    status = await gw.fetch(make_payment())
    assert (status.status, status.paid_amount_kop, status.provider_payment_id) == (
        PaymentStatus.SUCCEEDED,
        19950,
        "504",
    )
    assert status.saved_method == SavedMethod(TOKEN, "Visa •• 4242")
    assert TOKEN not in repr(status.saved_method)
    assert fake.requests[-1].json() == {"InvoiceId": PAYMENT_ID}

    fake.reply("POST", "/v2/payments/find", (200, {"Success": False, "Message": "Not found"}))
    assert (await gw.fetch(make_payment())).status is PaymentStatus.PENDING

    declined = {"Success": True, "Model": _completed(Status="Declined", ReasonCode=5051, Token=None)}
    fake.reply("POST", "/v2/payments/find", (200, declined))
    assert (await gw.fetch(make_payment())).status is PaymentStatus.PENDING  # the payer may try another card
    assert (await gw.fetch(make_payment(saved_method_id=1))).status is PaymentStatus.CANCELED  # a charge is final

    fake.reply("POST", "/v2/payments/find", (200, {"Success": True, "Model": _completed(OperationType="Refund")}))
    assert (await gw.fetch(make_payment())).status is PaymentStatus.REFUNDED

    fake.reply("POST", "/v2/payments/find", (200, {"Success": True, "Model": _completed(Currency="USD")}))
    assert (await gw.fetch(make_payment())).paid_amount_kop is None

    fake.reply("POST", "/v2/payments/find", (200, {"Success": True, "Model": _completed(Status="Weird")}))
    with pytest.raises(GatewayError):
        await gw.fetch(make_payment())


def _form(**fields: Any) -> bytes:
    base = {"TransactionId": "504", "Amount": "199.50", "Currency": "RUB", "InvoiceId": PAYMENT_ID}
    base.update(fields)
    return urlencode(base).encode()


async def test_cloudpayments_webhook_signatures(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _cp(fake, closing)
    raw = _form(Token=TOKEN, Description="VPN: 1 месяц")
    signed = body_signature(raw, SECRET)
    assert signed == base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()).decode()

    result = await gw.parse_webhook(raw, {"Content-HMAC": signed}, "1.2.3.4")
    assert result.payment_ids == [PAYMENT_ID] and result.provider_ids == []
    assert result.response_body == '{"code":0}' and result.trusted_status is None

    decoded = unquote_plus(raw.decode()).encode()
    result = await gw.parse_webhook(raw, {"X-Content-HMAC": body_signature(decoded, SECRET)}, None)
    assert result.payment_ids == [PAYMENT_ID]

    json_raw = json.dumps({"InvoiceId": PAYMENT_ID, "Status": "Completed"}).encode()
    result = await gw.parse_webhook(json_raw, {"content-hmac": body_signature(json_raw, SECRET)}, None)
    assert result.payment_ids == [PAYMENT_ID]

    for headers in ({}, {"Content-HMAC": "AAAA"}, {"Content-HMAC": body_signature(raw, "other")}):
        with pytest.raises(WebhookRejected) as exc:
            await gw.parse_webhook(raw, headers, None)
        assert exc.value.status_code == 403
    for bad in (b"{not json", b"[1]"):
        with pytest.raises(WebhookRejected) as exc:
            await gw.parse_webhook(bad, {"Content-HMAC": body_signature(bad, SECRET)}, None)
        assert exc.value.status_code == 400


async def test_cloudpayments_token_charge(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _cp(fake, closing)
    fake.reply("POST", "/payments/tokens/charge", (200, {"Success": True, "Model": _completed(Amount=150)}))
    result = await gw.charge_saved(make_payment(15000, saved_method_id=1), make_user(), TOKEN, "VPN: 1 month")

    assert result == ChargeResult(
        ProviderStatus(PaymentStatus.SUCCEEDED, 15000, "504", SavedMethod(TOKEN, "Visa •• 4242"))
    )
    req = fake.requests[0]
    assert req.headers["X-Request-ID"] == PAYMENT_ID
    assert req.json() == {
        "Amount": 150,
        "Currency": "RUB",
        "AccountId": str(USER_ID),
        "Token": TOKEN,
        "InvoiceId": PAYMENT_ID,
        "Description": "VPN: 1 month",
        "TrInitiatorCode": 0,
        "PaymentScheduled": 1,
    }


@pytest.mark.parametrize(
    ("code", "invalid"),
    [(5051, False), (5096, False), (5054, True), (5041, True), (5043, True), (5057, True)],
)
async def test_cloudpayments_declined_charge(
    fake: FakeProvider, closing: list[PaymentGateway], code: int, invalid: bool
) -> None:
    model = _completed(Status="Declined", ReasonCode=code, Reason="Declined", Token=None)
    fake.reply("POST", "/payments/tokens/charge", (200, {"Success": False, "Model": model}))
    result = await _cp(fake, closing).charge_saved(make_payment(saved_method_id=1), make_user(), TOKEN, "x")
    assert result.status.status is PaymentStatus.CANCELED
    assert result.method_invalid is invalid and result.reason == "Declined"


async def test_cloudpayments_charge_3ds_and_errors(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _cp(fake, closing)
    three_ds = {"TransactionId": 504, "PaReq": "x", "AcsUrl": "https://acs.example/3ds"}
    fake.reply("POST", "/payments/tokens/charge", (200, {"Success": False, "Model": three_ds}))
    result = await gw.charge_saved(make_payment(), make_user(), TOKEN, "x")
    assert result.status.status is PaymentStatus.CANCELED and result.reason == "3ds_required"
    assert not result.method_invalid

    fake.reply("POST", "/payments/tokens/charge", (200, {"Success": False, "Message": "Token is required"}))
    with pytest.raises(GatewayError):
        await gw.charge_saved(make_payment(), make_user(), TOKEN, "x")


def test_cloudpayments_is_registered() -> None:
    settings = make_settings(
        cloudpayments_enabled=True,
        cloudpayments_public_id=PUBLIC_ID,
        cloudpayments_api_secret=SECRET,
        public_base_url="https://bot.example.com",
    )
    gateways = build_gateways(settings)
    assert isinstance(gateways["cloudpayments"], CloudPaymentsGateway)
    with pytest.raises(ValueError, match="CLOUDPAYMENTS"):
        make_settings(cloudpayments_enabled=True, public_base_url="https://bot.example.com")


# ---------------------------------------------------------------------------------------------
# YooKassa autopayments
# ---------------------------------------------------------------------------------------------
YK_SAVED = {
    "id": "22e18a2f-000f-5000-a000-1db6312b7767",
    "status": "succeeded",
    "paid": True,
    "amount": {"value": "199.00", "currency": "RUB"},
    "payment_method": {
        "type": "bank_card",
        "id": "22e18a2f-000f-5000-a000-1db6312b7767",
        "saved": True,
        "title": "Bank card *4444",
        "card": {"first6": "555555", "last4": "4444", "card_type": "Mir"},
    },
}


def _yk(fake: FakeProvider, closing: list[PaymentGateway]) -> YooKassaGateway:
    gw = _no_retry_delay(YooKassaGateway("shop", "key", base_url=fake.base_url, check_ip=False))
    closing.append(gw)
    return gw


async def test_yookassa_asks_to_save_only_with_consent(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    created = {"id": "p1", "status": "pending", "confirmation": {"confirmation_url": "https://yoomoney.ru/c"}}
    fake.reply("POST", "/payments", (200, created))
    gw = _yk(fake, closing)
    await gw.create(make_payment(), make_user(), "x")
    await gw.create(make_payment(save_method=True), make_user(), "x")
    assert "save_payment_method" not in fake.requests[0].json()
    assert fake.requests[1].json()["save_payment_method"] is True


async def test_yookassa_saved_method_from_status(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _yk(fake, closing)
    fake.reply("GET", f"/payments/{YK_SAVED['id']}", (200, YK_SAVED))
    status = await gw.fetch(make_payment(provider_payment_id=str(YK_SAVED["id"])))
    assert status.saved_method == SavedMethod(str(YK_SAVED["id"]), "Mir •• 4444")

    unsaved = {**YK_SAVED, "payment_method": {**YK_SAVED["payment_method"], "saved": False}}  # type: ignore[dict-item]
    fake.reply("GET", f"/payments/{YK_SAVED['id']}", (200, unsaved))
    assert (await gw.fetch(make_payment(provider_payment_id=str(YK_SAVED["id"])))).saved_method is None


async def test_yookassa_autopayment_request(fake: FakeProvider, closing: list[PaymentGateway]) -> None:
    gw = _yk(fake, closing)
    fake.reply("POST", "/payments", (200, {**YK_SAVED, "id": "p2"}))
    result = await gw.charge_saved(make_payment(15000), make_user(), "pm-1", "VPN: 1 month")
    assert result.status.status is PaymentStatus.SUCCEEDED and not result.method_invalid
    body = fake.requests[0].json()
    assert body == {
        "amount": {"value": "150.00", "currency": "RUB"},
        "capture": True,
        "payment_method_id": "pm-1",
        "description": "VPN: 1 month",
        "metadata": {"payment_id": PAYMENT_ID, "user_id": str(USER_ID)},
    }
    assert "confirmation" not in body
    assert fake.requests[0].headers["Idempotence-Key"] == PAYMENT_ID

    for reason, invalid in (("permission_revoked", True), ("card_expired", True), ("insufficient_funds", False)):
        canceled = {"id": "p3", "status": "canceled", "cancellation_details": {"party": "yoo_money", "reason": reason}}
        fake.reply("POST", "/payments", (200, canceled))
        result = await gw.charge_saved(make_payment(), make_user(), "pm-1", "x")
        assert result.status.status is PaymentStatus.CANCELED
        assert (result.reason, result.method_invalid) == (reason, invalid)


# ---------------------------------------------------------------------------------------------
# Saving a card
# ---------------------------------------------------------------------------------------------
async def _payment(sf: SessionFactory, *, save_method: bool, provider: str = "cloudpayments") -> Payment:
    async with sf.begin() as s:
        if await s.get(User, USER_ID) is None:
            await add_user(s, USER_ID)
        payment = Payment(
            id=PAYMENT_ID,
            provider=provider,
            user_id=USER_ID,
            amount_kop=19950,
            status=PaymentStatus.PENDING,
            save_method=save_method,
        )
        s.add(payment)
    return payment


async def _cards(sf: SessionFactory) -> list[SavedPaymentMethod]:
    async with sf() as s:
        return list((await s.scalars(select(SavedPaymentMethod).order_by(SavedPaymentMethod.id))).all())


@pytest.mark.parametrize(("enabled", "consent", "stored"), [(True, True, 1), (True, False, 0), (False, True, 0)])
async def test_card_saved_only_with_setting_and_consent(
    session_factory: SessionFactory, enabled: bool, consent: bool, stored: int
) -> None:
    settings = make_settings(saved_cards_enabled=enabled)
    payment = await _payment(session_factory, save_method=consent)
    status = ProviderStatus(PaymentStatus.SUCCEEDED, 19950, "504", SavedMethod(TOKEN, "Visa •• 4242"))
    assert await service.apply_provider_status(session_factory, settings, payment, status) is not None
    cards = await _cards(session_factory)
    assert len(cards) == stored
    if stored:
        assert (cards[0].user_id, cards[0].provider, cards[0].token, cards[0].title) == (
            USER_ID,
            "cloudpayments",
            TOKEN,
            "Visa •• 4242",
        )
    # A redelivered status changes nothing.
    await service.apply_provider_status(session_factory, settings, payment, status)
    assert len(await _cards(session_factory)) == stored


async def test_card_saved_from_verified_notification_only(
    session_factory: SessionFactory, fake: FakeProvider, closing: list[PaymentGateway]
) -> None:
    settings = make_settings(saved_cards_enabled=True)
    gw = _cp(fake, closing)
    await _payment(session_factory, save_method=True)
    raw = _form(Token="tk_forged", Status="Completed")

    # Forged notification: rejected before anything is stored or fetched.
    with pytest.raises(WebhookRejected):
        await service.handle_webhook(session_factory, settings, gw, raw, {"Content-HMAC": "forged"}, None)
    assert fake.requests == [] and await _cards(session_factory) == []

    # Signed, but the provider has no completed payment yet: the token in the body is ignored.
    fake.reply("POST", "/v2/payments/find", (200, {"Success": False, "Message": "Not found"}))
    signed = {"Content-HMAC": body_signature(raw, SECRET)}
    await service.handle_webhook(session_factory, settings, gw, raw, signed, None)
    assert await _cards(session_factory) == []

    # Signed and confirmed by the provider: the token reported by the provider is stored and the payment credited.
    fake.reply("POST", "/v2/payments/find", (200, {"Success": True, "Model": _completed()}))
    result = await service.handle_webhook(session_factory, settings, gw, raw, signed, None)
    assert result.response_body == '{"code":0}'
    assert [c.token for c in await _cards(session_factory)] == [TOKEN]
    async with session_factory() as s:
        payment = await s.get(Payment, PAYMENT_ID)
        assert payment is not None and payment.status is PaymentStatus.SUCCEEDED


async def test_removed_card_stays_removed_until_saved_again(session_factory: SessionFactory) -> None:
    settings = make_settings(saved_cards_enabled=True)
    payment = await _payment(session_factory, save_method=True)
    saved = SavedMethod(TOKEN, "Visa •• 4242")
    assert await saved_cards.remember(session_factory, settings, payment, saved)
    (card,) = await _cards(session_factory)

    async with session_factory.begin() as s:
        assert not await saved_cards.deactivate(s, 999, card.id)  # someone else's card
        assert await saved_cards.deactivate(s, USER_ID, card.id)
        assert not await saved_cards.deactivate(s, USER_ID, card.id)
    async with session_factory() as s:
        assert await saved_cards.list_active(s, USER_ID) == []

    # A redelivery of the old payment does not bring it back...
    assert not await saved_cards.remember(session_factory, settings, payment, saved)
    # ...a new payment with fresh consent does.
    later = Payment(
        id="later",
        provider="cloudpayments",
        user_id=USER_ID,
        amount_kop=100,
        status=PaymentStatus.SUCCEEDED,
        save_method=True,
        created_at=datetime.now(UTC) + timedelta(seconds=5),
    )
    assert await saved_cards.remember(session_factory, settings, later, saved)
    assert [c.is_active for c in await _cards(session_factory)] == [True]


# ---------------------------------------------------------------------------------------------
# Auto-renewal from a saved card
# ---------------------------------------------------------------------------------------------
@dataclass
class FakeRecurring:
    """A gateway double that charges saved cards with queued results."""

    key: str = "cloudpayments"
    title: str = "Card"
    supports_polling: bool = True
    polls_by_payment_id: bool = True
    min_amount_kop: int = 100
    results: list[ChargeResult | Exception] = field(default_factory=list)
    calls: list[tuple[str, int, str]] = field(default_factory=list)

    async def charge_saved(self, payment: Payment, user: User, token: str, description: str) -> ChargeResult:
        self.calls.append((payment.id, payment.amount_kop, token))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result

    async def create(self, *a: Any) -> Any:
        raise NotImplementedError

    async def fetch(self, payment: Payment) -> ProviderStatus:
        return ProviderStatus(PaymentStatus.PENDING)

    async def parse_webhook(self, *a: Any) -> Any:
        raise NotImplementedError

    async def close(self) -> None:
        return None


PAID = ChargeResult(ProviderStatus(PaymentStatus.SUCCEEDED, None, "tx-1"))


@pytest.fixture
def synced(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    async def fake_sync(session_factory: Any, registry: Any, settings: Any, sub_id: int, **kw: Any) -> Any:
        calls.append(sub_id)
        return provisioning.SyncResult(subscription_id=sub_id)

    monkeypatch.setattr(provisioning, "sync_subscription", fake_sync)
    return calls


async def _setup(sf: SessionFactory, now: datetime, *, balance_kop: int = 5_000, card: bool = True) -> int:
    async with sf.begin() as s:
        await add_user(s, USER_ID, balance_kop=balance_kop)
        plan = await add_plan(s, days=30, price_kop=15_000)
        sub = await add_sub(s, USER_ID, expires_at=now + timedelta(hours=10), plan=plan)
        if card:
            s.add(SavedPaymentMethod(user_id=USER_ID, provider="cloudpayments", token=TOKEN, title="Visa •• 4242"))
        return sub.id


async def _sub(sf: SessionFactory, sub_id: int) -> Subscription:
    async with sf() as s:
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        return sub


async def _payments(sf: SessionFactory) -> list[Payment]:
    async with sf() as s:
        return list((await s.scalars(select(Payment))).all())


@pytest.mark.parametrize(
    ("setting", "setup"),
    [(False, {}), (True, {"card": False}), (True, {"balance_kop": 15_000})],
)
async def test_no_charge_when_disabled_without_card_or_with_enough_balance(
    session_factory: SessionFactory, setting: bool, setup: dict[str, Any]
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    await _setup(session_factory, now, **setup)
    gateway = FakeRecurring(results=[PAID])
    charges = await autopay.charge_due(
        session_factory, make_settings(saved_cards_enabled=setting), {"cloudpayments": gateway}, now
    )
    assert charges == [] and gateway.calls == [] and await _payments(session_factory) == []


async def test_card_renewal_charges_shortfall_once_and_renews(
    session_factory: SessionFactory, tmp_path: Path, synced: list[int]
) -> None:
    ctx, _api = make_ctx(session_factory, tmp_path, saved_cards_enabled=True)
    now = datetime.now(UTC).replace(microsecond=0)
    sub_id = await _setup(session_factory, now)
    expires = (await _sub(session_factory, sub_id)).expires_at
    gateway = FakeRecurring(results=[PAID])

    async def on_credited(result: service.CreditResult) -> None:
        await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]

    gateways = {"cloudpayments": gateway}
    (charge,) = await autopay.charge_due(session_factory, ctx.settings, gateways, now, on_credited)
    assert charge.outcome is autopay.ChargeOutcome.PAID and charge.amount_kop == 10_000
    assert gateway.calls == [(charge.payment_id, 10_000, TOKEN)]

    sub = await _sub(session_factory, sub_id)
    assert sub.expires_at == expires + timedelta(days=30)
    assert synced == [sub_id]
    async with session_factory() as s:
        payment = await s.get(Payment, charge.payment_id)
        assert payment is not None and payment.status is PaymentStatus.SUCCEEDED and payment.saved_method_id
        user = await s.get(User, USER_ID)
        assert user is not None and user.balance_kop == 0
        purchase = await s.scalar(select(Purchase))
        assert purchase is not None and purchase.kind is PurchaseKind.AUTO_RENEW
        refs = set(await s.scalars(select(BalanceTransaction.ref)))
        assert any(ref.startswith(f"renew:{sub_id}:") for ref in refs)
        card = await s.scalar(select(SavedPaymentMethod))
        assert card is not None and card.last_used_at is not None

    # Running again (the period is renewed now) charges nothing.
    assert await autopay.charge_due(session_factory, ctx.settings, gateways, now, on_credited) == []
    assert len(gateway.calls) == 1
    await notifications.close(ctx)  # type: ignore[arg-type]


async def test_running_twice_for_the_same_period_charges_once(session_factory: SessionFactory) -> None:
    settings = make_settings(saved_cards_enabled=True)
    now = datetime.now(UTC).replace(microsecond=0)
    sub_id = await _setup(session_factory, now)
    pending = ChargeResult(ProviderStatus(PaymentStatus.PENDING, None, "tx-9"))
    gateway = FakeRecurring(results=[pending])

    (first,) = await autopay.charge_due(session_factory, settings, {"cloudpayments": gateway}, now)
    assert first.outcome is autopay.ChargeOutcome.PENDING
    assert await autopay.charge_due(session_factory, settings, {"cloudpayments": gateway}, now) == []
    assert len(gateway.calls) == 1
    (payment,) = await _payments(session_factory)
    assert payment.id == saved_cards.autopay_payment_id(await _sub(session_factory, sub_id))
    assert (payment.status, payment.provider_payment_id) == (PaymentStatus.PENDING, "tx-9")

    # The wallet auto-renewal and the card renewal use the same ledger ref: crediting the pending charge later
    # renews the period exactly once.
    credited = await service.credit(session_factory, settings, payment.id, provider_payment_id="tx-9")
    assert credited is not None
    async with session_factory.begin() as s:
        _, outcome = await subscriptions.renew_from_wallet(s, settings, sub_id, now)
    async with session_factory.begin() as s:
        _, again = await subscriptions.renew_from_wallet(s, settings, sub_id, now)
    assert outcome is subscriptions.RenewOutcome.RENEWED and again is subscriptions.RenewOutcome.NOT_DUE
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Purchase)) == 1


async def test_unknown_outcome_is_retried_with_the_same_key(session_factory: SessionFactory) -> None:
    settings = make_settings(saved_cards_enabled=True)
    now = datetime.now(UTC).replace(microsecond=0)
    await _setup(session_factory, now)
    gateway = FakeRecurring(results=[GatewayError("timeout"), ChargeResult(ProviderStatus(PaymentStatus.PENDING))])

    (first,) = await autopay.charge_due(session_factory, settings, {"cloudpayments": gateway}, now)
    assert first.outcome is autopay.ChargeOutcome.ERROR
    (second,) = await autopay.charge_due(session_factory, settings, {"cloudpayments": gateway}, now)
    assert second.outcome is autopay.ChargeOutcome.PENDING
    assert [c[0] for c in gateway.calls] == [first.payment_id, first.payment_id]
    assert len(await _payments(session_factory)) == 1

    # Past the retry window an unknown outcome is given up, not charged again.
    async with session_factory.begin() as s:
        payment = await s.get(Payment, first.payment_id)
        assert payment is not None
        payment.status = PaymentStatus.CREATED
        payment.created_at = now - autopay.RETRY_WINDOW - timedelta(minutes=1)
    assert await autopay.charge_due(session_factory, settings, {"cloudpayments": gateway}, now) == []
    assert len(gateway.calls) == 2
    assert (await _payments(session_factory))[0].status is PaymentStatus.EXPIRED


@pytest.mark.parametrize("invalid", [False, True])
async def test_declined_charge_is_not_retried_and_invalid_card_is_removed(
    session_factory: SessionFactory, invalid: bool
) -> None:
    settings = make_settings(saved_cards_enabled=True)
    now = datetime.now(UTC).replace(microsecond=0)
    await _setup(session_factory, now)
    declined = ChargeResult(ProviderStatus(PaymentStatus.CANCELED), method_invalid=invalid, reason="5054")
    gateway = FakeRecurring(results=[declined])

    (charge,) = await autopay.charge_due(session_factory, settings, {"cloudpayments": gateway}, now)
    assert charge.outcome is autopay.ChargeOutcome.DECLINED and charge.card_removed is invalid
    assert charge.card_title == "Visa •• 4242"
    assert (await _payments(session_factory))[0].status is PaymentStatus.CANCELED
    assert await autopay.charge_due(session_factory, settings, {"cloudpayments": gateway}, now) == []
    assert len(gateway.calls) == 1
    (card,) = await _cards(session_factory)
    assert card.is_active is not invalid


async def test_removed_card_is_not_charged(session_factory: SessionFactory) -> None:
    settings = make_settings(saved_cards_enabled=True)
    now = datetime.now(UTC).replace(microsecond=0)
    await _setup(session_factory, now)
    (card,) = await _cards(session_factory)
    async with session_factory.begin() as s:
        assert await saved_cards.deactivate(s, USER_ID, card.id)
    gateway = FakeRecurring(results=[PAID])
    assert await autopay.charge_due(session_factory, settings, {"cloudpayments": gateway}, now) == []
    assert gateway.calls == []


async def test_card_of_a_disabled_provider_is_not_charged(session_factory: SessionFactory) -> None:
    settings = make_settings(saved_cards_enabled=True)
    now = datetime.now(UTC).replace(microsecond=0)
    await _setup(session_factory, now)
    gateway = FakeRecurring(key="yookassa", results=[PAID])
    assert await autopay.charge_due(session_factory, settings, {"yookassa": gateway}, now) == []
    assert gateway.calls == []


# ---------------------------------------------------------------------------------------------
# Checkout consent
# ---------------------------------------------------------------------------------------------
class _InvoiceGateway(FakeRecurring):
    async def create(self, payment: Payment, *a: Any) -> Invoice:
        return Invoice(f"inv-{payment.id}", "https://pay.example/inv", "150.00", "RUB")


async def test_checkout_consent_is_stored_on_the_payment_only_when_allowed(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await add_user(s, USER_ID)
    card_gateway = _InvoiceGateway()
    for enabled in (False, True):
        ctx = SimpleNamespace(settings=make_settings(saved_cards_enabled=enabled), session_factory=session_factory)
        order = await flow.build_order(ctx, USER_ID, Purpose.TOPUP, amount_kop=15_000, save_card=True)  # type: ignore[arg-type]
        assert order.save_card is enabled
        assert flow.can_save_card(ctx.settings, card_gateway) is enabled
        payment = await flow.create(ctx, USER_ID, order, card_gateway)  # type: ignore[arg-type]
        assert payment.save_method is enabled

    ctx = SimpleNamespace(settings=make_settings(saved_cards_enabled=True), session_factory=session_factory)
    order = await flow.build_order(ctx, USER_ID, Purpose.TOPUP, amount_kop=15_000, save_card=True)  # type: ignore[arg-type]
    plain_gateway = SimpleNamespace(key="platega", title="Card", min_amount_kop=100)  # cannot keep cards
    assert flow.method_for(order, plain_gateway, ctx.settings) is None  # type: ignore[arg-type]
    plain = await flow.build_order(ctx, USER_ID, Purpose.TOPUP, amount_kop=15_000)  # type: ignore[arg-type]
    assert not plain.save_card


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_texts_exist(lang: str) -> None:
    root = Path(__file__).resolve().parent.parent / "app" / "modules"
    payments = tomllib.loads((root / "payments" / "locales" / f"{lang}.toml").read_text(encoding="utf-8"))
    wallet = tomllib.loads((root / "wallet" / "locales" / f"{lang}.toml").read_text(encoding="utf-8"))
    subscription = tomllib.loads((root / "subscription" / "locales" / f"{lang}.toml").read_text(encoding="utf-8"))
    assert "{title}" in payments["payments"]["method"]["save_card"]
    assert payments["payments"]["invoice"]["save_card_note"]
    assert {"title", "item", "hint", "remove", "removed", "empty", "off"} <= set(wallet["wallet"]["cards"])
    assert wallet["wallet"]["cards_button"]
    assert "{card}" in subscription["subscription"]["autopay"]["declined"]
