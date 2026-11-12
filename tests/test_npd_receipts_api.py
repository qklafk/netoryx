"""Web admin section and background job of "Мой налог" receipts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer
from sqlalchemy import select

from app.db.models import Admin, AdminAuditLog, AdminRole, PaymentStatus, TaxReceipt
from app.db.session import SessionFactory
from app.modules.npd_receipts import admin_api, jobs
from app.modules.npd_receipts import module as npd_module
from app.services import tax_receipts
from app.services.npd import NpdClient
from app.services.payments import service
from app.web.app import create_web_app
from tests.test_mod_personal_api import IP, Api
from tests.test_mod_personal_support import OWNER, ModCtx, make_ctx
from tests.test_npd_receipts import USER_ID, FakeCabinet, _seed
from tests.test_web_support import AsgiClient

VIEWER = 8002


@pytest.fixture
async def cabinet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeCabinet]:
    fake = FakeCabinet()
    server = TestServer(fake.app())
    await server.start_server()
    url = str(server.make_url("/api"))

    def client(device: str, **_: Any) -> NpdClient:
        return NpdClient(device, base_url=url, timeout=5)

    monkeypatch.setattr(admin_api, "_client", client)
    monkeypatch.setattr(tax_receipts, "NpdClient", client)
    try:
        yield fake
    finally:
        await server.close()


@pytest.fixture
async def setup(session_factory: SessionFactory, tmp_path: Path) -> tuple[Api, ModCtx]:
    ctx, _ = make_ctx(
        session_factory, tmp_path, [npd_module], npd_enabled=True, npd_inn="500100732259", npd_password="secret"
    )
    await _seed(session_factory)
    async with session_factory.begin() as session:
        from tests.test_subs_support import add_user

        await add_user(session, OWNER)
        await add_user(session, VIEWER)
        session.add(Admin(tg_id=VIEWER, role=AdminRole.VIEWER))
    app = create_web_app(ctx, [npd_module])  # type: ignore[arg-type]
    return Api(ctx, AsgiClient(app, client_ip=IP, host="bot.example.com")), ctx


async def test_admin_section(setup: tuple[Api, ModCtx], cabinet: FakeCabinet, session_factory: SessionFactory) -> None:
    api, ctx = setup
    assert (await api.get("/api/admin/npd_receipts")).status == 401
    await api.login()
    await service.credit(session_factory, ctx.settings, "pay-1")

    overview = (await api.get("/api/admin/npd_receipts/overview")).json()
    assert overview["enabled"] and not overview["logged_in"] and overview["counts"] == {"pending": 1}

    # SMS login stores the tokens encrypted
    started = (await api.post("/api/admin/npd_receipts/auth/sms/start", {"phone": "79001234567"})).json()
    assert started["challenge_token"] == "ch-1"
    bad = await api.post(
        "/api/admin/npd_receipts/auth/sms/verify", {"phone": "79001234567", "challenge_token": "ch-1", "code": "0000"}
    )
    assert bad.status == 400
    ok = await api.post(
        "/api/admin/npd_receipts/auth/sms/verify", {"phone": "79001234567", "challenge_token": "ch-1", "code": "1234"}
    )
    assert ok.json() == {"inn": "500100732259"}
    assert (await api.get("/api/admin/npd_receipts/overview")).json()["logged_in"]

    await jobs.process_job(ctx)  # type: ignore[arg-type]
    page = (await api.get("/api/admin/npd_receipts?status=registered")).json()
    assert page["total"] == 1 and page["items"][0]["receipt_uuid"] == "rcpt0001"
    assert not [r for r, _ in cabinet.requests if r == "POST /v1/auth/lkfl"]  # the SMS session was used
    receipt_id = page["items"][0]["id"]

    cancelled = (await api.post(f"/api/admin/npd_receipts/{receipt_id}/cancel")).json()
    assert cancelled["status"] == "cancel_pending"
    await jobs.process_job(ctx)  # type: ignore[arg-type]
    assert cabinet.incomes[0]["cancellationInfo"] is not None
    assert (await api.post("/api/admin/npd_receipts/999/retry")).status == 404
    assert (await api.post("/api/admin/npd_receipts/auth/logout")).status == 200
    async with session_factory() as session:
        actions = set(await session.scalars(select(AdminAuditLog.action)))
    assert {"npd.login", "npd.cancel", "npd.logout"} <= actions


async def test_viewer_cannot_change_or_log_in(setup: tuple[Api, ModCtx], cabinet: FakeCabinet) -> None:
    api, _ctx = setup
    await api.login(VIEWER)
    assert (await api.get("/api/admin/npd_receipts")).status == 200
    assert (await api.post("/api/admin/npd_receipts/1/retry")).status == 403
    assert (await api.post("/api/admin/npd_receipts/auth/password")).status == 403
    assert not cabinet.requests


async def test_job_sends_receipt_link_once(
    setup: tuple[Api, ModCtx], cabinet: FakeCabinet, session_factory: SessionFactory
) -> None:
    _api, ctx = setup
    sent: list[tuple[int, str]] = []

    async def fake_send(bot: Any, chat_id: int, text: str, **_: Any) -> bool:
        sent.append((chat_id, text))
        return True

    jobs_safe_send = jobs.safe_send
    try:
        jobs.safe_send = fake_send  # type: ignore[assignment]
        await service.credit(session_factory, ctx.settings, "pay-1")
        await jobs.process_job(ctx)  # type: ignore[arg-type]
        await jobs.process_job(ctx)  # type: ignore[arg-type]
    finally:
        jobs.safe_send = jobs_safe_send  # type: ignore[assignment]
    assert len(sent) == 1 and sent[0][0] == USER_ID and "/receipt/500100732259/rcpt0001/print" in sent[0][1]
    async with session_factory() as session:
        row = (await session.scalars(select(TaxReceipt))).one()
    assert row.user_notified
    assert await service.mark_status(session_factory, "pay-1", PaymentStatus.REFUNDED)
