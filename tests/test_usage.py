"""Daily traffic snapshots and usage series."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select

from app.config import Settings
from app.db.models import PanelKind, Provision, ProvisionState, UsageSnapshot
from app.db.session import SessionFactory
from app.services import usage
from app.services.panels.base import RemoteAccount
from tests.fakes import FakePanel
from tests.test_subs_support import NOW, FakeRegistry, add_rw_panel, add_sub, add_user, add_xui_panel, days

GIB = 1024**3


def _account(email: str, used: int) -> RemoteAccount:
    return RemoteAccount(email, "uuid", True, None, 0, 0, used_bytes=used)


@dataclass
class BatchPanel(FakePanel):
    """3x-ui-like backend that lists several inbounds in one call."""

    batch_calls: list[list[int]] = field(default_factory=list)

    async def list_accounts_by_target(self, targets: list[int]) -> dict[int, list[RemoteAccount]]:
        self.batch_calls.append(list(targets))
        return {t: [a for (tt, _), a in self.accounts.items() if tt == t] for t in targets}


async def _world(session_factory: SessionFactory) -> tuple[FakeRegistry, dict[str, int]]:
    """User 1: two 3x-ui inbounds + Remnawave; user 2: 3x-ui only; user 3: an ABSENT provision only."""
    async with session_factory.begin() as s:
        xui, (in1, in2) = await add_xui_panel(s, inbounds=2)
        rw = await add_rw_panel(s)
        subs = {}
        for uid in (1, 2, 3):
            await add_user(s, uid)
            subs[uid] = (await add_sub(s, uid, expires_at=NOW + days(10))).id

        def prov(sub_id: int, panel_id: int, inbound_id: int | None, email: str, state: ProvisionState) -> Provision:
            return Provision(
                subscription_id=sub_id,
                panel_id=panel_id,
                inbound_id=inbound_id,
                inbound_key=inbound_id or 0,
                remote_email=email,
                desired_state=state,
                actual_state=state,
            )

        enabled = ProvisionState.ENABLED
        s.add_all(
            [
                prov(subs[1], xui.id, in1.id, "tg1-a", enabled),
                prov(subs[1], xui.id, in2.id, "tg1-b", enabled),
                prov(subs[1], rw.id, None, "tg_1", enabled),
                prov(subs[2], xui.id, in1.id, "tg2-a", enabled),
                prov(subs[3], xui.id, in1.id, "tg3-a", ProvisionState.ABSENT),
            ]
        )
    xui_fake = BatchPanel()
    xui_fake.accounts = {
        (in1.remote_id, "tg1-a"): _account("tg1-a", 1 * GIB),
        (in2.remote_id, "tg1-b"): _account("tg1-b", 2 * GIB),
        (in1.remote_id, "tg2-a"): _account("tg2-a", 5 * GIB),
        (in1.remote_id, "stranger"): _account("stranger", 99 * GIB),
    }
    rw_fake = FakePanel(kind=PanelKind.REMNAWAVE)
    rw_fake.accounts = {(None, "tg_1"): _account("tg_1", 4 * GIB)}
    registry = FakeRegistry({xui.id: xui_fake, rw.id: rw_fake})
    return registry, {"xui": xui.id, "rw": rw.id, **{f"sub{k}": v for k, v in subs.items()}}


async def _snapshots(session_factory: SessionFactory) -> dict[tuple[int, str], int]:
    async with session_factory() as s:
        rows = (await s.scalars(select(UsageSnapshot))).all()
    return {(r.subscription_id, r.day): r.used_bytes for r in rows}


async def test_snapshot_sums_accounts_per_subscription(session_factory: SessionFactory, settings: Settings) -> None:
    registry, ids = await _world(session_factory)
    report = await usage.snapshot(session_factory, registry, settings, now=NOW)
    day = "2026-11-04"
    assert (report.day, report.panels_ok, report.panels_failed, report.stored, report.skipped) == (day, 2, 0, 2, 0)
    assert await _snapshots(session_factory) == {(ids["sub1"], day): 7 * GIB, (ids["sub2"], day): 5 * GIB}
    xui_fake = registry.panels[ids["xui"]]
    assert isinstance(xui_fake, BatchPanel) and len(xui_fake.batch_calls) == 1

    xui_fake.accounts[(1, "tg2-a")].used_bytes = 6 * GIB  # same day again: overwritten
    await usage.snapshot(session_factory, registry, settings, now=NOW + timedelta(hours=1))
    assert (await _snapshots(session_factory))[(ids["sub2"], day)] == 6 * GIB


async def test_partial_reads_are_skipped(session_factory: SessionFactory, settings: Settings) -> None:
    registry, ids = await _world(session_factory)
    registry.panels[ids["rw"]].down = True
    report = await usage.snapshot(session_factory, registry, settings, now=NOW)
    assert (report.panels_ok, report.panels_failed, report.stored, report.skipped) == (1, 1, 1, 1)
    assert report.errors and "panel" in report.errors[0]
    assert set(await _snapshots(session_factory)) == {(ids["sub2"], "2026-11-04")}


async def test_down_panels_are_not_called(session_factory: SessionFactory, settings: Settings) -> None:
    registry, ids = await _world(session_factory)
    async with session_factory.begin() as s:
        from app.db.models import Panel

        panel = await s.get(Panel, ids["rw"])
        assert panel is not None
        panel.consecutive_failures = 100
    report = await usage.snapshot(session_factory, registry, settings, now=NOW)
    assert report.panels_ok == 1 and report.panels_failed == 0 and report.skipped == 1


async def test_daily_usage_and_totals(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await add_user(s, 1)
        await add_user(s, 2)
        a = (await add_sub(s, 1, expires_at=NOW + days(10))).id
        b = (await add_sub(s, 2, expires_at=NOW + days(10))).id
        series = {
            a: {"2026-10-30": 100, "2026-10-31": 150, "2026-11-01": 400, "2026-11-02": 50, "2026-11-04": 90},
            b: {"2026-11-03": 10, "2026-11-04": 30},
        }
        for sub_id, values in series.items():
            s.add_all(UsageSnapshot(subscription_id=sub_id, day=d, used_bytes=v) for d, v in values.items())
    async with session_factory() as s:
        per_day = await usage.daily_usage(s, a, settings, days=4, now=NOW)
        totals = await usage.total_daily_usage(s, settings, days=3, now=NOW)
    # baseline 10-31 (150); 11-01 +250; 11-02 counter reset -> 50; 11-03 no data; 11-04 after a gap +40
    assert per_day == [("2026-11-01", 250), ("2026-11-02", 50), ("2026-11-03", 0), ("2026-11-04", 40)]
    assert totals == [("2026-11-02", 50), ("2026-11-03", 0), ("2026-11-04", 60)]
    async with session_factory.begin() as s:
        removed = await usage.prune(s, settings, keep_days=3, now=NOW)
    assert removed == 2  # 10-30 and 10-31
    assert min(day for _, day in await _snapshots(session_factory)) == "2026-11-01"
