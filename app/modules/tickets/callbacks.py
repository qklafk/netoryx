"""Callback data of the support screens (user side and admin side)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class TicketCb(CallbackData, prefix="tick"):
    """User side: ``a`` = action, ``id`` = ticket id."""

    a: str
    id: int = 0


class TicketAdminCb(CallbackData, prefix="tadm"):
    """Admin side: ``a`` = action, ``id`` = ticket id, ``v`` = inbox filter, ``p`` = page."""

    a: str
    id: int = 0
    v: str = ""
    p: int = 0
