"""Callback data factories shared by every module.

Modules declare their own factories next to their handlers (``class BuyCb(CallbackData, prefix="buy")``); prefixes
must be short and unique. Telegram limits ``callback_data`` to 64 bytes: ``CallbackData.pack()`` already enforces it,
raw strings go through :func:`check_callback_data`.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

MAX_CALLBACK_BYTES = 64


class Nav(CallbackData, prefix="nav"):
    """Generic navigation: open screen ``to`` (optionally with an argument such as an id or a page)."""

    to: str
    arg: str = ""


MENU = "menu"  # Nav target of the main menu
NOOP = "noop"  # Nav target of decorative buttons (page counter); handlers just answer the callback


def check_callback_data(data: str) -> str:
    """Return ``data`` if Telegram will accept it as ``callback_data`` (1..64 bytes), else raise ``ValueError``."""
    size = len(data.encode("utf-8"))
    if not 1 <= size <= MAX_CALLBACK_BYTES:
        raise ValueError(f"callback_data must be 1..{MAX_CALLBACK_BYTES} bytes, got {size}: {data[:80]!r}")
    return data


def pack(data: CallbackData | str) -> str:
    """Packed and size-checked ``callback_data``."""
    return check_callback_data(data.pack() if isinstance(data, CallbackData) else data)
