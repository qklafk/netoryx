""" "The VPN does not work": the subscription self-check and its bot screen."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import (
    Panel,
    PanelKind,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionDevice,
    SubscriptionStatus,
    Ticket,
    TicketMessage,
    User,
)
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.core_user import module as core_user_module
from app.modules.subscription import module as subscription_module
from app.modules.subscription.callbacks import DiagCb
from app.modules.tickets import module as tickets_module
from app.services import diagnostics
from app.services.diagnostics import Problem
from tests.conftest import make_settings
from tests.test_mod_user_support import USER, Harness, add_subscription, make_harness

Make = Callable[..., Harness]
OTHER = 777


@pytest.fixture
async def make(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Make]:
    harnesses: list[Harness] = []

    def factory(**settings: Any) -> Harness:
        harness = make_harness(session_factory, tmp_path / str(len(harnesses)), **settings)
        harnesses.append(harness)
        return harness

    yield factory
    for harness in harnesses:
        await harness.close()


async def _user(sf: SessionFactory, uid: int = USER, **values: Any) -> None:
    async with sf.begin() as s:
        s.add(User(tg_id=uid, referral_code=f"R{uid}", sub_token=f"t{uid}", **values))


async def _panel(sf: SessionFactory, sub_id: int, *, failures: int = 0, synced: bool = True, name: str = "P") -> int:
    async with sf.begin() as s:
        panel = Panel(name=name, kind=PanelKind.XUI, url=f"https://{name.lower()}.example.com")
        panel.consecutive_failures = failures
        s.add(panel)
        await s.flush()
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        s.add(
            Provision(
                subscription_id=sub_id,
                panel_id=panel.id,
                inbound_key=0,
                remote_email=f"tg_{USER}_{name}",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED if synced else ProvisionState.ABSENT,
                synced_revision=sub.revision if synced else 0,
            )
        )
        return panel.id


async def _diag(sf: SessionFactory, sub_id: int, uid: int = USER, **settings: Any) -> diagnostics.Diagnosis:
    return await diagnostics.diagnose(sf, make_settings(**settings), uid, sub_id)


# --- the check --------------------------------------------------------------------------------------------------------
async def test_a_healthy_subscription_has_no_problems(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    sub_id = await add_subscription(session_factory)
    await _panel(session_factory, sub_id, name="A")
    await _panel(session_factory, sub_id, name="B")
    found = await _diag(session_factory, sub_id)
    assert found.ok and found.servers == 2 and found.servers_down == 0
    assert found.summary() == "problems=none; devices=0/3; servers=2; down=0; unsynced=0"


async def test_ended_or_suspended_subscription_is_reported_first(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    expired = await add_subscription(session_factory, days=-1)
    assert (await _diag(session_factory, expired)).problems == (Problem.EXPIRED,)
    async with session_factory.begin() as s:
        sub = await s.get(Subscription, expired)
        assert sub is not None
        sub.status = SubscriptionStatus.DISABLED
        sub.expires_at = datetime.now(UTC) + timedelta(days=5)
    assert (await _diag(session_factory, expired)).problems == (Problem.DISABLED,)


async def test_banned_user(session_factory: SessionFactory) -> None:
    await _user(session_factory, is_banned=True)
    sub_id = await add_subscription(session_factory)
    assert Problem.BANNED in (await _diag(session_factory, sub_id)).problems


async def test_full_device_slots_count_only_with_the_bot_side_limit(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    sub_id = await add_subscription(session_factory, device_limit=2)
    await _panel(session_factory, sub_id)
    now = datetime.now(UTC)
    async with session_factory.begin() as s:
        for n in range(2):
            s.add(
                SubscriptionDevice(subscription_id=sub_id, hwid_hash=f"{n:064x}", first_seen_at=now, last_seen_at=now)
            )
    found = await _diag(session_factory, sub_id, xui_device_limit="hwid")
    assert found.problems == (Problem.DEVICES,) and (found.devices, found.device_limit) == (2, 2)
    assert (await _diag(session_factory, sub_id, xui_device_limit="ip")).ok


async def test_used_up_traffic(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    sub_id = await add_subscription(session_factory, traffic_limit_gb=10, traffic_exhausted_epoch=0)
    await _panel(session_factory, sub_id)
    assert Problem.TRAFFIC in (await _diag(session_factory, sub_id)).problems


async def test_settings_not_on_the_server_yet(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    sub_id = await add_subscription(session_factory)
    await _panel(session_factory, sub_id, synced=False)
    found = await _diag(session_factory, sub_id)
    assert found.problems == (Problem.NOT_SYNCED,) and found.unsynced == 1


async def test_unreachable_servers(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    sub_id = await add_subscription(session_factory)
    await _panel(session_factory, sub_id, name="A", failures=3)
    await _panel(session_factory, sub_id, name="B")
    found = await _diag(session_factory, sub_id)
    assert found.problems == (Problem.SERVERS_DOWN,) and (found.servers_down, found.servers) == (1, 2)
    await _panel(session_factory, sub_id, name="C", failures=1)  # one failed check is not "down"
    assert (await _diag(session_factory, sub_id)).servers_down == 1
    lonely = await add_subscription(session_factory, client_uuid="00000000-0000-0000-0000-00000000aaaa")
    await _panel(session_factory, lonely, name="D", failures=5)
    assert (await _diag(session_factory, lonely)).problems == (Problem.ALL_SERVERS_DOWN,)


async def test_subscription_on_no_server(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    sub_id = await add_subscription(session_factory)
    assert (await _diag(session_factory, sub_id)).problems == (Problem.NO_SERVERS,)


async def test_someone_elses_subscription_is_not_checked(session_factory: SessionFactory) -> None:
    await _user(session_factory)
    await _user(session_factory, OTHER)
    sub_id = await add_subscription(session_factory)
    assert (await _diag(session_factory, sub_id, uid=OTHER)).problems == (Problem.NO_SUBSCRIPTION,)


# --- the bot screen ---------------------------------------------------------------------------------------------------
async def test_card_offers_the_check_and_a_healthy_result_shows_the_checklist(make: Make) -> None:
    h = make(support_url="https://t.me/help")
    await h.send("/start")
    sub_id = await add_subscription(h.sf)
    await _panel(h.sf, sub_id)
    await h.send("/sub")
    assert h.t("subscription.diag.open") in h.button_texts()
    await h.press(Nav(to="diag", arg=str(sub_id)))
    text = h.last_text()
    assert h.t("subscription.diag.ok") in text and "1." in text
    assert h.t("subscription.btn.support") in h.button_texts()  # no tickets module: the support link


async def test_expired_subscription_is_explained_with_a_renew_button(make: Make) -> None:
    h = make()
    await h.send("/start")
    sub_id = await add_subscription(h.sf, days=-1)
    await h.press(Nav(to="diag", arg=str(sub_id)))
    assert h.t("subscription.diag.expired") in h.last_text()
    assert h.t("subscription.btn.renew") in h.button_texts()


async def test_unsynced_settings_are_pushed_again(make: Make) -> None:
    h = make()
    await h.send("/start")
    sub_id = await add_subscription(h.sf)
    await _panel(h.sf, sub_id, synced=False)
    await h.press(Nav(to="diag", arg=str(sub_id)))
    text = h.last_text()
    assert h.t("subscription.diag.synced") in text or h.t("subscription.diag.not_synced") in text


async def test_did_not_help_opens_a_ticket_with_the_result(session_factory: SessionFactory, tmp_path: Path) -> None:
    h = make_harness(session_factory, tmp_path, modules=[core_user_module, subscription_module, tickets_module])
    try:
        await h.send("/start")
        sub_id = await add_subscription(h.sf)
        await _panel(h.sf, sub_id)
        await h.press(Nav(to="diag", arg=str(sub_id)))
        assert h.t("subscription.diag.btn_support") in h.button_texts()
        await h.press(DiagCb(a="t", s=sub_id))
        async with h.sf() as s:
            ticket = await s.scalar(select(Ticket).where(Ticket.user_id == USER))
            assert ticket is not None
            body = await s.scalar(select(TicketMessage.text).where(TicketMessage.ticket_id == ticket.id))
        assert body is not None and "problems=none" in body and f"#{sub_id}" in body
        assert any(h.t("subscription.diag.ticket_sent", id=ticket.id) == p for p in h.popups())
        # somebody else's subscription id gives nothing away
        await h.press(DiagCb(a="t", s=sub_id + 1000))
        async with h.sf() as s:
            assert len((await s.scalars(select(Ticket))).all()) == 1
    finally:
        await h.close()
