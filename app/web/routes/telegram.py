"""Telegram webhook endpoint: verifies the secret header and feeds updates to the dispatcher in the background."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import ValidationError

from app.config import Settings
from app.utils.security import const_eq
from app.web.deps import Ctx
from app.web.errors import ApiError

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram/webhook"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_CONCURRENT_UPDATES = 32
MAX_PENDING_UPDATES = 1000  # beyond this Telegram gets 503 and redelivers later


def webhook_secret_token(settings: Settings) -> str:
    """Fallback secret for ``X-Telegram-Bot-Api-Secret-Token`` when the context does not carry one.

    ``TELEGRAM_WEBHOOK_SECRET`` if set, else derived from the bot token, so every replica agrees without storage.
    """
    if settings.telegram_webhook_secret is not None:
        configured = settings.telegram_webhook_secret.get_secret_value()
        if configured:
            return configured
    if settings.bot_token is None:
        raise RuntimeError("BOT_TOKEN is required for Telegram webhooks")
    key = settings.bot_token.get_secret_value().encode()
    return hmac.new(key, b"telegram-webhook-secret", hashlib.sha256).hexdigest()


def expected_secret(ctx: Any) -> str:
    """The secret registered with ``set_webhook``: the context's (generated at startup) or the fallback."""
    secret = getattr(ctx, "webhook_secret", None)
    return secret if isinstance(secret, str) and secret else webhook_secret_token(ctx.settings)


def webhook_url(settings: Settings) -> str | None:
    return f"{settings.public_base_url}{WEBHOOK_PATH}" if settings.public_base_url else None


class UpdateFeeder:
    """Runs ``handler(update)`` as background tasks with bounded concurrency and a bounded queue."""

    def __init__(
        self,
        max_concurrency: int = MAX_CONCURRENT_UPDATES,
        max_pending: int = MAX_PENDING_UPDATES,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_pending = max_pending
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def submit(self, handler: Callable[[], Awaitable[Any]], label: str = "update") -> bool:
        """Schedule ``handler``; False if the queue is full or the feeder is shutting down."""
        if self._closed or len(self._tasks) >= self._max_pending:
            return False
        task = asyncio.create_task(self._run(handler, label))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run(self, handler: Callable[[], Awaitable[Any]], label: str) -> None:
        async with self._semaphore:
            try:
                await handler()
            except Exception:
                log.exception("telegram %s: handler failed", label)

    async def close(self, grace_sec: float = 10.0) -> None:
        """Stop accepting updates and wait up to ``grace_sec`` seconds for in-flight ones (then cancel them)."""
        self._closed = True
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=grace_sec)
        for task in pending:
            task.cancel()
        if pending:
            log.warning("telegram webhook: cancelled %d unfinished updates on shutdown", len(pending))
            await asyncio.gather(*pending, return_exceptions=True)


router = APIRouter(include_in_schema=False)


@router.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request, ctx: Ctx) -> Response:
    settings: Settings = ctx.settings
    if settings.bot_mode != "webhook":
        raise ApiError(404, "not_found")
    received = request.headers.get(SECRET_HEADER, "")
    if not received or not const_eq(received, expected_secret(ctx)):
        log.warning("telegram webhook: bad secret header")
        raise ApiError(401, "unauthorized")
    try:
        payload = await request.json()
    except ValueError:
        raise ApiError(400, "bad_request", "Body is not JSON") from None
    if not isinstance(payload, dict):
        raise ApiError(400, "bad_request", "Update must be an object")

    from aiogram.types import Update

    try:
        update = Update.model_validate(payload, context={"bot": ctx.bot})
    except ValidationError:
        log.warning("telegram webhook: unparseable update %s", payload.get("update_id"))
        return Response(status_code=200)  # never make Telegram redeliver an update we cannot parse

    feeder: UpdateFeeder = request.app.state.update_feeder
    bot, dispatcher = ctx.bot, ctx.dispatcher
    accepted = feeder.submit(lambda: dispatcher.feed_update(bot, update), f"update {update.update_id}")
    if not accepted:
        log.warning("telegram webhook: queue full (%d), asking Telegram to retry", feeder.pending)
        return Response(status_code=503, headers={"Retry-After": "5"})
    return Response(status_code=200)
