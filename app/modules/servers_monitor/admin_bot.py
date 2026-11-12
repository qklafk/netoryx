"""Admin screens: servers list with health, live server status, restart of the proxy core."""

from __future__ import annotations

import logging

from aiogram import F
from aiogram.types import CallbackQuery

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, render
from app.keyboards import Nav
from app.modules.admin.common import admin_router, first_time, forget, new_nonce
from app.modules.servers_monitor import service
from app.modules.servers_monitor.views import (
    NAV,
    MonCb,
    error_screen,
    list_screen,
    restart_confirm_screen,
    status_screen,
)
from app.services import audit
from app.services.panels.base import PanelError

log = logging.getLogger(__name__)

router = admin_router("servers_monitor_admin")
for _observer in (router.message, router.callback_query):
    _observer.filter(HasPermission(Permission.PANELS))


async def _list(ctx: AppContext, _: Translator) -> Screen:
    panels = await service.monitored_panels(ctx.session_factory)
    return list_screen(_, panels, ctx.settings.health_failures_before_alert)


async def status_of(ctx: AppContext, _: Translator, panel_id: int) -> Screen:
    """The live status screen of a panel (or a friendly error screen)."""
    panel = await service.get_panel(ctx.session_factory, panel_id)
    if panel is None:
        return await _list(ctx, _)
    try:
        status = await service.fetch_status(ctx.panels, panel_id)
    except service.NotSupported:
        return error_screen(_, panel, "servers_monitor.not_supported")
    except TimeoutError:
        return error_screen(_, panel, "servers_monitor.timeout")
    except (PanelError, LookupError) as exc:
        return error_screen(_, panel, "servers_monitor.unreachable", str(exc) or type(exc).__name__)
    can_restart = await service.can_restart(ctx.panels, panel_id)
    return status_screen(_, panel, status, can_restart=can_restart)


@router.callback_query(Nav.filter(F.to == NAV))
async def open_list(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await _list(ctx, _))


@router.callback_query(MonCb.filter(F.action == "show"))
async def show(callback: CallbackQuery, callback_data: MonCb, ctx: AppContext, _: Translator) -> None:
    await render(callback, await status_of(ctx, _, callback_data.id))


@router.callback_query(MonCb.filter(F.action == "restart"))
async def restart_ask(callback: CallbackQuery, callback_data: MonCb, ctx: AppContext, _: Translator) -> None:
    panel = await service.get_panel(ctx.session_factory, callback_data.id)
    if panel is None:
        await render(callback, await _list(ctx, _))
        return
    await render(callback, restart_confirm_screen(_, panel, new_nonce()))


@router.callback_query(MonCb.filter(F.action == "restart_ok"))
async def restart(callback: CallbackQuery, callback_data: MonCb, ctx: AppContext, _: Translator) -> None:
    panel_id = callback_data.id
    # Each confirmation screen carries its own nonce: a double tap restarts once, a new confirmation restarts again.
    key = f"mon:restart:{panel_id}:{callback_data.n}"
    if not callback_data.n or not first_time(key):
        await render(callback, await status_of(ctx, _, panel_id))
        return
    try:
        await service.restart_core(ctx.panels, panel_id)
    except (service.NotSupported, PanelError, LookupError, TimeoutError) as exc:
        forget(key)  # the same confirmation may be tapped again
        log.warning("restart of panel %s failed: %s", panel_id, exc)
        await render(
            callback, await status_of(ctx, _, panel_id), answer=_("servers_monitor.restart_failed"), alert=True
        )
        return
    async with ctx.session_factory.begin() as session:
        await audit.log(session, callback.from_user.id, "panel.restart_core", "panel", panel_id)
    await render(callback, await status_of(ctx, _, panel_id), answer=_("servers_monitor.restarted"))
