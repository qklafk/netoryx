"""Routers of the web admin API, one per admin section."""

from __future__ import annotations

from fastapi import APIRouter

from app.modules.admin_api.routers import (
    admins,
    backups,
    broadcasts,
    dashboard,
    imports,
    locations,
    menu_buttons,
    mode,
    panels,
    payments,
    plans,
    promo,
    referral,
    settings,
    subscriptions,
    system,
    texts,
    tickets,
    users,
    webhooks,
)

SECTION_ROUTERS: tuple[APIRouter, ...] = (
    dashboard.router,
    users.router,
    subscriptions.router,
    plans.router,
    panels.router,
    imports.router,
    locations.router,
    payments.router,
    promo.router,
    referral.router,
    broadcasts.router,
    tickets.router,
    texts.router,
    menu_buttons.router,
    settings.router,
    admins.router,
    backups.router,
    system.router,
    mode.router,
    webhooks.router,
)
