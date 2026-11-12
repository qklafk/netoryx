"""Callback data of the server management screens."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class SrvCb(CallbackData, prefix="srv"):
    """``a`` = action; ``id`` = panel / inbound / endpoint / location id depending on the action; ``v`` = extra."""

    a: str
    id: int = 0
    v: str = ""
