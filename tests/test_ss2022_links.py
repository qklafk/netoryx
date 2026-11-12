"""Shadowsocks-2022: keys, ``ss://`` links (byte-exact against 3x-ui), inbound parsing and inventory import rules."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote

import pytest
from sqlalchemy import select

from app.db.models import Endpoint, EndpointKind, EndpointProfile, Inbound, Panel, PanelKind
from app.db.session import SessionFactory
from app.services import links
from app.services.panels import inventory, shadowsocks
from app.services.panels.base import InboundInfo
from app.services.panels.xui import build_shadowsocks_inbound, parse_inbound
from tests.fakes import FakePanel, default_inbound
from tests.test_xui_inventory import StubRegistry, add_panel

UUID = "6f1c1c2e-8d1f-4b6c-9a51-3e0e2c4d5f60"
AES128, AES256, CHACHA = (
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
)
SERVER_KEYS = {
    AES128: base64.b64encode(bytes(range(16))).decode(),
    AES256: base64.b64encode(bytes(range(32))).decode(),
    CHACHA: base64.b64encode(bytes(range(32))).decode(),
}


def ss_raw(
    method: str = AES128,
    *,
    password: str | None = None,
    stream: dict[str, Any] | None = None,
    enable: bool = True,
    remote_id: int = 5,
) -> dict[str, Any]:
    """One ``inbounds/list`` entry as 3x-ui v3.11 returns it."""
    return {
        "id": remote_id,
        "remark": "ss",
        "enable": enable,
        "port": 8388,
        "protocol": "shadowsocks",
        "settings": {
            "method": method,
            "password": SERVER_KEYS.get(method, "x") if password is None else password,
            "network": "tcp,udp",
            "clients": [],
            "ivCheck": False,
        },
        "streamSettings": stream or {"network": "tcp", "security": "none", "tcpSettings": {"header": {"type": "none"}}},
    }


# ---------------------------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("method", "size"), [(AES128, 16), (AES256, 32), (CHACHA, 32)])
def test_client_keys_are_base64_of_the_method_key_size(method: str, size: int) -> None:
    key = shadowsocks.derive_client_key(UUID, 7, method)
    assert len(base64.b64decode(key, validate=True)) == size
    assert shadowsocks.key_bytes(method) == size
    assert len(base64.b64decode(shadowsocks.generate_key(method), validate=True)) == size


def test_client_keys_are_stable_and_per_subscription_and_inbound() -> None:
    key = shadowsocks.derive_client_key(UUID, 7, AES128)
    assert key == "si3X9nMpNys62IcjnrIffA=="  # fixed vector: a change here would re-key every user
    assert key == shadowsocks.derive_client_key(UUID, 7, AES128)
    assert key != shadowsocks.derive_client_key(UUID, 8, AES128)
    assert key != shadowsocks.derive_client_key(UUID.replace("6f", "6e", 1), 7, AES128)
    assert shadowsocks.derive_client_key(UUID, 7, AES256) != key + key  # not just a longer copy of the short key
    assert not shadowsocks.derive_client_key(UUID, 7, AES256).startswith(key[:-2])


def test_derive_rejects_other_methods() -> None:
    with pytest.raises(ValueError):
        shadowsocks.derive_client_key(UUID, 1, "aes-256-gcm")
    with pytest.raises(ValueError):
        shadowsocks.generate_key("aes-256-gcm")


# ---------------------------------------------------------------------------------------------
# Links (3x-ui v3.11 genShadowsocksLink: unencoded userinfo, url.QueryEscape per part)
# ---------------------------------------------------------------------------------------------
def test_link_aes128_matches_3xui() -> None:
    client = shadowsocks.derive_client_key(UUID, 7, AES128)
    link = links.build_ss(AES128, SERVER_KEYS[AES128], client, "203.0.113.1", 8388, {"type": "tcp"}, "ss")
    assert link == (
        "ss://2022-blake3-aes-128-gcm:AAECAwQFBgcICQoLDA0ODw%3D%3D:si3X9nMpNys62IcjnrIffA%3D%3D"
        "@203.0.113.1:8388?type=tcp#ss"
    )


def test_link_aes256_escapes_slash_plus_and_padding() -> None:
    client = shadowsocks.derive_client_key(UUID, 7, AES256)
    assert client == "jSGPOxT1xiCaEgk5Nb0G//nBKC+1svqvDZMjDNdZEgY="
    link = links.build_ss(AES256, SERVER_KEYS[AES256], client, "ss.example.com", 443, {"type": "tcp"}, "x")
    assert link == (
        "ss://2022-blake3-aes-256-gcm:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8%3D"
        ":jSGPOxT1xiCaEgk5Nb0G%2F%2FnBKC%2B1svqvDZMjDNdZEgY%3D@ss.example.com:443?type=tcp#x"
    )


def test_link_chacha_method_has_the_same_shape() -> None:
    link = links.build_ss(CHACHA, SERVER_KEYS[CHACHA], "Y2xpZW50", "h.example", 1, {"type": "tcp"}, "n")
    assert link.startswith(
        "ss://2022-blake3-chacha20-poly1305:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8%3D:Y2xpZW50@"
    )


def test_link_remark_description_and_ipv6() -> None:
    remark = "🇩🇪 Германия · SS"
    link = links.build_ss(AES128, "a", "b", "2001:db8::1", 8388, {"type": "tcp"}, remark, "Fast")
    assert link == (
        f"ss://2022-blake3-aes-128-gcm:a:b@[2001:db8::1]:8388?type=tcp#{quote(remark, safe='')}"
        f"?serverDescription={base64.b64encode(b'Fast').decode()}"
    )


def test_link_query_follows_the_transport_and_never_carries_vless_fields() -> None:
    params = {
        "type": "tcp",
        "encryption": "none",
        "security": "none",
        "flow_allowed": True,
        "flow": "xtls-rprx-vision",
        "sni": "leftover.example",
        "fp": "chrome",
        shadowsocks.PARAM_METHOD: AES128,
        shadowsocks.PARAM_PASSWORD: "k",
        "protocol": "shadowsocks",
        "port": 8388,
    }
    assert links.ss_link_query(params) == {"type": "tcp"}
    tls = {
        "type": "tcp",
        "security": "tls",
        "sni": "ss.sni",
        "fp": "chrome",
        "alpn": ["h2", "http/1.1"],
        "pbk": "stale",
    }
    assert links.ss_link_query(tls) == {
        "type": "tcp",
        "security": "tls",
        "sni": "ss.sni",
        "fp": "chrome",
        "alpn": "h2,http/1.1",
    }
    link = links.build_ss(AES128, "a", "b", "h", 1, tls, "n")
    assert "?alpn=h2%2Chttp%2F1.1&fp=chrome&security=tls&sni=ss.sni&type=tcp#n" in link


def test_http_obfuscation_becomes_a_plugin_like_3xui() -> None:
    params = {"type": "tcp", "headerType": "http", "host": "test", "path": "/"}
    assert links.ss_link_query(params) == {"plugin": "obfs-local;obfs=http;obfs-host=test"}
    link = links.build_ss(AES128, "a", "b", "h", 1, params, "n")
    assert link.endswith("@h:1?plugin=obfs-local%3Bobfs%3Dhttp%3Bobfs-host%3Dtest#n")


def test_ws_transport_keeps_xray_native_fields() -> None:
    params = {"type": "ws", "path": "/ws", "host": "cdn.example", "security": "none"}
    assert links.ss_link_query(params) == {"type": "ws", "path": "/ws", "host": "cdn.example"}


def _endpoint(kind: EndpointKind = EndpointKind.DIRECT, **kw: Any) -> Endpoint:
    values: dict[str, Any] = {
        "id": 1,
        "kind": kind,
        "profile": EndpointProfile.STANDARD,
        "address": "198.51.100.7",
        "port": None,
    }
    values.update(kw)
    return Endpoint(**values)


def test_endpoint_only_changes_address_and_port_for_shadowsocks() -> None:
    base = {
        "protocol": "shadowsocks",
        "port": 8388,
        "type": "tcp",
        "security": "none",
        shadowsocks.PARAM_METHOD: AES128,
    }
    cdn = _endpoint(EndpointKind.CDN, port=2053, overrides=json.dumps({"security": "tls", "sni": "x.example"}))
    address, port, params = links.effective_ss(base, cdn)
    assert (address, port) == ("198.51.100.7", 2053)
    assert params == {"type": "tcp", "security": "none"}  # no SNI / security switch for Shadowsocks
    assert links.effective_ss(base, _endpoint())[1] == 8388
    with pytest.raises(ValueError):
        links.effective_ss(base, _endpoint(address=" "))


def test_entry_name_protocol_tag_survives_truncation() -> None:
    from tests.conftest import make_settings

    settings = make_settings()
    endpoint = _endpoint(remark="A very long custom server name that overflows")
    name = links.entry_name(None, endpoint, settings, protocol_tag=links.SS_SUFFIX)
    assert name.endswith(" · SS") and len(name) <= links.REMARK_MAX_LEN
    assert links.entry_name(None, endpoint, settings, ordinal=2, protocol_tag=links.SS_SUFFIX).endswith(" · SS 2")


# ---------------------------------------------------------------------------------------------
# parse_inbound
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("method", [AES128, AES256])
def test_multi_user_2022_inbound_is_supported(method: str) -> None:
    info = parse_inbound(ss_raw(method))
    assert (info.protocol, info.port, info.enabled) == ("shadowsocks", 8388, True)
    assert info.params[shadowsocks.PARAM_METHOD] == method
    assert info.params[shadowsocks.PARAM_PASSWORD] == SERVER_KEYS[method]
    assert info.params["type"] == "tcp" and "encryption" not in info.params and "flow_allowed" not in info.params
    assert shadowsocks.PARAM_UNSUPPORTED not in info.params


def test_legacy_panels_return_settings_as_json_strings() -> None:
    raw = ss_raw()
    raw["settings"] = json.dumps(raw["settings"])
    raw["streamSettings"] = json.dumps(raw["streamSettings"])
    assert parse_inbound(raw).params[shadowsocks.PARAM_METHOD] == AES128


@pytest.mark.parametrize(
    ("method", "needle"),
    [
        ("aes-256-gcm", "deprecated"),
        ("chacha20-ietf-poly1305", "deprecated"),
        ("xchacha20-ietf-poly1305", "deprecated"),
        ("", "deprecated"),
        (CHACHA, "several users"),
        ("2022-blake3-aes-512-gcm", "unknown"),
    ],
)
def test_other_methods_are_unsupported_with_a_reason(method: str, needle: str) -> None:
    reason = parse_inbound(ss_raw(method)).params[shadowsocks.PARAM_UNSUPPORTED]
    assert needle in reason


def test_unsupported_transport_and_missing_server_key() -> None:
    kcp = parse_inbound(ss_raw(stream={"network": "kcp", "security": "none"}))
    assert "transport" in kcp.params[shadowsocks.PARAM_UNSUPPORTED]
    assert "server key" in parse_inbound(ss_raw(password="")).params[shadowsocks.PARAM_UNSUPPORTED]


def test_blocked_reason_also_judges_params_without_the_flag() -> None:
    assert shadowsocks.blocked_reason({"protocol": "vless"}) is None
    assert shadowsocks.blocked_reason({"protocol": "shadowsocks", "ss_method": AES128, "ss_password": "k"}) is None
    assert shadowsocks.blocked_reason({"protocol": "shadowsocks", "ss_method": "aes-256-gcm", "ss_password": "k"})
    assert shadowsocks.blocked_reason({"protocol": "shadowsocks", "ss_method": AES128})


def test_client_identity_is_for_shadowsocks_only() -> None:
    params = {"protocol": "shadowsocks", "ss_method": AES128}
    assert shadowsocks.client_identity(params, UUID, 7) == shadowsocks.derive_client_key(UUID, 7, AES128)
    assert shadowsocks.client_identity({"protocol": "vless"}, UUID, 7) is None
    assert shadowsocks.client_identity({"protocol": "shadowsocks", "ss_method": "aes-256-gcm"}, UUID, 7) is None


# ---------------------------------------------------------------------------------------------
# New inbound body
# ---------------------------------------------------------------------------------------------
def test_new_inbound_body() -> None:
    body = build_shadowsocks_inbound(8388, method=AES256, remark="ss-main")
    assert (body["protocol"], body["port"], body["remark"]) == ("shadowsocks", 8388, "ss-main")
    settings = json.loads(body["settings"])
    assert settings["method"] == AES256 and settings["clients"] == []
    assert len(base64.b64decode(settings["password"], validate=True)) == 32
    assert json.loads(body["streamSettings"])["network"] == "tcp"
    assert build_shadowsocks_inbound(1)["remark"] == "ss-1"


@pytest.mark.parametrize(
    ("kwargs", "port"),
    [({"method": CHACHA}, 1), ({"method": "aes-256-gcm"}, 1), ({}, 0), ({}, 70000), ({"server_key": "AAAA"}, 1)],
)
def test_new_inbound_rejects_bad_input(kwargs: dict[str, Any], port: int) -> None:
    with pytest.raises(ValueError):
        build_shadowsocks_inbound(port, **kwargs)


# ---------------------------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------------------------
def ss_info(remote_id: int, method: str = AES128, *, network: str = "tcp", enabled: bool = True) -> InboundInfo:
    raw = ss_raw(method, remote_id=remote_id, enable=enabled)
    raw["streamSettings"] = {"network": network, "security": "none"}
    return parse_inbound(raw)


async def rows(session_factory: SessionFactory) -> dict[int, Inbound]:
    async with session_factory() as session:
        return {r.remote_id: r for r in await session.scalars(select(Inbound))}


async def test_import_never_activates_shadowsocks_inbounds(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[default_inbound(1, 443), ss_info(2), ss_info(3, "aes-256-gcm"), ss_info(4, CHACHA)])
    result = await inventory.refresh_inbounds(
        session_factory, StubRegistry({panel_id: fake}), panel_id, activate_new=True, endpoint_address="203.0.113.5"
    )
    by_id = await rows(session_factory)
    assert {rid: row.is_active for rid, row in by_id.items()} == {1: True, 2: False, 3: False, 4: False}
    assert result.shadowsocks == [by_id[2].id, by_id[3].id, by_id[4].id]
    assert inventory.is_shadowsocks_inbound(by_id[2]) and not inventory.is_shadowsocks_inbound(by_id[1])
    assert inventory.shadowsocks_block_reason(by_id[2]) is None
    assert "deprecated" in (inventory.shadowsocks_block_reason(by_id[3]) or "")
    assert "several users" in (inventory.shadowsocks_block_reason(by_id[4]) or "")
    assert by_id[2].flow == ""
    params = json.loads(by_id[2].params or "")
    assert params["protocol"] == "shadowsocks" and params["port"] == 8388 and params[shadowsocks.PARAM_METHOD] == AES128
    async with session_factory() as session:
        endpoints = (await session.scalars(select(Endpoint))).all()
    assert len(endpoints) == 4  # an endpoint exists for the operator to switch on


async def test_unsupported_transport_is_imported_inactive(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[ss_info(2, network="kcp")])
    await inventory.refresh_inbounds(session_factory, StubRegistry({panel_id: fake}), panel_id)
    (row,) = (await rows(session_factory)).values()
    assert not row.is_active and "transport" in (inventory.shadowsocks_block_reason(row) or "")


async def test_refresh_deactivates_an_inbound_switched_to_a_deprecated_method(session_factory: SessionFactory) -> None:
    panel_id = await add_panel(session_factory)
    fake = FakePanel(inbounds=[ss_info(2)])
    registry = StubRegistry({panel_id: fake})
    await inventory.refresh_inbounds(session_factory, registry, panel_id)
    async with session_factory.begin() as session:
        row = await session.scalar(select(Inbound))
        assert row is not None
        row.is_active = True  # the operator turned it on
    fake.inbounds = [ss_info(2)]
    assert (await inventory.refresh_inbounds(session_factory, registry, panel_id)).deactivated == []
    fake.inbounds = [ss_info(2, "aes-256-gcm")]
    result = await inventory.refresh_inbounds(session_factory, registry, panel_id)
    (row,) = (await rows(session_factory)).values()
    assert result.deactivated == [row.id] and not row.is_active


async def test_bootstrap_counts_only_vpn_inbounds(session_factory: SessionFactory) -> None:
    from app.utils.crypto import SecretBox
    from tests.conftest import make_settings

    settings = make_settings(xui_host="https://panel.example:2053/p", xui_username="a", xui_password="b")
    fake = FakePanel(inbounds=[ss_info(2)])
    result = await inventory.bootstrap_from_env(
        session_factory, settings, SecretBox("test-secret-key-0123456789abcdef"), StubRegistry(default=fake)
    )
    assert result is not None and result.inbounds_imported == 0 and "no VLESS inbounds" in result.errors[0]
    async with session_factory() as session:
        assert await session.scalar(select(Panel.id).where(Panel.kind == PanelKind.XUI)) is not None
