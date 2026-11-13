"""Telegram-only subscription: server rules, eligible servers, panel convergence, the JSON config, the token, the
public URL, the admin API and bot screen, the one-time owner message and the installer summary."""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiogram.methods import SendMessage

from app.config import Settings
from app.core.modules import ModuleRegistry
from app.db.models import (
    AdminRole,
    Endpoint,
    EndpointKind,
    EndpointProfile,
    Inbound,
    Location,
    Panel,
    PanelKind,
)
from app.db.session import SessionFactory
from app.modules import admin_api
from app.modules.public_web import module as public_module
from app.modules.tg_only import module as tg_only_module
from app.modules.tg_only.admin_bot import TgOnlyCb
from app.modules.tg_only.jobs import notify_owners, reconcile_job
from app.services import kv, notifications, subscription_page, tg_only, tg_only_config
from app.services.panels.base import AccountSpec, PanelError, TgOnlyRouting
from app.services.panels.xray_template import (
    TG_ONLY_BLOCK_TAG,
    TG_ONLY_DOMAIN_TAG,
    TG_ONLY_IP_TAG,
    TemplateError,
    YoutubeExit,
    ad_rule,
    apply_extras,
    set_ad_blocking,
    set_tg_only,
    set_youtube_exit,
    tg_only_enabled,
)
from app.services.tg_only_data import TELEGRAM_CIDRS, TELEGRAM_DOMAINS
from app.utils.time import utcnow
from app.utils.xray_outbound import outbound_from_link
from app.web.app import create_web_app
from tests.conftest import make_settings
from tests.fakes import FakePanel
from tests.test_adblock_blocking import CATCH_ALL, OPERATOR_RULE, default_template
from tests.test_mod_admin_tools_support import Harness, make_harness, module, release_module_routers  # noqa: F401
from tests.test_mod_personal_support import OWNER, ModCtx, make_ctx
from tests.test_mod_webapi_support import Api, as_role, build_ctx
from tests.test_web_support import AsgiClient

REALITY = {
    "protocol": "vless",
    "port": 443,
    "type": "tcp",
    "security": "reality",
    "pbk": "PUBKEY",
    "sid": "6ba85179",
    "sni": "www.example.ru",
    "fp": "chrome",
    "spx": "/",
    "flow_allowed": True,
}
DIRECT = {"type": "field", "outboundTag": "direct", "network": "tcp,udp"}


# ---------------------------------------------------------------------------------------------
# Fakes and data
# ---------------------------------------------------------------------------------------------
class FakeTgPanel(FakePanel):
    """A 3x-ui panel with the Telegram-only capability; ``events`` keeps the order of the calls."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.template = default_template()
        self.sniffing = 0

    async def apply_tg_only(self, emails: Sequence[str]) -> bool:
        self._maybe_fail("apply_tg_only")
        self.events.append(f"rules:{','.join(emails)}")
        self.template, changed = set_tg_only(self.template, emails)
        return changed

    async def get_tg_only(self, emails: Sequence[str]) -> bool:
        return tg_only_enabled(self.template, emails)

    async def ensure_sniffing(self) -> int:
        self.sniffing += 1
        return 0

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> Any:
        self.events.append(f"client:{spec.email}")
        return await super().upsert_account(target, spec)

    async def delete_account(self, target: int | None, email: str, uuid: str) -> None:
        self.events.append(f"delete:{email}")
        await super().delete_account(target, email, uuid)


async def seed(
    sf: SessionFactory,
    *,
    panel_id: int = 1,
    location_id: int = 1,
    flag: str = "🇩🇪",
    address: str = "203.0.113.1",
    kind: PanelKind = PanelKind.XUI,
    active: bool = True,
    params: dict[str, Any] | None = None,
) -> None:
    async with sf.begin() as session:
        if await session.get(Location, location_id) is None:
            session.add(Location(id=location_id, name=f"Loc{location_id}", flag=flag))
        session.add(
            Panel(id=panel_id, name=f"xui{panel_id}", kind=kind, url=f"https://x{panel_id}.example/p", is_active=active)
        )
        await session.flush()
        session.add(
            Inbound(
                id=panel_id,
                panel_id=panel_id,
                remote_id=7,
                location_id=location_id,
                name="in",
                flow="xtls-rprx-vision",
                params=json.dumps(params or REALITY),
            )
        )
        await session.flush()
        session.add(
            Endpoint(
                id=panel_id,
                inbound_id=panel_id,
                kind=EndpointKind.DIRECT,
                profile=EndpointProfile.STANDARD,
                address=address,
            )
        )


class Registry:
    def __init__(self, *panels: tuple[int, FakeTgPanel]) -> None:
        self.backends = dict(panels)

    async def get(self, panel_id: int) -> Any:
        return self.backends[panel_id]


def rule_tags(template: dict[str, Any]) -> list[str | None]:
    return [r.get("ruleTag") or r.get("outboundTag") for r in template["routing"]["rules"]]


def settings(**overrides: Any) -> Settings:
    return make_settings(**overrides)


# ---------------------------------------------------------------------------------------------
# Template rules
# ---------------------------------------------------------------------------------------------
def test_the_data_is_telegrams_published_list() -> None:
    assert "149.154.160.0/20" in TELEGRAM_CIDRS and "2001:b28:f23d::/48" in TELEGRAM_CIDRS
    assert len(TELEGRAM_CIDRS) == 14
    for domain in ("telegram.org", "t.me", "telesco.pe", "tg.dev", "telegra.ph", "cdn-telegram.org"):
        assert f"domain:{domain}" in TELEGRAM_DOMAINS


def test_rules_confine_the_client_and_sit_first() -> None:
    template = default_template()
    template["routing"]["rules"].append(OPERATOR_RULE)
    out, changed = set_tg_only(template, ["tg-only"])
    assert changed
    rules = out["routing"]["rules"]
    assert rules[0]["outboundTag"] == "api"  # the panel's own API rule stays first
    ours = rules[1:4]
    assert [r["ruleTag"] for r in ours] == [TG_ONLY_IP_TAG, TG_ONLY_DOMAIN_TAG, TG_ONLY_BLOCK_TAG]
    assert all(r["user"] == ["tg-only"] for r in ours)
    assert ours[0]["ip"] == list(TELEGRAM_CIDRS) and ours[0]["outboundTag"] == "direct"
    assert ours[1]["domain"] == list(TELEGRAM_DOMAINS) and ours[1]["outboundTag"] == "direct"
    assert ours[2]["outboundTag"] == "blocked" and "ip" not in ours[2] and "domain" not in ours[2]
    assert OPERATOR_RULE in rules[4:]  # operator rules follow, untouched
    assert template["routing"]["rules"] != rules  # the input is not modified


def test_rules_are_idempotent_and_removable() -> None:
    once, _ = set_tg_only(default_template(), ["tg-only"])
    twice, changed = set_tg_only(once, ["tg-only"])
    assert not changed and twice == once
    assert tg_only_enabled(once, ["tg-only"]) and not tg_only_enabled(default_template(), ["tg-only"])
    removed, changed = set_tg_only(once, [])
    assert changed and removed == default_template()
    untouched, changed = set_tg_only(default_template(), [])
    assert not changed and untouched == default_template()
    # A template without a routing section is not rewritten when there is nothing to remove.
    assert set_tg_only({"outbounds": []}, []) == ({"outbounds": []}, False)


def test_a_missing_blackhole_is_created_and_a_broken_template_is_refused() -> None:
    template = default_template()
    template["outbounds"] = [o for o in template["outbounds"] if o["tag"] != "blocked"]
    out, _ = set_tg_only(template, ["tg-only"])
    assert {"tag": "blocked", "protocol": "blackhole", "settings": {}} in out["outbounds"]
    no_freedom = default_template()
    no_freedom["outbounds"] = [{"tag": "blocked", "protocol": "blackhole", "settings": {}}]
    with pytest.raises(TemplateError):
        set_tg_only(no_freedom, ["tg-only"])


YT = YoutubeExit(
    outbound_from_link(
        "vless://11111111-2222-3333-4444-555555555555@exit.example.com:443?type=tcp&security=reality&sni=a.example"
        "&fp=firefox&pbk=K&sid=ab",
        "youtube-exit",
    ),
    ("geosite:youtube",),
)


@pytest.mark.parametrize("tg_first", [True, False])
def test_the_block_stays_ahead_of_ad_blocking_and_the_youtube_exit(tg_first: bool) -> None:
    base = default_template()
    base["routing"]["rules"].append(CATCH_ALL)
    if tg_first:
        template, _ = set_tg_only(base, ["tg-only"])
        template, _ = apply_extras(template, ads=True, youtube=YT)
    else:
        template, _ = apply_extras(base, ads=True, youtube=YT)
        template, _ = set_tg_only(template, ["tg-only"])
    rules = template["routing"]["rules"]
    block = next(i for i, r in enumerate(rules) if r.get("ruleTag") == TG_ONLY_BLOCK_TAG)
    ad = next(i for i, r in enumerate(rules) if r == ad_rule())
    youtube = next(i for i, r in enumerate(rules) if r.get("balancerTag") or r.get("outboundTag") == "youtube-exit")
    # The client's rules come before every rule that could let it reach YouTube or anything else.
    assert block < youtube and rules[1].get("ruleTag") == TG_ONLY_IP_TAG
    assert all(r.get("ruleTag") for r in rules[1 : block + 1])
    assert ad > 0 and rules[-1] == CATCH_ALL
    # Applying everything again changes nothing, in any combination.
    again, changed = apply_extras(template, ads=True, youtube=YT)
    again, changed_tg = set_tg_only(again, ["tg-only"])
    assert not changed and not changed_tg and again == template
    # Removing the extras leaves the Telegram-only block alone, and removing the block leaves the extras.
    plain, _ = apply_extras(template, ads=False, youtube=None)
    assert [r.get("ruleTag") for r in plain["routing"]["rules"]].count(TG_ONLY_BLOCK_TAG) == 1
    no_block, _ = set_tg_only(template, [])
    assert ad_rule() in no_block["routing"]["rules"]
    assert set_ad_blocking(set_youtube_exit(no_block, YT)[0], True)[1] is False


# ---------------------------------------------------------------------------------------------
# Eligible servers
# ---------------------------------------------------------------------------------------------
def test_country_from_flag() -> None:
    assert tg_only.country_of_flag("🇩🇪") == "DE" and tg_only.country_of_flag("x 🇨🇿 y") == "CZ"
    assert tg_only.country_of_flag("🌍") is None and tg_only.country_of_flag("") is None
    assert tg_only.flag_of_country("de") == "🇩🇪" and tg_only.flag_of_country("DEU") is None


async def test_excluded_and_unknown_countries(session_factory: SessionFactory) -> None:
    await seed(session_factory, panel_id=1, location_id=1, flag="🇩🇪")
    await seed(session_factory, panel_id=2, location_id=2, flag="🇷🇺")
    await seed(session_factory, panel_id=3, location_id=3, flag="🌍")
    await seed(session_factory, panel_id=4, location_id=4, flag="🇳🇱", kind=PanelKind.REMNAWAVE)
    await seed(session_factory, panel_id=5, location_id=5, flag="🇳🇱", active=False)
    servers, skipped = await tg_only.load_servers(session_factory, settings())
    assert [s.panel_id for s in servers] == [1] and servers[0].country == "DE" and servers[0].flow == "xtls-rprx-vision"
    assert [(s.panel_id, s.reason) for s in skipped] == [(2, "country"), (3, "unknown_country")]
    # The unknown-country server is offered only when the admin allows it; the deny list is configurable.
    allowed, _ = await tg_only.load_servers(session_factory, settings(tg_only_allow_unknown_country=True))
    assert [s.panel_id for s in allowed] == [1, 3]
    custom, _ = await tg_only.load_servers(session_factory, settings(tg_only_excluded_countries="de, ru"))
    assert [s.panel_id for s in custom] == []
    off, skipped_off = await tg_only.load_servers(session_factory, settings(tg_only_enabled=False))
    assert off == [] and skipped_off == []


async def test_inbounds_without_a_secure_vless_are_not_used(session_factory: SessionFactory) -> None:
    await seed(session_factory, params={**REALITY, "security": "none"})
    servers, skipped = await tg_only.load_servers(session_factory, settings())
    assert servers == [] and skipped[0].reason == "no_inbound"


def test_the_defaults() -> None:
    fresh = Settings(_env_file=None, bot_token="1:T")
    assert fresh.tg_only_enabled and fresh.tg_only_allow_unknown_country is False
    assert {"RU", "BY", "CN", "IR", "KP", "TM"} <= set(fresh.tg_only_excluded_countries)
    assert fresh.tg_proxy_access == "off" and fresh.tg_socks_enabled is False  # the legacy proxies are off
    with pytest.raises(ValueError, match="TG_ONLY_EXCLUDED_COUNTRIES"):
        Settings(_env_file=None, bot_token="1:T", tg_only_excluded_countries="RUS")


# ---------------------------------------------------------------------------------------------
# Convergence of the panels
# ---------------------------------------------------------------------------------------------
async def test_reconcile_applies_rules_before_the_client_and_is_idempotent(session_factory: SessionFactory) -> None:
    await seed(session_factory)
    panel = FakeTgPanel()
    registry = Registry((1, panel))
    assert isinstance(panel, TgOnlyRouting)
    report = await tg_only.reconcile(session_factory, registry, settings())
    assert report.ready == [1] and report.failed == {}
    assert panel.events == ["rules:tg-only", "client:tg-only"]  # never a client without its restrictions
    assert panel.sniffing == 1
    account = panel.accounts[(7, "tg-only")]
    assert account.enabled and account.expires_at is None and account.traffic_limit_bytes == 0
    assert tg_only_enabled(panel.template, ["tg-only"])
    before = copy.deepcopy(panel.template)
    await tg_only.reconcile(session_factory, registry, settings())
    assert panel.template == before and panel.accounts[(7, "tg-only")].uuid == account.uuid  # stable client id
    states = await tg_only.load_states(session_factory, [1])
    assert states[1].ok and states[1].error is None


async def test_a_failing_panel_is_not_offered(session_factory: SessionFactory) -> None:
    await seed(session_factory)
    panel = FakeTgPanel()
    panel.fail_next = 1
    registry = Registry((1, panel))
    report = await tg_only.reconcile(session_factory, registry, settings())
    assert report.ready == [] and 1 in report.failed
    assert "client:tg-only" not in panel.events and (7, "tg-only") not in panel.accounts
    assert await tg_only.render_configs(session_factory, settings(), "ru") is None
    view = await tg_only.admin_view(session_factory, settings())
    assert not view.ready and view.servers[0].error
    await tg_only.reconcile(session_factory, registry, settings())  # recovers on the next run
    assert (await tg_only.admin_view(session_factory, settings())).ready


async def test_a_panel_without_the_capability_is_reported(session_factory: SessionFactory) -> None:
    await seed(session_factory)
    registry = Registry((1, FakePanel()))  # type: ignore[arg-type]
    report = await tg_only.reconcile(session_factory, registry, settings())
    assert 1 in report.failed and "does not support" in report.failed[1]


async def test_a_server_that_stops_being_eligible_loses_client_and_rules(session_factory: SessionFactory) -> None:
    await seed(session_factory)
    panel = FakeTgPanel()
    registry = Registry((1, panel))
    await tg_only.reconcile(session_factory, registry, settings())
    panel.events.clear()
    report = await tg_only.reconcile(session_factory, registry, settings(tg_only_excluded_countries="DE"))
    assert report.removed == [1] and panel.events == ["delete:tg-only", "rules:"]  # client first, then the rules
    assert (7, "tg-only") not in panel.accounts and not any(
        r.get("ruleTag") for r in panel.template["routing"]["rules"]
    )
    assert await tg_only.load_states(session_factory, [1]) == {}
    assert await tg_only.render_configs(session_factory, settings(tg_only_excluded_countries="DE"), "ru") is None


# ---------------------------------------------------------------------------------------------
# The client config
# ---------------------------------------------------------------------------------------------
async def _ready(session_factory: SessionFactory, count: int) -> Registry:
    pairs = []
    for i in range(1, count + 1):
        await seed(session_factory, panel_id=i, location_id=i, address=f"203.0.113.{i}")
        pairs.append((i, FakeTgPanel()))
    registry = Registry(*pairs)
    await tg_only.reconcile(session_factory, registry, settings())
    return registry


async def test_single_server_config(session_factory: SessionFactory) -> None:
    await _ready(session_factory, 1)
    rendered = await tg_only.render_configs(session_factory, settings(), "ru")
    assert rendered is not None and rendered.servers == 1 and len(rendered.configs) == 1
    config = rendered.configs[0]
    assert config["remarks"] == "TestVPN · Telegram"
    proxies = [o for o in config["outbounds"] if o["protocol"] == "vless"]
    assert [o["tag"] for o in proxies] == ["proxy"]
    user = proxies[0]["settings"]["vnext"][0]["users"][0]
    assert proxies[0]["settings"]["vnext"][0]["address"] == "203.0.113.1" and user["flow"] == "xtls-rprx-vision"
    assert proxies[0]["streamSettings"]["realitySettings"]["fingerprint"] == "firefox"  # compact hello
    assert "balancers" not in config["routing"] and "burstObservatory" not in config
    domain, ip, rest = config["routing"]["rules"]
    assert domain["domain"] == list(TELEGRAM_DOMAINS) and domain["outboundTag"] == "proxy"
    assert ip["ip"] == list(TELEGRAM_CIDRS) and ip["outboundTag"] == "proxy"
    assert rest == {"type": "field", "network": "tcp,udp", "outboundTag": "direct"}  # everything else is direct
    assert config["dns"]["servers"] == ["localhost"]
    assert {"tag": "direct", "protocol": "freedom"} in config["outbounds"]


async def test_two_servers_get_a_balancer(session_factory: SessionFactory) -> None:
    await _ready(session_factory, 2)
    rendered = await tg_only.render_configs(session_factory, settings(), "en")
    assert rendered is not None and rendered.servers == 2
    config = rendered.configs[0]
    assert [o["tag"] for o in config["outbounds"] if o["protocol"] == "vless"] == ["proxy-1", "proxy-2"]
    (balancer,) = config["routing"]["balancers"]
    assert balancer["strategy"] == {"type": "leastPing"} and balancer["selector"] == ["proxy-"]
    assert balancer["fallbackTag"] == "proxy-1"
    assert config["burstObservatory"]["subjectSelector"] == ["proxy-"]
    assert "telegram.org" in config["burstObservatory"]["pingConfig"]["destination"]  # a host the server allows
    domain, ip, rest = config["routing"]["rules"]
    assert domain["balancerTag"] == "telegram" and ip["balancerTag"] == "telegram" and rest["outboundTag"] == "direct"
    ids = {o["settings"]["vnext"][0]["users"][0]["id"] for o in config["outbounds"] if o["protocol"] == "vless"}
    assert len(ids) == 2  # every server has its own client id


async def test_unready_servers_are_left_out(session_factory: SessionFactory) -> None:
    registry = await _ready(session_factory, 2)
    registry.backends[2].fail_next = 5
    await tg_only.reconcile(session_factory, registry, settings(tg_only_allow_unknown_country=False))
    # Panel 2 had been ready; a failure keeps the previous state, so it is still offered. A new, never-ready
    # panel is not.
    await seed(session_factory, panel_id=3, location_id=3, address="203.0.113.3")
    rendered = await tg_only.render_configs(session_factory, settings(), "ru")
    assert rendered is not None and rendered.servers == 2


def test_config_builder_is_pure() -> None:
    outbound = outbound_from_link(
        "vless://11111111-2222-3333-4444-555555555555@h.example.com:443?type=tcp&security=reality&sni=a.example"
        "&fp=firefox&pbk=K&sid=ab",
        "proxy-1",
    )
    config = tg_only_config.build_config([outbound], settings(brand_name="MyService"))
    assert config["remarks"] == "MyService · Telegram" and outbound["tag"] == "proxy-1"  # the input keeps its tag
    headers = tg_only_config.response_headers(settings(brand_name="MyService"))
    assert base64.b64decode(headers["profile-title"].removeprefix("base64:")).decode() == "MyService · Telegram"


# ---------------------------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------------------------
async def test_rotation_replaces_token_and_client_ids(session_factory: SessionFactory) -> None:
    await seed(session_factory)
    panel = FakeTgPanel()
    registry = Registry((1, panel))
    await tg_only.reconcile(session_factory, registry, settings())
    old = await tg_only.get_token(session_factory)
    assert await tg_only.get_token(session_factory) == old and len(old) >= 16
    assert await tg_only.token_matches(session_factory, old)
    old_id = panel.accounts[(7, "tg-only")].uuid
    new = await tg_only.rotate(session_factory, registry, settings())
    assert new != old and await tg_only.token_matches(session_factory, new)
    assert not await tg_only.token_matches(session_factory, old)
    assert panel.accounts[(7, "tg-only")].uuid != old_id  # the old client id is revoked on the server
    assert not await tg_only.token_matches(session_factory, "short")
    assert not await tg_only.token_matches(session_factory, "x" * 40)


async def test_a_lookup_never_creates_a_token(session_factory: SessionFactory) -> None:
    assert not await tg_only.token_matches(session_factory, "a" * 32)
    async with session_factory() as session:
        assert await kv.get(session, tg_only.TOKEN_KEY) is None


# ---------------------------------------------------------------------------------------------
# Public URL
# ---------------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    subscription_page.clear_caches()


@pytest.fixture
async def web(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[tuple[ModCtx, AsgiClient, FakeTgPanel]]:
    ctx, fake_api = make_ctx(session_factory, tmp_path, [public_module, tg_only_module])
    ctx.fake_api = fake_api  # type: ignore[attr-defined]
    await seed(session_factory)
    panel = FakeTgPanel()
    ctx.panels.panels[1] = panel
    await tg_only.reconcile(session_factory, Registry((1, panel)), ctx.settings)
    yield ctx, AsgiClient(create_web_app(ctx, [public_module]), host="bot.example.com"), panel  # type: ignore[arg-type]
    await notifications.close(ctx, drain_seconds=1)  # type: ignore[arg-type]


async def test_happ_gets_the_json_and_other_apps_an_explanation(
    web: tuple[ModCtx, AsgiClient, FakeTgPanel], session_factory: SessionFactory
) -> None:
    _ctx, client, _panel = web
    token = await tg_only.get_token(session_factory)
    happ = await client.get(f"/sub/tg/{token}", headers={"user-agent": "Happ/3.2.1"})
    assert happ.status == 200 and happ.headers["content-type"].startswith("application/json")
    assert happ.headers["cache-control"] == "no-store" and happ.headers["profile-title"].startswith("base64:")
    (config,) = json.loads(happ.body)
    assert config["remarks"].endswith("Telegram")
    tun = await client.get(f"/sub/tg/{token}", headers={"user-agent": "v2RayTun/5.0"})
    assert tun.status == 200 and tun.headers["content-type"].startswith("application/json")
    forced = await client.get(f"/sub/tg/{token}?format=json", headers={"user-agent": "curl/8"})
    assert forced.status == 200 and json.loads(forced.body) == [config]
    # Any other app (and a browser) is told what the link is; it never receives server links.
    for agent in ("Hiddify/2.5", "v2rayNG/1.9", "Mozilla/5.0"):
        other = await client.get(f"/sub/tg/{token}", headers={"user-agent": agent})
        assert other.status == 200 and other.headers["content-type"].startswith("text/plain")
        assert "Happ" in other.text and "vless://" not in other.text and "203.0.113.1" not in other.text
    head = await client.request("HEAD", f"/sub/tg/{token}", headers={"user-agent": "Happ/3"})
    assert head.status == 200


async def test_unknown_and_rotated_tokens_are_404(
    web: tuple[ModCtx, AsgiClient, FakeTgPanel], session_factory: SessionFactory
) -> None:
    ctx, client, panel = web
    old = await tg_only.get_token(session_factory)
    assert (await client.get("/sub/tg/" + "z" * 32, headers={"user-agent": "Happ/3"})).status == 404
    assert (await client.get("/sub/tg/bad token", headers={"user-agent": "Happ/3"})).status == 404
    await tg_only.rotate(session_factory, Registry((1, panel)), ctx.settings)
    assert (await client.get(f"/sub/tg/{old}", headers={"user-agent": "Happ/3"})).status == 404
    # A customer's subscription token never opens the Telegram-only URL.
    assert (await client.get("/sub/tg/tok-123", headers={"user-agent": "Happ/3"})).status == 404


async def test_no_ready_server_answers_503(
    web: tuple[ModCtx, AsgiClient, FakeTgPanel], session_factory: SessionFactory
) -> None:
    _ctx, client, _panel = web
    token = await tg_only.get_token(session_factory)
    async with session_factory.begin() as session:
        await kv.delete(session, "tgonly:panel:1")
    resp = await client.get(f"/sub/tg/{token}", headers={"user-agent": "Happ/3"})
    assert resp.status == 503 and resp.headers["retry-after"]


# ---------------------------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------------------------
@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    ctx, engine = await build_ctx(tmp_path)
    ctx.modules = ModuleRegistry([admin_api.module, tg_only_module])
    panel = FakeTgPanel()
    ctx.panels.backends[1] = panel  # type: ignore[assignment]
    await seed(ctx.session_factory)
    app = create_web_app(ctx)  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield await Api(ctx, client).login()
    from app.modules.admin_api.deps import drain_background

    await drain_background()
    await engine.dispose()


async def test_admin_api_shows_rotates_and_syncs(api: Api) -> None:
    first = (await api.get("/tg_only")).json()
    assert first["enabled"] and first["url"].startswith("https://bot.example.com/sub/tg/") and not first["ready"]
    assert first["page"] == "https://bot.example.com/tg"
    assert first["servers"] == [{"panel_id": 1, "title": "🇩🇪 Loc1", "country": "DE", "ready": False, "error": None}]
    assert base64.b64decode(first["qr_png_base64"])[:4] == b"\x89PNG"
    assert (await api.post("/tg_only/sync")).status_code == 200
    synced = (await api.get("/tg_only")).json()
    assert synced["ready"] and synced["url"] == first["url"] and "RU" in synced["excluded_countries"]
    rotated = (await api.post("/tg_only/rotate")).json()
    assert rotated["url"] != first["url"] and rotated["ready"]
    assert not await tg_only.token_matches(api.ctx.session_factory, first["url"].rsplit("/", 1)[1])


async def test_admin_api_is_for_server_managers_only(api: Api) -> None:
    for role, allowed in ((AdminRole.ADMIN, True), (AdminRole.SUPPORT, False), (AdminRole.VIEWER, False)):
        other = await as_role(api, role, tg_id=2000 + len(role.value))
        assert (await other.get("/tg_only")).status_code == (200 if allowed else 403), role
        assert (await other.post("/tg_only/rotate")).status_code == (200 if allowed else 403), role
    anonymous = Api(api.ctx, api.client)
    assert (await anonymous.get("/tg_only")).status_code == 401


# ---------------------------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------------------------
async def _bot(session_factory: SessionFactory) -> Harness:
    h = await make_harness(session_factory, [module("tg_only")])
    h.ctx.panels.backends[1] = FakeTgPanel()
    await seed(session_factory)
    return h


async def test_owner_sees_link_qr_and_rotates(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.tg_only import admin_bot

    shown: list[Any] = []
    real_render = admin_bot.render

    async def spy(event: Any, screen: Any, **kw: Any) -> Any:
        shown.append(screen)
        return await real_render(event, screen, **kw) if screen.photo is None else None

    monkeypatch.setattr(admin_bot, "render", spy)
    h = await _bot(session_factory)
    await h.click(TgOnlyCb(a="home").pack())
    assert "Доступ к Telegram" in h.last() and "/sub/tg/" in h.last() and "⏳" in h.last()
    assert "https://bot.example.com/tg<" in h.last() and "публиковать" in h.last()  # both addresses are shown
    assert "секрет" not in h.last().lower()
    await h.click(h.button("Обновить"))
    assert "✅" in h.last()
    first = h.last()
    await h.click(TgOnlyCb(a="qr").pack())
    assert shown[-1].photo[:4] == b"\x89PNG" and "QR" in shown[-1].text
    await h.click(TgOnlyCb(a="rotate").pack())
    assert "Выпустить новую ссылку" in h.last()
    await h.click(TgOnlyCb(a="rotate_ok").pack())
    assert "/sub/tg/" in h.last() and h.last() != first


async def test_support_staff_cannot_open_it(session_factory: SessionFactory) -> None:
    h = await _bot(session_factory)
    await h.click(TgOnlyCb(a="home").pack(), role=AdminRole.SUPPORT)
    assert not h.screens() or "/sub/tg/" not in h.last()
    await h.click(TgOnlyCb(a="home").pack(), role=None)
    assert not any("/sub/tg/" in text for text in h.screens())
    assert any(item.key == "tg_only" for item in tg_only_module.admin_menu)


async def test_the_screen_explains_excluded_servers(session_factory: SessionFactory) -> None:
    h = await make_harness(session_factory, [module("tg_only")])
    await seed(session_factory, flag="🇷🇺")
    await h.click(TgOnlyCb(a="home").pack())
    assert "Не подходят" in h.last() and "RU" in h.last()


# ---------------------------------------------------------------------------------------------
# One message to the owners
# ---------------------------------------------------------------------------------------------
async def test_owner_gets_the_link_once_after_the_first_ready_server(
    web: tuple[ModCtx, AsgiClient, FakeTgPanel], session_factory: SessionFactory
) -> None:
    ctx, _client, _panel = web
    api = ctx.fake_api  # type: ignore[attr-defined]
    assert await tg_only.pending_owner_notices(session_factory, ctx.settings) == [OWNER]
    await notify_owners(ctx)  # type: ignore[arg-type]
    token = await tg_only.get_token(session_factory)
    sent = [c for c in api.of(SendMessage) if int(c.chat_id) == OWNER and "/sub/tg/" in c.text]
    assert len(sent) == 1 and token in sent[0].text and "Happ" in sent[0].text
    assert "https://bot.example.com/tg<" in sent[0].text and "публиковать" in sent[0].text
    await notify_owners(ctx)  # type: ignore[arg-type]
    await reconcile_job(ctx)  # type: ignore[arg-type]
    assert len([c for c in api.of(SendMessage) if "/sub/tg/" in c.text]) == 1  # never repeated


async def test_a_failed_send_is_retried(
    web: tuple[ModCtx, AsgiClient, FakeTgPanel], session_factory: SessionFactory
) -> None:
    ctx, _client, _panel = web
    api = ctx.fake_api  # type: ignore[attr-defined]
    api.errors[SendMessage] = [OSError("offline")]
    await notify_owners(ctx)  # type: ignore[arg-type]
    assert await tg_only.pending_owner_notices(session_factory, ctx.settings) == [OWNER]
    await notify_owners(ctx)  # type: ignore[arg-type]
    assert await tg_only.pending_owner_notices(session_factory, ctx.settings) == []


async def test_nothing_is_sent_before_a_server_is_ready(session_factory: SessionFactory) -> None:
    await seed(session_factory)
    assert await tg_only.pending_owner_notices(session_factory, settings()) == []
    await tg_only.reconcile(session_factory, Registry((1, FakeTgPanel())), settings())
    assert await tg_only.pending_owner_notices(session_factory, settings()) == [OWNER]
    await tg_only.mark_notified(session_factory, OWNER)
    assert await tg_only.pending_owner_notices(session_factory, settings()) == []
    assert await tg_only.pending_owner_notices(session_factory, settings(tg_only_enabled=False)) == []


# ---------------------------------------------------------------------------------------------
# Panels that cannot do it
# ---------------------------------------------------------------------------------------------
def test_only_3x_ui_backends_support_the_rules() -> None:
    from app.services.panels.remnawave import RemnawaveBackend
    from app.services.panels.xui import XuiBackend

    assert hasattr(XuiBackend, "apply_tg_only") and not hasattr(RemnawaveBackend, "apply_tg_only")
    with pytest.raises(PanelError):
        raise PanelError("x")


# ---------------------------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------------------------
def test_installer_offers_the_telegram_only_link_and_not_the_proxies() -> None:
    from tests.test_install_script import _run

    done = _run()
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "Telegram-only subscription" in out and "Happ or v2RayTun" in out
    assert "tgproxy" not in out and "SOCKS5" not in out and "MTProto" not in out  # not installed by default
    assert "Telegram proxy (port 443)" not in out
    russian = _run("--lang", "ru")
    assert "«только Telegram»" in russian.stdout and "MTProto" not in russian.stdout
    none = _run("--panel", "none")
    assert "Telegram-only" not in none.stdout  # no panel, no subscription


def test_the_legacy_flags_still_work_and_are_marked() -> None:
    from tests.test_install_script import _run

    helped = _run("--help")
    assert "legacy" in helped.stdout and "may not work in some countries" in helped.stdout
    legacy = _run("--with-tgproxy", "--with-socks")
    assert legacy.returncode == 0, legacy.stderr
    assert "petrovich.ru -> tgproxy" in legacy.stdout and "--socks-port 2080" in legacy.stdout


# ---------------------------------------------------------------------------------------------
# Public landing page
# ---------------------------------------------------------------------------------------------
BROWSER = {
    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Safari",
    "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
}


async def test_the_page_guides_a_visitor(
    web: tuple[ModCtx, AsgiClient, FakeTgPanel], session_factory: SessionFactory
) -> None:
    _ctx, client, _panel = web
    token = await tg_only.get_token(session_factory)
    resp = await client.get("/tg", headers=BROWSER)
    assert resp.status == 200 and resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert 'lang="ru"' in html and "TestVPN" in html and "Netoryx" not in html
    assert "Telegram будет работать через это подключение" in html
    assert "apps.apple.com" in html  # the App Store button comes first on an iPhone
    assert "happ://add/" in html and f"https://bot.example.com/sub/tg/{token}" in html
    assert "data:image/png;base64," in html  # the QR code
    assert 'id="copy"' in html
    # Nothing is fetched from elsewhere: no external scripts, styles, images or trackers; strict CSP; not cached.
    assert "<script src" not in html and "<link" not in html and "<iframe" not in html
    assert "default-src 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["cache-control"] == "no-store"
    assert html.count(token) >= 2 and "tok-123" not in html  # only the shared link, nothing of customers


async def test_the_page_follows_the_language_and_the_device(
    web: tuple[ModCtx, AsgiClient, FakeTgPanel],
) -> None:
    _ctx, client, _panel = web
    english = await client.get(
        "/tg",
        headers={
            "user-agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120",
            "accept": "text/html",
            "accept-language": "en-US",
        },
    )
    assert 'lang="en"' in english.text and "Telegram will work through this connection" in english.text
    assert "Happ.apk" in english.text and "Other devices" in english.text
    desktop = await client.get(
        "/tg", headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120", "accept-language": "en"}
    )
    assert "setup-Happ.x64.exe" in desktop.text
    unknown = await client.get("/tg", headers={"user-agent": "curl/8", "accept-language": "en"})
    assert unknown.status == 200 and "Other devices" in unknown.text  # every download is offered


async def test_the_page_address_is_stable_while_the_link_rotates(
    web: tuple[ModCtx, AsgiClient, FakeTgPanel], session_factory: SessionFactory
) -> None:
    ctx, client, panel = web
    old = await tg_only.get_token(session_factory)
    new = await tg_only.rotate(session_factory, Registry((1, panel)), ctx.settings)
    html = (await client.get("/tg", headers=BROWSER)).text
    assert new in html and old not in html


async def test_the_page_is_gone_when_disabled_and_polite_without_a_server(
    web: tuple[ModCtx, AsgiClient, FakeTgPanel], session_factory: SessionFactory
) -> None:
    ctx, client, _panel = web
    async with session_factory.begin() as session:
        await kv.delete(session, "tgonly:panel:1")
    waiting = await client.get("/tg", headers=BROWSER)
    assert waiting.status == 503 and "чуть позже" in waiting.text and "/sub/tg/" not in waiting.text
    ctx.current = ctx.current.model_copy(update={"tg_only_enabled": False})  # type: ignore[attr-defined]
    off = await client.get("/tg", headers=BROWSER)
    assert off.status == 404 and "/sub/tg/" not in off.text


# ---------------------------------------------------------------------------------------------
# Traffic limit and alert
# ---------------------------------------------------------------------------------------------
GIB = 1024**3


async def test_the_monthly_limit_is_applied_without_touching_xray(session_factory: SessionFactory) -> None:
    await seed(session_factory)
    panel = FakeTgPanel()
    registry = Registry((1, panel))
    await tg_only.reconcile(session_factory, registry, settings())
    assert panel.accounts[(7, "tg-only")].traffic_limit_bytes == 0  # unlimited by default
    rules = copy.deepcopy(panel.template)
    await tg_only.reconcile(session_factory, registry, settings(tg_only_monthly_limit_gb=5))
    account = panel.accounts[(7, "tg-only")]
    assert account.traffic_limit_bytes == 5 * GIB and account.enabled
    assert panel.template == rules and panel.events.count("rules:tg-only") == 2  # the idempotent rule check, no change
    assert not any(call[0] == "reset_traffic" for call in panel.calls)  # the first month starts without a reset


async def test_a_new_month_resets_the_counter_and_an_exhausted_client_stays_off(
    session_factory: SessionFactory,
) -> None:
    await seed(session_factory)
    panel = FakeTgPanel()
    registry = Registry((1, panel))
    limited = settings(tg_only_monthly_limit_gb=5)
    await tg_only.reconcile(session_factory, registry, limited)
    panel.accounts[(7, "tg-only")].used_bytes = 6 * GIB
    await tg_only.reconcile(session_factory, registry, limited)
    assert panel.accounts[(7, "tg-only")].enabled is False  # 3x-ui switches it off; we do not switch it back on
    async with session_factory.begin() as session:
        await kv.set(session, "tgonly:reset:1", "2000-01")  # a past month
    await tg_only.reconcile(session_factory, registry, limited)
    assert ("reset_traffic", 7, "tg-only") in panel.calls
    account = panel.accounts[(7, "tg-only")]
    assert account.used_bytes == 0 and account.enabled  # re-enabled after the reset
    calls = len([c for c in panel.calls if c[0] == "reset_traffic"])
    await tg_only.reconcile(session_factory, registry, limited)
    assert len([c for c in panel.calls if c[0] == "reset_traffic"]) == calls  # once per month


async def test_the_daily_alert_fires_once_a_day(session_factory: SessionFactory) -> None:
    from datetime import timedelta

    await seed(session_factory)
    panel = FakeTgPanel()
    registry = Registry((1, panel))
    config = settings(tg_only_daily_alert_gb=10)
    await tg_only.reconcile(session_factory, registry, config)
    now = utcnow()
    assert await tg_only.check_daily_traffic(session_factory, registry, config, now) is None  # baseline stored
    panel.accounts[(7, "tg-only")].used_bytes = 4 * GIB
    assert await tg_only.check_daily_traffic(session_factory, registry, config, now + timedelta(hours=1)) is None
    panel.accounts[(7, "tg-only")].used_bytes = 12 * GIB
    moved = await tg_only.check_daily_traffic(session_factory, registry, config, now + timedelta(hours=2))
    assert moved == 12 * GIB
    # No second alert the same day, however much more flows.
    panel.accounts[(7, "tg-only")].used_bytes = 30 * GIB
    assert await tg_only.check_daily_traffic(session_factory, registry, config, now + timedelta(hours=3)) is None
    # After a day the baseline moves on; a counter reset is not mistaken for traffic.
    panel.accounts[(7, "tg-only")].used_bytes = 0
    assert await tg_only.check_daily_traffic(session_factory, registry, config, now + timedelta(days=2)) is None
    off = settings(tg_only_daily_alert_gb=0)
    panel.accounts[(7, "tg-only")].used_bytes = 500 * GIB
    assert await tg_only.check_daily_traffic(session_factory, registry, off, now + timedelta(days=3)) is None


async def test_the_alert_reaches_the_admins(
    web: tuple[ModCtx, AsgiClient, FakeTgPanel], session_factory: SessionFactory
) -> None:
    from app.modules.tg_only.jobs import alert_on_traffic

    ctx, _client, panel = web
    ctx.panels.panels[1] = panel  # type: ignore[attr-defined]
    ctx.current = ctx.current.model_copy(update={"tg_only_daily_alert_gb": 1})  # type: ignore[attr-defined]
    await alert_on_traffic(ctx)  # type: ignore[arg-type]
    panel.accounts[(7, "tg-only")].used_bytes = 3 * GIB
    await alert_on_traffic(ctx)  # type: ignore[arg-type]
    await alert_on_traffic(ctx)  # type: ignore[arg-type]
    await notifications.close(ctx, drain_seconds=1)  # type: ignore[arg-type]
    api = ctx.fake_api  # type: ignore[attr-defined]
    alerts = [c for c in api.of(SendMessage) if "Telegram" in c.text and "3.0" in c.text]
    assert len(alerts) == 1


def test_the_new_settings() -> None:
    fresh = Settings(_env_file=None, bot_token="1:T")
    assert fresh.tg_only_monthly_limit_gb == 0 and fresh.tg_only_daily_alert_gb == 50
    with pytest.raises(ValueError, match="TG_ONLY_MONTHLY_LIMIT_GB"):
        Settings(_env_file=None, bot_token="1:T", tg_only_monthly_limit_gb=-1)
