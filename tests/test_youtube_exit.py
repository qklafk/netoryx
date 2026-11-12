"""YouTube exit: share link parsing, template editing next to ad blocking, 3x-ui backend, convergence, settings."""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.core.settings_registry import REGISTRY
from app.db.models import PanelKind
from app.db.session import SessionFactory
from app.services import ad_blocking
from app.services.panels.base import TemplateExtras
from app.services.panels.xray_template import (
    YOUTUBE_BALANCER_TAG,
    YOUTUBE_TAG,
    TemplateError,
    YoutubeExit,
    ad_rule,
    apply_extras,
    set_ad_blocking,
    set_youtube_exit,
    youtube_exit_enabled,
)
from app.services.panels.xui import XuiBackend, sniffing_update
from app.utils.xray_outbound import LinkError, outbound_from_link
from tests.test_adblock_blocking import (
    CATCH_ALL,
    OPERATOR_RULE,
    TOKEN,
    FakeXray,
    Registry,
    _add_panel,
    default_template,
)

REALITY = (
    "vless://11111111-2222-3333-4444-555555555555@exit.example.com:443?type=tcp&security=reality&sni=www.example.com"
    "&fp=firefox&pbk=PUBKEY&sid=ab12&spx=%2F&flow=xtls-rprx-vision&encryption=none#exit"
)
DOMAINS = ("geosite:youtube", "domain:googlevideo.com")


def wanted(link: str = REALITY) -> YoutubeExit:
    return YoutubeExit(outbound_from_link(link, YOUTUBE_TAG), DOMAINS)


# ---------------------------------------------------------------------------------------------
# Link parsing
# ---------------------------------------------------------------------------------------------
def test_vless_reality_tcp_with_flow() -> None:
    out = outbound_from_link(REALITY, YOUTUBE_TAG)
    assert out["tag"] == YOUTUBE_TAG and out["protocol"] == "vless"
    (vnext,) = out["settings"]["vnext"]
    assert vnext["address"] == "exit.example.com" and vnext["port"] == 443
    assert vnext["users"] == [
        {"id": "11111111-2222-3333-4444-555555555555", "encryption": "none", "flow": "xtls-rprx-vision"}
    ]
    assert out["streamSettings"] == {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
            "serverName": "www.example.com",
            "fingerprint": "firefox",
            "publicKey": "PUBKEY",
            "shortId": "ab12",
            "spiderX": "/",
        },
    }


def test_vless_tls_ws_grpc_xhttp_httpupgrade() -> None:
    ws = outbound_from_link(
        "vless://u@h.example.com:8443?type=ws&security=tls&sni=h.example.com&alpn=h2,http/1.1&path=/p&host=cdn.x&fp=chrome",
        YOUTUBE_TAG,
    )["streamSettings"]
    assert ws["network"] == "ws" and ws["wsSettings"] == {"path": "/p", "headers": {"Host": "cdn.x"}}
    assert ws["tlsSettings"] == {"serverName": "h.example.com", "fingerprint": "chrome", "alpn": ["h2", "http/1.1"]}

    grpc = outbound_from_link("vless://u@h:443?type=grpc&security=tls&serviceName=svc&mode=multi", YOUTUBE_TAG)
    assert grpc["streamSettings"]["grpcSettings"] == {"serviceName": "svc", "multiMode": True}

    extra = json.dumps({"xPaddingBytes": "100-1000"})
    xhttp = outbound_from_link(
        f"vless://u@h:443?type=xhttp&security=tls&path=/x&mode=packet-up&extra={extra}", YOUTUBE_TAG
    )["streamSettings"]
    assert xhttp["xhttpSettings"] == {"path": "/x", "mode": "packet-up", "extra": {"xPaddingBytes": "100-1000"}}

    up = outbound_from_link("vless://u@h:443?type=httpupgrade&security=tls&path=/u&host=z", YOUTUBE_TAG)
    assert up["streamSettings"]["httpupgradeSettings"] == {"path": "/u", "host": "z"}


def test_socks_links() -> None:
    out = outbound_from_link("socks5://us%40er:p%3Aw@10.0.0.1:1080#x", YOUTUBE_TAG)
    assert out["protocol"] == "socks"
    assert out["settings"]["servers"] == [
        {"address": "10.0.0.1", "port": 1080, "users": [{"user": "us@er", "pass": "p:w"}]}
    ]
    b64 = base64.urlsafe_b64encode(b"bob:secret").decode().rstrip("=")
    out = outbound_from_link(f"socks://{b64}@[2001:db8::1]:1081", YOUTUBE_TAG)
    assert out["settings"]["servers"][0] == {
        "address": "2001:db8::1",
        "port": 1081,
        "users": [{"user": "bob", "pass": "secret"}],
    }
    anon = outbound_from_link("socks5://h.example.com:1080", YOUTUBE_TAG)
    assert "users" not in anon["settings"]["servers"][0]


def test_shadowsocks_links() -> None:
    userinfo = base64.urlsafe_b64encode(b"chacha20-ietf-poly1305:pw").decode().rstrip("=")
    out = outbound_from_link(f"ss://{userinfo}@h.example.com:8388#n", YOUTUBE_TAG)
    expected = {"address": "h.example.com", "port": 8388, "method": "chacha20-ietf-poly1305", "password": "pw"}
    assert out["protocol"] == "shadowsocks" and out["settings"]["servers"] == [expected]
    legacy = base64.b64encode(b"chacha20-ietf-poly1305:pw@h.example.com:8388").decode()
    assert outbound_from_link(f"ss://{legacy}", YOUTUBE_TAG)["settings"]["servers"] == [expected]
    plain = outbound_from_link("ss://2022-blake3-aes-128-gcm:a%2Bb@h:1", YOUTUBE_TAG)
    assert plain["settings"]["servers"][0]["password"] == "a+b"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "https://example.com",
        "not a link",
        "vless://@h:443?type=tcp",
        "vless://u@:443",
        "vless://u@h",
        "vless://u@h:99999",
        "vless://u@h:443?security=reality&sni=x",
        "vless://u@h:443?type=kcp",
        "vless://u@h:443?security=weird",
        "vless://u@h:443?type=xhttp&extra=notjson",
        "vless://u@h:443?type=xhttp&extra=[1]",
        "vless://u@h:443?type=tcp&headerType=http",
        "socks5://nohost",
        "ss://%%%",
        "ss://Zm9v@h:1?plugin=obfs",
        "ss://bm9jb2xvbg@h:1",
        "vless://" + "a" * 5000,
    ],
)
def test_bad_links_are_rejected_without_echo(bad: str) -> None:
    with pytest.raises(LinkError) as caught:
        outbound_from_link(bad, YOUTUBE_TAG)
    assert len(bad.strip()) < 12 or bad.strip()[:12] not in str(caught.value)


# ---------------------------------------------------------------------------------------------
# Template editing
# ---------------------------------------------------------------------------------------------
def rules_of(t: dict[str, Any]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = t["routing"]["rules"]
    return rules


def yt_rule(t: dict[str, Any]) -> dict[str, Any]:
    (rule,) = [
        r for r in rules_of(t) if r.get("balancerTag") == YOUTUBE_BALANCER_TAG or r.get("outboundTag") == YOUTUBE_TAG
    ]
    return rule


def test_enable_adds_outbound_balancer_observatory_and_rule() -> None:
    original = default_template()
    updated, changed = set_youtube_exit(original, wanted())
    assert changed and original == default_template()
    assert [o for o in updated["outbounds"] if o["tag"] == YOUTUBE_TAG] == [wanted().outbound]
    assert updated["routing"]["balancers"] == [
        {"tag": YOUTUBE_BALANCER_TAG, "selector": [YOUTUBE_TAG], "fallbackTag": "direct"}
    ]
    assert updated["observatory"]["subjectSelector"] == [YOUTUBE_TAG]
    assert yt_rule(updated) == {"type": "field", "domain": list(DOMAINS), "balancerTag": YOUTUBE_BALANCER_TAG}
    assert rules_of(updated)[0]["outboundTag"] == "api"
    assert rules_of(updated)[1] == yt_rule(updated)
    assert rules_of(updated)[2:] == default_template()["routing"]["rules"][1:]
    assert youtube_exit_enabled(updated, wanted()) and not youtube_exit_enabled(updated, None)


def test_disable_restores_the_original_exactly() -> None:
    on, _ = set_youtube_exit(default_template(), wanted())
    off, changed = set_youtube_exit(on, None)
    assert changed and off == default_template()
    again, changed = set_youtube_exit(off, None)
    assert not changed and again == default_template()


def test_enable_is_idempotent_and_updates_in_place() -> None:
    on, _ = set_youtube_exit(default_template(), wanted())
    again, changed = set_youtube_exit(on, wanted())
    assert not changed and again == on
    other = YoutubeExit(wanted().outbound, ("domain:example.org",))
    moved, changed = set_youtube_exit(on, other)
    assert changed and yt_rule(moved)["domain"] == ["domain:example.org"]
    assert len(moved["outbounds"]) == len(on["outbounds"])
    new_link = wanted("socks5://u:p@h.example.com:1080")
    swapped, changed = set_youtube_exit(on, YoutubeExit(new_link.outbound, DOMAINS))
    assert changed and [o for o in swapped["outbounds"] if o["tag"] == YOUTUBE_TAG] == [new_link.outbound]


def test_without_a_freedom_outbound_the_rule_points_at_the_exit() -> None:
    template = default_template()
    template["outbounds"] = [o for o in template["outbounds"] if o["protocol"] != "freedom"]
    updated, _ = set_youtube_exit(template, wanted())
    assert yt_rule(updated) == {"type": "field", "domain": list(DOMAINS), "outboundTag": YOUTUBE_TAG}
    assert "balancers" not in updated["routing"] and "observatory" not in updated
    off, _ = set_youtube_exit(updated, None)
    assert off == template


def test_fallback_uses_the_freedom_outbound_that_exists() -> None:
    template = default_template()
    template["outbounds"][0]["tag"] = "freedom-out"
    template["outbounds"][2]["protocol"] = "blackhole"
    updated, _ = set_youtube_exit(template, wanted())
    assert updated["routing"]["balancers"][0]["fallbackTag"] == "freedom-out"


@pytest.mark.parametrize("ads", [False, True])
@pytest.mark.parametrize("yt", [False, True])
def test_combinations_roundtrip(ads: bool, yt: bool) -> None:
    youtube = wanted() if yt else None
    updated, changed = apply_extras(default_template(), ads=ads, youtube=youtube)
    assert changed == (ads or yt)
    rules = rules_of(updated)
    assert rules[0]["outboundTag"] == "api"
    expected = ([ad_rule()] if ads else []) + ([yt_rule(updated)] if yt else [])
    assert rules[1 : 1 + len(expected)] == expected  # api, ads, youtube, then the operator's rules
    assert rules[1 + len(expected) :] == default_template()["routing"]["rules"][1:]
    again, changed = apply_extras(updated, ads=ads, youtube=youtube)
    assert not changed and again == updated
    back, changed = apply_extras(updated, ads=False, youtube=None)
    assert changed == (ads or yt)
    assert back == default_template()  # the default template already holds the blackhole outbound


def test_order_is_stable_whichever_feature_is_enabled_second() -> None:
    yt_first, _ = apply_extras(default_template(), ads=False, youtube=wanted())
    both_a, _ = apply_extras(yt_first, ads=True, youtube=wanted())
    both_b, _ = apply_extras(default_template(), ads=True, youtube=wanted())
    assert rules_of(both_a) == rules_of(both_b)
    assert rules_of(both_a)[1] == ad_rule()  # ads keep being blocked before the exit rule


def test_observatory_of_the_operator_is_extended_not_replaced() -> None:
    template = default_template()
    template["observatory"] = {
        "subjectSelector": ["proxy-"],
        "probeUrl": "https://example.com/204",
        "probeInterval": "5s",
    }
    template["routing"]["balancers"] = [{"tag": "mine", "selector": ["proxy-"]}]
    on, _ = set_youtube_exit(template, wanted())
    assert on["observatory"]["subjectSelector"] == ["proxy-", YOUTUBE_TAG]
    assert on["observatory"]["probeUrl"] == "https://example.com/204"
    assert on["routing"]["balancers"][0] == {"tag": "mine", "selector": ["proxy-"]}
    again, changed = set_youtube_exit(on, wanted())
    assert not changed and again == on
    off, _ = set_youtube_exit(on, None)
    assert off == template


def test_operator_rules_with_similar_shape_are_kept() -> None:
    template = default_template()
    similar = {"type": "field", "domain": ["geosite:youtube"], "outboundTag": "proxy-out"}
    template["routing"]["rules"].insert(1, similar)
    off, changed = set_youtube_exit(template, None)
    assert not changed and similar in rules_of(off)


def test_broken_template_shapes_raise() -> None:
    with pytest.raises(TemplateError):
        set_youtube_exit({"routing": {"rules": [], "balancers": 5}}, wanted())
    with pytest.raises(TemplateError):
        set_youtube_exit({"outbounds": {}}, wanted())


def test_nothing_to_remove_is_unchanged() -> None:
    assert set_youtube_exit({}, None)[1] is False
    assert set_ad_blocking(default_template(), False)[1] is False


# ---------------------------------------------------------------------------------------------
# Sniffing
# ---------------------------------------------------------------------------------------------
def test_sniffing_update_rules() -> None:
    assert sniffing_update(json.dumps({"enabled": True, "destOverride": ["http", "tls"]})) is None
    assert sniffing_update({"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": False}) is None
    assert sniffing_update("") == {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}
    assert sniffing_update({"enabled": False, "destOverride": ["fakedns"], "metadataOnly": False}) == {
        "enabled": True,
        "destOverride": ["fakedns", "http", "tls", "quic"],
        "metadataOnly": False,
        "routeOnly": True,
    }
    partial = sniffing_update({"enabled": True, "destOverride": ["http"], "routeOnly": False})
    assert partial == {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": False}


# ---------------------------------------------------------------------------------------------
# Backend against the 3x-ui emulation
# ---------------------------------------------------------------------------------------------
class FakeXrayWithInbounds(FakeXray):
    def __init__(self, layout: str, *, wrap: str) -> None:
        super().__init__(layout, wrap=wrap)
        quic = json.dumps({"enabled": True, "destOverride": ["http", "tls", "quic"]})
        base = {"settings": "{}", "streamSettings": "{}"}
        self.inbounds: dict[int, dict[str, Any]] = {
            1: {"id": 1, "protocol": "vless", "port": 443, "sniffing": "", **base},
            2: {"id": 2, "protocol": "vless", "port": 444, "sniffing": quic, **base},
            3: {"id": 3, "protocol": "mixed", "port": 2080, "sniffing": "", **base},
        }
        self.inbound_updates: list[int] = []

    def app(self) -> web.Application:
        app = super().app()
        app.router.add_get("/panel/api/inbounds/list", self._list)
        app.router.add_post("/panel/api/inbounds/update/{id}", self._inbound_update)
        return app

    async def _list(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return web.Response(status=401)
        return web.json_response({"success": True, "msg": "", "obj": list(self.inbounds.values())})

    async def _inbound_update(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return web.Response(status=401)
        inbound_id = int(request.match_info["id"])
        body = await request.json()
        assert isinstance(body["sniffing"], str) and "clientStats" not in body
        self.inbounds[inbound_id] = body
        self.inbound_updates.append(inbound_id)
        return web.json_response({"success": True, "msg": "ok", "obj": None})


@pytest.fixture(params=[("v3", "string"), ("v3", "object"), ("v2", "string"), ("v2", "object")], ids=str)
async def xray(request: pytest.FixtureRequest) -> AsyncIterator[tuple[FakeXrayWithInbounds, XuiBackend]]:
    layout, wrap = request.param
    fake = FakeXrayWithInbounds(layout, wrap=wrap)
    server = TestServer(fake.app())
    await server.start_server()
    backend = XuiBackend(str(server.make_url("")), api_token=TOKEN, timeout=5)
    try:
        yield fake, backend
    finally:
        await backend.close()
        await server.close()


async def test_backend_single_write_and_restart_for_both_features(
    xray: tuple[FakeXrayWithInbounds, XuiBackend],
) -> None:
    fake, backend = xray
    assert isinstance(backend, TemplateExtras)
    assert await backend.get_youtube_exit(None) is True and await backend.get_youtube_exit(wanted()) is False
    assert await backend.apply_template_extras(ads=True, youtube=wanted()) is True
    assert fake.updates == 1 and fake.restarts == 1  # one write and one restart for both features
    assert rules_of(fake.template)[1] == ad_rule()
    assert yt_rule(fake.template)["balancerTag"] == YOUTUBE_BALANCER_TAG
    assert OPERATOR_RULE in rules_of(fake.template) and CATCH_ALL in rules_of(fake.template)
    assert await backend.get_youtube_exit(wanted()) is True

    assert await backend.apply_template_extras(ads=True, youtube=wanted()) is False  # unchanged: nothing sent
    assert fake.updates == 1 and fake.restarts == 1

    assert await backend.apply_template_extras(ads=False, youtube=None) is True
    assert fake.updates == 2 and fake.restarts == 2
    assert fake.template == default_template()


async def test_backend_sniffing_touches_only_vless_inbounds_that_need_it(
    xray: tuple[FakeXrayWithInbounds, XuiBackend],
) -> None:
    fake, backend = xray
    before = copy.deepcopy(fake.inbounds)
    assert await backend.ensure_sniffing() == 1
    assert fake.inbound_updates == [1]
    assert json.loads(fake.inbounds[1]["sniffing"]) == {
        "enabled": True,
        "destOverride": ["http", "tls", "quic"],
        "routeOnly": True,
    }
    assert fake.inbounds[2] == before[2] and fake.inbounds[3] == before[3]
    assert await backend.ensure_sniffing() == 0
    assert fake.inbound_updates == [1]


# ---------------------------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------------------------
class FakeExtrasPanel:
    kind = PanelKind.XUI

    def __init__(self) -> None:
        self.calls: list[tuple[bool, bool]] = []
        self.sniffing_calls = 0
        self.error: str | None = None

    async def get_ad_blocking(self) -> bool:
        return False

    async def set_ad_blocking(self, on: bool) -> bool:  # pragma: no cover - the combined call is used instead
        raise AssertionError("the combined call must be used")

    async def get_youtube_exit(self, wanted_exit: YoutubeExit | None) -> bool:
        return True

    async def apply_template_extras(self, *, ads: bool, youtube: YoutubeExit | None) -> bool:
        if self.error:
            raise RuntimeError(self.error)
        self.calls.append((ads, youtube is not None))
        return True

    async def ensure_sniffing(self) -> int:
        self.sniffing_calls += 1
        return 2


async def test_converge_applies_both_features_once_and_records_state(session_factory: SessionFactory) -> None:
    panel_id = await _add_panel(session_factory, PanelKind.XUI, "a")
    panel = FakeExtrasPanel()
    registry = Registry()
    registry.backends[panel_id] = panel
    (result,) = await ad_blocking.converge(session_factory, registry, True, wanted())
    assert panel.calls == [(True, True)] and panel.sniffing_calls == 1
    assert result.youtube is True and result.sniffing_fixed == 2
    stored = await ad_blocking.load_state(session_factory, panel_id)
    assert stored.youtube is True and stored.sniffing_fixed == 2 and stored.enabled is True

    await ad_blocking.converge(session_factory, registry, True, None)
    assert panel.calls[-1] == (True, False) and panel.sniffing_calls == 1  # sniffing is left alone when disabling
    assert (await ad_blocking.load_state(session_factory, panel_id)).youtube is False


async def test_converge_keeps_previous_state_on_error(session_factory: SessionFactory) -> None:
    panel_id = await _add_panel(session_factory, PanelKind.XUI, "a")
    panel = FakeExtrasPanel()
    registry = Registry()
    registry.backends[panel_id] = panel
    await ad_blocking.converge(session_factory, registry, False, wanted())
    panel.error = "boom"
    (result,) = await ad_blocking.converge(session_factory, registry, False, wanted())
    assert result.error == "boom" and result.youtube is True


async def test_converge_skips_unsupported_panels(session_factory: SessionFactory) -> None:
    class Plain:
        kind = PanelKind.XUI

    panel_id = await _add_panel(session_factory, PanelKind.XUI, "legacy")
    remna = await _add_panel(session_factory, PanelKind.REMNAWAVE, "r")
    registry = Registry()
    registry.backends = {panel_id: Plain(), remna: FakeExtrasPanel()}
    results = await ad_blocking.converge(session_factory, registry, True, wanted())
    assert [(r.panel_id, r.supported) for r in results] == [(panel_id, False)]


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------
def test_settings_registered_with_both_languages() -> None:
    for key in ("youtube_exit_enabled", "youtube_exit_link", "youtube_exit_domains"):
        spec = REGISTRY[key]
        assert spec.group == "panels_sync" and spec.label_ru and spec.label_en and spec.help_ru and spec.help_en
    assert REGISTRY["youtube_exit_link"].type == "secret" and REGISTRY["youtube_exit_link"].owner_only
    assert REGISTRY["youtube_exit_domains"].type == "list[str]"
    assert "may stop working" in REGISTRY["youtube_exit_enabled"].help_en
    assert Settings.model_fields["youtube_exit_enabled"].default is False
    assert Settings().youtube_exit_domains == ["geosite:youtube"]


def test_link_validation_does_not_echo_the_secret() -> None:
    assert Settings(youtube_exit_link=SecretStr(REALITY)).youtube_exit_link is not None
    secret = "vless://topsecret-uuid@h:443?type=kcp"
    with pytest.raises(ValidationError) as caught:
        Settings(youtube_exit_link=SecretStr(secret))
    text = str(caught.value)
    assert "YOUTUBE_EXIT_LINK" in text and "topsecret" not in text
    with pytest.raises(ValidationError):
        Settings(youtube_exit_domains=["ok", "has space"])


def test_domains_split_from_environment_text() -> None:
    settings = Settings(youtube_exit_domains="geosite:youtube, domain:googlevideo.com")  # type: ignore[arg-type]
    assert settings.youtube_exit_domains == ["geosite:youtube", "domain:googlevideo.com"]


def test_youtube_exit_from_settings() -> None:
    assert ad_blocking.youtube_exit_from_settings(Settings()) is None
    assert ad_blocking.youtube_exit_from_settings(Settings(youtube_exit_enabled=True)) is None  # no link yet
    on = Settings(youtube_exit_enabled=True, youtube_exit_link=SecretStr(REALITY), youtube_exit_domains=["a", "b"])
    exit_ = ad_blocking.youtube_exit_from_settings(on)
    assert exit_ is not None and exit_.domains == ("a", "b") and exit_.outbound["tag"] == YOUTUBE_TAG
    off = Settings(youtube_exit_enabled=False, youtube_exit_link=SecretStr(REALITY))
    assert ad_blocking.youtube_exit_from_settings(off) is None
