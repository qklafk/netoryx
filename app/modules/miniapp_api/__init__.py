"""Mini App API (``/api/app/*``, authorized by the Telegram ``initData`` or by a web cabinet session).

Mirrors the bot for the Mini App: profile, subscription card and traffic, connect data (subscription URL, one-tap
app links, encrypted links, QR), devices, plans and payments (provider page or Telegram Stars invoice link), wallet,
referral program and payouts, promo and gift codes, Telegram proxy and support tickets. Sections follow the
enabled feature modules, so personal mode exposes only what members need. ``GET /me`` lists them in ``features``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.modules import FeatureModule
from app.modules.miniapp_api import account, imports, proxy, rewards, shop, support
from app.modules.miniapp_api.deps import maintenance_guard
from app.web.auth import miniapp_init_data

API_PREFIX = "/api/app"

# Mounted as a public web router with its own prefix (short paths: /api/app/me, /api/app/plans ...); the initData
# check runs first, then the maintenance guard.
router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(miniapp_init_data), Depends(maintenance_guard)])
for _part in (account.router, shop.router, rewards.router, proxy.router, support.router, imports.router):
    router.include_router(_part)

module = FeatureModule(
    name="miniapp_api",
    title_ru="Mini App",
    title_en="Mini App",
    enabled=lambda settings: settings.miniapp_enabled or settings.cabinet_enabled,
    web_routers=[router],
    order=900,
)
