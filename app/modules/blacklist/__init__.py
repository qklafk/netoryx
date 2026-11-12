"""External blacklist: ban or flag users whose Telegram IDs appear on subscribed lists."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.modules import FeatureModule
from app.modules.blacklist.admin_api import router as api_router
from app.modules.blacklist.jobs import JOBS
from app.services import blacklist

if TYPE_CHECKING:
    from app.core.context import AppContext


async def _load(ctx: AppContext) -> None:
    """Load the stored list in the background; startup never waits for it."""
    blacklist.CACHE.schedule_reload(ctx.session_factory)


module = FeatureModule(
    name="blacklist",
    title_ru="Чёрный список",
    title_en="Blacklist",
    required=True,  # switched on and off by the BLACKLIST_ENABLED setting
    admin_api_routers=[api_router],
    jobs=list(JOBS),
    on_startup=[_load],
    order=3,
)
