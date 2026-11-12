"""Payment jobs: poll open payments of pollable providers, credit the paid ones and expire stale invoices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.modules import JobSpec
from app.services.payments import service

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.context import AppContext

MIN_INTERVAL_SEC = 10


async def reconcile(ctx: AppContext) -> None:
    """Webhooks can be lost: catch up with the providers (expiry of stale payments happens in the same run)."""
    await service.reconcile(ctx.session_factory, ctx.settings, ctx.gateways.as_dict(), ctx.on_payment_credited)


def _schedule(settings: Settings) -> dict[str, int]:
    return {"seconds": max(MIN_INTERVAL_SEC, settings.payment_reconcile_interval_sec)}


JOBS = [JobSpec(id="payments.reconcile", func=reconcile, schedule=_schedule, lock_ttl_sec=300)]
