"""Feature modules: self-contained features (bot routers, web routers, jobs, settings, admin menu entries).

Each module lives in ``app/modules/<name>/`` and exposes ``module = FeatureModule(...)`` from its ``__init__``.
Routers of every module are always included; a :class:`ModuleEnabled` filter (bot) or :func:`require_module`
dependency (web) makes switching a module on or off take effect immediately, without a restart.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from aiogram import Router
from aiogram.filters import Filter
from fastapi import APIRouter, HTTPException, Request

from app.config import Settings
from app.core.permissions import Permission, has_permission
from app.core.settings_registry import SettingSpec
from app.db.models import AdminRole
from app.services.users import ru_or_en

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

# Every known module, in load order. Missing packages are skipped, so the application boots with any subset.
MODULE_NAMES: tuple[str, ...] = (
    # core: public URLs, housekeeping, start / main menu, subscription
    "system",
    "public_web",
    "housekeeping",
    "blacklist",
    "core_user",
    "subscription",
    # money
    "wallet",
    "payments",
    "referral",
    "promo",
    "gifts",
    # personal mode, Telegram proxy, support
    "invites",
    "members",
    "cost_sharing",
    "tg_proxy",
    "tg_only",
    "tickets",
    "broadcasts",
    "servers_monitor",
    "ad_blocking",
    "webhooks",
    "npd_receipts",
    # Mini App and web admin APIs
    "miniapp_api",
    "web_cabinet",
    "admin_api",
    # bot admin
    "admin",
    "admin_dashboard",
    "admin_users",
    "admin_subscriptions",
    "admin_plans",
    "admin_payments",
    "admin_promo",
    "admin_withdrawals",
    "admin_campaigns",
    "admin_servers",
    "admin_maintenance",
    "admin_settings",
    "admin_texts",
    "admin_backups",
    "admin_mode",
    "admin_admins",
)

# Selling features: switched off in personal mode (a private VPN for family and friends).
SHOP_ONLY_MODULES: frozenset[str] = frozenset(
    {
        "plans",
        "payments",
        "wallet",
        "referral",
        "promo",
        "gifts",
        "campaigns",
        "admin_plans",
        "admin_payments",
        "admin_promo",
        "admin_withdrawals",
        "admin_campaigns",
    }
)

JobTrigger = Literal["interval", "cron"]
JobFunc = Callable[["AppContext"], Awaitable[None]]
Hook = Callable[["AppContext"], Awaitable[None]]


@dataclass(slots=True)
class JobSpec:
    """A background job. ``kwargs`` are APScheduler trigger arguments (``seconds=30``, ``hour=4`` ...).

    ``schedule`` computes them from the settings instead (re-applied when settings change); ``enabled`` turns
    the job off for some settings. ``lock_ttl_sec`` bounds how long one run may hold its cross-process lock.
    """

    id: str
    func: JobFunc
    trigger: JobTrigger = "interval"
    kwargs: dict[str, Any] = field(default_factory=dict)
    schedule: Callable[[Settings], dict[str, Any]] | None = None
    enabled: Callable[[Settings], bool] | None = None
    lock_ttl_sec: int = 600
    run_on_start: bool = False

    def trigger_kwargs(self, settings: Settings) -> dict[str, Any]:
        return dict(self.schedule(settings)) if self.schedule else dict(self.kwargs)

    def is_enabled(self, settings: Settings) -> bool:
        return self.enabled(settings) if self.enabled else True


@dataclass(frozen=True, slots=True)
class AdminMenuItem:
    """An entry of the bot admin menu and/or the web admin sidebar."""

    key: str
    title_key: str  # i18n key
    icon: str
    permission: Permission
    web_path: str | None = None
    bot_callback: str | None = None
    order: int = 100


@dataclass(frozen=True, slots=True)
class BotCommandSpec:
    """A command shown in Telegram's command menu."""

    command: str
    description_ru: str
    description_en: str
    admin: bool = False  # only in admins' command menus

    def description(self, lang: str) -> str:
        return self.description_ru if ru_or_en(lang) == "ru" else self.description_en


def _always(_: Settings) -> bool:
    return True


@dataclass(slots=True)
class FeatureModule:
    name: str
    title_ru: str
    title_en: str
    enabled: Callable[[Settings], bool] = _always
    bot_routers: list[Router] = field(default_factory=list)
    admin_bot_routers: list[Router] = field(default_factory=list)
    web_routers: list[APIRouter] = field(default_factory=list)
    admin_api_routers: list[APIRouter] = field(default_factory=list)
    miniapp_routers: list[APIRouter] = field(default_factory=list)
    jobs: list[JobSpec] = field(default_factory=list)
    settings: list[SettingSpec] = field(default_factory=list)
    admin_menu: list[AdminMenuItem] = field(default_factory=list)
    commands: list[BotCommandSpec] = field(default_factory=list)
    on_startup: list[Hook] = field(default_factory=list)
    on_shutdown: list[Hook] = field(default_factory=list)
    order: int = 100
    required: bool = False  # core modules cannot be switched off
    shop_only: bool = False  # off in personal mode (in addition to SHOP_ONLY_MODULES)

    def title(self, lang: str) -> str:
        return self.title_ru if ru_or_en(lang) == "ru" else self.title_en


def module_enabled(module: FeatureModule, settings: Settings) -> bool:
    """Whether a module is on for these settings (mode preset, owner's switch, module's own condition)."""
    if module.required:
        return True
    if settings.app_mode == "personal" and (module.shop_only or module.name in SHOP_ONLY_MODULES):
        return False
    if module.name in settings.disabled_modules:
        return False
    try:
        return bool(module.enabled(settings))
    except Exception:
        log.exception("module %s: enabled() failed; treating as disabled", module.name)
        return False


class ModuleRegistry:
    """The loaded modules with enablement checks."""

    def __init__(self, modules: Iterable[FeatureModule]) -> None:
        ordered = sorted(modules, key=lambda m: (m.order, m.name))
        self._modules: dict[str, FeatureModule] = {}
        for module in ordered:
            if module.name in self._modules:
                raise ValueError(f"duplicate module name {module.name!r}")
            self._modules[module.name] = module

    def __iter__(self) -> Any:
        return iter(self._modules.values())

    def __len__(self) -> int:
        return len(self._modules)

    def __contains__(self, name: object) -> bool:
        return name in self._modules

    def all(self) -> list[FeatureModule]:
        return list(self._modules.values())

    def get(self, name: str) -> FeatureModule | None:
        return self._modules.get(name)

    def is_enabled(self, name: str, settings: Settings) -> bool:
        """False for unknown modules."""
        module = self._modules.get(name)
        return module is not None and module_enabled(module, settings)

    def enabled(self, settings: Settings) -> list[FeatureModule]:
        return [m for m in self._modules.values() if module_enabled(m, settings)]

    def admin_menu(self, settings: Settings, role: AdminRole | None) -> list[tuple[FeatureModule, AdminMenuItem]]:
        """Menu entries of enabled modules that ``role`` may open, in display order."""
        items = [
            (module, item)
            for module in self.enabled(settings)
            for item in module.admin_menu
            if has_permission(role, item.permission)
        ]
        return sorted(items, key=lambda pair: (pair[1].order, pair[0].order, pair[1].key))

    def commands(self, settings: Settings, *, admin: bool) -> list[BotCommandSpec]:
        seen: set[str] = set()
        result: list[BotCommandSpec] = []
        for module in self.enabled(settings):
            for command in module.commands:
                if (command.admin and not admin) or command.command in seen:
                    continue
                seen.add(command.command)
                result.append(command)
        return result


def load_modules(names: Iterable[str] = MODULE_NAMES) -> list[FeatureModule]:
    """Import ``app.modules.<name>`` for each name and collect their ``module`` objects.

    Packages that do not exist are skipped; an existing module that fails to import is a bug and raises.
    """
    modules: list[FeatureModule] = []
    for name in names:
        package = f"app.modules.{name}"
        if importlib.util.find_spec(package) is None:
            log.debug("module %s is not installed", name)
            continue
        imported = importlib.import_module(package)
        module = getattr(imported, "module", None)
        if not isinstance(module, FeatureModule):
            raise TypeError(f"{package}.module must be a FeatureModule")
        if module.name != name:
            raise ValueError(f"{package}.module.name is {module.name!r}, expected {name!r}")
        modules.append(module)
    log.info("loaded modules: %s", ", ".join(m.name for m in modules) or "none")
    return modules


class ModuleEnabled(Filter):
    """aiogram filter: the module is enabled and not switched off for this user by a partial maintenance."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def __call__(
        self,
        event: Any,
        ctx: AppContext,
        admin_role: AdminRole | None = None,
        event_from_user: Any = None,
    ) -> bool:
        if not ctx.modules.is_enabled(self.name, ctx.settings):
            return False
        user_id = getattr(event_from_user, "id", None)
        return not await ctx.maintenance.module_blocked(self.name, user_id, is_admin=admin_role is not None)

    def __str__(self) -> str:
        return f"ModuleEnabled({self.name!r})"


def guard_router(router: Router, name: str) -> Router:
    """Attach :class:`ModuleEnabled` to every event observer of ``router`` (and so of its sub-routers)."""
    module_filter = ModuleEnabled(name)
    for event_name, observer in router.observers.items():
        if event_name in ("error", "update"):
            continue
        observer.filter(module_filter)
    return router


def require_module(name: str) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency: answer 404 while the module is disabled (``request.app.state.ctx`` is the context)."""

    async def dependency(request: Request) -> None:
        ctx = getattr(request.app.state, "ctx", None)
        if ctx is None or not ctx.modules.is_enabled(name, ctx.settings):
            raise HTTPException(status_code=404, detail="Not Found")

    dependency.__name__ = f"require_module_{name}"
    return dependency
