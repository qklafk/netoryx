"""Database backups: create, encrypt, rotate, send to Telegram and restore.

Two formats, both gzip-compressed and optionally encrypted with a password:

* logical (``*.jsonl.gz``) - every table of the schema exported with SQLAlchemy Core in primary-key batches as JSON
  lines. Works on SQLite, PostgreSQL and MySQL alike and restores into any of them (so it doubles as a migration
  path between databases);
* SQLite snapshot (``*.sqlite.gz``) - a consistent copy made with SQLite's online backup API (fast path for the
  default SQLite setup). It is restored through the same importer, so it can be loaded into PostgreSQL/MySQL too.

The logical file: a header line ``{"format": "vpnbot-backup", "version": 1, ...}``, then for every table a line
``{"table": name, "columns": [...]}`` followed by one JSON array per row, and a footer ``{"end": true, "rows": {...}}``
(a missing footer means a truncated file).

Encrypted files (``*.enc``): scrypt-derived key, AES-256-GCM in 1 MiB chunks with the chunk counter and a "last
chunk" flag authenticated, so reordering or truncation is detected.

Restoring needs an empty database, or ``wipe=True`` (an explicit confirmation) to replace everything; it runs in one
transaction. Stop the bot's background jobs (maintenance mode) while restoring.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import os
import shutil
import sqlite3
import struct
import tempfile
from collections.abc import AsyncIterator, Generator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import JSON, Column, Table, TypeDecorator, bindparam, delete, func, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.models import Base
from app.db.session import READ_ONLY_OPTION
from app.utils.time import utcnow

if TYPE_CHECKING:
    from aiogram import Bot

    from app.config import Settings

log = logging.getLogger(__name__)

FORMAT = "vpnbot-backup"
FORMAT_VERSION = 1
LOGICAL_SUFFIX = ".jsonl.gz"
SQLITE_SUFFIX = ".sqlite.gz"
ENCRYPTED_SUFFIX = ".enc"
NAME_PREFIX = "backup-"
BATCH_SIZE = 1000
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024
PART_SIZE = 49 * 1024 * 1024
DEFAULT_KEEP = 7
DEFAULT_DIRECTORY = "data/backups"

_SQLITE_MAGIC = b"SQLite format 3\x00"
_ENC_MAGIC = b"VPNBKENC"
_ENC_VERSION = 1
_ENC_CHUNK = 1024 * 1024
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_COPY_CHUNK = 1024 * 1024


class BackupError(Exception):
    pass


class WrongPassword(BackupError):
    pass


class RestoreNotEmpty(BackupError):
    """The target database has data and ``wipe`` was not confirmed."""


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    size: int
    created_at: datetime
    kind: str  # "logical" | "sqlite"
    encrypted: bool
    rows: dict[str, int] = field(default_factory=dict)  # logical backups only


@dataclass(slots=True)
class RestoreReport:
    kind: str
    rows: dict[str, int] = field(default_factory=dict)
    skipped_tables: list[str] = field(default_factory=list)  # in the backup but unknown to this version
    wiped: bool = False

    @property
    def total(self) -> int:
        return sum(self.rows.values())


@dataclass(frozen=True, slots=True)
class _Chunk:
    table: str
    columns: list[str]
    rows: list[list[Any]]


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
async def _iterate_in_thread[T](iterator: Iterator[T]) -> AsyncIterator[T]:
    """Drive a blocking iterator from a worker thread."""
    done = object()
    while True:
        item = await asyncio.to_thread(next, iterator, done)
        if item is done:
            return
        yield cast(T, item)


def _python_type(column: Column[Any]) -> type | None:
    column_type: Any = column.type
    if isinstance(column_type, TypeDecorator):
        column_type = column_type.impl_instance
    try:
        return cast(type, column_type.python_type)
    except NotImplementedError:
        return None


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return {"$b64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Decimal):
        return str(value)
    return value


def _decoder(column: Column[Any]) -> Any:
    column_type: Any = column.type
    if isinstance(column_type, TypeDecorator):
        column_type = column_type.impl_instance
    if isinstance(column_type, JSON):

        def to_json(value: Any) -> Any:
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError as exc:
                    raise BackupError(f"invalid JSON value in {column.table.name}.{column.name}") from exc
            return value

        return to_json
    kind = _python_type(column)
    if kind is datetime:

        def to_datetime(value: Any) -> Any:
            if isinstance(value, str):
                value = datetime.fromisoformat(value)
            if isinstance(value, datetime) and value.tzinfo is None:
                value = value.replace(tzinfo=UTC)  # SQLite/MySQL store UTC without a zone
            return value

        return to_datetime
    if kind is bool:
        return lambda value: bool(value) if isinstance(value, int) else value
    if kind is bytes:
        return lambda value: base64.b64decode(value["$b64"]) if isinstance(value, dict) else value
    return None


def _primary_key(table: Table) -> Column[Any]:
    keys = list(table.primary_key.columns)
    if len(keys) != 1:
        raise BackupError(f"table {table.name} needs a single-column primary key")
    return keys[0]


def _self_references(table: Table) -> list[Column[Any]]:
    return [col for col in table.columns if any(fk.column.table is table for fk in col.foreign_keys)]


def _timestamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%d-%H%M%S")


def _is_sqlite_file(engine: AsyncEngine) -> str | None:
    url = make_url(str(engine.url))
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    return database if database and database != ":memory:" else None


# ---------------------------------------------------------------------------------------------
# Encryption (streaming AES-GCM)
# ---------------------------------------------------------------------------------------------
def _derive_key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P).derive(password.encode("utf-8"))


def _aad(counter: int, last: bool) -> bytes:
    return struct.pack(">IB", counter, 1 if last else 0)


def encrypt_file(src: Path, dst: Path, password: str) -> None:
    """Encrypt ``src`` into ``dst`` with ``password`` (blocking)."""
    if not password:
        raise ValueError("empty password")
    salt, prefix = os.urandom(16), os.urandom(8)
    aead = AESGCM(_derive_key(password, salt))
    with src.open("rb") as fin, dst.open("wb") as fout:
        fout.write(_ENC_MAGIC + bytes([_ENC_VERSION]) + salt + prefix)
        counter = 0
        chunk = fin.read(_ENC_CHUNK)
        while True:
            following = fin.read(_ENC_CHUNK)
            last = not following
            sealed = aead.encrypt(prefix + struct.pack(">I", counter), chunk, _aad(counter, last))
            fout.write(struct.pack(">I", len(sealed)) + sealed)
            if last:
                break
            chunk, counter = following, counter + 1


def decrypt_file(src: Path, dst: Path, password: str) -> None:
    """Decrypt a file made by :func:`encrypt_file` (blocking). Raises :class:`WrongPassword`/:class:`BackupError`."""
    with src.open("rb") as fin, dst.open("wb") as fout:
        header = fin.read(len(_ENC_MAGIC) + 1 + 16 + 8)
        if len(header) < 33 or not header.startswith(_ENC_MAGIC) or header[8] != _ENC_VERSION:
            raise BackupError("not an encrypted backup")
        salt, prefix = header[9:25], header[25:33]
        aead = AESGCM(_derive_key(password, salt))
        counter = 0
        while True:
            size_raw = fin.read(4)
            if len(size_raw) != 4:
                raise BackupError("encrypted backup is truncated")
            sealed = fin.read(struct.unpack(">I", size_raw)[0])
            nonce = prefix + struct.pack(">I", counter)
            try:
                plain = aead.decrypt(nonce, sealed, _aad(counter, False))
                last = False
            except InvalidTag:
                try:
                    plain = aead.decrypt(nonce, sealed, _aad(counter, True))
                    last = True
                except InvalidTag:
                    raise WrongPassword("wrong password or corrupted backup") from None
            fout.write(plain)
            if last:
                if fin.read(1):
                    raise BackupError("unexpected data after the last chunk")
                return
            counter += 1


def is_encrypted(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(len(_ENC_MAGIC)) == _ENC_MAGIC


# ---------------------------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------------------------
async def _read_connection(engine: AsyncEngine) -> AsyncConnection:
    conn = await engine.connect()
    options: dict[str, Any] = {READ_ONLY_OPTION: True}
    if engine.dialect.name != "sqlite":
        options["isolation_level"] = "REPEATABLE READ"  # one consistent snapshot for all tables
    return await conn.execution_options(**options)


async def _export_rows(conn: AsyncConnection, table: Table) -> AsyncIterator[list[Any]]:
    pk = _primary_key(table)
    columns = list(table.columns)
    index = [c.name for c in columns].index(pk.name)
    last: Any = None
    while True:
        stmt = select(*columns).order_by(pk).limit(BATCH_SIZE)
        if last is not None:
            stmt = stmt.where(pk > last)
        rows = (await conn.execute(stmt)).all()
        if not rows:
            return
        last = rows[-1][index]
        for row in rows:
            yield [_encode(value) for value in row]
        if len(rows) < BATCH_SIZE:
            return


def _open_gzip_writer(path: Path) -> gzip.GzipFile:
    return gzip.open(path, "wb", 6)


def _line(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


async def export_logical(engine: AsyncEngine, dst: Path, *, now: datetime | None = None) -> dict[str, int]:
    """Write a logical backup of every table to ``dst`` (gzip). Returns row counts per table."""
    counts: dict[str, int] = {}
    gz = await asyncio.to_thread(_open_gzip_writer, dst)
    try:
        header = {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "created_at": (now or utcnow()).isoformat(),
            "dialect": engine.dialect.name,
            "tables": [t.name for t in Base.metadata.sorted_tables],
        }
        await asyncio.to_thread(gz.write, _line(header))
        conn = await _read_connection(engine)
        try:
            async with conn.begin():
                for table in Base.metadata.sorted_tables:
                    await asyncio.to_thread(gz.write, _line({"table": table.name, "columns": table.columns.keys()}))
                    buffer: list[bytes] = []
                    count = 0
                    async for row in _export_rows(conn, table):
                        buffer.append(_line(row))
                        count += 1
                        if len(buffer) >= BATCH_SIZE:
                            await asyncio.to_thread(gz.write, b"".join(buffer))
                            buffer.clear()
                    if buffer:
                        await asyncio.to_thread(gz.write, b"".join(buffer))
                    counts[table.name] = count
        finally:
            await conn.close()
        await asyncio.to_thread(gz.write, _line({"end": True, "rows": counts}))
    finally:
        await asyncio.to_thread(gz.close)
    return counts


def _sqlite_snapshot(database: str, dst: Path) -> None:
    """Consistent copy of a live SQLite database (online backup API), gzip-compressed (blocking)."""
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "snapshot.db"
        source = sqlite3.connect(database)
        target = sqlite3.connect(raw)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        with raw.open("rb") as fin, gzip.open(dst, "wb", 6) as fout:
            shutil.copyfileobj(fin, fout, _COPY_CHUNK)


async def create(
    engine: AsyncEngine,
    directory: Path | str,
    *,
    password: str | None = None,
    prefer_sqlite_snapshot: bool = True,
    now: datetime | None = None,
) -> BackupInfo:
    """Create a backup file in ``directory`` (SQLite snapshot when possible, else logical) and return its info."""
    now = now or utcnow()
    folder = Path(directory)
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    sqlite_path = _is_sqlite_file(engine) if prefer_sqlite_snapshot else None
    kind = "sqlite" if sqlite_path else "logical"
    suffix = SQLITE_SUFFIX if sqlite_path else LOGICAL_SUFFIX
    plain = folder / f"{NAME_PREFIX}{_timestamp(now)}{suffix}"
    rows: dict[str, int] = {}
    partial = plain.with_name(plain.name + ".part")
    try:
        if sqlite_path:
            await asyncio.to_thread(_sqlite_snapshot, sqlite_path, partial)
        else:
            rows = await export_logical(engine, partial, now=now)
        final = plain
        if password:
            final = plain.with_name(plain.name + ENCRYPTED_SUFFIX)
            await asyncio.to_thread(encrypt_file, partial, final, password)
            await asyncio.to_thread(partial.unlink)
        else:
            await asyncio.to_thread(partial.replace, final)
    except BaseException:
        await asyncio.to_thread(partial.unlink, True)
        raise
    size = (await asyncio.to_thread(final.stat)).st_size
    log.info("backup created: %s (%d bytes, %s%s)", final.name, size, kind, ", encrypted" if password else "")
    return BackupInfo(final, size, now, kind, bool(password), rows)


# ---------------------------------------------------------------------------------------------
# Listing & retention
# ---------------------------------------------------------------------------------------------
def list_backups(directory: Path | str) -> list[Path]:
    """Backup files in ``directory``, newest first (blocking; cheap)."""
    folder = Path(directory)
    if not folder.is_dir():
        return []
    suffixes = (LOGICAL_SUFFIX, SQLITE_SUFFIX, LOGICAL_SUFFIX + ENCRYPTED_SUFFIX, SQLITE_SUFFIX + ENCRYPTED_SUFFIX)
    files = [
        p for p in folder.iterdir() if p.is_file() and p.name.startswith(NAME_PREFIX) and p.name.endswith(suffixes)
    ]
    return sorted(files, key=lambda p: p.name, reverse=True)


def prune(directory: Path | str, keep: int) -> list[Path]:
    """Delete all but the ``keep`` newest backups (blocking). Returns the removed files."""
    removed = []
    for path in list_backups(directory)[max(1, keep) :]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            log.warning("could not delete old backup %s", path)
    return removed


# ---------------------------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------------------------
def split_file(path: Path, part_size: int = PART_SIZE) -> list[Path]:
    """Split ``path`` into ``<name>.001``, ``<name>.002``... next to it (blocking). One part if small enough."""
    parts: list[Path] = []
    with path.open("rb") as fin:
        index = 1
        while True:
            data = fin.read(part_size)
            if not data:
                break
            part = path.with_name(f"{path.name}.{index:03d}")
            part.write_bytes(data)
            parts.append(part)
            index += 1
    return parts


def join_parts(parts: list[Path], dst: Path) -> None:
    """Concatenate parts made by :func:`split_file` (blocking)."""
    with dst.open("wb") as fout:
        for part in sorted(parts, key=lambda p: p.name):
            with part.open("rb") as fin:
                shutil.copyfileobj(fin, fout, _COPY_CHUNK)


async def send_to_telegram(bot: Bot, chat_id: int, path: Path, *, caption: str | None = None) -> int:
    """Send the backup as document(s); files over 50 MB go in parts. Returns the number of messages sent."""
    from aiogram.types import FSInputFile

    size = (await asyncio.to_thread(path.stat)).st_size
    if size <= TELEGRAM_FILE_LIMIT:
        await bot.send_document(chat_id, FSInputFile(path), caption=caption)
        return 1
    parts = await asyncio.to_thread(split_file, path, PART_SIZE)
    try:
        for index, part in enumerate(parts, start=1):
            part_caption = f"{caption or path.name} ({index}/{len(parts)})"
            await bot.send_document(chat_id, FSInputFile(part), caption=part_caption)
    finally:
        for part in parts:
            await asyncio.to_thread(part.unlink, True)
    return len(parts)


# ---------------------------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------------------------
def _read_logical(path: Path) -> Generator[_Chunk]:
    """Chunks of a logical backup (blocking generator)."""
    with gzip.open(path, "rb") as fh:
        header = json.loads(fh.readline() or b"null")
        if not isinstance(header, dict) or header.get("format") != FORMAT:
            raise BackupError("not a backup file")
        if int(header.get("version", 0)) > FORMAT_VERSION:
            raise BackupError("backup was made by a newer version")
        table: str | None = None
        columns: list[str] = []
        rows: list[list[Any]] = []
        ended = False
        for raw in fh:
            item = json.loads(raw)
            if isinstance(item, list):
                if table is None:
                    raise BackupError("row before table header")
                rows.append(item)
                if len(rows) >= BATCH_SIZE:
                    yield _Chunk(table, columns, rows)
                    rows = []
                continue
            if table is not None:
                yield _Chunk(table, columns, rows)
                rows = []
            if item.get("end"):
                ended = True
                break
            table, columns = str(item["table"]), list(item["columns"])
        if not ended:
            raise BackupError("backup file is truncated")


def _read_sqlite(path: Path) -> Generator[_Chunk]:
    """Chunks of a SQLite snapshot for every table this version knows (blocking generator)."""
    conn = sqlite3.connect(path, check_same_thread=False)  # driven from changing worker threads
    try:
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in Base.metadata.sorted_tables:
            if table.name not in present:
                continue
            cursor = conn.execute(f'SELECT * FROM "{table.name}"')  # names come from our own schema
            columns = [d[0] for d in cursor.description]
            while True:
                rows = cursor.fetchmany(BATCH_SIZE)
                if not rows:
                    break
                yield _Chunk(table.name, columns, [list(r) for r in rows])
            yield _Chunk(table.name, columns, [])
    finally:
        conn.close()


async def _is_empty(conn: AsyncConnection) -> bool:
    for table in Base.metadata.sorted_tables:
        if await conn.scalar(select(func.count()).select_from(table)):
            return False
    return True


async def _wipe(conn: AsyncConnection) -> None:
    for table in reversed(Base.metadata.sorted_tables):
        for column in _self_references(table):
            await conn.execute(update(table).values({column.name: None}))
        await conn.execute(delete(table))


async def _reset_sequences(conn: AsyncConnection) -> None:
    if conn.dialect.name != "postgresql":
        return
    for table in Base.metadata.sorted_tables:
        pk = _primary_key(table)
        if _python_type(pk) is not int or pk.autoincrement is False:
            continue
        sequence = await conn.scalar(text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table.name, "c": pk.name})
        if not sequence:
            continue
        await conn.execute(
            text(f'SELECT setval(:s, COALESCE((SELECT MAX("{pk.name}") FROM "{table.name}"), 0) + 1, false)'),
            {"s": sequence},
        )


class _Importer:
    def __init__(self, conn: AsyncConnection, report: RestoreReport) -> None:
        self._conn = conn
        self._report = report
        self._tables = {t.name: t for t in Base.metadata.sorted_tables}
        self._deferred: dict[str, list[dict[str, Any]]] = {}

    async def add(self, chunk: _Chunk) -> None:
        table = self._tables.get(chunk.table)
        if table is None:
            if chunk.table not in self._report.skipped_tables:
                self._report.skipped_tables.append(chunk.table)
            return
        self._report.rows.setdefault(table.name, 0)
        if not chunk.rows:
            return
        known = [(i, table.columns[name]) for i, name in enumerate(chunk.columns) if name in table.columns]
        decoders = {column.name: _decoder(column) for _, column in known}
        self_refs = {c.name for c in _self_references(table)}
        pk = _primary_key(table)
        records: list[dict[str, Any]] = []
        for raw in chunk.rows:
            record: dict[str, Any] = {}
            for index, column in known:
                value = raw[index]
                decode = decoders[column.name]
                record[column.name] = decode(value) if decode and value is not None else value
            for name in self_refs:
                if record.get(name) is not None:
                    self._deferred.setdefault(table.name, []).append(
                        {"b_pk": record[pk.name], "b_value": record[name], "column": name}
                    )
                    record[name] = None
            records.append(record)
        await self._conn.execute(insert(table), records)
        self._report.rows[table.name] += len(records)

    async def finish(self) -> None:
        for table_name, updates in self._deferred.items():
            table = self._tables[table_name]
            pk = _primary_key(table)
            by_column: dict[str, list[dict[str, Any]]] = {}
            for item in updates:
                by_column.setdefault(item["column"], []).append({"b_pk": item["b_pk"], "b_value": item["b_value"]})
            for column, params in by_column.items():
                stmt = update(table).where(pk == bindparam("b_pk")).values({column: bindparam("b_value")})
                for start in range(0, len(params), BATCH_SIZE):
                    await self._conn.execute(stmt, params[start : start + BATCH_SIZE])
        await _reset_sequences(self._conn)


def _detect(path: Path) -> str:
    with gzip.open(path, "rb") as fh:
        head = fh.read(len(_SQLITE_MAGIC))
    if head == _SQLITE_MAGIC:
        return "sqlite"
    if head.startswith(b"{"):
        return "logical"
    raise BackupError("unknown backup format")


def _gunzip(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as fin, dst.open("wb") as fout:
        shutil.copyfileobj(fin, fout, _COPY_CHUNK)


async def restore(
    engine: AsyncEngine, path: Path | str, *, password: str | None = None, wipe: bool = False
) -> RestoreReport:
    """Load a backup (any format, encrypted or not) into the database of ``engine``.

    The schema must exist (run migrations first). Raises :class:`RestoreNotEmpty` when the database has data and
    ``wipe`` is not set; with ``wipe=True`` all current rows are deleted first. Runs in one transaction.
    """
    source = Path(path)
    with tempfile.TemporaryDirectory() as tmp:
        work = source
        if await asyncio.to_thread(is_encrypted, source):
            if not password:
                raise WrongPassword("the backup is encrypted; a password is required")
            work = Path(tmp) / "decrypted.gz"
            await asyncio.to_thread(decrypt_file, source, work, password)
        try:
            kind = await asyncio.to_thread(_detect, work)
        except (OSError, EOFError) as exc:
            raise BackupError(f"cannot read backup: {exc}") from exc
        if kind == "sqlite":
            raw = Path(tmp) / "snapshot.db"
            await asyncio.to_thread(_gunzip, work, raw)
            chunks = _read_sqlite(raw)
        else:
            chunks = _read_logical(work)
        report = RestoreReport(kind)
        try:
            async with engine.begin() as conn:
                if not await _is_empty(conn):
                    if not wipe:
                        raise RestoreNotEmpty("the database is not empty; confirm wiping it to restore")
                    await _wipe(conn)
                    report.wiped = True
                importer = _Importer(conn, report)
                async for chunk in _iterate_in_thread(chunks):
                    await importer.add(chunk)
                await importer.finish()
        finally:
            await asyncio.to_thread(chunks.close)  # release the snapshot file before the temp dir is removed
    log.info("backup restored (%s): %d rows%s", kind, report.total, ", database wiped first" if report.wiped else "")
    return report


# ---------------------------------------------------------------------------------------------
# Scheduled job
# ---------------------------------------------------------------------------------------------
def directory_from(settings: Settings) -> Path:
    return Path(settings.backup_dir or DEFAULT_DIRECTORY)


def keep_from(settings: Settings) -> int:
    return max(1, settings.backup_keep)


def password_from(settings: Settings) -> str | None:
    secret = settings.backup_password
    return (secret.get_secret_value() or None) if secret is not None else None


async def run_scheduled(engine: AsyncEngine, settings: Settings, bot: Bot | None = None) -> BackupInfo:
    """Create a backup, rotate old ones and send it to ``BACKUP_CHAT_ID`` (first admin by default)."""
    folder = directory_from(settings)
    info = await create(engine, folder, password=password_from(settings))
    await asyncio.to_thread(prune, folder, keep_from(settings))
    chat_id = settings.backup_target_chat
    if bot is not None and chat_id is not None:
        caption = f"{settings.brand_name} backup {info.created_at:%Y-%m-%d %H:%M} UTC"
        try:
            await send_to_telegram(bot, chat_id, info.path, caption=caption)
        except Exception:
            log.exception("could not send backup %s to chat %s", info.path.name, chat_id)
    return info
