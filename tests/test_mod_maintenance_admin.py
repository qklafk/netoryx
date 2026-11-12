"""Maintenance controls in the bot and the date/time parser used by admin inputs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db.models import AdminRole
from app.db.session import SessionFactory
from app.modules.admin_maintenance.admin_bot import MntCb
from app.modules.admin_maintenance.timeinput import parse_moment, parse_range, parse_relative
from app.utils.time import utcnow
from tests.test_mod_admin_tools_support import (
    Harness,
    make_harness,
    module,
    release_module_routers,  # noqa: F401 - autouse fixture
)

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)  # 15:00 in Moscow


async def _harness(session_factory: SessionFactory) -> Harness:
    return await make_harness(session_factory, [module("admin_maintenance"), module("broadcasts")])


def _home() -> str:
    return MntCb(a="home").pack()


async def test_turn_on_with_eta_and_off(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(_home())
    assert "Бот работает в обычном режиме" in h.last()
    await h.click(h.button("Включить техработы"))
    await h.click(h.button("1 час"))
    state = await h.ctx.maintenance.state(fresh=True)
    assert state.enabled and state.eta is not None
    assert timedelta(minutes=59) < state.eta - utcnow() <= timedelta(hours=1)
    assert "Бот закрыт на техработы" in h.last()
    await h.click(h.button("Выключить техработы"))
    assert not (await h.ctx.maintenance.state(fresh=True)).enabled


async def test_custom_eta_and_messages(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(MntCb(a="etain").pack())
    await h.say("вчера")
    assert "Не понял время" in h.last()
    await h.say("+90m")
    state = await h.ctx.maintenance.state(fresh=True)
    assert state.enabled and state.eta is not None
    await h.click(MntCb(a="msg", v="en").pack())
    await h.say("Upgrading servers")
    await h.click(MntCb(a="msg", v="ru").pack())
    await h.say("Обновляем серверы")
    state = await h.ctx.maintenance.state(fresh=True)
    assert state.message_en == "Upgrading servers" and state.message_ru == "Обновляем серверы"
    assert state.eta is not None  # editing the text keeps the ETA
    await h.click(MntCb(a="msgreset").pack())
    state = await h.ctx.maintenance.state(fresh=True)
    assert state.message_ru is None and state.message_en is None


async def test_testers_and_partial_maintenance(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(MntCb(a="allow").pack())
    await h.say("111, 222 abc 333")
    assert (await h.ctx.maintenance.state(fresh=True)).allowlist == [111, 222, 333]
    await h.click(MntCb(a="mods").pack())
    await h.click(h.button("Рассылки"))
    state = await h.ctx.maintenance.state(fresh=True)
    assert state.disabled_modules == ["broadcasts"]
    assert any(text.startswith("⛔️") for text, _ in h.buttons())
    await h.click(h.button("Рассылки"))
    assert (await h.ctx.maintenance.state(fresh=True)).disabled_modules == []
    await h.click(MntCb(a="allowclr").pack())
    assert (await h.ctx.maintenance.state(fresh=True)).allowlist == []


async def test_plan_and_cancel_window(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(MntCb(a="win").pack())
    await h.say("ерунда")
    assert "Не понял интервал" in h.last()
    tomorrow = (utcnow().astimezone(MSK) + timedelta(days=1)).strftime("%d.%m.%Y")
    await h.say(f"{tomorrow} 03:00 - 05:00")
    state = await h.ctx.maintenance.state(fresh=True)
    assert len(state.windows) == 1
    window = state.windows[0]
    assert window.ends_at - window.starts_at == timedelta(hours=2)
    await h.click(h.button("Отменить работы"))
    assert (await h.ctx.maintenance.state(fresh=True)).windows == []


async def test_needs_settings_permission(session_factory: SessionFactory) -> None:
    h = await _harness(session_factory)
    await h.click(_home(), role=AdminRole.SUPPORT)
    assert h.screens() == []


def test_parse_moment_forms() -> None:
    assert parse_moment("18:30", MSK, NOW) == datetime(2026, 11, 4, 15, 30, tzinfo=UTC)
    assert parse_moment("14:00", MSK, NOW) == datetime(2026, 11, 5, 11, 0, tzinfo=UTC)  # passed today -> tomorrow
    assert parse_moment("25.12 10:00", MSK, NOW) == datetime(2026, 12, 25, 7, 0, tzinfo=UTC)
    assert parse_moment("01.01 10:00", MSK, NOW) == datetime(2027, 1, 1, 7, 0, tzinfo=UTC)
    assert parse_moment("01.01.27 10:00", MSK, NOW) == datetime(2027, 1, 1, 7, 0, tzinfo=UTC)
    assert parse_moment("+2h", MSK, NOW) == NOW + timedelta(hours=2)
    assert parse_moment("+30м", MSK, NOW) == NOW + timedelta(minutes=30)
    for bad in ("", "завтра", "31.02 10:00", "25:00", "+0h"):
        assert parse_moment(bad, MSK, NOW) is None
    assert parse_relative("+400d") is None


def test_parse_range_forms() -> None:
    start, end = parse_range("05.11 03:00 - 05:00", MSK, NOW) or (None, None)
    assert start == datetime(2026, 11, 5, 0, 0, tzinfo=UTC) and end == datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    start, end = parse_range("05.11 23:00 — 01:00", MSK, NOW) or (None, None)
    assert end is not None and start is not None and end - start == timedelta(hours=2)
    start, end = parse_range("16:00 - +90m", MSK, NOW) or (None, None)
    assert end is not None and start is not None and end - start == timedelta(minutes=90)
    assert parse_range("05.11 03:00 - 04.11 03:00", MSK, NOW) is None
    assert parse_range("no range", MSK, NOW) is None
