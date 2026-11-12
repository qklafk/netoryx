"""Receipts in "Мой налог": queueing on credit, registration against a fake cabinet API, retries, refunds."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

from app.config import Settings
from app.db.models import Payment, PaymentStatus, Plan, TaxReceipt, TaxReceiptStatus, User
from app.db.session import SessionFactory
from app.services import tax_receipts
from app.services.npd import NpdAuthError, NpdClient, NpdError, moscow_iso
from app.services.payments import service
from app.utils.crypto import SecretBox
from tests.conftest import make_settings

USER_ID = 4242
INN = "500100732259"
BOX = SecretBox("npd-test-key-0123456789abcdef")


@dataclass
class FakeCabinet:
    """Just enough of lknpd.nalog.ru/api to exercise the client."""

    password: str = "secret"
    tokens: set[str] = field(default_factory=set)
    refresh_tokens: set[str] = field(default_factory=set)
    incomes: list[dict[str, Any]] = field(default_factory=list)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_income: int = 0  # answer 500 to this many income requests ...
    apply_failed_income: bool = False  # ... after registering them anyway
    counter: int = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/api/{tail:.*}", self.handle)
        return app

    def _issue(self) -> dict[str, Any]:
        self.counter += 1
        token, refresh = f"tok-{self.counter}", f"ref-{self.counter}"
        self.tokens.add(token)
        self.refresh_tokens.add(refresh)
        return {
            "token": token,
            "refreshToken": refresh,
            "tokenExpireIn": "2030-01-01T00:00:00Z",
            "profile": {"inn": INN},
        }

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path.removeprefix("/api")
        body: dict[str, Any] = await request.json() if request.can_read_body else {}
        self.requests.append((f"{request.method} {path}", body))
        if path == "/v1/auth/lkfl":
            if body.get("username") != INN or body.get("password") != self.password:
                return web.json_response({"message": "bad credentials"}, status=422)
            return web.json_response(self._issue())
        if path == "/v1/auth/token":
            if body.get("refreshToken") not in self.refresh_tokens:
                return web.json_response({"message": "expired"}, status=401)
            self.refresh_tokens.discard(body["refreshToken"])  # rotated
            return web.json_response(self._issue())
        if path == "/v2/auth/challenge/sms/start":
            return web.json_response({"challengeToken": "ch-1", "expireDate": "x", "expireIn": 120})
        if path == "/v1/auth/challenge/sms/verify":
            if body.get("code") != "1234" or body.get("challengeToken") != "ch-1":
                return web.json_response({"message": "wrong code"}, status=400)
            return web.json_response(self._issue())
        auth = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if auth not in self.tokens:
            return web.json_response({"message": "unauthorized"}, status=401)
        if path == "/v1/income":
            uuid = f"rcpt{len(self.incomes) + 1:04d}"
            if self.fail_income > 0:
                self.fail_income -= 1
                if self.apply_failed_income:
                    self._store(uuid, body)
                return web.json_response({"message": "internal"}, status=500)
            self._store(uuid, body)
            return web.json_response({"approvedReceiptUuid": uuid})
        if path == "/v1/incomes":
            return web.json_response(
                {"content": list(reversed(self.incomes)), "hasMore": False, "currentOffset": 0, "currentLimit": 100}
            )
        if path == "/v1/cancel":
            for income in self.incomes:
                if income["approvedReceiptUuid"] == body["receiptUuid"]:
                    income["cancellationInfo"] = {"comment": body["comment"]}
                    return web.json_response({"incomeInfo": income})
            return web.json_response({"message": "not found"}, status=404)
        return web.json_response({}, status=404)

    def _store(self, uuid: str, body: dict[str, Any]) -> None:
        self.incomes.append(
            {
                "approvedReceiptUuid": uuid,
                "name": body["services"][0]["name"],
                "operationTime": body["operationTime"],
                "totalAmount": float(body["totalAmount"]),
                "cancellationInfo": None,
            }
        )

    def income_posts(self) -> list[dict[str, Any]]:
        return [b for r, b in self.requests if r == "POST /v1/income"]


@pytest.fixture
async def cabinet() -> AsyncIterator[tuple[FakeCabinet, str]]:
    fake = FakeCabinet()
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield fake, str(server.make_url("/api"))
    finally:
        await server.close()


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {"npd_enabled": True, "npd_inn": INN, "npd_password": "secret"}
    base.update(kw)
    return make_settings(**base)


async def _seed(sf: SessionFactory, *, provider: str = "yookassa", plan: bool = True, pid: str = "pay-1") -> None:
    async with sf.begin() as s:
        if await s.get(User, USER_ID) is None:
            s.add(User(tg_id=USER_ID, referral_code="U1", sub_token="tok-u1"))
        if plan and await s.get(Plan, 1) is None:
            s.add(Plan(id=1, title="1 месяц", duration_days=30, price_kop=19900))
        await s.flush()
        s.add(
            Payment(
                id=pid,
                provider=provider,
                provider_payment_id=f"prov-{pid}",
                user_id=USER_ID,
                amount_kop=19900,
                plan_id=1 if plan else None,
                status=PaymentStatus.PENDING,
            )
        )


async def _receipts(sf: SessionFactory) -> list[TaxReceipt]:
    async with sf() as s:
        return list((await s.scalars(select(TaxReceipt).order_by(TaxReceipt.id))).all())


def _factory(base_url: str) -> tax_receipts.ClientFactory:
    return lambda device: NpdClient(device, base_url=base_url, timeout=5)


async def _run(sf: SessionFactory, settings: Settings, base_url: str, **kw: Any) -> tax_receipts.ProcessResult:
    return await tax_receipts.process_due(sf, settings, BOX, _factory(base_url), **kw)


# --- queueing ---------------------------------------------------------------------------------------------------
def test_disabled_by_default() -> None:
    settings = make_settings()
    assert settings.npd_enabled is False
    assert not tax_receipts.eligible(settings, "yookassa")
    assert "stars" not in settings.npd_providers and "cryptobot" not in settings.npd_providers


async def test_credit_does_not_queue_when_disabled(session_factory: SessionFactory) -> None:
    await _seed(session_factory)
    assert await service.credit(session_factory, make_settings(), "pay-1") is not None
    assert await _receipts(session_factory) == []


async def test_credit_queues_one_receipt_for_allowed_provider(session_factory: SessionFactory) -> None:
    settings = _settings()
    await _seed(session_factory)
    await _seed(session_factory, provider="stars", pid="pay-stars")
    assert await service.credit(session_factory, settings, "pay-1") is not None
    assert await service.credit(session_factory, settings, "pay-stars") is not None
    assert await service.credit(session_factory, settings, "pay-1") is None  # already credited
    rows = await _receipts(session_factory)
    assert [(r.payment_id, r.status, r.amount_kop, r.service_name) for r in rows] == [
        ("pay-1", "pending", 19900, "Подписка VPN 1 месяц")
    ]


async def test_topup_uses_the_topup_service_name(session_factory: SessionFactory) -> None:
    await _seed(session_factory, plan=False)
    await service.credit(session_factory, _settings(npd_topup_service_name="Доступ к сервису"), "pay-1")
    assert (await _receipts(session_factory))[0].service_name == "Доступ к сервису"


# --- registration -----------------------------------------------------------------------------------------------
async def test_registers_income_with_expected_request_shape(
    session_factory: SessionFactory, cabinet: tuple[FakeCabinet, str]
) -> None:
    fake, url = cabinet
    settings = _settings()
    await _seed(session_factory)
    await service.credit(session_factory, settings, "pay-1")

    result = await _run(session_factory, settings, url)

    assert result.auth_error is None and len(result.registered) == 1
    login = next(b for r, b in fake.requests if r == "POST /v1/auth/lkfl")
    assert login["username"] == INN and login["password"] == "secret"
    assert login["deviceInfo"]["sourceType"] == "WEB"
    assert set(login["deviceInfo"]) == {"sourceType", "sourceDeviceId", "appVersion", "metaDetails"}
    assert "userAgent" in login["deviceInfo"]["metaDetails"]
    (body,) = fake.income_posts()
    assert body["services"] == [{"name": "Подписка VPN 1 месяц", "amount": "199.00", "quantity": 1}]
    assert body["totalAmount"] == "199.00"
    assert body["paymentType"] == "CASH"
    assert body["client"] == {"contactPhone": None, "displayName": None, "incomeType": "FROM_INDIVIDUAL", "inn": None}
    assert body["ignoreMaxTotalIncomeRestriction"] is False
    assert body["operationTime"].endswith("+03:00") and body["requestTime"].endswith("+03:00")
    (row,) = await _receipts(session_factory)
    assert row.status == TaxReceiptStatus.REGISTERED and row.receipt_uuid == "rcpt0001"
    assert row.receipt_url == f"{url}/v1/receipt/{INN}/rcpt0001/print"
    # the session is stored encrypted and reused
    stored = await tax_receipts.load_session(session_factory, BOX)
    assert stored is not None and stored.inn == INN
    assert await _run(session_factory, settings, url) == tax_receipts.ProcessResult()


async def test_expired_token_is_refreshed_and_rotated(
    session_factory: SessionFactory, cabinet: tuple[FakeCabinet, str]
) -> None:
    fake, url = cabinet
    settings = _settings(npd_password=None)
    async with NpdClient("dev", base_url=url) as client:
        first = await client.login_password(INN, "secret")
    await tax_receipts.save_session(session_factory, BOX, first)
    fake.tokens.clear()  # the access token expired
    await _seed(session_factory)
    await service.credit(session_factory, settings, "pay-1")

    result = await _run(session_factory, settings, url)

    assert len(result.registered) == 1
    refreshes = [b for r, b in fake.requests if r == "POST /v1/auth/token"]
    assert [b["refreshToken"] for b in refreshes] == ["ref-1"]
    assert refreshes[0]["deviceInfo"]["sourceType"] == "WEB"
    assert not [r for r, _ in fake.requests if r == "POST /v1/auth/lkfl"][1:]  # no password login needed
    stored = await tax_receipts.load_session(session_factory, BOX)
    assert stored is not None and stored.refresh_token == "ref-2"  # the rotated one


async def test_rejected_credentials_report_auth_error(
    session_factory: SessionFactory, cabinet: tuple[FakeCabinet, str]
) -> None:
    _fake, url = cabinet
    settings = _settings(npd_password="wrong")
    await _seed(session_factory)
    await service.credit(session_factory, settings, "pay-1")
    result = await _run(session_factory, settings, url)
    assert result.auth_error is not None and not result.registered
    (row,) = await _receipts(session_factory)
    assert row.status == "pending" and row.attempts == 0


async def test_sms_login(cabinet: tuple[FakeCabinet, str]) -> None:
    _fake, url = cabinet
    async with NpdClient("dev", base_url=url) as client:
        token, expires = await client.start_sms("79001234567")
        assert (token, expires) == ("ch-1", 120)
        with pytest.raises(NpdError):
            await client.verify_sms("79001234567", token, "0000")
        session = await client.verify_sms("79001234567", token, "1234")
    assert session.inn == INN and session.refresh_token


# --- retries and idempotency ------------------------------------------------------------------------------------
async def test_failure_backs_off_and_gives_up(
    session_factory: SessionFactory, cabinet: tuple[FakeCabinet, str]
) -> None:
    fake, url = cabinet
    fake.fail_income = 100
    settings = _settings(npd_max_attempts=2)
    await _seed(session_factory)
    await service.credit(session_factory, settings, "pay-1")

    first = await _run(session_factory, settings, url)
    (row,) = await _receipts(session_factory)
    assert not first.failed and row.status == "pending" and row.attempts == 1 and row.last_error
    assert row.next_attempt_at is not None and row.next_attempt_at > datetime.now(UTC)
    assert await _run(session_factory, settings, url) == tax_receipts.ProcessResult()  # not due yet

    later = datetime.now(UTC) + timedelta(hours=1)
    second = await _run(session_factory, settings, url, now=later)
    (row,) = await _receipts(session_factory)
    assert second.failed == [row.id] and row.status == "failed" and row.next_attempt_at is None

    fake.fail_income = 0
    async with session_factory.begin() as s:
        await tax_receipts.request_retry(s, row.id)
    third = await _run(session_factory, settings, url)
    assert third.registered == [row.id]
    assert len(fake.income_posts()) == 3


async def test_unknown_outcome_is_looked_up_instead_of_registering_twice(
    session_factory: SessionFactory, cabinet: tuple[FakeCabinet, str]
) -> None:
    fake, url = cabinet
    fake.fail_income, fake.apply_failed_income = 1, True  # registered, but the reply was lost
    settings = _settings()
    await _seed(session_factory)
    await service.credit(session_factory, settings, "pay-1")

    await _run(session_factory, settings, url)
    assert len(fake.incomes) == 1
    result = await _run(session_factory, settings, url, now=datetime.now(UTC) + timedelta(hours=1))

    assert len(result.registered) == 1
    assert len(fake.income_posts()) == 1  # no second income
    (row,) = await _receipts(session_factory)
    assert row.receipt_uuid == fake.incomes[0]["approvedReceiptUuid"]


async def test_lookup_ignores_incomes_of_other_receipts(
    session_factory: SessionFactory, cabinet: tuple[FakeCabinet, str]
) -> None:
    fake, url = cabinet
    settings = _settings()
    await _seed(session_factory)
    await service.credit(session_factory, settings, "pay-1")
    await _run(session_factory, settings, url)
    (first,) = await _receipts(session_factory)
    # a second payment with the same name, amount and second: an income that is already taken must not be adopted
    await _seed(session_factory, pid="pay-2")
    await service.credit(session_factory, settings, "pay-2")
    async with session_factory.begin() as s:
        row = (await s.scalars(select(TaxReceipt).where(TaxReceipt.payment_id == "pay-2"))).one()
        row.operation_time, row.sent = first.operation_time, True
    await _run(session_factory, settings, url)
    rows = await _receipts(session_factory)
    assert len({r.receipt_uuid for r in rows}) == 2 and len(fake.income_posts()) == 2


# --- refunds and cancellation -----------------------------------------------------------------------------------
async def test_refund_annuls_registered_receipt(
    session_factory: SessionFactory, cabinet: tuple[FakeCabinet, str]
) -> None:
    fake, url = cabinet
    settings = _settings()
    await _seed(session_factory)
    await service.credit(session_factory, settings, "pay-1")
    await _run(session_factory, settings, url)

    assert await service.mark_status(session_factory, "pay-1", PaymentStatus.REFUNDED)
    (row,) = await _receipts(session_factory)
    assert row.status == "cancel_pending"
    result = await _run(session_factory, settings, url)

    assert result.cancelled == [row.id]
    cancel = next(b for r, b in fake.requests if r == "POST /v1/cancel")
    assert cancel["receiptUuid"] == "rcpt0001" and cancel["comment"] == "Возврат средств"
    assert set(cancel) == {"operationTime", "requestTime", "comment", "receiptUuid", "partnerCode"}
    (row,) = await _receipts(session_factory)
    assert row.status == "cancelled" and row.cancelled_at is not None


async def test_refund_before_registration_drops_the_receipt(session_factory: SessionFactory) -> None:
    settings = _settings()
    await _seed(session_factory)
    await service.credit(session_factory, settings, "pay-1")
    await service.mark_status(session_factory, "pay-1", PaymentStatus.REFUNDED)
    (row,) = await _receipts(session_factory)
    assert row.status == "cancelled"


async def test_admin_cancel_of_pending_receipt(session_factory: SessionFactory) -> None:
    await _seed(session_factory)
    await service.credit(session_factory, _settings(), "pay-1")
    (row,) = await _receipts(session_factory)
    async with session_factory.begin() as s:
        await tax_receipts.request_cancel(s, row.id)
    assert (await _receipts(session_factory))[0].status == "cancelled"


# --- helpers ----------------------------------------------------------------------------------------------------
def test_moscow_time_and_backoff() -> None:
    assert moscow_iso(datetime(2026, 11, 9, 12, 0, 5, 123, tzinfo=UTC)) == "2026-11-09T15:00:05+03:00"
    assert tax_receipts.backoff(1) == timedelta(minutes=1)
    assert tax_receipts.backoff(3) == timedelta(minutes=4)
    assert tax_receipts.backoff(30) == tax_receipts.BACKOFF_CAP


async def test_unreadable_session_is_ignored(session_factory: SessionFactory) -> None:
    from app.services import kv

    async with session_factory.begin() as s:
        await kv.set(s, tax_receipts.SESSION_KEY, json.dumps({"broken": True}))
    assert await tax_receipts.load_session(session_factory, BOX) is None


def test_auth_error_is_an_npd_error() -> None:
    assert issubclass(NpdAuthError, NpdError)
