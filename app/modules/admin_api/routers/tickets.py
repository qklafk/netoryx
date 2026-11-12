"""Support tickets: inbox, conversation, reply (delivered to the user by the bot), close, attachments."""

from __future__ import annotations

import html
import logging
from typing import Annotated, Any

from aiogram.exceptions import TelegramAPIError
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, select

from app.core.permissions import Permission
from app.db.models import Ticket, TicketMessage, TicketStatus, User
from app.db.session import read_session
from app.modules.admin_api.deps import Actor, bad_request, conflict, not_found, require
from app.modules.admin_api.schemas.common import Page
from app.modules.admin_api.schemas.tickets import (
    ReplyIn,
    ReplyOut,
    TicketCounts,
    TicketItem,
    TicketMessageOut,
    TicketOut,
    TicketView,
)
from app.modules.admin_api.texts import t
from app.services import tickets
from app.services.notify import safe_send
from app.web.deps import PageParams, ReadDb

log = logging.getLogger(__name__)

router = APIRouter(prefix="/tickets", tags=["admin: tickets"])

Support = Annotated[Actor, Depends(require(Permission.TICKETS))]

PREVIEW_LENGTH = 160
MAX_FILE_BYTES = 20 * 1024 * 1024  # Bot API download limit


def _error(exc: tickets.TicketError) -> Exception:
    if exc.code == tickets.TicketErrorCode.NOT_FOUND:
        return not_found("Ticket not found")
    return bad_request(str(exc.code), str(exc))


@router.get("/counts", response_model=TicketCounts)
async def counts(actor: Support, session: ReadDb) -> TicketCounts:
    rows = dict((await session.execute(select(Ticket.status, func.count(Ticket.id)).group_by(Ticket.status))).all())
    return TicketCounts(
        open=int(rows.get(TicketStatus.OPEN, 0)),
        answered=int(rows.get(TicketStatus.ANSWERED, 0)),
        closed=int(rows.get(TicketStatus.CLOSED, 0)),
    )


@router.get("", response_model=Page[TicketItem])
async def inbox(
    actor: Support,
    session: ReadDb,
    page: PageParams,
    status: Annotated[list[TicketStatus] | None, Query()] = None,
    user_id: int | None = None,
) -> Page[TicketItem]:
    """Tickets (open ones by default), the longest waiting first."""
    statuses = status or [TicketStatus.OPEN]
    stmt = select(Ticket).where(Ticket.status.in_(statuses))
    if user_id is not None:
        stmt = stmt.where(Ticket.user_id == user_id)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    order = (
        (Ticket.updated_at, Ticket.id)
        if statuses == [TicketStatus.OPEN]
        else (Ticket.updated_at.desc(), Ticket.id.desc())
    )
    rows = list((await session.scalars(stmt.order_by(*order).limit(page.limit).offset(page.offset))).all())
    ids = [r.id for r in rows]
    users = {
        u.tg_id: u for u in (await session.scalars(select(User).where(User.tg_id.in_({r.user_id for r in rows})))).all()
    }
    message_counts: dict[int, int] = {}
    last_text: dict[int, str | None] = {}
    if ids:
        message_counts = {
            int(tid): int(n)
            for tid, n in (
                await session.execute(
                    select(TicketMessage.ticket_id, func.count(TicketMessage.id))
                    .where(TicketMessage.ticket_id.in_(ids))
                    .group_by(TicketMessage.ticket_id)
                )
            ).all()
        }
        last_ids = (
            select(func.max(TicketMessage.id)).where(TicketMessage.ticket_id.in_(ids)).group_by(TicketMessage.ticket_id)
        )
        for tid, text in (
            await session.execute(
                select(TicketMessage.ticket_id, TicketMessage.text).where(TicketMessage.id.in_(last_ids))
            )
        ).all():
            last_text[int(tid)] = (text or "")[:PREVIEW_LENGTH] or None
    items = [
        TicketItem(
            ticket=TicketOut.model_validate(r),
            username=users[r.user_id].username if r.user_id in users else None,
            first_name=users[r.user_id].first_name if r.user_id in users else None,
            messages=message_counts.get(r.id, 0),
            last_message=last_text.get(r.id),
        )
        for r in rows
    ]
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


async def _view(session: Any, ticket_id: int) -> TicketView:
    try:
        data = await tickets.view(session, ticket_id, limit=500)
    except tickets.TicketError as exc:
        raise _error(exc) from None
    user = await session.get(User, data.ticket.user_id)
    return TicketView(
        ticket=TicketOut.model_validate(data.ticket),
        username=user.username if user else None,
        first_name=user.first_name if user else None,
        language=user.language if user else None,
        messages=[TicketMessageOut.model_validate(m) for m in data.messages],
    )


@router.get("/{ticket_id}", response_model=TicketView)
async def view(ticket_id: int, actor: Support, session: ReadDb) -> TicketView:
    return await _view(session, ticket_id)


@router.post("/{ticket_id}/reply", response_model=ReplyOut)
async def reply(ticket_id: int, body: ReplyIn, actor: Support) -> ReplyOut:
    """Answer the user (the bot delivers it right away); optionally close the ticket."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        try:
            message = await tickets.reply(
                session, ticket_id, actor.tg_id, body.text, is_admin=True, source="web", ip=actor.ip
            )
            if body.close:
                await tickets.close(session, ticket_id, actor.tg_id, is_admin=True, source="web", ip=actor.ip)
        except tickets.TicketError as exc:
            raise _error(exc) from None
        ticket = await session.get(Ticket, ticket_id)
        user = await session.get(User, ticket.user_id) if ticket else None
        out = TicketMessageOut.model_validate(message)
    delivered = False
    if user is not None and not user.is_banned:
        text = t(ctx, "admin_api.notify.ticket_reply", user.language, ticket_id=ticket_id, text=html.escape(body.text))
        delivered = await safe_send(ctx.bot, user.tg_id, text, session_factory=ctx.session_factory, parse_mode="HTML")
    return ReplyOut(message=out, delivered=delivered)


@router.post("/{ticket_id}/close", response_model=TicketView)
async def close(ticket_id: int, actor: Support, notify_user: bool = True) -> TicketView:
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        try:
            before = await tickets.get(session, ticket_id)
            was_closed = before.status == TicketStatus.CLOSED
            await tickets.close(session, ticket_id, actor.tg_id, is_admin=True, source="web", ip=actor.ip)
        except tickets.TicketError as exc:
            raise _error(exc) from None
        user = await session.get(User, before.user_id)
    if notify_user and not was_closed and user is not None and not user.is_banned:
        text = t(ctx, "admin_api.notify.ticket_closed", user.language, ticket_id=ticket_id)
        await safe_send(ctx.bot, user.tg_id, text, session_factory=ctx.session_factory, parse_mode="HTML")
    async with read_session(ctx.session_factory) as session:
        return await _view(session, ticket_id)


@router.get("/{ticket_id}/messages/{message_id}/file")
async def attachment(ticket_id: int, message_id: int, actor: Support) -> Response:
    """Download a photo/document the user attached (fetched from Telegram)."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        message = await session.get(TicketMessage, message_id)
    if message is None or message.ticket_id != ticket_id or not message.file_id:
        raise not_found("No attachment")
    try:
        file = await ctx.bot.get_file(message.file_id)
        if file.file_size and file.file_size > MAX_FILE_BYTES:
            raise conflict("too_large", "The file is too large to download through the bot")
        data = await ctx.bot.download(file)
    except TelegramAPIError as exc:
        raise conflict("telegram_refused", str(exc)[:300]) from None
    if data is None:
        raise not_found("No attachment")
    name = (file.file_path or "file").rsplit("/", 1)[-1]
    return Response(
        content=data.getvalue(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )
