"""Remnawave webhook signature verification and parsing.

``tests/integration/remnawave/captured_webhooks.jsonl`` holds real deliveries captured from a local
Remnawave 3.4.8 (throwaway container; the secret below is that container's test-only
``WEBHOOK_SECRET_HEADER``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.services.panels.remnawave import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    RemnawaveBackend,
    RemnawaveWebhookError,
)

SECRET = "vsmu67Kmg6R8FjIOF1WUY8LWBHie4scdEqrfsKmyf4IAf8dY3nFS0wwYHkhh6ZvQ"
CAPTURED = Path(__file__).parent / "integration" / "remnawave" / "captured_webhooks.jsonl"


def _captured() -> list[dict[str, Any]]:
    return [json.loads(line) for line in CAPTURED.read_text(encoding="utf-8").splitlines() if line.strip()]


def _by_event(event: str) -> dict[str, Any]:
    return next(c for c in _captured() if json.loads(c["body"])["event"] == event)


def _sign(raw: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_header_names() -> None:
    assert SIGNATURE_HEADER == "X-Remnawave-Signature"
    assert TIMESTAMP_HEADER == "X-Remnawave-Timestamp"


def test_captured_corpus_is_present_and_varied() -> None:
    events = {json.loads(c["body"])["event"] for c in _captured()}
    assert {"user.created", "user.disabled", "user.expired", "user_hwid_devices.added"} <= events


@pytest.mark.parametrize("capture", _captured(), ids=lambda c: json.loads(c["body"])["event"])
def test_captured_signatures_verify(capture: dict[str, Any]) -> None:
    raw = capture["body"].encode("utf-8")
    assert RemnawaveBackend.verify_webhook(raw, capture["signature"], SECRET) is True


def test_signature_is_case_and_whitespace_tolerant() -> None:
    capture = _by_event("user.created")
    sig = f"  {capture['signature'].upper()}\n"
    assert RemnawaveBackend.verify_webhook(capture["body"].encode(), sig, SECRET)


@pytest.mark.parametrize(
    ("mutate", "secret"),
    [
        (lambda raw, sig: (raw + b" ", sig), SECRET),  # body changed after signing
        (lambda raw, sig: (raw.replace(b'"ACTIVE"', b'"LIMITED"'), sig), SECRET),
        (lambda raw, sig: (raw, sig[:-1] + ("0" if sig[-1] != "0" else "1")), SECRET),  # one hex digit off
        (lambda raw, sig: (raw, sig[:32]), SECRET),  # truncated
        (lambda raw, sig: (raw, sig), "another-secret"),
        (lambda raw, sig: (raw, ""), SECRET),
        (lambda raw, sig: (raw, None), SECRET),
        (lambda raw, sig: (raw, sig), ""),  # no secret configured -> never trust
        (lambda raw, sig: (raw, "zz" * 32), SECRET),  # not hex
        (lambda raw, sig: (raw, "подпись"), SECRET),  # non-ASCII must not raise
    ],
)
def test_bad_signatures_rejected(mutate: Any, secret: str) -> None:
    capture = _by_event("user.created")
    raw, sig = mutate(capture["body"].encode(), capture["signature"])
    assert RemnawaveBackend.verify_webhook(raw, sig, secret) is False


def test_verify_matches_reference_hmac() -> None:
    raw = b'{"scope":"user","event":"user.expired"}'
    assert RemnawaveBackend.verify_webhook(raw, _sign(raw, "s3cr3t"), "s3cr3t")


def test_parse_user_event() -> None:
    capture = _by_event("user.created")
    ev = RemnawaveBackend.parse_webhook(capture["body"].encode())
    assert ev.event == "user.created"
    assert ev.scope == "user"
    assert ev.user_id == 2
    assert ev.username == "tg_123456789"
    assert ev.telegram_id == 123456789
    assert ev.data["vlessUuid"] == "0b9a3c0e-5f4e-4c7a-9a4b-2c1d3e4f5a6b"
    assert ev.timestamp == datetime(2026, 11, 3, 20, 50, 12, 458000, tzinfo=UTC)
    assert ev.meta is None


def test_parse_expired_event_from_str() -> None:
    ev = RemnawaveBackend.parse_webhook(_by_event("user.expired")["body"])
    assert ev.event == "user.expired" and ev.user_id is not None and ev.username


def test_parse_hwid_event_uses_nested_user() -> None:
    ev = RemnawaveBackend.parse_webhook(_by_event("user_hwid_devices.added")["body"].encode())
    assert ev.scope == "user_hwid_devices"
    assert ev.user_id == ev.data["user"]["id"]
    assert ev.username == ev.data["user"]["username"]
    assert "hwid" in ev.data["hwidUserDevice"]


def test_parse_service_event_has_no_user() -> None:
    ev = RemnawaveBackend.parse_webhook(_by_event("service.panel_started")["body"].encode())
    assert ev.scope == "service"
    assert ev.user_id is None and ev.username is None and ev.telegram_id is None
    assert ev.data == {"panelVersion": "3.4.8"}


def test_parse_node_event_and_meta() -> None:
    raw = json.dumps(
        {
            "scope": "node",
            "event": "node.connection_lost",
            "timestamp": "2026-11-03T21:00:00.000Z",
            "data": {"uuid": "n1", "name": "DE-1"},
            "meta": {"notConnectedAfterHours": 1},
        }
    )
    ev = RemnawaveBackend.parse_webhook(raw)
    assert ev.event == "node.connection_lost" and ev.scope == "node"
    assert ev.user_id is None
    assert ev.meta == {"notConnectedAfterHours": 1}


def test_parse_tolerates_missing_scope_bad_timestamp_and_odd_ids() -> None:
    raw = json.dumps(
        {
            "event": "user.limited",
            "timestamp": "yesterday",
            "data": {"id": "17", "username": "tg_17", "telegramId": True},
        }
    )
    ev = RemnawaveBackend.parse_webhook(raw)
    assert ev.scope == "user"  # derived from the event name
    assert ev.timestamp is None
    assert ev.user_id == 17
    assert ev.telegram_id is None  # booleans are not ids


def test_parse_non_object_data() -> None:
    ev = RemnawaveBackend.parse_webhook('{"scope":"user","event":"user.modified","data":[1,2]}')
    assert ev.data == {} and ev.user_id is None


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[]", b'"user.created"', b"{}", b'{"event": ""}', b'{"event": 5}', b"\xff\xfe"],
)
def test_parse_rejects_malformed(raw: bytes) -> None:
    with pytest.raises(RemnawaveWebhookError):
        RemnawaveBackend.parse_webhook(raw)


def test_webhook_error_is_value_error() -> None:
    assert issubclass(RemnawaveWebhookError, ValueError)
