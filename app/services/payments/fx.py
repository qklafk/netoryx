"""Fixed-rate conversion of ruble amounts into a foreign charge currency.

Gateways that charge in USD / EUR / ... are configured with a fixed "rubles per 1 unit" rate. The wallet
is always credited with the original kopeck amount; the foreign amount is only what the buyer pays, so
conversions round up to the currency's smallest chargeable unit and never lose money.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, InvalidOperation

_CURRENCY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def parse_rate(value: str | Decimal | int | float | None) -> Decimal:
    """A positive, finite "rubles per 1 unit" rate. Raises ValueError otherwise."""
    if value is None or isinstance(value, bool):
        raise ValueError("exchange rate is not set")
    try:
        rate = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"invalid exchange rate: {value!r}") from exc
    if not rate.is_finite() or rate <= 0:
        raise ValueError(f"invalid exchange rate: {value!r}")
    return rate


def normalize_currency(code: str | None) -> str:
    """Upper-case ISO 4217 code. Raises ValueError for anything that is not three letters."""
    text = (code or "").strip().upper()
    if len(text) != 3 or not set(text) <= _CURRENCY_CHARS:
        raise ValueError(f"invalid currency code: {code!r}")
    return text


def kop_to_minor(amount_kop: int, rate: Decimal, exponent: int, *, whole_units: bool = False) -> int:
    """Convert kopecks into minor units of the foreign currency, rounding up.

    ``exponent`` is the number of decimals of the wire amount (2 for USD, 0 for JPY). With
    ``whole_units`` the amount is rounded up to a whole unit first (currencies that keep two decimals on
    the wire but cannot be charged in fractions).
    """
    if amount_kop < 0:
        raise ValueError("amount must not be negative")
    units = Decimal(amount_kop) / 100 / rate
    if whole_units:
        units = units.to_integral_value(rounding=ROUND_CEILING)
    return int((units * (Decimal(10) ** exponent)).to_integral_value(rounding=ROUND_CEILING))


def minor_to_str(minor: int, exponent: int) -> str:
    """Wire decimal string: (1999, 2) -> "19.99", (500, 0) -> "500"."""
    if exponent == 0:
        return str(minor)
    return f"{(Decimal(minor) / (Decimal(10) ** exponent)):.{exponent}f}"


def str_to_minor(value: object, exponent: int) -> int:
    """Parse a decimal amount into minor units exactly. Raises ValueError (also for sub-minor fractions)."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"invalid amount: {value!r}")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"invalid amount: {value!r}")
    scaled = number * (Decimal(10) ** exponent)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount has more than {exponent} decimals: {value!r}")
    return int(scaled)


def min_amount_kop(min_units: Decimal, rate: Decimal) -> int:
    """Smallest kopeck amount whose conversion reaches ``min_units`` of the foreign currency."""
    return int((min_units * rate * 100).to_integral_value(rounding=ROUND_CEILING))
