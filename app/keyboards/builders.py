"""Inline keyboard builders.

kb([
    [Button.cb(_("menu.buy"), BuyCb(step="plans"))],
    [Button.copy(_("common.copy"), sub_url)],
    [back("menu", _)],
])
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.keyboards.callbacks import MENU, NOOP, Nav, pack

Translate = Callable[..., str]
Btn = InlineKeyboardButton
Row = Sequence[InlineKeyboardButton | None]

MAX_COPY_TEXT = 256  # CopyTextButton.text limit
MAX_ROW_WIDTH = 8  # Telegram renders at most 8 buttons per row
_URL_SCHEMES = ("https://", "http://", "tg://")


class Button:
    """Factories for inline buttons. ``style`` is ``"primary"``/``"success"``/``"danger"``; ``icon`` is a custom
    emoji id shown before the text (both are ignored by old clients)."""

    @staticmethod
    def cb(text: str, data: CallbackData | str, *, style: str | None = None, icon: str | None = None) -> Btn:
        return InlineKeyboardButton(text=text, callback_data=pack(data), style=style, icon_custom_emoji_id=icon)

    @staticmethod
    def url(text: str, url: str, *, style: str | None = None, icon: str | None = None) -> Btn:
        if not url.lower().startswith(_URL_SCHEMES):
            raise ValueError(f"unsupported button URL: {url!r}")
        return InlineKeyboardButton(text=text, url=url, style=style, icon_custom_emoji_id=icon)

    @staticmethod
    def webapp(text: str, url: str, *, style: str | None = None, icon: str | None = None) -> Btn:
        """Opens a Mini App (Telegram requires HTTPS)."""
        if not url.lower().startswith("https://"):
            raise ValueError(f"Mini App URL must be HTTPS: {url!r}")
        return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url), style=style, icon_custom_emoji_id=icon)

    @staticmethod
    def copy(text: str, value: str, *, style: str | None = None, icon: str | None = None) -> Btn:
        """Copies ``value`` to the clipboard (1..256 characters)."""
        if not 1 <= len(value) <= MAX_COPY_TEXT:
            raise ValueError(f"copy text must be 1..{MAX_COPY_TEXT} characters")
        return InlineKeyboardButton(
            text=text, copy_text=CopyTextButton(text=value), style=style, icon_custom_emoji_id=icon
        )

    @staticmethod
    def pay(text: str) -> Btn:
        """Pay button of an invoice message (must be the first button)."""
        return InlineKeyboardButton(text=text, pay=True)


def kb(rows: Iterable[Row]) -> InlineKeyboardMarkup:
    """Keyboard from rows; ``None`` buttons and empty rows are dropped (handy for conditional buttons)."""
    clean: list[list[InlineKeyboardButton]] = []
    for row in rows:
        buttons = [button for button in row if button is not None]
        if len(buttons) > MAX_ROW_WIDTH:
            raise ValueError(f"a keyboard row holds at most {MAX_ROW_WIDTH} buttons")
        if buttons:
            clean.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=clean)


def grid(buttons: Iterable[InlineKeyboardButton | None], columns: int) -> list[list[InlineKeyboardButton]]:
    """Lay buttons out in rows of ``columns``."""
    if not 1 <= columns <= MAX_ROW_WIDTH:
        raise ValueError(f"columns must be 1..{MAX_ROW_WIDTH}")
    items = [button for button in buttons if button is not None]
    return [items[i : i + columns] for i in range(0, len(items), columns)]


def nav(text: str, to: str, arg: str | int = "") -> InlineKeyboardButton:
    """Button that opens the screen ``to``."""
    return Button.cb(text, Nav(to=to, arg=str(arg)))


def back(to: str, _: Translate, arg: str | int = "") -> InlineKeyboardButton:
    """Back button ("◀️ Back") to the screen ``to``."""
    return nav(_("nav.back"), to, arg)


def cancel(to: str, _: Translate, arg: str | int = "") -> InlineKeyboardButton:
    """Cancel button leading to the screen ``to`` (handlers also clear the FSM state)."""
    return nav(_("nav.cancel"), to, arg)


def to_menu(_: Translate) -> InlineKeyboardButton:
    """Main menu button."""
    return nav(_("nav.menu"), MENU)


def noop(text: str) -> InlineKeyboardButton:
    """Decorative button (e.g. a page counter)."""
    return nav(text, NOOP)


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of a paginated list: ``rows`` = item buttons + the navigation row (if more than one page)."""

    items: Sequence[T]
    page: int  # zero-based, clamped into range
    pages: int
    total: int
    rows: list[list[InlineKeyboardButton]] = field(default_factory=list)

    @property
    def has_prev(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return self.page + 1 < self.pages


def paginate[T](
    items: Sequence[T],
    page: int,
    per_page: int,
    make_btn: Callable[[T], InlineKeyboardButton],
    nav: Callable[[int], CallbackData | str],
    *,
    columns: int = 1,
) -> Page[T]:
    """Slice ``items`` to ``page`` and build buttons: ``make_btn(item)`` per item, ``nav(page)`` for ◀️ / ▶️."""
    if per_page < 1:
        raise ValueError("per_page must be positive")
    total = len(items)
    pages = max(1, -(-total // per_page))
    page = min(max(page, 0), pages - 1)
    chunk = items[page * per_page : (page + 1) * per_page]
    rows = grid((make_btn(item) for item in chunk), columns)
    if pages > 1:
        nav_row = [noop(f"{page + 1}/{pages}")]
        if page > 0:
            nav_row.insert(0, Button.cb("◀️", nav(page - 1)))
        if page + 1 < pages:
            nav_row.append(Button.cb("▶️", nav(page + 1)))
        rows.append(nav_row)
    return Page(items=chunk, page=page, pages=pages, total=total, rows=rows)
