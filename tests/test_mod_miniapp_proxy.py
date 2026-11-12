from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.db.models import ProvisionState
from app.db.session import SessionFactory
from app.services import tg_proxy
from app.services.panels.mtproto import ProxyLink
from app.services.tg_proxy import AccessReason, Policy, ProxyEntry, UserProxyView
from tests.test_mod_miniapp_support import USER_ID, Env, add_user, miniapp_env

LINK = ProxyLink(server="proxy.example.com", port=443, secret="ee" + "ab" * 16 + "676f6f676c652e636f6d")
ALLOWED = Policy(ProvisionState.ENABLED, AccessReason.FREE, limited=True)


def entry(*, ready: bool) -> ProxyEntry:
    return ProxyEntry(
        access_id=1,
        panel_id=3,
        inbound_id=None,
        title="🇩🇪 Германия",
        state=ProvisionState.ENABLED if ready else ProvisionState.ABSENT,
        links=(LINK,) if ready else (),
        web_url="tg://webproxy?server=web.example.com&secret=dd00" if ready else None,
    )


@pytest.fixture
async def env(tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with miniapp_env(tmp_path, session_factory, monkeypatch) as environment:
        await add_user(environment.ctx.session_factory, environment.settings)
        yield environment


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    log: dict[str, list[Any]] = {"view": [], "open": [], "rotate": []}
    state = {"ready": False}

    async def user_view(_f: Any, _s: Any, _c: Any, user_id: int, **_: Any) -> UserProxyView:
        log["view"].append(user_id)
        return UserProxyView(ALLOWED, (entry(ready=state["ready"]),))

    async def open_proxy(_f: Any, _r: Any, _s: Any, _c: Any, user_id: int, *, max_targets: int, **_: Any) -> Any:
        log["open"].append((user_id, max_targets))
        state["ready"] = True
        return UserProxyView(ALLOWED, (entry(ready=True),))

    async def rotate(_f: Any, _c: Any, user_id: int, *, min_interval: timedelta, **_: Any) -> int:
        log["rotate"].append(min_interval)
        if len(log["rotate"]) > 1:
            raise tg_proxy.RotationTooSoonError(datetime.now(UTC) + timedelta(minutes=30))
        return 1

    monkeypatch.setattr(tg_proxy, "user_view", user_view)
    monkeypatch.setattr(tg_proxy, "open_proxy", open_proxy)
    monkeypatch.setattr(tg_proxy, "rotate", rotate)
    return log


async def test_first_open_pushes_accounts(env: Env, calls: dict[str, list[Any]]) -> None:
    body = (await env.get("/tg-proxy")).json()
    assert calls["open"] == [(USER_ID, tg_proxy.DEFAULT_MAX_TARGETS)]
    assert body["allowed"] is True and body["reason"] == "free" and body["limited"] is True
    server = body["servers"][0]
    assert server["title"] == "🇩🇪 Германия" and server["ready"] is True
    assert server["links"][0]["tme_url"].startswith("https://t.me/proxy?server=proxy.example.com")
    assert server["links"][0]["tg_url"].startswith("tg://proxy?")
    assert server["web_url"].startswith("tg://webproxy") and server["qr"].startswith("data:image/png")

    # Ready links are served from the database without touching the servers.
    again = (await env.get("/tg-proxy")).json()
    assert again["servers"][0]["ready"] and len(calls["open"]) == 1
    await env.get("/tg-proxy", params={"refresh": True})
    assert len(calls["open"]) == 2


async def test_rotate_with_cooldown(env: Env, calls: dict[str, list[Any]]) -> None:
    resp = await env.post("/tg-proxy/rotate")
    assert resp.status_code == 200 and resp.json()["servers"][0]["ready"]
    assert calls["rotate"] == [timedelta(hours=1)] and len(calls["open"]) == 1
    too_soon = await env.post("/tg-proxy/rotate")
    assert too_soon.status_code == 429
    body = too_soon.json()
    assert body["error"] == "proxy_rotate_cooldown" and "retry_at" in body
    assert body["message"].startswith("Ссылки недавно уже обновлялись")


async def test_disallowed_users_get_no_links(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    async def user_view(*_: Any, **__: Any) -> UserProxyView:
        return UserProxyView(Policy(ProvisionState.DISABLED, AccessReason.NO_SUBSCRIPTION), ())

    async def must_not_open(*_: Any, **__: Any) -> None:
        raise AssertionError("must not push accounts for users without access")

    monkeypatch.setattr(tg_proxy, "user_view", user_view)
    monkeypatch.setattr(tg_proxy, "open_proxy", must_not_open)
    body = (await env.get("/tg-proxy")).json()
    assert body == {"allowed": False, "reason": "no_subscription", "limited": False, "servers": [], "socks": []}


async def test_proxy_section_follows_module_switch(env: Env, calls: dict[str, list[Any]]) -> None:
    env.ctx.box.update(disabled_modules=["tg_proxy"])
    resp = await env.get("/tg-proxy")
    assert resp.status_code == 404 and calls["view"] == []


async def test_proxy_access_off_keeps_it_for_admins(env: Env, calls: dict[str, list[Any]]) -> None:
    env.ctx.box.update(tg_proxy_access="off")
    assert (await env.get("/tg-proxy")).status_code == 404
    assert (await env.get("/me")).json()["features"]["tg_proxy"] is False
    await add_user(env.ctx.session_factory, env.settings, 1000)
    assert (await env.get("/tg-proxy", user_id=1000)).status_code == 200


async def test_personal_mode_strangers_get_no_proxy(env: Env, calls: dict[str, list[Any]]) -> None:
    env.ctx.box.update(app_mode="personal")
    resp = await env.get("/tg-proxy")
    assert resp.status_code == 403 and resp.json()["error"] == "invite_only"
    assert calls["open"] == [] and calls["view"] == []
