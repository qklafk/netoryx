"""Support tickets: lifecycle, ownership checks and unread counters."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import AdminAuditLog, TicketStatus
from app.db.session import SessionFactory
from app.services import tickets
from app.services.tickets import TicketError, TicketErrorCode
from tests.test_subs_support import add_user

USER = 100
OTHER = 101
ADMIN = 1000


async def _users(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        await add_user(s, USER)
        await add_user(s, OTHER)


async def test_conversation_flow(session_factory: SessionFactory) -> None:
    await _users(session_factory)
    async with session_factory.begin() as s:
        ticket = await tickets.create(s, USER, "  The VPN does not connect on my phone  ")
    assert ticket.status == TicketStatus.OPEN
    assert ticket.subject == "The VPN does not connect on my phone"
    async with session_factory.begin() as s:
        assert await tickets.inbox_count(s) == 1
        await tickets.reply(s, ticket.id, ADMIN, "Try reinstalling the app", is_admin=True)
    async with session_factory.begin() as s:
        assert (await tickets.get(s, ticket.id)).status == TicketStatus.ANSWERED
        assert await tickets.inbox_count(s) == 0
        assert await tickets.unread_for_user(s, USER) == 1
        await tickets.mark_seen(s, USER)
        assert await tickets.unread_for_user(s, USER) == 0
        await tickets.reply(s, ticket.id, USER, None, is_admin=False, file_id="photo-1")
    async with session_factory.begin() as s:
        view = await tickets.view(s, ticket.id, user_id=USER)
        assert view.ticket.status == TicketStatus.OPEN
        assert [(m.is_admin, m.text, m.file_id) for m in view.messages] == [
            (False, "The VPN does not connect on my phone", None),
            (True, "Try reinstalling the app", None),
            (False, None, "photo-1"),
        ]
        closed = await tickets.close(s, ticket.id, ADMIN, is_admin=True, source="web", ip="10.0.0.1")
        assert closed.status == TicketStatus.CLOSED
        await tickets.close(s, ticket.id, ADMIN, is_admin=True)  # idempotent
    async with session_factory() as s:
        actions = (await s.scalars(select(AdminAuditLog.action).order_by(AdminAuditLog.id))).all()
    assert actions == ["ticket.reply", "ticket.close"]


async def test_user_message_reopens_closed_ticket(session_factory: SessionFactory) -> None:
    await _users(session_factory)
    async with session_factory.begin() as s:
        ticket = await tickets.create(s, USER, "help", subject="Payment")
        await tickets.close(s, ticket.id, USER, is_admin=False)
        await tickets.reply(s, ticket.id, ADMIN, "late answer", is_admin=True)
        assert (await tickets.get(s, ticket.id)).status == TicketStatus.CLOSED
        await tickets.reply(s, ticket.id, USER, "still broken", is_admin=False)
        assert (await tickets.get(s, ticket.id)).status == TicketStatus.OPEN
        assert ticket.subject == "Payment"


async def test_users_cannot_touch_foreign_tickets(session_factory: SessionFactory) -> None:
    await _users(session_factory)
    async with session_factory.begin() as s:
        ticket = await tickets.create(s, USER, "mine")
        for call in (
            tickets.reply(s, ticket.id, OTHER, "hi", is_admin=False),
            tickets.close(s, ticket.id, OTHER, is_admin=False),
            tickets.view(s, ticket.id, user_id=OTHER),
        ):
            with pytest.raises(TicketError) as exc:
                await call
            assert exc.value.code == TicketErrorCode.FORBIDDEN
        with pytest.raises(TicketError) as missing:
            await tickets.get(s, 999)
        assert missing.value.code == TicketErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    ("text", "code"),
    [("   ", TicketErrorCode.EMPTY), (None, TicketErrorCode.EMPTY), ("x" * 4001, TicketErrorCode.TOO_LONG)],
)
async def test_message_validation(session_factory: SessionFactory, text: str | None, code: TicketErrorCode) -> None:
    await _users(session_factory)
    async with session_factory.begin() as s:
        with pytest.raises(TicketError) as exc:
            await tickets.create(s, USER, text)
    assert exc.value.code == code


async def test_open_ticket_limit_and_lists(session_factory: SessionFactory) -> None:
    await _users(session_factory)
    async with session_factory.begin() as s:
        created = [await tickets.create(s, USER, f"issue {n}") for n in range(tickets.MAX_OPEN_TICKETS_PER_USER)]
        with pytest.raises(TicketError) as exc:
            await tickets.create(s, USER, "one more")
        assert exc.value.code == TicketErrorCode.TOO_MANY_OPEN
        await tickets.close(s, created[0].id, USER, is_admin=False)
        await tickets.create(s, USER, "now allowed")
        await tickets.create(s, OTHER, "other user")
    async with session_factory() as s:
        assert len(await tickets.list_for_user(s, USER)) == 4
        inbox = await tickets.list_inbox(s)
        assert len(inbox) == 4 and all(t.status == TicketStatus.OPEN for t in inbox)
        assert len(await tickets.list_inbox(s, statuses=(TicketStatus.CLOSED,))) == 1
        assert await tickets.count_open(s, USER) == 3


def test_long_subject_is_truncated() -> None:
    subject = tickets._subject_from(None, "word " * 100)
    assert len(subject) == tickets.MAX_SUBJECT_LENGTH and subject.endswith("…")
    assert tickets._subject_from(None, None) == "—"
