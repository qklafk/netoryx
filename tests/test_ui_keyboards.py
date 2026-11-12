from __future__ import annotations

from typing import Any

import pytest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton

from app.keyboards import MENU, NOOP, Button, Nav, back, cancel, check_callback_data, grid, kb, pack, paginate, to_menu
from app.keyboards.common import channel_gate_kb, confirm_kb, consent_kb, language_kb


def _(key: str, **kw: Any) -> str:
    return f"<{key}>"


class ItemCb(CallbackData, prefix="it"):
    id: int


class LangCb(CallbackData, prefix="lng"):
    lang: str


def test_nav_pack_roundtrip() -> None:
    data = Nav(to="sub", arg="12").pack()
    assert data == "nav:sub:12"
    assert Nav.unpack(data) == Nav(to="sub", arg="12")
    assert Nav.unpack("nav:menu:") == Nav(to="menu")


def test_callback_data_limit() -> None:
    assert check_callback_data("x" * 64) == "x" * 64
    with pytest.raises(ValueError):
        check_callback_data("x" * 65)
    with pytest.raises(ValueError):
        check_callback_data("я" * 33)  # 66 bytes in UTF-8
    with pytest.raises(ValueError):
        check_callback_data("")
    with pytest.raises(ValueError):
        pack(Nav(to="x" * 70))
    with pytest.raises(ValueError):
        Button.cb("t", "y" * 65)


def test_button_factories() -> None:
    cb = Button.cb("Buy", Nav(to="buy"), style="success", icon="5368324170671202286")
    assert cb.callback_data == "nav:buy:"
    assert cb.style == "success" and cb.icon_custom_emoji_id == "5368324170671202286"

    assert Button.url("Site", "https://example.com").url == "https://example.com"
    assert Button.url("Chat", "tg://resolve?domain=x").url == "tg://resolve?domain=x"
    with pytest.raises(ValueError):
        Button.url("bad", "javascript:alert(1)")

    web = Button.webapp("App", "https://bot.example.com/app")
    assert web.web_app is not None and web.web_app.url == "https://bot.example.com/app"
    with pytest.raises(ValueError):
        Button.webapp("App", "http://bot.example.com/app")

    copy = Button.copy("Copy", "https://bot.example.com/sub/abc")
    assert copy.copy_text is not None and copy.copy_text.text == "https://bot.example.com/sub/abc"
    with pytest.raises(ValueError):
        Button.copy("Copy", "x" * 257)
    with pytest.raises(ValueError):
        Button.copy("Copy", "")

    assert Button.pay("Pay").pay is True


def test_kb_drops_none_and_empty_rows() -> None:
    a = Button.cb("a", "a")
    markup = kb([[a, None], [None], [], [Button.cb("b", "b")]])
    assert [[b.text for b in row] for row in markup.inline_keyboard] == [["a"], ["b"]]
    with pytest.raises(ValueError):
        kb([[Button.cb(str(i), str(i)) for i in range(9)]])


def test_grid() -> None:
    buttons = [Button.cb(str(i), str(i)) for i in range(5)]
    assert [[b.text for b in row] for row in grid([*buttons, None], 2)] == [["0", "1"], ["2", "3"], ["4"]]
    with pytest.raises(ValueError):
        grid(buttons, 0)


def test_navigation_buttons() -> None:
    assert back("plans", _).text == "<nav.back>"
    assert back("plans", _).callback_data == "nav:plans:"
    assert back("plan", _, arg=5).callback_data == "nav:plan:5"
    assert cancel("menu", _).text == "<nav.cancel>"
    assert to_menu(_).callback_data == f"nav:{MENU}:"


def _texts(rows: list[list[InlineKeyboardButton]]) -> list[list[str]]:
    return [[b.text for b in row] for row in rows]


def test_paginate_middle_page() -> None:
    items = list(range(23))
    page = paginate(items, 1, 10, lambda i: Button.cb(f"#{i}", ItemCb(id=i)), lambda p: Nav(to="list", arg=str(p)))

    assert list(page.items) == list(range(10, 20))
    assert (page.page, page.pages, page.total) == (1, 3, 23)
    assert page.has_prev and page.has_next
    nav_row = page.rows[-1]
    assert [b.text for b in nav_row] == ["◀️", "2/3", "▶️"]
    assert nav_row[0].callback_data == "nav:list:0"
    assert nav_row[1].callback_data == f"nav:{NOOP}:"
    assert nav_row[2].callback_data == "nav:list:2"
    assert page.rows[0][0].callback_data == "it:10"


def test_paginate_edges_and_clamping() -> None:
    items = list(range(5))
    first = paginate(items, -3, 2, lambda i: Button.cb(str(i), str(i)), lambda p: f"p:{p}")
    assert first.page == 0
    assert [b.text for b in first.rows[-1]] == ["1/3", "▶️"]

    last = paginate(items, 99, 2, lambda i: Button.cb(str(i), str(i)), lambda p: f"p:{p}")
    assert last.page == 2 and list(last.items) == [4]
    assert [b.text for b in last.rows[-1]] == ["◀️", "3/3"]


def test_paginate_single_page_and_empty() -> None:
    one = paginate([1, 2], 0, 5, lambda i: Button.cb(str(i), str(i)), lambda p: f"p:{p}", columns=2)
    assert _texts(one.rows) == [["1", "2"]]
    no_items: list[int] = []
    empty = paginate(no_items, 0, 5, lambda i: Button.cb(str(i), str(i)), lambda p: f"p:{p}")
    assert empty.rows == [] and empty.pages == 1 and empty.total == 0
    with pytest.raises(ValueError):
        paginate([1], 0, 0, lambda i: Button.cb(str(i), str(i)), lambda p: f"p:{p}")


def test_common_keyboards() -> None:
    gate = channel_gate_kb(_, "https://t.me/channel", Nav(to="gate"))
    assert _texts(gate.inline_keyboard) == [["<access.channel.subscribe>"], ["<access.channel.check>"]]

    consent = consent_kb(_, "consent:ok", terms_url="https://ex.com/terms")
    assert _texts(consent.inline_keyboard) == [["<consent.terms>"], ["<consent.accept>"]]
    both = consent_kb(_, "consent:ok", terms_url="https://ex.com/t", privacy_url="https://ex.com/p")
    assert len(both.inline_keyboard[0]) == 2

    confirm = confirm_kb(_, "y", "n", danger=True)
    assert confirm.inline_keyboard[0][0].style == "danger"

    langs = language_kb(_, ["ru", "en"], LangCb, current="en")
    assert _texts(langs.inline_keyboard) == [["<lang.ru>"], ["✓ <lang.en>"], ["<nav.menu>"]]
    assert langs.inline_keyboard[0][0].callback_data == "lng:ru"
    assert language_kb(_, ["ru"]).inline_keyboard[0][0].callback_data == "nav:lang:ru"
