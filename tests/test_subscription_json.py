"""Xray JSON subscription: config shape, balancer, routing order, client detection, placeholders."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Any

import pytest

from app.services import subscription_json as sj
from app.services import subscription_page as sp
from app.services.links import build_ss, build_vless, compact_reality_hello
from tests.conftest import make_settings
from tests.test_links_subscription_page import NOW, REALITY_PARAMS, World, world  # noqa: F401

UUID = "6f1c1c2e-8d1f-4b6c-9a51-3e0e2c4d5f60"
HAPP = {"User-Agent": "Happ/3.7.0/ios"}
RAYTUN = {"User-Agent": "v2RayTun/5.1 (iOS)"}


def vless(host: str, name: str, **extra: Any) -> str:
    params = {**compact_reality_hello(REALITY_PARAMS), **extra}
    params.pop("protocol", None)
    return build_vless(UUID, host, 443, params, name, None, "xtls-rprx-vision")


def ss(host: str, name: str) -> str:
    key = "MDEyMzQ1Njc4OWFiY2RlZg=="
    return build_ss("2022-blake3-aes-128-gcm", key, key, host, 8388, {"type": "tcp"}, name)


def build(links: list[str], **overrides: Any) -> list[dict[str, Any]]:
    return sj.build_configs(links, make_settings(**overrides), "ru")


def rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    return config["routing"]["rules"]  # type: ignore[no-any-return]


def test_single_server_has_no_auto_and_valid_shape() -> None:
    configs = build([vless("203.0.113.1", "🇩🇪 Германия")])
    assert len(configs) == 1
    config = json.loads(sj.render_body(configs))[0]
    assert config["remarks"] == "🇩🇪 Германия"
    assert {"log", "dns", "inbounds", "outbounds", "routing"} <= config.keys()
    assert [(i["protocol"], i["port"], i["listen"]) for i in config["inbounds"]] == [
        ("socks", 10808, "127.0.0.1"),
        ("http", 10809, "127.0.0.1"),
    ]
    for inbound in config["inbounds"]:
        assert inbound["sniffing"] == {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}
    assert [o["tag"] for o in config["outbounds"]] == ["proxy", "direct", "block"]
    assert config["routing"]["domainStrategy"] == "IPIfNonMatch"
    assert "balancers" not in config["routing"] and "burstObservatory" not in config


def test_reality_parameters_and_compact_hello_are_kept() -> None:
    outbound = build([vless("203.0.113.1", "x")])[0]["outbounds"][0]
    user = outbound["settings"]["vnext"][0]["users"][0]
    assert user["id"] == UUID and user["flow"] == "xtls-rprx-vision"
    stream = outbound["streamSettings"]
    assert stream["security"] == "reality"
    reality = stream["realitySettings"]
    assert reality["fingerprint"] == "firefox"
    assert reality["publicKey"] == "PUBKEY" and reality["shortId"] == "6ba85179"
    assert reality["serverName"] == "www.example.ru" and reality["spiderX"] == "/a1b2c3"


def test_auto_config_with_balancer_first() -> None:
    configs = build([vless("203.0.113.1", "A"), ss("203.0.113.2", "B"), vless("203.0.113.3", "C")])
    assert [c["remarks"] for c in configs] == ["TestVPN · Авто", "A", "B", "C"]
    auto = configs[0]
    assert [o["tag"] for o in auto["outbounds"][:3]] == ["proxy-1", "proxy-2", "proxy-3"]
    balancer = auto["routing"]["balancers"][0]
    assert balancer["strategy"] == {"type": "leastPing"}
    assert balancer["fallbackTag"] == "proxy-1" and balancer["selector"] == ["proxy-"]
    ping = auto["burstObservatory"]["pingConfig"]
    assert ping["destination"].endswith("generate_204") and ping["timeout"] == "5s"
    assert rules(auto)[-1] == {"type": "field", "network": "tcp,udp", "balancerTag": "auto"}
    # Per-server configs stay plain, with the Shadowsocks entry expressed as a Shadowsocks outbound.
    assert configs[2]["outbounds"][0]["protocol"] == "shadowsocks"
    assert "balancers" not in configs[1]["routing"]


def test_auto_remark_language_and_switch() -> None:
    links = [vless("203.0.113.1", "A"), vless("203.0.113.2", "B")]
    assert sj.build_configs(links, make_settings(), "en")[0]["remarks"] == "TestVPN · Auto"
    assert [c["remarks"] for c in build(links, sub_json_auto=False)] == ["A", "B"]


def test_rule_order_and_overrides() -> None:
    config = build(
        [vless("203.0.113.1", "A")],
        sub_json_direct_domains="corp.example\nfull:intranet.example",
        sub_json_proxy_domains="blocked.ru, domain:other.su",
    )[0]
    kinds = []
    for rule in rules(config):
        if "inboundTag" in rule:
            kinds.append("dns")
        elif "ip" in rule:
            kinds.append("private")
        elif "domain" in rule:
            kinds.append("proxy-domains" if rule["outboundTag"] == "proxy" else "direct-domains")
        else:
            kinds.append("final")
    assert kinds == ["dns", "dns", "proxy-domains", "private", "direct-domains", "final"]
    by_kind = dict(zip(kinds[2:], rules(config)[2:], strict=True))
    assert by_kind["proxy-domains"]["domain"] == ["domain:blocked.ru", "domain:other.su"]
    direct = by_kind["direct-domains"]["domain"]
    assert {"domain:ru", "domain:su", "domain:xn--p1ai", "domain:corp.example", "full:intranet.example"} <= set(direct)
    assert "10.0.0.0/8" in by_kind["private"]["ip"] and "192.168.0.0/16" in by_kind["private"]["ip"]
    assert rules(config)[-1]["outboundTag"] == "proxy"
    servers = config["dns"]["servers"]
    assert servers[-1] == "1.1.1.1"
    assert any(isinstance(s, dict) and s["address"] == "77.88.8.8" for s in servers)
    assert config["dns"]["queryStrategy"] == "UseIPv4"


def test_direct_ru_off_has_no_direct_domain_rules_and_one_dns() -> None:
    config = build([vless("203.0.113.1", "A")], sub_json_direct_ru=False, sub_json_proxy_domains="x.example")[0]
    assert all("domain" not in r for r in rules(config))
    assert config["dns"]["servers"] == ["1.1.1.1"]
    assert [r.get("outboundTag") for r in rules(config)] == ["proxy", "direct", "proxy"]


def test_server_hostname_resolved_locally() -> None:
    config = build([vless("vpn.example.org", "A")])[0]
    assert config["dns"]["servers"][0] == {"address": "localhost", "domains": ["full:vpn.example.org"]}


def test_unsupported_links_are_skipped_and_description_kept() -> None:
    odd = vless("203.0.113.9", "odd", type="kcp")
    described = build_vless(UUID, "203.0.113.1", 443, {"type": "tcp", "security": "none"}, "A", "hello")
    configs = build([odd, described])
    assert [c["remarks"] for c in configs] == ["A"]
    assert configs[0]["meta"] == {"serverDescription": "hello"}


# ---------------------------------------------------------------------------------------------
# Rendering through the subscription page
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("headers", [HAPP, RAYTUN])
async def test_json_for_happ_and_v2raytun(world: World, headers: dict[str, str]) -> None:  # noqa: F811
    result = await world.render(headers=headers)
    assert result.media_type.startswith("application/json")
    configs = json.loads(result.body)
    assert configs[0]["remarks"] == "TestVPN · Авто"
    assert len(configs) == len(result.links) + 1
    assert [c["remarks"] for c in configs[1:]] == sp_names(result.links)
    assert "subscription-userinfo" in result.headers and "profile-title" in result.headers


def sp_names(links: list[str]) -> list[str]:
    return [sj._remark(link)[0] for link in links]


@pytest.mark.parametrize("headers", [None, {"User-Agent": "Hiddify/2.5"}, {"User-Agent": "INCY/2.7.0"}])
async def test_other_clients_keep_the_link_list(world: World, headers: dict[str, str] | None) -> None:  # noqa: F811
    result = await world.render(headers=headers)
    assert result.media_type == sp.MEDIA_TYPE
    assert sp.base64.b64decode(result.body).decode().split("\n") == result.links


async def test_json_disabled_gives_old_format_to_everyone(world: World) -> None:  # noqa: F811
    result = await world.render(headers=HAPP, settings=make_settings(sub_json_enabled=False))
    assert result.media_type == sp.MEDIA_TYPE
    assert sp.base64.b64decode(result.body).decode().split("\n") == result.links


async def test_expired_subscription_is_a_single_placeholder_config(world: World) -> None:  # noqa: F811
    result = await world.render(headers=HAPP, now=NOW + timedelta(days=30))
    configs = json.loads(result.body)
    assert [c["remarks"] for c in configs] == ["⛔ Подписка истекла — продлите в боте"]
    assert configs[0]["outbounds"][0]["settings"]["vnext"][0]["address"] == "0.0.0.0"
    assert result.state == sp.AccessState.EXPIRED and "announce" in result.headers


async def test_per_server_selection_has_no_auto(world: World) -> None:  # noqa: F811
    full = await world.render(headers=HAPP)
    one = sp.server_key(full.links[0])
    from app.services import subscription_page

    result = await subscription_page.render(
        world.session_factory,
        world.registry,
        world.settings,
        "tok-123",
        client_headers=HAPP,
        now=NOW,
        server_id=one,
    )
    configs = json.loads(result.body)
    assert len(configs) == 1 and "balancers" not in configs[0]["routing"]


def test_example_config_for_external_validation() -> None:
    configs = build([vless("203.0.113.10", "🇩🇪 Германия"), ss("203.0.113.20", "🇳🇱 Нидерланды · SS")])
    target = os.environ.get("JSON_SUB_EXAMPLE_PATH")
    if target:
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(configs[0], fh, ensure_ascii=False, indent=2)
    assert configs[0]["remarks"].endswith("Авто")
