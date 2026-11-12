"""Byte-size helpers. Binary units (1 GB = 1024**3 bytes), as 3x-ui and Remnawave count traffic."""

from __future__ import annotations

GIB = 1024**3
_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def fmt_bytes(n: int) -> str:
    """Human-readable size with at most one decimal: ``512 -> "512 B"``, ``1610612736 -> "1.5 GB"``."""
    sign = "-" if n < 0 else ""
    value = float(abs(n))
    unit = 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{sign}{int(value)} B"
    text = f"{value:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    if text == "1024" and unit < len(_UNITS) - 1:  # 1023.95 KB rounds up to the next unit
        text, unit = "1", unit + 1
    return f"{sign}{text} {_UNITS[unit]}"


def gb_to_bytes(gb: int | float) -> int:
    """Convert (binary) gigabytes to bytes; ``0`` stays ``0`` (= unlimited in limits)."""
    if gb < 0:
        raise ValueError("traffic limit cannot be negative")
    return int(gb * GIB)
