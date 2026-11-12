"""Existing VPN identities are adopted once, with explicit ownership and one-time encrypted staging."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import KeyValue, Panel, PanelKind, Provision, Subscription, User
from app.db.session import SessionFactory
from app.services import imports, kv
from app.services.panels.marzban import parse_account
from app.utils.crypto import SecretBox


@dataclass
class Source:
    users: list[dict[str, Any]] = field(default_factory=list)

    async def list_users(self) -> list[dict[str, Any]]:
        return self.users

    async def get_account(self, target: int | None, username: str) -> Any:
        raw = next((raw for raw in self.users if raw["username"] == username), None)
        return parse_account(raw) if raw else None


@dataclass
class Registry:
    source: Source

    async def get(self, panel_id: int) -> Source:
        return self.source


def account(name: str = "operator_user", **values: Any) -> dict[str, Any]:
    data = {
        "username": name,
        "proxies": {"vless": {"id": str(uuid4())}},
        "status": "active",
        "expire": 1900000000,
        "data_limit": 1_500_000_000,
        "used_traffic": 125_000_000,
        "links": ["vless://identity@host.example:443#Server"],
        "subscription_url": "https://panel.example/sub/native-token",
    }
    data.update(values)
    return data


async def setup(sf: SessionFactory) -> int:
    async with sf.begin() as session:
        session.add_all(
            [
                User(tg_id=1, referral_code="r1", sub_token="old-token-1"),
                User(tg_id=2, referral_code="r2", sub_token="old-token-2"),
            ]
        )
        panel = Panel(name="Marzban", kind=PanelKind.MARZBAN, url="https://panel.example")
        session.add(panel)
        await session.flush()
        return panel.id


async def test_preview_has_no_writes_and_reports_unsupported(session_factory: SessionFactory) -> None:
    sf = session_factory
    panel_id = await setup(sf)
    source = Source([account(), account("trojan_only", proxies={"trojan": {"password": "secret"}})])
    result = await imports.preview(sf, Registry(source), panel_id)
    assert result["total"] == 2 and result["items"][0]["supported"]
    assert not result["items"][1]["supported"] and result["items"][1]["reason"]
    async with sf() as session:
        assert not await session.scalar(select(func.count()).select_from(Subscription))
        assert not await session.scalar(select(func.count()).select_from(KeyValue))


async def test_import_preserves_identity_exact_quota_and_native_link(
    session_factory: SessionFactory, settings: Settings
) -> None:
    sf, box = session_factory, SecretBox("test-secret-key-0123456789abcdef")
    panel_id = await setup(sf)
    raw = account()
    registry = Registry(Source([raw]))
    first = await imports.apply(
        sf, registry, settings, box, panel_id, [imports.Selection(raw["username"], 1)], "request01"
    )
    repeat = await imports.apply(
        sf, registry, settings, box, panel_id, [imports.Selection(raw["username"], 1)], "request01"
    )
    assert first == repeat
    async with sf() as session:
        sub = await session.scalar(select(Subscription))
        provision = await session.scalar(select(Provision))
        assert sub and provision
        assert sub.client_uuid == raw["proxies"]["vless"]["id"]
        assert sub.sub_token == "old-token-1" and sub.traffic_limit_bytes == 1_500_000_000
        assert provision.remote_email == raw["username"] and provision.synced_revision == sub.revision
        assert raw["links"] == json.loads(box.decrypt(provision.links_cache or ""))["links"]
        assert raw["used_traffic"] == 125_000_000
    again = await imports.apply(
        sf, registry, settings, box, panel_id, [imports.Selection(raw["username"], 1)], "request02"
    )
    assert again["items"][0]["already_imported"]
    async with sf() as session:
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 1


async def test_multiple_existing_accounts_get_distinct_subscriptions(
    session_factory: SessionFactory, settings: Settings
) -> None:
    sf, box = session_factory, SecretBox("test-secret-key-0123456789abcdef")
    panel_id = await setup(sf)
    registry = Registry(Source([account("first"), account("second")]))
    result = await imports.apply(
        sf,
        registry,
        settings,
        box,
        panel_id,
        [imports.Selection("first", 1), imports.Selection("second", 1)],
        "request03",
    )
    assert result["imported"] == 2
    async with sf() as session:
        rows = list(await session.scalars(select(Subscription).order_by(Subscription.id)))
        assert len(rows) == 2 and rows[0].sub_token != rows[1].sub_token


async def test_claim_is_one_time_and_snapshot_is_encrypted(session_factory: SessionFactory, settings: Settings) -> None:
    sf, box = session_factory, SecretBox("test-secret-key-0123456789abcdef")
    panel_id = await setup(sf)
    registry = Registry(Source([account()]))
    result = await imports.apply(
        sf,
        registry,
        settings,
        box,
        panel_id,
        [imports.Selection("operator_user")],
        "request04",
        bot_username="example_bot",
    )
    assert result["staged"] == 1
    token = result["items"][0]["claim_url"].split("import_", 1)[1]
    async with sf() as session:
        assert not await session.scalar(select(func.count()).select_from(Subscription))
        data = " ".join(await session.scalars(select(KeyValue.value)))
        assert token not in data and "native-token" not in data and "proxies" not in data
        assert "operator_user" in await imports.protected_accounts(session, panel_id)
    sub_id = await imports.claim(sf, settings, box, 1, token)
    async with sf() as session:
        sub = await session.get(Subscription, sub_id)
        assert sub and sub.user_id == 1
    with pytest.raises(imports.ImportError, match="already been used"):
        await imports.claim(sf, settings, box, 2, token)


async def test_existing_identity_cannot_be_reassigned(session_factory: SessionFactory, settings: Settings) -> None:
    sf, box = session_factory, SecretBox("test-secret-key-0123456789abcdef")
    panel_id = await setup(sf)
    registry = Registry(Source([account()]))
    await imports.apply(sf, registry, settings, box, panel_id, [imports.Selection("operator_user", 1)], "request05")
    result = await imports.apply(
        sf, registry, settings, box, panel_id, [imports.Selection("operator_user", 2)], "request06"
    )
    assert result["imported"] == 0 and result["errors"]


async def test_unknown_user_and_on_hold_are_not_imported(session_factory: SessionFactory, settings: Settings) -> None:
    sf, box = session_factory, SecretBox("test-secret-key-0123456789abcdef")
    panel_id = await setup(sf)
    registry = Registry(Source([account(), account("on_hold", status="on_hold")]))
    result = await imports.apply(
        sf,
        registry,
        settings,
        box,
        panel_id,
        [imports.Selection("operator_user", 999), imports.Selection("on_hold", 1)],
        "request07",
    )
    assert len(result["errors"]) == 2 and result["imported"] == 0
    async with sf() as session:
        assert not await session.scalar(select(func.count()).select_from(Subscription))


async def test_expired_claim_rejected(session_factory: SessionFactory, settings: Settings) -> None:
    sf, box = session_factory, SecretBox("test-secret-key-0123456789abcdef")
    panel_id = await setup(sf)
    result = await imports.apply(
        sf, Registry(Source([account()])), settings, box, panel_id, [imports.Selection("operator_user")], "request08"
    )
    token = result["items"][0]["claim_url"]
    async with sf.begin() as session:
        row = await session.scalar(select(KeyValue).where(KeyValue.key.startswith("importc:")))
        assert row
        data = json.loads(box.decrypt(row.value))
        data["expires_at"] = "2000-01-01T00:00:00+00:00"
        await kv.set(session, row.key, box.encrypt(json.dumps(data)))
    with pytest.raises(imports.ImportError, match="expired"):
        await imports.claim(sf, settings, box, 1, token)


async def test_claim_refreshes_current_expiry_and_rejects_replaced_identity(
    session_factory: SessionFactory, settings: Settings
) -> None:
    sf, box = session_factory, SecretBox("test-secret-key-0123456789abcdef")
    panel_id = await setup(sf)
    raw = account()
    registry = Registry(Source([raw]))
    result = await imports.apply(
        sf, registry, settings, box, panel_id, [imports.Selection("operator_user")], "request09"
    )
    token = result["items"][0]["claim_url"]
    raw["expire"] = 1950000000
    sub_id = await imports.claim(sf, settings, box, 1, token, registry=registry)
    async with sf() as session:
        sub = await session.get(Subscription, sub_id)
        assert sub and int(sub.expires_at.timestamp()) == 1950000000
    raw2 = account("second")
    registry.source.users.append(raw2)
    result2 = await imports.apply(sf, registry, settings, box, panel_id, [imports.Selection("second")], "request10")
    token2 = result2["items"][0]["claim_url"]
    raw2["proxies"]["vless"]["id"] = str(uuid4())
    with pytest.raises(imports.ImportError, match="source account changed"):
        await imports.claim(sf, settings, box, 1, token2, registry=registry)


class _DownSource(Source):
    async def get_account(self, target: int | None, username: str) -> Any:
        from app.services.panels.base import PanelError

        raise PanelError("connection refused")


async def test_claim_survives_an_unreachable_panel(session_factory: SessionFactory, settings: Settings) -> None:
    sf, box = session_factory, SecretBox("test-secret-key-0123456789abcdef")
    panel_id = await setup(sf)
    source = Source([account()])
    result = await imports.apply(
        sf,
        Registry(source),
        settings,
        box,
        panel_id,
        [imports.Selection("operator_user")],
        "request21",
        bot_username="example_bot",
    )
    token = result["items"][0]["claim_url"].split("import_", 1)[1]
    from app.services.panels.base import PanelError

    with pytest.raises(PanelError):
        await imports.claim(sf, settings, box, 1, token, registry=Registry(_DownSource(source.users)))
    sub_id = await imports.claim(sf, settings, box, 1, token, registry=Registry(source))
    async with sf() as session:
        sub = await session.get(Subscription, sub_id)
        assert sub and sub.user_id == 1
