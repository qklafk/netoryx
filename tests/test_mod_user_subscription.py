"""Subscription screens: card, trial, auto-renewal, link rotation, connection guide, QR and devices."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import SendPhoto

from app.core.i18n import I18n
from app.db.models import Panel, PanelKind, Provision, ProvisionState
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.subscription import views
from app.modules.subscription.callbacks import ConnCb, DevCb, SubAct
from app.modules.subscription.state import Status, SubState
from app.services.deeplinks import incy_decrypt_link
from app.services.devices import DeviceInfo
from app.services.invites import UNLIMITED_EXPIRES_AT
from app.services.panels.base import RemoteAccount
from tests.fakes import FakePanel
from tests.test_mod_user_support import (
    USER,
    Harness,
    add_plan,
    add_subscription,
    get_sub,
    get_user,
    make_harness,
)

Make = Callable[..., Harness]


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


# --- formatting -------------------------------------------------------------------------------------------------------
def test_traffic_and_discount_formatting(tmp_path: Path) -> None:
    from tests.test_mod_user_support import make_i18n

    i18n: I18n = make_i18n(tmp_path)
    ru, en = i18n.translator("ru"), i18n.translator("en")
    assert views.fmt_traffic(ru, 300 * 1024**2) == "300 МБ"
    assert views.fmt_traffic(ru, int(1.5 * 1024**3)) == "1,5 ГБ"
    assert views.fmt_traffic(en, 2 * 1024**3) == "2 GB"
    plans = [_plan(30, 15_000), _plan(90, 40_000), _plan(365, 140_000)]
    reference = views.reference_monthly_kop(plans)
    assert reference == 15_000
    assert [views.discount_percent(p, reference) for p in plans] == [0, 11, 23]


def _plan(days: int, price: int) -> Any:
    from app.db.models import Plan

    return Plan(title=f"{days}", duration_days=days, price_kop=price, device_limit=3, traffic_limit_gb=0)


# --- card -------------------------------------------------------------------------------------------------------------
async def test_card_without_subscription_offers_trial_and_plans(make: Make) -> None:
    h = make()
    await h.send("/sub")
    assert h.t("subscription.card.none") in h.last_text()
    assert h.t("subscription.btn.trial") in h.button_texts()
    assert h.t("subscription.btn.buy") in h.button_texts()


async def test_card_with_plan_shows_terms_and_toggles_auto_renew(make: Make) -> None:
    h = make()
    await h.send("/start")
    plan_id = await add_plan(h.sf, title="Месяц", price_kop=15_000)
    await add_subscription(h.sf, days=10, plan_id=plan_id, device_limit=2)
    await h.press(Nav(to="sub"))
    text = h.last_text()
    assert "<b>Месяц</b>" in text and "2 устройства" in text and "ещё 10 дней" in text
    assert h.t("subscription.card.auto_renew_on") in text
    assert "Для автопродления нужно 150 ₽" in text  # empty wallet
    await h.press(SubAct(act="ar0"))
    sub = await get_sub(h.sf)
    assert sub is not None and sub.auto_renew is False
    assert h.t("subscription.auto_renew.disabled") in h.popups()
    assert h.t("subscription.btn.auto_renew_on") in h.button_texts()


async def test_card_of_unlimited_complimentary_access(make: Make) -> None:
    h = make()
    await h.send("/start")
    await add_subscription(h.sf, expires_at=UNLIMITED_EXPIRES_AT, is_complimentary=True, auto_renew=False)
    await h.send("/sub")
    text = h.last_text()
    assert h.t("subscription.card.complimentary") in text and "бессрочно" in text
    assert h.t("subscription.btn.renew") not in h.button_texts()
    assert h.t("subscription.btn.auto_renew_on") not in h.button_texts()


async def test_expired_card_offers_renewal(make: Make) -> None:
    h = make()
    await h.send("/start")
    await add_subscription(h.sf, days=-1)
    await h.send("/sub")
    assert h.t("subscription.card.status_expired") in h.last_text()
    assert h.t("subscription.btn.renew") in h.button_texts()
    assert h.t("subscription.btn.connect") not in h.button_texts()


# --- trial ------------------------------------------------------------------------------------------------------------
async def test_trial_once(make: Make) -> None:
    h = make()
    await h.send("/start")
    await h.press(Nav(to="trial"))
    sub = await get_sub(h.sf)
    assert sub is not None and sub.is_trial and sub.device_limit == 1
    # The owner notification is dispatched asynchronously and can be recorded
    # after the user's screen by the shared fake Bot API.
    assert any("Пробный период активирован" in text for text, _ in h.screens())
    await h.press(Nav(to="trial"))
    assert h.t("subscription.trial.used") in h.popups()


async def test_no_trial_in_personal_mode(make: Make) -> None:
    h = make(app_mode="personal")
    await h.send("/start", uid=1000)  # owners always get in
    await h.press(Nav(to="trial"), uid=1000)
    assert h.t("subscription.trial.unavailable") in h.popups()
    assert await get_sub(h.sf, 1000) is None


# --- link rotation ----------------------------------------------------------------------------------------------------
async def test_rotate_link(make: Make) -> None:
    h = make()
    await h.send("/start")
    await add_subscription(h.sf)
    old = (await get_user(h.sf)).sub_token
    await h.press(SubAct(act="rot"))
    assert "Старая ссылка перестанет работать" in h.last_text()
    await h.press(SubAct(act="rot!"))
    new = (await get_user(h.sf)).sub_token
    assert new != old
    assert f"https://bot.example.com/sub/{new}" in h.last_text()


# --- connection -------------------------------------------------------------------------------------------------------
async def test_connect_screen_and_app_guide(make: Make) -> None:
    h = make()
    await h.send("/start")
    await add_subscription(h.sf)
    token = (await get_user(h.sf)).sub_token
    await h.send("/connect")
    url = f"https://bot.example.com/sub/{token}"
    assert url in h.last_text()
    copy = h.button(h.t("subscription.btn.copy"))
    assert copy.copy_text.text == url
    assert h.t("subscription.platform.ios") in h.button_texts()

    await h.press(ConnCb(p="android"))
    assert "Happ" in h.last_text()
    await h.press(ConnCb(p="android", a="incy"))
    add = h.button(h.t("subscription.app.add", app="INCY"))
    assert add.url == f"https://bot.example.com/open/incy/{token}"
    assert any(b.text.startswith("⬇️") for b in h.last_buttons())
    link = h.last_text().split("<code>")[-1].split("</code>")[0]
    assert incy_decrypt_link(link)[0] == url


async def test_connect_requires_an_active_subscription(make: Make) -> None:
    h = make()
    await h.send("/start")
    await add_subscription(h.sf, days=-2)
    await h.send("/connect")
    assert h.t("subscription.connect.need_sub") in h.last_text()


async def test_qr_code_is_sent_as_photo(make: Make) -> None:
    h = make()
    await h.send("/start")
    await add_subscription(h.sf)
    await h.press(Nav(to="qr"))
    photos = h.api.of(SendPhoto)
    assert photos and photos[-1].caption == h.t("subscription.connect.qr")


async def test_lte_note_when_whitelist_endpoints_exist(make: Make) -> None:
    from app.db.models import Endpoint, EndpointKind, EndpointProfile, Inbound

    h = make()
    await h.send("/start")
    await add_subscription(h.sf)
    async with h.sf.begin() as session:
        panel = Panel(name="DE", kind=PanelKind.XUI, url="https://p.example.com")
        session.add(panel)
        await session.flush()
        inbound = Inbound(panel_id=panel.id, remote_id=1, name="in")
        session.add(inbound)
        await session.flush()
        session.add(
            Endpoint(
                inbound_id=inbound.id,
                kind=EndpointKind.DIRECT,
                profile=EndpointProfile.WHITELIST,
                address="1.2.3.4",
            )
        )
    await h.send("/connect")
    assert h.t("subscription.connect.lte") in h.last_text()


# --- devices ----------------------------------------------------------------------------------------------------------
@dataclass
class ListingPanel(FakePanel):
    """A panel that lists devices and deletes them one by one (like Remnawave)."""

    devices: list[DeviceInfo] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    async def list_devices(self, account: RemoteAccount) -> list[DeviceInfo]:
        return list(self.devices)

    async def delete_device(self, account: RemoteAccount, device_id: str) -> None:
        self.deleted.append(device_id)
        self.devices = [d for d in self.devices if d.id != device_id]


async def _with_provision(h: Harness, backend: FakePanel, kind: PanelKind) -> int:
    sub_id = await add_subscription(h.sf)
    async with h.sf.begin() as session:
        panel = Panel(name="P", kind=kind, url="https://p.example.com")
        session.add(panel)
        await session.flush()
        session.add(
            Provision(
                subscription_id=sub_id,
                panel_id=panel.id,
                inbound_key=0,
                remote_email=f"tg_{USER}",
                desired_state=ProvisionState.ENABLED,
                actual_state=ProvisionState.ENABLED,
            )
        )
        panel_id = panel.id
    h.ctx.panels.backends[panel_id] = backend
    return panel_id


async def test_devices_list_and_delete_one(make: Make) -> None:
    h = make(device_reset_cooldown_min=0)
    await h.send("/start")
    backend = ListingPanel(
        kind=PanelKind.REMNAWAVE,
        devices=[
            DeviceInfo(id="hwid-1", platform="iOS", model="iPhone 15"),
            DeviceInfo(id="hwid-2", platform="Android"),
        ],
    )
    panel_id = await _with_provision(h, backend, PanelKind.REMNAWAVE)
    await h.press(Nav(to="devices"))
    assert "iPhone 15" in h.last_text() and "Android" in h.last_text()
    await h.press(DevCb(act="del", panel=panel_id, i=0))
    assert backend.deleted == ["hwid-1"]
    assert h.t("subscription.devices.deleted") in h.popups()
    assert "iPhone 15" not in h.last_text()


async def test_reset_all_devices_with_cooldown(make: Make) -> None:
    h = make(device_reset_cooldown_min=30)
    await h.send("/start")
    backend = FakePanel(kind=PanelKind.XUI)
    await _with_provision(h, backend, PanelKind.REMNAWAVE)
    await h.press(Nav(to="devices"))
    assert h.t("subscription.devices.not_listed") in h.last_text()
    await h.press(DevCb(act="all"))
    assert "Отвязать все устройства?" in h.last_text()
    await h.press(DevCb(act="all!"))
    assert ("reset_devices", None, f"tg_{USER}") in backend.calls
    assert h.t("subscription.devices.reset_done") in h.popups()
    await h.press(DevCb(act="all!"))
    assert any("Отвязывать устройства можно не так часто" in p for p in h.popups())


# --- state ------------------------------------------------------------------------------------------------------------
def test_state_status_rules() -> None:
    from app.db.models import Subscription, SubscriptionStatus, User

    now = datetime(2026, 11, 4, tzinfo=UTC)
    user = User(tg_id=1, trial_used=False, is_banned=False, balance_kop=0)
    active = Subscription(status=SubscriptionStatus.ACTIVE, expires_at=now + timedelta(hours=1))
    assert SubState(user, active, None, now).status == Status.ACTIVE
    stale = Subscription(status=SubscriptionStatus.ACTIVE, expires_at=now - timedelta(seconds=1))
    assert SubState(user, stale, None, now).status == Status.EXPIRED  # before the expiry job ran
    blocked = Subscription(status=SubscriptionStatus.DISABLED, expires_at=now + timedelta(days=1))
    assert SubState(user, blocked, None, now).status == Status.DISABLED
    assert SubState(user, None, None, now).status == Status.NONE
