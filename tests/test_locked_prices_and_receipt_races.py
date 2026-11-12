"""Money-flow regressions: locked invoice prices and receipt registration races."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import Payment, PaymentStatus, Purchase
from app.db.session import SessionFactory
from app.services import hooks, plans, provisioning, users, wallet
from app.services.payments import service
from tests.test_hooks import _balance, _ctx, _plan, _register

BUYER = 7101


@pytest.fixture(autouse=True)
def _no_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sync(session_factory: Any, registry: Any, settings: Any, sub_id: int, **kw: Any) -> Any:
        return provisioning.SyncResult(subscription_id=sub_id)

    monkeypatch.setattr(provisioning, "sync_subscription", fake_sync)


async def test_payment_with_locked_price_above_a_lowered_plan_price_still_buys_the_plan(
    session_factory: SessionFactory, tmp_path: Path
) -> None:
    ctx, _api = _ctx(session_factory, tmp_path)
    await _register(ctx, BUYER)
    plan_id = await _plan(ctx, price_rub=150)
    pid = str(uuid.uuid4())
    async with ctx.session_factory.begin() as s:
        s.add(
            Payment(
                id=pid,
                provider="cryptobot",
                user_id=BUYER,
                amount_kop=15000,
                plan_id=plan_id,
                plan_price_kop=15000,
                status=PaymentStatus.PENDING,
                provider_payment_id="prov-lock",
            )
        )
    # The operator lowers the price after the invoice was issued.
    async with ctx.session_factory.begin() as s:
        plan = await plans.get(s, plan_id)
        assert plan is not None
        plan.price_kop = 10000
    result = await service.credit(ctx.session_factory, ctx.settings, pid)
    assert result is not None
    outcome = await hooks.on_payment_credited(ctx, result)  # type: ignore[arg-type]
    assert outcome.error is None and outcome.purchase_id is not None
    async with ctx.session_factory() as s:
        purchase = await s.scalar(select(Purchase).where(Purchase.id == outcome.purchase_id))
        assert purchase is not None and purchase.price_kop == 10000
    assert await _balance(ctx, BUYER) == 5000
    _ = (users, wallet)


async def test_overlapping_receipt_runs_register_an_income_once(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from app.services import tax_receipts
    from app.services.npd import NpdClient, NpdSession
    from tests.test_npd_receipts import BOX, INN, _receipts, _seed, _settings

    settings = _settings()
    await _seed(session_factory)
    await service.credit(session_factory, settings, "pay-1")
    await tax_receipts.save_session(session_factory, BOX, NpdSession("tok", "ref", INN))
    calls: list[int] = []
    gate = asyncio.Event()

    async def create_income(self: Any, token: str, *args: Any, **kw: Any) -> str:
        calls.append(1)
        await gate.wait()
        return f"rcpt{len(calls):04d}"

    monkeypatch.setattr(NpdClient, "create_income", create_income)
    real_due = tax_receipts._due
    seen = 0
    both_read = asyncio.Event()

    async def due_after_both_read(*args: Any, **kw: Any) -> Any:
        nonlocal seen
        rows = await real_due(*args, **kw)
        seen += 1
        if seen == 2:
            both_read.set()
        await both_read.wait()
        return rows

    monkeypatch.setattr(tax_receipts, "_due", due_after_both_read)

    async def release() -> None:
        await asyncio.sleep(0.5)
        gate.set()

    await asyncio.gather(
        tax_receipts.process_due(session_factory, settings, BOX),
        tax_receipts.process_due(session_factory, settings, BOX),
        release(),
    )
    assert len(calls) == 1
    (row,) = await _receipts(session_factory)
    assert row.receipt_uuid == "rcpt0001"
