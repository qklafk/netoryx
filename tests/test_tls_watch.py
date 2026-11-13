"""Certificate expiry watchdog: probing a local TLS server, thresholds, daily dedupe, alerts and the admin API."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.db.session import SessionFactory
from app.modules.admin_api.deps import drain_background
from app.modules.system import module as system_module
from app.modules.system.jobs import tls_watch_job
from app.services import notifications, tls_watch
from app.services.tls_watch import Probe
from app.web.app import create_web_app
from tests.conftest import make_settings
from tests.test_mod_personal_support import OWNER, Harness, harnesses
from tests.test_mod_webapi_support import Api, build_ctx

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 13)


def make_cert(directory: Path, *, days: float, name: str = "localhost") -> tuple[Path, Path, datetime]:
    """A self-signed certificate valid for ``days`` from now (negative = already expired)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    not_after = now + timedelta(days=days)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(min(now, not_after) - timedelta(days=30))
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / f"{name}.crt", directory / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path, not_after


async def serve(cert: Path, key: Path, seen_sni: list[str | None] | None = None) -> tuple[asyncio.Server, int]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    if seen_sni is not None:

        def callback(_sock: Any, name: str | None, _ctx: Any) -> None:
            seen_sni.append(name)

        context.sni_callback = callback

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    return server, server.sockets[0].getsockname()[1]


def trusting(cert: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(cert))


async def free_port() -> int:
    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return int(port)


# --- probing ---------------------------------------------------------------------------------------
async def test_probe_reads_not_after_with_sni_and_connect_override(tmp_path: Path) -> None:
    cert, key, not_after = make_cert(tmp_path, days=40)
    sni: list[str | None] = []
    server, port = await serve(cert, key, sni)
    async with server:
        found = await tls_watch.probe("localhost", f"127.0.0.1:{port}", verify_context=trusting(cert))
    assert found.reachable and found.verify_ok and found.error is None
    assert found.not_after is not None and abs((found.not_after - not_after).total_seconds()) < 2
    assert sni == ["localhost"]  # the server sees the watched name, not the connect address


async def test_probe_reports_failed_verification_but_still_reads_the_date(tmp_path: Path) -> None:
    cert, key, _ = make_cert(tmp_path, days=40)
    server, port = await serve(cert, key)
    async with server:
        found = await tls_watch.probe("localhost", f"127.0.0.1:{port}")  # system roots do not know this cert
    assert found.reachable and not found.verify_ok and found.error
    assert found.not_after is not None


async def test_probe_reads_an_expired_certificate(tmp_path: Path) -> None:
    cert, key, _ = make_cert(tmp_path, days=-5)
    server, port = await serve(cert, key)
    async with server:
        found = await tls_watch.probe("localhost", f"127.0.0.1:{port}", verify_context=trusting(cert))
    assert found.reachable and not found.verify_ok and found.not_after is not None
    assert found.not_after < datetime.now(UTC)


async def test_probe_of_an_unreachable_host_does_not_raise() -> None:
    found = await tls_watch.probe("localhost", f"127.0.0.1:{await free_port()}")
    assert not found.reachable and found.not_after is None and found.error


def test_connect_override_parsing() -> None:
    assert tls_watch.parse_connect(None, "a.example") == ("a.example", 443)
    assert tls_watch.parse_connect("edge:443", "a.example") == ("edge", 443)
    assert tls_watch.parse_connect("10.0.0.5:8443", "a.example") == ("10.0.0.5", 8443)
    assert tls_watch.parse_connect("edge", "a.example") == ("edge", 443)


def test_watched_hosts() -> None:
    s = make_settings(public_base_url="https://Bot.Example.com", tls_watch_hosts=["vpn.example.com", "bot.example.com"])
    assert tls_watch.watched_hosts(s) == ["bot.example.com", "vpn.example.com"]
    assert tls_watch.watched_hosts(make_settings(public_base_url="https://203.0.113.9")) == []
    assert tls_watch.watched_hosts(make_settings(public_base_url="http://bot.example.com")) == []
    assert not tls_watch.watch_enabled(make_settings(public_base_url=None))


# --- thresholds, dedupe, recovery ------------------------------------------------------------------
def good(days: float, *, verify: bool = True) -> Probe:
    return Probe("h", reachable=True, not_after=NOW + timedelta(days=days), verify_ok=verify)


def test_thresholds_and_daily_dedupe() -> None:
    state, alert = tls_watch.evaluate(None, good(30), NOW, 14, TODAY)
    assert state.ok and alert is None and state.days_left == 30

    state, alert = tls_watch.evaluate(state, good(10), NOW, 14, TODAY)
    assert alert is not None and alert.kind == "warn" and state.problem == "warn"
    state, alert = tls_watch.evaluate(state, good(10), NOW + timedelta(hours=12), 14, TODAY)
    assert alert is None  # once per day
    state, alert = tls_watch.evaluate(state, good(9), NOW + timedelta(days=1), 14, TODAY + timedelta(days=1))
    assert alert is not None and alert.kind == "warn"


def test_urgent_cases_and_escalation() -> None:
    state, alert = tls_watch.evaluate(None, good(10), NOW, 14, TODAY)
    assert alert is not None and alert.kind == "warn"
    state, alert = tls_watch.evaluate(state, good(2), NOW, 14, TODAY)  # same day, but now urgent
    assert alert is not None and alert.kind == "urgent"
    state, alert = tls_watch.evaluate(state, good(2), NOW, 14, TODAY)
    assert alert is None

    _, alert = tls_watch.evaluate(None, good(-1, verify=False), NOW, 14, TODAY)
    assert alert is not None and alert.kind == "urgent" and alert.days_left == -1
    _, alert = tls_watch.evaluate(None, good(60, verify=False), NOW, 14, TODAY)  # bad chain / wrong name
    assert alert is not None and alert.kind == "urgent"


def test_recovered_message_once() -> None:
    state, _ = tls_watch.evaluate(None, good(2), NOW, 14, TODAY)
    state, alert = tls_watch.evaluate(state, good(80), NOW + timedelta(hours=12), 14, TODAY)
    assert alert is not None and alert.kind == "recovered" and state.ok and state.last_alert_day is None
    state, alert = tls_watch.evaluate(state, good(80), NOW + timedelta(hours=24), 14, TODAY)
    assert alert is None
    _, alert = tls_watch.evaluate(state, good(1), NOW, 14, TODAY)  # a new problem alerts again
    assert alert is not None and alert.kind == "urgent"


def test_unreachable_keeps_the_last_known_expiry_and_alerts_once_a_day() -> None:
    state, _ = tls_watch.evaluate(None, good(30), NOW, 14, TODAY)
    down = Probe("h", error="connection refused")
    state, alert = tls_watch.evaluate(state, down, NOW, 14, TODAY)
    assert alert is not None and alert.kind == "unreachable" and state.days_left == 30 and not state.ok
    _, alert = tls_watch.evaluate(state, down, NOW + timedelta(hours=12), 14, TODAY)
    assert alert is None


# --- job and admin API -----------------------------------------------------------------------------
@pytest.fixture
async def harness_factory(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Callable[..., Harness]]:
    async with harnesses(session_factory, tmp_path) as factory:
        yield factory


async def test_job_alerts_once_a_day_and_survives_an_unreachable_host(
    harness_factory: Callable[..., Harness], tmp_path: Path
) -> None:
    cert, key, _ = make_cert(tmp_path, days=40)
    server, port = await serve(cert, key)
    async with server:
        h = harness_factory(
            system_module,
            public_base_url="https://localhost",
            tls_watch_connect=f"127.0.0.1:{port}",
        )
        for _ in range(2):  # self-signed: verification fails -> one urgent alert, not two
            await tls_watch_job(h.ctx)  # type: ignore[arg-type]
            await notifications.close(h.ctx, drain_seconds=2)  # type: ignore[arg-type]
        alerts = [m for m in h.sent_to(OWNER) if "localhost" in m]
        assert len(alerts) == 1 and "netoryx logs caddy" in alerts[0]


async def test_job_survives_an_unreachable_host_with_one_warning(harness_factory: Callable[..., Harness]) -> None:
    dead = await free_port()
    h2 = harness_factory(system_module, tls_watch_hosts=["down.example"], tls_watch_connect=f"127.0.0.1:{dead}")
    for _ in range(2):
        await tls_watch_job(h2.ctx)  # type: ignore[arg-type]
        await notifications.close(h2.ctx, drain_seconds=2)  # type: ignore[arg-type]
    down = [m for m in h2.sent_to(OWNER) if "down.example" in m]
    assert len(down) == 1


async def test_admin_api_lists_the_status(tmp_path: Path) -> None:
    cert, key, _ = make_cert(tmp_path, days=40)
    server, port = await serve(cert, key)
    ctx, engine = await build_ctx(
        tmp_path,
        public_base_url="https://localhost",
        tls_watch_hosts=["other.example"],
        tls_watch_connect=f"127.0.0.1:{port}",
    )
    try:
        async with server:
            app = create_web_app(ctx)  # type: ignore[arg-type]
            transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 50000))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                api = await Api(ctx, client).login()
                before = (await api.get("/system/tls")).json()
                assert before["enabled"] and [h["host"] for h in before["hosts"]] == ["localhost", "other.example"]
                assert before["hosts"][0]["checked_at"] is None
                await tls_watch.run_check(ctx.settings, ctx.session_factory)
                data = (await api.get("/system/tls")).json()
        by_host = {h["host"]: h for h in data["hosts"]}
        assert by_host["localhost"]["days_left"] in (39, 40) and by_host["localhost"]["ok"] is False  # self-signed
        assert by_host["localhost"]["problem"] == "urgent" and by_host["localhost"]["error"]
        assert data["warn_days"] == 14
    finally:
        await drain_background()
        await engine.dispose()
