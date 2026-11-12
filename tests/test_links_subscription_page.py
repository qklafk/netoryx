"""Tests for app.services.subscription_page (DB rows + fake panel registry, fully offline)."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest

from app.config import Settings
from app.db.models import (
    Endpoint,
    EndpointKind,
    EndpointProfile,
    Inbound,
    Location,
    Panel,
    PanelKind,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory
from app.services import subscription_page as sp
from app.services.links import build_vless, compact_reality_hello
from app.services.panels.base import PanelBackend, PanelError, RemoteAccount
from app.services.subscription_page import AccessState, render, usage
from tests.conftest import make_settings
from tests.fakes import FakePanel

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
TOKEN = "tok-123"
CLIENT_UUID = "6f1c1c2e-8d1f-4b6c-9a51-3e0e2c4d5f60"
GIB = 1024**3
REALITY_PARAMS = {
    "protocol": "vless",
    "port": 443,
    "type": "tcp",
    "security": "reality",
    "pbk": "PUBKEY",
    "sid": "6ba85179",
    "sni": "www.example.ru",
    "fp": "chrome",
    "spx": "/a1b2c3",
    "flow_allowed": True,
}


# ---------------------------------------------------------------------------------------------
# Fakes & fixtures
# ---------------------------------------------------------------------------------------------
@dataclass
class CountingPanel(FakePanel):
    """FakePanel that counts get_links calls and can be switched to fail."""

    get_links_calls: int = 0
    links_override: list[str] | None = None
    seen_headers: list[Mapping[str, str] | None] = field(default_factory=list)

    async def get_links(self, account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        self.get_links_calls += 1
        self.seen_headers.append(client_headers)
        links = await super().get_links(account, client_headers)
        return self.links_override if self.links_override is not None else links


class FakeRegistry:
    def __init__(self, panels: dict[int, PanelBackend]) -> None:
        self.panels = panels
        self.gets: list[int] = []

    async def get(self, panel_id: int) -> PanelBackend:
        self.gets.append(panel_id)
        try:
            return self.panels[panel_id]
        except KeyError:
            raise PanelError(f"unknown panel {panel_id}") from None


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    sp.clear_caches()


@dataclass
class World:
    settings: Settings
    session_factory: SessionFactory
    registry: FakeRegistry
    xui: CountingPanel
    rw: CountingPanel
    ids: dict[str, int] = field(default_factory=dict)

    async def render(self, profile: sp.Profile = "all", headers: Mapping[str, str] | None = None, **kw: Any) -> Any:
        return await render(
            self.session_factory,
            self.registry,
            kw.pop("settings", self.settings),
            kw.pop("token", TOKEN),
            profile=profile,
            client_headers=headers,
            now=kw.pop("now", NOW),
        )

    async def update(self, model: type[Any], pk: Any, **values: Any) -> None:
        async with self.session_factory.begin() as session:
            row = await session.get(model, pk)
            for key, value in values.items():
                setattr(row, key, value)


async def add(session_factory: SessionFactory, *rows: Any) -> None:
    async with session_factory.begin() as session:
        session.add_all(rows)


async def seed_user(
    session_factory: SessionFactory, *, status: SubscriptionStatus | None = SubscriptionStatus.ACTIVE, **user: Any
) -> None:
    rows: list[Any] = [
        User(tg_id=42, referral_code="REF42", sub_token=TOKEN, language=user.pop("language", "ru"), **user)
    ]
    await add(session_factory, *rows)
    if status is not None:
        await add(
            session_factory,
            Subscription(
                id=1,
                user_id=42,
                client_uuid=CLIENT_UUID,
                status=status,
                expires_at=NOW + timedelta(days=10),
                device_limit=3,
                traffic_limit_gb=100,
            ),
        )


@pytest.fixture
async def world(session_factory: SessionFactory) -> World:
    """DE (sort 0) with a standard direct and a whitelist relay endpoint, NL (sort 1) with a standard one."""
    settings = make_settings(support_url="https://t.me/support", channel_url="https://t.me/channel")
    xui = CountingPanel(kind=PanelKind.XUI)
    rw = CountingPanel(kind=PanelKind.REMNAWAVE)
    registry = FakeRegistry({1: xui, 2: rw})
    await seed_user(session_factory)
    await add(
        session_factory,
        Location(id=1, name="Германия", flag="🇩🇪", sort_order=0),
        Location(id=2, name="Нидерланды", flag="🇳🇱", sort_order=1),
        Panel(id=1, name="xui", kind=PanelKind.XUI, url="https://xui.example:2053/p"),
    )
    await add(
        session_factory,
        Inbound(
            id=1,
            panel_id=1,
            remote_id=7,
            location_id=1,
            name="de",
            params=json.dumps(REALITY_PARAMS),
            flow="xtls-rprx-vision",
        ),
        Inbound(
            id=2,
            panel_id=1,
            remote_id=8,
            location_id=2,
            name="nl",
            params=json.dumps(REALITY_PARAMS),
            flow="xtls-rprx-vision",
        ),
    )
    await add(
        session_factory,
        # Inserted in scrambled order on purpose: rendering must sort.
        Endpoint(
            id=3, inbound_id=2, kind=EndpointKind.DIRECT, profile=EndpointProfile.STANDARD, address="198.51.100.2"
        ),
        Endpoint(
            id=2,
            inbound_id=1,
            kind=EndpointKind.RELAY,
            profile=EndpointProfile.WHITELIST,
            address="84.201.1.2",
            port=8443,
        ),
        Endpoint(
            id=1, inbound_id=1, kind=EndpointKind.DIRECT, profile=EndpointProfile.STANDARD, address="203.0.113.10"
        ),
    )
    return World(settings, session_factory, registry, xui, rw)


async def add_remnawave(
    world: World, *, location_id: int | None = None, state: ProvisionState = ProvisionState.ENABLED
) -> None:
    options = json.dumps({"location_id": location_id}) if location_id else None
    await add(
        world.session_factory, Panel(id=2, name="rw", kind=PanelKind.REMNAWAVE, url="http://rw:3000", options=options)
    )
    await add(
        world.session_factory,
        Provision(
            id=10,
            subscription_id=1,
            panel_id=2,
            inbound_id=None,
            inbound_key=0,
            remote_email="tg_42",
            remote_id="5",
            remote_short_id="short5",
            desired_state=ProvisionState.ENABLED,
            actual_state=state,
        ),
    )


def names(links: list[str]) -> list[str]:
    return [unquote(urlsplit(link).fragment).split("?serverDescription=")[0] for link in links]


def decode_body(body: str) -> list[str]:
    return base64.b64decode(body).decode().split("\n") if body else []


def b64_header(value: str) -> str:
    assert value.startswith("base64:")
    return base64.b64decode(value[len("base64:") :]).decode()


# ---------------------------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------------------------
async def test_unknown_or_empty_token_is_none(world: World) -> None:
    assert await world.render(token="nope") is None
    assert await world.render(token="") is None


async def test_reality_links_follow_the_panel_when_compact_hello_is_off(world: World) -> None:
    result = await world.render(settings=make_settings(reality_compact_hello=False))
    expected = build_vless(CLIENT_UUID, "203.0.113.10", 443, REALITY_PARAMS, "🇩🇪 Германия", None, "xtls-rprx-vision")
    assert result.links[0] == expected


async def test_active_subscription_order_names_and_body(world: World) -> None:
    result = await world.render()
    assert result.state == AccessState.ACTIVE
    assert names(result.links) == ["🇩🇪 Германия", "🇩🇪 Германия · LTE", "🇳🇱 Нидерланды"]
    assert decode_body(result.body) == result.links
    assert result.media_type == "text/plain; charset=utf-8"
    expected_first = build_vless(
        CLIENT_UUID, "203.0.113.10", 443, compact_reality_hello(REALITY_PARAMS), "🇩🇪 Германия", None, "xtls-rprx-vision"
    )
    assert result.links[0] == expected_first
    assert "fp=firefox" in result.links[0] and "support-x25519mlkem768=false" in result.links[0]
    assert "flow=xtls-rprx-vision" in result.links[0]
    assert result.links[1].startswith(f"vless://{CLIENT_UUID}@84.201.1.2:8443?")


async def test_sort_order_location_then_profile_then_endpoint(world: World) -> None:
    await world.update(Location, 2, sort_order=-1)  # NL first now
    await add(
        world.session_factory,
        Endpoint(
            id=4,
            inbound_id=1,
            kind=EndpointKind.DIRECT,
            profile=EndpointProfile.STANDARD,
            address="203.0.113.11",
            sort_order=-5,
            remark="DE fast",
        ),
    )
    result = await world.render()
    assert names(result.links) == ["🇳🇱 Нидерланды", "DE fast", "🇩🇪 Германия", "🇩🇪 Германия · LTE"]


async def test_duplicate_default_names_get_ordinals(world: World) -> None:
    await add(
        world.session_factory,
        Endpoint(
            id=4,
            inbound_id=1,
            kind=EndpointKind.DIRECT,
            profile=EndpointProfile.STANDARD,
            address="203.0.113.11",
            sort_order=1,
        ),
        Endpoint(
            id=5,
            inbound_id=1,
            kind=EndpointKind.DIRECT,
            profile=EndpointProfile.STANDARD,
            address="203.0.113.12",
            sort_order=2,
        ),
    )
    result = await world.render(profile="standard")
    assert names(result.links) == ["🇩🇪 Германия", "🇩🇪 Германия 2", "🇩🇪 Германия 3", "🇳🇱 Нидерланды"]


async def test_endpoint_location_overrides_inbound_location_and_missing_location_sorts_last(world: World) -> None:
    await world.update(Endpoint, 1, location_id=2)
    await world.update(Inbound, 2, location_id=None)
    result = await world.render(profile="standard")
    assert names(result.links) == ["🇳🇱 Нидерланды", "TestVPN"]


async def test_description_becomes_server_description(world: World) -> None:
    await world.update(Endpoint, 1, description="Самый быстрый сервер в Европе и мире")
    link = (await world.render()).links[0]
    encoded = urlsplit(link).fragment.split("?serverDescription=")[1]
    assert base64.b64decode(encoded).decode() == "Самый быстрый сервер в Европе…"


@pytest.mark.parametrize(
    ("model", "pk", "values"),
    [
        (Endpoint, 1, {"auto_hidden": True}),
        (Endpoint, 1, {"is_active": False}),
        (Location, 2, {"is_active": False}),
    ],
)
async def test_hidden_entries_skipped(world: World, model: type[Any], pk: int, values: dict[str, Any]) -> None:
    await world.update(model, pk, **values)
    result = await world.render()
    assert len(result.links) == 2


async def test_inactive_inbound_or_panel_skipped(world: World) -> None:
    await world.update(Inbound, 2, is_active=False)
    assert names((await world.render()).links) == ["🇩🇪 Германия", "🇩🇪 Германия · LTE"]
    await world.update(Panel, 1, is_active=False)
    assert (await world.render()).links == []


@pytest.mark.parametrize(
    "params",
    [None, "{not json", json.dumps({**REALITY_PARAMS, "protocol": "trojan"}), json.dumps([1])],
)
async def test_unusable_inbound_params_skipped(world: World, params: str | None) -> None:
    await world.update(Inbound, 2, params=params)
    assert names((await world.render()).links) == ["🇩🇪 Германия", "🇩🇪 Германия · LTE"]


async def test_broken_endpoint_skipped_others_render(world: World) -> None:
    await world.update(Endpoint, 1, overrides="[not an object")
    no_port = {k: v for k, v in REALITY_PARAMS.items() if k != "port"}
    await world.update(Inbound, 2, params=json.dumps(no_port))
    assert names((await world.render()).links) == ["🇩🇪 Германия · LTE"]


async def test_empty_server_list_renders_empty_body(world: World) -> None:
    await world.update(Panel, 1, is_active=False)
    result = await world.render()
    assert result.body == "" and result.links == [] and result.state == AccessState.ACTIVE


# ---------------------------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------------------------
async def test_profile_filters(world: World) -> None:
    assert names((await world.render("standard")).links) == ["🇩🇪 Германия", "🇳🇱 Нидерланды"]
    assert names((await world.render("whitelist")).links) == ["🇩🇪 Германия · LTE"]


async def test_separate_whitelist_url_moves_lte_out_of_all(world: World) -> None:
    settings = make_settings(sub_whitelist_separate_url=True)
    assert names((await world.render("all", settings=settings)).links) == ["🇩🇪 Германия", "🇳🇱 Нидерланды"]
    assert names((await world.render("whitelist", settings=settings)).links) == ["🇩🇪 Германия · LTE"]


async def test_network_tags(world: World) -> None:
    settings = make_settings(sub_happ_network_tags=True)
    result = await world.render(settings=settings)
    assert names(result.links)[1] == "🇩🇪 Германия · LTE only Mobile"
    assert result.headers["network-filter"] == "true"
    assert "network-filter" not in (await world.render()).headers


# ---------------------------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------------------------
async def test_headers(world: World) -> None:
    world.xui.accounts[(7, "tg42-1")] = RemoteAccount("tg42-1", CLIENT_UUID, True, None, 3, 0, used_bytes=1000)
    await add(
        world.session_factory,
        Provision(
            id=1,
            subscription_id=1,
            panel_id=1,
            inbound_id=1,
            inbound_key=1,
            remote_email="tg42-1",
            desired_state=ProvisionState.ENABLED,
            actual_state=ProvisionState.ENABLED,
        ),
    )
    settings = make_settings(
        support_url="https://t.me/support",
        channel_url="https://t.me/channel",
        sub_announce="Привет!",
        sub_update_interval_hours=6,
    )
    headers = (await world.render(settings=settings)).headers
    expire = int((NOW + timedelta(days=10)).timestamp())
    assert headers["subscription-userinfo"] == f"upload=0; download=1000; total={100 * GIB}; expire={expire}"
    assert b64_header(headers["profile-title"]) == "TestVPN"
    assert headers["profile-update-interval"] == "6"
    assert headers["support-url"] == "https://t.me/support"
    assert headers["profile-web-page-url"] == "https://t.me/channel"
    assert b64_header(headers["announce"]) == "Привет!"
    assert headers["content-disposition"] == "attachment; filename=\"TestVPN\"; filename*=UTF-8''TestVPN"
    assert "routing" not in headers
    for value in headers.values():
        value.encode("ascii")  # every header must be ASCII-safe


async def test_optional_headers_absent_and_interval_floor(world: World) -> None:
    headers = (await world.render(settings=make_settings(sub_update_interval_hours=0))).headers
    assert not {"support-url", "profile-web-page-url", "announce", "routing"} & set(headers)
    assert headers["profile-update-interval"] == "1"


async def test_profile_title_limits_and_whitelist_suffix(world: World) -> None:
    settings = make_settings(brand_name="Очень длинное название VPN сервиса")
    standard = b64_header((await world.render(settings=settings)).headers["profile-title"])
    whitelist = b64_header((await world.render("whitelist", settings=settings)).headers["profile-title"])
    assert len(standard) <= 25 and standard.endswith("…")
    assert len(whitelist) <= 25 and whitelist.endswith(" LTE")
    assert b64_header((await world.render("whitelist")).headers["profile-title"]) == "TestVPN LTE"


async def test_announce_truncated_to_200(world: World) -> None:
    headers = (await world.render(settings=make_settings(sub_announce="я" * 500))).headers
    assert len(b64_header(headers["announce"])) == 200


async def test_unlimited_traffic_total_zero(world: World) -> None:
    await world.update(Subscription, 1, traffic_limit_gb=0)
    assert "total=0;" in (await world.render()).headers["subscription-userinfo"]


def test_header_helpers() -> None:
    assert sp.header_safe(" https://t.me/x\r\n") == "https://t.me/x"
    assert sp.header_safe("https://пример.рф/a b") == "https://%D0%BF%D1%80%D0%B8%D0%BC%D0%B5%D1%80.%D1%80%D1%84/a b"
    assert sp.content_disposition('Мой "VPN"') == (
        "attachment; filename=\"VPN\"; filename*=UTF-8''%D0%9C%D0%BE%D0%B9%20%22VPN%22"
    )
    assert sp.content_disposition("Мой").startswith('attachment; filename="subscription";')


# ---------------------------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------------------------
ROUTING_ON = {"sub_routing_ru_direct": True}


@pytest.mark.parametrize(
    ("headers", "prefix"),
    [
        ({"User-Agent": "Happ/3.7.0/ios"}, "happ://routing/onadd/"),
        ({"user-agent": "INCY/2.7.0/android"}, "incy://routing/onadd/"),
        ({"User-Agent": "okhttp/4", "X-Client": "INCY"}, "incy://routing/onadd/"),
    ],
)
async def test_routing_happ_and_incy(world: World, headers: dict[str, str], prefix: str) -> None:
    routing = (await world.render(headers=headers, settings=make_settings(**ROUTING_ON))).headers["routing"]
    assert routing.startswith(prefix)
    profile = json.loads(base64.b64decode(routing[len(prefix) :]))
    assert profile["Name"] == "TestVPN RU"
    assert "geosite:category-ru" in profile["DirectSites"]
    assert profile["DirectIp"][0] == "geoip:ru" and "10.0.0.0/8" in profile["DirectIp"]
    assert profile["GlobalProxy"] == "true" and profile["LastUpdated"] == sp.ROUTING_PROFILE_VERSION


async def test_routing_v2raytun(world: World) -> None:
    headers = {"User-Agent": "v2RayTun/5.1 (iOS)"}
    routing = (await world.render(headers=headers, settings=make_settings(**ROUTING_ON))).headers["routing"]
    profile = json.loads(base64.b64decode(routing))
    rule = profile["rules"][0]
    assert rule["outboundTag"] == "direct" and "geoip:ru" in rule["ip"] and "geosite:category-ru" in rule["domain"]
    assert profile["id"] == sp.v2raytun_routing_profile(world.settings)["id"]  # stable across renders


@pytest.mark.parametrize("headers", [None, {"User-Agent": "Hiddify/2.5"}, {"User-Agent": "Mozilla/5.0"}])
async def test_routing_omitted_for_unknown_clients(world: World, headers: dict[str, str] | None) -> None:
    assert "routing" not in (await world.render(headers=headers, settings=make_settings(**ROUTING_ON))).headers


async def test_routing_omitted_for_expired(world: World) -> None:
    headers = {"User-Agent": "Happ/3.7.0"}
    result = await world.render(headers=headers, settings=make_settings(**ROUTING_ON), now=NOW + timedelta(days=30))
    assert "routing" not in result.headers


# ---------------------------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------------------------
def assert_placeholder(result: Any, name: str) -> None:
    assert len(result.links) == 1
    link = result.links[0]
    assert link.startswith(f"vless://{sp.PLACEHOLDER_UUID}@0.0.0.0:1?encryption=none&security=none&type=tcp#")
    assert names(result.links) == [name]
    assert decode_body(result.body) == result.links


async def test_expired_by_time_shows_placeholder_and_announce(world: World) -> None:
    result = await world.render(now=NOW + timedelta(days=10))  # expires_at == now counts as expired
    assert result.state == AccessState.EXPIRED
    assert_placeholder(result, "⛔ Подписка истекла — продлите в боте")
    assert "Подписка истекла" in b64_header(result.headers["announce"])
    assert "expire=" in result.headers["subscription-userinfo"]
    assert world.registry.gets == []  # no panel calls for a placeholder


async def test_expired_status_and_english(world: World) -> None:
    await world.update(Subscription, 1, status=SubscriptionStatus.EXPIRED)
    await world.update(User, 42, language="en")
    result = await world.render()
    assert_placeholder(result, "⛔ Subscription expired — renew in the bot")
    assert b64_header(result.headers["announce"]).startswith("Your subscription has expired")


async def test_disabled_and_banned(world: World) -> None:
    await world.update(Subscription, 1, status=SubscriptionStatus.DISABLED)
    result = await world.render()
    assert result.state == AccessState.DISABLED
    assert_placeholder(result, "⛔ Подписка приостановлена — напишите в поддержку")
    await world.update(Subscription, 1, status=SubscriptionStatus.ACTIVE)
    await world.update(User, 42, is_banned=True)
    assert (await world.render()).state == AccessState.DISABLED


async def test_user_without_subscription(session_factory: SessionFactory) -> None:
    await seed_user(session_factory, status=None)
    result = await render(session_factory, FakeRegistry({}), make_settings(), TOKEN, now=NOW)
    assert result is not None and result.state == AccessState.NONE
    assert_placeholder(result, "⛔ Нет активной подписки — оформите в боте")
    assert "subscription-userinfo" not in result.headers


def test_placeholder_unknown_language_falls_back_to_ru() -> None:
    link = sp.placeholder_link(AccessState.EXPIRED, "de")
    assert unquote(urlsplit(link).fragment) == "⛔ Подписка истекла — продлите в боте"
    assert sp.placeholder_link(AccessState.EXPIRED, None) == link


def test_access_state_matrix() -> None:
    user = User(tg_id=1, referral_code="r", sub_token="t", is_banned=False)
    sub = Subscription(status=SubscriptionStatus.ACTIVE, expires_at=NOW + timedelta(seconds=1))
    assert sp.access_state(user, sub, NOW) == AccessState.ACTIVE
    assert sp.access_state(user, None, NOW) == AccessState.NONE
    sub.expires_at = NOW
    assert sp.access_state(user, sub, NOW) == AccessState.EXPIRED
    user.is_banned = True
    assert sp.access_state(user, None, NOW) == AccessState.DISABLED


# ---------------------------------------------------------------------------------------------
# Remnawave
# ---------------------------------------------------------------------------------------------
RW_LINK = f"vless://{CLIENT_UUID}@rw.example.com:443?type=tcp&security=reality#RW"


async def test_remnawave_links_merged_after_locations_without_location(world: World) -> None:
    await add_remnawave(world)
    result = await world.render(headers={"User-Agent": "Happ/3.7.0", "X-HWID": "abc"})
    assert result.links[-1] == RW_LINK
    assert len(result.links) == 4
    assert world.rw.seen_headers[0] == {"user-agent": "Happ/3.7.0", "x-hwid": "abc"}


async def test_remnawave_links_grouped_into_panel_location(world: World) -> None:
    await add_remnawave(world, location_id=1)
    world.rw.links_override = ["vless://a@rw:1#A", "vless://b@rw:2#B"]
    result = await world.render()
    assert names(result.links) == ["🇩🇪 Германия", "A", "B", "🇩🇪 Германия · LTE", "🇳🇱 Нидерланды"]


async def test_remnawave_excluded_from_whitelist_profile(world: World) -> None:
    await add_remnawave(world)
    assert RW_LINK not in (await world.render("whitelist")).links
    assert world.rw.get_links_calls == 0


@pytest.mark.parametrize(
    ("change", "kwargs"),
    [
        ("absent", {"state": ProvisionState.ABSENT}),
        ("panel-off", {}),
        ("location-off", {"location_id": 2}),
    ],
)
async def test_remnawave_skipped(world: World, change: str, kwargs: dict[str, Any]) -> None:
    await add_remnawave(world, **kwargs)
    if change == "panel-off":
        await world.update(Panel, 2, is_active=False)
    if change == "location-off":
        await world.update(Location, 2, is_active=False)
    assert RW_LINK not in (await world.render()).links


async def test_remnawave_malformed_options_ignored(world: World) -> None:
    await add_remnawave(world)
    await world.update(Panel, 2, options="{broken")
    assert (await world.render()).links[-1] == RW_LINK
    await world.update(Panel, 2, options=json.dumps({"location_id": True}))
    assert (await world.render()).links[-1] == RW_LINK


async def test_remnawave_links_cached_and_invalidated_by_revision(world: World) -> None:
    await add_remnawave(world)
    await world.render()
    await world.render()
    assert world.rw.get_links_calls == 1
    await world.update(Subscription, 1, revision=2)
    await world.render()
    assert world.rw.get_links_calls == 2


async def test_remnawave_account_built_from_provision(world: World) -> None:
    await add_remnawave(world)
    seen: list[RemoteAccount] = []
    original = world.rw.get_links

    async def capture(account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        seen.append(account)
        return await original(account, client_headers)

    world.rw.get_links = capture  # type: ignore[method-assign]
    await world.render()
    account = seen[0]
    assert (account.email, account.uuid, account.short_id, account.remote_id) == ("tg_42", CLIENT_UUID, "short5", "5")
    assert account.traffic_limit_bytes == 100 * GIB and account.enabled


async def test_remnawave_failure_serves_stale_links(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_remnawave(world)
    assert (await world.render()).links[-1] == RW_LINK
    monkeypatch.setattr(sp, "REMNAWAVE_LINKS_TTL_SEC", -1.0)
    sp.clear_caches()
    await world.render()  # repopulate with an already-expired entry
    world.rw.down = True
    result = await world.render()
    assert result.links[-1] == RW_LINK


async def test_remnawave_failure_without_cache_drops_only_its_links(world: World) -> None:
    await add_remnawave(world)
    world.rw.down = True
    result = await world.render()
    assert len(result.links) == 3 and RW_LINK not in result.links


async def test_remnawave_unknown_panel_in_registry(world: World) -> None:
    await add_remnawave(world)
    world.registry.panels.pop(2)
    assert len((await world.render()).links) == 3


async def test_remnawave_slow_panel_times_out(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    await add_remnawave(world)

    async def slow(account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        await asyncio.sleep(5)
        return []

    world.rw.get_links = slow  # type: ignore[method-assign]
    monkeypatch.setattr(sp, "PANEL_CALL_TIMEOUT_SEC", 0.05)
    assert len((await world.render()).links) == 3


# ---------------------------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------------------------
async def add_xui_provision(world: World, pid: int, inbound_id: int, email: str, state: ProvisionState) -> None:
    await add(
        world.session_factory,
        Provision(
            id=pid,
            subscription_id=1,
            panel_id=1,
            inbound_id=inbound_id,
            inbound_key=inbound_id,
            remote_email=email,
            desired_state=ProvisionState.ENABLED,
            actual_state=state,
        ),
    )


async def load_sub(world: World) -> Subscription:
    async with world.session_factory() as session:
        sub = await session.get(Subscription, 1)
    assert sub is not None
    return sub


async def test_usage_sums_all_accounts(world: World) -> None:
    await add_xui_provision(world, 1, 1, "tg42-1", ProvisionState.ENABLED)
    await add_xui_provision(world, 2, 2, "tg42-2", ProvisionState.DISABLED)
    await add(world.session_factory, Inbound(id=3, panel_id=1, remote_id=9, name="x", params=None))
    await add_xui_provision(world, 3, 3, "tg42-gone", ProvisionState.ABSENT)
    await add_remnawave(world)
    world.xui.accounts[(7, "tg42-1")] = RemoteAccount("tg42-1", CLIENT_UUID, True, None, 3, 0, used_bytes=100)
    world.xui.accounts[(8, "tg42-2")] = RemoteAccount("tg42-2", CLIENT_UUID, False, None, 3, 0, used_bytes=20)
    world.xui.accounts[(9, "tg42-gone")] = RemoteAccount("tg42-gone", CLIENT_UUID, True, None, 3, 0, used_bytes=999)
    world.rw.accounts[(None, "tg_42")] = RemoteAccount("tg_42", CLIENT_UUID, True, None, 3, 0, used_bytes=3)
    assert await usage(world.session_factory, world.registry, await load_sub(world)) == (0, 123)


async def test_usage_cached_for_five_minutes(world: World) -> None:
    await add_xui_provision(world, 1, 1, "tg42-1", ProvisionState.ENABLED)
    world.xui.accounts[(7, "tg42-1")] = RemoteAccount("tg42-1", CLIENT_UUID, True, None, 3, 0, used_bytes=100)
    sub = await load_sub(world)
    assert await usage(world.session_factory, world.registry, sub) == (0, 100)
    world.xui.accounts[(7, "tg42-1")].used_bytes = 500
    assert await usage(world.session_factory, world.registry, sub) == (0, 100)
    sp.clear_caches()
    assert await usage(world.session_factory, world.registry, sub) == (0, 500)


async def test_usage_partial_failure_and_missing_account(world: World) -> None:
    await add_xui_provision(world, 1, 1, "tg42-1", ProvisionState.ENABLED)
    await add_xui_provision(world, 2, 2, "tg42-missing", ProvisionState.ENABLED)
    await add_remnawave(world)
    world.xui.accounts[(7, "tg42-1")] = RemoteAccount("tg42-1", CLIENT_UUID, True, None, 3, 0, used_bytes=100)
    world.rw.down = True
    assert await usage(world.session_factory, world.registry, await load_sub(world)) == (0, 100)


async def test_usage_all_failing_serves_stale_or_zero(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_xui_provision(world, 1, 1, "tg42-1", ProvisionState.ENABLED)
    world.xui.accounts[(7, "tg42-1")] = RemoteAccount("tg42-1", CLIENT_UUID, True, None, 3, 0, used_bytes=100)
    sub = await load_sub(world)
    monkeypatch.setattr(sp, "USAGE_FAILURE_TTL_SEC", -1.0)  # negative cache covered separately
    world.xui.down = True
    assert await usage(world.session_factory, world.registry, sub) == (0, 0)
    world.xui.down = False
    monkeypatch.setattr(sp, "USAGE_TTL_SEC", -1.0)
    assert await usage(world.session_factory, world.registry, sub) == (0, 100)
    world.xui.down = True
    assert await usage(world.session_factory, world.registry, sub) == (0, 100)


async def test_usage_without_provisions_is_zero(world: World) -> None:
    assert await usage(world.session_factory, world.registry, await load_sub(world)) == (0, 0)
    assert world.registry.gets == []


async def test_usage_reads_only_traffic_counters(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Usage must use the cheap counter call: legacy 3x-ui get_account downloads the whole inbound."""
    await add_xui_provision(world, 1, 1, "tg42-1", ProvisionState.ENABLED)
    world.xui.accounts[(7, "tg42-1")] = RemoteAccount("tg42-1", CLIENT_UUID, True, None, 3, 0, used_bytes=100)

    async def heavy(target: int | None, email: str) -> RemoteAccount | None:
        raise AssertionError("get_account must not be used for usage")

    monkeypatch.setattr(world.xui, "get_account", heavy)
    assert await usage(world.session_factory, world.registry, await load_sub(world)) == (0, 100)


# ---------------------------------------------------------------------------------------------
# Unreachable panels must not stall /sub (health-aware, negative-cached, short usage budget)
# ---------------------------------------------------------------------------------------------
async def test_usage_skips_panels_marked_down(world: World) -> None:
    await add_xui_provision(world, 1, 1, "tg42-1", ProvisionState.ENABLED)
    await add_remnawave(world)
    world.xui.accounts[(7, "tg42-1")] = RemoteAccount("tg42-1", CLIENT_UUID, True, None, 3, 0, used_bytes=100)
    world.rw.accounts[(None, "tg_42")] = RemoteAccount("tg_42", CLIENT_UUID, True, None, 3, 0, used_bytes=3)
    await world.update(Panel, 1, consecutive_failures=99)
    sub = await load_sub(world)
    assert await usage(world.session_factory, world.registry, sub, world.settings) == (0, 3)
    assert 1 not in world.registry.gets


async def test_render_does_not_query_usage_of_down_panel(world: World) -> None:
    await add_xui_provision(world, 1, 1, "tg42-1", ProvisionState.ENABLED)
    await world.update(Panel, 1, consecutive_failures=world.settings.health_failures_before_alert)
    result = await world.render()
    assert len(result.links) == 3  # 3x-ui links are local: still served
    assert world.registry.gets == []


async def test_usage_failure_is_negative_cached(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_xui_provision(world, 1, 1, "tg42-1", ProvisionState.ENABLED)
    world.xui.down = True
    sub = await load_sub(world)
    assert await usage(world.session_factory, world.registry, sub) == (0, 0)
    assert await usage(world.session_factory, world.registry, sub) == (0, 0)
    assert world.registry.gets == [1]  # the second request does not retry the dead panel
    monkeypatch.setattr(sp, "USAGE_FAILURE_TTL_SEC", -1.0)
    sp.clear_caches()
    await usage(world.session_factory, world.registry, sub)
    await usage(world.session_factory, world.registry, sub)
    assert world.registry.gets == [1, 1, 1]


async def test_usage_failure_keeps_last_known_value(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_xui_provision(world, 1, 1, "tg42-1", ProvisionState.ENABLED)
    world.xui.accounts[(7, "tg42-1")] = RemoteAccount("tg42-1", CLIENT_UUID, True, None, 3, 0, used_bytes=100)
    sub = await load_sub(world)
    monkeypatch.setattr(sp, "USAGE_TTL_SEC", -1.0)
    assert await usage(world.session_factory, world.registry, sub) == (0, 100)
    world.xui.down = True
    assert await usage(world.session_factory, world.registry, sub) == (0, 100)
    assert await usage(world.session_factory, world.registry, sub) == (0, 100)
    assert world.registry.gets == [1, 1]


async def test_hanging_usage_does_not_hold_back_body(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import time

    await add_xui_provision(world, 1, 1, "tg42-1", ProvisionState.ENABLED)

    async def hang(target: int | None, email: str) -> int | None:
        await asyncio.sleep(30)
        return 0

    monkeypatch.setattr(world.xui, "get_used_bytes", hang)
    monkeypatch.setattr(sp, "PANEL_CALL_TIMEOUT_SEC", 5.0)
    monkeypatch.setattr(sp, "USAGE_CALL_TIMEOUT_SEC", 0.05)
    started = time.monotonic()
    result = await world.render()
    assert time.monotonic() - started < 1.0  # bounded by the usage budget, not the links timeout
    assert len(result.links) == 3
    started = time.monotonic()
    await world.render()
    assert time.monotonic() - started < 0.04  # failure cached: no second wait


async def test_remnawave_down_panel_serves_stale_without_calling(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_remnawave(world)
    monkeypatch.setattr(sp, "REMNAWAVE_LINKS_TTL_SEC", -1.0)
    assert (await world.render()).links[-1] == RW_LINK
    await world.update(Panel, 2, consecutive_failures=99)
    assert (await world.render()).links[-1] == RW_LINK
    assert world.rw.get_links_calls == 1


async def test_remnawave_down_panel_without_cache_is_skipped(world: World) -> None:
    await add_remnawave(world)
    await world.update(Panel, 2, consecutive_failures=99)
    result = await world.render()
    assert RW_LINK not in result.links and len(result.links) == 3
    assert world.rw.get_links_calls == 0


async def test_remnawave_failure_is_negative_cached(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_remnawave(world)
    world.rw.down = True
    await world.render()
    await world.render()
    assert world.rw.get_links_calls == 1
    world.rw.down = False
    monkeypatch.setattr(sp, "PANEL_FAILURE_TTL_SEC", -1.0)
    sp.clear_caches()
    world.rw.down = True
    await world.render()
    world.rw.down = False
    assert (await world.render()).links[-1] == RW_LINK


async def stored_links_cache(world: World) -> str | None:
    async with world.session_factory() as session:
        provision = await session.get(Provision, 10)
    assert provision is not None
    return provision.links_cache


async def test_remnawave_links_survive_restart_while_panel_fails(world: World) -> None:
    await add_remnawave(world)
    assert (await world.render()).links[-1] == RW_LINK
    sp.clear_caches()  # a restart empties the in-process caches
    world.rw.down = True
    result = await world.render()
    assert result.links[-1] == RW_LINK
    assert not result.servers_unavailable


async def test_remnawave_links_survive_restart_while_panel_marked_down(world: World) -> None:
    await add_remnawave(world)
    await world.render()
    sp.clear_caches()
    await world.update(Panel, 2, consecutive_failures=99)
    assert (await world.render()).links[-1] == RW_LINK
    assert world.rw.get_links_calls == 1


async def test_remnawave_links_survive_revision_bump_during_outage(world: World) -> None:
    await add_remnawave(world)
    await world.render()
    await world.update(Subscription, 1, revision=5)  # e.g. a renewal
    world.rw.down = True
    assert (await world.render()).links[-1] == RW_LINK


async def test_remnawave_saved_links_dropped_after_key_rotation(world: World) -> None:
    await add_remnawave(world)
    await world.render()
    sp.clear_caches()
    await world.update(Subscription, 1, client_uuid="0f0e0d0c-0000-4000-8000-000000000001", revision=2)
    world.rw.down = True
    assert RW_LINK not in (await world.render()).links


async def test_remnawave_saved_links_written_only_on_change(world: World) -> None:
    await add_remnawave(world)
    await world.render()
    first = await stored_links_cache(world)
    assert first is not None and RW_LINK in first
    sp.clear_caches()
    await world.render()
    assert await stored_links_cache(world) == first
    sp.clear_caches()
    world.rw.links_override = ["vless://x@rw:1#X"]
    await world.render()
    assert "vless://x@rw:1#X" in (await stored_links_cache(world) or "")


async def test_remnawave_saved_links_encrypted_with_secret_box(world: World) -> None:
    from app.utils.crypto import SecretBox

    box = SecretBox("k" * 32)
    await add_remnawave(world)
    await render(world.session_factory, world.registry, world.settings, TOKEN, now=NOW, secret_box=box)
    stored = await stored_links_cache(world)
    assert stored is not None and stored.startswith("enc:") and RW_LINK not in stored
    sp.clear_caches()
    world.rw.down = True
    result = await render(world.session_factory, world.registry, world.settings, TOKEN, now=NOW, secret_box=box)
    assert result is not None and result.links[-1] == RW_LINK


async def test_remnawave_failures_do_not_evict_cached_links(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_remnawave(world)
    await add(
        world.session_factory,
        User(tg_id=43, referral_code="REF43", sub_token="tok-43"),
        Subscription(
            id=2,
            user_id=43,
            client_uuid="0f0e0d0c-0000-4000-8000-000000000002",
            status=SubscriptionStatus.ACTIVE,
            expires_at=NOW + timedelta(days=5),
        ),
    )
    await add(
        world.session_factory,
        Provision(
            id=11,
            subscription_id=2,
            panel_id=2,
            inbound_key=0,
            remote_email="tg_43",
            desired_state=ProvisionState.ENABLED,
            actual_state=ProvisionState.ENABLED,
        ),
    )
    monkeypatch.setattr(sp, "_remnawave_links_cache", sp._TTLCache(max_entries=1))
    await world.render()
    await world.update(Provision, 10, links_cache=None)  # only the in-process copy is left
    world.rw.down = True
    await world.render(token="tok-43")  # fails with nothing to fall back on
    assert (await world.render()).links[-1] == RW_LINK


async def test_active_subscription_without_any_server_because_panel_failed_is_flagged(world: World) -> None:
    await add_remnawave(world)
    await world.update(Inbound, 1, is_active=False)
    await world.update(Inbound, 2, is_active=False)
    world.rw.down = True
    result = await world.render()
    assert result.links == [] and result.servers_unavailable
    world.rw.down = False
    sp.clear_caches()
    result = await world.render()
    assert result.links == [RW_LINK] and not result.servers_unavailable


async def test_empty_server_list_without_failures_is_not_flagged(world: World) -> None:
    await world.update(Inbound, 1, is_active=False)
    await world.update(Inbound, 2, is_active=False)
    result = await world.render()
    assert result.links == [] and not result.servers_unavailable


# ---------------------------------------------------------------------------------------------
# Remnawave device limits (HWID)
# ---------------------------------------------------------------------------------------------
@dataclass
class HwidPanel(CountingPanel):
    """Remnawave fake that also serves the panel's public subscription (where devices are counted)."""

    fetches: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    sub_body: str = ""
    sub_headers: dict[str, str] = field(default_factory=dict)

    async def fetch_subscription(
        self, short_id: str, forward_headers: Mapping[str, str] | None = None
    ) -> tuple[str, dict[str, str]]:
        self.fetches.append((short_id, dict(forward_headers or {})))
        self._maybe_fail("fetch_subscription")
        return self.sub_body, dict(self.sub_headers)


DEVICE_LINK = f"vless://{CLIENT_UUID}@node.example.com:443?type=tcp&security=reality#Node"
LIMIT_LINK = "vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1?type=tcp&security=none#Limit%20of%20devices"


@pytest.fixture
async def hwid_world(world: World) -> World:
    panel = HwidPanel(kind=PanelKind.REMNAWAVE)
    panel.sub_body = base64.b64encode(DEVICE_LINK.encode()).decode()
    world.rw = panel
    world.registry.panels[2] = panel
    await add_remnawave(world)
    return world


async def test_remnawave_device_limit_served_through_panel_subscription(hwid_world: World) -> None:
    world = hwid_world
    assert isinstance(world.rw, HwidPanel)
    client = {"User-Agent": "Happ/3.7.0", "X-HWID": "dev-1", "X-Device-OS": "Android", "Accept": "*/*"}
    result = await render(
        world.session_factory,
        world.registry,
        world.settings,
        TOKEN,
        client_headers=client,
        now=NOW,
        client_ip="1.2.3.4",
    )
    assert result is not None and result.links[-1] == DEVICE_LINK
    assert world.rw.get_links_calls == 0  # connection keys skip device accounting
    assert world.rw.fetches == [
        (
            "short5",
            {"x-hwid": "dev-1", "x-device-os": "Android", "user-agent": "Happ/3.7.0", "x-remnawave-real-ip": "1.2.3.4"},
        )
    ]


async def test_remnawave_device_answers_are_cached_per_device(hwid_world: World) -> None:
    world = hwid_world
    assert isinstance(world.rw, HwidPanel)
    await world.render(headers={"X-HWID": "dev-1"})
    await world.render(headers={"X-HWID": "dev-1"})
    await world.render(headers={"X-HWID": "dev-2"})
    assert [headers.get("x-hwid") for _, headers in world.rw.fetches] == ["dev-1", "dev-2"]


async def test_remnawave_device_limit_reached_shows_panel_notice(hwid_world: World) -> None:
    world = hwid_world
    assert isinstance(world.rw, HwidPanel)
    world.rw.sub_body = base64.b64encode(LIMIT_LINK.encode()).decode()
    world.rw.sub_headers = {"x-hwid-max-devices-reached": "true", "x-hwid-limit": "true"}
    result = await world.render(headers={"X-HWID": "dev-11"})
    assert result.links[-1] == LIMIT_LINK and DEVICE_LINK not in result.links


async def test_remnawave_device_failure_does_not_hand_saved_links_to_new_devices(hwid_world: World) -> None:
    world = hwid_world
    assert isinstance(world.rw, HwidPanel)
    await world.update(Provision, 10, links_cache=json.dumps({"uuid": CLIENT_UUID, "links": [DEVICE_LINK]}))
    assert (await world.render(headers={"X-HWID": "dev-1"})).links[-1] == DEVICE_LINK
    world.rw.down = True
    sp._remnawave_links_cache.clear()
    sp._remnawave_failures.clear()
    assert DEVICE_LINK not in (await world.render(headers={"X-HWID": "dev-new"})).links


async def test_remnawave_non_base64_client_agent_is_not_forwarded(hwid_world: World) -> None:
    world = hwid_world
    assert isinstance(world.rw, HwidPanel)
    result = await world.render(headers={"User-Agent": "SFA/1.11 (sing-box 1.11)", "X-HWID": "dev-1"})
    assert result.links[-1] == DEVICE_LINK
    assert "user-agent" not in world.rw.fetches[0][1]


async def test_remnawave_unlimited_devices_keep_connection_keys(hwid_world: World) -> None:
    world = hwid_world
    assert isinstance(world.rw, HwidPanel)
    await world.update(Subscription, 1, device_limit=0)
    assert (await world.render(headers={"X-HWID": "dev-1"})).links[-1] == RW_LINK
    assert world.rw.fetches == [] and world.rw.get_links_calls == 1
