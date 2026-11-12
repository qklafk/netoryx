"""PanelRegistry / build_backend: decryption, caching, rebuild on change, Remnawave wiring."""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest
from sqlalchemy import update

from app.config import Settings
from app.db.models import Panel, PanelKind
from app.db.session import SessionFactory
from app.services.panels.base import PanelAuthError, PanelError
from app.services.panels.hiddify import HiddifyBackend
from app.services.panels.marzneshin import MarzneshinBackend
from app.services.panels.pasarguard import PasarGuardBackend
from app.services.panels.registry import PanelRegistry, build_backend, panel_fingerprint, panel_options
from app.services.panels.xui import XuiBackend
from app.utils.crypto import SecretBox

KEY = "test-secret-key-0123456789abcdef"


class StubRemnawave:
    """Records constructor arguments; replaces the real backend (owned by another module)."""

    def __init__(self, url: str, api_token: str, **kwargs: Any) -> None:
        self.url, self.api_token, self.kwargs = url, api_token, kwargs
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def stub_remnawave(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("app.services.panels.remnawave")
    module.RemnawaveBackend = StubRemnawave  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.services.panels.remnawave", module)


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(KEY)


def xui_panel(box: SecretBox, **overrides: Any) -> Panel:
    values: dict[str, Any] = {
        "id": 1,
        "name": "DE",
        "kind": PanelKind.XUI,
        "url": "https://1.2.3.4:2053/base/",
        "username": "admin",
        "password": box.encrypt("s3cret"),
        "api_token": None,
        "tls_verify": False,
        "options": json.dumps({"headers": {"X-Api-Key": "k"}}),
    }
    values.update(overrides)
    return Panel(**values)


async def insert(session_factory: SessionFactory, panel: Panel) -> int:
    async with session_factory.begin() as session:
        session.add(panel)
        await session.flush()
        return panel.id


# ---------------------------------------------------------------------------------------------
# build_backend
# ---------------------------------------------------------------------------------------------
def test_build_xui_backend_decrypts_credentials(box: SecretBox, settings: Settings) -> None:
    backend = build_backend(xui_panel(box, api_token=box.encrypt("tok")), box, settings)
    assert isinstance(backend, XuiBackend)
    assert backend.url == "https://1.2.3.4:2053/base"
    transport = backend._t
    assert (transport._password, transport._token, transport._tls_verify) == ("s3cret", "tok", False)
    assert transport._extra_headers == {"X-Api-Key": "k"}
    assert transport._timeout.total == settings.panel_request_timeout_sec


def test_build_xui_backend_accepts_plaintext_secrets(box: SecretBox, settings: Settings) -> None:
    backend = build_backend(xui_panel(box, password="plain", options=None), box, settings)
    assert isinstance(backend, XuiBackend)
    assert backend._t._password == "plain"


def test_build_xui_backend_without_credentials_fails(box: SecretBox, settings: Settings) -> None:
    with pytest.raises(PanelAuthError, match="username"):
        build_backend(xui_panel(box, username=None, password=None), box, settings)


def test_build_remnawave_backend(box: SecretBox, settings: Settings) -> None:
    options = {
        "squad_uuids": ["a", "b"],
        "username_prefix": "vpn_",
        "direct": False,
        "headers": {"X-Api-Key": "caddy"},
        "cookies": {"session": "c"},
    }
    panel = xui_panel(
        box,
        kind=PanelKind.REMNAWAVE,
        url="http://remnawave:3000",
        username=None,
        password=None,
        api_token=box.encrypt("rw-token"),
        tls_verify=True,
        options=json.dumps(options),
    )
    backend = build_backend(panel, box, settings)
    assert isinstance(backend, StubRemnawave)
    assert (backend.url, backend.api_token) == ("http://remnawave:3000", "rw-token")
    assert backend.kwargs == {
        "squad_uuids": ["a", "b"],
        "username_prefix": "vpn_",
        "direct": False,
        "tls_verify": True,
        "timeout": settings.panel_request_timeout_sec,
        "extra_headers": {"X-Api-Key": "caddy"},
        "cookies": {"session": "c"},
        "traffic_limit_strategy": "NO_RESET",
    }


def test_build_remnawave_defaults(box: SecretBox, settings: Settings) -> None:
    panel = xui_panel(box, kind=PanelKind.REMNAWAVE, api_token="tok", options=None)
    backend = build_backend(panel, box, settings)
    assert isinstance(backend, StubRemnawave)
    assert backend.kwargs["squad_uuids"] == []
    assert backend.kwargs["username_prefix"] == "tg_"
    assert backend.kwargs["direct"] is True
    assert backend.kwargs["extra_headers"] is None


def test_build_remnawave_undecryptable_token_is_auth_error(box: SecretBox, settings: Settings) -> None:
    token = SecretBox("another-secret-key-0123456789abcd").encrypt("rw-token")
    panel = xui_panel(box, kind=PanelKind.REMNAWAVE, api_token=token, options=None)
    with pytest.raises(PanelAuthError, match="panel 1"):
        build_backend(panel, box, settings)


def test_build_remnawave_requires_token(box: SecretBox, settings: Settings) -> None:
    with pytest.raises(PanelAuthError, match="API token"):
        build_backend(xui_panel(box, kind=PanelKind.REMNAWAVE, api_token=None), box, settings)


def test_build_hiddify_backend(box: SecretBox, settings: Settings) -> None:
    backend = build_backend(
        xui_panel(
            box,
            kind=PanelKind.HIDDIFY,
            url="https://hiddify.example/admin-path",
            username=None,
            password=None,
            api_token=box.encrypt("api-key"),
        ),
        box,
        settings,
    )
    assert isinstance(backend, HiddifyBackend)
    assert backend._url == "https://hiddify.example/admin-path" and backend._api_key == "api-key"


def test_build_pasarguard_backend(box: SecretBox, settings: Settings) -> None:
    backend = build_backend(
        xui_panel(
            box,
            kind=PanelKind.PASARGUARD,
            url="https://pasarguard.example",
            username=None,
            password=None,
            api_token=box.encrypt("api-key"),
        ),
        box,
        settings,
    )
    assert isinstance(backend, PasarGuardBackend)
    assert backend._url == "https://pasarguard.example" and backend._api_key == "api-key"


def test_build_marzneshin_backend(box: SecretBox, settings: Settings) -> None:
    backend = build_backend(
        xui_panel(
            box,
            kind=PanelKind.MARZNESHIN,
            url="https://marzneshin.example",
            username="admin",
            password=box.encrypt("pass"),
            api_token=None,
            options=json.dumps({"service_ids": [3, 7, "bad"]}),
        ),
        box,
        settings,
    )
    assert isinstance(backend, MarzneshinBackend)
    assert backend._username == "admin" and backend._password == "pass" and backend._service_ids == [3, 7]


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "", None])
def test_invalid_options_are_ignored(box: SecretBox, raw: str | None) -> None:
    assert panel_options(xui_panel(box, options=raw)) == {}


def test_fingerprint_tracks_connection_fields_only(box: SecretBox) -> None:
    panel = xui_panel(box)
    base = panel_fingerprint(panel)
    panel.name, panel.version, panel.consecutive_failures = "renamed", "3.11.0", 5
    assert panel_fingerprint(panel) == base
    for field, value in [("url", "https://x"), ("password", "other"), ("tls_verify", True), ("options", "{}")]:
        changed = xui_panel(box, **{field: value})
        assert panel_fingerprint(changed) != base, field


# ---------------------------------------------------------------------------------------------
# PanelRegistry
# ---------------------------------------------------------------------------------------------
async def test_get_caches_backend(session_factory: SessionFactory, settings: Settings, box: SecretBox) -> None:
    panel_id = await insert(session_factory, xui_panel(box, id=None))
    registry = PanelRegistry(session_factory, settings, box)
    first = await registry.get(panel_id)
    assert await registry.get(panel_id) is first
    await registry.close()


async def test_get_rebuilds_and_closes_stale_backend_when_row_changes(
    session_factory: SessionFactory, settings: Settings, box: SecretBox
) -> None:
    panel_id = await insert(session_factory, xui_panel(box, id=None, kind=PanelKind.REMNAWAVE, api_token="t1"))
    registry = PanelRegistry(session_factory, settings, box)
    first = await registry.get(panel_id)
    async with session_factory.begin() as session:
        await session.execute(update(Panel).where(Panel.id == panel_id).values(api_token=box.encrypt("t2")))
    second = await registry.get(panel_id)
    assert second is not first
    assert isinstance(first, StubRemnawave) and first.closed
    assert isinstance(second, StubRemnawave) and second.api_token == "t2"
    await registry.close()
    assert second.closed


async def test_invalidate_forces_rebuild(session_factory: SessionFactory, settings: Settings, box: SecretBox) -> None:
    panel_id = await insert(session_factory, xui_panel(box, id=None, kind=PanelKind.REMNAWAVE, api_token="t"))
    registry = PanelRegistry(session_factory, settings, box)
    first = await registry.get(panel_id)
    await registry.invalidate(panel_id)
    await registry.invalidate(panel_id)  # second call is a no-op
    assert isinstance(first, StubRemnawave) and first.closed
    assert await registry.get(panel_id) is not first


async def test_missing_panel_raises_lookup_error_and_drops_cache(
    session_factory: SessionFactory, settings: Settings, box: SecretBox
) -> None:
    panel_id = await insert(session_factory, xui_panel(box, id=None, kind=PanelKind.REMNAWAVE, api_token="t"))
    registry = PanelRegistry(session_factory, settings, box)
    backend = await registry.get(panel_id)
    async with session_factory.begin() as session:
        await session.delete(await session.get(Panel, panel_id))
    with pytest.raises(LookupError, match=f"panel {panel_id}"):
        await registry.get(panel_id)
    assert isinstance(backend, StubRemnawave) and backend.closed


async def test_bad_config_raises_panel_error(
    session_factory: SessionFactory, settings: Settings, box: SecretBox
) -> None:
    panel_id = await insert(session_factory, xui_panel(box, id=None, kind=PanelKind.REMNAWAVE, api_token=None))
    registry = PanelRegistry(session_factory, settings, box)
    with pytest.raises(PanelError):
        await registry.get(panel_id)


async def test_close_survives_failing_backends(
    session_factory: SessionFactory, settings: Settings, box: SecretBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel_id = await insert(session_factory, xui_panel(box, id=None, kind=PanelKind.REMNAWAVE, api_token="t"))
    registry = PanelRegistry(session_factory, settings, box)
    backend = await registry.get(panel_id)

    async def boom() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(backend, "close", boom)
    await registry.close()  # must not raise
    assert await registry.get(panel_id) is not backend
    await registry.close()
