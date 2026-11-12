"""Callback data of the broadcasts screens."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class BcCb(CallbackData, prefix="bcast"):
    """``a`` = action, ``id`` = broadcast id, ``v`` = an extra value (segment, page...)."""

    a: str
    id: int = 0
    v: str = ""
