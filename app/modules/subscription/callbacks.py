"""Callback data of the subscription screens (``sb*`` prefixes; ``callback_data`` is limited to 64 bytes).

Screens without parameters are opened with :class:`~app.keyboards.callbacks.Nav` and the targets below.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

# Nav targets handled by this module.
SUB = "sub"  # "My subscription" card
BUY = "buy"  # plans list
CONNECT = "connect"  # connection screen (subscription URL, apps, QR)
DEVICES = "devices"
ADDONS = "addons"
TRIAL = "trial"
QR = "qr"
LINKS = "my_links"

# Nav targets of other modules this module links to.
WALLET = "wallet"
SUPPORT = "tickets"


class SubAct(CallbackData, prefix="sbact"):
    """Actions on the subscription card: ``ar1``/``ar0`` auto-renew on/off, ``rot``/``rot!`` rotate the link."""

    act: str
    s: int = 0


class PlanCb(CallbackData, prefix="sbplan"):
    """Open a plan (``id``); ``t`` is the link token of a hidden plan."""

    id: int
    t: str = ""
    s: int = 0


class PayCb(CallbackData, prefix="sbpay"):
    """Pay for plan ``plan`` with ``via`` (``bal`` = wallet, else a gateway key); ``n`` makes a tap idempotent,
    ``t`` is the link token of a hidden plan."""

    plan: int
    via: str
    n: str = ""
    t: str = ""
    s: int = 0


class CheckCb(CallbackData, prefix="sbchk"):
    """Check the status of payment ``id``."""

    id: str


class ConnCb(CallbackData, prefix="sbcon"):
    """Connection guide: platform ``p`` (empty = platform list) and app ``a`` (empty = app list)."""

    p: str = ""
    a: str = ""
    s: int = 0


class DevCb(CallbackData, prefix="sbdev"):
    """Devices: ``del`` one (panel ``panel``, list index ``i``), ``all`` ask to reset all, ``all!`` confirmed."""

    act: str
    panel: int = 0
    i: int = 0
    s: int = 0


class AddonCb(CallbackData, prefix="sbadd"):
    """Buy a configured wallet-funded add-on; nonce makes a double tap idempotent."""

    k: str
    s: int
    n: str = ""


class ServerCb(CallbackData, prefix="sbsrv"):
    """An owned subscription's server, profile index, and optional QR view."""

    s: int
    server: str = ""
    i: int = -1
    q: bool = False
