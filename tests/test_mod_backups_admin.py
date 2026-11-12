"""Backups in the bot: create and send, resend an older file, restore an uploaded file (owner only)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.methods import SendDocument
from sqlalchemy import select

from app.db.models import AdminRole, Location
from app.db.session import SessionFactory
from app.modules.admin_backups import admin_bot
from app.modules.admin_backups.admin_bot import BkCb
from app.services import backups
from tests.test_mod_admin_tools_support import (
    OWNER,
    Harness,
    make_harness,
    media_message,
    module,
    release_module_routers,  # noqa: F401 - autouse fixture
)


async def _harness(session_factory: SessionFactory, tmp_path: Path, **settings: object) -> Harness:
    folder = tmp_path / "backups"
    folder.mkdir(exist_ok=True)
    return await make_harness(session_factory, [module("admin_backups")], backup_dir=str(folder), **settings)


async def test_create_sends_file_and_lists_it(session_factory: SessionFactory, tmp_path: Path) -> None:
    h = await _harness(session_factory, tmp_path)
    await h.click(BkCb(a="home").pack())
    assert "пока нет" in h.last()
    await h.click(h.button("Создать копию сейчас"))
    docs = h.api.of(SendDocument)
    assert docs and int(docs[-1].chat_id) == OWNER
    assert "Копия готова" in h.last()
    files = backups.list_backups(tmp_path / "backups")
    assert len(files) == 1
    await h.click(BkCb(a="send", v=files[0].name).pack())
    assert len(h.api.of(SendDocument)) == 2
    await h.click(BkCb(a="send", v="../../etc/passwd").pack())
    assert "не найден" in (h.popups()[-1] or "")


async def test_admin_cannot_restore(session_factory: SessionFactory, tmp_path: Path) -> None:
    h = await _harness(session_factory, tmp_path)
    await h.click(BkCb(a="home").pack(), role=AdminRole.ADMIN)
    assert all("Восстановить" not in text for text, _ in h.buttons())
    await h.click(BkCb(a="restore").pack(), role=AdminRole.ADMIN)
    assert "только владелец" in (h.popups()[-1] or "")


async def test_restore_uploaded_backup(
    session_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await _harness(session_factory, tmp_path, backup_password="pw-one")
    async with session_factory.begin() as session:
        session.add(Location(name="До копии", flag=""))
    info = await backups.create(h.ctx.engine, tmp_path / "source", password="pw-two")
    async with session_factory.begin() as session:
        session.add(Location(name="После копии", flag=""))

    async def fake_download(bot: Bot, file_id: str, destination: Path) -> None:
        shutil.copyfile(info.path, destination)

    monkeypatch.setattr(admin_bot, "download_document", fake_download)
    await h.click(BkCb(a="restore").pack())
    assert "Восстановление из копии" in h.last()
    await h.say("not a file")
    assert "документом" in h.sent_to(OWNER)[-1]
    await h.feed(media_message(OWNER, document="DOC", file_name=info.path.name, file_size=info.size))
    assert "Восстановить данные из копии" in h.last()
    await h.click(h.button("Да, заменить все данные"))
    assert "зашифрована другим паролем" in h.last()
    await h.say("pw-two")
    assert "Данные восстановлены" in h.last()
    async with session_factory() as session:
        names = sorted((await session.scalars(select(Location.name))).all())
    assert names == ["До копии"]
    safety = [p for p in backups.list_backups(tmp_path / "backups")]
    assert safety, "a safety backup is taken before restoring"


async def test_backups_need_permission(session_factory: SessionFactory, tmp_path: Path) -> None:
    h = await _harness(session_factory, tmp_path)
    await h.click(BkCb(a="home").pack(), role=AdminRole.SUPPORT)
    assert h.screens() == []


async def test_admin_gets_no_backup_files(session_factory: SessionFactory, tmp_path: Path) -> None:
    """Downloading a backup (a full database dump) is owner-only, like in the web admin."""
    h = await _harness(session_factory, tmp_path)
    await h.click(BkCb(a="home").pack(), role=AdminRole.ADMIN)
    await h.click(h.button("Создать копию сейчас"), role=AdminRole.ADMIN)
    assert h.api.of(SendDocument) == []
    files = backups.list_backups(tmp_path / "backups")
    assert len(files) == 1, "the backup is still created on disk"
    assert "сохранена на сервере" in h.last()
    assert all(not (data or "").startswith(BkCb(a="send").pack()[:6]) for _, data in h.buttons())
    await h.click(BkCb(a="send", v=files[0].name).pack(), role=AdminRole.ADMIN)
    assert h.api.of(SendDocument) == []
    assert "только владелец" in (h.popups()[-1] or "")


async def test_abandoned_restore_upload_is_swept(
    session_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uploaded dump left behind by another flow clearing the state does not stay on disk."""
    import os
    import time

    folder = tmp_path / "backups"
    folder.mkdir()
    h = await make_harness(session_factory, [module("admin"), module("admin_backups")], backup_dir=str(folder))
    info = await backups.create(h.ctx.engine, tmp_path / "source")
    uploaded: list[Path] = []

    async def fake_download(bot: Bot, file_id: str, destination: Path) -> None:
        uploaded.append(destination)
        shutil.copyfile(info.path, destination)

    monkeypatch.setattr(admin_bot, "download_document", fake_download)
    await h.click(BkCb(a="restore").pack())
    await h.feed(media_message(OWNER, document="DOC", file_name=info.path.name, file_size=info.size))
    assert "Восстановить данные из копии" in h.last()
    upload_dir = uploaded[0].parent
    await h.say("/admin")  # another flow clears the state, the confirmation is abandoned
    assert upload_dir.exists()

    sweep = next(job for job in module("admin_backups").jobs if job.id == "backups.sweep_uploads")
    await sweep.func(h.ctx)  # type: ignore[arg-type]
    assert upload_dir.exists(), "a fresh upload is kept while the owner may still confirm it"
    old = time.time() - admin_bot.UPLOAD_TTL_SEC - 60
    os.utime(upload_dir, (old, old))
    await sweep.func(h.ctx)  # type: ignore[arg-type]
    assert not upload_dir.exists()
    assert backups.list_backups(folder) == [], "uploads are not listed as backups"


async def test_new_upload_sweeps_stale_ones(
    session_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import time

    h = await _harness(session_factory, tmp_path)
    info = await backups.create(h.ctx.engine, tmp_path / "source")
    uploaded: list[Path] = []

    async def fake_download(bot: Bot, file_id: str, destination: Path) -> None:
        uploaded.append(destination)
        shutil.copyfile(info.path, destination)

    monkeypatch.setattr(admin_bot, "download_document", fake_download)
    await h.click(BkCb(a="restore").pack())
    await h.feed(media_message(OWNER, document="DOC", file_name=info.path.name, file_size=info.size))
    first = uploaded[0].parent
    old = time.time() - admin_bot.UPLOAD_TTL_SEC - 60
    os.utime(first, (old, old))
    await h.dp.fsm.get_context(h.ctx.bot, chat_id=OWNER, user_id=OWNER).clear()  # abandoned by another flow
    await h.click(BkCb(a="restore").pack())
    assert first.exists()
    await h.feed(media_message(OWNER, document="DOC", file_name=info.path.name, file_size=info.size))
    assert not first.exists(), "a new upload removes stale ones"
    second = uploaded[1].parent
    assert second.exists() and second.parent == first.parent
    await h.click(h.button("Да, заменить все данные"))
    assert "Данные восстановлены" in h.last()
    assert not second.exists()
