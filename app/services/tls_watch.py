"""TLS certificate watchdog: read the expiry of the public certificates and decide which alerts to send."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import ssl
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from cryptography import x509

from app.db.session import read_session
from app.services import kv

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory

log = logging.getLogger(__name__)

STATE_KEY = "tls_watch"
CONNECT_TIMEOUT_SEC = 10.0
URGENT_DAYS = 3


@dataclass(slots=True)
class Probe:
    """What one connection to a watched name showed."""

    host: str
    reachable: bool = False
    not_after: datetime | None = None
    verify_ok: bool = False
    error: str | None = None


@dataclass(slots=True)
class HostState:
    """Persisted status of a watched name (also served to the admin API)."""

    host: str
    checked_at: str = ""
    expires_at: str | None = None
    days_left: int | None = None
    ok: bool = True
    problem: str | None = None  # None | "warn" | "urgent" | "unreachable"
    error: str | None = None
    last_alert_day: str | None = None
    last_alert_level: str | None = None

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("last_alert_day")
        data.pop("last_alert_level")
        return data


@dataclass(frozen=True, slots=True)
class Alert:
    host: str
    kind: str  # "warn" | "urgent" | "unreachable" | "recovered"
    days_left: int | None
    expires_at: datetime | None
    error: str | None


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def watched_hosts(settings: Settings) -> list[str]:
    """Names to check: the PUBLIC_BASE_URL host (when HTTPS and not an IP) and ``TLS_WATCH_HOSTS``."""
    names: list[str] = []
    base = urlsplit(settings.public_base_url or "")
    if base.scheme == "https" and base.hostname and not _is_ip(base.hostname):
        names.append(base.hostname)
    names.extend(settings.tls_watch_hosts)
    unique: dict[str, None] = {}
    for name in names:
        host = name.strip().lower().rstrip(".")
        if host:
            unique[host] = None
    return list(unique)


def watch_enabled(settings: Settings) -> bool:
    return bool(watched_hosts(settings))


def parse_connect(value: str | None, host: str) -> tuple[str, int]:
    """(address, port) to connect to for ``host`` under the ``TLS_WATCH_CONNECT`` override."""
    if not value or not value.strip():
        return host, 443
    name, sep, port = value.strip().rpartition(":")
    if sep and port.isdigit():
        return name.strip("[]") or host, int(port)
    return value.strip(), 443


def not_after_of(der: bytes) -> datetime:
    """The ``notAfter`` of a DER certificate as an aware UTC datetime."""
    return x509.load_der_x509_certificate(der).not_valid_after_utc.astimezone(UTC)


async def _handshake(address: str, port: int, host: str, context: ssl.SSLContext) -> bytes:
    _reader, writer = await asyncio.wait_for(
        asyncio.open_connection(address, port, ssl=context, server_hostname=host),
        timeout=CONNECT_TIMEOUT_SEC,
    )
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        if not der:
            raise ssl.SSLError("the server sent no certificate")
        return bytes(der)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=3)


def _unverified_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def probe(host: str, connect: str | None = None, *, verify_context: ssl.SSLContext | None = None) -> Probe:
    """Connect to ``host`` (SNI = ``host``) and read the certificate; never raises.

    The certificate is read even when it is expired or untrusted; ``verify_ok`` tells whether a normal
    verifying handshake succeeds.
    """
    address, port = parse_connect(connect, host)
    result = Probe(host=host)
    try:
        try:
            der = await _handshake(address, port, host, verify_context or ssl.create_default_context())
            result.verify_ok = True
        except ssl.SSLCertVerificationError as exc:
            result.error = exc.verify_message or str(exc)
            der = await _handshake(address, port, host, _unverified_context())
        except ssl.SSLError as exc:
            result.error = str(exc)
            der = await _handshake(address, port, host, _unverified_context())
        result.reachable = True
        result.not_after = not_after_of(der)
    except (TimeoutError, OSError, ssl.SSLError, ValueError) as exc:
        result.reachable = False
        result.verify_ok = False
        result.error = str(exc) or type(exc).__name__
    return result


def _days_left(not_after: datetime, now: datetime) -> int:
    return int((not_after - now).total_seconds() // 86400)


def evaluate(
    prev: HostState | None, found: Probe, now: datetime, warn_days: int, today: date
) -> tuple[HostState, Alert | None]:
    """New state of a host after a probe and the alert to send (one per day; urgent ones escalate)."""
    state = HostState(host=found.host, checked_at=now.isoformat())
    if prev is not None:
        state.last_alert_day, state.last_alert_level = prev.last_alert_day, prev.last_alert_level
    if found.not_after is not None:
        state.expires_at = found.not_after.isoformat()
        state.days_left = _days_left(found.not_after, now)
    elif prev is not None and prev.expires_at:  # unreachable: keep the last known expiry for display
        state.expires_at = prev.expires_at
        state.days_left = _days_left(datetime.fromisoformat(prev.expires_at), now)
    state.error = found.error

    level: str | None
    if not found.reachable:
        level = "unreachable"
    elif state.days_left is not None and (state.days_left < URGENT_DAYS or not found.verify_ok):
        level = "urgent"
    elif state.days_left is not None and state.days_left < warn_days:
        level = "warn"
    else:
        level = None
    state.problem = level
    state.ok = level is None
    if level is None:
        state.error = None
        state.last_alert_day = state.last_alert_level = None
        was_bad = prev is not None and prev.problem is not None
        return state, Alert(found.host, "recovered", state.days_left, found.not_after, None) if was_bad else None

    already = state.last_alert_day == today.isoformat() and (
        state.last_alert_level == level or (state.last_alert_level == "urgent" and level == "warn")
    )
    if already:
        return state, None
    state.last_alert_day, state.last_alert_level = today.isoformat(), level
    return state, Alert(found.host, level, state.days_left, found.not_after, found.error)


async def load_states(session_factory: SessionFactory) -> dict[str, HostState]:
    async with read_session(session_factory) as session:
        raw = await kv.get(session, STATE_KEY, fresh=True)
    states: dict[str, HostState] = {}
    if raw:
        try:
            for host, item in json.loads(raw).items():
                states[host] = HostState(**{k: v for k, v in item.items() if k in HostState.__dataclass_fields__})
        except (ValueError, TypeError, AttributeError):
            log.warning("tls watch: the stored state is unreadable, starting over")
    return states


async def save_states(session_factory: SessionFactory, states: dict[str, HostState]) -> None:
    payload = json.dumps({host: asdict(state) for host, state in states.items()})
    async with session_factory.begin() as session:
        await kv.set(session, STATE_KEY, payload)


async def run_check(
    settings: Settings,
    session_factory: SessionFactory,
    *,
    now: datetime | None = None,
    verify_context: ssl.SSLContext | None = None,
) -> list[Alert]:
    """Probe every watched name (no transaction is open while connecting), store the state, return the alerts."""
    hosts = watched_hosts(settings)
    moment = now or datetime.now(UTC)
    previous = await load_states(session_factory)
    probes = await asyncio.gather(*(probe(h, settings.tls_watch_connect, verify_context=verify_context) for h in hosts))
    states: dict[str, HostState] = {}
    alerts: list[Alert] = []
    today = moment.astimezone(settings.zone).date()
    for found in probes:
        state, alert = evaluate(previous.get(found.host), found, moment, settings.tls_warn_days, today)
        states[found.host] = state
        if alert is not None:
            alerts.append(alert)
    await save_states(session_factory, states)
    return alerts
