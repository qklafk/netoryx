"""Backups: logical export, SQLite snapshot, encryption, restore, retention and Telegram delivery."""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import (
    Base,
    KeyValue,
    LedgerAccount,
    Payment,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    TxKind,
    User,
)
from app.db.session import SessionFactory, create_engine, create_session_factory
from app.services import backups, wallet
from app.services.backups import BackupError, RestoreNotEmpty, WrongPassword
from tests.conftest import make_settings
from tests.test_subs_support import NOW, add_plan, add_sub, add_user, days


def _engine(session_factory: SessionFactory) -> AsyncEngine:
    return cast(AsyncEngine, session_factory.kw["bind"])


@pytest.fixture
async def target(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def _populate(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as s:
        plan = await add_plan(s)
        await add_user(s, 1, referred_by=None, first_name="Алиса")
        await add_user(s, 2)
        await add_user(s, 3, is_banned=True)
        (await s.get(User, 1)).referred_by = 3  # type: ignore[union-attr]  # inviter has a larger id
        await add_sub(s, 1, expires_at=NOW + days(30), plan=plan, is_trial=True)
        await add_sub(s, 2, expires_at=NOW - days(1), status=SubscriptionStatus.EXPIRED)
        await wallet.apply(s, 2, 12_345, TxKind.ADMIN_ADJUST, "seed")
        await wallet.apply(s, 3, 500, TxKind.REFERRAL_COMMISSION, "L1:x", account=LedgerAccount.REFERRAL)
        s.add(
            Payment(
                id="pay-1",
                provider="stars",
                user_id=1,
                amount_kop=15_000,
                status=PaymentStatus.SUCCEEDED,
                credited_at=NOW,
            )
        )
        s.add(KeyValue(key="secret_key", value="s3cret"))


async def _dump(session_factory: SessionFactory) -> dict[str, list[tuple[Any, ...]]]:
    result: dict[str, list[tuple[Any, ...]]] = {}
    async with session_factory() as s:
        for table in Base.metadata.sorted_tables:
            pk = next(iter(table.primary_key.columns))
            rows = (await s.execute(select(table).order_by(pk))).all()
            result[table.name] = [tuple(row) for row in rows]
    return result


# ---------------------------------------------------------------------------------------------
# encryption
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("size", [0, 10, 2500])
def test_encryption_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    monkeypatch.setattr(backups, "_ENC_CHUNK", 1000)
    monkeypatch.setattr(backups, "_SCRYPT_N", 2**10)
    src, enc, out = tmp_path / "a", tmp_path / "a.enc", tmp_path / "a.out"
    src.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
    backups.encrypt_file(src, enc, "pw")
    assert backups.is_encrypted(enc) and not backups.is_encrypted(src)
    backups.decrypt_file(enc, out, "pw")
    assert out.read_bytes() == src.read_bytes()
    with pytest.raises(WrongPassword):
        backups.decrypt_file(enc, out, "other")
    with pytest.raises(ValueError):
        backups.encrypt_file(src, enc, "")


def test_encryption_detects_truncation_and_garbage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backups, "_ENC_CHUNK", 100)
    monkeypatch.setattr(backups, "_SCRYPT_N", 2**10)
    src, enc, out = tmp_path / "a", tmp_path / "a.enc", tmp_path / "out"
    src.write_bytes(b"y" * 350)
    backups.encrypt_file(src, enc, "pw")
    data = enc.read_bytes()
    chunk_record = 4 + 100 + 16
    (tmp_path / "cut").write_bytes(data[: 33 + chunk_record * 2])  # whole chunks, but not the last one
    with pytest.raises(BackupError, match="truncated"):
        backups.decrypt_file(tmp_path / "cut", out, "pw")
    (tmp_path / "extra").write_bytes(data + b"z")
    with pytest.raises(BackupError, match="unexpected data"):
        backups.decrypt_file(tmp_path / "extra", out, "pw")
    (tmp_path / "plain").write_bytes(b"not encrypted at all, just text....")
    with pytest.raises(BackupError, match="not an encrypted"):
        backups.decrypt_file(tmp_path / "plain", out, "pw")


# ---------------------------------------------------------------------------------------------
# export / restore
# ---------------------------------------------------------------------------------------------
async def test_logical_backup_restores_identically(
    session_factory: SessionFactory, target: SessionFactory, tmp_path: Path
) -> None:
    await _populate(session_factory)
    info = await backups.create(_engine(session_factory), tmp_path / "bk", prefer_sqlite_snapshot=False, now=NOW)
    assert info.kind == "logical" and not info.encrypted
    assert info.path.name == "backup-20261104-120000.jsonl.gz"
    assert info.rows["users"] == 3 and info.rows["subscriptions"] == 2
    with gzip.open(info.path, "rt", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert json.loads(lines[0])["format"] == backups.FORMAT
    assert json.loads(lines[-1]) == {"end": True, "rows": info.rows}

    report = await backups.restore(_engine(target), info.path)
    assert report.kind == "logical" and report.rows["users"] == 3 and not report.wiped
    assert await _dump(target) == await _dump(session_factory)
    async with target() as s:
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == 1))
        assert sub is not None and sub.expires_at == NOW + days(30) and sub.is_trial
        user = await s.get(User, 1)
        assert user is not None and user.referred_by == 3 and user.first_name == "Алиса"


async def test_restore_refuses_non_empty_database_without_wipe(
    session_factory: SessionFactory, target: SessionFactory, tmp_path: Path
) -> None:
    await _populate(session_factory)
    info = await backups.create(_engine(session_factory), tmp_path, prefer_sqlite_snapshot=False, now=NOW)
    async with target.begin() as s:
        await add_user(s, 99)
    with pytest.raises(RestoreNotEmpty):
        await backups.restore(_engine(target), info.path)
    report = await backups.restore(_engine(target), info.path, wipe=True)
    assert report.wiped
    assert await _dump(target) == await _dump(session_factory)


async def test_sqlite_snapshot_encrypted(
    session_factory: SessionFactory, target: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backups, "_SCRYPT_N", 2**10)
    await _populate(session_factory)
    info = await backups.create(_engine(session_factory), tmp_path / "bk", password="pw", now=NOW)
    assert info.kind == "sqlite" and info.encrypted
    assert info.path.name.endswith(".sqlite.gz.enc")
    assert not list((tmp_path / "bk").glob("*.part"))
    with pytest.raises(WrongPassword):
        await backups.restore(_engine(target), info.path)
    with pytest.raises(WrongPassword):
        await backups.restore(_engine(target), info.path, password="wrong")
    report = await backups.restore(_engine(target), info.path, password="pw")
    assert report.kind == "sqlite"
    assert await _dump(target) == await _dump(session_factory)


async def test_unknown_tables_are_skipped_and_truncated_files_rejected(target: SessionFactory, tmp_path: Path) -> None:
    good = tmp_path / "good.jsonl.gz"
    lines = [
        {"format": backups.FORMAT, "version": 1},
        {"table": "legacy_stuff", "columns": ["id"]},
        [1],
        {"table": "kv", "columns": ["key", "value", "obsolete"]},
        ["a", "b", "ignored"],
        {"end": True, "rows": {}},
    ]
    with gzip.open(good, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    report = await backups.restore(_engine(target), good)
    assert report.skipped_tables == ["legacy_stuff"] and report.rows == {"kv": 1}

    bad = tmp_path / "bad.jsonl.gz"
    with gzip.open(bad, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines[:-1]) + "\n")
    with pytest.raises(BackupError, match="truncated"):
        await backups.restore(_engine(target), bad, wipe=True)
    async with target() as s:  # the failed restore rolled back: the earlier data is intact
        assert (await s.get(KeyValue, "a")) is not None

    junk = tmp_path / "junk.gz"
    junk.write_bytes(b"definitely not gzip")
    with pytest.raises(BackupError):
        await backups.restore(_engine(target), junk)


# ---------------------------------------------------------------------------------------------
# retention & delivery
# ---------------------------------------------------------------------------------------------
def test_prune_keeps_newest(tmp_path: Path) -> None:
    names = [f"backup-2026110{d}-040000{backups.LOGICAL_SUFFIX}" for d in range(1, 6)]
    for name in [*names, "notes.txt", "backup-x.tmp"]:
        (tmp_path / name).write_bytes(b"x")
    removed = backups.prune(tmp_path, 2)
    assert sorted(p.name for p in removed) == names[:3]
    assert [p.name for p in backups.list_backups(tmp_path)] == names[:2:-1]
    assert (tmp_path / "notes.txt").exists()
    assert backups.list_backups(tmp_path / "missing") == []


def test_split_and_join(tmp_path: Path) -> None:
    src = tmp_path / "big.bin"
    src.write_bytes(bytes(range(256)) * 10)
    parts = backups.split_file(src, part_size=1000)
    assert [p.name for p in parts] == ["big.bin.001", "big.bin.002", "big.bin.003"]
    backups.join_parts(list(reversed(parts)), tmp_path / "joined.bin")
    assert (tmp_path / "joined.bin").read_bytes() == src.read_bytes()


def _parts_left(folder: Path) -> list[Path]:
    return list(folder.glob("*.0*"))


@dataclass
class FakeBot:
    documents: list[tuple[int, str, str | None]] = field(default_factory=list)

    async def send_document(self, chat_id: int, document: Any, caption: str | None = None, **kw: Any) -> None:
        self.documents.append((chat_id, Path(document.path).name, caption))


async def test_send_to_telegram_splits_large_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "backup-1.jsonl.gz"
    path.write_bytes(b"x" * 2500)
    bot = FakeBot()
    assert await backups.send_to_telegram(cast(Bot, bot), 5, path, caption="c") == 1
    monkeypatch.setattr(backups, "TELEGRAM_FILE_LIMIT", 1000)
    monkeypatch.setattr(backups, "PART_SIZE", 1000)
    bot.documents.clear()
    assert await backups.send_to_telegram(cast(Bot, bot), 5, path, caption="c") == 3
    assert [caption for _, _, caption in bot.documents] == ["c (1/3)", "c (2/3)", "c (3/3)"]
    assert _parts_left(tmp_path) == []


async def test_run_scheduled(session_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await _populate(session_factory)
    settings = make_settings(admin_ids=[42])
    folder = tmp_path / "auto"
    monkeypatch.setattr(backups, "directory_from", lambda _settings: folder)
    bot = FakeBot()
    info = await backups.run_scheduled(_engine(session_factory), settings, cast(Bot, bot))
    assert info.path.parent == folder and info.kind == "sqlite"
    assert bot.documents and bot.documents[0][0] == 42
    assert backups.keep_from(settings) == backups.DEFAULT_KEEP and backups.password_from(settings) is None


def test_settings_helpers() -> None:
    custom = make_settings(backup_dir="x/y", backup_keep=3, backup_password="pw")
    assert backups.directory_from(custom) == Path("x/y")
    assert backups.keep_from(custom) == 3 and backups.password_from(custom) == "pw"
    assert backups.password_from(make_settings(backup_password="")) is None
    assert backups.directory_from(make_settings()) == Path(backups.DEFAULT_DIRECTORY)
