"""Callback data of the members screens."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

NAV = "adm_members"  # Nav target: the members list (admin menu entry)
UNLIMITED = -1


class MemCb(CallbackData, prefix="mem"):
    """``action`` on member ``uid``; ``value`` carries the chosen option; ``page`` returns to the list page.

    Actions: list, card, ext (choices), ext_ok, pause, resume, lim (choices), dev_ok, gb_ok, reset, rot, rot_ok,
    del, del_ok.
    """

    action: str
    uid: int = 0
    value: int = 0
    page: int = 0
