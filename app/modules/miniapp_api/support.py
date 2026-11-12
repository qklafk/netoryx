"""Support tickets."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from app.db.models import Ticket, User
from app.modules.miniapp_api import deps
from app.modules.miniapp_api.deps import TICKETS, error, needs
from app.modules.miniapp_api.schemas import (
    TicketCreateIn,
    TicketMessageOut,
    TicketOut,
    TicketReplyIn,
    TicketsOut,
    TicketViewOut,
)
from app.services import tickets
from app.services.notifications import AdminEvent
from app.services.tickets import TicketError, TicketErrorCode
from app.web.auth import MiniAppUser
from app.web.deps import Ctx, DbSession, ReadDb

log = logging.getLogger(__name__)

router = APIRouter(tags=["Mini App: support"], dependencies=[needs(TICKETS)])

CREATE_LIMIT = (5, 600)  # new tickets per user per 10 minutes
REPLY_LIMIT = (20, 600)
TICKETS_SHOWN = 20
PREVIEW_LENGTH = 500  # characters of the user's message quoted in the admin notification

_STATUS = {
    TicketErrorCode.EMPTY: 422,
    TicketErrorCode.TOO_LONG: 422,
    TicketErrorCode.TOO_MANY_OPEN: 409,
    TicketErrorCode.NOT_FOUND: 404,
    TicketErrorCode.FORBIDDEN: 404,  # do not reveal that someone else's ticket exists
}


def _ticket_error(ctx: Any, user: User, exc: TicketError) -> Exception:
    code = TicketErrorCode.NOT_FOUND if exc.code is TicketErrorCode.FORBIDDEN else exc.code
    return error(ctx, user, _STATUS.get(exc.code, 400), f"ticket_{code.value}", max=tickets.MAX_OPEN_TICKETS_PER_USER)


def ticket_out(ticket: Ticket) -> TicketOut:
    return TicketOut(
        id=ticket.id,
        subject=ticket.subject,
        status=ticket.status.value,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )


def _preview(text: str) -> str:
    return text if len(text) <= PREVIEW_LENGTH else text[: PREVIEW_LENGTH - 1] + "…"


def _notify(ctx: Any, user: User, key: str, ticket_id: int, message: str) -> None:
    deps.notify_admins(
        ctx,
        AdminEvent.TICKETS,
        key,
        id=ticket_id,
        user_id=user.tg_id,
        username=ctx.i18n.esc(f"@{user.username}" if user.username else ""),
        text=ctx.i18n.esc(_preview(message)),
    )


@router.get("/tickets", response_model=TicketsOut, summary="My tickets (marks support replies as read)")
async def list_tickets(user: MiniAppUser, db: DbSession) -> TicketsOut:
    rows = await tickets.list_for_user(db, user.tg_id, limit=TICKETS_SHOWN)
    open_count = await tickets.count_open(db, user.tg_id)
    await tickets.mark_seen(db, user.tg_id)
    return TicketsOut(
        items=[ticket_out(t) for t in rows], open_count=open_count, max_open=tickets.MAX_OPEN_TICKETS_PER_USER
    )


@router.post("/tickets", response_model=TicketViewOut, status_code=201, summary="Open a ticket")
async def create_ticket(body: TicketCreateIn, request: Request, ctx: Ctx, user: MiniAppUser) -> TicketViewOut:
    await deps.rate_limit(request, ctx, user, "ticket_create", *CREATE_LIMIT)
    try:
        async with ctx.session_factory.begin() as session:
            ticket = await tickets.create(session, user.tg_id, body.text, subject=body.subject)
            view = await tickets.view(session, ticket.id, user_id=user.tg_id)
            out = _view_out(view)
    except TicketError as exc:
        raise _ticket_error(ctx, user, exc) from None
    _notify(ctx, user, "admin.ticket_new", out.ticket.id, body.text)
    return out


@router.get("/tickets/{ticket_id}", response_model=TicketViewOut, summary="A ticket with its messages")
async def get_ticket(ticket_id: int, ctx: Ctx, user: MiniAppUser, db: ReadDb) -> TicketViewOut:
    try:
        view = await tickets.view(db, ticket_id, user_id=user.tg_id)
    except TicketError as exc:
        raise _ticket_error(ctx, user, exc) from None
    return _view_out(view)


@router.post("/tickets/{ticket_id}/reply", response_model=TicketViewOut, summary="Write into a ticket")
async def reply(ticket_id: int, body: TicketReplyIn, request: Request, ctx: Ctx, user: MiniAppUser) -> TicketViewOut:
    await deps.rate_limit(request, ctx, user, "ticket_reply", *REPLY_LIMIT)
    try:
        async with ctx.session_factory.begin() as session:
            await tickets.reply(session, ticket_id, user.tg_id, body.text, is_admin=False, source="miniapp")
            out = _view_out(await tickets.view(session, ticket_id, user_id=user.tg_id))
    except TicketError as exc:
        raise _ticket_error(ctx, user, exc) from None
    _notify(ctx, user, "admin.ticket_reply", ticket_id, body.text)
    return out


@router.post("/tickets/{ticket_id}/close", response_model=TicketOut, summary="Close a ticket")
async def close(ticket_id: int, ctx: Ctx, user: MiniAppUser) -> TicketOut:
    try:
        async with ctx.session_factory.begin() as session:
            ticket = await tickets.close(session, ticket_id, user.tg_id, is_admin=False, source="miniapp")
            out = ticket_out(ticket)
    except TicketError as exc:
        raise _ticket_error(ctx, user, exc) from None
    return out


def _view_out(view: tickets.TicketView) -> TicketViewOut:
    return TicketViewOut(
        ticket=ticket_out(view.ticket),
        messages=[
            TicketMessageOut(
                id=m.id,
                from_support=m.is_admin,
                text=m.text,
                has_attachment=m.file_id is not None,
                created_at=m.created_at,
            )
            for m in view.messages
        ],
    )
