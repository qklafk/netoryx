"""Bot admin money sections: payments, promo codes and partner payouts."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import RefundStarPayment, SendMessage
from sqlalchemy import select, update

from app.db.models import (
    AdminAuditLog,
    AdminRole,
    LedgerAccount,
    Payment,
    PaymentStatus,
    PromoCode,
    PromoKind,
    TxKind,
    User,
    Withdrawal,
    WithdrawalStatus,
)
from app.db.session import SessionFactory
from app.modules.admin_payments import ops as payment_ops
from app.services import wallet
from app.services.hooks import HookOutcome
from app.services.payments.base import ProviderStatus
from app.services.payments.service import CreditResult
from tests.test_mod_admin_support import USER, Builder, add_admin, add_user

PID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def make(session_factory: SessionFactory, tmp_path: Path) -> Iterator[Builder]:
    builder = Builder(session_factory, tmp_path)
    yield builder
    builder.close()


@pytest.fixture
def hook_calls(monkeypatch: pytest.MonkeyPatch) -> list[CreditResult]:
    calls: list[CreditResult] = []

    async def fake_hook(ctx: Any, result: CreditResult) -> HookOutcome:
        calls.append(result)
        return HookOutcome(result.payment_id, first_run=True, purchase_id=1, purchase_new=True, notified_users=[USER])

    monkeypatch.setattr(payment_ops.hooks, "on_payment_credited", fake_hook)
    return calls


async def _payment(sf: SessionFactory, **fields: Any) -> Payment:
    values: dict[str, Any] = {
        "id": PID,
        "provider": "stars",
        "user_id": USER,
        "amount_kop": 15000,
        "status": PaymentStatus.PENDING,
        "provider_amount": "100",
        "provider_currency": "XTR",
    }
    values.update(fields)
    async with sf.begin() as s:
        payment = Payment(**values)
        s.add(payment)
    return payment


async def _get_payment(sf: SessionFactory, pid: str = PID) -> Payment:
    async with sf() as s:
        payment = await s.get(Payment, pid)
    assert payment is not None
    return payment


async def _balance(sf: SessionFactory, uid: int = USER, account: LedgerAccount = LedgerAccount.MAIN) -> int:
    async with sf() as s:
        return await wallet.balance(s, uid, account)


async def _audit(sf: SessionFactory) -> list[str]:
    async with sf() as s:
        return list(await s.scalars(select(AdminAuditLog.action).order_by(AdminAuditLog.id)))


# --- payments --------------------------------------------------------------------------------------------------------


async def test_payment_list_filters_and_details(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await _payment(session_factory)
    await _payment(session_factory, id="p-ok", status=PaymentStatus.SUCCEEDED, provider_payment_id="ch-1")
    await h.press("nav:adm_pay:")
    labels = list(h.buttons())
    assert sum("stars" in label for label in labels) == 2
    await h.press("apl:open:0")
    assert sum("stars" in label for label in h.buttons()) == 1
    await h.press(f"apay:v:{PID}:")
    text = h.last_text()
    assert PID in text and "150 ₽" in text and "100 XTR" in text and "ожидает" in text
    assert "✅ Отметить оплаченным" in h.buttons()


async def test_payment_search(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await _payment(session_factory, provider_payment_id="charge-xyz")
    await h.press("apl:search:0")
    await h.message("charge-xyz")
    assert PID in h.last_text()
    await h.press("apl:search:0")
    await h.message(str(USER))
    assert f"пользователь {USER}" in h.last_text()
    await h.press("apl:search:0")
    await h.message("nope")
    assert "не нашёлся" in h.last_text()


async def test_mark_paid_credits_once_and_runs_hook(
    make: Builder, session_factory: SessionFactory, hook_calls: list[CreditResult]
) -> None:
    h = make()
    await add_user(session_factory)
    await _payment(session_factory, status=PaymentStatus.EXPIRED)
    await h.press(f"apay:paid:{PID}:")
    assert "Отметить платёж оплаченным" in h.last_text()
    await h.press(f"apay:paid_ok:{PID}:")
    assert "Платёж зачислен" in h.last_text() and "Тариф оформлен" in h.last_text()
    await h.press(f"apay:paid_ok:{PID}:")
    assert "уже зачислен" in h.last_text()
    assert await _balance(session_factory) == 15000
    assert len(hook_calls) == 1
    assert (await _get_payment(session_factory)).status == PaymentStatus.SUCCEEDED
    assert "payment.mark_paid" in await _audit(session_factory)


async def test_rerun_hook_for_paid_payment(
    make: Builder, session_factory: SessionFactory, hook_calls: list[CreditResult]
) -> None:
    h = make()
    await add_user(session_factory, balance_kop=15000)
    await _payment(session_factory, status=PaymentStatus.SUCCEEDED, plan_id=None, credited_at=datetime.now(UTC))
    await h.press(f"apay:hook:{PID}:")
    assert "Обработка выполнена повторно" in h.last_text()
    assert hook_calls and hook_calls[0].payment_id == PID and hook_calls[0].new_balance_kop == 15000
    hook_calls.clear()
    await _payment(session_factory, id="p-open")
    await h.press("apay:hook:p-open:")
    assert "только для оплаченного" in h.last_text() and not hook_calls


async def test_viewer_cannot_change_payments(
    make: Builder, session_factory: SessionFactory, hook_calls: list[CreditResult]
) -> None:
    h = make()
    await add_admin(session_factory, 2002, AdminRole.VIEWER)
    await add_user(session_factory)
    await _payment(session_factory)
    await h.press(f"apay:v:{PID}:", uid=2002)
    assert "✅ Отметить оплаченным" not in h.buttons()
    await h.press(f"apay:paid_ok:{PID}:", uid=2002)
    assert (await _get_payment(session_factory)).status == PaymentStatus.PENDING


class PollingGateway:
    key = "cryptobot"
    supports_polling = True

    def __init__(self, status: ProviderStatus) -> None:
        self.status = status

    async def fetch(self, payment: Payment) -> ProviderStatus:
        return self.status


async def test_check_with_provider(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    h.ctx.gateways.items["cryptobot"] = PollingGateway(ProviderStatus(PaymentStatus.SUCCEEDED, 15000, "inv-1"))
    await add_user(session_factory)
    await _payment(session_factory, provider="cryptobot")
    await h.press(f"apay:v:{PID}:")
    assert "🔄 Проверить у провайдера" in h.buttons()
    await h.press(f"apay:chk:{PID}:")
    assert "оплачен" in h.last_text()
    assert await _balance(session_factory) == 15000
    assert h.ctx.credited and h.ctx.credited[0].payment_id == PID


async def test_stars_refund_with_debit(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await _payment(session_factory, status=PaymentStatus.PENDING)
    async with session_factory.begin() as s:
        await s.execute(update(Payment).values(status=PaymentStatus.SUCCEEDED, provider_payment_id="charge-1"))
        await wallet.apply(s, USER, 10000, TxKind.PAYMENT, PID)
    await h.press(f"apay:rf:{PID}:")
    assert "Вернуть 100" in h.last_text()
    await h.press(f"apay:rf_ok:{PID}:d")
    await h.press(f"apay:rf_ok:{PID}:d")
    refunds = h.calls(RefundStarPayment)
    assert len(refunds) == 1 and refunds[0].telegram_payment_charge_id == "charge-1" and refunds[0].user_id == USER
    assert (await _get_payment(session_factory)).status == PaymentStatus.REFUNDED
    assert await _balance(session_factory) == 0  # only what was left on the balance
    assert "Списано с баланса: 100 ₽" in h.last_text()


async def test_stars_refund_failure_can_be_retried(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    await _payment(session_factory, status=PaymentStatus.SUCCEEDED, provider_payment_id="charge-2")
    h.api.fail(
        RefundStarPayment,
        TelegramBadRequest(
            method=RefundStarPayment(user_id=USER, telegram_payment_charge_id="x"), message="CHARGE_NOT_FOUND"
        ),
    )
    await h.press(f"apay:rf_ok:{PID}:k")
    assert "не принял возврат" in h.last_text() and "CHARGE_NOT_FOUND" in h.last_text()
    await h.press(f"apay:rf_ok:{PID}:k")
    assert (await _get_payment(session_factory)).status == PaymentStatus.REFUNDED


# --- promo codes -----------------------------------------------------------------------------------------------------


async def test_create_promo_code_wizard(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await h.press("nav:adm_promo:")
    assert "Промокодов пока нет" in h.last_text()
    await h.press("admc:new:0:0")
    await h.message("spring-2026")
    await h.message("100")
    await h.message("7")
    await h.message("50")
    await h.message("30")
    await h.press(h.buttons()["✅ Да"])
    text = h.last_text()
    assert "Промокод создан" in text and "SPRING-2026" in text and "+100 ₽ +7 дней" in text
    assert "https://t.me/test_bot?start=promo_SPRING-2026" in text
    async with session_factory() as s:
        code = await s.scalar(select(PromoCode))
    assert code is not None and code.max_uses == 50 and code.only_new_users and code.expires_at is not None
    assert "promo.create" in await _audit(session_factory)


async def test_promo_random_code_validation_and_duplicates(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await h.press("admc:new:0:0")
    await h.press(h.buttons()["🎲 Сгенерировать"])
    await h.message("0")
    await h.message("0")  # no bonus at all: refused
    assert "не подходит" in h.last_text()
    await h.message("3")
    await h.message("0")
    await h.message("0")
    await h.press(h.buttons()["❌ Нет"])
    assert "Промокод создан" in h.last_text()

    await h.press("admc:new:0:0")
    async with session_factory() as s:
        existing = await s.scalar(select(PromoCode.code))
    for answer in (str(existing), "10", "0", "1", "0"):
        await h.message(answer)
    await h.press(h.buttons()["❌ Нет"])
    assert "уже есть" in h.last_text()


async def test_deactivate_promo_and_gift_list(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    async with session_factory.begin() as s:
        code = PromoCode(code="SALE", kind=PromoKind.PROMO, bonus_kop=1000, max_uses=0, uses=3)
        gift = PromoCode(code="GIFT123", kind=PromoKind.GIFT, bonus_days=30, max_uses=1, uses=1)
        s.add_all([code, gift])
    await h.press(f"admc:card:{code.id}:0")
    assert "работает" in h.last_text() and "3/∞" in h.last_text()
    await h.press(f"admc:off:{code.id}:0")
    assert "отключён" in h.last_text()
    async with session_factory() as s:
        stored = await s.get(PromoCode, code.id)
    assert stored is not None and not stored.is_active
    await h.press("admc:gifts:0:0")
    assert any("GIFT123" in label for label in h.buttons())


# --- withdrawals -----------------------------------------------------------------------------------------------------


async def _withdrawal(sf: SessionFactory, amount: int = 50000, **fields: Any) -> Withdrawal:
    async with sf.begin() as s:
        withdrawal = Withdrawal(
            user_id=USER,
            amount_kop=amount,
            method="card",
            details="2200 0000 0000 0000",
            status=WithdrawalStatus.PENDING,
            **fields,
        )
        s.add(withdrawal)
        await s.flush()
        await wallet.apply(s, USER, amount, TxKind.REFERRAL_COMMISSION, "L1:x", account=LedgerAccount.REFERRAL)
        await wallet.apply(
            s, USER, -amount, TxKind.WITHDRAWAL, f"withdrawal:{withdrawal.id}", account=LedgerAccount.REFERRAL
        )
    return withdrawal


async def test_withdrawal_queue_approve_and_paid(make: Builder, session_factory: SessionFactory) -> None:
    h = make(referral_reward_mode="partner", withdrawals_enabled=True)
    await add_user(session_factory, created_at=datetime.now(UTC))
    withdrawal = await _withdrawal(session_factory)
    await h.press("nav:adm_wd:")
    assert "В очереди: 1" in h.last_text()
    await h.press(f"admw:card:{withdrawal.id}:0")
    text = h.last_text()
    assert "2200 0000 0000 0000" in text and "аккаунту всего 0 дн." in text
    await h.press(f"admw:approve:{withdrawal.id}:0")
    assert "одобрена" in h.last_text()
    await h.press(f"admw:paid:{withdrawal.id}:0")
    await h.press(h.buttons()["✅ Да"])
    assert "Выплата отмечена" in h.last_text()
    async with session_factory() as s:
        stored = await s.get(Withdrawal, withdrawal.id)
    assert stored is not None and stored.status == WithdrawalStatus.PAID
    notices = [c.text for c in h.calls(SendMessage) if c.chat_id == USER]
    assert any("одобрена" in t for t in notices) and any("отправлена" in t for t in notices)


async def test_withdrawal_reject_with_comment_refunds(make: Builder, session_factory: SessionFactory) -> None:
    h = make(referral_reward_mode="partner")
    await add_user(session_factory)
    withdrawal = await _withdrawal(session_factory)
    assert await _balance(session_factory, account=LedgerAccount.REFERRAL) == 0
    await h.press(f"admw:reject:{withdrawal.id}:0")
    await h.message("Реквизиты не совпадают с именем")
    assert "отклонена" in h.last_text()
    assert await _balance(session_factory, account=LedgerAccount.REFERRAL) == 50000
    notice = [c.text for c in h.calls(SendMessage) if c.chat_id == USER][-1]
    assert "Реквизиты не совпадают" in notice
    await h.press(f"admw:approve:{withdrawal.id}:0")
    assert "уже обработал" in h.last_text()


async def test_withdrawal_reject_without_comment(make: Builder, session_factory: SessionFactory) -> None:
    h = make()
    await add_user(session_factory)
    withdrawal = await _withdrawal(session_factory)
    await h.press(f"admw:reject:{withdrawal.id}:0")
    await h.press(h.buttons()["✖️ Отклонить без комментария"])
    async with session_factory() as s:
        stored = await s.get(Withdrawal, withdrawal.id)
        user = await s.get(User, USER)
    assert stored is not None and stored.status == WithdrawalStatus.REJECTED and stored.admin_comment is None
    assert user is not None and user.referral_balance_kop == 50000
    await h.press("admw:hist:0:0")
    assert any(f"#{withdrawal.id}" in label for label in h.buttons())
