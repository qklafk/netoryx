"""Time helpers. Storage is aware UTC; dates shown to users and business days use ``settings.zone``."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

DATE_FORMAT = "%d.%m.%Y"
DATETIME_FORMAT = "%d.%m.%Y %H:%M"
_DAY_SECONDS = 86_400
# Upper bound for any single period (plan duration, granted days, promo bonus days): 100 years. Unbounded values
# overflow ``datetime`` (year 9999) when added to an expiry date.
MAX_PERIOD_DAYS = 36_500


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime; use aware UTC datetimes")


def local(dt: datetime, settings: Settings) -> datetime:
    """Convert an aware datetime to the configured time zone."""
    _require_aware(dt)
    return dt.astimezone(settings.zone)


def fmt_date(dt: datetime, settings: Settings) -> str:
    """``"04.12.2026"`` in the configured time zone."""
    return local(dt, settings).strftime(DATE_FORMAT)


def fmt_dt(dt: datetime, settings: Settings) -> str:
    """``"04.12.2026 19:00"`` in the configured time zone."""
    return local(dt, settings).strftime(DATETIME_FORMAT)


def today_local(settings: Settings, now: datetime | None = None) -> date:
    """The current calendar date in the configured time zone."""
    return local(now or utcnow(), settings).date()


def local_day_start(day: date, settings: Settings) -> datetime:
    """The UTC instant at which local calendar day ``day`` begins."""
    return datetime.combine(day, time.min, tzinfo=settings.zone).astimezone(UTC)


def days_left(expires_at: datetime, now: datetime) -> int:
    """Whole days until ``expires_at``, rounded up; 0 once expired (1 second left -> 1)."""
    _require_aware(expires_at)
    _require_aware(now)
    seconds = (expires_at - now) / timedelta(seconds=1)
    return max(0, math.ceil(seconds / _DAY_SECONDS))
