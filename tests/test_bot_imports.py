"""Moving customers over from a Bedolaga backup."""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import BalanceTransaction, Panel, PanelKind, Provision, Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory
from app.services import bot_imports
from app.services.panels.base import RemoteAccount
from tests.conftest import make_settings

FUTURE = datetime.now(UTC) + timedelta(days=20)
PAST = datetime.now(UTC) - timedelta(days=3)
RW_UUID = "11111111-2222-4333-8444-555555555555"
VLESS = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

USERS = [
    # id, telegram_id, username, language, balance_kopeks, referred_by_id, status, remnawave_uuid
    (1, 1001, "alice", "en", 15000, None, "active", RW_UUID),
    (2, 1002, "bob", "ru", 0, 1, "active", None),
    (3, None, None, "ru", 500, None, "active", None),  # e-mail only
    (4, 1004, "gone", "ru", 0, None, "deleted", None),
]
SUBSCRIPTIONS = [
    # id, user_id, status, is_trial, end_date, traffic_limit_gb, device_limit
    (10, 1, "expired", "f", PAST, 0, 2),
    (11, 1, "active", "f", FUTURE, 100, 3),
    (12, 2, "trial", "t", FUTURE, 10, 1),
]
USER_COLS = (
    "id",
    "telegram_id",
    "username",
    "language",
    "balance_kopeks",
    "referred_by_id",
    "status",
    "remnawave_uuid",
)
SUB_COLS = ("id", "user_id", "status", "is_trial", "end_date", "traffic_limit_gb", "device_limit")


def _copy(value: Any) -> str:
    if value is None:
        return "\\N"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return str(value).replace("\\", "\\\\").replace("\t", "\\t")


def sql_dump() -> str:
    lines = ["-- PostgreSQL database dump", "SET statement_timeout = 0;"]
    for table, cols, rows in (("users", USER_COLS, USERS), ("subscriptions", SUB_COLS, SUBSCRIPTIONS)):
        lines.append(f"COPY public.{table} ({', '.join(cols)}) FROM stdin;")
        lines += ["\t".join(_copy(v) for v in row) for row in rows]
        lines.append("\\.")
        lines.append("")
    lines += ["COPY public.payments (id, amount) FROM stdin;", "1\t100", "\\."]
    return "\n".join(lines) + "\n"


def json_dump() -> bytes:
    def rows(cols: tuple[str, ...], data: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
        return [
            {c: (v.isoformat() if isinstance(v, datetime) else v) for c, v in zip(cols, r, strict=True)} for r in data
        ]

    doc = {
        "metadata": {"version": "orm-1.0"},
        "data": {"users": rows(USER_COLS, USERS), "subscriptions": rows(SUB_COLS, SUBSCRIPTIONS)},
    }
    return json.dumps(doc).encode()


def sqlite_file(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE users ({', '.join(USER_COLS)})")
        conn.execute(f"CREATE TABLE subscriptions ({', '.join(SUB_COLS)})")
        conn.executemany("INSERT INTO users VALUES (?,?,?,?,?,?,?,?)", USERS)
        conn.executemany(
            "INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?)",
            [(r[0], r[1], r[2], r[3], r[4].isoformat(), r[5], r[6]) for r in SUBSCRIPTIONS],
        )
    return path


def archive(path: Path, name: str, data: bytes) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for member, body in (("metadata.json", b'{"format_version": "2.0"}'), (name, data)):
            info = tarfile.TarInfo(member)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return path


@pytest.mark.parametrize("kind", ["sql", "json", "sqlite", "tar-sql", "tar-json", "tar-sqlite"])
def test_every_backup_format_is_read(tmp_path: Path, kind: str) -> None:
    if kind == "sql":
        path = tmp_path / "database.sql"
        path.write_text(sql_dump(), encoding="utf-8")
    elif kind == "json":
        path = tmp_path / "database.json"
        path.write_bytes(json_dump())
    elif kind == "sqlite":
        path = sqlite_file(tmp_path / "database.sqlite")
    else:
        inner = kind.split("-", 1)[1]
        body = {
            "sql": sql_dump().encode(),
            "json": json_dump(),
            "sqlite": sqlite_file(tmp_path / "x.sqlite").read_bytes(),
        }[inner]
        path = archive(tmp_path / "backup.tar.gz", f"database.{inner}", body)
    data = bot_imports.read_bedolaga(path)
    assert [str(u["telegram_id"]) for u in data.users] == ["1001", "1002", "None", "1004"]
    assert len(data.subscriptions) == 3


def test_unknown_files_are_refused(tmp_path: Path) -> None:
    for name, body in (("x.txt", b"x"), ("database.json", b"{}"), ("backup.tar.gz", b"not a tar")):
        path = tmp_path / name
        path.write_bytes(body)
        with pytest.raises(bot_imports.BotImportError):
            bot_imports.read_bedolaga(path)


def _data(tmp_path: Path) -> bot_imports.BotData:
    path = tmp_path / "database.sql"
    path.write_text(sql_dump(), encoding="utf-8")
    return bot_imports.read_bedolaga(path)


async def _panel(sf: SessionFactory) -> int:
    async with sf.begin() as s:
        panel = Panel(name="rw", kind=PanelKind.REMNAWAVE, url="https://rw.example.com")
        s.add(panel)
        await s.flush()
        return panel.id


ACCOUNT = RemoteAccount(
    email="user_1001",
    uuid=VLESS,
    enabled=True,
    expires_at=FUTURE,
    device_limit=3,
    traffic_limit_bytes=0,
    remote_id="77",
    short_id="shortA",
    raw={"uuid": RW_UUID},
)


async def test_dry_run_counts_and_changes_nothing(session_factory: SessionFactory, tmp_path: Path) -> None:
    report = await bot_imports.import_bedolaga(session_factory, make_settings(), _data(tmp_path))
    assert report.dry_run and (report.users, report.subscriptions, report.referrals) == (2, 2, 1)
    assert (report.skipped_no_telegram, report.skipped_deleted, report.new_links) == (1, 1, 2)
    async with session_factory() as s:
        assert (await s.scalars(select(User))).first() is None


async def test_customers_keep_their_links_through_their_panel_account(
    session_factory: SessionFactory, tmp_path: Path
) -> None:
    panel_id = await _panel(session_factory)
    report = await bot_imports.import_bedolaga(
        session_factory, make_settings(), _data(tmp_path), panel_id=panel_id, accounts={RW_UUID: ACCOUNT}, apply=True
    )
    assert (report.users, report.links_kept, report.new_links, report.balance_kop) == (2, 1, 1, 15000)
    async with session_factory() as s:
        alice = await s.get(User, 1001)
        bob = await s.get(User, 1002)
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == 1001))
        provision = await s.scalar(select(Provision).where(Provision.subscription_id == sub.id))  # type: ignore[union-attr]
        trial = await s.scalar(select(Subscription).where(Subscription.user_id == 1002))
        ledger = (await s.scalars(select(BalanceTransaction))).all()
    assert alice is not None and alice.balance_kop == 15000 and alice.language == "en"
    assert bob is not None and bob.referred_by == 1001
    assert [t.amount_kop for t in ledger] == [15000]  # the balance went through the ledger
    # the latest subscription, adopting the panel account as is
    assert sub is not None and sub.client_uuid == VLESS and sub.status == SubscriptionStatus.ACTIVE
    assert (sub.device_limit, sub.traffic_limit_gb) == (3, 100)
    assert provision is not None and (provision.remote_email, provision.remote_id, provision.remote_short_id) == (
        "user_1001",
        "77",
        "shortA",
    )
    assert provision.synced_revision == sub.revision  # nothing is pushed to the panel by the import itself
    assert trial is not None and trial.is_trial and trial.client_uuid != VLESS


async def test_a_second_run_imports_nothing_twice(session_factory: SessionFactory, tmp_path: Path) -> None:
    settings = make_settings()
    await bot_imports.import_bedolaga(session_factory, settings, _data(tmp_path), apply=True)
    again = await bot_imports.import_bedolaga(session_factory, settings, _data(tmp_path), apply=True)
    assert (again.users, again.skipped_existing, again.balance_kop) == (0, 2, 0)
    async with session_factory() as s:
        assert len((await s.scalars(select(Subscription))).all()) == 2


async def test_existing_users_are_left_alone(session_factory: SessionFactory, tmp_path: Path) -> None:
    async with session_factory.begin() as s:
        s.add(User(tg_id=1002, referral_code="HEREBEFORE", sub_token="tok-here", balance_kop=7))
    await bot_imports.import_bedolaga(session_factory, make_settings(), _data(tmp_path), apply=True)
    async with session_factory() as s:
        bob = await s.get(User, 1002)
    assert bob is not None and bob.referred_by is None and bob.balance_kop == 7


async def test_only_a_remnawave_panel_can_be_used(session_factory: SessionFactory) -> None:
    class Registry:
        async def get(self, panel_id: int) -> Any:
            class Other:
                kind = PanelKind.XUI

            return Other()

    with pytest.raises(bot_imports.BotImportError):
        await bot_imports.panel_accounts(Registry(), 1)
