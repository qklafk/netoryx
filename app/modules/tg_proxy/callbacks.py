"""Callback data of the Telegram proxy screens."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

# Nav targets that open the proxy screen (main menu button, notifications).
NAV_TARGETS = frozenset({"tg_proxy", "proxy"})
NAV = "tg_proxy"


class ProxyCb(CallbackData, prefix="tgp"):
    """``action``: show | qr | rotate | rotate_ok | faq; ``arg``: index of the server (QR)."""

    action: str
    arg: int = 0
