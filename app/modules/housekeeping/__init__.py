"""Housekeeping: daily backups sent to the backup chat and cleanup of expired technical data."""

from __future__ import annotations

from app.core.modules import FeatureModule
from app.modules.housekeeping.jobs import JOBS

module = FeatureModule(
    name="housekeeping",
    title_ru="Обслуживание",
    title_en="Housekeeping",
    required=True,
    jobs=list(JOBS),
    order=2,
)
