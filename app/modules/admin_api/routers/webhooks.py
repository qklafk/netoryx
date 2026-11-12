"""Outgoing webhooks (owner and administrators): endpoints, signing secrets, test events and delivery history."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.permissions import Permission
from app.db.models import WebhookDelivery, WebhookDeliveryStatus, WebhookEndpoint
from app.modules.admin_api.deps import Actor, bad_request, not_found, require
from app.modules.admin_api.schemas.common import Ok
from app.services import webhooks
from app.web.deps import ReadDb

router = APIRouter(tags=["admin: webhooks"])

Manager = Annotated[Actor, Depends(require(Permission.WEBHOOKS))]


class EndpointIn(BaseModel):
    url: str = Field(max_length=webhooks.MAX_URL_LENGTH)
    events: list[str] = Field(default_factory=lambda: [webhooks.ALL_EVENTS], max_length=100)
    description: str | None = Field(default=None, max_length=255)


class EndpointUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=webhooks.MAX_URL_LENGTH)
    events: list[str] | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


class EndpointOut(BaseModel):
    id: int
    url: str
    events: list[str]
    description: str | None
    is_active: bool
    created_by: int | None
    consecutive_failures: int
    consecutive_dead: int
    disabled_reason: str | None
    disabled_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    created_at: datetime
    pending: int = 0


class EndpointCreated(EndpointOut):
    secret: str  # shown once


class SecretOut(BaseModel):
    secret: str


class DeliveryOut(BaseModel):
    id: int
    endpoint_id: int
    event_id: str
    event_type: str
    status: str
    attempts: int
    next_attempt_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None


class DeliveryDetail(DeliveryOut):
    payload: str


class DeliveryResult(BaseModel):
    delivery: DeliveryOut
    ok: bool
    status_code: int | None = None
    error: str | None = None


class Catalog(BaseModel):
    events: list[str]
    retry_delays_sec: list[int]


def _out(row: WebhookEndpoint, pending: int = 0) -> EndpointOut:
    return EndpointOut(
        id=row.id,
        url=row.url,
        events=webhooks.event_list(row.events),
        description=row.description,
        is_active=row.is_active,
        created_by=row.created_by,
        consecutive_failures=row.consecutive_failures,
        consecutive_dead=row.consecutive_dead,
        disabled_reason=row.disabled_reason,
        disabled_at=row.disabled_at,
        last_success_at=row.last_success_at,
        last_failure_at=row.last_failure_at,
        created_at=row.created_at,
        pending=pending,
    )


def _delivery(row: WebhookDelivery) -> DeliveryOut:
    return DeliveryOut(
        id=row.id,
        endpoint_id=row.endpoint_id,
        event_id=row.event_id,
        event_type=row.event_type,
        status=str(row.status),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        created_at=row.created_at,
        sent_at=row.sent_at,
    )


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


def _error(exc: webhooks.WebhookError) -> Exception:
    return bad_request(exc.code, str(exc))


async def _check(actor: Actor, url: str) -> None:
    """Destination checks with DNS: run before any transaction is opened."""
    try:
        await webhooks.check_destination(url, dev_mode=actor.ctx.settings.dev_mode)
    except webhooks.WebhookError as exc:
        raise _error(exc) from None


async def _send_now(actor: Actor, delivery_id: int) -> DeliveryResult:
    ctx = actor.ctx
    settings = ctx.settings
    run = await webhooks.deliver(
        ctx.session_factory,
        ctx.secret_box,
        dev_mode=settings.dev_mode,
        auto_disable_after=settings.webhooks_auto_disable_after,
        delivery_id=delivery_id,
        limit=1,
    )
    async with ctx.session_factory() as session:
        row = await session.get(WebhookDelivery, delivery_id)
        if row is None:
            raise not_found("No such delivery")
        out = _delivery(row)
    outcome = run.outcomes.get(delivery_id)
    if outcome is None:
        return DeliveryResult(delivery=out, ok=False, error="the delivery was not sent (endpoint switched off?)")
    return DeliveryResult(delivery=out, ok=outcome.ok, status_code=outcome.status_code, error=outcome.error)


@router.get("/webhooks/catalog", response_model=Catalog)
async def catalog(actor: Manager) -> Catalog:
    """Event types that can be subscribed to and the retry schedule."""
    return Catalog(
        events=list(webhooks.EVENT_TYPES), retry_delays_sec=[int(d.total_seconds()) for d in webhooks.BACKOFF]
    )


@router.get("/webhooks", response_model=list[EndpointOut])
async def list_endpoints(actor: Manager, session: ReadDb) -> list[EndpointOut]:
    rows = (await session.scalars(select(WebhookEndpoint).order_by(WebhookEndpoint.id))).all()
    counts = await session.execute(
        select(WebhookDelivery.endpoint_id, func.count())
        .where(WebhookDelivery.status.in_((WebhookDeliveryStatus.PENDING, WebhookDeliveryStatus.FAILED)))
        .group_by(WebhookDelivery.endpoint_id)
    )
    pending = {int(endpoint_id): int(count) for endpoint_id, count in counts.all()}
    return [_out(r, int(pending.get(r.id, 0))) for r in rows]


@router.post("/webhooks", response_model=EndpointCreated, status_code=201)
async def create_endpoint(body: EndpointIn, actor: Manager) -> EndpointCreated:
    """Add an endpoint. The signing secret is returned only in this response."""
    await _check(actor, body.url)
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        try:
            secret, row = await webhooks.create_endpoint(
                session,
                ctx.secret_box,
                body.url,
                events=body.events,
                description=body.description,
                created_by=actor.tg_id,
                dev_mode=ctx.settings.dev_mode,
            )
        except webhooks.WebhookError as exc:
            raise _error(exc) from None
        await actor.audit(session, "webhook.create", "webhook", row.id, host=_host(row.url), events=row.events)
        out = _out(row)
    return EndpointCreated(**out.model_dump(), secret=secret)


@router.get("/webhooks/{endpoint_id}", response_model=EndpointOut)
async def get_endpoint(endpoint_id: int, actor: Manager, session: ReadDb) -> EndpointOut:
    row = await session.get(WebhookEndpoint, endpoint_id)
    if row is None:
        raise not_found("No such webhook")
    return _out(row)


@router.patch("/webhooks/{endpoint_id}", response_model=EndpointOut)
async def update_endpoint(endpoint_id: int, body: EndpointUpdate, actor: Manager) -> EndpointOut:
    if body.url is not None:
        await _check(actor, body.url)
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        row = await session.get(WebhookEndpoint, endpoint_id)
        if row is None:
            raise not_found("No such webhook")
        try:
            webhooks.update_endpoint(
                row,
                url=body.url,
                events=body.events,
                description=body.description,
                is_active=body.is_active,
                dev_mode=ctx.settings.dev_mode,
            )
        except webhooks.WebhookError as exc:
            raise _error(exc) from None
        changes = body.model_dump(exclude_none=True)
        if "url" in changes:
            changes["url"] = _host(row.url)
        await actor.audit(session, "webhook.update", "webhook", row.id, **changes)
        await session.flush()
        return _out(row)


@router.delete("/webhooks/{endpoint_id}", response_model=Ok)
async def delete_endpoint(endpoint_id: int, actor: Manager) -> Ok:
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        row = await session.get(WebhookEndpoint, endpoint_id)
        if row is None:
            raise not_found("No such webhook")
        host = _host(row.url)
        await webhooks.delete_endpoint(session, row)
        await actor.audit(session, "webhook.delete", "webhook", endpoint_id, host=host)
    return Ok()


@router.post("/webhooks/{endpoint_id}/rotate-secret", response_model=SecretOut)
async def rotate_secret(endpoint_id: int, actor: Manager) -> SecretOut:
    """Issue a new signing secret (returned once); the old one stops working immediately."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        row = await session.get(WebhookEndpoint, endpoint_id)
        if row is None:
            raise not_found("No such webhook")
        secret = webhooks.rotate_secret(ctx.secret_box, row)
        await actor.audit(session, "webhook.rotate_secret", "webhook", endpoint_id)
    return SecretOut(secret=secret)


@router.post("/webhooks/{endpoint_id}/test", response_model=DeliveryResult)
async def send_test(endpoint_id: int, actor: Manager) -> DeliveryResult:
    """Queue a ``webhook.test`` event for this endpoint and send it right away."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        row = await session.get(WebhookEndpoint, endpoint_id)
        if row is None:
            raise not_found("No such webhook")
        if not row.is_active:
            raise bad_request("inactive", "Turn the endpoint on first")
        delivery = webhooks.enqueue_test(session, row)
        await session.flush()
        delivery_id = delivery.id
        await actor.audit(session, "webhook.test", "webhook", endpoint_id)
    return await _send_now(actor, delivery_id)


@router.get("/webhooks/{endpoint_id}/deliveries", response_model=list[DeliveryOut])
async def list_deliveries(
    endpoint_id: int,
    actor: Manager,
    session: ReadDb,
    status: WebhookDeliveryStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DeliveryOut]:
    """Recent deliveries of an endpoint, newest first."""
    query = select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint_id)
    if status is not None:
        query = query.where(WebhookDelivery.status == status)
    rows = (await session.scalars(query.order_by(WebhookDelivery.id.desc()).limit(limit).offset(offset))).all()
    return [_delivery(r) for r in rows]


@router.get("/webhooks/deliveries/{delivery_id}", response_model=DeliveryDetail)
async def get_delivery(delivery_id: int, actor: Manager, session: ReadDb) -> DeliveryDetail:
    row = await session.get(WebhookDelivery, delivery_id)
    if row is None:
        raise not_found("No such delivery")
    return DeliveryDetail(**_delivery(row).model_dump(), payload=row.payload)


@router.post("/webhooks/deliveries/{delivery_id}/redeliver", response_model=DeliveryResult)
async def redeliver(delivery_id: int, actor: Manager) -> DeliveryResult:
    """Send a delivery again now (same event id and body) with a fresh retry schedule."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        row = await session.get(WebhookDelivery, delivery_id)
        if row is None:
            raise not_found("No such delivery")
        webhooks.reset_for_redelivery(row)
        await actor.audit(session, "webhook.redeliver", "webhook_delivery", delivery_id, endpoint_id=row.endpoint_id)
    return await _send_now(actor, delivery_id)
