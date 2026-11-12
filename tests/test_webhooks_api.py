"""Web admin API for outgoing webhooks: RBAC, CRUD, one-time secrets, test events, redelivery, audit."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import ClassVar

import pytest
from sqlalchemy import select

from app.db.models import AdminAuditLog, AdminRole, WebhookEndpoint
from app.services import events, webhooks
from tests.test_mod_webapi_support import Api, api, as_role, ctx

__all__ = ["api", "ctx"]


class FakeSender:
    calls: ClassVar[list[tuple[str, bytes, dict[str, str]]]] = []
    status = 200

    def __init__(self, *, dev_mode: bool = False) -> None:
        pass

    async def close(self) -> None:
        return None

    async def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> webhooks.Outcome:
        FakeSender.calls.append((url, body, dict(headers)))
        if 200 <= FakeSender.status < 300:
            return webhooks.Outcome(True, FakeSender.status)
        return webhooks.Outcome(False, FakeSender.status, f"HTTP {FakeSender.status}")


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["10.0.0.7"] if host.startswith("internal") else ["93.184.216.34"]

    FakeSender.calls = []
    FakeSender.status = 200
    monkeypatch.setattr(webhooks, "_system_resolve", resolve)
    monkeypatch.setattr(webhooks, "HttpSender", FakeSender)


async def test_rbac(api: Api) -> None:
    for role in (AdminRole.SUPPORT, AdminRole.VIEWER):
        other = await as_role(api, role, tg_id=3000 + len(role))
        assert (await other.get("/webhooks")).status_code == 403
        assert (await other.post("/webhooks", json={"url": "https://crm.example.com/h"})).status_code == 403
    admin = await as_role(api, AdminRole.ADMIN, tg_id=4000)
    assert (await admin.get("/webhooks")).status_code == 200
    assert (await admin.post("/webhooks", json={"url": "https://crm.example.com/h"})).status_code == 201


async def test_crud_secret_test_and_redeliver(api: Api) -> None:
    ctx = api.ctx
    catalog = (await api.get("/webhooks/catalog")).json()
    assert "purchase" in catalog["events"] and catalog["retry_delays_sec"][0] == 60

    bad = await api.post("/webhooks", json={"url": "http://crm.example.com/h"})
    assert bad.status_code == 400 and bad.json()["error"] == "insecure_url"
    private = await api.post("/webhooks", json={"url": "https://internal.example.com/h"})
    assert private.status_code == 400 and private.json()["error"] == "blocked_address"
    bad_filter = await api.post("/webhooks", json={"url": "https://crm.example.com/h", "events": ["a b"]})
    assert bad_filter.status_code == 400

    created = await api.post(
        "/webhooks",
        json={"url": "https://crm.example.com/h?token=abc", "events": ["purchase", "payment.*"], "description": "CRM"},
    )
    assert created.status_code == 201
    body = created.json()
    secret, endpoint_id = body["secret"], body["id"]
    assert secret.startswith("whsec_") and body["events"] == ["purchase", "payment.*"]
    listed = (await api.get("/webhooks")).json()
    assert len(listed) == 1 and "secret" not in listed[0]
    async with ctx.session_factory() as s:
        row = await s.get(WebhookEndpoint, endpoint_id)
        assert row is not None and row.secret != secret and row.secret.startswith("enc:")

    test = (await api.post(f"/webhooks/{endpoint_id}/test")).json()
    assert test["ok"] and test["delivery"]["status"] == "sent" and test["delivery"]["event_type"] == "webhook.test"
    url, sent_body, headers = FakeSender.calls[-1]
    assert url == "https://crm.example.com/h?token=abc"
    assert webhooks.verify(secret, headers["X-Webhook-Timestamp"], sent_body, headers["X-Webhook-Signature"])

    rotated = (await api.post(f"/webhooks/{endpoint_id}/rotate-secret")).json()["secret"]
    assert rotated != secret

    FakeSender.status = 503
    async with ctx.session_factory.begin() as s:
        await events.track(s, "payment.succeeded", user_id=5, amount_kop=1000, provider="stars")
    deliveries = (await api.get(f"/webhooks/{endpoint_id}/deliveries")).json()
    assert [d["event_type"] for d in deliveries] == ["payment.succeeded", "webhook.test"]
    pending_id = deliveries[0]["id"]
    detail = (await api.get(f"/webhooks/deliveries/{pending_id}")).json()
    assert json.loads(detail["payload"])["data"] == {"provider": "stars"}
    failed = (await api.post(f"/webhooks/deliveries/{pending_id}/redeliver")).json()
    assert not failed["ok"] and failed["status_code"] == 503 and failed["delivery"]["status"] == "failed"
    FakeSender.status = 200
    again = (await api.post(f"/webhooks/deliveries/{pending_id}/redeliver")).json()
    assert again["ok"] and again["delivery"]["status"] == "sent" and again["delivery"]["attempts"] == 1
    assert webhooks.verify(
        rotated,
        FakeSender.calls[-1][2]["X-Webhook-Timestamp"],
        FakeSender.calls[-1][1],
        FakeSender.calls[-1][2]["X-Webhook-Signature"],
    )
    only_sent = (await api.get(f"/webhooks/{endpoint_id}/deliveries", params={"status": "sent"})).json()
    assert len(only_sent) == 2

    updated = (await api.patch(f"/webhooks/{endpoint_id}", json={"is_active": False, "events": ["*"]})).json()
    assert updated["is_active"] is False and updated["events"] == ["*"] and updated["disabled_reason"]
    assert (await api.post(f"/webhooks/{endpoint_id}/test")).status_code == 400
    moved = await api.patch(f"/webhooks/{endpoint_id}", json={"url": "https://internal.example.com/"})
    assert moved.status_code == 400

    assert (await api.delete(f"/webhooks/{endpoint_id}")).status_code == 200
    assert (await api.get(f"/webhooks/{endpoint_id}")).status_code == 404
    async with ctx.session_factory() as s:
        actions = (await s.scalars(select(AdminAuditLog.action).order_by(AdminAuditLog.id))).all()
        details = " ".join(d or "" for d in (await s.scalars(select(AdminAuditLog.details))).all())
    assert {
        "webhook.create",
        "webhook.test",
        "webhook.rotate_secret",
        "webhook.redeliver",
        "webhook.update",
        "webhook.delete",
    } <= set(actions)
    assert "token=abc" not in details and secret not in details and rotated not in details


async def test_missing_rows(api: Api) -> None:
    assert (await api.post("/webhooks/99/test")).status_code == 404
    assert (await api.post("/webhooks/99/rotate-secret")).status_code == 404
    assert (await api.post("/webhooks/deliveries/99/redeliver")).status_code == 404
    assert (await api.patch("/webhooks/99", json={"description": "x"})).status_code == 404
