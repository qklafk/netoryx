"""Administrators in the bot (owner only): add by id / @username / forward, role, note, sessions, remove."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram.methods import DeleteMyCommands, SetMyCommands
from aiogram.types import MessageOriginUser
from sqlalchemy import select

from app.db.models import Admin, AdminRole
from app.db.session import SessionFactory
from app.modules.admin_admins.admin_bot import AdmCb
from tests.test_mod_admin_tools_support import (
    OWNER,
    Harness,
    add_user,
    make_harness,
    module,
    release_module_routers,  # noqa: F401 - autouse fixture
    text_message,
    tg_user,
)


async def _harness(session_factory: SessionFactory) -> Harness:
    return await make_harness(session_factory, [module("admin_admins"), module("broadcasts")])


async def _admin(session_factory: SessionFactory, tg_id: int) -> Admin | None:
    async with session_factory() as session:
        return await session.get(Admin, tg_id)


async def test_list_shows_env_owner_read_only(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(AdmCb(a="home").pack())
    assert "Администраторы" in h.last()
    await h.click(h.button("🔒"))
    assert "ADMIN_IDS" in h.last()
    assert all("Забрать права" not in text for text, _ in h.buttons())


async def test_add_by_username_change_role_note_and_remove(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await add_user(session_factory, h.ctx.settings, 4242, username="helper")
    await h.click(AdmCb(a="add").pack())
    await h.say("@nobody_here")
    assert "Не нашёл" in h.last()
    await h.say("@helper")
    await h.click(h.button("Поддержка"))
    admin = await _admin(session_factory, 4242)
    assert admin is not None and admin.role == AdminRole.SUPPORT and admin.added_by == OWNER
    assert h.api.of(SetMyCommands), "the new admin gets the admin commands"

    await h.click(AdmCb(a="role", id=4242, v="admin").pack())
    assert (await _admin(session_factory, 4242)).role == AdminRole.ADMIN  # type: ignore[union-attr]
    await h.click(AdmCb(a="note", id=4242).pack())
    await h.say("модератор чата")
    assert (await _admin(session_factory, 4242)).note == "модератор чата"  # type: ignore[union-attr]
    await h.click(AdmCb(a="sess", id=4242).pack())
    assert "Завершено веб-сессий: 0" in h.last()
    await h.click(AdmCb(a="rmy", id=4242).pack())
    assert await _admin(session_factory, 4242) is None
    assert h.api.of(DeleteMyCommands)


async def test_add_by_forwarded_message_and_duplicates(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(AdmCb(a="add").pack())
    origin = MessageOriginUser(date=datetime.now(UTC), sender_user=tg_user(5151))
    await h.feed(text_message(OWNER, "переслано", forward_origin=origin))
    await h.click(h.button("Наблюдатель"))
    assert (await _admin(session_factory, 5151)).role == AdminRole.VIEWER  # type: ignore[union-attr]
    await h.click(AdmCb(a="addrole", id=5151, v="admin").pack())
    assert "уже администратор" in (h.popups()[-1] or "")
    await h.click(AdmCb(a="addrole", id=OWNER, v="admin").pack())
    assert "ADMIN_IDS" in (h.popups()[-1] or "")


async def test_only_owner(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(AdmCb(a="home").pack(), role=AdminRole.ADMIN)
    assert h.screens() == []
    async with session_factory() as session:
        assert (await session.scalars(select(Admin))).all() == []
