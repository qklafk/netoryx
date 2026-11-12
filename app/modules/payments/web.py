"""Payment provider webhooks: ``POST /webhooks/<provider>`` (FreeKassa may also notify by ``GET``).

The body is verified by the gateway (signature / source IP), the status is re-checked with the provider whenever it
can be polled, and the payment is credited exactly once (:func:`app.services.payments.service.handle_webhook`).
Each provider gets its own route, so other ``/webhooks/...`` routes (panel webhooks) are not shadowed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from app.db.models import PaymentProvider
from app.services.payments import service
from app.services.payments.base import WebhookRejected

log = logging.getLogger(__name__)

WEBHOOK_PREFIX = "/webhooks"
# Telegram Stars payments arrive as bot updates.
WEBHOOK_PROVIDERS: tuple[str, ...] = tuple(p.value for p in PaymentProvider if p is not PaymentProvider.STARS)

router = APIRouter(include_in_schema=False)


def webhook_path(provider: str) -> str:
    return f"{WEBHOOK_PREFIX}/{provider}"


async def handle(provider: str, request: Request) -> Response:
    ctx = request.app.state.ctx
    gateway = ctx.gateways.get(provider)
    if gateway is None:
        return PlainTextResponse("not found", status_code=404)
    if request.method == "GET" and not getattr(gateway, "webhook_query_fields", False):
        return PlainTextResponse("method not allowed", status_code=405)
    raw = await request.body()
    remote_ip = request.client.host if request.client is not None else None
    try:
        result = await service.handle_webhook(
            ctx.session_factory,
            ctx.settings,
            gateway,
            raw,
            request.headers,
            remote_ip,
            on_credited=ctx.on_payment_credited,
            query=request.url.query,
        )
    except WebhookRejected as exc:
        log.warning("%s webhook from %s rejected: %s", provider, remote_ip, exc.reason)
        return PlainTextResponse("rejected", status_code=exc.status_code)
    if result.response_body.startswith("{"):  # providers that insist on a JSON acknowledgement
        return Response(result.response_body, status_code=result.status_code, media_type="application/json")
    return PlainTextResponse(result.response_body, status_code=result.status_code)


def _endpoint(provider: str) -> Callable[[Request], Awaitable[Response]]:
    async def endpoint(request: Request) -> Response:
        return await handle(provider, request)

    endpoint.__name__ = f"{provider}_webhook"
    return endpoint


for _provider in WEBHOOK_PROVIDERS:
    router.add_api_route(webhook_path(_provider), _endpoint(_provider), methods=["POST", "GET"])
