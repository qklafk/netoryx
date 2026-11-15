"""Moving customers over from another bot: Bedolaga (Remnawave) backups.

:func:`read_bedolaga` reads a backup made by that bot (``.tar.gz`` / ``.tar`` with ``database.sql`` from pg_dump,
``database.json`` or ``database.sqlite``, or one of those files on its own) and returns the users and subscriptions.
:func:`import_bedolaga` creates the users (Telegram id, name, language, balance through the ledger, who invited whom)
and their subscriptions. When the Remnawave panel those customers use is connected, each subscription adopts the
customer's existing panel account, so the links already in their apps keep working; without it a new account and a
new link are made on the next sync.

Nothing is overwritten: users that already exist here are skipped, so a second run imports only what is new.
Panel I/O (reading the panel's users) happens before the database transaction.
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
import tarfile
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    PanelKind,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    TxKind,
    User,
)
from app.db.session import SessionFactory
from app.services import wallet
from app.services.panels.base import RemoteAccount
from app.services.users import REFERRAL_CODE_LENGTH, SUB_TOKEN_BYTES, pick_language
from app.utils.security import random_code, random_token
from app.utils.time import utcnow

log = logging.getLogger(__name__)

TABLES = ("users", "subscriptions")
DUMP_NAMES = ("database.sql", "database.json", "database.sqlite")
MAX_MEMBER_BYTES = 512 * 1024 * 1024
BALANCE_REF = "import:bedolaga"


class BotImportError(ValueError):
    """The file cannot be read as a backup of that bot."""


@dataclass(slots=True)
class BotData:
    users: list[dict[str, Any]] = field(default_factory=list)
    subscriptions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ImportReport:
    users: int = 0
    subscriptions: int = 0
    links_kept: int = 0  # subscriptions that adopted the customer's existing panel account
    new_links: int = 0  # subscriptions that get a new account (the customer must import the new link)
    balance_kop: int = 0
    referrals: int = 0
    skipped_existing: int = 0
    skipped_no_telegram: int = 0
    skipped_deleted: int = 0
    dry_run: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}


# --- reading the backup -----------------------------------------------------------------------------------------------
def read_bedolaga(path: Path) -> BotData:
    """The users and subscriptions of a Bedolaga backup (raises :class:`BotImportError`)."""
    name = path.name.lower()
    if name.endswith((".tar.gz", ".tgz", ".tar")):
        return _read_archive(path)
    if name.endswith(".sql"):
        return _parse_sql(path.read_text(encoding="utf-8", errors="replace"))
    if name.endswith(".json"):
        return _parse_json(path.read_bytes())
    if name.endswith((".sqlite", ".db")):
        return _read_sqlite(path)
    raise BotImportError("expected a .tar.gz backup or database.sql / database.json / database.sqlite")


def _read_archive(path: Path) -> BotData:
    try:
        with tarfile.open(path) as tar:
            members = {Path(m.name).name: m for m in tar.getmembers() if m.isfile()}
            for dump in DUMP_NAMES:
                member = members.get(dump)
                if member is None:
                    continue
                if member.size > MAX_MEMBER_BYTES:
                    raise BotImportError(f"{dump} is too large")
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                data = handle.read()
                if dump.endswith(".sql"):
                    return _parse_sql(data.decode("utf-8", errors="replace"))
                if dump.endswith(".json"):
                    return _parse_json(data)
                with tempfile.TemporaryDirectory() as tmp:
                    copy = Path(tmp) / "database.sqlite"
                    copy.write_bytes(data)
                    return _read_sqlite(copy)
    except (tarfile.TarError, OSError) as exc:
        raise BotImportError(f"cannot read the archive: {exc}") from None
    raise BotImportError("the archive has no database.sql, database.json or database.sqlite")


_COPY_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def _copy_value(raw: str) -> str | None:
    """One field of a pg_dump ``COPY ... FROM stdin`` row (text format)."""
    if raw == "\\N":
        return None
    if "\\" not in raw:
        return raw
    out: list[str] = []
    i = 0
    while i < len(raw):
        char = raw[i]
        if char == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt in _COPY_ESCAPES:
                out.append(_COPY_ESCAPES[nxt])
                i += 2
                continue
            if nxt.isdigit():
                digits = raw[i + 1 : i + 4]
                out.append(chr(int(digits, 8)))
                i += 1 + len(digits)
                continue
        out.append(char)
        i += 1
    return "".join(out)


def _parse_sql(text: str) -> BotData:
    data = BotData()
    lines = io.StringIO(text)
    for line in lines:
        if not line.startswith("COPY "):
            continue
        head = line[5:].split(" FROM stdin", 1)[0]
        table, _, columns = head.partition(" (")
        table = table.split(".")[-1].strip('"')
        names = [c.strip().strip('"') for c in columns.rstrip(")").split(",")]
        rows: list[dict[str, Any]] = []
        for row in lines:
            row = row.rstrip("\n")
            if row == "\\.":
                break
            if table in TABLES:
                rows.append(dict(zip(names, (_copy_value(v) for v in row.split("\t")), strict=False)))
        if table in TABLES:
            getattr(data, table).extend(rows)
    if not data.users:
        raise BotImportError("no users table in the dump")
    return data


def _parse_json(raw: bytes) -> BotData:
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise BotImportError(f"invalid JSON: {exc}") from None
    tables = doc.get("data") if isinstance(doc, Mapping) else None
    if not isinstance(tables, Mapping) or not isinstance(tables.get("users"), list):
        raise BotImportError("no users table in the JSON backup")
    return BotData(
        users=[dict(r) for r in tables.get("users", []) if isinstance(r, Mapping)],
        subscriptions=[dict(r) for r in tables.get("subscriptions", []) or [] if isinstance(r, Mapping)],
    )


def _read_sqlite(path: Path) -> BotData:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:  # a plain `with` keeps it open
            conn.row_factory = sqlite3.Row
            data = BotData()
            for table in TABLES:
                try:
                    data_rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]  # table names are fixed
                except sqlite3.OperationalError:
                    data_rows = []
                getattr(data, table).extend(data_rows)
    except sqlite3.Error as exc:
        raise BotImportError(f"cannot read the SQLite database: {exc}") from None
    if not data.users:
        raise BotImportError("no users table in the SQLite database")
    return data


# --- importing --------------------------------------------------------------------------------------------------------
def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    return value in (True, 1, "t", "true", "True", "1")


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _latest(subscriptions: Iterable[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """The subscription with the latest end date per Bedolaga user id."""
    best: dict[int, dict[str, Any]] = {}
    for row in subscriptions:
        owner = _int(row.get("user_id"))
        end = _when(row.get("end_date"))
        if owner is None or end is None:
            continue
        current = best.get(owner)
        if current is None or end > (_when(current.get("end_date")) or end):
            best[owner] = row
    return best


def _status(row: Mapping[str, Any], expires: datetime, now: datetime) -> SubscriptionStatus:
    if row.get("status") == "disabled":
        return SubscriptionStatus.DISABLED
    return SubscriptionStatus.EXPIRED if expires <= now else SubscriptionStatus.ACTIVE


async def panel_accounts(registry: Any, panel_id: int) -> dict[str, RemoteAccount]:
    """Every user of a connected Remnawave panel by its user UUID (what Bedolaga keeps as ``remnawave_uuid``)."""
    backend = await registry.get(panel_id)
    if getattr(backend, "kind", None) != PanelKind.REMNAWAVE:
        raise BotImportError("the panel must be the Remnawave panel those customers use")
    return {account.raw.get("uuid", ""): account for account in await backend.all_accounts()}


async def import_bedolaga(
    session_factory: SessionFactory,
    settings: Settings,
    data: BotData,
    *,
    panel_id: int | None = None,
    accounts: Mapping[str, RemoteAccount] | None = None,
    apply: bool = False,
) -> ImportReport:
    """Create the users and subscriptions of ``data`` (see the module docstring); a dry run only counts."""
    report = ImportReport(dry_run=not apply)
    now = utcnow()
    latest = _latest(data.subscriptions)
    known = accounts or {}
    tg_of: dict[int, int] = {}
    imported: set[int] = set()
    async with session_factory.begin() as session:
        for row in data.users:
            tg_id, own_id = _int(row.get("telegram_id")), _int(row.get("id"))
            if tg_id is None or own_id is None:
                report.skipped_no_telegram += 1
                continue
            if row.get("status") == "deleted":
                report.skipped_deleted += 1
                continue
            tg_of[own_id] = tg_id
            if await session.get(User, tg_id) is not None:
                report.skipped_existing += 1
                continue
            report.users += 1
            balance = max(0, _int(row.get("balance_kopeks")) or 0)
            report.balance_kop += balance
            sub_row = latest.get(own_id)
            account = known.get(str(row.get("remnawave_uuid") or "")) if panel_id is not None else None
            if sub_row is not None:
                report.subscriptions += 1
                if account is not None and account.uuid:
                    report.links_kept += 1
                else:
                    report.new_links += 1
            if not apply:
                continue
            user = await _create_user(session, settings, row, tg_id)
            imported.add(tg_id)
            if balance:
                await wallet.apply(session, tg_id, balance, TxKind.ADMIN_ADJUST, BALANCE_REF)
            if sub_row is not None:
                await _create_subscription(session, user, sub_row, account, panel_id, now)
        for row in data.users:
            invitee, inviter = tg_of.get(_int(row.get("id")) or -1), tg_of.get(_int(row.get("referred_by_id")) or -1)
            if invitee is None or inviter is None or invitee == inviter:
                continue
            report.referrals += 1
            if apply and invitee in imported:  # links of users that were here before are left alone
                invited = await session.get(User, invitee)
                if invited is not None and invited.referred_by is None and await session.get(User, inviter) is not None:
                    invited.referred_by = inviter
    log.info("bedolaga import: %s", report.as_dict())
    return report


async def _create_user(session: AsyncSession, settings: Settings, row: Mapping[str, Any], tg_id: int) -> User:
    created = _when(row.get("created_at")) or utcnow()
    user = User(
        tg_id=tg_id,
        username=(row.get("username") or None),
        first_name=(row.get("first_name") or None),
        language=pick_language(str(row.get("language") or ""), settings),
        balance_kop=0,
        referral_code=random_code(REFERRAL_CODE_LENGTH),
        sub_token=random_token(SUB_TOKEN_BYTES),
        created_at=created,
        last_seen_at=created,
        is_banned=row.get("status") == "blocked",
    )
    session.add(user)
    await session.flush()
    return user


async def _create_subscription(
    session: AsyncSession,
    user: User,
    row: Mapping[str, Any],
    account: RemoteAccount | None,
    panel_id: int | None,
    now: datetime,
) -> Subscription:
    expires = _when(row.get("end_date")) or now
    adopt = account is not None and bool(account.uuid) and panel_id is not None
    client_uuid = account.uuid if adopt and account is not None else str(uuid.uuid4())
    sub = Subscription(
        user_id=user.tg_id,
        label=None,
        sub_token=user.sub_token,
        is_legacy=False,
        client_uuid=client_uuid,
        status=_status(row, expires, now),
        expires_at=expires,
        device_limit=max(0, _int(row.get("device_limit")) or 0),
        traffic_limit_gb=max(0, _int(row.get("traffic_limit_gb")) or 0),
        auto_renew=False,
        is_trial=_bool(row.get("is_trial")),
        revision=1,
        traffic_epoch=0,
    )
    session.add(sub)
    await session.flush()
    if adopt and account is not None and panel_id is not None:
        state = ProvisionState.ENABLED if account.enabled else ProvisionState.DISABLED
        # The customer's panel account is adopted as is: same name and identity, so their links keep working.
        session.add(
            Provision(
                subscription_id=sub.id,
                panel_id=panel_id,
                inbound_id=None,
                inbound_key=0,
                remote_email=account.email,
                remote_id=account.remote_id,
                remote_short_id=account.short_id,
                desired_state=state,
                actual_state=state,
                synced_revision=sub.revision,
                traffic_epoch=sub.traffic_epoch,
            )
        )
    return sub
