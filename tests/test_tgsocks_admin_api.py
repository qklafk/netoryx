"""Admin API and panel/user cleanup for the SOCKS5 proxy."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping

from sqlalchemy import select

from app.db.models import Inbound, ProvisionState, TgSocksAccess
from app.services.panels.socks import SocksAccount
from tests.fakes import FakeCapablePanel
from tests.test_mod_webapi_servers import _create_xui, fake_backends
from tests.test_mod_webapi_support import Api, add_user, api, ctx

__all__ = ["api", "ctx", "fake_backends"]


class SocksCapablePanel(FakeCapablePanel):
    """A panel that also hosts SOCKS5 accounts."""

    def __init__(self) -> None:
        super().__init__()
        self.socks: dict[int, dict[str, str]] = {5: {"tg77": "pw", "admin-made": "keep"}}
        self.socks_calls: list[tuple[int | None, list[str]]] = []

    async def list_socks_users(self, target: int | None) -> list[SocksAccount]:
        return [SocksAccount(u, p) for u, p in self.socks.get(target or 0, {}).items()]

    async def apply_socks_users(
        self, target: int | None, put: Mapping[str, str], remove: Collection[str] = ()
    ) -> list[SocksAccount]:
        self.socks_calls.append((target, list(remove)))
        accounts = self.socks.setdefault(target or 0, {})
        for name in remove:
            accounts.pop(name, None)
        accounts.update(put)
        return await self.list_socks_users(target)

    async def delete_socks_user(self, target: int | None, name: str) -> None:
        await self.apply_socks_users(target, {}, [name])


async def _mixed_inbound(api: Api, panel_id: int) -> int:
    params = {"protocol": "mixed", "port": 1080, "local": True, "panel_enabled": True, "socks_auth": "noauth"}
    async with api.ctx.session_factory.begin() as session:
        inbound = Inbound(panel_id=panel_id, remote_id=5, name="socks", params=json.dumps(params), is_active=False)
        session.add(inbound)
        await session.flush()
        return inbound.id


async def test_select_a_mixed_inbound_for_the_telegram_socks_proxy(api: Api) -> None:
    panel = await _create_xui(api)
    inbound_id = await _mixed_inbound(api, panel["id"])
    listed = {i["id"]: i for i in (await api.get("/inbounds", params={"panel_id": panel["id"]})).json()}
    mixed = listed[inbound_id]
    assert mixed["protocol"] == "mixed" and mixed["tg_socks"] is False and mixed["tg_proxy"] is None
    assert mixed["socks_auth"] == "noauth"
    (vless,) = [i for i in listed.values() if i["id"] != inbound_id]
    assert vless["tg_socks"] is None and vless["socks_auth"] is None

    on = await api.patch(f"/inbounds/{inbound_id}", json={"tg_socks": True})
    assert on.status_code == 200 and on.json()["tg_socks"] is True
    off = await api.patch(f"/inbounds/{inbound_id}", json={"tg_socks": False})
    assert off.json()["tg_socks"] is False

    wrong = await api.patch(f"/inbounds/{vless['id']}", json={"tg_socks": True})
    assert wrong.status_code == 400 and wrong.json()["error"] == "not_socks"
    # A proxy inbound can never be switched into the VPN rotation.
    assert (await api.patch(f"/inbounds/{inbound_id}", json={"is_active": True})).status_code == 400


async def test_deleting_a_panel_removes_its_socks_accounts(api: Api) -> None:
    ctx = api.ctx
    panel = await _create_xui(api)
    inbound_id = await _mixed_inbound(api, panel["id"])
    await add_user(ctx, 77, sub_days=5)
    backend = SocksCapablePanel()
    ctx.panels.backends[panel["id"]] = backend  # type: ignore[assignment]
    async with ctx.session_factory.begin() as session:
        session.add(
            TgSocksAccess(
                user_id=77,
                panel_id=panel["id"],
                inbound_id=inbound_id,
                username="tg77",
                password_enc="x",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
            )
        )
    result = (await api.delete(f"/panels/{panel['id']}", params={"remove_accounts": True})).json()
    assert result["deleted"] is True and result["accounts_removed"] == 1 and result["errors"] == []
    assert backend.socks[5] == {"admin-made": "keep"}  # only the account the bot created is removed
    async with ctx.session_factory() as session:
        assert (await session.scalars(select(TgSocksAccess))).first() is None
