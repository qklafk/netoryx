"""Invitations module: the admin wizard, card, revoke, who joined, QR, and accepting an invitation via /start."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import Invite, Purchase, PurchaseKind, Subscription
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.invites import module as invites_module
from app.modules.invites.callbacks import NAV, UNLIMITED, InvCb, InvNew
from app.services import invites
from tests.test_middlewares_support import callback_update, message_update
from tests.test_mod_personal_support import OWNER, Harness, add_user, harnesses

FRIEND = 5002
STRANGER = 5003


@pytest.fixture
async def harness_factory(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Any]:
    async with harnesses(session_factory, tmp_path) as factory:
        yield factory


async def _invites(sf: SessionFactory) -> list[Invite]:
    async with sf() as session:
        return list(await session.scalars(select(Invite).order_by(Invite.id)))


async def test_wizard_creates_an_invitation_with_a_note(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(invites_module)
    await h.feed(callback_update(OWNER, Nav(to=NAV).pack()))
    assert "Приглашений пока нет" in h.last_text()

    await h.feed(callback_update(OWNER, InvNew(step="days").pack()))
    assert "На какой срок" in h.last_text()
    await h.feed(callback_update(OWNER, InvNew(step="devices", days=30).pack()))
    assert "Сколько устройств" in h.last_text()
    await h.feed(callback_update(OWNER, InvNew(step="uses", days=30, devices=3).pack()))
    assert "Сколько человек" in h.last_text()
    await h.feed(callback_update(OWNER, InvNew(step="note", days=30, devices=3, uses=1).pack()))
    assert "Кого приглашаете" in h.last_text() and "1 месяц" in h.last_text()

    await h.feed(message_update(OWNER, "Мама"))
    (invite,) = await _invites(session_factory)
    assert (invite.note, invite.days, invite.device_limit, invite.max_uses) == ("Мама", 30, 3, 1)
    assert invite.created_by == OWNER
    text = h.last_text()
    assert "Мама" in text and f"https://t.me/test_bot?start=inv_{invite.code}" in text
    assert any(b.url and b.url.startswith("https://t.me/share/url?url=") for b in h.buttons())

    await h.feed(callback_update(OWNER, Nav(to=NAV).pack()))
    assert "🟢 Мама · 1 месяц" in [b.text for b in h.buttons()]


async def test_unlimited_invitation_without_note(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(invites_module)
    data = InvNew(step="create", days=UNLIMITED, devices=UNLIMITED, uses=UNLIMITED).pack()
    await h.feed(callback_update(OWNER, data))
    (invite,) = await _invites(session_factory)
    assert (invite.days, invite.device_limit, invite.max_uses, invite.note) == (None, 0, 0, None)
    assert "✅ Приглашение готово" in h.popups()
    assert "бессрочно" in h.last_text()


async def test_users_without_rights_see_nothing(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(invites_module)
    await h.feed(callback_update(FRIEND, InvNew(step="create", days=30, devices=1, uses=1).pack()))
    assert await _invites(session_factory) == []


async def _create(sf: SessionFactory, **kw: Any) -> Invite:
    await add_user(sf, OWNER)
    async with sf.begin() as session:
        return await invites.create(session, OWNER, **kw)


async def test_accepting_an_invitation(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(invites_module)
    invite = await _create(session_factory, days=None, device_limit=2, note="Дима")

    await h.feed(message_update(FRIEND, f"/start inv_{invite.code}", first_name="Дима"))
    assert "Добро пожаловать" in h.last_text() and "бессрочно" in h.last_text()
    assert any(b.callback_data == Nav(to="connect").pack() for b in h.buttons())
    async with session_factory() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.user_id == FRIEND))
        assert sub is not None and sub.is_complimentary and sub.device_limit == 2
        kinds = list(await session.scalars(select(Purchase.kind)))
    assert kinds == [PurchaseKind.INVITE]
    (note,) = h.sent_to(OWNER)
    assert "Дима" in note and "сработало" in note

    await h.feed(message_update(FRIEND, f"/start inv_{invite.code}"))
    assert "уже приняли" in h.last_text()


async def test_used_up_and_unknown_invitations(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(invites_module)
    invite = await _create(session_factory, days=30, max_uses=1)
    await h.feed(message_update(FRIEND, f"/start inv_{invite.code}"))
    await h.feed(message_update(STRANGER, f"/start inv_{invite.code}"))
    assert "уже воспользовались" in h.last_text()
    await h.feed(message_update(STRANGER, "/start inv_NOPE123"))
    assert "Такого приглашения нет" in h.last_text()


async def test_personal_mode_lets_invited_people_in(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(invites_module, app_mode="personal")
    invite = await _create(session_factory, days=90)
    await h.feed(message_update(STRANGER, "/start"))
    assert "только по приглашению" in h.last_text()
    await h.feed(message_update(FRIEND, f"/start inv_{invite.code}"))
    assert "Добро пожаловать" in h.last_text() and "до " in h.last_text()


async def test_revoke_users_and_qr(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(invites_module)
    invite = await _create(session_factory, days=30, max_uses=0)
    await h.feed(message_update(FRIEND, f"/start inv_{invite.code}", first_name="Оля"))

    await h.feed(callback_update(OWNER, InvCb(action="card", id=invite.id).pack()))
    assert "1 из ∞" in h.last_text()
    await h.feed(callback_update(OWNER, InvCb(action="users", id=invite.id).pack()))
    assert "Оля" in h.last_text()
    await h.feed(callback_update(OWNER, InvCb(action="qr", id=invite.id).pack()))
    assert h.photos()

    await h.feed(callback_update(OWNER, InvCb(action="revoke", id=invite.id).pack()))
    assert "Отозвать приглашение" in h.last_text()
    await h.feed(callback_update(OWNER, InvCb(action="revoke_ok", id=invite.id).pack()))
    assert "🚫 Приглашение отозвано" in h.popups()
    assert "отозвано" in h.last_text() and "Ссылка:" not in h.last_text()
    (row,) = await _invites(session_factory)
    assert not row.is_active

    await h.feed(callback_update(OWNER, InvCb(action="card", id=999).pack()))
    assert "Ничего не нашлось 🤷" in h.popups()


async def test_list_pagination(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(invites_module)
    await add_user(session_factory, OWNER)
    async with session_factory.begin() as session:
        for i in range(10):
            await invites.create(session, OWNER, days=30, note=f"n{i}")
    await h.feed(callback_update(OWNER, Nav(to=NAV).pack()))
    assert InvCb(action="list", page=1).pack() in h.button_data()
    await h.feed(callback_update(OWNER, InvCb(action="list", page=1).pack()))
    labels = [b.text for b in h.buttons()]
    assert any("n0" in label for label in labels) and InvCb(action="list", page=0).pack() in h.button_data()
