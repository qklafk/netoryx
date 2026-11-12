"""Wallet module: balance screen, top-up amounts (quick and typed), operations history."""

from __future__ import annotations

import time
import uuid
from typing import Any

from aiogram.fsm.storage.base import StorageKey

from app.db.models import TxKind
from app.keyboards.callbacks import Nav
from app.modules.payments.callbacks import PayCb
from app.modules.payments.helpers import parse_rubles
from app.modules.wallet.bot import WalletCb, WalletInput
from app.services import wallet
from tests.test_mod_money_support import BOT_ID, Harness, buttons, callback_datas, money, register

__all__ = ["money"]

UID = 6001


async def test_balance_screen(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID, balance_kop=12_345)
    await h.press(UID, Nav(to="balance").pack())
    text = h.api.screens()[-1]
    assert "123,45 ₽" in text and "Партнёрский" not in text
    datas = callback_datas(h.api.last_markup())
    assert WalletCb(a="top").pack() in datas and WalletCb(a="hist").pack() in datas


async def test_partner_balance_is_shown_in_partner_mode(money: Any) -> None:
    h: Harness = money(referral_reward_mode="partner")
    await register(h.ctx, UID, referral_kop=7_000)
    await h.say(UID, "/balance")
    assert "Партнёрский баланс: <b>70 ₽</b>" in h.api.screens()[-1]
    assert Nav(to="referral").pack() in callback_datas(h.api.last_markup())


async def test_no_topup_without_payment_methods(money: Any) -> None:
    h: Harness = money(gateways=[])
    await register(h.ctx, UID)
    await h.press(UID, Nav(to="balance").pack())
    assert WalletCb(a="top").pack() not in callback_datas(h.api.last_markup())
    await h.press(UID, Nav(to="topup").pack())
    assert "недоступно" in h.api.screens()[-1]


async def test_quick_amounts_open_the_method_picker(money: Any) -> None:
    h: Harness = money(topup_quick_amounts_rub=[10, 300, 500, 300], min_topup_rub=50)
    await register(h.ctx, UID)
    await h.press(UID, WalletCb(a="top").pack())
    datas = callback_datas(h.api.last_markup())
    amounts = [PayCb.unpack(d).amt for d in datas if d.startswith("pay:")]
    assert amounts == [30_000, 50_000]  # below the minimum dropped, duplicates merged


async def test_custom_amount(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.press(UID, WalletCb(a="custom").pack())
    await h.say(UID, "10")  # below the minimum
    assert "Не получилось" in h.api.screens()[-1]
    await h.say(UID, "1 500 ₽")
    text = h.api.screens()[-1]
    assert "1 500 ₽" in text and "способ оплаты" in text
    assert any(d.startswith("pay:go:t:150000") for d in callback_datas(h.api.last_markup()))
    await h.say(UID, "700")  # the input step is over: not an amount any more
    assert "700 ₽" not in h.api.screens()[-1]


async def test_commands_pass_through_an_input_step(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID, balance_kop=100)
    await h.press(UID, WalletCb(a="custom").pack())
    await h.say(UID, "/balance")
    assert "Ваш баланс" in h.api.screens()[-1]


async def test_expired_input_step_is_ignored(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.press(UID, WalletCb(a="custom").pack())
    key = StorageKey(bot_id=BOT_ID, chat_id=UID, user_id=UID)
    storage = h.ctx.dispatcher.fsm.storage
    await storage.set_data(key, {"_input_at": time.time() - 3600})
    sent = len(h.api.screens())
    await h.say(UID, "500")
    assert len(h.api.screens()) == sent  # nobody answered: the step expired
    assert await storage.get_state(key) is None
    assert WalletInput.amount.state  # the step exists


async def test_history_pages(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    async with h.ctx.session_factory.begin() as session:
        for i in range(12):
            await wallet.apply(session, UID, 100 * (i + 1), TxKind.PAYMENT, f"p-{uuid.uuid4().hex}")
        await wallet.apply(session, UID, -50, TxKind.PURCHASE, "buy-1")
    await h.press(UID, WalletCb(a="hist").pack())
    first = h.api.screens()[-1]
    assert "−0,50 ₽" in first and "Оплата подписки" in first
    older = [d for d in callback_datas(h.api.last_markup()) if d.startswith("wal:hist:") and d != "wal:hist:0"]
    assert len(older) == 1
    await h.press(UID, older[0])
    second = h.api.screens()[-1]
    assert second.count("Пополнение") == 3
    assert WalletCb(a="hist").pack() in callback_datas(h.api.last_markup())


async def test_empty_history(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.press(UID, WalletCb(a="hist").pack())
    assert "пока не было" in h.api.screens()[-1]
    assert all("hist" not in d or d == "wal:hist:0" for d in callback_datas(h.api.last_markup()))
    assert buttons(h.api.last_markup())


def test_parse_rubles() -> None:
    assert parse_rubles(" 1 500 ₽ ") == 1500
    assert parse_rubles("500р") == 500
    assert parse_rubles("500 руб") == 500
    assert parse_rubles("12.5") is None
    assert parse_rubles("-5") is None
    assert parse_rubles("٣") is None
    assert parse_rubles("") is None
