"""Backups from the bot: list, create now, send an older one, restore an uploaded file (with a safety copy taken
first). A backup is a full database dump, so only the owner gets the files and can restore."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, F
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission, has_permission
from app.core.ui import Screen, answer_callback, delete_message, render
from app.db.models import AdminRole
from app.keyboards import Button, back, kb
from app.modules.admin.common import admin_input_group, admin_router
from app.services import audit, backups
from app.utils.bytes import fmt_bytes

log = logging.getLogger(__name__)

router = admin_router("backups_admin")
router.message.filter(HasPermission(Permission.BACKUPS))
router.callback_query.filter(HasPermission(Permission.BACKUPS))

LIST_LIMIT = 5
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # what a self-hosted Bot API server can hand over
# Uploaded restore files wait for the owner's confirmation in a folder next to the backups. An upload abandoned by
# leaving the confirmation through another screen is removed by a periodic sweep once it is this old.
UPLOADS_DIRNAME = ".restore-uploads"
UPLOAD_TTL_SEC = 3600.0
SWEEP_INTERVAL_MIN = 15


class BkCb(CallbackData, prefix="bkp"):
    a: str
    v: str = ""


@admin_input_group
class BackupStates(StatesGroup):
    upload = State()
    password = State()


def _cb(action: str, value: str = "") -> BkCb:
    return BkCb(a=action, v=value)


async def download_document(bot: Bot, file_id: str, destination: Path) -> None:
    """Download an uploaded file (separate function so tests can replace the network part)."""
    await bot.download(file_id, destination=destination)


def _file_line(_: Translator, path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return f"• {_.esc(path.name)}"
    created = datetime.fromtimestamp(stat.st_mtime, UTC)
    lock = " 🔒" if path.name.endswith(backups.ENCRYPTED_SUFFIX) else ""
    return f"• {_.datetime(created)} · {fmt_bytes(stat.st_size)}{lock}"


async def home_screen(
    ctx: AppContext, _: Translator, note: str | None = None, role: AdminRole | None = AdminRole.OWNER
) -> Screen:
    settings = ctx.settings
    folder = backups.directory_from(settings)
    files = await asyncio.to_thread(backups.list_backups, folder)
    schedule = (
        _("admin_backups.schedule_on", time=settings.backup_time.strftime("%H:%M"), tz=_.esc(settings.tz))
        if settings.backup_enabled
        else _("admin_backups.schedule_off")
    )
    encrypted = _("admin_backups.encrypted_yes") if backups.password_from(settings) else _("admin_backups.encrypted_no")
    listing = "\n".join(_file_line(_, path) for path in files[:LIST_LIMIT]) or _("admin_backups.none")
    text = _(
        "admin_backups.home",
        schedule=schedule,
        encrypted=encrypted,
        keep=settings.backup_keep,
        count=len(files),
        files=listing,
    )
    if note:
        text = note + "\n\n" + text
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_backups.btn.create"), _cb("create"), style="primary")],
    ]
    owner = has_permission(role, Permission.BACKUP_RESTORE)
    if owner:
        for path in files[:LIST_LIMIT]:
            label = _("admin_backups.btn.send", name=path.name.removeprefix(backups.NAME_PREFIX)[:40])
            rows.append([Button.cb(label, _cb("send", path.name))])
        rows.append([Button.cb(_("admin_backups.btn.restore"), _cb("restore"))])
    rows.append([back("admin", _)])
    return Screen(text, kb(rows))


def _find(ctx: AppContext, name: str) -> Path | None:
    """A backup file of the backup folder by name (never a path given by the client)."""
    for path in backups.list_backups(backups.directory_from(ctx.settings)):
        if path.name == name:
            return path
    return None


async def _log(ctx: AppContext, actor_id: int, action: str, **details: Any) -> None:
    async with ctx.session_factory.begin() as session:
        await audit.log(session, actor_id, action, "backup", None, **details)


# --- list / create / send ---------------------------------------------------------------------------------------------
@router.callback_query(BkCb.filter(F.a == "home"))
async def open_home(
    callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext, admin_role: AdminRole | None = None
) -> None:
    await _drop_upload(state)
    await render(callback, await home_screen(ctx, _, role=admin_role))


@router.callback_query(BkCb.filter(F.a == "create"))
async def create(callback: CallbackQuery, ctx: AppContext, _: Translator, admin_role: AdminRole | None = None) -> None:
    await answer_callback(callback, _("admin_backups.creating"))
    settings = ctx.settings
    folder = backups.directory_from(settings)
    try:
        info = await backups.create(ctx.engine, folder, password=backups.password_from(settings))
        await asyncio.to_thread(backups.prune, folder, backups.keep_from(settings))
    except Exception as exc:
        log.exception("backup from the bot failed")
        await render(callback, await home_screen(ctx, _, _("admin_backups.failed", error=_.esc(exc)), admin_role))
        return
    await _log(ctx, callback.from_user.id, "backup.create", file=info.path.name, size=info.size)
    if not has_permission(admin_role, Permission.BACKUP_RESTORE):
        note = _("admin_backups.created_on_server", size=fmt_bytes(info.size))
        await render(callback, await home_screen(ctx, _, note, admin_role))
        return
    caption = _("admin_backups.caption", brand=_.esc(settings.brand_name), size=fmt_bytes(info.size))
    try:
        await backups.send_to_telegram(ctx.bot, callback.from_user.id, info.path, caption=caption)
        note = _("admin_backups.created", size=fmt_bytes(info.size))
    except Exception as exc:
        log.warning("could not send the backup to %s: %s", callback.from_user.id, exc)
        note = _("admin_backups.created_not_sent", size=fmt_bytes(info.size), error=_.esc(exc))
    await render(callback, await home_screen(ctx, _, note, admin_role))


@router.callback_query(BkCb.filter(F.a == "send"), HasPermission(Permission.BACKUP_RESTORE))
async def send(
    callback: CallbackQuery, callback_data: BkCb, ctx: AppContext, _: Translator, admin_role: AdminRole | None = None
) -> None:
    path = await asyncio.to_thread(_find, ctx, callback_data.v)
    if path is None:
        await answer_callback(callback, _("admin_backups.not_found"), alert=True)
        return
    await answer_callback(callback, _("admin_backups.sending"))
    try:
        await backups.send_to_telegram(ctx.bot, callback.from_user.id, path, caption=path.name)
    except Exception as exc:
        log.warning("could not send backup %s: %s", path.name, exc)
        await render(callback, await home_screen(ctx, _, _("admin_backups.send_failed", error=_.esc(exc)), admin_role))
        return
    await _log(ctx, callback.from_user.id, "backup.download", file=path.name)


@router.callback_query(BkCb.filter(F.a == "send"))
async def send_denied(callback: CallbackQuery, _: Translator) -> None:
    await answer_callback(callback, _("admin_backups.download_owner_only"), alert=True)


# --- restore (owner) --------------------------------------------------------------------------------------------------
def uploads_root(ctx: AppContext) -> Path:
    return backups.directory_from(ctx.settings) / UPLOADS_DIRNAME


def sweep_uploads(root: Path, max_age_sec: float = UPLOAD_TTL_SEC, now: float | None = None) -> int:
    """Delete uploads older than ``max_age_sec`` (blocking). Returns how many were removed."""
    if not root.is_dir():
        return 0
    deadline = (time.time() if now is None else now) - max_age_sec
    removed = 0
    for entry in root.iterdir():
        try:
            if entry.stat().st_mtime >= deadline:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as exc:
            log.warning("could not remove the old upload %s: %s", entry.name, exc)
    return removed


async def sweep_uploads_job(ctx: AppContext) -> None:
    removed = await asyncio.to_thread(sweep_uploads, uploads_root(ctx))
    if removed:
        log.info("removed %d abandoned restore uploads", removed)


def _new_upload_folder(root: Path) -> Path:
    sweep_uploads(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="upload-", dir=root))


async def _drop_upload(state: FSMContext) -> None:
    """Forget an uploaded file (and delete it from disk)."""
    folder = (await state.get_data()).get("tmp")
    await state.clear()
    if folder:
        await asyncio.to_thread(shutil.rmtree, str(folder), True)


@router.callback_query(BkCb.filter(F.a == "restore"), HasPermission(Permission.BACKUP_RESTORE))
async def ask_upload(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await _drop_upload(state)
    await state.set_state(BackupStates.upload)
    await render(callback, Screen(_("admin_backups.ask_file"), kb([[Button.cb(_("nav.cancel"), _cb("home"))]])))


@router.callback_query(BkCb.filter(F.a == "restore"))
async def restore_denied(callback: CallbackQuery, _: Translator) -> None:
    await answer_callback(callback, _("admin_backups.owner_only"), alert=True)


@router.message(BackupStates.upload, F.document, HasPermission(Permission.BACKUP_RESTORE))
async def got_file(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    document = message.document
    assert document is not None
    name = Path(document.file_name or "backup").name
    if not name.startswith(backups.NAME_PREFIX) and not name.endswith((".gz", backups.ENCRYPTED_SUFFIX)):
        await render(message, Screen(_("admin_backups.bad_file")))
        return
    if document.file_size and document.file_size > MAX_UPLOAD_BYTES:
        await render(message, Screen(_("admin_backups.too_big")))
        return
    folder = await asyncio.to_thread(_new_upload_folder, uploads_root(ctx))
    path = folder / name
    try:
        await download_document(ctx.bot, document.file_id, path)
    except Exception as exc:
        await asyncio.to_thread(shutil.rmtree, str(folder), True)
        log.warning("backup upload download failed: %s", exc)
        await render(message, Screen(_("admin_backups.download_failed", error=_.esc(exc))))
        return
    await state.set_state(None)
    await state.update_data(tmp=str(folder), file=str(path))
    encrypted = await asyncio.to_thread(backups.is_encrypted, path)
    text = _(
        "admin_backups.confirm",
        name=_.esc(name),
        size=fmt_bytes((await asyncio.to_thread(path.stat)).st_size),
        encrypted=_("admin_backups.encrypted_yes") if encrypted else _("admin_backups.encrypted_no"),
    )
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_backups.btn.restore_yes"), _cb("go"), style="danger")],
        [Button.cb(_("nav.cancel"), _cb("home"))],
    ]
    await render(message, Screen(text, kb(rows)))


@router.message(BackupStates.upload)
async def not_a_file(message: Message, _: Translator) -> None:
    await message.answer(_("admin_backups.send_document"))


@router.callback_query(BkCb.filter(F.a == "go"), HasPermission(Permission.BACKUP_RESTORE))
async def confirm_restore(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("file"):
        await render(callback, Screen(_("admin_backups.expired"), kb([[Button.cb(_("nav.back"), _cb("home"))]])))
        return
    await answer_callback(callback, _("admin_backups.restoring"))
    await _restore(callback, ctx, _, state, backups.password_from(ctx.settings))


@router.message(BackupStates.password, F.text, HasPermission(Permission.BACKUP_RESTORE))
async def got_password(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    password = message.text or ""
    await delete_message(message)
    await state.set_state(None)
    await _restore(message, ctx, _, state, password)


async def _restore(
    event: Message | CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext, password: str | None
) -> None:
    data = await state.get_data()
    path = Path(str(data.get("file") or ""))
    actor_id = event.from_user.id if event.from_user else 0
    if not await asyncio.to_thread(path.is_file):
        await _drop_upload(state)
        await render(event, Screen(_("admin_backups.expired"), kb([[Button.cb(_("nav.back"), _cb("home"))]])))
        return
    with contextlib.suppress(OSError):
        await asyncio.to_thread(os.utime, path.parent)  # in use: keep the periodic sweep away from it
    settings = ctx.settings
    try:
        safety = await backups.create(
            ctx.engine, backups.directory_from(settings), password=backups.password_from(settings)
        )
    except Exception as exc:
        log.exception("safety backup before restore failed")
        await render(
            event,
            Screen(_("admin_backups.safety_failed", error=_.esc(exc)), kb([[Button.cb(_("nav.back"), _cb("home"))]])),
        )
        return
    try:
        report = await backups.restore(ctx.engine, path, password=password, wipe=True)
    except backups.WrongPassword:
        await state.set_state(BackupStates.password)
        await render(event, Screen(_("admin_backups.ask_password"), kb([[Button.cb(_("nav.cancel"), _cb("home"))]])))
        return
    except Exception as exc:
        log.exception("restore from the bot failed")
        await _drop_upload(state)
        await render(
            event,
            Screen(_("admin_backups.restore_failed", error=_.esc(exc)), kb([[Button.cb(_("nav.back"), _cb("home"))]])),
        )
        return
    await _drop_upload(state)
    await _reload(ctx)
    await _log(ctx, actor_id, "backup.restore", file=path.name, rows=report.total, safety=safety.path.name)
    note = _("admin_backups.restored", rows=report.total, safety=_.esc(safety.path.name))
    await render(event, await home_screen(ctx, _, note))


async def _reload(ctx: AppContext) -> None:
    """Caches that hold database state: settings, text overrides, maintenance."""
    for step in (ctx.settings_provider.load, ctx.i18n.reload_overrides):
        try:
            await step()
        except Exception:
            log.exception("reload after restore failed")
    ctx.maintenance.invalidate()
