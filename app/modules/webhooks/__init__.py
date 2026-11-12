"""Outgoing webhooks: signed event notifications for CRMs and other integrations (managed in the web admin)."""

from __future__ import annotations

from app.core.modules import FeatureModule
from app.modules.webhooks.jobs import JOBS

module = FeatureModule(
    name="webhooks",
    title_ru="Исходящие вебхуки",
    title_en="Outgoing webhooks",
    jobs=list(JOBS),
    order=60,
)
