"""The main menu: one inline screen, mode-aware (shop / personal) and showing only enabled features.

Buttons open other modules' screens through ``Nav(to=<target>)``; each module handles its own target:

========== ======================================= ==================
target     screen                                  module
========== ======================================= ==================
menu       main menu                               core_user
profile    profile                                 core_user
help       help / instructions                     core_user
lang       language picker (``arg`` = new code)    core_user
sub        "My subscription" card                  subscription
buy        plans list                              subscription
connect    connection guide                        subscription
trial      start the free trial                    subscription
wallet     balance, top-up, history                wallet
referral   invite friends                          referral
promo      gift / promo code entry                 promo (gifts)
tickets    support                                 tickets
tg_proxy   Telegram proxy                          tg_proxy
admin      admin menu                              admin modules
mbtn       text screen of a custom menu button     core_user
========== ======================================= ==================
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton

from app.config import Settings
from app.core.i18n import Translator
from app.core.ui import Screen
from app.keyboards import Button, Nav, grid, kb, nav
from app.keyboards.builders import MAX_ROW_WIDTH
from app.modules.subscription.callbacks import BUY, CONNECT, SUB, TRIAL
from app.modules.subscription.state import Status, SubState
from app.modules.subscription.views import Rows, fmt_expiry
from app.services import menu_buttons
from app.services.menu_buttons import MenuItem

if TYPE_CHECKING:
    from app.core.modules import ModuleRegistry

log = logging.getLogger(__name__)

PROFILE = "profile"
HELP = "help"
LANG = "lang"
WALLET = "wallet"
REFERRAL = "referral"
PROMO = "promo"
TICKETS = "tickets"
TG_PROXY = "tg_proxy"
ADMIN = "admin"
CUSTOM_SCREEN = "mbtn"  # text screen of a custom menu button (``arg`` = button id)


def feature_on(modules: ModuleRegistry, settings: Settings, name: str) -> bool:
    """A feature module is on. Modules that are not installed count as on in shop mode unless switched off."""
    if name in modules:
        return modules.is_enabled(name, settings)
    return settings.app_mode == "shop" and name not in settings.disabled_modules


def support_button(_: Translator, modules: ModuleRegistry, settings: Settings) -> InlineKeyboardButton | None:
    """Support: the tickets screen if that module is on, else ``SUPPORT_URL``."""
    if name_on(modules, settings, TICKETS):
        return nav(_("menu.support"), TICKETS)
    if settings.support_url:
        return Button.url(_("menu.support"), settings.support_url)
    return None


def name_on(modules: ModuleRegistry, settings: Settings, name: str) -> bool:
    """Like :func:`feature_on`, but only for installed modules (screens that must exist to be linked)."""
    return name in modules and modules.is_enabled(name, settings)


def mini_app_url(settings: Settings) -> str | None:
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/app/" if settings.miniapp_enabled and base.startswith("https://") else None


def status_line(_: Translator, settings: Settings, state: SubState) -> str | None:
    """One line about the subscription under the menu title."""
    status = state.status
    if status == Status.NONE:
        if state.trial_available(settings):
            return _("core_user.menu.status_trial", trial=_.duration(settings.trial_days))
        return _("core_user.menu.status_none") if settings.app_mode == "shop" else None
    if status == Status.ACTIVE:
        key = "core_user.menu.status_forever" if state.unlimited else "core_user.menu.status_active"
        return _(key, expires=fmt_expiry(_, state))
    if status == Status.EXPIRED:
        return _("core_user.menu.status_expired")
    return _("core_user.menu.status_disabled")


def main_menu(
    _: Translator,
    settings: Settings,
    state: SubState,
    modules: ModuleRegistry,
    *,
    is_admin: bool = False,
    header: str | None = None,
    custom: Sequence[MenuItem] = (),
) -> Screen:
    """The main menu: built-in entries (minus ``MENU_HIDDEN_BUTTONS``), then the owner's custom buttons."""
    shop = settings.app_mode == "shop"
    hidden = set(settings.menu_hidden_buttons)
    lines: list[str] = [header, ""] if header else []
    lines.append(_("menu.title", brand=_.esc(settings.brand_name)))
    status = status_line(_, settings, state)
    # the welcome header already offers the trial; do not repeat it in the status line
    if status and not (header and state.status == Status.NONE and state.trial_available(settings)):
        lines += ["", status]

    def show(key: str, button: InlineKeyboardButton | None) -> InlineKeyboardButton | None:
        return None if key in hidden else button

    rows: Rows = []
    if state.trial_available(settings):
        rows.append([show("trial", Button.cb(_("menu.trial"), Nav(to=TRIAL), style="success"))])
    rows.append(
        [show("subscription", nav(_("menu.my_subscription"), SUB)), show("connect", nav(_("menu.connect"), CONNECT))]
    )
    if shop:
        rows.append([show("buy", nav(_("menu.buy"), BUY))])
    extra: list[InlineKeyboardButton | None] = [
        show("balance", nav(_("menu.balance"), WALLET) if shop and name_on(modules, settings, WALLET) else None),
        show("referral", nav(_("menu.referral"), REFERRAL) if shop and name_on(modules, settings, REFERRAL) else None),
        show("gift", nav(_("menu.gift"), PROMO) if shop and _gifts_or_promo(modules, settings) else None),
        show("tg_proxy", nav(_("menu.tg_proxy"), TG_PROXY) if _tg_proxy_on(modules, settings, is_admin) else None),
        show("support", support_button(_, modules, settings)),
        show("instructions", nav(_("menu.instructions"), HELP)),
        show("language", nav(_("menu.language"), LANG)),
    ]
    url = mini_app_url(settings)
    if url:
        extra.append(show("mini_app", Button.webapp(_("menu.mini_app"), url)))
    rows += grid(extra, 2)
    rows += custom_rows(_, custom, subscribed=state.status == Status.ACTIVE, is_admin=is_admin)
    if is_admin:
        rows.append([nav(_("menu.admin"), ADMIN)])
    return Screen("\n".join(lines), kb(rows))


def custom_button(_: Translator, item: MenuItem) -> InlineKeyboardButton | None:
    """The Telegram button of a custom entry, or None if its stored data is no longer acceptable."""
    title = item.title_for(_.lang)
    try:
        if item.kind == menu_buttons.KIND_URL:
            return Button.url(title, item.payload)
        if item.kind == menu_buttons.KIND_WEBAPP:
            return Button.webapp(title, item.payload)
        if item.kind == menu_buttons.KIND_COPY:
            return Button.copy(title, item.payload)
        if item.kind == menu_buttons.KIND_SCREEN:
            return nav(title, CUSTOM_SCREEN, item.id)
    except ValueError:
        log.warning("menu button %s skipped: invalid %s payload", item.id, item.kind)
    return None


def custom_rows(
    _: Translator, items: Sequence[MenuItem], *, subscribed: bool, is_admin: bool
) -> list[list[InlineKeyboardButton]]:
    """Custom buttons visible to this user; buttons with the same ``row`` share a keyboard row."""
    grouped: dict[int, list[InlineKeyboardButton]] = {}
    for item in sorted(items, key=lambda i: (i.row, i.sort_order, i.id)):
        if not item.visible_to(subscribed=subscribed, is_admin=is_admin):
            continue
        button = custom_button(_, item)
        if button is not None:
            grouped.setdefault(item.row, []).append(button)
    rows: list[list[InlineKeyboardButton]] = []
    for row in grouped.values():
        rows += grid(row, MAX_ROW_WIDTH)
    return rows


def _gifts_or_promo(modules: ModuleRegistry, settings: Settings) -> bool:
    return name_on(modules, settings, PROMO) or name_on(modules, settings, "gifts")


def _tg_proxy_on(modules: ModuleRegistry, settings: Settings, is_admin: bool) -> bool:
    if not name_on(modules, settings, TG_PROXY):
        return False
    return settings.tg_proxy_access != "off"
