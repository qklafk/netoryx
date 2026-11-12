"""Broadcasts: compose (HTML, media, URL buttons), audience segments, preview, schedule, start, cancel, progress."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.core.i18n import TextValidationError, check_html
from app.core.permissions import Permission
from app.db.models import Broadcast, BroadcastStatus
from app.db.session import read_session
from app.modules.admin_api.deps import Actor, bad_request, conflict, kick_jobs, not_found, require
from app.modules.admin_api.schemas.broadcasts import (
    AudienceIn,
    BroadcastIn,
    BroadcastOut,
    CountOut,
    MediaOut,
    PreviewOut,
    ScheduleIn,
)
from app.modules.admin_api.schemas.common import Ok, Page
from app.services import broadcasts
from app.services.broadcasts import AudienceSpec, BroadcastError, BroadcastErrorCode, Delivery, MediaType
from app.web.deps import PageParams, ReadDb
from app.web.errors import ApiError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/broadcasts", tags=["admin: broadcasts"])

Sender = Annotated[Actor, Depends(require(Permission.BROADCASTS))]

MAX_MEDIA_BYTES = 50 * 1024 * 1024
STREAM_INTERVAL_SEC = 2.0
STREAM_MAX_SEC = 3600.0
_FINAL = (BroadcastStatus.DONE, BroadcastStatus.CANCELED)
_ERROR_STATUS = {
    BroadcastErrorCode.NOT_FOUND: 404,
    BroadcastErrorCode.BAD_STATUS: 409,
}


def _error(exc: BroadcastError) -> Exception:
    return ApiError(_ERROR_STATUS.get(exc.code, 400), str(exc.code), str(exc))


def _spec(audience: AudienceIn) -> AudienceSpec:
    spec = AudienceSpec(
        kind=audience.kind,
        days=audience.days,
        language=audience.language,
        plan_id=audience.plan_id,
        panel_id=audience.panel_id,
        location_id=audience.location_id,
        user_ids=tuple(dict.fromkeys(audience.user_ids)),
    )
    try:
        spec.validate()
    except BroadcastError as exc:
        raise _error(exc) from None
    return spec


async def _out(session: Any, broadcast: Broadcast) -> BroadcastOut:
    options = await broadcasts.get_options(session, broadcast)
    try:
        buttons = json.loads(broadcast.buttons) if broadcast.buttons else []
    except ValueError:
        buttons = []
    processed = broadcast.sent + broadcast.failed + broadcast.blocked
    return BroadcastOut(
        id=broadcast.id,
        created_by=broadcast.created_by,
        status=broadcast.status,
        audience=options.audience.to_dict(),
        text=broadcast.text,
        buttons=buttons if isinstance(buttons, list) else [],
        media_type=options.media_type,
        media=options.media,
        copied_message=broadcast.source_message_id is not None,
        disable_preview=options.disable_preview,
        scheduled_at=options.scheduled_at,
        total=broadcast.total,
        sent=broadcast.sent,
        failed=broadcast.failed,
        blocked=broadcast.blocked,
        progress=round(processed / broadcast.total, 4)
        if broadcast.total
        else (1.0 if broadcast.status == BroadcastStatus.DONE else 0.0),
        created_at=broadcast.created_at,
        started_at=broadcast.started_at,
        finished_at=broadcast.finished_at,
    )


async def _get(session: Any, broadcast_id: int) -> Broadcast:
    broadcast = await session.scalar(
        select(Broadcast).where(Broadcast.id == broadcast_id).execution_options(populate_existing=True)
    )
    if broadcast is None:
        raise not_found("Broadcast not found")
    return broadcast


@router.get("", response_model=Page[BroadcastOut])
async def history(
    actor: Sender, session: ReadDb, page: PageParams, status: BroadcastStatus | None = None
) -> Page[BroadcastOut]:
    """Broadcasts, newest first, with delivery counters."""
    stmt = select(Broadcast)
    if status is not None:
        stmt = stmt.where(Broadcast.status == status)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await session.scalars(stmt.order_by(Broadcast.id.desc()).limit(page.limit).offset(page.offset))).all()
    return Page(items=[await _out(session, b) for b in rows], total=total, limit=page.limit, offset=page.offset)


@router.post("/audience/count", response_model=CountOut)
async def count_audience(body: AudienceIn, actor: Sender, session: ReadDb) -> CountOut:
    """How many users the segment reaches now (banned users and those who blocked the bot excluded)."""
    return CountOut(recipients=await broadcasts.count_audience(session, _spec(body)))


@router.post("/media", response_model=MediaOut)
async def upload_media(
    request: Request,
    actor: Sender,
    media_type: Annotated[MediaType, Query(alias="type")],
    filename: Annotated[str, Query(min_length=1, max_length=128, pattern=r"^[\w.\- ()]+$")] = "file",
) -> MediaOut:
    """Upload a photo/video/animation/document (raw request body) and get a Telegram ``file_id`` for a broadcast.

    The file is sent to your own chat with the bot once (Telegram stores it) and that message is deleted.
    """
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_MEDIA_BYTES:
            raise bad_request("too_large", "The file is larger than 50 MB")
    if not data:
        raise bad_request("empty", "Send the file as the request body")
    bot = actor.ctx.bot
    upload = BufferedInputFile(bytes(data), filename=filename)
    senders = {
        MediaType.PHOTO: bot.send_photo,
        MediaType.VIDEO: bot.send_video,
        MediaType.ANIMATION: bot.send_animation,
        MediaType.DOCUMENT: bot.send_document,
    }
    try:
        message = await senders[media_type](actor.tg_id, upload, disable_notification=True)
    except TelegramAPIError as exc:
        raise conflict("telegram_refused", f"Telegram did not accept the file: {str(exc)[:300]}") from None
    file_id = _file_id(message, media_type)
    try:
        await bot.delete_message(actor.tg_id, message.message_id)
    except TelegramAPIError:
        log.debug("could not delete the media upload message", exc_info=True)
    if not file_id:
        raise conflict("telegram_refused", "Telegram did not return a file id")
    return MediaOut(media_type=media_type, media=file_id)


def _file_id(message: Any, media_type: MediaType) -> str | None:
    if media_type == MediaType.PHOTO and getattr(message, "photo", None):
        return str(message.photo[-1].file_id)
    for attr in ("video", "animation", "document"):
        item = getattr(message, attr, None)
        if item is not None:
            return str(item.file_id)
    return None


@router.post("", response_model=BroadcastOut, status_code=201)
async def create(body: BroadcastIn, actor: Sender) -> BroadcastOut:
    """Create a draft (and optionally schedule it). Send a preview to yourself before starting."""
    if body.text:
        try:
            check_html(body.text)
        except TextValidationError as exc:
            raise bad_request("bad_html", str(exc)) from None
    spec = _spec(body.audience)
    async with actor.ctx.session_factory.begin() as session:
        try:
            broadcast = await broadcasts.create_draft(
                session,
                actor.tg_id,
                spec,
                text=body.text or None,
                buttons=[[b.model_dump() for b in row] for row in body.buttons],
                media_type=body.media_type,
                media=body.media or None,
                disable_preview=body.disable_preview,
                source="web",
                ip=actor.ip,
            )
            if body.scheduled_at is not None:
                await broadcasts.schedule(
                    session, broadcast.id, body.scheduled_at, actor.tg_id, source="web", ip=actor.ip
                )
        except BroadcastError as exc:
            raise _error(exc) from None
        return await _out(session, await _get(session, broadcast.id))


@router.get("/{broadcast_id}", response_model=BroadcastOut)
async def get(broadcast_id: int, actor: Sender, session: ReadDb) -> BroadcastOut:
    """One broadcast with its live counters (poll it, or use ``/events``)."""
    return await _out(session, await _get(session, broadcast_id))


@router.post("/{broadcast_id}/preview", response_model=PreviewOut)
async def preview(broadcast_id: int, actor: Sender) -> PreviewOut:
    """Send the broadcast to yourself to see how it looks."""
    try:
        result = await broadcasts.send_preview(actor.ctx.session_factory, actor.ctx.bot, broadcast_id, actor.tg_id)
    except BroadcastError as exc:
        raise _error(exc) from None
    return PreviewOut(delivered=result == Delivery.SENT)


@router.post("/{broadcast_id}/schedule", response_model=BroadcastOut)
async def schedule(broadcast_id: int, body: ScheduleIn, actor: Sender) -> BroadcastOut:
    """Start the draft automatically at ``scheduled_at`` (null clears the schedule)."""
    async with actor.ctx.session_factory.begin() as session:
        try:
            await broadcasts.schedule(session, broadcast_id, body.scheduled_at, actor.tg_id, source="web", ip=actor.ip)
        except BroadcastError as exc:
            raise _error(exc) from None
        return await _out(session, await _get(session, broadcast_id))


@router.post("/{broadcast_id}/start", response_model=BroadcastOut)
async def start(broadcast_id: int, actor: Sender) -> BroadcastOut:
    """Start sending now; the background sender picks it up (and resumes it after a restart)."""
    async with actor.ctx.session_factory.begin() as session:
        try:
            await broadcasts.start(session, broadcast_id, actor.tg_id, source="web", ip=actor.ip)
        except BroadcastError as exc:
            raise _error(exc) from None
    kick_jobs(actor.ctx, "broadcasts.*")
    async with read_session(actor.ctx.session_factory) as session:
        return await _out(session, await _get(session, broadcast_id))


@router.post("/{broadcast_id}/cancel", response_model=Ok)
async def cancel(broadcast_id: int, actor: Sender) -> Ok:
    """Stop a draft or a running broadcast (messages already sent stay)."""
    async with actor.ctx.session_factory.begin() as session:
        await _get(session, broadcast_id)
        if not await broadcasts.cancel(session, broadcast_id, actor.tg_id, source="web", ip=actor.ip):
            raise conflict("bad_status", "The broadcast has already finished")
    return Ok()


async def _events(ctx: Any, broadcast_id: int, request: Request) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STREAM_MAX_SEC
    last: str | None = None
    while loop.time() < deadline:
        if await request.is_disconnected():
            return
        async with read_session(ctx.session_factory) as session:
            broadcast = await session.scalar(
                select(Broadcast).where(Broadcast.id == broadcast_id).execution_options(populate_existing=True)
            )
            if broadcast is None:
                return
            payload = (await _out(session, broadcast)).model_dump_json()
        if payload != last:
            last = payload
            yield f"event: progress\ndata: {payload}\n\n"
        else:
            yield ": keep-alive\n\n"
        if broadcast.status in _FINAL:
            yield "event: done\ndata: {}\n\n"
            return
        await asyncio.sleep(STREAM_INTERVAL_SEC)


@router.get("/{broadcast_id}/events")
async def events(broadcast_id: int, request: Request, actor: Sender) -> StreamingResponse:
    """Server-sent events with the broadcast's progress until it finishes."""
    async with read_session(actor.ctx.session_factory) as session:
        await _get(session, broadcast_id)
    return StreamingResponse(
        _events(actor.ctx, broadcast_id, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
