from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageMedia,
    EditMessageText,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto, Message

from app.core.ui import Screen, answer_callback, delete_message, render, send_screen
from app.keyboards import Button, kb

CHAT_ID = 4242


class FakeSession(BaseSession):
    """Records every API call; ``fail[MethodName]`` makes that call raise."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.fail: dict[str, Callable[[TelegramMethod[Any]], Exception]] = {}
        self._next_id = 100

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:  # noqa: ASYNC109
        self.calls.append(method)
        name = type(method).__name__
        if name in self.fail:
            raise self.fail[name](method)
        if isinstance(method, SendMessage | EditMessageText):
            return self._message(bot, text=method.text)
        if isinstance(method, SendPhoto | EditMessageMedia):
            return self._message(bot, photo=[{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}])
        return True

    def _message(self, bot: Bot, **content: Any) -> Message:
        self._next_id += 1
        data = {"message_id": self._next_id, "date": datetime.now(UTC), "chat": {"id": CHAT_ID, "type": "private"}}
        return Message.model_validate(data | content, context={"bot": bot})

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    def names(self) -> list[str]:
        return [type(call).__name__ for call in self.calls]

    def of(self, cls: type[TelegramMethod[Any]]) -> list[Any]:
        return [call for call in self.calls if isinstance(call, cls)]


def bad_request(text: str) -> Callable[[TelegramMethod[Any]], Exception]:
    return lambda method: TelegramBadRequest(method=method, message=f"Bad Request: {text}")


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return Bot(token="42:TEST", session=session)


def message(bot: Bot, *, photo: bool = False, sticker: bool = False, message_id: int = 7) -> Message:
    data: dict[str, Any] = {
        "message_id": message_id,
        "date": datetime.now(UTC),
        "chat": {"id": CHAT_ID, "type": "private"},
    }
    if photo:
        data["photo"] = [{"file_id": "old", "file_unique_id": "old", "width": 1, "height": 1}]
        data["caption"] = "old"
    elif sticker:
        data["sticker"] = {
            "file_id": "s",
            "file_unique_id": "s",
            "type": "regular",
            "width": 1,
            "height": 1,
            "is_animated": False,
            "is_video": False,
        }
    else:
        data["text"] = "old"
    return Message.model_validate(data, context={"bot": bot})


def callback(bot: Bot, msg: dict[str, Any] | Message | None, *, inline_id: str | None = None) -> CallbackQuery:
    data: dict[str, Any] = {
        "id": "cb1",
        "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Ann"},
        "chat_instance": "ci",
        "data": "nav:menu:",
    }
    if msg is not None:
        data["message"] = msg.model_dump() if isinstance(msg, Message) else msg
    if inline_id:
        data["inline_message_id"] = inline_id
    return CallbackQuery.model_validate(data, context={"bot": bot})


SCREEN = Screen("<b>Hello</b>", kb([[Button.cb("Go", "nav:menu:")]]))
PHOTO_SCREEN = Screen("<b>Hello</b>", photo=b"\x89PNG")


async def test_callback_on_text_message_edits_in_place(bot: Bot, session: FakeSession) -> None:
    result = await render(callback(bot, message(bot)), SCREEN)

    assert session.names() == ["EditMessageText", "AnswerCallbackQuery"]
    edit = session.of(EditMessageText)[0]
    assert edit.text == "<b>Hello</b>"
    assert edit.parse_mode == "HTML"
    assert edit.reply_markup == SCREEN.kb
    assert edit.link_preview_options.is_disabled is True
    assert edit.chat_id == CHAT_ID and edit.message_id == 7
    assert isinstance(result, Message)


async def test_not_modified_is_ignored(bot: Bot, session: FakeSession) -> None:
    session.fail["EditMessageText"] = bad_request("message is not modified: specified new message content ...")
    original = message(bot)
    cq = callback(bot, original)

    result = await render(cq, SCREEN)

    assert session.names() == ["EditMessageText", "AnswerCallbackQuery"]
    assert result is not None and result.message_id == 7


async def test_unknown_edit_error_propagates_but_callback_is_answered(bot: Bot, session: FakeSession) -> None:
    session.fail["EditMessageText"] = bad_request("can't parse entities: unsupported start tag")

    with pytest.raises(TelegramBadRequest):
        await render(callback(bot, message(bot)), SCREEN)

    assert session.names() == ["EditMessageText", "AnswerCallbackQuery"]


async def test_deleted_message_falls_back_to_send(bot: Bot, session: FakeSession) -> None:
    session.fail["EditMessageText"] = bad_request("message to edit not found")

    result = await render(callback(bot, message(bot)), SCREEN)

    assert session.names() == ["EditMessageText", "SendMessage", "DeleteMessage", "AnswerCallbackQuery"]
    assert result is not None and result.message_id == 101
    assert session.of(SendMessage)[0].chat_id == CHAT_ID


async def test_text_to_photo_sends_new_and_deletes_old(bot: Bot, session: FakeSession) -> None:
    await render(callback(bot, message(bot)), PHOTO_SCREEN)

    assert session.names() == ["SendPhoto", "DeleteMessage", "AnswerCallbackQuery"]
    send = session.of(SendPhoto)[0]
    assert isinstance(send.photo, BufferedInputFile)
    assert send.caption == "<b>Hello</b>"
    assert session.of(DeleteMessage)[0].message_id == 7


async def test_photo_to_photo_edits_media(bot: Bot, session: FakeSession) -> None:
    screen = Screen("caption", photo="file-id-123")

    await render(callback(bot, message(bot, photo=True)), screen)

    assert session.names() == ["EditMessageMedia", "AnswerCallbackQuery"]
    media = session.of(EditMessageMedia)[0].media
    assert isinstance(media, InputMediaPhoto)
    assert media.media == "file-id-123"
    assert media.caption == "caption"


async def test_photo_to_text_sends_new_and_deletes_old(bot: Bot, session: FakeSession) -> None:
    await render(callback(bot, message(bot, photo=True)), SCREEN)

    assert session.names() == ["SendMessage", "DeleteMessage", "AnswerCallbackQuery"]


async def test_sticker_message_is_replaced(bot: Bot, session: FakeSession) -> None:
    await render(callback(bot, message(bot, sticker=True)), SCREEN)

    assert session.names() == ["SendMessage", "DeleteMessage", "AnswerCallbackQuery"]


async def test_failed_delete_is_ignored(bot: Bot, session: FakeSession) -> None:
    session.fail["DeleteMessage"] = bad_request("message can't be deleted for everyone")

    result = await render(callback(bot, message(bot, photo=True)), SCREEN)

    assert result is not None
    assert session.names() == ["SendMessage", "DeleteMessage", "AnswerCallbackQuery"]


async def test_long_caption_is_sent_as_text(bot: Bot, session: FakeSession) -> None:
    screen = Screen("<b>" + "x" * 1100 + "</b>", photo=b"img")
    assert screen.has_photo is False

    await render(callback(bot, message(bot)), screen)

    assert session.names() == ["EditMessageText", "AnswerCallbackQuery"]


def test_caption_limit_counts_visible_characters() -> None:
    assert Screen("<b>" + "x" * 1024 + "</b>", photo=b"img").has_photo is True
    assert Screen("&amp;" * 1024, photo=b"img").has_photo is True
    assert Screen("x" * 1025, photo=b"img").has_photo is False


async def test_inaccessible_message_sends_new(bot: Bot, session: FakeSession) -> None:
    cq = callback(bot, {"chat": {"id": CHAT_ID, "type": "private"}, "message_id": 5, "date": 0})

    await render(cq, SCREEN)

    assert session.names() == ["SendMessage", "AnswerCallbackQuery"]


async def test_inline_message_is_edited_by_inline_id(bot: Bot, session: FakeSession) -> None:
    result = await render(callback(bot, None, inline_id="inl-1"), SCREEN)

    assert result is None
    assert session.names() == ["EditMessageText", "AnswerCallbackQuery"]
    assert session.of(EditMessageText)[0].inline_message_id == "inl-1"


async def test_callback_without_message_sends_to_user(bot: Bot, session: FakeSession) -> None:
    await render(callback(bot, None), SCREEN)

    assert session.names() == ["SendMessage", "AnswerCallbackQuery"]
    assert session.of(SendMessage)[0].chat_id == CHAT_ID


async def test_message_event_sends_new(bot: Bot, session: FakeSession) -> None:
    result = await render(message(bot), Screen("hi", disable_preview=False))

    assert session.names() == ["SendMessage"]
    assert session.of(SendMessage)[0].link_preview_options.is_disabled is False
    assert result is not None and result.text == "hi"


async def test_message_event_with_photo(bot: Bot, session: FakeSession) -> None:
    await render(message(bot), PHOTO_SCREEN)

    assert session.names() == ["SendPhoto"]


async def test_callback_answer_text_and_alert(bot: Bot, session: FakeSession) -> None:
    await render(callback(bot, message(bot)), SCREEN, answer="Saved", alert=True)

    answer = session.of(AnswerCallbackQuery)[0]
    assert answer.text == "Saved" and answer.show_alert is True


async def test_answer_callback_swallows_old_query(bot: Bot, session: FakeSession) -> None:
    session.fail["AnswerCallbackQuery"] = bad_request("query is too old and response timeout expired")

    await answer_callback(callback(bot, message(bot)), "x")

    assert session.names() == ["AnswerCallbackQuery"]


async def test_send_screen_and_delete_message(bot: Bot, session: FakeSession) -> None:
    sent = await send_screen(bot, 77, SCREEN, thread_id=3)
    assert session.of(SendMessage)[0].chat_id == 77
    assert session.of(SendMessage)[0].message_thread_id == 3

    assert await delete_message(sent) is True
    assert await delete_message(None) is False
