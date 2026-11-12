"""Small helpers shared by the money screens (wallet, payments, referral, promo, gifts).

Text input steps are kept in the FSM with a timestamp: an abandoned step expires after :data:`INPUT_TTL_SEC`, so a
message typed much later (say, to support) is not mistaken for an amount or a promo code.
"""

from __future__ import annotations

import secrets
import time
from typing import Any
from urllib.parse import quote

from aiogram import F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State

from app.core.i18n import Translator

INPUT_TTL_SEC = 900.0
_STARTED_AT = "_input_at"

# Plain text that is not a command: commands typed during an input step go to their own handlers.
TEXT_INPUT = F.text & ~F.text.startswith("/")


async def start_input(state: FSMContext, step: State, **data: Any) -> None:
    """Enter an input step, replacing any previous FSM data."""
    await state.set_state(step)
    await state.set_data({_STARTED_AT: time.time(), **data})


async def input_data(state: FSMContext) -> dict[str, Any]:
    """Data of the current input step; an expired step is cleared and the update goes to other handlers."""
    data = await state.get_data()
    started = data.get(_STARTED_AT)
    if not isinstance(started, int | float) or time.time() - started > INPUT_TTL_SEC:
        await state.clear()
        raise SkipHandler
    return data


async def touch_input(state: FSMContext, **data: Any) -> None:
    """Update the step's data and restart its timer."""
    await state.update_data({_STARTED_AT: time.time(), **data})


def nonce(nbytes: int = 6) -> str:
    """Short random token for one-shot buttons (a double tap must not repeat a money operation)."""
    return secrets.token_hex(nbytes)


def signed_money(_: Translator, kop: int) -> str:
    """``+150 ₽`` / ``−290 ₽``."""
    if kop < 0:
        return "−" + _.money(-kop)
    return "+" + _.money(kop)


def share_url(link: str, text: str) -> str:
    """Telegram "share" link that opens the chat picker with ``text`` and ``link``."""
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(text, safe='')}"


def parse_rubles(text: str | None) -> int | None:
    """Whole rubles typed by a user ("500", "1 500", "500 ₽", "500р"); None if it is not such a number."""
    if text is None:
        return None
    cleaned = text.strip().lower().replace(" ", "").replace(" ", "")
    for suffix in ("₽", "руб.", "руб", "р.", "р", "rub"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    if not cleaned.isascii() or not cleaned.isdigit() or len(cleaned) > 7:
        return None
    return int(cleaned)
