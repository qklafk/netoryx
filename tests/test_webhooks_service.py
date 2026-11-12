"""Outgoing webhooks: outbox enqueue, filters, signatures, delivery, retries, auto-disable, SSRF checks."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from sqlalchemy import func, select

from app.db.models import Event, WebhookDelivery, WebhookDeliveryStatus, WebhookEndpoint
from app.db.session import SessionFactory
from app.services import events, webhooks
from app.utils.crypto import SecretBox
from app.utils.time import utcnow

BOX = SecretBox("webhook-test-key")


class Recorder:
    """A fake sender returning scripted outcomes and remembering requests."""

    def __init__(self, *outcomes: webhooks.Outcome) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[tuple[str, bytes, dict[str, str]]] = []

    async def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> webhooks.Outcome:
        self.requests.append((url, body, dict(headers)))
        if self.outcomes:
            return self.outcomes.pop(0)
        return webhooks.Outcome(True, 200)


async def _endpoint(
    factory: SessionFactory, url: str = "https://crm.example.com/hook", events_filter: Any = None
) -> tuple[str, int]:
    async with factory.begin() as s:
        secret, row = await webhooks.create_endpoint(s, BOX, url, events=events_filter)
        return secret, row.id


async def _deliveries(factory: SessionFactory) -> list[WebhookDelivery]:
    async with factory() as s:
        return list((await s.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id))).all())


# --- filters, secrets, signatures, payloads ------------------------------------------------------------------
def test_filter_matching_and_normalization() -> None:
    assert webhooks.normalize_events(None) == "*"
    assert webhooks.normalize_events(["purchase", " renewal ", "purchase"]) == "purchase,renewal"
    assert webhooks.normalize_events(["purchase", "*"]) == "*"
    assert webhooks.matches("*", "anything")
    assert webhooks.matches("purchase,payment.*", "payment.succeeded")
    assert webhooks.matches("purchase,payment.*", "purchase")
    assert not webhooks.matches("purchase,payment.*", "renewal")
    assert not webhooks.matches("payment.*", "paymentx")
    for bad in (["pay*ment"], ["bad type"], ["x" * 60]):
        with pytest.raises(webhooks.WebhookError):
            webhooks.normalize_events(bad)


def test_signature_and_payload() -> None:
    body = b'{"a":1}'
    signature = webhooks.sign("whsec_x", 1700000000, body)
    assert signature.startswith("sha256=") and len(signature) == 7 + 64
    assert webhooks.verify("whsec_x", "1700000000", body, signature)
    assert not webhooks.verify("whsec_y", 1700000000, body, signature)
    assert not webhooks.verify("whsec_x", 1700000001, body, signature)
    payload = json.loads(
        webhooks.build_payload(
            "evt_1",
            "purchase",
            utcnow(),
            5,
            100,
            {"plan_id": 1, "sub_token": "s", "api_key": "k", "link": "vless://", "nested": {"password": "p", "x": 1}},
        )
    )
    assert set(payload) == {"id", "type", "created_at", "user_id", "amount_kop", "data"}
    assert payload["data"] == {"plan_id": 1, "nested": {"x": 1}}
    assert webhooks.generate_secret().startswith("whsec_")


# --- enqueue -------------------------------------------------------------------------------------------------
async def test_track_enqueues_in_same_transaction(session_factory: SessionFactory) -> None:
    await _endpoint(session_factory, events_filter=["purchase"])
    _, second = await _endpoint(session_factory, "https://other.example.com/x", events_filter=["payment.*"])
    async with session_factory.begin() as s:
        await events.track(s, "purchase", user_id=7, amount_kop=19900, plan_id=3)
        await events.track(s, "user.registered", user_id=7)
        await events.track(s, "payment.succeeded", user_id=7, amount_kop=19900, provider="stars")
    rows = await _deliveries(session_factory)
    assert [(r.event_type, r.status) for r in rows] == [
        ("purchase", WebhookDeliveryStatus.PENDING),
        ("payment.succeeded", WebhookDeliveryStatus.PENDING),
    ]
    assert rows[1].endpoint_id == second
    body = json.loads(rows[0].payload)
    assert body["user_id"] == 7 and body["amount_kop"] == 19900 and body["data"] == {"plan_id": 3}
    assert body["id"] == rows[0].event_id and body["id"].startswith("evt_")

    # Rolled back business transaction: no event, no delivery.
    with pytest.raises(RuntimeError):
        async with session_factory.begin() as s:
            await events.track(s, "purchase", user_id=8)
            raise RuntimeError("boom")
    assert len(await _deliveries(session_factory)) == 2
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Event).where(Event.user_id == 8)) == 0


async def test_cache_invalidated_on_changes(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await events.track(s, "purchase", user_id=1)  # caches "no endpoints"
    _, endpoint_id = await _endpoint(session_factory)
    async with session_factory.begin() as s:
        await events.track(s, "purchase", user_id=1)
    assert len(await _deliveries(session_factory)) == 1
    async with session_factory.begin() as s:
        row = await s.get(WebhookEndpoint, endpoint_id)
        assert row is not None
        webhooks.update_endpoint(row, is_active=False)
    async with session_factory.begin() as s:
        await events.track(s, "purchase", user_id=1)
    assert len(await _deliveries(session_factory)) == 1


# --- delivery ------------------------------------------------------------------------------------------------
async def test_delivery_headers_and_success(session_factory: SessionFactory) -> None:
    secret, endpoint_id = await _endpoint(session_factory)
    async with session_factory.begin() as s:
        await events.track(s, "renewal", user_id=3, amount_kop=500)
    sender = Recorder()
    run = await webhooks.deliver(session_factory, BOX, sender=sender)
    assert run.sent == 1 and len(sender.requests) == 1
    url, body, headers = sender.requests[0]
    assert url == "https://crm.example.com/hook"
    assert headers["X-Webhook-Event"] == "renewal"
    assert headers["X-Webhook-Id"] == json.loads(body)["id"]
    assert webhooks.verify(secret, headers["X-Webhook-Timestamp"], body, headers["X-Webhook-Signature"])
    [row] = await _deliveries(session_factory)
    assert row.status is WebhookDeliveryStatus.SENT and row.attempts == 1 and row.sent_at is not None
    assert row.last_status_code == 200
    # Nothing left to send.
    assert (await webhooks.deliver(session_factory, BOX, sender=sender)).sent == 0
    async with session_factory() as s:
        endpoint = await s.get(WebhookEndpoint, endpoint_id)
        assert endpoint is not None and endpoint.last_success_at is not None


async def test_retry_backoff_dead_and_auto_disable(session_factory: SessionFactory) -> None:
    _, endpoint_id = await _endpoint(session_factory)
    async with session_factory.begin() as s:
        await events.track(s, "purchase", user_id=1)
        await events.track(s, "purchase", user_id=2)
    failing = webhooks.Outcome(False, 500, "HTTP 500")
    sender = Recorder(*([failing] * 100))
    now = utcnow()
    for attempt in range(1, webhooks.MAX_ATTEMPTS + 1):
        run = await webhooks.deliver(session_factory, BOX, sender=sender, now=now, auto_disable_after=2)
        assert run.sent == 0 and run.failed + run.dead == 2
        rows = await _deliveries(session_factory)
        assert all(r.attempts == attempt for r in rows)
        if attempt < webhooks.MAX_ATTEMPTS:
            delay = webhooks.BACKOFF[attempt - 1]
            assert all(r.status is WebhookDeliveryStatus.FAILED for r in rows)
            assert all(r.next_attempt_at == now + delay for r in rows)
            assert all(r.last_error == "HTTP 500" and r.last_status_code == 500 for r in rows)
            # Not due yet.
            assert (await webhooks.deliver(session_factory, BOX, sender=sender, now=now)).failed == 0
            now += delay
        else:
            assert run.dead == 2
            assert all(r.status is WebhookDeliveryStatus.DEAD for r in rows)
            assert [d.id for d in run.disabled] == [endpoint_id]
            assert run.disabled[0].host == "crm.example.com" and run.disabled[0].failures == 2
    async with session_factory() as s:
        endpoint = await s.get(WebhookEndpoint, endpoint_id)
        assert endpoint is not None and not endpoint.is_active and endpoint.disabled_reason
        assert endpoint.consecutive_dead == 2 and endpoint.consecutive_failures == 2 * webhooks.MAX_ATTEMPTS

    # Disabled endpoints get neither new deliveries nor sends; turning it back on resets the counters.
    async with session_factory.begin() as s:
        await events.track(s, "purchase", user_id=3)
    assert len(await _deliveries(session_factory)) == 2
    async with session_factory.begin() as s:
        endpoint = await s.get(WebhookEndpoint, endpoint_id)
        assert endpoint is not None
        webhooks.update_endpoint(endpoint, is_active=True)
        assert endpoint.consecutive_dead == 0 and endpoint.disabled_reason is None
        delivery = await s.get(WebhookDelivery, 1)
        assert delivery is not None
        webhooks.reset_for_redelivery(delivery)
    run = await webhooks.deliver(session_factory, BOX, sender=Recorder(), delivery_id=1)
    assert run.sent == 1 and run.outcomes[1].ok
    rows = await _deliveries(session_factory)
    assert rows[0].status is WebhookDeliveryStatus.SENT and rows[1].status is WebhookDeliveryStatus.DEAD


async def test_auto_disable_off(session_factory: SessionFactory) -> None:
    _, endpoint_id = await _endpoint(session_factory)
    async with session_factory.begin() as s:
        await events.track(s, "purchase", user_id=1)
    now = utcnow()
    for _ in range(webhooks.MAX_ATTEMPTS):
        await webhooks.deliver(
            session_factory, BOX, sender=Recorder(webhooks.Outcome(False, error="x")), now=now, auto_disable_after=0
        )
        now += timedelta(days=2)
    async with session_factory() as s:
        endpoint = await s.get(WebhookEndpoint, endpoint_id)
        assert endpoint is not None and endpoint.is_active


async def test_concurrent_claims_do_not_overlap(session_factory: SessionFactory) -> None:
    await _endpoint(session_factory)
    async with session_factory.begin() as s:
        for i in range(30):
            await events.track(s, "purchase", user_id=i)
    claims = await asyncio.gather(*(webhooks.claim(session_factory, limit=10) for _ in range(5)))
    ids = [job.delivery_id for jobs in claims for job in jobs]
    assert len(ids) == len(set(ids)) == 30
    # Claimed rows are leased: nothing is due until the lease runs out.
    assert await webhooks.claim(session_factory) == []
    later = await webhooks.claim(session_factory, now=utcnow() + webhooks.CLAIM_LEASE + timedelta(seconds=1))
    assert len(later) == 30 and all(job.attempts == 2 for job in later)


async def test_stale_claim_result_is_ignored(session_factory: SessionFactory) -> None:
    await _endpoint(session_factory)
    async with session_factory.begin() as s:
        await events.track(s, "purchase", user_id=1)
    [job] = await webhooks.claim(session_factory)
    async with session_factory.begin() as s:
        delivery = await s.get(WebhookDelivery, job.delivery_id)
        assert delivery is not None
        webhooks.reset_for_redelivery(delivery)
    run = webhooks.RunResult()
    await webhooks._record(
        session_factory, [(job, webhooks.Outcome(True, 200))], auto_disable_after=3, now=utcnow(), run=run
    )
    assert run.sent == 0
    [row] = await _deliveries(session_factory)
    assert row.status is WebhookDeliveryStatus.PENDING and row.attempts == 0


async def test_purge_keeps_recent_and_in_flight(session_factory: SessionFactory) -> None:
    await _endpoint(session_factory)
    async with session_factory.begin() as s:
        await events.track(s, "purchase", user_id=1)
        await events.track(s, "purchase", user_id=2)
    await webhooks.deliver(session_factory, BOX, sender=Recorder(webhooks.Outcome(True, 200)), limit=1)
    assert await webhooks.purge(session_factory, 14) == 0
    assert await webhooks.purge(session_factory, 14, now=utcnow() + timedelta(days=15)) == 1
    assert await webhooks.purge(session_factory, 14, now=utcnow() + timedelta(days=30)) == 1
    assert await _deliveries(session_factory) == []


async def test_undecryptable_secret_fails_without_sending(session_factory: SessionFactory) -> None:
    await _endpoint(session_factory)
    async with session_factory.begin() as s:
        await events.track(s, "purchase", user_id=1)
    sender = Recorder()
    run = await webhooks.deliver(session_factory, SecretBox("another-key"), sender=sender)
    assert run.failed == 1 and sender.requests == []
    [row] = await _deliveries(session_factory)
    assert row.last_error is not None and "secret" in row.last_error


# --- SSRF ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("address", "blocked"),
    [
        ("10.0.0.1", True),
        ("192.168.1.1", True),
        ("172.16.0.5", True),
        ("127.0.0.1", True),
        ("169.254.169.254", True),
        ("100.64.0.1", True),
        ("0.0.0.0", True),
        ("::1", True),
        ("fe80::1", True),
        ("fd00::1", True),
        ("::ffff:10.0.0.1", True),
        ("224.0.0.1", True),
        ("8.8.8.8", False),
        ("2606:4700:4700::1111", False),
        ("not-an-ip", True),
    ],
)
def test_blocked_addresses(address: str, blocked: bool) -> None:
    assert webhooks.is_blocked_ip(address) is blocked


def test_url_validation() -> None:
    assert webhooks.validate_url(" https://crm.example.com/x?y=1 ") == "https://crm.example.com/x?y=1"
    for bad in (
        "http://crm.example.com/x",
        "ftp://crm.example.com",
        "https://user:pass@crm.example.com/",
        "https:///nohost",
        "https://crm.example.com/#frag",
        "https://" + "a" * 1100 + ".com",
        "",
    ):
        with pytest.raises(webhooks.WebhookError):
            webhooks.validate_url(bad)
    with pytest.raises(webhooks.WebhookError):
        webhooks.validate_url("http://crm.example.com/x", dev_mode=True)
    assert webhooks.validate_url("http://localhost:8080/x", dev_mode=True)


async def test_check_destination_refuses_private_hosts() -> None:
    async def private(host: str, port: int) -> list[str]:
        return ["8.8.8.8", "10.1.2.3"]

    async def public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def broken(host: str, port: int) -> list[str]:
        raise OSError("nxdomain")

    with pytest.raises(webhooks.WebhookError) as err:
        await webhooks.check_destination("https://intranet.example.com/", resolve=private)
    assert err.value.code == "blocked_address"
    with pytest.raises(webhooks.WebhookError):
        await webhooks.check_destination("https://169.254.169.254/latest/meta-data")
    with pytest.raises(webhooks.WebhookError) as err:
        await webhooks.check_destination("https://nowhere.example.com/", resolve=broken)
    assert err.value.code == "unresolvable"
    await webhooks.check_destination("https://crm.example.com/", resolve=public)
    await webhooks.check_destination("https://10.0.0.1/", dev_mode=True)


class _StaticResolver(AbstractResolver):
    def __init__(self, address: str) -> None:
        self.address = address

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        return [{"hostname": host, "host": self.address, "port": port, "family": family, "proto": 0, "flags": 0}]

    async def close(self) -> None:
        return None


async def test_safe_resolver_rechecks_at_connect_time() -> None:
    with pytest.raises(webhooks.BlockedAddressError):
        await webhooks.SafeResolver(_StaticResolver("192.168.0.10")).resolve("crm.example.com", 443)
    ok = await webhooks.SafeResolver(_StaticResolver("93.184.216.34")).resolve("crm.example.com", 443)
    assert ok[0]["host"] == "93.184.216.34"
    allowed = await webhooks.SafeResolver(_StaticResolver("127.0.0.1"), allow_private=True).resolve("localhost", 80)
    assert allowed[0]["host"] == "127.0.0.1"


async def test_http_sender_refuses_private_literal_ip() -> None:
    async with webhooks.HttpSender() as sender:
        outcome = await sender("https://127.0.0.1:9/x", b"{}", {})
    assert not outcome.ok and outcome.error and "private" in outcome.error


async def test_http_sender_end_to_end_in_dev_mode(unused_tcp_port: int) -> None:
    received: list[tuple[bytes, dict[str, str]]] = []

    async def hook(request: web.Request) -> web.StreamResponse:
        received.append((await request.read(), dict(request.headers)))
        if request.path == "/redirect":
            raise web.HTTPFound("https://example.com/")
        return web.Response(text="x" * 100_000)

    app = web.Application()
    app.router.add_post("/hook", hook)
    app.router.add_post("/redirect", hook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        async with webhooks.HttpSender(dev_mode=True) as sender:
            ok = await sender(f"http://localhost:{unused_tcp_port}/hook", b'{"a":1}', {"X-Webhook-Event": "t"})
            redirect = await sender(f"http://localhost:{unused_tcp_port}/redirect", b"{}", {})
    finally:
        await runner.cleanup()
    assert ok.ok and ok.status_code == 200
    assert not redirect.ok and redirect.status_code == 302 and len(received) == 2
    assert received[0][0] == b'{"a":1}' and received[0][1]["X-Webhook-Event"] == "t"
