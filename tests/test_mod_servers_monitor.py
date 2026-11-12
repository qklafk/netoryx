"""Server monitoring module: servers list, live status, core restart, load alerts with confirmation and recovery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import AdminAuditLog, Panel, PanelKind
from app.db.session import SessionFactory
from app.keyboards import Nav
from app.modules.servers_monitor import module as monitor_module
from app.modules.servers_monitor import service
from app.modules.servers_monitor.jobs import monitor_job
from app.modules.servers_monitor.views import NAV, MonCb, bar, decimal, fmt_bytes, fmt_uptime
from app.services import kv, notifications
from app.services.panels.base import NodeStatus, PanelError, ServerStatus
from app.utils.time import utcnow
from tests.fakes import FakeCapablePanel, FakePanel
from tests.test_middlewares_support import callback_update
from tests.test_mod_personal_support import OWNER, Harness, add_user, harnesses, module_i18n

GIB = 1024**3
BUSY = ServerStatus(
    cpu_percent=97.0,
    cpu_cores=4,
    mem_used=7 * GIB,
    mem_total=8 * GIB,
    disk_used=10 * GIB,
    disk_total=40 * GIB,
    load=(1.5, 1.2, 0.9),
    uptime_sec=3 * 86400 + 5 * 3600,
    net_up_bps=1_500_000,
    net_down_bps=3_000_000,
    traffic_sent=120 * GIB,
    traffic_recv=340 * GIB,
    core_state="running",
    core_version="25.10.15",
)


@pytest.fixture
async def harness_factory(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Any]:
    async with harnesses(session_factory, tmp_path) as factory:
        yield factory


async def _panels(sf: SessionFactory, h: Harness, *kinds: PanelKind) -> list[FakePanel]:
    fakes: list[FakePanel] = []
    async with sf.begin() as session:
        for index, kind in enumerate(kinds):
            panel = Panel(name=f"srv{index}", kind=kind, url=f"https://s{index}.example.com", version="3.11.0")
            panel.last_ok_at = utcnow()
            session.add(panel)
            await session.flush()
            fake = FakeCapablePanel(kind=kind)
            h.ctx.panels.panels[panel.id] = fake
            fakes.append(fake)
    await add_user(sf, OWNER)
    return fakes


async def test_list_and_live_status(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(monitor_module)
    (fake,) = await _panels(session_factory, h, PanelKind.XUI)
    assert isinstance(fake, FakeCapablePanel)
    fake.status = BUSY
    await h.feed(callback_update(OWNER, Nav(to=NAV).pack()))
    assert "🟢 srv0 · 3x-ui" in [b.text for b in h.buttons()]

    await h.feed(callback_update(OWNER, h.find("mon:show:")))
    text = h.last_text()
    for part in ("Xray работает 25.10.15", "Процессор: 97%", "4 ядер", "7 ГБ из 8 ГБ (88%)", "↑ 1,4 МБ/с", "3 д 5 ч"):
        assert part in text, part
    assert h.find("mon:restart:")


async def test_restart_with_confirmation_and_audit(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(monitor_module)
    fake, broken = await _panels(session_factory, h, PanelKind.XUI, PanelKind.XUI)
    assert isinstance(fake, FakeCapablePanel)
    panel_id, broken_id = sorted(h.ctx.panels.panels)
    await h.feed(callback_update(OWNER, MonCb(action="restart", id=panel_id).pack()))
    assert "Перезапустить Xray" in h.last_text()
    await h.feed(callback_update(OWNER, h.find("mon:restart_ok:")))
    assert fake.restarts == 1 and "✅ Перезапуск выполнен" in h.popups()
    async with session_factory() as session:
        assert list(await session.scalars(select(AdminAuditLog.action))) == ["panel.restart_core"]

    broken.fail_next = 1
    await h.feed(callback_update(OWNER, MonCb(action="restart", id=broken_id).pack()))
    await h.feed(callback_update(OWNER, h.find("mon:restart_ok:")))
    assert any(p and "Не удалось перезапустить" in p for p in h.popups())


async def test_restart_can_be_retried_and_repeated(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(monitor_module)
    (fake,) = await _panels(session_factory, h, PanelKind.XUI)
    assert isinstance(fake, FakeCapablePanel)
    (panel_id,) = h.ctx.panels.panels

    async def confirm() -> str:
        await h.feed(callback_update(OWNER, MonCb(action="restart", id=panel_id).pack()))
        return h.find("mon:restart_ok:")

    fake.fail_next = 1
    await h.feed(callback_update(OWNER, await confirm()))
    assert fake.restarts == 0 and any(p and "Не удалось перезапустить" in p for p in h.popups())
    await h.feed(callback_update(OWNER, await confirm()))  # a retry after the failure
    assert fake.restarts == 1
    button = await confirm()  # a later restart from the same status message
    await h.feed(callback_update(OWNER, button))
    await h.feed(callback_update(OWNER, button))  # a double tap of one confirmation restarts once
    assert fake.restarts == 2


async def test_errors_and_unsupported(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(monitor_module)
    await _panels(session_factory, h, PanelKind.XUI, PanelKind.REMNAWAVE)
    first, second = sorted(h.ctx.panels.panels)
    h.ctx.panels.panels[second] = FakePanel(kind=PanelKind.REMNAWAVE)  # no server_status
    h.ctx.panels.panels[first].down = True
    await h.feed(callback_update(OWNER, MonCb(action="show", id=first).pack()))
    assert "Не удалось получить состояние" in h.last_text()
    await h.feed(callback_update(OWNER, MonCb(action="show", id=second).pack()))
    assert "не сообщает состояние" in h.last_text()


async def test_remnawave_nodes_are_listed(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(monitor_module)
    (fake,) = await _panels(session_factory, h, PanelKind.REMNAWAVE)
    assert isinstance(fake, FakeCapablePanel)
    fake.status = ServerStatus(
        online_users=7,
        nodes=(
            NodeStatus("DE-1", online=True, users_online=7, traffic_used_bytes=5 * GIB),
            NodeStatus("NL-1", online=False, message="connect ECONNREFUSED"),
        ),
    )
    await h.feed(callback_update(OWNER, MonCb(action="show", id=next(iter(h.ctx.panels.panels))).pack()))
    text = h.last_text()
    assert "на связи 1 из 2" in text and "🟢 DE-1 · 7 онлайн · 5 ГБ" in text and "NL-1 — нет связи" in text
    assert any("Перезапустить ноды" in b.text for b in h.buttons())


async def test_viewers_cannot_open_it(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(monitor_module)
    await _panels(session_factory, h, PanelKind.XUI)
    await h.feed(callback_update(5555, Nav(to=NAV).pack()))
    assert not [c for c in h.api.calls if type(c).__name__ in ("SendMessage", "EditMessageText")]


async def test_job_alerts_after_confirmation_and_on_recovery(
    harness_factory: Any, session_factory: SessionFactory
) -> None:
    h: Harness = harness_factory(monitor_module, monitor_cpu_alert_percent=80)
    (fake,) = await _panels(session_factory, h, PanelKind.XUI)
    assert isinstance(fake, FakeCapablePanel)
    fake.status = ServerStatus(cpu_percent=99.0, core_state="stop", core_error="config error")

    await monitor_job(h.ctx)  # type: ignore[arg-type]
    await notifications.close(h.ctx, drain_seconds=2)  # type: ignore[arg-type]
    assert not h.sent_to(OWNER)  # seen once: not confirmed yet
    await monitor_job(h.ctx)  # type: ignore[arg-type]
    await notifications.close(h.ctx, drain_seconds=2)  # type: ignore[arg-type]
    alerts = h.sent_to(OWNER)
    assert any("процессор загружен на 99%" in a for a in alerts)
    assert any("Xray не работает (stop): config error" in a for a in alerts)

    await monitor_job(h.ctx)  # type: ignore[arg-type]  # still the same: no repeats
    await notifications.close(h.ctx, drain_seconds=2)  # type: ignore[arg-type]
    assert len(h.sent_to(OWNER)) == 2

    fake.status = ServerStatus(cpu_percent=10.0, core_state="running")
    await monitor_job(h.ctx)  # type: ignore[arg-type]
    await notifications.close(h.ctx, drain_seconds=2)  # type: ignore[arg-type]
    recovered = h.sent_to(OWNER)[2:]
    assert any("Xray снова работает" in a for a in recovered) and any("снизилась" in a for a in recovered)
    async with session_factory() as session:
        assert await kv.get(session, service.state_key(1)) is None


async def test_job_skips_down_and_unsupported_panels(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(monitor_module)
    await _panels(session_factory, h, PanelKind.XUI)
    async with session_factory.begin() as session:
        panel = (await session.scalars(select(Panel))).one()
        panel.consecutive_failures = 10
    h.ctx.panels.panels[panel.id].fail_next = 0
    h.ctx.panels.panels[panel.id].status = ServerStatus(cpu_percent=100.0)
    for _ in range(3):
        await monitor_job(h.ctx)  # type: ignore[arg-type]
    await notifications.close(h.ctx, drain_seconds=1)  # type: ignore[arg-type]
    assert not h.sent_to(OWNER)


def test_advance_confirms_and_clears() -> None:
    state = service.AlertState()
    assert service.advance(state, {"cpu": {"value": 95}}) == []
    (raised,) = service.advance(state, {"cpu": {"value": 96}})
    assert raised.raised and raised.values == {"value": 96}
    assert service.advance(state, {"cpu": {"value": 97}}) == []
    assert service.advance(state, {"mem": {"value": 91}}) == [service.Transition("cpu", {"value": 97}, raised=False)]
    assert service.advance(state, {}) == [] and state.seen == {} and state.active == {}
    restored = service.AlertState.from_json(service.AlertState({"core": {}}, {"cpu": 1}).to_json())
    assert restored.active == {"core": {}} and restored.seen == {"cpu": 1}
    assert service.AlertState.from_json("not json").active == {}


def test_problems_and_thresholds(harness_factory: Any) -> None:
    limits = service.Thresholds(cpu=90, mem=90, disk=50)
    found = service.problems(BUSY, limits)
    assert set(found) == {"cpu", "disk"} or set(found) == {"cpu"}
    nodes = ServerStatus(nodes=(NodeStatus("a", online=False), NodeStatus("b", online=False, disabled=True)))
    assert set(service.problems(nodes, limits)) == {"node:a"}
    h: Harness = harness_factory(monitor_module, monitor_ram_alert_percent=75)
    assert service.thresholds_from(h.ctx.settings) == service.Thresholds(mem=75)
    with pytest.raises(ValueError, match="MONITOR_RAM_ALERT_PERCENT"):
        harness_factory(monitor_module, monitor_ram_alert_percent=150)


@pytest.mark.parametrize(
    ("size", "ru", "en"),
    [(None, "—", "—"), (512, "512 Б", "512 B"), (1536, "1,5 КБ", "1.5 KB"), (5 * GIB, "5 ГБ", "5 GB")],
)
def test_formatting(size: int | None, ru: str, en: str, tmp_path: Any) -> None:
    i18n = module_i18n(tmp_path, ["servers_monitor"])
    assert fmt_bytes(i18n.translator("ru"), size) == ru
    assert fmt_bytes(i18n.translator("en"), size) == en
    assert fmt_uptime(i18n.translator("ru"), 7260) == "2 ч 1 мин"
    assert bar(50) == "▰▰▰▰▰▱▱▱▱▱" and bar(None) == ""
    assert decimal(12.4, "ru") == "12" and decimal(1.25, "ru") == "1,2"


async def test_fetch_status_errors() -> None:
    from tests.test_mod_personal_support import StubRegistry

    registry = StubRegistry({1: FakePanel()})
    with pytest.raises(service.NotSupported):
        await service.fetch_status(registry, 1)
    with pytest.raises(LookupError):
        await service.fetch_status(registry, 2)
    down = FakeCapablePanel(down=True)
    with pytest.raises(PanelError):
        await service.fetch_status(StubRegistry({1: down}), 1)
