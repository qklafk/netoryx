"""Monitoring job: check every active server and alert admins about high load, a stopped core or offline nodes."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.core.modules import JobSpec
from app.modules.servers_monitor import service
from app.services.notifications import AdminEvent, notify

if TYPE_CHECKING:
    from app.config import Settings
    from app.core.context import AppContext
    from app.db.models import Panel

log = logging.getLogger(__name__)


def alert_text(ctx: AppContext, panel: Panel, transition: service.Transition) -> str:
    i18n = ctx.i18n
    lang = ctx.settings.default_language
    key = transition.key.split(":", 1)[0]
    values = {k: i18n.esc(v) if isinstance(v, str) else v for k, v in transition.values.items()}
    error = values.pop("error", "")
    suffix = f": {error}" if error else ""
    state = "alert" if transition.raised else "ok"
    return i18n.t(f"servers_monitor.{state}.{key}", lang, server=i18n.esc(panel.name), error=suffix, **values)


async def _check(ctx: AppContext, panel: Panel, limits: service.Thresholds) -> None:
    try:
        status = await service.fetch_status(ctx.panels, panel.id)
    except service.NotSupported:
        return
    except Exception as exc:  # reachability is the health job's business
        log.debug("monitor: panel %s status unavailable: %s", panel.id, exc)
        return
    state = await service.load_state(ctx.session_factory, panel.id)
    transitions = service.advance(state, service.problems(status, limits))
    await service.save_state(ctx.session_factory, panel.id, state)
    for transition in transitions:
        notify(ctx, AdminEvent.ALERTS, alert_text(ctx, panel, transition))


async def monitor_job(ctx: AppContext) -> None:
    settings = ctx.settings
    threshold = max(1, settings.health_failures_before_alert)
    active = await service.monitored_panels(ctx.session_factory, active_only=True)
    reachable = [p for p in active if p.consecutive_failures < threshold]  # the health job reports the others
    limits = service.thresholds_from(settings)
    await asyncio.gather(*(_check(ctx, panel, limits) for panel in reachable))
    await service.forget_missing(ctx.session_factory, [p.id for p in active])


def _schedule(settings: Settings) -> dict[str, Any]:
    return {"minutes": service.interval_from(settings)}


JOBS = [
    JobSpec(
        id="servers_monitor.check",
        func=monitor_job,
        trigger="interval",
        schedule=_schedule,
        enabled=service.monitoring_enabled,
        lock_ttl_sec=600,
    )
]
