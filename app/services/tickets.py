"""Support tickets: a user opens a ticket, admins and the user exchange messages, either side closes it.

Status follows the conversation: a user message makes the ticket OPEN (waiting for support), an admin reply makes
it ANSWERED (waiting for the user). A user writing into a closed ticket reopens it.

Unread counters:
* support inbox - tickets waiting for an answer (:func:`inbox_count`);
* user - admin replies newer than the last one the user has seen (marker in ``kv``, :func:`mark_seen`).

Functions take the caller's ``session`` and never commit.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket, TicketMessage, TicketStatus
from app.services import audit, events, kv
from app.utils.time import utcnow

MAX_SUBJECT_LENGTH = 128
MAX_TEXT_LENGTH = 4000
MAX_OPEN_TICKETS_PER_USER = 3
_SEEN_KEY = "tickets:seen:{user_id}"


class TicketErrorCode(enum.StrEnum):
    EMPTY = "empty"
    TOO_LONG = "too_long"
    TOO_MANY_OPEN = "too_many_open"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"  # the ticket belongs to another user


class TicketError(Exception):
    def __init__(self, code: TicketErrorCode, message: str | None = None) -> None:
        super().__init__(message or code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class TicketView:
    ticket: Ticket
    messages: list[TicketMessage]


def _clean_text(text: str | None, file_id: str | None) -> str | None:
    cleaned = (text or "").strip()
    if not cleaned and not file_id:
        raise TicketError(TicketErrorCode.EMPTY)
    if len(cleaned) > MAX_TEXT_LENGTH:
        raise TicketError(TicketErrorCode.TOO_LONG)
    return cleaned or None


def _subject_from(subject: str | None, text: str | None) -> str:
    candidate = " ".join((subject or text or "").split())
    if not candidate:
        return "—"
    return candidate if len(candidate) <= MAX_SUBJECT_LENGTH else candidate[: MAX_SUBJECT_LENGTH - 1] + "…"


async def count_open(session: AsyncSession, user_id: int) -> int:
    """User's tickets that are not closed."""
    stmt = select(func.count(Ticket.id)).where(Ticket.user_id == user_id, Ticket.status != TicketStatus.CLOSED)
    return int(await session.scalar(stmt) or 0)


async def create(
    session: AsyncSession,
    user_id: int,
    text: str | None,
    *,
    subject: str | None = None,
    file_id: str | None = None,
) -> Ticket:
    """Open a ticket with its first message. Subject defaults to the beginning of the text.

    Raises :class:`TicketError` (EMPTY, TOO_LONG, TOO_MANY_OPEN).
    """
    body = _clean_text(text, file_id)
    if await count_open(session, user_id) >= MAX_OPEN_TICKETS_PER_USER:
        raise TicketError(TicketErrorCode.TOO_MANY_OPEN)
    now = utcnow()
    ticket = Ticket(
        user_id=user_id, subject=_subject_from(subject, body), status=TicketStatus.OPEN, created_at=now, updated_at=now
    )
    session.add(ticket)
    await session.flush()
    session.add(TicketMessage(ticket_id=ticket.id, author_id=user_id, is_admin=False, text=body, file_id=file_id))
    await session.flush()
    await events.track(session, "ticket.created", user_id=user_id, ticket_id=ticket.id)
    return ticket


async def get(session: AsyncSession, ticket_id: int, *, user_id: int | None = None) -> Ticket:
    """Ticket by id; with ``user_id`` it must belong to that user. Raises :class:`TicketError`."""
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise TicketError(TicketErrorCode.NOT_FOUND)
    if user_id is not None and ticket.user_id != user_id:
        raise TicketError(TicketErrorCode.FORBIDDEN)
    return ticket


async def reply(
    session: AsyncSession,
    ticket_id: int,
    author_id: int,
    text: str | None,
    *,
    is_admin: bool,
    file_id: str | None = None,
    source: str = "bot",
    ip: str | None = None,
) -> TicketMessage:
    """Add a message. Users may only write into their own tickets (a closed one is reopened).

    Admin replies set ANSWERED (also on a closed ticket, which stays closed) and are audited.
    """
    body = _clean_text(text, file_id)
    ticket = await get(session, ticket_id, user_id=None if is_admin else author_id)
    message = TicketMessage(ticket_id=ticket.id, author_id=author_id, is_admin=is_admin, text=body, file_id=file_id)
    session.add(message)
    if is_admin:
        if ticket.status != TicketStatus.CLOSED:
            ticket.status = TicketStatus.ANSWERED
        await audit.log(session, author_id, "ticket.reply", "ticket", ticket.id, source, ip, user_id=ticket.user_id)
    else:
        ticket.status = TicketStatus.OPEN
    ticket.updated_at = utcnow()
    await session.flush()
    return message


async def close(
    session: AsyncSession,
    ticket_id: int,
    actor_id: int,
    *,
    is_admin: bool,
    source: str = "bot",
    ip: str | None = None,
) -> Ticket:
    """Close a ticket (idempotent). Users may only close their own."""
    ticket = await get(session, ticket_id, user_id=None if is_admin else actor_id)
    if ticket.status == TicketStatus.CLOSED:
        return ticket
    ticket.status = TicketStatus.CLOSED
    ticket.updated_at = utcnow()
    if is_admin:
        await audit.log(session, actor_id, "ticket.close", "ticket", ticket.id, source, ip, user_id=ticket.user_id)
    await session.flush()
    return ticket


async def view(session: AsyncSession, ticket_id: int, *, user_id: int | None = None, limit: int = 100) -> TicketView:
    """Ticket with its last ``limit`` messages in chronological order."""
    ticket = await get(session, ticket_id, user_id=user_id)
    stmt = (
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket.id)
        .order_by(TicketMessage.id.desc())
        .limit(max(1, min(limit, 500)))
    )
    messages = list((await session.scalars(stmt)).all())
    messages.reverse()
    return TicketView(ticket, messages)


async def list_for_user(session: AsyncSession, user_id: int, *, limit: int = 20) -> list[Ticket]:
    """User's tickets, most recently updated first."""
    stmt = (
        select(Ticket)
        .where(Ticket.user_id == user_id)
        .order_by(Ticket.updated_at.desc(), Ticket.id.desc())
        .limit(max(1, min(limit, 200)))
    )
    return list((await session.scalars(stmt)).all())


async def list_inbox(
    session: AsyncSession,
    *,
    statuses: tuple[TicketStatus, ...] = (TicketStatus.OPEN,),
    limit: int = 50,
    offset: int = 0,
) -> list[Ticket]:
    """Support inbox: tickets in ``statuses``, the longest waiting first."""
    stmt = (
        select(Ticket)
        .where(Ticket.status.in_(statuses))
        .order_by(Ticket.updated_at, Ticket.id)
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
    )
    return list((await session.scalars(stmt)).all())


async def inbox_count(session: AsyncSession) -> int:
    """Tickets waiting for a support answer."""
    return int(await session.scalar(select(func.count(Ticket.id)).where(Ticket.status == TicketStatus.OPEN)) or 0)


def _seen_key(user_id: int) -> str:
    return _SEEN_KEY.format(user_id=user_id)


async def unread_for_user(session: AsyncSession, user_id: int) -> int:
    """Admin replies in the user's tickets the user has not seen yet."""
    marker = await kv.get(session, _seen_key(user_id))
    last_seen = int(marker) if marker and marker.isdigit() else 0
    stmt = (
        select(func.count(TicketMessage.id))
        .join(Ticket, Ticket.id == TicketMessage.ticket_id)
        .where(Ticket.user_id == user_id, TicketMessage.is_admin.is_(True), TicketMessage.id > last_seen)
    )
    return int(await session.scalar(stmt) or 0)


async def mark_seen(session: AsyncSession, user_id: int) -> None:
    """The user opened their tickets: every admin reply so far counts as read."""
    stmt = (
        select(func.max(TicketMessage.id))
        .join(Ticket, Ticket.id == TicketMessage.ticket_id)
        .where(Ticket.user_id == user_id, TicketMessage.is_admin.is_(True))
    )
    last = await session.scalar(stmt)
    if last is not None:
        await kv.set(session, _seen_key(user_id), str(int(last)))
