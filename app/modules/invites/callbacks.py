"""Callback data and FSM states of the invitation screens."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.state import State, StatesGroup

from app.modules.admin.common import admin_input_group

NAV = "adm_inv"  # Nav target: the invitations list (admin menu entry)
UNLIMITED = -1  # "no limit" in callback values (days / devices / uses)


class InvCb(CallbackData, prefix="inv"):
    """``action``: list | card | revoke | revoke_ok | users | qr; ``id``: invite id; ``page``: list page."""

    action: str
    id: int = 0
    page: int = 0


class InvNew(CallbackData, prefix="invn"):
    """The "new invitation" wizard: ``step`` days -> devices -> uses -> note; chosen values travel along."""

    step: str
    days: int = 0
    devices: int = 0
    uses: int = 0


@admin_input_group
class InviteInput(StatesGroup):
    note = State()  # who the invitation is for ("Мама"); data: days, devices, uses
