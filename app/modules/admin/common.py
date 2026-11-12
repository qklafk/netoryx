"""Building blocks shared by the bot admin sections: routers, input states, buttons and value parsing."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from aiogram import Router
from aiogram.filters import Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.state import StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton

from app.core.filters import IsAdmin, IsPrivate
from app.core.i18n import Translator
from app.core.ui import answer_callback
from app.keyboards import Button, nav
from app.middlewares.common import TTLCache
from app.utils.security import random_token

if TYPE_CHECKING:
    from app.db.models import User

ADMIN_NAV = "admin"  # Nav target of the admin menu: ``back(ADMIN_NAV, _)`` from any admin screen
PAGE_SIZE = 8
MAX_AMOUNT_KOP = 100_000_000_00  # 100 million rubles: anything larger is a typo

# Groups (``StatesGroup.__full_group_name__``) whose states are "the admin is typing something". Pressing another
# button or sending a command while in one of them abandons the input (see ``app.modules.admin.admin_bot``).
INPUT_GROUPS: set[str] = set()


def admin_input_group[G: type[StatesGroup]](group: G) -> G:
    """Class decorator: register a StatesGroup as an admin input (left automatically on navigation)."""
    INPUT_GROUPS.add(group.__full_group_name__)
    return group


class InAdminInput(Filter):
    """Passes while the admin is in one of the registered input states."""

    async def __call__(self, event: Any, raw_state: str | None = None) -> bool:
        return raw_state is not None and raw_state.split(":", 1)[0] in INPUT_GROUPS


class AdminInputCb(CallbackData, prefix="admi"):
    """Buttons that belong to an input in progress (skip, random code, choices): they keep the input state."""

    action: str
    value: str = ""


def admin_router(name: str) -> Router:
    """A router for admin screens: private chats with administrators only."""
    router = Router(name=name)
    for observer in (router.message, router.callback_query):
        observer.filter(IsPrivate())
        observer.filter(IsAdmin())
    return router


# --- one-shot confirmations ---------------------------------------------------------------------------------------

ONCE_TTL_SEC = 3600.0
_done: TTLCache[str, bool] = TTLCache(10_000)


def new_nonce() -> str:
    """A short random id put into a confirmation button, so a double tap performs the action once."""
    return random_token(6)


def first_time(key: str) -> bool:
    """True the first time ``key`` is seen (per process, for an hour)."""
    if key in _done:
        return False
    _done.set(key, True, ONCE_TTL_SEC)
    return True


def reset_once() -> None:
    """Forget every remembered confirmation (tests)."""
    _done.clear()


def forget(key: str) -> None:
    """Allow ``key`` again (the action failed and may be retried)."""
    _done.pop(key)


# --- buttons -------------------------------------------------------------------------------------------------------


def to_admin(_: Translator) -> InlineKeyboardButton:
    """ "◀️ Admin menu" button."""
    return nav(_("admin.back_to_menu"), ADMIN_NAV)


def input_button(text: str, action: str, value: str | int = "") -> InlineKeyboardButton:
    return Button.cb(text, AdminInputCb(action=action, value=str(value)))


async def deny(callback: CallbackQuery, _: Translator) -> None:
    """Tell the admin their role does not allow this action."""
    await answer_callback(callback, _("error.not_allowed"), alert=True)


# --- formatting ----------------------------------------------------------------------------------------------------


def user_title(user: User) -> str:
    """``Ann (@ann) · 123`` with every user-controlled part escaped."""
    parts = [Translator.esc(user.first_name or "—")]
    if user.username:
        parts.append(f"(@{Translator.esc(user.username)})")
    parts.append(f"· <code>{user.tg_id}</code>")
    return " ".join(parts)


def user_short(user: User) -> str:
    """Plain button label for a user (no HTML)."""
    name = (user.first_name or "").strip() or str(user.tg_id)
    label = f"{name} @{user.username}" if user.username else name
    return f"{label[:40]} · {user.tg_id}"


def when(_: Translator, value: datetime | None) -> str:
    return _.datetime(value) if value is not None else "—"


def yes_no(_: Translator, value: bool) -> str:
    return _("admin.yes") if value else _("admin.no")


def money_signed(_: Translator, kop: int) -> str:
    return ("+" if kop > 0 else "") + _.money(kop)


# --- parsing -------------------------------------------------------------------------------------------------------

_INT = re.compile(r"[+-]?\d{1,9}")
_AMOUNT = re.compile(r"[+-]?\d{1,12}(?:[.,]\d{1,2})?")


def parse_int(
    text: str | None, *, signed: bool = False, minimum: int | None = None, maximum: int | None = None
) -> int | None:
    """An integer typed by the admin (spaces allowed), or None if it is not one or out of range."""
    clean = (text or "").replace(" ", "").replace(" ", "")
    if not _INT.fullmatch(clean) or (not signed and clean[0] in "+-"):
        return None
    value = int(clean)
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def parse_amount_kop(text: str | None, *, signed: bool = False) -> int | None:
    """Rubles typed by the admin (``150``, ``-99,50``, ``1 000.5``) as kopecks; None if invalid or zero."""
    clean = (text or "").replace(" ", "").replace(" ", "").replace("₽", "")
    if not _AMOUNT.fullmatch(clean) or (not signed and clean[0] in "+-"):
        return None
    try:
        kop = int((Decimal(clean.replace(",", ".")) * 100).to_integral_value())
    except InvalidOperation:
        return None
    if kop == 0 or abs(kop) > MAX_AMOUNT_KOP:
        return None
    return kop
