"""Offline end-to-end flow across the real service modules (only panels and CryptoBot are faked).

referral sign-up -> trial -> provisions reconciled on a 3x-ui and a Remnawave FakePanel -> subscription page
-> Stars top-up and CryptoBot plan payment (webhook racing the payment reconciler) -> plan bought from the
wallet -> revision bump pushed to panels -> reminders -> expiry -> retention deletion.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import func, select

from app.db.models import (
    BalanceTransaction,
    Inbound,
    Location,
    Panel,
    PanelKind,
    Payment,
    PaymentStatus,
    Plan,
    Provision,
    ProvisionState,
    Purchase,
    PurchaseKind,
    Subscription,
    SubscriptionStatus,
    TxKind,
    User,
)
from app.db.session import SessionFactory
from app.services import plans, provisioning, referrals, subscription_page, subscriptions, users, wallet
from app.services.panels.base import PanelAuthError, PanelBackend
from app.services.panels.inventory import refresh_inbounds
from app.services.panels.registry import build_backend
from app.services.panels.remnawave import RemnawaveBackend
from app.services.panels.xui import XuiBackend
from app.services.payments import service as payments
from app.services.payments.cryptobot import SIGNATURE_HEADER, CryptoBotGateway, sign_body
from app.services.payments.service import CreditResult
from app.services.payments.stars import StarsGateway, credit_successful_payment, validate_pre_checkout
from app.services.referrals import ReferralOutcome
from app.utils.crypto import SecretBox
from tests.conftest import make_settings
from tests.fakes import FakePanel, default_inbound

INVITER_ID = 111
INVITEE_ID = 222
CRYPTO_TOKEN = "12345:CRYPTO-TEST-TOKEN"


class Registry:
    """Stands in for PanelRegistry: panel id -> FakePanel."""

    def __init__(self) -> None:
        self.panels: dict[int, FakePanel] = {}

    async def get(self, panel_id: int) -> PanelBackend:
        return self.panels[panel_id]


@dataclass
class FakeCryptoPay:
    """Minimal Crypto Pay API: createInvoice / getInvoices with invoices the test can mark paid."""

    invoices: dict[int, dict[str, Any]] = field(default_factory=dict)
    fetches: int = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/api/createInvoice", self.create_invoice)
        app.router.add_post("/api/getInvoices", self.get_invoices)
        return app

    async def create_invoice(self, request: web.Request) -> web.Response:
        assert request.headers.get("Crypto-Pay-API-Token") == CRYPTO_TOKEN
        body = await request.json()
        invoice_id = len(self.invoices) + 1
        self.invoices[invoice_id] = {
            "invoice_id": invoice_id,
            "status": "active",
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": body["amount"],
            "payload": body["payload"],
            "bot_invoice_url": f"https://t.me/CryptoBot?start=IV{invoice_id}",
        }
        return web.json_response({"ok": True, "result": self.invoices[invoice_id]})

    async def get_invoices(self, request: web.Request) -> web.Response:
        self.fetches += 1
        body = await request.json()
        ids = {int(i) for i in str(body["invoice_ids"]).split(",")}
        items = [inv for iid, inv in self.invoices.items() if iid in ids]
        return web.json_response({"ok": True, "result": {"items": items}})

    def pay(self, invoice_id: int) -> bytes:
        """Mark paid and return the webhook body CryptoBot would send."""
        self.invoices[invoice_id]["status"] = "paid"
        update = {"update_type": "invoice_paid", "payload": self.invoices[invoice_id]}
        return json.dumps(update).encode()


@pytest.fixture
async def crypto_server() -> AsyncIterator[tuple[FakeCryptoPay, TestServer]]:
    fake = FakeCryptoPay()
    server = TestServer(fake.app())
    async with server:
        yield fake, server


@pytest.fixture(autouse=True)
def _clear_page_caches() -> None:
    subscription_page.clear_caches()


async def _ledger_sum(sf: SessionFactory, user_id: int) -> int:
    async with sf() as s:
        return int(
            await s.scalar(
                select(func.coalesce(func.sum(BalanceTransaction.amount_kop), 0)).where(
                    BalanceTransaction.user_id == user_id
                )
            )
            or 0
        )


async def _balance(sf: SessionFactory, user_id: int) -> int:
    async with sf() as s:
        balance = await wallet.balance(s, user_id)
    assert balance == await _ledger_sum(sf, user_id), "wallet balance diverged from the ledger"
    return balance


async def _sub(sf: SessionFactory) -> Subscription:
    async with sf() as s:
        sub = await subscriptions.get_for_user(s, INVITEE_ID)
    assert sub is not None
    return sub


async def _provisions(sf: SessionFactory) -> list[Provision]:
    async with sf() as s:
        return list((await s.scalars(select(Provision).order_by(Provision.id))).all())


async def test_full_flow(session_factory: SessionFactory, crypto_server: tuple[FakeCryptoPay, TestServer]) -> None:
    sf = session_factory
    fake_crypto, server = crypto_server
    settings = make_settings(
        xui_device_limit="ip",
        referral_enabled=True,
        referral_invitee_bonus_rub=50,
        referral_invitee_requires_payment=False,
        referral_inviter_bonus_rub=50,
        referral_inviter_requires_payment=True,
        referral_inviter_percent=10,
        trial_days=3,
        trial_device_limit=1,
        expiry_reminder_days=[3, 1],
        expired_client_retention_days=30,
        stars_enabled=True,
        stars_rub_per_star=1.5,
        cryptobot_enabled=True,
        cryptobot_token=CRYPTO_TOKEN,
    )
    now = datetime.now(UTC).replace(microsecond=0)
    registry = Registry()
    xui = FakePanel(kind=PanelKind.XUI, inbounds=[default_inbound(remote_id=7, port=443)])
    remna = FakePanel(kind=PanelKind.REMNAWAVE)

    # --- admin setup: location, a 3x-ui panel (inbound imported by inventory) and a Remnawave panel ----------
    async with sf.begin() as s:
        location = Location(name="Германия", flag="🇩🇪", sort_order=1)
        s.add(location)
        await s.flush()
        xui_panel = Panel(name="de-xui", kind=PanelKind.XUI, url="https://xui.example.com:2053/base")
        remna_panel = Panel(
            name="rw",
            kind=PanelKind.REMNAWAVE,
            url="https://rw.example.com",
            options=json.dumps({"location_id": location.id, "squad_uuids": []}),
        )
        s.add_all([xui_panel, remna_panel])
        await s.flush()
        location_id, xui_id, remna_id = location.id, xui_panel.id, remna_panel.id
        plan = await plans.create(s, title="1 месяц", duration_days=30, price_kop=19900, device_limit=3)
        plan_id = plan.id
    registry.panels = {xui_id: xui, remna_id: remna}
    refreshed = await refresh_inbounds(
        sf, registry, xui_id, activate_new=True, location_id=location_id, endpoint_address="de.example.com"
    )
    assert len(refreshed.created) == 1
    async with sf() as s:
        inbound = await s.scalar(select(Inbound).where(Inbound.panel_id == xui_id))
        assert inbound is not None and inbound.is_active and inbound.remote_id == 7
        inbound_db_id, inbound_remote_id = inbound.id, inbound.remote_id
        assert inbound.flow == "xtls-rprx-vision"
        assert json.loads(inbound.params or "{}")["port"] == 443

    # --- registration via referral link ------------------------------------------------------------------------
    async with sf.begin() as s:
        inviter, created = await users.get_or_create(s, settings, INVITER_ID, "Inviter", "Inv", "ru")
        assert created
        code = inviter.referral_code
    async with sf.begin() as s:
        invitee, created = await users.get_or_create(s, settings, INVITEE_ID, "@Newbie", "New", "en-US")
        assert created and invitee.language == "en" and invitee.username == "newbie"
        result = await referrals.apply_on_registration(s, settings, invitee, f"ref_{code}", is_new=True)
        assert result.outcome == ReferralOutcome.APPLIED
        assert (result.inviter_id, result.invitee_bonus_kop, result.inviter_bonus_kop) == (INVITER_ID, 5000, 0)
        sub_token = invitee.sub_token
    async with sf.begin() as s:  # a repeated /start is not a new registration
        again, created = await users.get_or_create(s, settings, INVITEE_ID, "newbie", "New", "en")
        assert not created
        repeat = await referrals.apply_on_registration(s, settings, again, code, is_new=False)
        assert repeat.outcome == ReferralOutcome.NOT_NEW
    assert await _balance(sf, INVITEE_ID) == 5000
    assert await _balance(sf, INVITER_ID) == 0

    # --- trial -> provisions on both panels -------------------------------------------------------------------
    async with sf.begin() as s:
        invitee_row = await users.get(s, INVITEE_ID)
        assert invitee_row is not None
        trial = await subscriptions.start_trial(s, settings, invitee_row, now=now)
        assert trial.is_trial and trial.device_limit == 1
        trial_expiry, client_uuid, trial_revision = trial.expires_at, trial.client_uuid, trial.revision
    assert trial_expiry == now + timedelta(days=3)
    async with sf.begin() as s:
        invitee_row = await users.get(s, INVITEE_ID)
        assert invitee_row is not None
        with pytest.raises(subscriptions.SubscriptionError) as exc_info:
            await subscriptions.start_trial(s, settings, invitee_row, now=now)
        assert exc_info.value.code == subscriptions.SubErrorCode.TRIAL_USED

    report = await provisioning.reconcile(sf, registry, settings, now=now)
    assert (report.synced, report.failed, report.errors) == (2, 0, [])
    assert report.orphans == []
    xui_key = (inbound_remote_id, f"tg{INVITEE_ID}-{inbound_db_id}")
    remna_key = (None, f"tg_{INVITEE_ID}")
    for panel, key in ((xui, xui_key), (remna, remna_key)):
        account = panel.accounts[key]
        assert account.uuid == client_uuid and account.enabled
        assert account.expires_at == trial_expiry and account.device_limit == 1
    assert xui.accounts[xui_key].email == xui_key[1]
    provs = await _provisions(sf)
    assert len(provs) == 2
    assert all(p.actual_state == ProvisionState.ENABLED and p.synced_revision == trial_revision for p in provs)
    # converged: a second run pushes nothing
    calls_before = len(xui.calls) + len(remna.calls)
    report = await provisioning.reconcile(sf, registry, settings, now=now)
    assert report.synced == 0 and len(xui.calls) + len(remna.calls) == calls_before

    # --- subscription page renders links for both panels --------------------------------------------------------
    page = await subscription_page.render(
        sf, registry, settings, sub_token, profile="all", client_headers={"User-Agent": "Hiddify/2.5"}, now=now
    )
    assert page is not None and page.state == subscription_page.AccessState.ACTIVE
    assert len(page.links) == 2
    xui_link, rw_link = page.links
    assert xui_link.startswith(f"vless://{client_uuid}@de.example.com:443?")
    assert "flow=xtls-rprx-vision" in xui_link and "pbk=PUBKEY" in xui_link and "security=reality" in xui_link
    assert rw_link == f"vless://{client_uuid}@rw.example.com:443?type=tcp&security=reality#RW"
    assert base64.b64decode(page.body).decode().splitlines() == page.links
    assert f"expire={int(trial_expiry.timestamp())}" in page.headers["subscription-userinfo"]
    assert await subscription_page.render(sf, registry, settings, "nope", profile="all") is None

    # --- Stars top-up: pre-checkout, successful_payment (redelivered) -------------------------------------------
    stars = StarsGateway(settings)
    stars_payment = await payments.create_payment(sf, settings, stars, INVITEE_ID, 15000)
    assert stars_payment.status == PaymentStatus.PENDING and stars_payment.provider_amount == "100"
    assert await validate_pre_checkout(sf, stars_payment.id, 100, INVITEE_ID, settings=settings)
    assert not await validate_pre_checkout(sf, stars_payment.id, 99, INVITEE_ID, settings=settings)
    assert not await validate_pre_checkout(sf, stars_payment.id, 100, INVITER_ID, settings=settings)
    credited = await credit_successful_payment(
        sf, settings, payload=stars_payment.id, user_id=INVITEE_ID, total_amount=100, charge_id="tg-charge-1"
    )
    assert credited is not None and credited.amount_kop == 15000
    # first invitee payment: fixed 50 RUB + legacy 10% + level-1 10% (default REFERRAL_LEVELS_PERCENT) of 150 RUB
    assert credited.inviter == (INVITER_ID, 5000 + 1500 + 1500)
    again_credit = await credit_successful_payment(
        sf, settings, payload=stars_payment.id, user_id=INVITEE_ID, total_amount=100, charge_id="tg-charge-1"
    )
    assert again_credit is None
    assert await _balance(sf, INVITEE_ID) == 5000 + 15000
    assert await _balance(sf, INVITER_ID) == 8000

    # --- CryptoBot payment for the plan: webhook races the reconciler, credited once, plan bought -------------
    crypto = CryptoBotGateway(CRYPTO_TOKEN, base_url=str(server.make_url("/api")))
    on_credited_calls: list[CreditResult] = []

    async def on_credited(res: CreditResult) -> None:
        """What the bot does after a credit: buy the plan the payment was made for, from the wallet."""
        on_credited_calls.append(res)
        if res.plan_id is None:
            return
        async with sf.begin() as s:
            user = await users.get(s, res.user_id)
            bought_plan = await plans.get(s, res.plan_id)
            assert user is not None and bought_plan is not None
            await subscriptions.purchase_plan(s, settings, user, bought_plan, payment_id=res.payment_id, now=now)

    try:
        crypto_payment = await payments.create_payment(
            sf, settings, crypto, INVITEE_ID, 19900, plan_id=plan_id, description="1 месяц"
        )
        assert crypto_payment.status == PaymentStatus.PENDING
        assert crypto_payment.confirmation_url == "https://t.me/CryptoBot?start=IV1"
        # nothing paid yet: reconcile polls and leaves it pending
        assert await payments.reconcile(sf, settings, {"cryptobot": crypto}, on_credited) == 0
        raw = fake_crypto.pay(int(crypto_payment.provider_payment_id or 0))
        headers = {SIGNATURE_HEADER: sign_body(raw, CRYPTO_TOKEN)}
        webhook, polled = await asyncio.gather(
            payments.handle_webhook(sf, settings, crypto, raw, headers, "1.2.3.4", on_credited),
            payments.reconcile(sf, settings, {"cryptobot": crypto}, on_credited),
        )
        assert webhook.status_code == 200
        assert polled in (0, 1)
        # a redelivered webhook is a no-op
        await payments.handle_webhook(sf, settings, crypto, raw, headers, "1.2.3.4", on_credited)
    finally:
        await crypto.close()
    # webhook + reconcile + redelivery: credited (and the plan bought) exactly once
    assert [c.payment_id for c in on_credited_calls] == [crypto_payment.id]
    assert on_credited_calls[0].amount_kop == 19900 and on_credited_calls[0].plan_id == plan_id
    assert on_credited_calls[0].inviter == (INVITER_ID, 1990 + 1990)  # legacy + level-1 percent, no fixed bonus
    async with sf() as s:
        payment_rows = await s.scalar(
            select(func.count())
            .select_from(BalanceTransaction)
            .where(BalanceTransaction.kind == TxKind.PAYMENT, BalanceTransaction.ref == crypto_payment.id)
        )
        assert payment_rows == 1
        stored = await s.get(Payment, crypto_payment.id)
        assert stored is not None and stored.status == PaymentStatus.SUCCEEDED and stored.credited_at is not None
        purchase = await s.scalar(select(Purchase).where(Purchase.payment_id == crypto_payment.id))
        assert purchase is not None and purchase.kind == PurchaseKind.PURCHASE and purchase.price_kop == 19900
    assert await _balance(sf, INVITEE_ID) == 5000 + 15000 + 19900 - 19900
    # legacy 10% (REFERRAL_INVITER_PERCENT) + level-1 10% (default REFERRAL_LEVELS_PERCENT, balance mode) of the
    # payment; buying the plan from the wallet earns nothing more
    assert await _balance(sf, INVITER_ID) == 8000 + 1990 + 1990
    async with sf() as s:
        assert await referrals.stats(s, INVITER_ID) == (1, 11980)

    # --- revision bump -> reconcile pushes the new expiry and plan limits ----------------------------------------
    sub = await _sub(sf)
    paid_expiry = trial_expiry + timedelta(days=30)
    assert sub.expires_at == paid_expiry and not sub.is_trial and sub.plan_id == plan_id
    assert sub.revision > trial_revision and sub.device_limit == 3
    assert all(p.synced_revision < sub.revision for p in await _provisions(sf))
    report = await provisioning.reconcile(sf, registry, settings, now=now)
    assert (report.synced, report.failed) == (2, 0)
    for panel, key in ((xui, xui_key), (remna, remna_key)):
        assert panel.accounts[key].expires_at == paid_expiry
        assert panel.accounts[key].device_limit == 3 and panel.accounts[key].enabled
    assert all(p.synced_revision == sub.revision for p in await _provisions(sf))

    # --- reminders before expiry; auto-renew is off -------------------------------------------------------------
    async with sf.begin() as s:
        locked = await subscriptions.get_for_user(s, INVITEE_ID)
        assert locked is not None
        await subscriptions.set_auto_renew(s, locked, False)
    two_days_before = paid_expiry - timedelta(days=1, hours=12)
    assert await subscriptions.run_auto_renew(sf, settings, two_days_before) == []
    async with sf.begin() as s:
        due = await subscriptions.reminders_due(s, settings, two_days_before)
        assert [(d.user_id, days) for d, days in due] == [(INVITEE_ID, 2)]
        assert due[0][0].user.tg_id == INVITEE_ID
        await subscriptions.mark_reminded(s, due[0][0], 2)
    async with sf.begin() as s:
        assert await subscriptions.reminders_due(s, settings, two_days_before) == []
        due = await subscriptions.reminders_due(s, settings, paid_expiry - timedelta(hours=6))
        assert [days for _, days in due] == [1]
        await subscriptions.mark_reminded(s, due[0][0], 1)

    # --- expiry: expire_due, expired notice, panels disabled, placeholder page --------------------------------
    after_expiry = paid_expiry + timedelta(minutes=5)
    assert await subscriptions.expire_due(sf, after_expiry) == [INVITEE_ID]
    assert await subscriptions.expire_due(sf, after_expiry) == []
    async with sf.begin() as s:
        due = await subscriptions.reminders_due(s, settings, after_expiry)
        assert [days for _, days in due] == [0]
        await subscriptions.mark_reminded(s, due[0][0], 0)
    async with sf() as s:
        assert await subscriptions.reminders_due(s, settings, after_expiry) == []
    sub = await _sub(sf)
    assert sub.status == SubscriptionStatus.EXPIRED
    report = await provisioning.reconcile(sf, registry, settings, now=after_expiry)
    assert (report.synced, report.failed) == (2, 0)
    assert not xui.accounts[xui_key].enabled and not remna.accounts[remna_key].enabled
    assert all(p.actual_state == ProvisionState.DISABLED for p in await _provisions(sf))
    page = await subscription_page.render(sf, registry, settings, sub_token, profile="all", now=after_expiry)
    assert page is not None and page.state == subscription_page.AccessState.EXPIRED
    assert len(page.links) == 1 and "@0.0.0.0:1" in page.links[0]

    # --- retention elapsed: accounts deleted from both panels ---------------------------------------------------
    after_retention = paid_expiry + timedelta(days=settings.expired_client_retention_days, hours=1)
    report = await provisioning.reconcile(sf, registry, settings, now=after_retention)
    assert (report.synced, report.failed) == (2, 0)
    assert xui_key not in xui.accounts and remna_key not in remna.accounts
    assert ("delete", inbound_remote_id, xui_key[1]) in xui.calls
    assert ("delete", None, remna_key[1]) in remna.calls
    assert all(p.actual_state == ProvisionState.ABSENT for p in await _provisions(sf))
    report = await provisioning.reconcile(sf, registry, settings, now=after_retention)
    assert report.synced == 0 and report.provisions_created == 0

    # final bookkeeping: users and plan untouched, balances match their ledgers
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(User)) == 2
        assert (await s.get(Plan, plan_id)) is not None
    assert await _balance(sf, INVITEE_ID) == 20000
    assert await _balance(sf, INVITER_ID) == 11980


async def test_registry_builds_real_backends() -> None:
    """The registry's constructor kwargs match the real backends (its own tests use a stub)."""
    settings = make_settings()
    box = SecretBox(settings.secret_key.get_secret_value() if settings.secret_key else "k")
    xui_panel = Panel(
        id=1, name="x", kind=PanelKind.XUI, url="https://x.example.com/p", username="u", password=box.encrypt("pw")
    )
    rw_options = {"squad_uuids": ["s1"], "username_prefix": "vpn_", "direct": False, "traffic_limit_strategy": "MONTH"}
    rw_panel = Panel(
        id=2,
        name="r",
        kind=PanelKind.REMNAWAVE,
        url="http://remnawave:3000",
        api_token=box.encrypt("tok"),
        tls_verify=True,
        options=json.dumps(rw_options),
    )
    xui_backend = build_backend(xui_panel, box, settings)
    rw_backend = build_backend(rw_panel, box, settings)
    try:
        assert isinstance(xui_backend, XuiBackend) and isinstance(xui_backend, PanelBackend)
        assert isinstance(rw_backend, RemnawaveBackend) and isinstance(rw_backend, PanelBackend)
        assert rw_backend.traffic_limit_strategy == "MONTH"
        bad = Panel(
            id=3,
            name="b",
            kind=PanelKind.REMNAWAVE,
            url="http://rw",
            api_token="t",
            options='{"traffic_limit_strategy": "YEAR"}',
        )
        with pytest.raises(PanelAuthError, match="traffic_limit_strategy"):
            build_backend(bad, box, settings)
    finally:
        await xui_backend.close()
        await rw_backend.close()
