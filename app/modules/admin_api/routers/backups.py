"""Backups: list, create now, download, upload, delete and restore (restore and download: owner only)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from app.core.permissions import Permission
from app.modules.admin_api.deps import Actor, bad_request, conflict, not_found, require
from app.modules.admin_api.schemas.backups import BackupOut, BackupsOut, RestoreIn, RestoreOut
from app.modules.admin_api.schemas.common import Ok
from app.services import backups, kv
from app.utils.time import utcnow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/backups", tags=["admin: backups"])

Operator = Annotated[Actor, Depends(require(Permission.BACKUPS))]
Owner = Annotated[Actor, Depends(require(Permission.BACKUPS, Permission.BACKUP_RESTORE))]

NAME_RE = re.compile(r"^backup-[0-9A-Za-z_\-]{1,64}\.(?:jsonl|sqlite)\.gz(?:\.enc)?$")
_SUFFIXES = (
    backups.LOGICAL_SUFFIX + backups.ENCRYPTED_SUFFIX,
    backups.SQLITE_SUFFIX + backups.ENCRYPTED_SUFFIX,
    backups.LOGICAL_SUFFIX,
    backups.SQLITE_SUFFIX,
)
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
Name = Annotated[str, PathParam(min_length=8, max_length=128)]

_RESTORE_LOCK = asyncio.Lock()
RESTORE_LOCK = "backups.restore"  # shared with other processes (the bot can restore too)
RESTORE_LOCK_TTL = 1800.0


def _directory(actor: Actor) -> Path:
    return backups.directory_from(actor.ctx.settings)


def _path(actor: Actor, name: str) -> Path:
    if not NAME_RE.fullmatch(name):
        raise bad_request("bad_name", "Not a backup file name")
    folder = _directory(actor).resolve()
    path = (folder / name).resolve()
    if path.parent != folder:
        raise bad_request("bad_name", "Not a backup file name")
    return path


def _info(path: Path) -> BackupOut:
    stat = path.stat()
    name = path.name
    return BackupOut(
        name=name,
        size=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime, UTC),
        kind="sqlite" if ".sqlite." in name else "logical",
        encrypted=name.endswith(backups.ENCRYPTED_SUFFIX),
    )


def _listing(actor: Actor) -> BackupsOut:
    settings = actor.ctx.settings
    folder = _directory(actor)
    return BackupsOut(
        directory=str(folder),
        keep=backups.keep_from(settings),
        scheduled=bool(settings.backup_enabled),
        schedule_time=str(settings.backup_time) if getattr(settings, "backup_time", None) is not None else None,
        encrypted_by_default=backups.password_from(settings) is not None,
        items=[_info(p) for p in backups.list_backups(folder)],
    )


@router.get("", response_model=BackupsOut)
async def list_backups(actor: Operator) -> BackupsOut:
    """Backup files on the server, newest first. Schedule and retention are in the settings (group ``backups``)."""
    return await asyncio.to_thread(_listing, actor)


@router.post("", response_model=BackupOut, status_code=201)
async def create_backup(actor: Operator) -> BackupOut:
    """Make a backup now (encrypted when a backup password is set); old ones beyond the retention are removed."""
    ctx = actor.ctx
    settings = ctx.settings
    folder = _directory(actor)
    try:
        info = await backups.create(ctx.engine, folder, password=backups.password_from(settings))
    except (OSError, backups.BackupError) as exc:
        raise conflict("backup_failed", f"Backup failed: {exc}"[:500]) from None
    await asyncio.to_thread(backups.prune, folder, backups.keep_from(settings))
    async with ctx.session_factory.begin() as session:
        await actor.audit(session, "backup.create", "backup", info.path.name, size=info.size, kind=info.kind)
    return await asyncio.to_thread(_info, info.path)


@router.get("/{name}")
async def download_backup(name: Name, actor: Owner) -> FileResponse:
    """Download a backup file (it holds every user's data: keep it safe)."""
    path = _path(actor, name)
    if not await asyncio.to_thread(path.is_file):
        raise not_found("Backup not found")
    async with actor.ctx.session_factory.begin() as session:
        await actor.audit(session, "backup.download", "backup", name)
    return FileResponse(
        path, media_type="application/octet-stream", filename=name, headers={"Cache-Control": "no-store"}
    )


@router.delete("/{name}", response_model=Ok)
async def delete_backup(name: Name, actor: Operator) -> Ok:
    path = _path(actor, name)
    try:
        await asyncio.to_thread(path.unlink)
    except FileNotFoundError:
        raise not_found("Backup not found") from None
    async with actor.ctx.session_factory.begin() as session:
        await actor.audit(session, "backup.delete", "backup", name)
    return Ok()


def _suffix(filename: str) -> str | None:
    lower = filename.lower()
    return next((s for s in _SUFFIXES if lower.endswith(s)), None)


@router.post("/upload", response_model=BackupOut, status_code=201)
async def upload_backup(
    request: Request,
    actor: Owner,
    filename: Annotated[str, Query(min_length=4, max_length=200)],
) -> BackupOut:
    """Upload a backup file (raw request body) to restore it afterwards."""
    suffix = _suffix(filename)
    if suffix is None:
        raise bad_request("bad_name", "Expected a .jsonl.gz or .sqlite.gz backup (optionally .enc)")
    folder = _directory(actor)
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    name = f"{backups.NAME_PREFIX}{utcnow():%Y%m%d-%H%M%S}-upload{suffix}"
    target = _path(actor, name)
    partial = target.with_name(target.name + ".part")
    size = 0
    try:
        with await asyncio.to_thread(partial.open, "wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise bad_request("too_large", "The file is too large")
                await asyncio.to_thread(fh.write, chunk)
        if size == 0:
            raise bad_request("empty", "Send the file as the request body")
        await asyncio.to_thread(partial.replace, target)
    finally:
        await asyncio.to_thread(partial.unlink, True)
    async with actor.ctx.session_factory.begin() as session:
        await actor.audit(session, "backup.upload", "backup", name, size=size)
    return await asyncio.to_thread(_info, target)


async def _rekey(ctx: Any, key_before: str) -> bool:
    """A backup from another install brings its own application secret (when SECRET_KEY is not set): panel
    credentials and secret settings in it are encrypted with that key, so switch to it before reloading anything.

    Returns whether the key changed (other processes of this install still use the old one until restarted)."""
    key_after = await kv.get_or_create_secret(ctx.session_factory, ctx.settings)
    if key_after == key_before:
        return False
    ctx.secret_box.rekey(key_after)
    log.warning("restored backup brought another application secret; switched to it, restart the other processes")
    return True


async def _after_restore(ctx: Any, key_before: str) -> bool:
    """Reload everything cached from the database. True if the application secret changed."""
    key_changed = False
    try:
        key_changed = await _rekey(ctx, key_before)
    except Exception:
        log.exception("after restore: switching the secret key failed")
    for name, call in (
        ("settings", lambda: ctx.settings_provider.load()),
        ("texts", lambda: ctx.i18n.reload_overrides()),
        ("panels", lambda: ctx.panels.close()),
    ):
        try:
            await call()
        except Exception:
            log.exception("after restore: reloading %s failed", name)
    maintenance = getattr(ctx, "maintenance", None)
    if maintenance is not None:
        maintenance.invalidate()
    return key_changed


async def _safety_backup(ctx: Any, folder: Path) -> Path:
    """Back up the current data under a name of its own (a plain backup made in the same second as the file being
    restored would replace that file)."""
    staging = folder / ".safety"
    info = await backups.create(ctx.engine, staging, password=backups.password_from(ctx.settings))
    suffix = _suffix(info.path.name) or ""
    stem = info.path.name[: len(info.path.name) - len(suffix)] + "-safety"
    target = folder / (stem + suffix)
    # Never replace an existing file: restoring a safety copy within the same second would overwrite it first.
    counter = 2
    while await asyncio.to_thread(target.exists):
        target = folder / f"{stem}-{counter}{suffix}"
        counter += 1
    await asyncio.to_thread(info.path.replace, target)
    return target


@asynccontextmanager
async def _restore_slot(ctx: Any) -> AsyncIterator[bool]:
    """Only one restore at a time, in this process and across processes when a lock manager is available."""
    if _RESTORE_LOCK.locked():
        yield False
        return
    async with _RESTORE_LOCK:
        locks = getattr(ctx, "locks", None)
        if locks is None:
            yield True
            return
        async with locks.job_lock(RESTORE_LOCK, RESTORE_LOCK_TTL) as acquired:
            yield acquired


@router.post("/restore", response_model=RestoreOut)
async def restore_backup(body: RestoreIn, actor: Owner) -> RestoreOut:
    """Replace ALL data with a backup (a safety copy of the current data is made first). Everyone (you too) is
    logged out of the web admin afterwards."""
    ctx = actor.ctx
    settings = ctx.settings
    path = _path(actor, body.name)
    if not await asyncio.to_thread(path.is_file):
        raise not_found("Backup not found")
    async with _restore_slot(ctx) as acquired:
        if not acquired:
            raise conflict("busy", "A restore is already running")
        try:
            safety = await _safety_backup(ctx, _directory(actor))
        except Exception as exc:
            log.exception("safety backup before restore failed")
            raise conflict("safety_backup_failed", f"Could not back up the current data first: {exc}"[:500]) from None
        key_before = await kv.get_or_create_secret(ctx.session_factory, settings)
        try:
            report = await backups.restore(ctx.engine, path, password=body.password or None, wipe=True)
        except backups.WrongPassword as exc:
            raise bad_request("wrong_password", str(exc) or "Wrong backup password") from None
        except backups.BackupError as exc:
            raise bad_request("bad_backup", str(exc)[:500]) from None
        restart_recommended = await _after_restore(ctx, key_before)
    async with ctx.session_factory.begin() as session:
        await actor.audit(
            session,
            "backup.restore",
            "backup",
            body.name,
            rows=report.total,
            wiped=report.wiped,
            safety=safety.name,
        )
    log.warning("backup %s restored by %s from the web admin (safety copy %s)", body.name, actor.tg_id, safety)
    return RestoreOut(
        kind=report.kind,
        rows=report.total,
        tables=dict(report.rows),
        skipped_tables=list(report.skipped_tables),
        wiped=report.wiped,
        safety_backup=safety.name,
        restart_recommended=restart_recommended,
    )
