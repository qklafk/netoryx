"""Support tickets: users open and answer tickets, admins work the inbox, notifications go both ways."""

from __future__ import annotations

from aiogram.methods import SendMessage, SendPhoto
from sqlalchemy import select

from app.db.models import AdminRole, Ticket, TicketMessage, TicketStatus
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.tickets.callbacks import TicketAdminCb, TicketCb
from app.services import notifications
from tests.test_mod_admin_tools_support import (
    OWNER,
    Harness,
    add_user,
    make_harness,
    media_message,
    module,
    release_module_routers,  # noqa: F401 - autouse fixture
)

USER = 77


async def _harness(session_factory: SessionFactory) -> Harness:
    h = await make_harness(session_factory, [module("tickets")])
    await add_user(session_factory, h.ctx.settings, USER)
    return h


async def _ticket(session_factory: SessionFactory) -> Ticket:
    async with session_factory() as session:
        ticket = await session.scalar(select(Ticket).order_by(Ticket.id.desc()))
    assert ticket is not None
    return ticket


async def _open_ticket(h: Harness, text: str = "Не подключается на iPhone") -> Ticket:
    await h.click(Nav(to="tickets").pack(), uid=USER, role=None)
    await h.click(h.button("Написать в поддержку"), uid=USER, role=None)
    await h.say(text, uid=USER, role=None)
    return await _ticket(h.ctx.session_factory)


async def test_support_screen_from_main_menu(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(Nav(to="tickets").pack(), uid=USER, role=None)
    assert "Поддержка TestVPN" in h.last()
    assert all("Мои обращения" not in text for text, _ in h.buttons())


async def test_user_opens_ticket_and_admins_are_notified(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    ticket = await _open_ticket(h)
    assert ticket.status == TicketStatus.OPEN
    assert ticket.subject.startswith("Не подключается")
    assert f"#{ticket.id}" in h.last()
    notifier = notifications.get_notifier(h.ctx)  # type: ignore[arg-type]
    await notifier.join()
    notices = [c for c in h.api.of(SendMessage) if int(c.chat_id) == OWNER]
    assert notices and "Новое обращение" in notices[-1].text
    data = notices[-1].reply_markup.inline_keyboard[0][0].callback_data
    assert data == TicketAdminCb(a="nview", id=ticket.id).pack()
    await notifier.close()


async def test_ticket_with_photo(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(TicketCb(a="new").pack(), uid=USER, role=None)
    await h.feed(media_message(USER, photo="PHOTO1", caption="скрин ошибки"), role=None)
    async with session_factory() as session:
        message = await session.scalar(select(TicketMessage))
    assert message is not None and message.file_id == "PHOTO1" and message.text == "скрин ошибки"


async def test_admin_replies_and_user_gets_it(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    ticket = await _open_ticket(h)
    await h.click(TicketAdminCb(a="inbox").pack())
    assert "Ждут ответа: <b>1</b>" in h.last()
    await h.click(h.button(f"#{ticket.id}"))
    assert "Не подключается на iPhone" in h.last()
    await h.click(h.button("Ответить"))
    await h.say("Обновите приложение, пожалуйста")
    assert "доставлен" in h.last()
    to_user = h.sent_to(USER)
    assert any("Обновите приложение" in text for text in to_user)
    ticket = await _ticket(session_factory)
    assert ticket.status == TicketStatus.ANSWERED

    # the user sees an unread mark, opens the ticket and answers
    await h.click(TicketCb(a="home").pack(), uid=USER, role=None)
    assert any("🔔 1" in text for text, _ in h.buttons())
    await h.click(TicketCb(a="view", id=ticket.id).pack(), uid=USER, role=None)
    assert "🛟 Поддержка" in h.last()
    await h.click(h.button("Ответить"), uid=USER, role=None)
    await h.say("Помогло, спасибо!", uid=USER, role=None)
    assert (await _ticket(session_factory)).status == TicketStatus.OPEN


async def test_admin_reply_with_attachment(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    ticket = await _open_ticket(h)
    await h.click(TicketAdminCb(a="reply", id=ticket.id).pack())
    await h.feed(media_message(OWNER, photo="HOWTO", caption="Вот инструкция"))
    photos = [c for c in h.api.of(SendPhoto) if int(c.chat_id) == USER]
    assert photos and photos[0].photo == "HOWTO"


async def test_close_by_admin_notifies_user(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    ticket = await _open_ticket(h)
    await h.click(TicketAdminCb(a="close", id=ticket.id).pack())
    assert (await _ticket(session_factory)).status == TicketStatus.CLOSED
    assert any("закрыто" in text for text in h.sent_to(USER))


async def test_user_closes_and_cannot_touch_others(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    ticket = await _open_ticket(h)
    await add_user(session_factory, h.ctx.settings, 88)
    await h.click(TicketCb(a="view", id=ticket.id).pack(), uid=88, role=None)
    assert "не найдено" in h.last()
    await h.click(TicketCb(a="close", id=ticket.id).pack(), uid=USER, role=None)
    assert (await _ticket(session_factory)).status == TicketStatus.CLOSED


async def test_open_tickets_limit(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    for n in range(3):
        await _open_ticket(h, f"вопрос {n}")
    await h.click(TicketCb(a="new").pack(), uid=USER, role=None)
    assert "несколько открытых" in (h.popups()[-1] or "")


async def test_notification_button_opens_ticket_in_private_chat(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    ticket = await _open_ticket(h)
    await h.click(TicketAdminCb(a="nview", id=ticket.id).pack())
    assert any(f"Обращение #{ticket.id}" in text for text in h.sent_to(OWNER))


async def test_support_role_has_inbox_viewer_does_not(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(TicketAdminCb(a="inbox").pack(), role=AdminRole.SUPPORT)
    assert "Обращения" in h.last()
    count = len(h.screens())
    await h.click(TicketAdminCb(a="inbox").pack(), role=AdminRole.VIEWER)
    assert len(h.screens()) == count
