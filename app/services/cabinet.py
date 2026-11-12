"""Web cabinet: one-time login tokens, Telegram Login Widget check, customer sessions and confirmed e-mails."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import CursorResult, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import CabinetSession, KeyValue, UserEmail
from app.db.session import SessionFactory, read_session
from app.services import kv
from app.utils.privacy import client_ip_for_record
from app.utils.security import const_eq, random_token, sha256_hex

LOGIN_PREFIX = "cablogin:"  # one-time login links (bot, e-mail)
VERIFY_PREFIX = "cabmail:"  # e-mail confirmation links
WIDGET_PREFIX = "cabtg:"  # used Telegram Login Widget payloads (replay guard)
TOKEN_BYTES = 32
LOGIN_TTL = timedelta(minutes=10)
VERIFY_TTL = timedelta(hours=24)
WIDGET_MAX_AGE = timedelta(minutes=15)
WIDGET_CLOCK_SKEW = timedelta(minutes=2)
SESSION_TOUCH_INTERVAL = timedelta(minutes=10)
MAX_EMAIL_LENGTH = 254

TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
_LABEL = r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
_EMAIL_RE = re.compile(rf"^[a-z0-9._%+'\-]{{1,64}}@{_LABEL}(?:\.{_LABEL})+$")

METHODS = ("bot", "telegram", "email")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------------------------
# One-time tokens (kv; only a hash of the token is stored)
# ---------------------------------------------------------------------------------------------
def _key(prefix: str, token: str) -> str:
    digest = hashlib.sha256(token.encode()).digest()
    return prefix + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def issue_token(
    session_factory: SessionFactory,
    prefix: str,
    payload: dict[str, Any],
    ttl: timedelta,
    *,
    now: datetime | None = None,
) -> str:
    """Store a single-use token carrying ``payload``; returns the token (never stored in clear)."""
    token = random_token(TOKEN_BYTES)
    expires = (now or _now()) + ttl
    async with session_factory.begin() as session:
        await kv.add(session, _key(prefix, token), json.dumps({**payload, "exp": expires.timestamp()}))
    return token


async def redeem_token(
    session: AsyncSession, prefix: str, token: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Consume a token in the caller's transaction; its payload, or None if unknown, used or expired."""
    if not TOKEN_RE.match(token):
        return None
    key = _key(prefix, token)
    raw = await kv.get(session, key)
    if raw is None or not await kv.delete(session, key):  # delete() is the single-use guard under concurrency
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or (now or _now()).timestamp() >= float(data["exp"]):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return data


def _expired(raw: str, now_ts: float) -> bool:
    try:
        return float(json.loads(raw)["exp"]) <= now_ts
    except (ValueError, KeyError, TypeError):
        return True


async def purge_expired_tokens(session_factory: SessionFactory, *, now: datetime | None = None) -> int:
    """Delete expired cabinet tokens and replay markers; returns how many were removed."""
    current = (now or _now()).timestamp()
    removed = 0
    prefixes = (LOGIN_PREFIX, VERIFY_PREFIX, WIDGET_PREFIX)
    async with session_factory.begin() as session:
        rows = (
            await session.execute(
                select(KeyValue.key, KeyValue.value).where(
                    or_(*(KeyValue.key.startswith(p, autoescape=True) for p in prefixes))
                )
            )
        ).all()
        stale = [key for key, value in rows if _expired(value, current)]
        if stale:
            result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
                delete(KeyValue).where(KeyValue.key.in_(stale))
            )
            removed = int(result.rowcount or 0)
    return removed


# ---------------------------------------------------------------------------------------------
# Telegram Login Widget
# ---------------------------------------------------------------------------------------------
class WidgetError(ValueError):
    """The Login Widget payload is forged, malformed or too old."""


@dataclass(frozen=True, slots=True)
class WidgetUser:
    id: int
    first_name: str | None
    username: str | None
    auth_date: datetime
    digest: str  # the payload hash (replay key)


def sign_widget_login(fields: Mapping[str, str], bot_token: str) -> str:
    """The ``hash`` Telegram sends for ``fields``: HMAC-SHA256 of the sorted lines with SHA256(bot token)."""
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()) if k != "hash")
    secret = hashlib.sha256(bot_token.encode()).digest()
    return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


def verify_widget_login(
    params: Mapping[str, str], bot_token: str, *, now: datetime | None = None, max_age: timedelta = WIDGET_MAX_AGE
) -> WidgetUser:
    """Check a Telegram Login Widget payload (signature and freshness of ``auth_date``)."""
    if not params or len(params) > 20 or any(len(k) > 32 or len(v) > 1024 for k, v in params.items()):
        raise WidgetError("malformed payload")
    received = params.get("hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", received):
        raise WidgetError("hash is missing")
    if not const_eq(sign_widget_login(params, bot_token), received):
        raise WidgetError("bad signature")
    try:
        auth_date = datetime.fromtimestamp(int(params["auth_date"]), UTC)
        user_id = int(params["id"])
    except (KeyError, ValueError, OverflowError, OSError):
        raise WidgetError("id or auth_date is invalid") from None
    current = now or _now()
    if current - auth_date > max_age or auth_date - current > WIDGET_CLOCK_SKEW:
        raise WidgetError("login data is stale")
    if user_id <= 0:
        raise WidgetError("id is invalid")
    return WidgetUser(user_id, params.get("first_name"), params.get("username"), auth_date, received)


async def claim_widget_payload(session: AsyncSession, digest: str, *, now: datetime | None = None) -> bool:
    """Remember a payload so it cannot be replayed; False if it was already used."""
    expires = (now or _now()) + WIDGET_MAX_AGE + WIDGET_CLOCK_SKEW
    key = WIDGET_PREFIX + base64.urlsafe_b64encode(bytes.fromhex(digest)).rstrip(b"=").decode()
    return await kv.add(session, key, json.dumps({"exp": expires.timestamp()}))


# ---------------------------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CabinetPrincipal:
    user_id: int
    session_id: int
    expires_at: datetime


def csrf_token_for(session_token: str) -> str:
    """CSRF token bound to a session: only someone who can read the HttpOnly cookie can derive it."""
    return sha256_hex("cabinet-csrf:" + session_token)[:43]


async def create_session(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    method: str,
    *,
    ip: str | None,
    user_agent: str | None,
    now: datetime | None = None,
) -> tuple[str, CabinetSession]:
    """Create a session in the caller's transaction; returns ``(cookie token, row)``."""
    current = now or _now()
    token = random_token(TOKEN_BYTES)
    kept_ip = client_ip_for_record(ip, settings.client_ip_logging)
    keep_agent = settings.client_ip_logging != "off"
    row = CabinetSession(
        user_id=user_id,
        token_hash=sha256_hex(token),
        method=method,
        ip=kept_ip[:64] if kept_ip else None,
        user_agent=user_agent[:256] if user_agent and keep_agent else None,
        created_at=current,
        expires_at=current + timedelta(days=max(1, settings.cabinet_session_days)),
        last_used_at=current,
        revoked=False,
    )
    session.add(row)
    await session.flush()
    return token, row


async def authenticate(
    session_factory: SessionFactory, token: str, *, now: datetime | None = None
) -> CabinetPrincipal | None:
    """Resolve a session cookie to its user; None if unknown, revoked or expired."""
    if not TOKEN_RE.match(token):
        return None
    current = now or _now()
    async with read_session(session_factory) as session:
        row = await session.scalar(select(CabinetSession).where(CabinetSession.token_hash == sha256_hex(token)))
    if row is None or row.revoked or row.expires_at <= current:
        return None
    if row.last_used_at is None or current - row.last_used_at >= SESSION_TOUCH_INTERVAL:
        async with session_factory.begin() as session:
            await session.execute(
                update(CabinetSession)
                .where(CabinetSession.id == row.id, CabinetSession.revoked.is_(False))
                .values(last_used_at=current)
            )
    return CabinetPrincipal(row.user_id, row.id, row.expires_at)


async def revoke_session(session: AsyncSession, session_id: int, user_id: int) -> bool:
    """Revoke one session of ``user_id``; True if it was active."""
    result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
        update(CabinetSession)
        .where(CabinetSession.id == session_id, CabinetSession.user_id == user_id, CabinetSession.revoked.is_(False))
        .values(revoked=True)
    )
    return bool(result.rowcount)


async def revoke_all(session: AsyncSession, user_id: int) -> int:
    """Revoke every session of the user (log out everywhere); returns how many were active."""
    result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
        update(CabinetSession)
        .where(CabinetSession.user_id == user_id, CabinetSession.revoked.is_(False))
        .values(revoked=True)
    )
    return int(result.rowcount or 0)


async def purge_sessions(
    session_factory: SessionFactory, *, keep: timedelta = timedelta(days=7), now: datetime | None = None
) -> int:
    """Delete sessions that expired, or were revoked, more than ``keep`` ago."""
    cutoff = (now or _now()) - keep
    async with session_factory.begin() as session:
        result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
            delete(CabinetSession).where(
                or_(
                    CabinetSession.expires_at < cutoff,
                    (CabinetSession.revoked.is_(True)) & (CabinetSession.created_at < cutoff),
                )
            )
        )
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------------------------
# E-mail addresses
# ---------------------------------------------------------------------------------------------
def normalize_email(raw: str) -> str | None:
    """Lower-cased address, or None when it does not look like a plain mailbox."""
    value = raw.strip().lower()
    if len(value) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(value):
        return None
    return value


async def get_email(session: AsyncSession, user_id: int) -> UserEmail | None:
    return await session.get(UserEmail, user_id)


async def user_id_for_email(session: AsyncSession, email: str) -> int | None:
    """The user who confirmed ``email`` (already normalized), if any."""
    return await session.scalar(select(UserEmail.user_id).where(UserEmail.email == email))


async def bind_email(session: AsyncSession, user_id: int, email: str, *, now: datetime | None = None) -> bool:
    """Attach a confirmed ``email`` to the user (replacing their previous one); False if somebody else owns it."""
    owner = await user_id_for_email(session, email)
    if owner is not None and owner != user_id:
        return False
    current = now or _now()
    existing = await session.get(UserEmail, user_id)
    if existing is not None:
        existing.email = email
        existing.verified_at = current
        await session.flush()
        return True
    try:
        async with session.begin_nested():
            session.add(UserEmail(user_id=user_id, email=email, verified_at=current))
            await session.flush()
    except IntegrityError:
        return False
    return True


async def remove_email(session: AsyncSession, user_id: int) -> bool:
    result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
        delete(UserEmail).where(UserEmail.user_id == user_id)
    )
    return bool(result.rowcount)
