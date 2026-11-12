"""In private chats the bot keeps one screen: typed answers and the previous screen are removed."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import Message

from app.core import ui
from app.core.ui import Screen, render
from tests.test_ui_render import CHAT_ID, FakeSession


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return Bot(token="42:TEST", session=session)


@pytest.fixture(autouse=True)
def clean_state() -> Iterator[None]:
    ui._screens.clear()
    ui.configure_single_screen(lambda: True)
    yield
    ui._screens.clear()
    ui.configure_single_screen(lambda: True)


def user_message(
    bot: Bot, message_id: int, *, text: str | None = "hello", photo: bool = False, chat: str = "private"
) -> Message:
    data: dict[str, Any] = {
        "message_id": message_id,
        "date": datetime.now(UTC),
        "chat": {"id": CHAT_ID, "type": chat},
        "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Ann"},
    }
    if photo:
        data["photo"] = [{"file_id": "receipt", "file_unique_id": "r", "width": 1, "height": 1}]
    elif text is not None:
        data["text"] = text
    return Message.model_validate(data, context={"bot": bot})


def deleted_ids(session: FakeSession) -> list[int]:
    return [call.message_id for call in session.of(DeleteMessage)]


async def test_typed_answer_replaces_the_previous_screen(bot: Bot, session: FakeSession) -> None:
    first = await render(user_message(bot, 1, text="/start"), Screen("menu"))
    assert first is not None
    assert deleted_ids(session) == [1]  # the command itself

    second = await render(user_message(bot, 2, text="PROMO2026"), Screen("promo applied"))
    assert second is not None
    assert sorted(deleted_ids(session)) == sorted([1, first.message_id, 2])
    assert [call.text for call in session.of(SendMessage)] == ["menu", "promo applied"]


async def test_photos_and_files_from_the_user_are_kept(bot: Bot, session: FakeSession) -> None:
    first = await render(user_message(bot, 1, text="/start"), Screen("menu"))
    assert first is not None
    await render(user_message(bot, 2, photo=True), Screen("receipt received"))
    assert 2 not in deleted_ids(session)  # the receipt photo stays
    assert first.message_id in deleted_ids(session)  # the old screen is still replaced


async def test_disabled_setting_keeps_every_message(bot: Bot, session: FakeSession) -> None:
    ui.configure_single_screen(lambda: False)
    await render(user_message(bot, 1, text="/start"), Screen("menu"))
    await render(user_message(bot, 2, text="hello"), Screen("answer"))
    assert deleted_ids(session) == []


async def test_group_chats_are_left_alone(bot: Bot, session: FakeSession) -> None:
    await render(user_message(bot, 1, text="/start", chat="group"), Screen("menu"))
    await render(user_message(bot, 2, text="hello", chat="group"), Screen("answer"))
    assert deleted_ids(session) == []


async def test_input_used_later_can_be_kept(bot: Bot, session: FakeSession) -> None:
    await render(user_message(bot, 1, text="/start"), Screen("menu"))
    await render(user_message(bot, 2, text="Broadcast text"), Screen("add buttons?"), keep_input=True)
    assert 2 not in deleted_ids(session)
