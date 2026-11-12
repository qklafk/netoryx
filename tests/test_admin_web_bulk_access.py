from __future__ import annotations

from app.db.models import AdminRole
from tests.test_mod_webapi_support import Api, add_user, api, as_role, ctx

__all__ = ["api", "ctx"]


async def test_bulk_ban_needs_the_bulk_operations_permission(api: Api) -> None:
    await add_user(api.ctx, 61, sub_days=3)
    support = await as_role(api, AdminRole.SUPPORT)
    denied = await support.post("/users/bulk-ban", json={"user_ids": [61]})
    assert denied.status_code == 403
    assert (await api.get("/users/61")).json()["user"]["is_banned"] is False
    # a single ban stays available to support staff
    assert (await support.put("/users/61/ban", json={"value": True})).status_code == 200
    admin = await as_role(api, AdminRole.ADMIN, tg_id=2001)
    assert (await admin.post("/users/bulk-ban", json={"user_ids": [61], "value": False})).status_code == 200
