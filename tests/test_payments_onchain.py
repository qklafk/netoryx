"""Direct TON and USDT TRC-20 gateways against fake indexers, amount reservations and the reconciler."""

from __future__ import annotations

import tomllib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

from app.db.models import KeyValue, Payment, PaymentStatus, User
from app.db.session import SessionFactory
from app.services.payments import onchain, service
from app.services.payments.base import GatewayError, WebhookRejected
from app.services.payments.onchain import ceil_units, payment_comment, str_to_units, units_to_str
from app.services.payments.registry import build_gateways
from app.services.payments.ton import TonGateway, raw_address
from app.services.payments.tron import USDT_CONTRACT, UsdtTrc20Gateway, is_tron_address, slot_order
from tests.conftest import make_settings

TON_ADDRESS = "UQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqEBI"
TON_RAW = "0:83DFD552E63729B472FCBCC8C45EBCC6691702558B68EC7527E1BA403A0F31A8"
OTHER_TON_RAW = "0:7C6E0F6B30ACBF1C1A2D15D857DE6215EF40F298E01E4D541FFACAB4943C9DF5"
TRON_ADDRESS = "TV6MuMXfmLbBqPZvBHdwFsDnQeVfnmiuSi"
OTHER_TRON = "TYr4DLeAY4S7g9FkQQGvo8DmyPDvzb8HWq"
PAYMENT_ID = "0b9c6a8e-6a4e-4a8f-9a59-1f1f7d0e2c11"
USER_ID = 5512345678
CREATED = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
CREATED_TS = int(CREATED.timestamp())


# ---------------------------------------------------------------------------------------------
# Fake indexer / rate API
# ---------------------------------------------------------------------------------------------
@dataclass
class Seen:
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]


@dataclass
class FakeApi:
    base_url: str = ""
    routes: dict[str, list[tuple[int, Any]]] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)

    def reply(self, path: str, *replies: tuple[int, Any]) -> None:
        self.routes[path] = list(replies)

    def calls(self, path: str) -> list[Seen]:
        return [s for s in self.seen if s.path == path]


@pytest.fixture
async def api() -> AsyncIterator[FakeApi]:
    fake = FakeApi()

    async def route(request: web.Request) -> web.Response:
        fake.seen.append(Seen(request.path, parse_qs(request.query_string), dict(request.headers)))
        queue = fake.routes.get(request.path)
        if not queue:
            return web.json_response({"error": "no route"}, status=404)
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", route)
    server = TestServer(app)
    await server.start_server()
    fake.base_url = str(server.make_url("")).rstrip("/")
    try:
        yield fake
    finally:
        await server.close()


def payment(amount_kop: int = 19900, provider_amount: str | None = None, **kw: Any) -> Payment:
    kw.setdefault("id", PAYMENT_ID)
    kw.setdefault("provider", "ton")
    kw.setdefault("created_at", CREATED)
    kw.setdefault("status", PaymentStatus.PENDING)
    return Payment(user_id=USER_ID, amount_kop=amount_kop, provider_amount=provider_amount, **kw)


def user() -> User:
    return User(tg_id=USER_ID, referral_code="R", sub_token="T")


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def test_unit_helpers() -> None:
    assert ceil_units(onchain.rub_amount(19900) / 200, 9, 4) == 995_000_000
    assert ceil_units(onchain.rub_amount(10000) / 300, 9, 4) == 333_400_000  # 0.33333.. rounded up
    assert units_to_str(995_000_000, 9) == "0.995"
    assert units_to_str(1_000_000_000, 9) == "1"
    assert units_to_str(2_491_000, 6) == "2.491"
    assert str_to_units("2.491", 6) == 2_491_000
    for bad in (None, "", "abc", "-1", "0", "1.0000001"):
        with pytest.raises(GatewayError):
            str_to_units(bad, 6)
    assert payment_comment(PAYMENT_ID) == "0B9C6A8E6A4E"


def test_ton_addresses() -> None:
    assert raw_address(TON_ADDRESS) == TON_RAW.lower()
    assert raw_address(TON_RAW) == TON_RAW.lower()
    assert raw_address("EQB8bg9rMKy_HBotFdhX3mIV70DymOAeTVQf-sq0lDyd9XUh") == OTHER_TON_RAW.lower()
    with pytest.raises(ValueError):
        raw_address(TON_ADDRESS[:-1] + ("A" if TON_ADDRESS[-1] != "A" else "B"))  # checksum
    with pytest.raises(ValueError):
        raw_address("not-an-address")


def test_tron_addresses_and_slots() -> None:
    assert is_tron_address(TRON_ADDRESS) and is_tron_address(USDT_CONTRACT)
    assert not is_tron_address(TRON_ADDRESS[:-1] + "j")
    assert not is_tron_address("0x" + "1" * 32)
    slots = slot_order()
    assert len(slots) == len(set(slots)) == 9999
    assert slots[:10] == [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 100]
    assert min(slots) == 1 and max(slots) == 9999


# ---------------------------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------------------------
def ton_gateway(api: FakeApi, **kw: Any) -> TonGateway:
    kw.setdefault("rate_rub", 200)
    gw = TonGateway(TON_ADDRESS, base_url=f"{api.base_url}/api/v3", rate_url=f"{api.base_url}/simple/price", **kw)
    gw._retry_delay = 0
    return gw


async def test_rate_fixed_auto_cached_and_fallback(api: FakeApi) -> None:
    gw = ton_gateway(api, rate_rub=190.5, auto_rate=True)
    api.reply("/simple/price", (200, {"the-open-network": {"rub": 200}}))
    try:
        assert await gw.rate() == 200
        assert await gw.rate() == 200
        assert len(api.calls("/simple/price")) == 1  # cached
        assert api.calls("/simple/price")[0].query == {"ids": ["the-open-network"], "vs_currencies": ["rub"]}
        gw._cached_rate = None
        api.reply("/simple/price", (200, {"the-open-network": {"usd": 3}}))
        assert str(await gw.rate()) == "190.5"  # falls back to the fixed rate
    finally:
        await gw.close()


async def test_unknown_rate_never_creates_an_invoice(api: FakeApi) -> None:
    with pytest.raises(ValueError):
        ton_gateway(api, rate_rub=None)
    gw = ton_gateway(api, rate_rub=None, auto_rate=True)
    api.reply("/simple/price", (500, {}))
    try:
        with pytest.raises(GatewayError):
            await gw.create(payment(), user(), "x")
    finally:
        await gw.close()


# ---------------------------------------------------------------------------------------------
# TON
# ---------------------------------------------------------------------------------------------
async def test_ton_create_and_instructions(api: FakeApi) -> None:
    gw = ton_gateway(api)
    invoice = await gw.create(payment(), user(), "x")
    assert invoice.provider_payment_id is None
    assert invoice.provider_amount == "0.995" and invoice.provider_currency == "TON"
    assert invoice.url == f"https://app.tonkeeper.com/transfer/{TON_ADDRESS}?amount=995000000&text=0B9C6A8E6A4E"
    details = onchain.instructions(gw, payment(provider_amount="0.995"))
    assert details is not None
    assert (details.address, details.amount, details.currency, details.comment) == (
        TON_ADDRESS,
        "0.995",
        "TON",
        "0B9C6A8E6A4E",
    )
    assert details.deep_link == f"ton://transfer/{TON_ADDRESS}?amount=995000000&text=0B9C6A8E6A4E"
    assert onchain.instructions(gw, payment(provider="cryptobot", provider_amount="1")) is None
    assert onchain.instructions(object(), payment(provider_amount="1")) is None
    assert gw.supports_polling and gw.polls_by_payment_id
    with pytest.raises(WebhookRejected) as exc:
        await gw.parse_webhook(b"{}", {}, "1.2.3.4")
    assert exc.value.status_code == 404


def ton_tx(
    *,
    tx_hash: str = "TXHASH=",
    comment: str | None = "0B9C6A8E6A4E",
    value: int = 995_000_000,
    destination: str = TON_RAW,
    bounced: bool = False,
    aborted: bool = False,
    now: int = CREATED_TS + 60,
    finality: str | None = "finalized",
    mc_block_seqno: int | None = 104115068,
) -> dict[str, Any]:
    content: dict[str, Any] = {"hash": "h", "body": "te6"}
    if comment is not None:
        content["decoded"] = {"@type": "text_comment", "comment": comment, "type": "text_comment"}
    tx: dict[str, Any] = {
        "account": TON_RAW,
        "hash": tx_hash,
        "lt": "1",
        "now": now,
        "mc_block_seqno": mc_block_seqno,
        "description": {"type": "ord", "aborted": aborted, "compute_ph": {"success": True}},
        "in_msg": {
            "source": OTHER_TON_RAW,
            "destination": destination,
            "value": str(value),
            "bounce": False,
            "bounced": bounced,
            "opcode": "0x00000000",
            "decoded_opcode": "text_comment",
            "message_content": content,
        },
        "out_msgs": [],
    }
    if finality is not None:
        tx["finality"] = finality
    return tx


async def ton_fetch(api: FakeApi, *txs: dict[str, Any], **kw: Any) -> Any:
    api.reply("/api/v3/transactions", (200, {"transactions": list(txs), "address_book": {}}))
    gw = ton_gateway(api, **kw)
    try:
        return await gw.fetch(payment(provider_amount="0.995"))
    finally:
        await gw.close()


async def test_ton_fetch_matches_comment_and_amount(api: FakeApi) -> None:
    status = await ton_fetch(api, ton_tx(tx_hash="OTHER=", comment="FFFFFFFFFFFF"), ton_tx(), api_key="tc-key")
    assert status.status is PaymentStatus.SUCCEEDED and status.provider_payment_id == "TXHASH="
    (call,) = api.calls("/api/v3/transactions")
    assert call.query["account"] == [TON_ADDRESS]
    assert call.query["start_utime"] == [str(CREATED_TS - onchain.CLOCK_SKEW_SEC)]
    assert call.query["sort"] == ["desc"] and call.query["limit"] == ["100"]
    assert call.headers.get("X-Api-Key") == "tc-key"


async def test_ton_overpayment_and_case_insensitive_comment(api: FakeApi) -> None:
    status = await ton_fetch(api, ton_tx(value=2_000_000_000, comment=" 0b9c6a8e6a4e "))
    assert status.status is PaymentStatus.SUCCEEDED


@pytest.mark.parametrize(
    "tx",
    [
        ton_tx(comment="0B9C6A8E6A4F"),
        ton_tx(comment=None),
        ton_tx(value=994_999_999),
        ton_tx(destination=OTHER_TON_RAW),
        ton_tx(bounced=True),
        ton_tx(aborted=True),
        ton_tx(now=CREATED_TS - 3600),
        ton_tx(finality="confirmed"),
        ton_tx(mc_block_seqno=None),
    ],
    ids=["comment", "no-comment", "short", "address", "bounced", "aborted", "too-old", "not-final", "no-mc-block"],
)
async def test_ton_ignores_non_matching(api: FakeApi, tx: dict[str, Any]) -> None:
    status = await ton_fetch(api, tx)
    assert status.status is PaymentStatus.PENDING and status.provider_payment_id is None


async def test_ton_malformed_response(api: FakeApi) -> None:
    api.reply("/api/v3/transactions", (200, {"error": "x"}))
    gw = ton_gateway(api)
    try:
        with pytest.raises(GatewayError):
            await gw.fetch(payment(provider_amount="0.995"))
    finally:
        await gw.close()


async def test_ton_transactions_are_shared_between_polls(api: FakeApi) -> None:
    api.reply("/api/v3/transactions", (200, {"transactions": [], "address_book": {}}))
    gw = ton_gateway(api)
    try:
        await gw.fetch(payment(provider_amount="0.995"))
        await gw.fetch(payment(provider_amount="0.995", id="1" * 8 + PAYMENT_ID[8:], created_at=CREATED + timedelta(1)))
        assert len(api.calls("/api/v3/transactions")) == 1
        await gw.fetch(payment(provider_amount="0.995", created_at=CREATED - timedelta(1)))  # wider window
        assert len(api.calls("/api/v3/transactions")) == 2
    finally:
        await gw.close()


# ---------------------------------------------------------------------------------------------
# USDT TRC-20
# ---------------------------------------------------------------------------------------------
def usdt_gateway(api: FakeApi, session_factory: SessionFactory, **kw: Any) -> UsdtTrc20Gateway:
    kw.setdefault("rate_rub", 80)
    gw = UsdtTrc20Gateway(
        TRON_ADDRESS, session_factory, base_url=api.base_url, rate_url=f"{api.base_url}/simple/price", **kw
    )
    gw._retry_delay = 0
    return gw


async def add_payment(session_factory: SessionFactory, pid: str, **kw: Any) -> Payment:
    async with session_factory.begin() as s:
        if await s.get(User, USER_ID) is None:
            s.add(user())
            await s.flush()
        row = payment(id=pid, provider="usdt_trc20", **kw)
        s.add(row)
    return row


def pid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


async def test_usdt_amounts_are_unique_per_open_payment(api: FakeApi, session_factory: SessionFactory) -> None:
    gw = usdt_gateway(api, session_factory)
    try:
        amounts = []
        for n in range(1, 4):
            row = await add_payment(session_factory, pid(n))
            invoice = await gw.create(row, user(), "x")
            assert invoice.url is None and invoice.provider_currency == "USDT" and invoice.provider_payment_id is None
            amounts.append(invoice.provider_amount)
        assert amounts == ["2.491", "2.492", "2.493"]  # 199 RUB / 80 = 2.4875 -> 2.49 + unique fraction
        async with session_factory() as s:
            keys = {k: v for k, v in await s.execute(select(KeyValue.key, KeyValue.value))}
        assert keys["usdt_trc20:249:1000"] == pid(1)
        details = onchain.instructions(gw, payment(provider="usdt_trc20", provider_amount="2.491"))
        assert details is not None
        assert (details.address, details.amount, details.comment, details.wallet_url) == (
            TRON_ADDRESS,
            "2.491",
            None,
            None,
        )
    finally:
        await gw.close()


async def test_usdt_slot_reuse_rules(api: FakeApi, session_factory: SessionFactory) -> None:
    gw = usdt_gateway(api, session_factory)
    now = datetime.now(UTC)
    try:
        first = await add_payment(session_factory, pid(1))
        assert (await gw.create(first, user(), "x")).provider_amount == "2.491"
        async with session_factory.begin() as s:
            row = await s.get(Payment, pid(1))
            assert row is not None
            row.status, row.updated_at = PaymentStatus.EXPIRED, now - timedelta(hours=1)
        second = await add_payment(session_factory, pid(2))
        assert (await gw.create(second, user(), "x")).provider_amount == "2.492"  # recently expired: kept
        async with session_factory.begin() as s:
            row = await s.get(Payment, pid(1))
            assert row is not None
            row.updated_at = now - timedelta(days=4)
        third = await add_payment(session_factory, pid(3))
        assert (await gw.create(third, user(), "x")).provider_amount == "2.491"  # long expired: reused
        async with session_factory.begin() as s:
            row = await s.get(Payment, pid(2))
            assert row is not None
            row.status = PaymentStatus.SUCCEEDED
        fourth = await add_payment(session_factory, pid(4))
        assert (await gw.create(fourth, user(), "x")).provider_amount == "2.492"  # paid: reused
    finally:
        await gw.close()


def trc20(
    *,
    tx_id: str = "aa" * 32,
    value: str = "2491000",
    to: str = TRON_ADDRESS,
    contract: str = USDT_CONTRACT,
    timestamp: int = (CREATED_TS + 60) * 1000,
    kind: str = "Transfer",
) -> dict[str, Any]:
    return {
        "transaction_id": tx_id,
        "token_info": {"symbol": "USDT", "address": contract, "decimals": 6, "name": "Tether USD"},
        "block_timestamp": timestamp,
        "from": OTHER_TRON,
        "to": to,
        "type": kind,
        "value": value,
    }


TRC20_PATH = f"/v1/accounts/{TRON_ADDRESS}/transactions/trc20"


async def usdt_fetch(api: FakeApi, session_factory: SessionFactory, *records: dict[str, Any], **kw: Any) -> Any:
    api.reply(TRC20_PATH, (200, {"data": list(records), "success": True, "meta": {"at": 1, "page_size": 1}}))
    gw = usdt_gateway(api, session_factory, **kw)
    try:
        return await gw.fetch(payment(provider="usdt_trc20", provider_amount="2.491"))
    finally:
        await gw.close()


async def test_usdt_fetch_matches_exact_amount(api: FakeApi, session_factory: SessionFactory) -> None:
    status = await usdt_fetch(api, session_factory, trc20(tx_id="bb" * 32, value="2492000"), trc20(), api_key="tg")
    assert status.status is PaymentStatus.SUCCEEDED and status.provider_payment_id == "aa" * 32
    (call,) = api.calls(TRC20_PATH)
    assert call.query["only_to"] == ["true"] and call.query["only_confirmed"] == ["true"]
    assert call.query["contract_address"] == [USDT_CONTRACT]
    assert call.query["min_timestamp"] == [str((CREATED_TS - onchain.CLOCK_SKEW_SEC) * 1000)]
    assert call.headers.get("TRON-PRO-API-KEY") == "tg"


@pytest.mark.parametrize(
    "record",
    [
        trc20(value="2491001"),
        trc20(value="2500000"),
        trc20(to=OTHER_TRON),
        trc20(contract="TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"),
        trc20(timestamp=(CREATED_TS - 3600) * 1000),
        trc20(kind="Approval"),
    ],
    ids=["amount", "overpaid", "address", "contract", "too-old", "not-transfer"],
)
async def test_usdt_ignores_non_matching(api: FakeApi, session_factory: SessionFactory, record: dict[str, Any]) -> None:
    status = await usdt_fetch(api, session_factory, record)
    assert status.status is PaymentStatus.PENDING


async def test_usdt_skips_transfers_used_by_another_payment(api: FakeApi, session_factory: SessionFactory) -> None:
    await add_payment(session_factory, pid(9), provider_payment_id="aa" * 32)
    status = await usdt_fetch(api, session_factory, trc20())
    assert status.status is PaymentStatus.PENDING
    status = await usdt_fetch(api, session_factory, trc20(), trc20(tx_id="cc" * 32))
    assert status.provider_payment_id == "cc" * 32


async def test_usdt_paginates_and_rejects_errors(api: FakeApi, session_factory: SessionFactory) -> None:
    page = [trc20(tx_id=f"{n:064x}", value="1") for n in range(200)]
    api.reply(
        TRC20_PATH,
        (200, {"data": page, "success": True, "meta": {"fingerprint": "FP1"}}),
        (200, {"data": [trc20()], "success": True, "meta": {}}),
    )
    gw = usdt_gateway(api, session_factory)
    try:
        status = await gw.fetch(payment(provider="usdt_trc20", provider_amount="2.491"))
        assert status.provider_payment_id == "aa" * 32
        calls = api.calls(TRC20_PATH)
        assert len(calls) == 2 and calls[1].query["fingerprint"] == ["FP1"]
        assert calls[1].query["min_timestamp"] == calls[0].query["min_timestamp"]
    finally:
        await gw.close()
    api.reply(TRC20_PATH, (200, {"success": False, "error": "bad"}))
    gw = usdt_gateway(api, session_factory)
    try:
        with pytest.raises(GatewayError):
            await gw.fetch(payment(provider="usdt_trc20", provider_amount="2.491", created_at=CREATED - timedelta(1)))
    finally:
        await gw.close()


async def test_usdt_reconcile_credits_once(api: FakeApi, session_factory: SessionFactory) -> None:
    settings = make_settings()
    async with session_factory.begin() as s:
        s.add(user())
    gw = usdt_gateway(api, session_factory)
    try:
        created = await service.create_payment(session_factory, settings, gw, USER_ID, 19900)
        assert created.status is PaymentStatus.PENDING and created.provider_amount == "2.491"
        assert created.provider_payment_id is None
        record = trc20(timestamp=int(created.created_at.timestamp() * 1000) + 5000)
        api.reply(TRC20_PATH, (200, {"data": [record], "success": True, "meta": {}}))
        assert await service.reconcile(session_factory, settings, {gw.key: gw}) == 1
        assert await service.reconcile(session_factory, settings, {gw.key: gw}) == 0
        async with session_factory() as s:
            stored = await s.get(Payment, created.id)
            balance = (await s.get(User, USER_ID)).balance_kop  # type: ignore[union-attr]
        assert stored is not None and stored.status is PaymentStatus.SUCCEEDED
        assert stored.provider_payment_id == "aa" * 32 and balance == 19900
    finally:
        await gw.close()


# ---------------------------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------------------------
def test_registry_and_settings_validation(session_factory: SessionFactory) -> None:
    settings = make_settings(
        ton_enabled=True,
        ton_wallet_address=TON_ADDRESS,
        ton_rate_rub=200,
        usdt_trc20_enabled=True,
        usdt_trc20_address=TRON_ADDRESS,
        usdt_trc20_rate_rub=80,
    )
    assert set(build_gateways(settings)) == {"ton"}  # USDT needs the database for amount reservations
    gateways = build_gateways(settings, session_factory=session_factory)
    assert {"ton", "usdt_trc20"} <= set(gateways)
    with pytest.raises(ValueError):
        make_settings(ton_enabled=True, ton_wallet_address="nope", ton_rate_rub=200)
    with pytest.raises(ValueError):
        make_settings(usdt_trc20_enabled=True, usdt_trc20_address=TRON_ADDRESS)  # no rate
    make_settings(usdt_trc20_enabled=True, usdt_trc20_address=TRON_ADDRESS, crypto_rate_auto=True)


def test_locales_have_onchain_strings() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "modules" / "payments" / "locales"
    for lang in ("ru", "en"):
        data = tomllib.loads((root / f"{lang}.toml").read_text(encoding="utf-8"))["payments"]
        assert {"ton", "usdt_trc20"} <= set(data["provider"])
        assert {"head", "address", "amount", "comment", "hint_comment", "hint_amount", "footer"} <= set(data["onchain"])


def test_wallet_link_query_is_well_formed() -> None:
    gw = TonGateway(TON_ADDRESS, rate_rub=200)
    details = gw.transfer_instructions(payment(provider_amount="1.5"))
    assert details.wallet_url is not None
    query = parse_qs(urlsplit(details.wallet_url).query)
    assert query == {"amount": ["1500000000"], "text": ["0B9C6A8E6A4E"]}


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_transfer_screen_renders(lang: str) -> None:
    from app.core.i18n import I18n, Translator
    from app.keyboards import Button
    from app.modules.payments.onchain import transfer_screen

    catalog = I18n(None)
    catalog.load_files()
    _ = Translator(catalog, lang)
    gw = TonGateway(TON_ADDRESS, rate_rub=200)
    pay = payment(provider_amount="0.995")
    details = gw.transfer_instructions(pay)
    screen = transfer_screen(_, pay, details, hours=24, check=Button.cb("check", "x"), footer=[Button.cb("menu", "m")])
    assert TON_ADDRESS in screen.text and "0B9C6A8E6A4E" in screen.text and "ton://transfer/" in screen.text
    assert "payments.onchain" not in screen.text  # every key resolved
    assert screen.kb is not None
    buttons = [b for row in screen.kb.inline_keyboard for b in row]
    assert buttons[0].url == details.wallet_url
    assert [b.copy_text.text for b in buttons if b.copy_text] == [TON_ADDRESS, "0.995", "0B9C6A8E6A4E"]
