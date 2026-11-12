"""Forged signatures with non-ASCII characters are rejected like any other bad signature (no server error)."""

from __future__ import annotations

import json

import pytest

from app.services import plans
from app.services.payments.base import WebhookRejected
from app.services.payments.cloudpayments import CloudPaymentsGateway
from app.services.payments.heleket import HeleketGateway
from app.services.payments.nowpayments import NowPaymentsGateway
from app.services.payments.plisio import PlisioGateway
from app.services.payments.stripe import verify_signature
from app.utils.crypto import SecretBox
from app.web.tma import InitDataError, validate_init_data

BAD = "é" * 64
NOW = 1_800_000_000


async def test_heleket_rejects_non_ascii_signature() -> None:
    gateway = HeleketGateway("merchant", "key")
    with pytest.raises(WebhookRejected):
        await gateway.parse_webhook(json.dumps({"order_id": "x", "sign": BAD}).encode(), {}, None)


async def test_plisio_rejects_non_ascii_signature() -> None:
    gateway = PlisioGateway("key")
    with pytest.raises(WebhookRejected):
        await gateway.parse_webhook(json.dumps({"order_number": "x", "verify_hash": BAD}).encode(), {}, None)


async def test_nowpayments_rejects_non_ascii_signature() -> None:
    gateway = NowPaymentsGateway("key", "secret")
    with pytest.raises(WebhookRejected):
        await gateway.parse_webhook(b'{"order_id": "x"}', {"x-nowpayments-sig": BAD}, None)


def test_stripe_rejects_non_ascii_signature() -> None:
    with pytest.raises(WebhookRejected):
        verify_signature(b"{}", f"t={NOW},v1={BAD}", "whsec", now=NOW)


def test_cloudpayments_rejects_non_ascii_signature() -> None:
    gateway = CloudPaymentsGateway("pk", "secret")
    assert gateway.verify(b"a=b", {"Content-HMAC": BAD}) is False
    assert gateway.verify(b"a=b", {"X-Content-HMAC": BAD}) is False


def test_plan_link_token_rejects_non_ascii() -> None:
    assert plans.link_token_valid(SecretBox("k" * 32), 1, BAD) is False


def test_init_data_rejects_non_ascii_hash() -> None:
    with pytest.raises(InitDataError):
        validate_init_data(f"auth_date={NOW}&user=%7B%22id%22%3A1%7D&hash=%C3%A9%C3%A9", "1:token")


async def test_yoomoney_transfer_protected_by_a_code_is_not_credited() -> None:
    from urllib.parse import urlencode

    from app.db.models import PaymentStatus
    from app.services.payments import yoomoney

    fields = {
        "notification_type": "p2p-incoming",
        "operation_id": "1001",
        "amount": "98.00",
        "withdraw_amount": "100.00",
        "currency": "643",
        "label": "pay-1",
        "codepro": "true",
        "unaccepted": "false",
    }
    fields["sign"] = yoomoney.notification_sign(fields, "secret123")
    gateway = yoomoney.YooMoneyGateway("4100111", "secret123")
    result = await gateway.parse_webhook(urlencode(fields).encode(), {}, None)
    assert result.trusted_status is not None
    assert result.trusted_status.status is PaymentStatus.PENDING
