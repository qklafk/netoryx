"""Referral module: link and terms per reward mode, stats, QR, partner transfers and payout requests; texts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aiogram.methods import AnswerCallbackQuery, SendPhoto
from sqlalchemy import select

from app.core.i18n import placeholders
from app.db.models import User, Withdrawal, WithdrawalStatus
from app.keyboards.callbacks import Nav
from app.modules.referral import service
from app.modules.referral.bot import RefCb
from app.services import partner, referrals, users
from tests.test_mod_money_support import (
    MONEY_MODULES,
    Harness,
    balance,
    buttons,
    callback_datas,
    money,
    money_i18n,
    register,
)

__all__ = ["money"]

UID = 8001
FRIEND = 8002


async def _invite(h: Harness, inviter: int, invitee: int) -> None:
    async with h.ctx.session_factory.begin() as session:
        owner = await session.get(User, inviter)
        assert owner is not None
        user, created = await users.get_or_create(session, h.ctx.settings, invitee, None, "Bob", "ru")
        await referrals.apply_on_registration(session, h.ctx.settings, user, owner.referral_code, is_new=created)


async def test_balance_mode_screen(money: Any) -> None:
    h: Harness = money(referral_invitee_bonus_rub=50, referral_levels_percent=[10])
    user = await register(h.ctx, UID)
    await _invite(h, UID, FRIEND)
    await h.press(UID, Nav(to="referral").pack())
    text = h.api.screens()[-1]
    assert f"https://t.me/test_bot?start=ref_{user.referral_code}" in text
    assert "Друг получит 50 ₽" in text and "10% с каждой его оплаты" in text
    assert "Приглашено: <b>1</b>" in text and "реальных оплат" in text
    markup = h.api.last_markup()
    assert any((b.url or "").startswith("https://t.me/share/url?") for b in buttons(markup))
    assert any(b.copy_text is not None for b in buttons(markup))
    assert RefCb(a="tr").pack() not in callback_datas(markup)  # nothing to transfer


async def test_days_mode_terms_and_levels(money: Any) -> None:
    h: Harness = money(
        referral_reward_mode="days",
        referral_levels_percent=[20, 5],
        referral_invitee_bonus_days=3,
        referral_inviter_bonus_days=7,
    )
    await register(h.ctx, UID)
    await h.say(UID, "/ref")
    text = h.api.screens()[-1]
    assert "3 дня" in text and "7 дней" in text and "после его первой оплаты" in text
    assert "1-й уровень — 20%, 2-й уровень — 5%" in text
    assert RefCb(a="lvl").pack() in callback_datas(h.api.last_markup())
    await h.press(UID, RefCb(a="lvl").pack())
    assert "<b>2-й уровень</b> · 5%" in h.api.screens()[-1]


async def test_qr_code(money: Any) -> None:
    h: Harness = money()
    await register(h.ctx, UID)
    await h.press(UID, RefCb(a="qr").pack())
    [photo] = h.api.of(SendPhoto)
    assert "start=ref_" in (photo.caption or "")


async def test_partner_transfer_all_once(money: Any) -> None:
    h: Harness = money(referral_reward_mode="partner")
    await register(h.ctx, UID, balance_kop=1_000, referral_kop=30_000)
    await h.press(UID, Nav(to="referral").pack())
    assert "Партнёрский баланс: <b>300 ₽</b>" in h.api.screens()[-1]
    await h.press(UID, RefCb(a="tr").pack())
    [transfer] = [d for d in callback_datas(h.api.last_markup()) if d.startswith("ref:tro:")]
    await h.press(UID, transfer)
    await h.press(UID, transfer)  # double tap
    assert await balance(h.ctx, UID) == (31_000, 0)
    assert "уже выполнен" in h.api.screens()[-1]


async def test_partner_transfer_custom_amount(money: Any) -> None:
    h: Harness = money(referral_reward_mode="partner")
    await register(h.ctx, UID, referral_kop=30_000)
    await h.press(UID, RefCb(a="trc").pack())
    await h.say(UID, "500")
    assert "не больше" in h.api.screens()[-1]
    await h.say(UID, "100")
    [confirm] = [d for d in callback_datas(h.api.last_markup()) if d.startswith("ref:tro:10000:")]
    await h.press(UID, confirm)
    assert await balance(h.ctx, UID) == (10_000, 20_000)
    await h.press(UID, RefCb(a="tro", v=10_000).pack())  # forged: no one-shot token
    assert await balance(h.ctx, UID) == (10_000, 20_000)


async def test_withdrawal_request_flow(money: Any) -> None:
    h: Harness = money(referral_reward_mode="partner", withdrawals_enabled=True, min_withdrawal_rub=100)
    await register(h.ctx, UID, referral_kop=50_000)
    await h.press(UID, Nav(to="referral").pack())
    assert RefCb(a="wd").pack() in callback_datas(h.api.last_markup())
    await h.press(UID, RefCb(a="wd").pack())
    assert RefCb(a="wdm", s="card").pack() in callback_datas(h.api.last_markup())
    await h.press(UID, RefCb(a="wdm", s="card").pack())
    await h.say(UID, "50")  # below the minimum
    assert "не получится" in h.api.screens()[-1]
    await h.say(UID, "300")
    assert "номер карты" in h.api.screens()[-1]
    await h.say(UID, "  2200 0000   0000 0000 ")
    text = h.api.screens()[-1]
    assert "300 ₽" in text and "2200 0000 0000 0000" in text
    await h.press(UID, RefCb(a="wdok").pack())
    assert "отправлена" in h.api.screens()[-1]
    await h.press(UID, RefCb(a="wdok").pack())  # the step is over

    async with h.ctx.session_factory() as session:
        rows = list((await session.scalars(select(Withdrawal))).all())
    assert len(rows) == 1 and rows[0].status is WithdrawalStatus.PENDING and rows[0].method == "card"
    assert await balance(h.ctx, UID) == (0, 20_000)
    assert len(h.admin_notes) == 1 and "2200 0000 0000 0000" in h.admin_notes[0][1]

    await h.press(UID, RefCb(a="wd").pack())
    assert "уже есть заявка" in h.api.screens()[-1]
    await h.press(UID, RefCb(a="wdl").pack())
    assert "на рассмотрении" in h.api.screens()[-1]


async def test_withdraw_everything_button(money: Any) -> None:
    h: Harness = money(referral_reward_mode="partner", withdrawals_enabled=True, min_withdrawal_rub=100)
    await register(h.ctx, UID, referral_kop=20_000)
    await h.press(UID, RefCb(a="wda", v=20_000).pack())  # outside the step
    assert h.api.of(AnswerCallbackQuery)[-1].show_alert
    await h.press(UID, RefCb(a="wdm", s="sbp").pack())
    await h.press(UID, RefCb(a="wda", v=20_000).pack())
    assert "СБП" in h.api.screens()[-1]
    await h.say(UID, "+7 900 000-00-00 Т-Банк")
    await h.press(UID, RefCb(a="wdok").pack())
    assert await balance(h.ctx, UID) == (0, 0)


async def test_withdrawals_unavailable_or_too_little(money: Any) -> None:
    h: Harness = money(referral_reward_mode="partner", withdrawals_enabled=True, min_withdrawal_rub=500)
    await register(h.ctx, UID, referral_kop=10_000)
    await h.press(UID, RefCb(a="wd").pack())
    assert "Минимальная сумма вывода" in h.api.screens()[-1]
    h.set(withdrawals_enabled=False)
    await h.press(UID, RefCb(a="wdm", s="card").pack())
    await h.press(UID, Nav(to="referral").pack())
    assert RefCb(a="wd").pack() not in callback_datas(h.api.last_markup())


async def test_withdrawal_status_notice(money: Any) -> None:
    h: Harness = money(referral_reward_mode="partner", withdrawals_enabled=True, min_withdrawal_rub=100)
    await register(h.ctx, UID, referral_kop=20_000)
    async with h.ctx.session_factory.begin() as session:
        withdrawal = await partner.request_withdrawal(session, h.ctx.settings, UID, 15_000, "card", "1234")
    async with h.ctx.session_factory.begin() as session:
        withdrawal = await partner.reject(session, withdrawal.id, 1000, comment="Неверные реквизиты")
    assert await service.notify_withdrawal_update(h.ctx, withdrawal)  # type: ignore[arg-type]
    text = h.api.texts()[-1]
    assert "отклонена" in text and "Неверные реквизиты" in text


async def test_referral_module_off(money: Any) -> None:
    h: Harness = money(referral_enabled=False)
    await register(h.ctx, UID)
    sent = len(h.api.screens())
    await h.say(UID, "/ref")
    assert len(h.api.screens()) == sent


# --- texts ------------------------------------------------------------------------------------------------------------
_KEY = re.compile(r"""f?["']((?:wallet|payments|referral|promo|gifts)\.[A-Za-z0-9_.{}]+)["']""")
_NOT_TEXTS = frozenset({"payments.reconcile"})  # job ids


def test_locales_are_complete(tmp_path: Path) -> None:
    i18n = money_i18n(tmp_path)
    problems = [p for p in i18n.check() if p.split(":")[0].split(".")[0] in MONEY_MODULES]
    assert problems == []
    for module in MONEY_MODULES:
        for info in i18n.keys(module):
            assert info.default_ru and info.default_en, info.key
            assert placeholders(info.default_ru) == placeholders(info.default_en), info.key


def test_every_used_key_exists(tmp_path: Path) -> None:
    i18n = money_i18n(tmp_path)
    known = [info.key for info in i18n.keys(None)]
    root = Path(__file__).resolve().parents[1] / "app" / "modules"
    missing: list[str] = []
    seen = 0
    for module in MONEY_MODULES:
        for path in (root / module).glob("*.py"):
            for key in _KEY.findall(path.read_text(encoding="utf-8")):
                if key in _NOT_TEXTS:
                    continue
                seen += 1
                if "{" in key:
                    prefix = key.split("{", 1)[0]
                    if not any(k.startswith(prefix) for k in known):
                        missing.append(key)
                elif not i18n.has(key):
                    missing.append(key)
    assert missing == []
    assert seen > 100
