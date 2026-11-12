"""Outgoing webhooks: signed copies of business events sent to external URLs (CRMs, analytics, integrations).

Delivery is a transactional outbox. :func:`enqueue_event` runs inside the transaction that records the event
(:func:`app.services.events.track`) and only inserts ``webhook_deliveries`` rows, so a rolled-back business
transaction never produces a webhook. A background job claims due rows, posts them outside any transaction and
records the outcome; failures are retried with exponential backoff and finally marked ``dead``.

Every request carries ``X-Webhook-Id``, ``X-Webhook-Event``, ``X-Webhook-Timestamp`` and
``X-Webhook-Signature: sha256=<hex HMAC-SHA256 of "{timestamp}.{body}" with the endpoint secret>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WebhookDelivery, WebhookDeliveryStatus, WebhookEndpoint
from app.db.session import SessionFactory
from app.utils.crypto import SecretBox
from app.utils.security import random_token
from app.utils.time import utcnow

log = logging.getLogger(__name__)

# Retry delays after the 1st, 2nd, ... failed attempt; the attempt after the last delay is the final one.
BACKOFF: tuple[timedelta, ...] = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
    timedelta(hours=24),
)
MAX_ATTEMPTS = len(BACKOFF) + 1
REQUEST_TIMEOUT_SEC = 10.0
RESPONSE_READ_LIMIT = 4096  # bytes of the response body read (and discarded) before closing
CLAIM_LEASE = timedelta(minutes=5)  # a claimed row becomes due again if its worker dies
BATCH_SIZE = 50
CONCURRENCY = 5
CACHE_TTL_SEC = 30.0  # endpoints changed by another process are picked up within this time
MAX_URL_LENGTH = 1024
MAX_ERROR_LENGTH = 255
SECRET_PREFIX = "whsec_"
TEST_EVENT = "webhook.test"
ALL_EVENTS = "*"
USER_AGENT = "vpn-bot-webhooks/1.0"

# Event types that can be subscribed to (the filter also accepts "*" and "<prefix>.*").
EVENT_TYPES: tuple[str, ...] = (
    "user.registered",
    "consent.accepted",
    "trial.started",
    "purchase",
    "renewal",
    "subscription.granted",
    "subscription.expired",
    "subscription.auto_renew",
    "subscription.link_rotated",
    "payment.succeeded",
    "payment.refunded",
    "payment.held",
    "payment.repeat",
    "payment.amount_mismatch",
    "promo.redeemed",
    "gift.purchased",
    "gift.redeemed",
    "referral.applied",
    "referral.reward",
    "partner.transfer",
    "withdrawal.requested",
    "withdrawal.paid",
    "invite.redeemed",
    "ticket.created",
)

# Keys of event data that are never sent, matched as substrings of the lower-cased key without "_" and "-".
_HIDDEN_MARKERS = (
    "secret",
    "token",
    "password",
    "apikey",
    "credential",
    "privatekey",
    "link",
    "url",
    "uri",
    "uuid",
    "subid",
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class WebhookError(ValueError):
    """An endpoint setting was rejected; ``code`` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BlockedAddressError(OSError):
    """The destination resolves to an address webhooks may not reach (private, loopback, link-local, ...)."""


# ---------------------------------------------------------------------------------------------------------------
# Event filters, secrets, signatures, payloads
# ---------------------------------------------------------------------------------------------------------------
def normalize_events(events: Iterable[str] | str | None) -> str:
    """Comma-separated filter string from a list (``"*"`` when empty or when ``"*"`` is included)."""
    items = events.split(",") if isinstance(events, str) else list(events or [])
    cleaned: list[str] = []
    for raw in items:
        item = str(raw).strip()
        if not item:
            continue
        if item == ALL_EVENTS:
            return ALL_EVENTS
        if len(item) > 48 or not all(c.isalnum() or c in "._*" for c in item):
            raise WebhookError("invalid_event", f"invalid event type: {item[:60]!r}")
        if "*" in item and not item.endswith(".*"):
            raise WebhookError("invalid_event", f"wildcards are only allowed as '<prefix>.*': {item!r}")
        if item not in cleaned:
            cleaned.append(item)
    if not cleaned:
        return ALL_EVENTS
    value = ",".join(cleaned)
    if len(value) > 1024:
        raise WebhookError("invalid_event", "too many event types")
    return value


def event_list(value: str) -> list[str]:
    return [item for item in value.split(",") if item]


def matches(filter_value: str, event_type: str) -> bool:
    """Whether an endpoint filter (``"*"``, exact types or ``"<prefix>.*"``) selects ``event_type``."""
    for item in event_list(filter_value):
        if item in (ALL_EVENTS, event_type):
            return True
        if item.endswith(".*") and event_type.startswith(item[:-1]):
            return True
    return False


def generate_secret() -> str:
    return SECRET_PREFIX + random_token(32)


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    """``sha256=<hex>`` HMAC-SHA256 of ``"{timestamp}.{body}"``."""
    message = str(timestamp).encode("ascii") + b"." + body
    return "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: int | str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


def new_event_id() -> str:
    return "evt_" + random_token(18)


def _hidden(key: str) -> bool:
    normalized = key.lower().replace("_", "").replace("-", "")
    return any(marker in normalized for marker in _HIDDEN_MARKERS)


def _clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items() if not _hidden(str(k))}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def build_payload(
    event_id: str,
    event_type: str,
    created_at: datetime,
    user_id: int | None,
    amount_kop: int | None,
    data: Mapping[str, Any] | None,
) -> str:
    """The JSON body sent for an event: ``{id, type, created_at, user_id, amount_kop, data}``."""
    body = {
        "id": event_id,
        "type": event_type,
        "created_at": created_at.isoformat(),
        "user_id": user_id,
        "amount_kop": amount_kop,
        "data": _clean(dict(data or {})),
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------------------------------------------
# Destination checks (SSRF protection)
# ---------------------------------------------------------------------------------------------------------------
def is_blocked_ip(value: str) -> bool:
    """True for anything that is not a public unicast address (private, loopback, link-local, metadata, ...)."""
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def validate_url(url: str, *, dev_mode: bool = False) -> str:
    """Syntax checks: https (http only for localhost in dev mode), a host name, no credentials in the URL."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise WebhookError("invalid_url", f"the URL must be 1..{MAX_URL_LENGTH} characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise WebhookError("invalid_url", "the URL cannot be parsed") from None
    host = (parts.hostname or "").lower()
    if not host:
        raise WebhookError("invalid_url", "the URL has no host")
    if parts.username or parts.password:
        raise WebhookError("invalid_url", "credentials in the URL are not allowed; use the signing secret")
    if parts.fragment:
        raise WebhookError("invalid_url", "the URL must not contain a fragment")
    if parts.scheme == "http":
        if not (dev_mode and host in _LOCAL_HOSTS):
            raise WebhookError("insecure_url", "only https:// URLs are allowed")
    elif parts.scheme != "https":
        raise WebhookError("invalid_url", "only https:// URLs are allowed")
    if port == 0:
        raise WebhookError("invalid_url", "invalid port")
    return url


def _literal_ip(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return None


Resolve = Callable[[str, int], Awaitable[list[str]]]


async def _system_resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def check_destination(url: str, *, dev_mode: bool = False, resolve: Resolve | None = None) -> None:
    """Validate ``url`` and refuse hosts that resolve to non-public addresses (all checks are skipped for the
    address part in dev mode). Does network I/O (DNS): never call it inside a database transaction."""
    validate_url(url, dev_mode=dev_mode)
    if dev_mode:
        return
    parts = urlsplit(url)
    host = parts.hostname or ""
    literal = _literal_ip(host)
    if literal is not None:
        addresses = [literal]
    else:
        try:
            addresses = await (resolve or _system_resolve)(host, parts.port or 443)
        except OSError:
            raise WebhookError("unresolvable", "the host name does not resolve") from None
    if not addresses or any(is_blocked_ip(a) for a in addresses):
        raise WebhookError("blocked_address", "the URL points to a private or reserved network address")


class SafeResolver(AbstractResolver):
    """Resolver for the delivery connector: refuses names that resolve to non-public addresses, so the address
    checked is the address connected to (no DNS rebinding between a check and the request)."""

    def __init__(self, inner: AbstractResolver | None = None, *, allow_private: bool = False) -> None:
        self._inner = inner or DefaultResolver()
        self._allow_private = allow_private

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._inner.resolve(host, port, family)
        if self._allow_private:
            return results
        allowed = [r for r in results if not is_blocked_ip(str(r["host"]))]
        if not allowed or len(allowed) != len(results):
            raise BlockedAddressError(f"{host} resolves to a non-public address")
        return allowed

    async def close(self) -> None:
        await self._inner.close()


# ---------------------------------------------------------------------------------------------------------------
# Enqueue (transactional outbox) with a per-process endpoint cache
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _CachedEndpoint:
    id: int
    events: str


_cache: weakref.WeakKeyDictionary[Any, tuple[float, tuple[_CachedEndpoint, ...]]] = weakref.WeakKeyDictionary()
_cache_generation = 0


def invalidate_cache() -> None:
    """Forget the cached endpoint lists (call after creating, changing or deleting an endpoint)."""
    global _cache_generation
    _cache.clear()
    _cache_generation += 1


def _cache_key(session: AsyncSession) -> Any:
    bind = session.bind
    return getattr(bind, "sync_engine", None)


async def _active_endpoints(session: AsyncSession) -> tuple[_CachedEndpoint, ...]:
    key = _cache_key(session)
    now = time.monotonic()
    if key is not None:
        try:
            cached = _cache.get(key)
        except TypeError:
            cached = None
        if cached is not None and cached[0] > now:
            return cached[1]
    generation = _cache_generation
    with session.no_autoflush:
        rows = (
            await session.execute(
                select(WebhookEndpoint.id, WebhookEndpoint.events).where(WebhookEndpoint.is_active.is_(True))
            )
        ).all()
    endpoints = tuple(_CachedEndpoint(int(r.id), str(r.events)) for r in rows)
    if key is not None and generation == _cache_generation:
        with contextlib.suppress(TypeError):  # bind that cannot be weakly referenced: no caching
            _cache[key] = (now + CACHE_TTL_SEC, endpoints)
    return endpoints


async def enqueue_event(
    session: AsyncSession,
    event_type: str,
    *,
    created_at: datetime,
    user_id: int | None = None,
    amount_kop: int | None = None,
    data: Mapping[str, Any] | None = None,
    event_id: str | None = None,
) -> list[WebhookDelivery]:
    """Add a delivery row for every active endpoint subscribed to ``event_type`` (caller's transaction, no I/O)."""
    endpoints = [e for e in await _active_endpoints(session) if matches(e.events, event_type)]
    if not endpoints:
        return []
    event_id = event_id or new_event_id()
    payload = build_payload(event_id, event_type, created_at, user_id, amount_kop, data)
    rows = [
        WebhookDelivery(
            endpoint_id=e.id,
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            status=WebhookDeliveryStatus.PENDING,
            attempts=0,
            next_attempt_at=created_at,
            created_at=created_at,
        )
        for e in endpoints
    ]
    session.add_all(rows)
    return rows


# ---------------------------------------------------------------------------------------------------------------
# Endpoint management (callers own the transaction)
# ---------------------------------------------------------------------------------------------------------------
async def create_endpoint(
    session: AsyncSession,
    box: SecretBox,
    url: str,
    *,
    events: Iterable[str] | str | None = None,
    description: str | None = None,
    created_by: int | None = None,
    dev_mode: bool = False,
) -> tuple[str, WebhookEndpoint]:
    """Create an endpoint; returns the raw signing secret (shown once) and the row."""
    secret = generate_secret()
    row = WebhookEndpoint(
        url=validate_url(url, dev_mode=dev_mode),
        secret=box.encrypt(secret),
        events=normalize_events(events),
        description=(description or "").strip()[:255] or None,
        is_active=True,
        created_by=created_by,
        consecutive_failures=0,
        consecutive_dead=0,
    )
    session.add(row)
    await session.flush()
    invalidate_cache()
    return secret, row


def update_endpoint(
    row: WebhookEndpoint,
    *,
    url: str | None = None,
    events: Iterable[str] | str | None = None,
    description: str | None = None,
    is_active: bool | None = None,
    dev_mode: bool = False,
) -> None:
    if url is not None:
        row.url = validate_url(url, dev_mode=dev_mode)
    if events is not None:
        row.events = normalize_events(events)
    if description is not None:
        row.description = description.strip()[:255] or None
    if is_active is not None and is_active != row.is_active:
        row.is_active = is_active
        if is_active:
            row.consecutive_failures = 0
            row.consecutive_dead = 0
            row.disabled_reason = None
            row.disabled_at = None
        else:
            row.disabled_reason = "disabled by an administrator"
            row.disabled_at = utcnow()
    invalidate_cache()


def rotate_secret(box: SecretBox, row: WebhookEndpoint) -> str:
    """Replace the signing secret; returns the new raw secret (pending deliveries are signed with it)."""
    secret = generate_secret()
    row.secret = box.encrypt(secret)
    return secret


async def delete_endpoint(session: AsyncSession, row: WebhookEndpoint) -> None:
    await session.execute(delete(WebhookDelivery).where(WebhookDelivery.endpoint_id == row.id))
    await session.delete(row)
    invalidate_cache()


def enqueue_test(session: AsyncSession, row: WebhookEndpoint) -> WebhookDelivery:
    """A ``webhook.test`` delivery for one endpoint, regardless of its filter."""
    now = utcnow()
    event_id = new_event_id()
    delivery = WebhookDelivery(
        endpoint_id=row.id,
        event_id=event_id,
        event_type=TEST_EVENT,
        payload=build_payload(event_id, TEST_EVENT, now, None, None, {"message": "Test delivery"}),
        status=WebhookDeliveryStatus.PENDING,
        attempts=0,
        next_attempt_at=now,
        created_at=now,
    )
    session.add(delivery)
    return delivery


def reset_for_redelivery(delivery: WebhookDelivery) -> None:
    """Queue a delivery again with a fresh retry schedule (same event id and body)."""
    delivery.status = WebhookDeliveryStatus.PENDING
    delivery.attempts = 0
    delivery.next_attempt_at = utcnow()
    delivery.last_error = None
    delivery.last_status_code = None
    delivery.sent_at = None


# ---------------------------------------------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Job:
    delivery_id: int
    endpoint_id: int
    attempts: int  # value after the claim (1 = first attempt)
    url: str
    secret: str  # encrypted
    event_id: str
    event_type: str
    payload: str


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    status_code: int | None = None
    error: str | None = None


@dataclass(slots=True)
class DisabledEndpoint:
    id: int
    host: str
    failures: int  # deliveries given up in a row


@dataclass(slots=True)
class RunResult:
    sent: int = 0
    failed: int = 0
    dead: int = 0
    disabled: list[DisabledEndpoint] = field(default_factory=list)
    outcomes: dict[int, Outcome] = field(default_factory=dict)  # delivery id -> outcome


class Sender(Protocol):
    async def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> Outcome: ...


def _short_error(exc: BaseException, url: str) -> str:
    text = f"{type(exc).__name__}: {exc}".replace(url, urlsplit(url).hostname or "")
    return text[:MAX_ERROR_LENGTH]


class HttpSender:
    """Posts deliveries with aiohttp: fixed timeout, no redirects, no proxies from the environment, the response
    body ignored beyond :data:`RESPONSE_READ_LIMIT` bytes, and only public destination addresses."""

    def __init__(self, *, dev_mode: bool = False, timeout: float = REQUEST_TIMEOUT_SEC) -> None:
        self._dev_mode = dev_mode
        connector = aiohttp.TCPConnector(resolver=SafeResolver(allow_private=dev_mode), limit=CONCURRENCY * 2)
        self._session = aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        )

    async def __aenter__(self) -> HttpSender:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._session.close()

    async def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> Outcome:
        try:
            validate_url(url, dev_mode=self._dev_mode)
        except WebhookError as exc:
            return Outcome(False, error=str(exc))
        literal = _literal_ip(urlsplit(url).hostname or "")
        if literal is not None and not self._dev_mode and is_blocked_ip(literal):
            return Outcome(False, error="the URL points to a private or reserved network address")
        try:
            async with self._session.post(url, data=body, headers=dict(headers), allow_redirects=False) as resp:
                await resp.content.read(RESPONSE_READ_LIMIT)
                status = resp.status
        except BlockedAddressError:
            return Outcome(False, error="the host resolves to a private or reserved network address")
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            return Outcome(False, error=_short_error(exc, url))
        if 200 <= status < 300:
            return Outcome(True, status_code=status)
        if 300 <= status < 400:
            return Outcome(False, status_code=status, error=f"HTTP {status}: redirects are not followed")
        return Outcome(False, status_code=status, error=f"HTTP {status}")


def _due_filter(now: datetime) -> Any:
    return (
        WebhookDelivery.status.in_((WebhookDeliveryStatus.PENDING, WebhookDeliveryStatus.FAILED)),
        or_(WebhookDelivery.next_attempt_at.is_(None), WebhookDelivery.next_attempt_at <= now),
    )


async def claim(
    session_factory: SessionFactory,
    *,
    now: datetime | None = None,
    limit: int = BATCH_SIZE,
    delivery_id: int | None = None,
) -> list[Job]:
    """Take due deliveries of active endpoints for this worker.

    Each row is claimed by a compare-and-swap on ``attempts`` that also moves ``next_attempt_at`` forward by
    :data:`CLAIM_LEASE`, so concurrent workers (any database) never send the same attempt twice and a crashed
    worker's rows become due again. On PostgreSQL/MySQL candidate rows are also read with ``SKIP LOCKED``.
    """
    now = now or utcnow()
    jobs: list[Job] = []
    async with session_factory.begin() as session:
        query = (
            select(
                WebhookDelivery.id,
                WebhookDelivery.attempts,
                WebhookDelivery.endpoint_id,
                WebhookDelivery.event_id,
                WebhookDelivery.event_type,
                WebhookDelivery.payload,
                WebhookEndpoint.url,
                WebhookEndpoint.secret,
            )
            .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
            .where(WebhookEndpoint.is_active.is_(True), *_due_filter(now))
            .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id)
            .limit(limit)
        )
        if delivery_id is not None:
            query = query.where(WebhookDelivery.id == delivery_id)
        if session.get_bind().dialect.name != "sqlite":
            query = query.with_for_update(skip_locked=True, of=WebhookDelivery)
        for row in (await session.execute(query)).all():
            result = await session.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == row.id, WebhookDelivery.attempts == row.attempts, *_due_filter(now))
                .values(attempts=row.attempts + 1, next_attempt_at=now + CLAIM_LEASE)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                continue  # another worker took it
            jobs.append(
                Job(
                    delivery_id=row.id,
                    endpoint_id=row.endpoint_id,
                    attempts=row.attempts + 1,
                    url=row.url,
                    secret=row.secret,
                    event_id=row.event_id,
                    event_type=row.event_type,
                    payload=row.payload,
                )
            )
    return jobs


def request_headers(job: Job, secret: str, body: bytes, timestamp: int) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Webhook-Id": job.event_id,
        "X-Webhook-Event": job.event_type,
        "X-Webhook-Timestamp": str(timestamp),
        "X-Webhook-Signature": sign(secret, timestamp, body),
    }


async def _send(job: Job, box: SecretBox, sender: Sender) -> Outcome:
    try:
        secret = box.decrypt(job.secret)
    except ValueError:
        return Outcome(False, error="the signing secret cannot be decrypted (was SECRET_KEY changed?)")
    body = job.payload.encode("utf-8")
    headers = request_headers(job, secret, body, int(time.time()))
    try:
        return await sender(job.url, body, headers)
    except Exception as exc:  # a sender bug must not lose the claim bookkeeping
        log.exception("webhook delivery %s: sender failed", job.delivery_id)
        return Outcome(False, error=_short_error(exc, job.url))


def next_delay(attempts: int) -> timedelta | None:
    """Delay before the next attempt after ``attempts`` failed ones, or None when it was the last."""
    return BACKOFF[attempts - 1] if 1 <= attempts <= len(BACKOFF) else None


async def _record(
    session_factory: SessionFactory,
    results: Sequence[tuple[Job, Outcome]],
    *,
    auto_disable_after: int,
    now: datetime,
    run: RunResult,
) -> None:
    async with session_factory.begin() as session:
        endpoint_ids = sorted({job.endpoint_id for job, _ in results})
        endpoints = {
            e.id: e
            for e in (
                await session.scalars(
                    select(WebhookEndpoint).where(WebhookEndpoint.id.in_(endpoint_ids)).with_for_update()
                )
            ).all()
        }
        for job, outcome in results:
            run.outcomes[job.delivery_id] = outcome
            values: dict[str, Any] = {"last_status_code": outcome.status_code}
            if outcome.ok:
                values.update(status=WebhookDeliveryStatus.SENT, sent_at=now, last_error=None, next_attempt_at=None)
            else:
                delay = next_delay(job.attempts)
                values["last_error"] = (outcome.error or "delivery failed")[:MAX_ERROR_LENGTH]
                if delay is None:
                    values.update(status=WebhookDeliveryStatus.DEAD, next_attempt_at=None)
                else:
                    values.update(status=WebhookDeliveryStatus.FAILED, next_attempt_at=now + delay)
            result = await session.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == job.delivery_id, WebhookDelivery.attempts == job.attempts)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                continue  # redelivered or deleted meanwhile; that newer state wins
            endpoint = endpoints.get(job.endpoint_id)
            if outcome.ok:
                run.sent += 1
                if endpoint is not None:
                    endpoint.consecutive_failures = 0
                    endpoint.consecutive_dead = 0
                    endpoint.last_success_at = now
                continue
            if values["status"] is WebhookDeliveryStatus.DEAD:
                run.dead += 1
            else:
                run.failed += 1
            if endpoint is None:
                continue
            endpoint.consecutive_failures += 1
            endpoint.last_failure_at = now
            if values["status"] is not WebhookDeliveryStatus.DEAD:
                continue
            endpoint.consecutive_dead += 1
            if endpoint.is_active and auto_disable_after > 0 and endpoint.consecutive_dead >= auto_disable_after:
                endpoint.is_active = False
                endpoint.disabled_at = now
                endpoint.disabled_reason = f"{endpoint.consecutive_dead} deliveries in a row failed"
                run.disabled.append(
                    DisabledEndpoint(endpoint.id, urlsplit(endpoint.url).hostname or "", endpoint.consecutive_dead)
                )
    if run.disabled:
        invalidate_cache()


async def deliver(
    session_factory: SessionFactory,
    box: SecretBox,
    *,
    dev_mode: bool = False,
    auto_disable_after: int = 3,
    sender: Sender | None = None,
    delivery_id: int | None = None,
    limit: int = BATCH_SIZE,
    now: datetime | None = None,
) -> RunResult:
    """Claim due deliveries (or one ``delivery_id``), send them concurrently and record the outcomes."""
    run = RunResult()
    jobs = await claim(session_factory, now=now, limit=limit, delivery_id=delivery_id)
    if not jobs:
        return run
    own: HttpSender | None = None
    if sender is None:
        own = HttpSender(dev_mode=dev_mode)
        sender = own
    try:
        gate = asyncio.Semaphore(CONCURRENCY)

        async def one(job: Job) -> tuple[Job, Outcome]:
            async with gate:
                return job, await _send(job, box, sender)

        results = await asyncio.gather(*(one(job) for job in jobs))
    finally:
        if own is not None:
            await own.close()
    await _record(session_factory, results, auto_disable_after=auto_disable_after, now=now or utcnow(), run=run)
    for job, outcome in results:
        if not outcome.ok:
            log.info(
                "webhook delivery %s (endpoint %s, attempt %d) failed: %s",
                job.delivery_id,
                job.endpoint_id,
                job.attempts,
                outcome.error,
            )
    return run


async def purge(session_factory: SessionFactory, keep_days: int, *, now: datetime | None = None) -> int:
    """Delete finished deliveries older than ``keep_days`` and anything older than that plus the retry window."""
    now = now or utcnow()
    cutoff = now - timedelta(days=max(keep_days, 1))
    stale = cutoff - sum(BACKOFF, timedelta()) - timedelta(days=1)
    async with session_factory.begin() as session:
        result = await session.execute(
            delete(WebhookDelivery)
            .where(
                or_(
                    (WebhookDelivery.created_at < cutoff)
                    & WebhookDelivery.status.in_((WebhookDeliveryStatus.SENT, WebhookDeliveryStatus.DEAD)),
                    WebhookDelivery.created_at < stale,
                )
            )
            .execution_options(synchronize_session=False)
        )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
