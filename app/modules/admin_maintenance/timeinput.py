"""Parsing of dates and times typed by admins in the bot (local time zone of the service).

Accepted forms (case-insensitive)::

    18:30                 today at 18:30, tomorrow if that time has passed
    25.12 18:30           this year (next year if the date has passed)
    25.12.2026 18:30      explicit year (two or four digits)
    +30m  +2h  +1d        relative: minutes / hours / days (also "м", "ч", "д")
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, tzinfo

_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_DATE_TIME = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?\s+(\d{1,2})[:.](\d{2})$")
_RELATIVE = re.compile(r"^\+\s*(\d{1,5})\s*([mhdмчд])$")
_UNITS = {"m": "minutes", "м": "minutes", "h": "hours", "ч": "hours", "d": "days", "д": "days"}
MAX_RELATIVE = timedelta(days=366)
MAX_RANGE = timedelta(days=31)  # longer "windows" are typos (e.g. a date that rolled over to next year)
_RANGE_SEPARATORS = (" - ", " — ", " – ", "—", "–")


def _local_dt(year: int, month: int, day: int, hour: int, minute: int, zone: tzinfo) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=zone)
    except ValueError:
        return None


def parse_relative(text: str) -> timedelta | None:
    """``"+2h"`` -> 2 hours; None if ``text`` is not a relative duration."""
    match = _RELATIVE.match(text.strip().lower())
    if match is None:
        return None
    delta = timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
    if not timedelta(0) < delta <= MAX_RELATIVE:
        return None
    return delta


def parse_moment(text: str, zone: tzinfo, now: datetime) -> datetime | None:
    """A point in time typed by an admin, as an aware UTC datetime (None if it cannot be parsed)."""
    value = " ".join(text.strip().lower().split())
    if not value:
        return None
    delta = parse_relative(value)
    if delta is not None:
        return (now + delta).astimezone(UTC)
    local_now = now.astimezone(zone)
    match = _TIME.match(value)
    if match is not None:
        hour, minute = int(match.group(1)), int(match.group(2))
        moment = _local_dt(local_now.year, local_now.month, local_now.day, hour, minute, zone)
        if moment is None:
            return None
        if moment <= local_now:
            tomorrow = local_now + timedelta(days=1)
            moment = _local_dt(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute, zone)
        return moment.astimezone(UTC) if moment else None
    match = _DATE_TIME.match(value)
    if match is None:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    hour, minute = int(match.group(4)), int(match.group(5))
    year_text = match.group(3)
    if year_text:
        year = int(year_text) + (2000 if len(year_text) == 2 else 0)
        moment = _local_dt(year, month, day, hour, minute, zone)
    else:
        moment = _local_dt(local_now.year, month, day, hour, minute, zone)
        if moment is not None and moment <= local_now:
            moment = _local_dt(local_now.year + 1, month, day, hour, minute, zone)
    return moment.astimezone(UTC) if moment else None


def parse_range(text: str, zone: tzinfo, now: datetime) -> tuple[datetime, datetime] | None:
    """``"<start> - <end>"``: end is a full moment, a time on the start's day (next day if earlier) or ``+2h``."""
    value = text.strip()
    for separator in _RANGE_SEPARATORS:
        if separator in value:
            left, right = (part.strip() for part in value.split(separator, 1))
            break
    else:
        return None
    start = parse_moment(left, zone, now)
    if start is None:
        return None
    delta = parse_relative(right)
    if delta is not None:
        return start, start + delta
    match = _TIME.match(right)
    if match is not None:
        local_start = start.astimezone(zone)
        end = _local_dt(
            local_start.year, local_start.month, local_start.day, int(match.group(1)), int(match.group(2)), zone
        )
        if end is None:
            return None
        if end <= local_start:
            end += timedelta(days=1)
        return start, end.astimezone(UTC)
    end_moment = parse_moment(right, zone, start)
    if end_moment is None or not timedelta(0) < end_moment - start <= MAX_RANGE:
        return None
    return start, end_moment
