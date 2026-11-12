"""Regression tests for gateway signature canonicalisation and status mapping."""

from __future__ import annotations

import hashlib
import hmac
import json

from app.db.models import PaymentStatus
from app.services.payments import nowpayments, plisio


def test_nowpayments_signature_covers_unicode_and_whole_floats() -> None:
    # The sender signs JavaScript's JSON.stringify of the key-sorted body: literal Unicode, 1500 not 1500.0.
    signed = '{"order_description":"Пополнение","payment_id":5,"price_amount":1500}'
    secret = "ipn"
    sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha512).hexdigest()
    body = json.loads('{"payment_id":5,"price_amount":1500.0,"order_description":"Пополнение"}')
    assert nowpayments.signature(body, secret) == sig


def test_plisio_mismatch_underpayment_is_not_credited() -> None:
    base = {"status": "mismatch", "source_amount": "199.00", "amount": "2.0"}
    assert plisio.status({**base, "actual_sum": "1.0"}).status is PaymentStatus.PENDING


def test_plisio_mismatch_overpayment_is_credited() -> None:
    result = plisio.status({"status": "mismatch", "source_amount": "199.00", "amount": "2.0", "actual_sum": "2.5"})
    assert result.status is PaymentStatus.SUCCEEDED and result.paid_amount_kop == 19900
