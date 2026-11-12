"""Housekeeping module: the daily backup job and the cleanup of expired technical data."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import time, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import SendDocument
from pydantic import SecretStr
from sqlalchemy import select

from app.db.models import AdminSession, Base, KeyValue
from app.db.session import create_engine, create_session_factory
from app.modules.housekeeping import module as housekeeping_module
from app.modules.housekeeping.jobs import JOBS, backup_caption, backup_job, cleanup_job, purge_kv
from app.services import backups, kv, notifications
from app.utils.time import utcnow
from app.web.auth import LOGIN_KEY_PREFIX
from tests.test_mod_personal_support import OWNER, ModCtx, make_ctx


@pytest.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[tuple[ModCtx, Any]]:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'bot.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    context, api = make_ctx(
        session_factory,
        tmp_path,
        [housekeeping_module],
        backup_dir=str(tmp_path / "backups"),
        backup_keep=2,
        backup_enabled=True,
    )
    context.engine = engine
    try:
        yield context, api
    finally:
        await notifications.close(context, drain_seconds=1)  # type: ignore[arg-type]
        await engine.dispose()


async def test_backup_is_created_rotated_and_sent(ctx: tuple[ModCtx, Any], tmp_path: Path) -> None:
    context, api = ctx
    for _ in range(3):
        await backup_job(context)  # type: ignore[arg-type]
    files = backups.list_backups(tmp_path / "backups")
    assert len(files) <= 2 and files
    sent = api.of(SendDocument)
    assert len(sent) == 3 and all(int(m.chat_id) == OWNER for m in sent)
    assert "Резервная копия TestVPN" in sent[0].caption and "не зашифрован" in sent[0].caption


async def test_encrypted_backup_caption_and_no_chat(ctx: tuple[ModCtx, Any], tmp_path: Path) -> None:
    context, api = ctx
    context.current = context.current.model_copy(update={"backup_password": SecretStr("s3cret"), "admin_ids": []})
    await backup_job(context)  # type: ignore[arg-type]
    (path,) = backups.list_backups(tmp_path / "backups")
    assert backups.is_encrypted(path)
    assert not api.of(SendDocument)
    info = backups.BackupInfo(path, 2048, utcnow(), "sqlite", True, {})
    assert "зашифрован паролем" in backup_caption(context, info)  # type: ignore[arg-type]
    assert "2 КБ" in backup_caption(context, info)  # type: ignore[arg-type]


async def test_backup_failure_is_reported(ctx: tuple[ModCtx, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    context, api = ctx

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(backups, "create", broken)
    with pytest.raises(OSError):
        await backup_job(context)  # type: ignore[arg-type]
    await notifications.close(context, drain_seconds=2)  # type: ignore[arg-type]
    assert any("disk full" in t for t in api.texts())


async def test_send_failure_keeps_the_file(ctx: tuple[ModCtx, Any], tmp_path: Path) -> None:
    context, api = ctx
    from aiogram.exceptions import TelegramBadRequest

    api.fail(SendDocument, TelegramBadRequest(method=None, message="chat not found"))  # type: ignore[arg-type]
    await backup_job(context)  # type: ignore[arg-type]
    await notifications.close(context, drain_seconds=2)  # type: ignore[arg-type]
    assert backups.list_backups(tmp_path / "backups")
    assert any("не отправилась" in t for t in api.texts())


async def test_cleanup(ctx: tuple[ModCtx, Any]) -> None:
    context, _api = ctx
    sf = context.session_factory
    now = utcnow()
    async with sf.begin() as session:
        await kv.set(
            session,
            LOGIN_KEY_PREFIX + "old",
            json.dumps({"admin_id": 1, "exp": (now - timedelta(hours=1)).timestamp()}),
        )
        await kv.set(
            session,
            LOGIN_KEY_PREFIX + "new",
            json.dumps({"admin_id": 1, "exp": (now + timedelta(hours=1)).timestamp()}),
        )
        await kv.set(session, "rwhook:old", (now - timedelta(days=8)).isoformat())
        await kv.set(session, "rwhook:new", now.isoformat())
        await kv.set(session, "rwhook:junk", "not a date")
        await kv.set(session, "devices:reset:1", (now - timedelta(days=40)).isoformat())
        await kv.set(session, "devices:reset:2", (now - timedelta(minutes=5)).isoformat())
        await kv.set(session, "unrelated", (now - timedelta(days=400)).isoformat())
        session.add(AdminSession(admin_id=1, token_hash="a" * 64, expires_at=now - timedelta(days=40)))
        session.add(AdminSession(admin_id=1, token_hash="b" * 64, expires_at=now + timedelta(hours=1)))
    await cleanup_job(context)  # type: ignore[arg-type]
    async with sf() as session:
        keys = set(await session.scalars(select(KeyValue.key)))
        sessions = list(await session.scalars(select(AdminSession.token_hash)))
    assert keys == {LOGIN_KEY_PREFIX + "new", "rwhook:new", "devices:reset:2", "unrelated"}
    assert sessions == ["b" * 64]
    assert await purge_kv(sf) == 0


def test_job_specs_follow_settings(ctx: tuple[ModCtx, Any]) -> None:
    context, _api = ctx
    backup, cleanup = JOBS
    settings = context.current.model_copy(update={"backup_time": time(5, 15)})
    assert backup.trigger_kwargs(settings) == {"hour": 5, "minute": 15}
    assert backup.is_enabled(settings)
    assert not backup.is_enabled(settings.model_copy(update={"backup_enabled": False}))
    assert cleanup.trigger == "cron" and cleanup.is_enabled(settings)
