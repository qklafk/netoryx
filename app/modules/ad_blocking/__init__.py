"""Ad and tracker blocking on 3x-ui servers (one switch in the settings, converged by a background job)."""

from __future__ import annotations

from app.core.modules import FeatureModule
from app.modules.ad_blocking.jobs import JOBS

module = FeatureModule(
    name="ad_blocking",
    title_ru="Блокировка рекламы",
    title_en="Ad blocking",
    jobs=list(JOBS),
    order=75,
)
