"""Remnawave webhook events -> notices to users and alerts to admins.

The database stays the source of truth: these events only inform people. Expiry notices come from the bot's own
reminders, so ``user.expired`` reaches the user only through them; a panel that expires an account the bot still
considers active is reported to the admins instead.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select

from app.core.ui import Screen, send_screen
from app.db.models import Subscription, User
from app.db.session import read_session
from app.keyboards import kb, nav
from app.services.notifications import AdminEvent, notify
from app.services.panels.remnawave import RemnawaveEvent
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

USER_PREFIX = "tg_"
SUB_NAV = "sub"
BUY_NAV = "buy"
DEVICES_NAV = "devices"
CONNECT_NAV = "connect"
ALERT_THROTTLE_SEC = 300.0

# Events that produce a notice to the user (key in the module locale).
USER_EVENTS = {
    "user.limited": "public_web.rw.limited",
    "user.bandwidth_usage_threshold_reached": "public_web.rw.threshold",
    "user.first_connected": "public_web.rw.first_connected",
    "user_hwid_devices.added": "public_web.rw.device_added",
}
NODE_EVENTS = {
    "node.connection_lost": "public_web.rw.node_lost",
    "node.connection_restored": "public_web.rw.node_restored",
    "node.traffic_notify": "public_web.rw.node_traffic",
}


def user_id_of(event: RemnawaveEvent) -> int | None:
    """Our Telegram user id: the account's ``telegramId`` or the ``tg_<id>`` username the bot gives accounts."""
    if event.telegram_id:
        return event.telegram_id
    name = event.username or ""
    if name.startswith(USER_PREFIX) and name[len(USER_PREFIX) :].isdigit():
        return int(name[len(USER_PREFIX) :])
    return None


def _device_name(data: Any) -> str:
    device = data.get("hwidUserDevice") if hasattr(data, "get") else None
    if not isinstance(device, dict):
        return ""
    parts = [str(device[k]) for k in ("platform", "deviceModel", "osVersion") if device.get(k)]
    if not parts and device.get("userAgent"):
        parts.append(str(device["userAgent"]))
    return " · ".join(parts)[:80]


def _threshold(data: Any) -> str:
    value = data.get("lastTriggeredThreshold") if hasattr(data, "get") else None
    return str(value) if isinstance(value, int | float | str) and str(value) else "?"


def _markup(ctx: AppContext, event: str, lang: str) -> InlineKeyboardMarkup | None:
    t = ctx.i18n.t
    if event == "user.limited" or event == "user.bandwidth_usage_threshold_reached":
        return kb(
            [[nav(t("public_web.rw.btn.subscription", lang), SUB_NAV), nav(t("public_web.rw.btn.buy", lang), BUY_NAV)]]
        )
    if event == "user_hwid_devices.added":
        return kb([[nav(t("public_web.rw.btn.devices", lang), DEVICES_NAV)]])
    if event == "user.first_connected":
        return kb([[nav(t("public_web.rw.btn.connect", lang), CONNECT_NAV)]])
    return None


async def handle_event(ctx: AppContext, event: RemnawaveEvent) -> str:
    """Act on one verified event; returns what was done (for logs and tests)."""
    if event.event in USER_EVENTS:
        return await _user_notice(ctx, event)
    if event.event == "user.expired":
        return await _check_expiry(ctx, event)
    if event.event in NODE_EVENTS:
        return _node_alert(ctx, event)
    log.debug("remnawave webhook %s ignored", event.event)
    return "ignored"


async def _user_notice(ctx: AppContext, event: RemnawaveEvent) -> str:
    user_id = user_id_of(event)
    if user_id is None:
        return "unknown_user"
    async with read_session(ctx.session_factory) as session:
        user = await session.get(User, user_id)
    if user is None or user.bot_blocked or user.is_banned:
        return "unknown_user"
    i18n = ctx.i18n
    lang = i18n.normalize(user.language)
    text = i18n.t(
        USER_EVENTS[event.event],
        lang,
        percent=i18n.esc(_threshold(event.data)),
        device=i18n.esc(_device_name(event.data) or i18n.t("public_web.rw.unknown_device", lang)),
    )
    try:
        await send_screen(ctx.bot, user_id, Screen(text, _markup(ctx, event.event, lang)))
    except Exception as exc:
        log.info("remnawave webhook: could not notify user %s: %s", user_id, exc)
        return "send_failed"
    return "notified"


async def _check_expiry(ctx: AppContext, event: RemnawaveEvent) -> str:
    user_id = user_id_of(event)
    if user_id is None:
        return "unknown_user"
    async with read_session(ctx.session_factory) as session:
        sub = await session.scalar(select(Subscription).where(Subscription.user_id == user_id))
    if sub is None or sub.expires_at <= utcnow():
        return "expected"
    text = ctx.i18n.t("public_web.rw.expiry_drift", ctx.settings.default_language, user_id=user_id)
    notify(ctx, AdminEvent.ALERTS, text, throttle_key=f"rw-expired:{user_id}", throttle_sec=3600.0)
    return "drift"


def _node_alert(ctx: AppContext, event: RemnawaveEvent) -> str:
    data = event.data
    nested = data.get("node")
    node = nested if isinstance(nested, dict) else data
    name = str(node.get("name") or node.get("address") or "?")
    message = str(node.get("lastStatusMessage") or "")[:200]
    i18n = ctx.i18n
    lang = ctx.settings.default_language
    text = i18n.t(
        NODE_EVENTS[event.event], lang, node=i18n.esc(name), error=f": {i18n.esc(message)}" if message else ""
    )
    notify(ctx, AdminEvent.ALERTS, text, throttle_key=f"rw-node:{event.event}:{name}", throttle_sec=ALERT_THROTTLE_SEC)
    return "alerted"
