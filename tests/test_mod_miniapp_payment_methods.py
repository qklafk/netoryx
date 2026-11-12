"""Mini App: blockchain transfers, manual transfers with receipts, saved cards and the provider list."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendPhoto

from app.db.models import Payment, PaymentStatus, SavedPaymentMethod, User
from app.db.session import SessionFactory
from app.services.payments.base import Invoice, ProviderStatus
from app.services.payments.manual import ManualTransferGateway
from app.services.payments.onchain import TransferInstructions
from app.services.payments.stars import StarsGateway
from tests.test_mod_miniapp_shop import make_plans
from tests.test_mod_miniapp_support import (
    BASE,
    USER_ID,
    Env,
    FakeGateway,
    FakeGateways,
    add_user,
    auth,
    miniapp_env,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 64


@dataclass
class FakeChainGateway(FakeGateway):
    key: str = "ton"
    title: str = "💎 Toncoin (TON)"
    polls_by_payment_id: bool = True

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        self.created.append(description)
        return Invoice(None, "https://app.tonkeeper.com/transfer/EQaddr?amount=1500000000", "1.5", "TON")

    def transfer_instructions(self, payment: Payment) -> TransferInstructions:
        return TransferInstructions(
            network="TON",
            currency="TON",
            address="EQaddr",
            amount=payment.provider_amount or "",
            comment="ABCDEF012345",
            wallet_url="https://app.tonkeeper.com/transfer/EQaddr?amount=1500000000&text=ABCDEF012345",
            deep_link="ton://transfer/EQaddr?amount=1500000000&text=ABCDEF012345",
        )


@dataclass
class FakeCardGateway(FakeGateway):
    key: str = "yookassa"
    title: str = "💳 Карта / СБП (ЮKassa)"
    charges: list[str] = field(default_factory=list)

    async def charge_saved(self, payment: Payment, token: str) -> ProviderStatus:
        self.charges.append(token)
        return ProviderStatus(PaymentStatus.SUCCEEDED, paid_amount_kop=payment.amount_kop)


@dataclass
class ReceiptBot:
    sent: list[tuple[str, int, str]] = field(default_factory=list)
    refuse: bool = False

    async def send_photo(self, chat_id: int, photo: Any, **kw: Any) -> Any:
        if self.refuse:
            raise TelegramForbiddenError(SendPhoto(chat_id=chat_id, photo="x"), "bot was blocked by the user")
        self.sent.append(("photo", chat_id, kw.get("caption", "")))
        return SimpleNamespace(photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="photo-id")])

    async def send_document(self, chat_id: int, document: Any, **kw: Any) -> Any:
        self.sent.append(("document", chat_id, kw.get("caption", "")))
        return SimpleNamespace(document=SimpleNamespace(file_id="doc-id"))


@pytest.fixture
async def env(tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with miniapp_env(
        tmp_path,
        session_factory,
        monkeypatch,
        manual_transfer_enabled=True,
        manual_transfer_details="Карта 2200 0000 0000 0000, Иван И.",
    ) as environment:
        ctx = environment.ctx
        ctx.gateways = FakeGateways(
            FakeGateway(),
            StarsGateway(ctx.box.value),
            FakeChainGateway(),
            FakeCardGateway(),
            ManualTransferGateway(),
        )
        await add_user(ctx.session_factory, environment.settings)
        yield environment


async def load(env: Env, payment_id: str) -> Payment:
    async with env.ctx.session_factory() as session:
        payment = await session.get(Payment, payment_id)
        assert payment is not None
        return payment


async def upload(env: Env, payment_id: str, body: bytes, user_id: int = USER_ID) -> Any:
    return await env.client.post(
        BASE + "/payments/receipt",
        params={"payment_id": payment_id},
        content=body,
        headers={**auth(user_id), "content-type": "application/octet-stream"},
    )


# ---------------------------------------------------------------------------------------------
# Provider list
# ---------------------------------------------------------------------------------------------
async def test_provider_list_kinds_titles_and_card_saving(env: Env) -> None:
    await make_plans(env)
    providers = {p["key"]: p for p in (await env.get("/plans")).json()["providers"]}
    assert providers["ton"]["kind"] == "onchain" and providers["ton"]["title"] == "💎 Toncoin (TON), напрямую"
    assert providers["manual"]["kind"] == "manual" and providers["manual"]["title"] == "💳 Перевод на карту / СБП"
    assert providers["stars"]["kind"] == "stars" and providers["cryptobot"]["kind"] == "redirect"
    assert not any(p["save_card"] for p in providers.values())  # saving cards is off by default

    env.ctx.box.update(saved_cards_enabled=True)
    options = (await env.get("/wallet/options")).json()["providers"]
    assert [p["key"] for p in options if p["save_card"]] == ["yookassa"]


# ---------------------------------------------------------------------------------------------
# Blockchain transfers
# ---------------------------------------------------------------------------------------------
async def test_onchain_payment_has_transfer_instructions_and_is_checked(env: Env) -> None:
    resp = await env.post("/wallet/topup", {"amount_kop": 300_00, "provider": "ton"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    transfer = body["transfer"]
    assert transfer["address"] == "EQaddr" and transfer["amount"] == "1.5" and transfer["currency"] == "TON"
    assert transfer["comment"] == "ABCDEF012345"
    assert transfer["wallet_url"].startswith("https://app.tonkeeper.com/transfer/")
    assert transfer["deep_link"].startswith("ton://transfer/")
    assert "комментарий" in transfer["notes"][0] and "24 часа" in transfer["notes"][1]
    assert body["provider_amount"] == "1.5" and body["provider_currency"] == "TON"

    # The chain is searched by our own payment id: no provider id is needed for "check payment".
    chain = env.ctx.gateways.get("ton")
    chain.status = PaymentStatus.SUCCEEDED
    paid = (await env.get(f"/payments/{body['id']}")).json()
    assert chain.fetched == [body["id"]]
    assert paid["status"] == "succeeded" and paid["transfer"] is None


async def test_onchain_notes_in_english(env: Env) -> None:
    async with env.ctx.session_factory.begin() as session:
        user = await session.get(User, USER_ID)
        assert user is not None
        user.language = "en"
    body = (await env.post("/wallet/topup", {"amount_kop": 300_00, "provider": "ton"})).json()
    assert body["transfer"]["notes"][0].startswith("⚠️ Be sure to include the comment")


async def test_redirect_payment_without_provider_id_is_not_polled(env: Env) -> None:
    created = (await env.post("/wallet/topup", {"amount_kop": 300_00, "provider": "cryptobot"})).json()
    async with env.ctx.session_factory.begin() as session:
        row = await session.get(Payment, created["id"])
        assert row is not None
        row.provider_payment_id = None
    await env.get(f"/payments/{created['id']}")
    assert env.ctx.gateways.get("cryptobot").fetched == []


# ---------------------------------------------------------------------------------------------
# Manual transfers
# ---------------------------------------------------------------------------------------------
async def test_manual_transfer_shows_details_and_accepts_a_receipt(env: Env) -> None:
    bot = ReceiptBot()
    env.ctx.bot = bot  # type: ignore[assignment]
    ids = await make_plans(env)
    resp = await env.post(f"/plans/{ids['month']}/buy", {"provider": "manual"})
    assert resp.status_code == 200, resp.text
    payment = resp.json()["payment"]
    assert payment["manual"] == {"details": "Карта 2200 0000 0000 0000, Иван И.", "receipt_sent": False}
    assert payment["url"] in (None, "")

    bad = await upload(env, payment["id"], b"GIF89a....")
    assert bad.status_code == 415 and bad.json()["error"] == "receipt_bad_type"
    empty = await upload(env, payment["id"], b"")
    assert empty.status_code == 400 and empty.json()["error"] == "receipt_empty"
    other = await upload(env, payment["id"], JPEG, user_id=5555)
    assert other.status_code in (401, 403, 404)

    ok = await upload(env, payment["id"], JPEG)
    assert ok.status_code == 200, ok.text
    assert ok.json()["manual"]["receipt_sent"] is True
    assert bot.sent[0][:2] == ("photo", USER_ID) and "200 ₽" in bot.sent[0][2]
    row = await load(env, payment["id"])
    assert row.receipt_file_id == "photo-id" and row.receipt_media_type == "photo"
    assert row.status is PaymentStatus.PENDING  # staff confirm it
    assert any("Чек ручного перевода" in text for _event, text in env.notified)

    pdf = await upload(env, payment["id"], PDF)
    assert pdf.status_code == 200
    row = await load(env, payment["id"])
    assert row.receipt_file_id == "doc-id" and row.receipt_media_type == "document"


async def test_receipt_rules(env: Env) -> None:
    bot = ReceiptBot(refuse=True)
    env.ctx.bot = bot  # type: ignore[assignment]
    card = (await env.post("/wallet/topup", {"amount_kop": 300_00, "provider": "cryptobot"})).json()
    wrong = await upload(env, card["id"], JPEG)
    assert wrong.status_code == 409 and wrong.json()["error"] == "receipt_not_expected"

    ids = await make_plans(env)
    manual = (await env.post(f"/plans/{ids['month']}/buy", {"provider": "manual"})).json()["payment"]
    blocked = await upload(env, manual["id"], JPEG)
    assert blocked.status_code == 502 and blocked.json()["error"] == "receipt_failed"
    assert (await load(env, manual["id"])).receipt_file_id is None

    too_big = await upload(env, manual["id"], JPEG + b"\x00" * (10 * 1024 * 1024))
    assert too_big.status_code == 413

    async with env.ctx.session_factory.begin() as session:
        row = await session.get(Payment, manual["id"])
        assert row is not None
        row.status = PaymentStatus.CANCELED
    closed = await upload(env, manual["id"], JPEG)
    assert closed.status_code == 409
    assert (await env.get(f"/payments/{manual['id']}")).json()["manual"]["details"] == ""


# ---------------------------------------------------------------------------------------------
# Saved cards
# ---------------------------------------------------------------------------------------------
async def test_save_card_consent(env: Env) -> None:
    ids = await make_plans(env)
    off = await env.post(f"/plans/{ids['month']}/buy", {"provider": "yookassa", "save_card": True})
    assert off.status_code == 400 and off.json()["error"] == "save_card_unavailable"

    env.ctx.box.update(saved_cards_enabled=True)
    unsupported = await env.post("/wallet/topup", {"amount_kop": 300_00, "provider": "cryptobot", "save_card": True})
    assert unsupported.status_code == 400 and unsupported.json()["error"] == "save_card_unavailable"
    gift = await env.post(f"/plans/{ids['month']}/buy", {"provider": "yookassa", "save_card": True, "gift": True})
    assert gift.status_code == 400

    plan = await env.post(f"/plans/{ids['month']}/buy", {"provider": "yookassa", "save_card": True})
    assert plan.status_code == 200, plan.text
    assert plan.json()["payment"]["save_card"] is True
    assert (await load(env, plan.json()["payment"]["id"])).save_method is True
    topup = await env.post("/wallet/topup", {"amount_kop": 300_00, "provider": "yookassa", "save_card": True})
    assert topup.status_code == 200 and topup.json()["save_card"] is True
    plain = await env.post("/wallet/topup", {"amount_kop": 300_00, "provider": "yookassa"})
    assert (await load(env, plain.json()["id"])).save_method is False


async def test_saved_cards_list_and_remove(env: Env) -> None:
    await add_user(env.ctx.session_factory, env.settings, 5555)
    async with env.ctx.session_factory.begin() as session:
        mine = SavedPaymentMethod(user_id=USER_ID, provider="yookassa", token="pm-1", title="Visa •• 4242")
        theirs = SavedPaymentMethod(user_id=5555, provider="yookassa", token="pm-2", title="Mir •• 0001")
        session.add_all([mine, theirs])
    async with env.ctx.session_factory() as session:
        mine_id = (await session.get(SavedPaymentMethod, 1)).id  # type: ignore[union-attr]

    listed = (await env.get("/wallet/cards")).json()
    assert listed["enabled"] is False
    assert [(c["title"], c["provider"]) for c in listed["items"]] == [("Visa •• 4242", "yookassa")]
    assert "token" not in listed["items"][0]

    foreign = await env.client.delete(BASE + "/wallet/cards/2", headers=auth())
    assert foreign.status_code == 404 and foreign.json()["error"] == "card_not_found"
    removed = await env.client.delete(BASE + f"/wallet/cards/{mine_id}", headers=auth())
    assert removed.status_code == 200 and removed.json() == {"ok": True}
    assert (await env.get("/wallet/cards")).json()["items"] == []
    again = await env.client.delete(BASE + f"/wallet/cards/{mine_id}", headers=auth())
    assert again.status_code == 404
