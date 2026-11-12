"""Custom main menu buttons: validation, rendering per audience and kind, cache, admin API."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import AdminAuditLog, AdminRole, MenuButton
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.core_user.menu import CUSTOM_SCREEN, custom_rows
from app.services import kv, menu_buttons
from app.services.menu_buttons import MenuButtonError, MenuCache, MenuItem
from tests.test_mod_user_support import OWNER, USER, Harness, add_subscription, make_harness, make_i18n
from tests.test_mod_webapi_support import Api, api, as_role, ctx

__all__ = ["api", "ctx"]

Make = Callable[..., Harness]


# --- validation -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("url", "ftp://example.com"),
        ("url", "javascript:alert(1)"),
        ("url", "https://"),
        ("url", "https://exa mple.com"),
        ("webapp", "http://example.com/app"),
        ("webapp", "tg://resolve?domain=x"),
        ("copy_text", "x" * 257),
        ("copy_text", "   "),
        ("text_screen", "<script>x</script>"),
        ("text_screen", "<b>unclosed"),
        ("text_screen", "x" * 4097),
        ("other", "x"),
    ],
)
def test_invalid_payloads(kind: str, payload: str) -> None:
    with pytest.raises(MenuButtonError):
        menu_buttons.clean_payload(kind, payload)


def test_valid_payloads_and_titles() -> None:
    assert menu_buttons.clean_payload("url", " https://example.com/x ") == "https://example.com/x"
    assert menu_buttons.clean_payload("url", "tg://resolve?domain=news") == "tg://resolve?domain=news"
    assert menu_buttons.clean_payload("webapp", "https://example.com/app") == "https://example.com/app"
    assert menu_buttons.clean_payload("text_screen", "<b>Rules</b> &amp; terms") == "<b>Rules</b> &amp; terms"
    assert menu_buttons.clean_title("  Канал  ") == "Канал"
    assert menu_buttons.clean_titles({"en": "Channel", "de": " "}) == {"en": "Channel"}
    for bad in ("", "x" * 65, "two\nlines"):
        with pytest.raises(MenuButtonError):
            menu_buttons.clean_title(bad)
    with pytest.raises(MenuButtonError):
        menu_buttons.clean_titles({"EN!": "x"})


# --- rendering --------------------------------------------------------------------------------------------------------
ITEMS = (
    MenuItem(1, "url", "Канал", "https://t.me/news", row=1, titles={"en": "Channel"}),
    MenuItem(2, "copy_text", "Промокод", "SALE10", row=1, audience="non_subscribers"),
    MenuItem(3, "webapp", "Магазин", "https://shop.example.com", row=2, audience="subscribers"),
    MenuItem(4, "text_screen", "Правила", "<b>Rules</b>", row=3, audience="admins"),
    MenuItem(5, "webapp", "Broken", "http://insecure.example.com", row=4),
)


def test_custom_rows_per_audience_and_kind(tmp_path: Path) -> None:
    i18n = make_i18n(tmp_path)
    ru, en = i18n.translator("ru"), i18n.translator("en")

    guest = custom_rows(ru, ITEMS, subscribed=False, is_admin=False)
    assert [[b.text for b in row] for row in guest] == [["Канал", "Промокод"]]
    assert guest[0][0].url == "https://t.me/news"
    assert guest[0][1].copy_text is not None and guest[0][1].copy_text.text == "SALE10"

    subscriber = custom_rows(en, ITEMS, subscribed=True, is_admin=False)
    assert [[b.text for b in row] for row in subscriber] == [["Channel"], ["Магазин"]]
    assert subscriber[1][0].web_app is not None and subscriber[1][0].web_app.url == "https://shop.example.com"

    admin = custom_rows(ru, ITEMS, subscribed=True, is_admin=True)
    screen_button = admin[-1][0]
    assert screen_button.text == "Правила"
    assert screen_button.callback_data == Nav(to=CUSTOM_SCREEN, arg="4").pack()


@pytest.fixture
async def make(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Make]:
    harnesses: list[Harness] = []

    def factory(**settings: Any) -> Harness:
        harness = make_harness(session_factory, tmp_path / str(len(harnesses)), **settings)
        harnesses.append(harness)
        return harness

    yield factory
    for harness in harnesses:
        await harness.close()


async def _add(sf: SessionFactory, **fields: Any) -> int:
    async with sf.begin() as session:
        button = await menu_buttons.create(session, **fields)
        button_id = button.id
    menu_buttons.invalidate(sf)
    return button_id


async def test_menu_shows_custom_buttons_and_hides_builtins(make: Make) -> None:
    h = make(menu_hidden_buttons=["referral", "language"])
    await _add(h.sf, kind="url", title="Наш канал", payload="https://t.me/news")
    await _add(h.sf, kind="copy_text", title="Только подписчикам", payload="X", audience="subscribers")
    await h.send("/start")
    buttons = h.button_texts()
    assert "Наш канал" in buttons and "Только подписчикам" not in buttons
    assert h.t("menu.language") not in buttons and h.t("menu.my_subscription") in buttons
    await add_subscription(h.sf, days=10)
    await h.press(Nav(to="menu"))
    assert "Только подписчикам" in h.button_texts()


async def test_text_screen_respects_the_audience(make: Make) -> None:
    h = make()
    public = await _add(h.sf, kind="text_screen", title="Правила", payload="<b>Правила сервиса</b>")
    secret = await _add(h.sf, kind="text_screen", title="Админам", payload="secret", audience="admins")
    await h.send("/start")
    await h.press(Nav(to=CUSTOM_SCREEN, arg=str(public)))
    assert h.last_text() == "<b>Правила сервиса</b>"
    assert h.t("nav.back") in h.button_texts()
    await h.press(Nav(to=CUSTOM_SCREEN, arg=str(secret)))
    assert h.t("core_user.menu.button_gone") in h.popups()
    await h.send("/start", uid=OWNER)
    assert "Админам" in h.button_texts()
    await h.press(Nav(to=CUSTOM_SCREEN, arg=str(secret)), uid=OWNER)
    assert h.last_text() == "secret"
    await h.press(Nav(to=CUSTOM_SCREEN, arg="999"))
    assert h.popups().count(h.t("core_user.menu.button_gone")) == 2
    assert USER  # the default user pressed the hidden button above


# --- cache ------------------------------------------------------------------------------------------------------------
async def test_cache_uses_version_stamp_and_ttl(session_factory: SessionFactory) -> None:
    now = [0.0]
    cache = MenuCache(session_factory, ttl=10.0, clock=lambda: now[0])
    assert await cache.items() == ()
    async with session_factory.begin() as session:  # a change made by "another process"
        await menu_buttons.create(session, kind="url", title="A", payload="https://a.example")
    assert await cache.items() == ()  # still within the TTL
    now[0] = 11.0
    assert [i.title for i in await cache.items()] == ["A"]

    async with session_factory.begin() as session:
        button = await session.scalar(select(MenuButton))
        assert button is not None
        await menu_buttons.update(session, button, title="B")
    cache.invalidate()
    assert [i.title for i in await cache.items()] == ["B"]

    async with session_factory.begin() as session:  # nothing is re-read while the stamp is unchanged
        await session.execute(MenuButton.__table__.update().values(title="C"))
    now[0] = 30.0
    assert [i.title for i in await cache.items()] == ["B"]
    async with session_factory.begin() as session:
        await kv.set(session, menu_buttons.VERSION_KEY, "changed")
    now[0] = 50.0
    assert [i.title for i in await cache.items()] == ["C"]


async def test_inactive_buttons_are_not_cached(session_factory: SessionFactory) -> None:
    await _add(session_factory, kind="url", title="Off", payload="https://a.example", is_active=False)
    assert await menu_buttons.cache_for(session_factory).items() == ()


async def test_move_crosses_rows(session_factory: SessionFactory) -> None:
    a = await _add(session_factory, kind="url", title="A", payload="https://a.example", row=0)
    b = await _add(session_factory, kind="url", title="B", payload="https://b.example", row=0)
    c = await _add(session_factory, kind="url", title="C", payload="https://c.example", row=1)
    async with session_factory.begin() as session:
        button = await menu_buttons.get(session, c)
        assert button is not None
        rows = await menu_buttons.move(session, button, -1)
        assert [(r.id, r.row) for r in rows] == [(a, 0), (b, 0), (c, 0)]
        button = await menu_buttons.get(session, a)
        assert button is not None
        rows = await menu_buttons.move(session, button, 1)
        assert [r.id for r in rows] == [b, a, c]


# --- admin API --------------------------------------------------------------------------------------------------------
async def test_api_crud_reorder_and_audit(api: Api) -> None:
    created = await api.post(
        "/menu-buttons",
        json={"kind": "url", "title": " Канал ", "titles": {"en": "Channel"}, "payload": "https://t.me/news"},
    )
    assert created.status_code == 201, created.text
    first = created.json()
    assert first["title"] == "Канал" and first["titles"] == {"en": "Channel"} and first["sort_order"] == 0
    second = (
        await api.post("/menu-buttons", json={"kind": "text_screen", "title": "FAQ", "payload": "<i>FAQ</i>"})
    ).json()
    assert second["sort_order"] == 1

    bad = await api.post("/menu-buttons", json={"kind": "webapp", "title": "x", "payload": "http://a.example"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_button"
    assert (await api.post("/menu-buttons", json={"kind": "nope", "title": "x", "payload": "y"})).status_code == 422
    kind_change = await api.patch(f"/menu-buttons/{first['id']}", json={"kind": "copy_text"})
    assert kind_change.status_code == 200 and kind_change.json()["payload"] == "https://t.me/news"
    assert (await api.patch(f"/menu-buttons/{first['id']}", json={"kind": "text_screen"})).status_code == 200
    patched = (await api.patch(f"/menu-buttons/{first['id']}", json={"audience": "admins", "is_active": False})).json()
    assert patched["audience"] == "admins" and patched["is_active"] is False
    assert (await api.patch(f"/menu-buttons/{first['id']}", json={})).status_code == 400

    order = (
        await api.post(
            "/menu-buttons/reorder",
            json={
                "items": [
                    {"id": second["id"], "row": 0, "sort_order": 0},
                    {"id": first["id"], "row": 1, "sort_order": 0},
                ]
            },
        )
    ).json()
    assert [b["id"] for b in order] == [second["id"], first["id"]]
    assert (
        await api.post("/menu-buttons/reorder", json={"items": [{"id": 999, "row": 0, "sort_order": 0}]})
    ).status_code == 400
    moved = (await api.post(f"/menu-buttons/{first['id']}/move", json={"direction": "up"})).json()
    assert [(b["id"], b["row"]) for b in moved] == [(second["id"], 0), (first["id"], 0)]  # joined the row above
    moved = (await api.post(f"/menu-buttons/{first['id']}/move", json={"direction": "up"})).json()
    assert [b["id"] for b in moved] == [first["id"], second["id"]]

    assert (await api.get(f"/menu-buttons/{second['id']}")).json()["title"] == "FAQ"
    assert (await api.delete(f"/menu-buttons/{second['id']}")).status_code == 200
    assert (await api.get(f"/menu-buttons/{second['id']}")).status_code == 404
    assert [b["id"] for b in (await api.get("/menu-buttons")).json()] == [first["id"]]

    async with api.ctx.session_factory() as session:
        actions = set((await session.scalars(select(AdminAuditLog.action))).all())
    assert {"menu_button.create", "menu_button.update", "menu_button.reorder", "menu_button.move"} <= actions
    assert "menu_button.delete" in actions


async def test_api_builtins(api: Api) -> None:
    listed = (await api.get("/menu-buttons/builtins")).json()
    assert {b["key"] for b in listed} == set(menu_buttons.BUILTIN_KEYS) and not any(b["hidden"] for b in listed)
    saved = await api.put("/menu-buttons/builtins", json={"hidden": ["referral", "gift"]})
    assert saved.status_code == 200
    assert api.ctx.settings.menu_hidden_buttons == ["referral", "gift"]
    assert (await api.put("/menu-buttons/builtins", json={"hidden": ["admin"]})).status_code == 400


async def test_api_rbac(api: Api) -> None:
    created = (
        await api.post("/menu-buttons", json={"kind": "url", "title": "A", "payload": "https://a.example"})
    ).json()
    viewer = await as_role(api, AdminRole.VIEWER, 3001)
    assert (await viewer.get("/menu-buttons")).status_code == 200
    assert (await viewer.get("/menu-buttons/builtins")).status_code == 200
    body = {"kind": "url", "title": "B", "payload": "https://b.example"}
    assert (await viewer.post("/menu-buttons", json=body)).status_code == 403
    assert (await viewer.patch(f"/menu-buttons/{created['id']}", json={"title": "Z"})).status_code == 403
    assert (await viewer.delete(f"/menu-buttons/{created['id']}")).status_code == 403
    assert (await viewer.put("/menu-buttons/builtins", json={"hidden": []})).status_code == 403
    support = await as_role(api, AdminRole.SUPPORT, 3002)
    assert (await support.post("/menu-buttons", json=body)).status_code == 403
    admin = await as_role(api, AdminRole.ADMIN, 3003)
    assert (await admin.post("/menu-buttons", json=body)).status_code == 201
    assert (await admin.put("/menu-buttons/builtins", json={"hidden": ["buy"]})).status_code == 200


async def test_api_changes_reach_the_menu_cache_at_once(api: Api) -> None:
    cache = menu_buttons.cache_for(api.ctx.session_factory)
    assert await cache.items() == ()
    created = (
        await api.post("/menu-buttons", json={"kind": "url", "title": "A", "payload": "https://a.example"})
    ).json()
    assert [i.id for i in await cache.items()] == [created["id"]]
    await api.patch(f"/menu-buttons/{created['id']}", json={"is_active": False})
    assert await cache.items() == ()
