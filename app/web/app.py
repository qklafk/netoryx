"""FastAPI application factory: core routes, feature module routers, SPAs and the security middleware stack."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI
from starlette.routing import Mount

from app.config import Settings
from app.core.modules import require_module
from app.web import auth, public_api
from app.web.errors import install_error_handlers
from app.web.ratelimit import build_rate_limiter
from app.web.routes import dev, health, spa, telegram
from app.web.security import (
    DEFAULT_BODY_LIMIT,
    DEFAULT_RATE_RULES,
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RateRule,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)

if TYPE_CHECKING:
    from app.core.context import AppContext
    from app.core.modules import FeatureModule

log = logging.getLogger(__name__)

ADMIN_API_PREFIX = "/api/admin"
MINIAPP_API_PREFIX = "/api/app"
# Larger request bodies are accepted only on the exact upload paths (both stream the body after authentication).
BODY_LIMIT_OVERRIDES: dict[str, int] = {
    f"{ADMIN_API_PREFIX}/backups/upload": 512 * 1024 * 1024,
    f"{ADMIN_API_PREFIX}/broadcasts/media": 50 * 1024 * 1024,
    f"{MINIAPP_API_PREFIX}/payments/receipt": 10 * 1024 * 1024,
}
SHUTDOWN_GRACE_SEC = 10.0


def create_web_app(
    ctx: AppContext,
    modules: Iterable[FeatureModule] | None = None,
    *,
    static_dir: Path | None = None,
    rate_rules: Sequence[RateRule] = DEFAULT_RATE_RULES,
    body_limit: int = DEFAULT_BODY_LIMIT,
    body_limit_overrides: dict[str, int] | None = None,
) -> FastAPI:
    """Build the web application for ``ctx``.

    Routers of every module (``ctx.modules`` unless ``modules`` is given) are mounted: public ``web_routers`` at
    the root, ``admin_api_routers`` under ``/api/admin/<module>`` (admin session + CSRF required), and
    ``miniapp_routers`` under ``/api/app/<module>`` (Mini App initData required). Disabled modules answer 404.
    Run uvicorn with ``proxy_headers=False`` and ``access_log=False``: forwarded headers are handled here (only
    from ``TRUSTED_PROXIES``) and the access log is written here with secrets masked.
    """
    settings: Settings = ctx.settings
    dev_mode = settings.dev_mode

    def settings_getter() -> Settings:
        return ctx.settings

    app = FastAPI(
        title=f"{settings.brand_name} API",
        docs_url="/api/docs" if dev_mode else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if dev_mode else None,
        lifespan=_lifespan,
    )
    app.state.ctx = ctx
    app.state.rate_limiter = build_rate_limiter(getattr(ctx, "redis", None))
    app.state.update_feeder = telegram.UpdateFeeder()
    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(telegram.router)
    app.include_router(auth.router)
    app.include_router(public_api.router)
    if dev_mode:
        app.include_router(dev.router)
        log.warning("DEV_MODE is on: test payments are accepted at /dev/pay; never use it in production")

    mounted = _mount_modules(app, modules if modules is not None else getattr(ctx, "modules", ()) or ())
    _mount_spas(app, static_dir or spa.DEFAULT_STATIC_DIR, settings_getter)

    # Added inner -> outer: request context (id + access log) wraps everything, so even 413/429 are logged.
    app.add_middleware(
        RateLimitMiddleware,
        limiter_getter=lambda: app.state.rate_limiter,
        settings_getter=settings_getter,
        rules=tuple(rate_rules),
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=body_limit,
        overrides=BODY_LIMIT_OVERRIDES if body_limit_overrides is None else body_limit_overrides,
    )
    app.add_middleware(SecurityHeadersMiddleware, settings_getter=settings_getter)
    app.add_middleware(RequestContextMiddleware, settings_getter=settings_getter)
    log.debug("web app created with modules: %s", ", ".join(mounted) or "none")
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    feeder: telegram.UpdateFeeder = app.state.update_feeder
    await feeder.close(SHUTDOWN_GRACE_SEC)


def _mount_modules(app: FastAPI, modules: Iterable[Any]) -> list[str]:
    mounted: list[str] = []
    for module in modules:
        guard = Depends(require_module(module.name))
        for router in module.web_routers:
            app.include_router(router, dependencies=[guard])
        for router in module.admin_api_routers:
            app.include_router(
                router,
                prefix=f"{ADMIN_API_PREFIX}/{module.name}",
                dependencies=[guard, Depends(auth.admin_user())],
            )
        for router in module.miniapp_routers:
            app.include_router(
                router,
                prefix=f"{MINIAPP_API_PREFIX}/{module.name}",
                dependencies=[guard, Depends(auth.miniapp_init_data)],
            )
        if module.web_routers or module.admin_api_routers or module.miniapp_routers:
            mounted.append(module.name)
    return mounted


def _mount_spas(app: FastAPI, static_dir: Path, settings_getter: Any) -> None:
    admin = spa.SpaMount(
        static_dir / "admin",
        title="Admin panel",
        what="web admin",
        enabled=lambda s: s.web_admin_enabled,
        settings_getter=settings_getter,
    )
    miniapp = spa.SpaMount(
        static_dir / "app",
        title="Mini App",
        what="Mini App",
        enabled=lambda s: s.miniapp_enabled,
        settings_getter=settings_getter,
    )
    cabinet = spa.CabinetMount(static_dir / "app", enabled=lambda s: s.cabinet_enabled, settings_getter=settings_getter)
    app.router.routes.append(Mount("/admin", app=admin, name="admin_spa"))
    app.router.routes.append(Mount("/app", app=miniapp, name="miniapp_spa"))
    app.router.routes.append(Mount("/cabinet", app=cabinet, name="cabinet_spa"))
