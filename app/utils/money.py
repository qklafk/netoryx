"""Money helpers. Internally all amounts are integer kopecks (1 ruble = 100 kopecks)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

KOP_PER_RUB = 100

_RUB_INPUT = re.compile(r"[0-9]{1,7}", re.ASCII)


def rub(kop: int) -> str:
    """Format kopecks as rubles: ``15000 -> "150"``, ``15050 -> "150.50"``, ``-5 -> "-0.05"``."""
    sign = "-" if kop < 0 else ""
    whole, frac = divmod(abs(kop), KOP_PER_RUB)
    return f"{sign}{whole}" if frac == 0 else f"{sign}{whole}.{frac:02d}"


def rub_to_kop(value: int | Decimal | str | float) -> int:
    """Convert a ruble amount to kopecks exactly.

    Accepts ``int``, ``Decimal``, ``float`` (via its shortest repr) and strings such as ``"199.00"``,
    ``" 199,5 "``. Raises ``ValueError`` for non-numeric, non-finite or sub-kopeck amounts
    (``"1.005"``) and ``TypeError`` for unsupported types (including ``bool``).
    """
    if isinstance(value, bool) or not isinstance(value, int | Decimal | str | float):
        raise TypeError(f"unsupported amount type: {type(value).__name__}")
    if isinstance(value, int):
        return value * KOP_PER_RUB
    amount = value if isinstance(value, Decimal) else _to_decimal(value)
    if not amount.is_finite():
        raise ValueError(f"not a finite amount: {value!r}")
    kop = amount * KOP_PER_RUB
    if kop != kop.to_integral_value():
        raise ValueError(f"amount has fractions of a kopeck: {value!r}")
    return int(kop)


def _to_decimal(value: str | float) -> Decimal:
    text = value.strip().replace(",", ".") if isinstance(value, str) else repr(value)
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not a number: {value!r}") from None


def parse_rub(text: str | None) -> int | None:
    """Parse user input of whole rubles (1-7 ASCII digits, surrounding spaces allowed).

    Returns the amount in RUBLES, or ``None`` if the text is not such a number. Range checks are the
    caller's job.
    """
    if text is None:
        return None
    candidate = text.strip()
    if not _RUB_INPUT.fullmatch(candidate):
        return None
    return int(candidate)
