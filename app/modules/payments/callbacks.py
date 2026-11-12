"""Callback data of the payment screens.

Other modules open the payment method picker with a :class:`PayCb` button, for example a cart that tops up exactly
the missing amount and buys the plan right after the payment::

    Button.cb(_("..."), PayCb(a="m", k=Purpose.PLAN, plan=plan.id))

Amounts in callback data are only hints: every value is validated again on the server.
"""

from __future__ import annotations

from enum import StrEnum

from aiogram.filters.callback_data import CallbackData


class Purpose(StrEnum):
    """What a payment is for (one letter: callback data is limited to 64 bytes)."""

    TOPUP = "t"  # wallet top-up
    PLAN = "p"  # buy a plan right after the payment (tops up the missing amount)
    GIFT = "g"  # buy a plan as a gift code
    DONATION = "d"  # Telegram Stars donation (never credited to the wallet)


class PayCb(CallbackData, prefix="pay"):
    """``a``: ``m`` = method picker, ``go`` = create the invoice with provider ``prov``, ``don`` = donation.

    ``amt`` is kopecks for top-ups and Stars for donations; ``plan`` is a plan id (0 = none); ``r=1`` asks for a
    Telegram Stars subscription that renews every 30 days, ``r=2`` pays by card and keeps it for auto-renewal.
    """

    a: str
    k: str = Purpose.TOPUP.value
    amt: int = 0
    plan: int = 0
    prov: str = ""
    r: int = 0


class PayCheckCb(CallbackData, prefix="payc"):
    """The "check payment" button of an invoice."""

    id: str
