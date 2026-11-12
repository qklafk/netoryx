"""Ad / tracker blocking: Xray template editing, the 3x-ui backend (both API layouts), convergence, settings."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.config import Settings
from app.core.settings_registry import REGISTRY
from app.db.models import Panel, PanelKind
from app.db.session import SessionFactory
from app.modules.ad_blocking import module as adblock_module
from app.services import ad_blocking
from app.services.panels.base import AdBlocker, PanelError
from app.services.panels.xray_template import (
    TemplateError,
    ad_blocking_enabled,
    ad_rule,
    parse_template,
    set_ad_blocking,
)
from app.services.panels.xui import XuiBackend

TOKEN = "tok-1234567890"
OPERATOR_RULE = {"type": "field", "domain": ["geosite:netflix"], "outboundTag": "proxy-out"}
CATCH_ALL = {"type": "field", "network": "tcp,udp", "outboundTag": "direct"}


def default_template() -> dict[str, Any]:
    return {
        "log": {"loglevel": "warning"},
        "api": {"tag": "api", "services": ["StatsService"]},
        "inbounds": [{"tag": "api", "port": 62789, "protocol": "tunnel"}],
        "outbounds": [
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "blocked", "protocol": "blackhole", "settings": {}},
            {"tag": "proxy-out", "protocol": "freedom", "settings": {}},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "outboundTag": "blocked", "ip": ["geoip:private"]},
                OPERATOR_RULE,
                CATCH_ALL,
            ],
        },
    }


# ---------------------------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------------------------
def test_rule_added_after_api_rule_and_before_operator_rules() -> None:
    template = default_template()
    updated, changed = set_ad_blocking(template, True)
    assert changed and ad_blocking_enabled(updated)
    rules = updated["routing"]["rules"]
    assert rules[0]["outboundTag"] == "api"
    assert rules[1] == ad_rule()
    assert rules[2:] == template["routing"]["rules"][1:]  # nothing else touched or reordered
    assert template == default_template()  # input is not mutated


def test_enabling_is_idempotent() -> None:
    once, _ = set_ad_blocking(default_template(), True)
    twice, changed = set_ad_blocking(once, True)
    assert not changed and twice == once


def test_disabling_removes_exactly_our_rule() -> None:
    original = default_template()
    on, _ = set_ad_blocking(original, True)
    off, changed = set_ad_blocking(on, False)
    assert changed and off == original
    again, changed = set_ad_blocking(off, False)
    assert not changed and again == original


def test_disabling_keeps_similar_operator_rules() -> None:
    similar = {"type": "field", "domain": ["geosite:category-ads-all"], "outboundTag": "direct"}
    extended = {"type": "field", "domain": ["geosite:category-ads-all", "geosite:other"], "outboundTag": "blocked"}
    template = default_template()
    template["routing"]["rules"] += [similar, extended]
    off, changed = set_ad_blocking(template, False)
    assert not changed and off == template
    assert not ad_blocking_enabled(template)


def test_blackhole_outbound_is_added_when_missing() -> None:
    template = default_template()
    template["outbounds"] = [o for o in template["outbounds"] if o["tag"] != "blocked"]
    updated, changed = set_ad_blocking(template, True)
    assert changed
    assert {"tag": "blocked", "protocol": "blackhole", "settings": {}} in updated["outbounds"]
    assert ad_blocking_enabled(updated)
    off, _ = set_ad_blocking(updated, False)  # disabling leaves the outbound alone
    assert any(o["tag"] == "blocked" for o in off["outbounds"])


def test_conflicting_blocked_outbound_is_refused() -> None:
    template = default_template()
    template["outbounds"][1] = {"tag": "blocked", "protocol": "freedom", "settings": {}}
    with pytest.raises(TemplateError):
        set_ad_blocking(template, True)


def test_rule_goes_first_without_an_api_rule() -> None:
    template = default_template()
    template["routing"]["rules"] = [CATCH_ALL]
    updated, _ = set_ad_blocking(template, True)
    assert updated["routing"]["rules"] == [ad_rule(), CATCH_ALL]


def test_missing_routing_and_outbounds_sections() -> None:
    updated, changed = set_ad_blocking({}, True)
    assert changed and ad_blocking_enabled(updated)


@pytest.mark.parametrize(
    "wrap",
    [
        lambda t: t,
        lambda t: json.dumps(t),
        lambda t: {"xraySetting": t, "inboundTags": "[]"},
        lambda t: {"xraySetting": json.dumps(t), "inboundTags": []},
        lambda t: json.dumps({"xraySetting": t, "inboundTags": []}),
    ],
)
def test_parse_template_shapes(wrap: Callable[[dict[str, Any]], Any]) -> None:
    assert parse_template(wrap(default_template())) == default_template()


def test_parse_template_rejects_garbage() -> None:
    for bad in ("not json", None, 5, "[]"):
        with pytest.raises(TemplateError):
            parse_template(bad)


# ---------------------------------------------------------------------------------------------
# Backend against an in-process 3x-ui emulation
# ---------------------------------------------------------------------------------------------
class FakeXray:
    """Serves the Xray template routes of one 3x-ui generation."""

    def __init__(self, layout: str, *, wrap: str) -> None:
        self.layout = layout  # "v3" (/panel/api/xray, modern clients) or "v2" (/panel/xray, legacy clients)
        self.wrap = wrap  # "string" | "object": how the template comes back in `obj`
        self.template = default_template()
        self.updates = 0
        self.restarts = 0
        self.fail_update: str | None = None

    def app(self) -> web.Application:
        prefix = "/panel/api/xray" if self.layout == "v3" else "/panel/xray"
        app = web.Application()
        app.router.add_get("/panel/api/server/status", self._status)
        app.router.add_get("/panel/api/clients/get/{email}", self._probe)
        app.router.add_post(f"{prefix}/", self._get)
        app.router.add_post(f"{prefix}/update", self._update)
        app.router.add_post("/panel/api/server/restartXrayService", self._restart)
        return app

    @staticmethod
    def _auth(request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    async def _status(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return web.Response(status=401)
        return web.json_response({"success": True, "msg": "", "obj": {"panelVersion": "3.11.0"}})

    async def _probe(self, request: web.Request) -> web.Response:
        if self.layout == "v2":
            return web.Response(status=404)
        return web.json_response({"success": True, "msg": "", "obj": None})

    async def _get(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return web.Response(status=401)
        wrapper = {"xraySetting": copy.deepcopy(self.template), "inboundTags": []}
        obj: Any = json.dumps(wrapper) if self.wrap == "string" else wrapper
        return web.json_response({"success": True, "msg": "", "obj": obj})

    async def _update(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return web.Response(status=401)
        form = await request.post()
        if self.fail_update:
            return web.json_response({"success": False, "msg": self.fail_update, "obj": None})
        self.template = json.loads(str(form["xraySetting"]))
        self.updates += 1
        if self.layout == "v3":
            self.restarts += 1  # the v3 panel restarts a running core by itself
        return web.json_response({"success": True, "msg": "ok", "obj": None})

    async def _restart(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return web.Response(status=401)
        self.restarts += 1
        return web.json_response({"success": True, "msg": "", "obj": None})


@pytest.fixture(params=[("v3", "string"), ("v3", "object"), ("v2", "string"), ("v2", "object")], ids=str)
async def xray(request: pytest.FixtureRequest) -> AsyncIterator[tuple[FakeXray, XuiBackend]]:
    layout, wrap = request.param
    fake = FakeXray(layout, wrap=wrap)
    server = TestServer(fake.app())
    await server.start_server()
    backend = XuiBackend(str(server.make_url("")), api_token=TOKEN, timeout=5)
    try:
        yield fake, backend
    finally:
        await backend.close()
        await server.close()


async def test_backend_toggles_rule_and_restarts_once(xray: tuple[FakeXray, XuiBackend]) -> None:
    fake, backend = xray
    assert isinstance(backend, AdBlocker)
    assert await backend.get_ad_blocking() is False
    assert await backend.set_ad_blocking(True) is True
    assert await backend.get_ad_blocking() is True
    assert fake.updates == 1 and fake.restarts == 1  # exactly one restart on every API layout
    assert fake.template["routing"]["rules"][1] == ad_rule()
    assert OPERATOR_RULE in fake.template["routing"]["rules"] and CATCH_ALL in fake.template["routing"]["rules"]

    assert await backend.set_ad_blocking(True) is False  # unchanged: no write, no restart
    assert fake.updates == 1 and fake.restarts == 1

    assert await backend.set_ad_blocking(False) is True
    assert fake.template == default_template()
    assert fake.updates == 2 and fake.restarts == 2
    assert await backend.set_ad_blocking(False) is False
    assert fake.restarts == 2


async def test_backend_ensures_blackhole_outbound(xray: tuple[FakeXray, XuiBackend]) -> None:
    fake, backend = xray
    fake.template["outbounds"] = [o for o in fake.template["outbounds"] if o["tag"] != "blocked"]
    assert await backend.set_ad_blocking(True) is True
    assert {"tag": "blocked", "protocol": "blackhole", "settings": {}} in fake.template["outbounds"]
    assert await backend.get_ad_blocking() is True


async def test_backend_reports_panel_refusal(xray: tuple[FakeXray, XuiBackend]) -> None:
    fake, backend = xray
    fake.fail_update = "geosite category not found"
    with pytest.raises(PanelError, match="category not found"):
        await backend.set_ad_blocking(True)
    assert fake.restarts == 0


async def test_backend_refuses_conflicting_outbound(xray: tuple[FakeXray, XuiBackend]) -> None:
    fake, backend = xray
    fake.template["outbounds"][1] = {"tag": "blocked", "protocol": "freedom", "settings": {}}
    with pytest.raises(PanelError, match="not a blackhole"):
        await backend.set_ad_blocking(True)
    assert fake.updates == 0


# ---------------------------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------------------------
class FakeAdBlockPanel:
    kind = PanelKind.XUI

    def __init__(self, *, enabled: bool = False, error: str | None = None) -> None:
        self.enabled = enabled
        self.error = error
        self.restarts = 0

    async def get_ad_blocking(self) -> bool:
        return self.enabled

    async def set_ad_blocking(self, on: bool) -> bool:
        if self.error:
            raise PanelError(self.error)
        if self.enabled == on:
            return False
        self.enabled = on
        self.restarts += 1
        return True


class PlainPanel:
    kind = PanelKind.REMNAWAVE


class Registry:
    def __init__(self) -> None:
        self.backends: dict[int, Any] = {}

    async def get(self, panel_id: int) -> Any:
        return self.backends[panel_id]


async def _add_panel(sf: SessionFactory, kind: PanelKind, name: str, *, active: bool = True) -> int:
    async with sf.begin() as session:
        panel = Panel(name=name, kind=kind, url=f"https://{name}.example.com", is_active=active)
        session.add(panel)
        await session.flush()
        return panel.id


async def test_converge_applies_setting_and_skips_unsupported(session_factory: SessionFactory) -> None:
    registry = Registry()
    xui_a = await _add_panel(session_factory, PanelKind.XUI, "a")
    xui_b = await _add_panel(session_factory, PanelKind.XUI, "b")
    inactive = await _add_panel(session_factory, PanelKind.XUI, "off", active=False)
    remna = await _add_panel(session_factory, PanelKind.REMNAWAVE, "r")
    registry.backends = {
        xui_a: FakeAdBlockPanel(),
        xui_b: FakeAdBlockPanel(enabled=True),
        inactive: FakeAdBlockPanel(),
        remna: PlainPanel(),
    }
    results = await ad_blocking.converge(session_factory, registry, True)
    assert {r.panel_id for r in results} == {xui_a, xui_b}  # Remnawave and inactive panels are not touched
    assert registry.backends[xui_a].enabled and registry.backends[xui_a].restarts == 1
    assert registry.backends[xui_b].restarts == 0  # already matched: no restart
    assert not registry.backends[inactive].enabled

    state = await ad_blocking.load_state(session_factory, xui_a)
    assert state.enabled is True and state.error is None and state.checked_at

    await ad_blocking.converge(session_factory, registry, False)
    assert not registry.backends[xui_a].enabled and not registry.backends[xui_b].enabled


async def test_converge_skips_a_panel_without_the_capability(session_factory: SessionFactory) -> None:
    registry = Registry()
    panel_id = await _add_panel(session_factory, PanelKind.XUI, "legacy")
    registry.backends[panel_id] = PlainPanel()
    (result,) = await ad_blocking.converge(session_factory, registry, True)
    assert result.supported is False and result.error is None


async def test_converge_records_errors_and_recovers(session_factory: SessionFactory) -> None:
    registry = Registry()
    panel_id = await _add_panel(session_factory, PanelKind.XUI, "a")
    registry.backends[panel_id] = FakeAdBlockPanel(error="geosite missing")
    (result,) = await ad_blocking.converge(session_factory, registry, True)
    assert result.error and "geosite missing" in result.error
    stored = await ad_blocking.load_state(session_factory, panel_id)
    assert stored.error and "geosite missing" in stored.error

    registry.backends[panel_id].error = None  # retried on the next run
    (result,) = await ad_blocking.converge(session_factory, registry, True)
    assert result.error is None and result.enabled is True
    assert (await ad_blocking.load_state(session_factory, panel_id)).error is None


async def test_unreachable_panel_does_not_block_others(session_factory: SessionFactory) -> None:
    good = FakeAdBlockPanel()
    first = await _add_panel(session_factory, PanelKind.XUI, "a")
    second = await _add_panel(session_factory, PanelKind.XUI, "b")

    class Broken:
        async def get(self, panel_id: int) -> Any:
            if panel_id == first:
                raise PanelError("down")
            return good

    results = {r.panel_id: r for r in await ad_blocking.converge(session_factory, Broken(), True)}
    assert results[first].error == "down" and results[second].error is None and good.enabled


# ---------------------------------------------------------------------------------------------
# Setting and module
# ---------------------------------------------------------------------------------------------
def test_setting_is_registered_and_off_by_default() -> None:
    spec = REGISTRY["adblock_enabled"]
    assert spec.type == "bool" and spec.group == "panels_sync"
    assert spec.label_ru and spec.label_en and spec.help_ru and spec.help_en
    assert "YouTube" in spec.help_en and "YouTube" in spec.help_ru
    assert Settings.model_fields["adblock_enabled"].default is False


def test_module_registers_the_convergence_job() -> None:
    (job,) = adblock_module.jobs
    assert job.id == "ad_blocking.converge"
    assert job.trigger_kwargs(Settings.model_construct()) == {"minutes": 10}
