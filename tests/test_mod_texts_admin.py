"""Bot text editor: search, browse by section, edit with validation, preview, reset."""

from __future__ import annotations

from aiogram.methods import SendMessage

from app.db.models import AdminRole
from app.db.session import SessionFactory
from app.modules.admin_texts.admin_bot import TxCb
from tests.test_mod_admin_tools_support import (
    OWNER,
    Harness,
    make_harness,
    module,
    release_module_routers,  # noqa: F401 - autouse fixture
)


async def _harness(session_factory: SessionFactory) -> Harness:
    return await make_harness(session_factory, [module("admin_texts"), module("tickets")])


async def test_search_edit_preview_reset(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(TxCb(a="home").pack())
    assert "Тексты бота" in h.last()
    await h.click(h.button("Найти текст"))
    await h.say("zzz-nothing-like-this")
    assert "Ничего не нашлось" in h.last()
    await h.say("tickets.created")
    assert "<code>tickets.created</code>" in h.last()
    assert "{id}" in h.last()

    await h.click(h.button("✏️ RU"))
    await h.say("Обращение {unknown} принято")
    assert "Не сохранил" in h.last()
    await h.say("<b>Готово!</b> Обращение #{id} у нас")
    assert "сохранён" in h.last()
    assert h.ctx.i18n.t("tickets.created", "ru", id=5) == "<b>Готово!</b> Обращение #5 у нас"

    await h.click(h.button("👀 RU"))
    previews = [c for c in h.api.of(SendMessage) if int(c.chat_id) == OWNER]
    assert previews and previews[-1].text.startswith("<b>Готово!</b>")

    await h.click(TxCb(a="reset", l="ru").pack())
    assert "Вернул стандартный текст" in h.last()
    assert h.ctx.i18n.override("tickets.created", "ru") is None


async def test_telegram_formatting_becomes_html(session_factory: SessionFactory) -> None:
    from aiogram.types import MessageEntity

    from tests.test_mod_admin_tools_support import text_message

    h = await _harness(session_factory)
    await h.click(TxCb(a="find").pack())
    await h.say("tickets.closed_by_support")
    await h.click(h.button("✏️ EN"))
    update = text_message(OWNER, "Closed #{id}", entities=[MessageEntity(type="bold", offset=0, length=6)])
    await h.feed(update)
    assert h.ctx.i18n.override("tickets.closed_by_support", "en") == "<b>Closed</b> #{id}"


async def test_browse_by_section_and_changed_list(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.ctx.i18n.set_override("tickets.sent", "en", "Sent!", OWNER)
    await h.click(TxCb(a="mods").pack())
    assert any(text.startswith("broadcasts (") for text, _ in h.buttons())
    await h.click(TxCb(a="list", m="tickets").pack())
    assert "tickets" in h.last()
    await h.click(h.button("tickets.admin.inbox"))
    assert "<code>tickets.admin.inbox</code>" in h.last()
    await h.click(TxCb(a="home").pack())
    await h.click(h.button("Изменённые (1)"))
    await h.click(h.button("tickets.sent"))
    assert "изменён" in h.last()


async def test_needs_texts_permission(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(TxCb(a="home").pack(), role=AdminRole.SUPPORT)
    assert h.screens() == []
