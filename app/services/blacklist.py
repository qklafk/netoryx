"""External blacklist of Telegram user IDs.

The owner subscribes to plain text lists (one ID per line, optionally followed by a reason; ``#`` starts a comment)
and may add IDs by hand (``BLACKLIST_MANUAL_IDS``). Each source is downloaded with a size limit and conditional
requests; a failed download keeps the last good copy. Lists are stored compressed in the ``kv`` table (no extra
tables) and kept in memory as a set, so checking an update costs one set lookup.

Listed users are either flagged (administrators are told) or banned through :mod:`app.services.bans`. Users with an
active paid subscription are only flagged unless ``BLACKLIST_BAN_PAYING`` is on, administrators are never touched,
and a ban is lifted automatically only if the blacklist itself imposed it and the ID has left every list.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import aiohttp
from sqlalchemy import func, select

from app.db.models import KeyValue, Subscription, SubscriptionStatus, User
from app.db.session import read_session
from app.services import admins, audit, bans, kv
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory

log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_IDS_PER_SOURCE = 1_000_000
FETCH_TIMEOUT_SEC = 30.0
CONNECT_TIMEOUT_SEC = 10.0
MAX_REASON_LENGTH = 120
RELOAD_CHECK_SEC = 60.0  # how often other processes look for a newer stored list
DECISION_TTL_SEC = 300.0  # per-user verdict cache of the update path
CHUNK_CHARS = 60_000  # kv values stay below MySQL's TEXT limit
BATCH = 200
_IN_CHUNK = 500

META_KEY = "blacklist:meta"
SOURCE_PREFIX = "blacklist:src:"
FLAG_PREFIX = "blacklist:flag:"
MAX_TG_ID = 2**63 - 1  # users.tg_id is a signed 64-bit column

_LINE = re.compile(r"^\+?(\d{1,20})(?:[\s,;:|]+(.*))?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

Verdict = Literal["staff", "exempt", "banned", "ban", "flag"]


# --- parsing and storage -----------------------------------------------------------------------------------------


@dataclass(slots=True)
class ParsedList:
    entries: dict[int, str] = field(default_factory=dict)  # tg_id -> reason ("" if none)
    skipped: int = 0  # lines that are neither comments nor valid IDs
    truncated: bool = False


def parse_list(text: str, max_ids: int = MAX_IDS_PER_SOURCE) -> ParsedList:
    """IDs from a plain text list. Blank lines and ``#`` comments are ignored, junk lines are counted."""
    result = ParsedList()
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith(("#", "//", ";")):
            continue
        match = _LINE.match(line)
        if match is None:
            result.skipped += 1
            continue
        tg_id = int(match.group(1))
        if not 0 < tg_id <= MAX_TG_ID:
            result.skipped += 1
            continue
        if len(result.entries) >= max_ids and tg_id not in result.entries:
            result.truncated = True
            break
        reason = (match.group(2) or "").split("#", 1)[0]
        result.entries[tg_id] = _CONTROL.sub(" ", reason).strip()[:MAX_REASON_LENGTH]
    return result


def encode_entries(entries: dict[int, str]) -> str:
    """Compact text form: sorted delta-encoded IDs plus the non-empty reasons, deflated and base64-encoded."""
    ids = sorted(entries)
    deltas = [b - a for a, b in zip([0, *ids], ids, strict=False)]
    reasons = {str(i): r for i, r in entries.items() if r}
    raw = json.dumps({"d": deltas, "r": reasons}, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def decode_entries(blob: str) -> dict[int, str]:
    data = json.loads(zlib.decompress(base64.b64decode(blob)))
    reasons: dict[str, str] = data.get("r") or {}
    entries: dict[int, str] = {}
    current = 0
    for delta in data.get("d") or []:
        current += int(delta)
        entries[current] = reasons.get(str(current), "")
    return entries


def source_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _chunk_key(skey: str, n: int) -> str:
    return f"{SOURCE_PREFIX}{skey}:{n}"


async def _read_meta(session: Any) -> dict[str, Any]:
    raw = await kv.get(session, META_KEY)
    if not raw:
        return {"sources": {}, "version": None}
    try:
        meta = json.loads(raw)
    except ValueError:
        return {"sources": {}, "version": None}
    meta.setdefault("sources", {})
    return meta


async def _write_blob(session: Any, skey: str, blob: str, old_chunks: int) -> int:
    parts = [blob[i : i + CHUNK_CHARS] for i in range(0, len(blob), CHUNK_CHARS)] or [""]
    for n, part in enumerate(parts):
        await kv.set(session, _chunk_key(skey, n), part)
    for n in range(len(parts), old_chunks):
        await kv.delete(session, _chunk_key(skey, n))
    return len(parts)


async def _read_blob(session: Any, skey: str, chunks: int) -> str | None:
    parts: list[str] = []
    for n in range(chunks):
        part = await kv.get(session, _chunk_key(skey, n))
        if part is None:
            return None
        parts.append(part)
    return "".join(parts)


# --- in-memory set -----------------------------------------------------------------------------------------------


class BlacklistCache:
    """The merged list of every stored source, refreshed from the database when its version changes."""

    def __init__(self) -> None:
        self.ids: frozenset[int] = frozenset()
        self.reasons: dict[int, str] = {}
        self.version: str | None = None
        self._checked = 0.0
        self._task: asyncio.Task[None] | None = None
        self._manual_src: list[int] | None = None
        self._manual: frozenset[int] = frozenset()
        self._verdicts: dict[int, tuple[float, bool]] = {}

    def clear(self) -> None:
        self.__init__()  # type: ignore[misc]

    def manual(self, settings: Settings) -> frozenset[int]:
        src = settings.blacklist_manual_ids
        if src is not self._manual_src:
            self._manual_src, self._manual = src, frozenset(src)
        return self._manual

    def merged(self, settings: Settings) -> frozenset[int]:
        manual = self.manual(settings)
        return self.ids | manual if manual else self.ids

    def listed(self, tg_id: int, settings: Settings) -> bool:
        """O(1): is ``tg_id`` on any list (always False while the feature is off)."""
        if not settings.blacklist_enabled:
            return False
        return tg_id in self.ids or tg_id in self.manual(settings)

    def reason(self, tg_id: int, settings: Settings) -> str:
        if tg_id in self.reasons:
            return self.reasons[tg_id]
        return "manual" if tg_id in self.manual(settings) else ""

    def set_entries(self, entries: dict[int, str], version: str | None) -> None:
        self.ids = frozenset(entries)
        self.reasons = {i: r for i, r in entries.items() if r}
        self.version = version
        self._verdicts.clear()

    def schedule_reload(self, session_factory: SessionFactory) -> None:
        """Pick up a list stored by another process (at most once per :data:`RELOAD_CHECK_SEC`, in the background)."""
        now = time.monotonic()
        if now - self._checked < RELOAD_CHECK_SEC or (self._task is not None and not self._task.done()):
            return
        self._checked = now
        self._task = asyncio.create_task(self._reload_quietly(session_factory), name="blacklist-reload")

    async def _reload_quietly(self, session_factory: SessionFactory) -> None:
        try:
            await self.reload(session_factory)
        except Exception:
            log.exception("could not load the blacklist")

    async def reload(self, session_factory: SessionFactory, *, force: bool = False) -> None:
        async with read_session(session_factory) as session, session.begin():
            meta = await _read_meta(session)
            if not force and meta.get("version") == self.version and self.version is not None:
                return
            merged: dict[int, str] = {}
            for url, info in meta["sources"].items():
                blob = await _read_blob(session, info["key"], int(info.get("chunks") or 0))
                if not blob:
                    continue
                try:
                    entries = decode_entries(blob)
                except (ValueError, zlib.error):
                    log.warning("stored blacklist of %s is unreadable", _safe_url(url))
                    continue
                for tg_id, reason in entries.items():
                    if reason or tg_id not in merged:
                        merged[tg_id] = reason
        self.set_entries(merged, meta.get("version"))
        self._checked = time.monotonic()

    def cached_verdict(self, tg_id: int) -> bool | None:
        item = self._verdicts.get(tg_id)
        if item is None or item[0] < time.monotonic():
            return None
        return item[1]

    def remember_verdict(self, tg_id: int, blocked: bool) -> None:
        if len(self._verdicts) > 50_000:
            self._verdicts.clear()
        self._verdicts[tg_id] = (time.monotonic() + DECISION_TTL_SEC, blocked)


CACHE = BlacklistCache()


# --- downloading -------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class FetchResult:
    status: Literal["ok", "not_modified", "error"]
    text: str = ""
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None


def _safe_url(url: str) -> str:
    """The URL without query string and credentials (lists may carry access tokens)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}{parts.path}"


async def fetch_source(
    http: aiohttp.ClientSession,
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    timeout_sec: float = FETCH_TIMEOUT_SEC,
) -> FetchResult:
    """Download one list; never raises. No database transaction may be open around this call."""
    headers = {"Accept": "text/plain, */*;q=0.5"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    timeout = aiohttp.ClientTimeout(total=timeout_sec, sock_connect=CONNECT_TIMEOUT_SEC)
    try:
        async with http.get(url, headers=headers, timeout=timeout, max_redirects=3) as resp:
            if resp.status == 304:
                return FetchResult("not_modified", etag=etag, last_modified=last_modified)
            if resp.status != 200:
                return FetchResult("error", error=f"HTTP {resp.status}")
            if resp.content_length is not None and resp.content_length > max_bytes:
                return FetchResult("error", error=f"larger than {max_bytes} bytes")
            body = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                body += chunk
                if len(body) > max_bytes:
                    return FetchResult("error", error=f"larger than {max_bytes} bytes")
            return FetchResult(
                "ok",
                text=bytes(body).decode("utf-8", errors="replace"),
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
            )
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        return FetchResult("error", error=exc.__class__.__name__)


# --- refresh -----------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RefreshReport:
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    removed: int = 0
    total_ids: int = 0
    errors: dict[str, str] = field(default_factory=dict)  # safe url -> error


async def refresh(
    session_factory: SessionFactory,
    http: aiohttp.ClientSession,
    settings: Settings,
    cache: BlacklistCache = CACHE,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> RefreshReport:
    """Download every configured list, store the good ones (a failure keeps the previous copy), reload ``cache``."""
    report = RefreshReport()
    urls = list(dict.fromkeys(settings.blacklist_urls))
    async with read_session(session_factory) as session, session.begin():
        meta = await _read_meta(session)
    old_sources: dict[str, Any] = meta["sources"]

    results: dict[str, tuple[FetchResult, ParsedList | None]] = {}
    for url in urls:  # sequential: a handful of sources, no transaction open
        info = old_sources.get(url) or {}
        result = await fetch_source(
            http, url, etag=info.get("etag"), last_modified=info.get("last_modified"), max_bytes=max_bytes
        )
        parsed: ParsedList | None = None
        if result.status == "ok":
            parsed = await asyncio.to_thread(parse_list, result.text)
            if not parsed.entries and parsed.skipped:
                result = FetchResult("error", error="no Telegram IDs found")  # an error page, not a list
                parsed = None
        results[url] = (result, parsed)

    now = utcnow().isoformat()
    changed = False
    async with session_factory.begin() as session:
        meta = await _read_meta(session)  # fresh copy: another worker may have refreshed meanwhile
        sources: dict[str, Any] = meta["sources"]
        for url in [u for u in sources if u not in urls]:
            info = sources.pop(url)
            for n in range(int(info.get("chunks") or 0)):
                await kv.delete(session, _chunk_key(info["key"], n))
            report.removed += 1
            changed = True
        for url, (result, parsed) in results.items():
            info = sources.setdefault(url, {"key": source_key(url), "chunks": 0, "count": 0})
            info["checked_at"] = now
            if result.status == "ok" and parsed is not None:
                info["chunks"] = await _write_blob(
                    session, info["key"], encode_entries(parsed.entries), int(info.get("chunks") or 0)
                )
                info.update(
                    count=len(parsed.entries),
                    skipped=parsed.skipped,
                    truncated=parsed.truncated,
                    etag=result.etag,
                    last_modified=result.last_modified,
                    fetched_at=now,
                    error=None,
                    error_at=None,
                )
                report.updated += 1
                changed = True
            elif result.status == "not_modified":
                info["error"] = None
                report.unchanged += 1
            else:
                info["error"], info["error_at"] = result.error, now
                report.failed += 1
                report.errors[_safe_url(url)] = result.error or "error"
        if changed or meta.get("version") is None:
            meta["version"] = now
        meta["refreshed_at"] = now
        await kv.set(session, META_KEY, json.dumps(meta, separators=(",", ":")))
    for url, error in report.errors.items():
        log.warning("blacklist %s: download failed (%s); keeping the previous copy", url, error)
    await cache.reload(session_factory, force=True)
    report.total_ids = len(cache.merged(settings))
    return report


# --- applying the policy -----------------------------------------------------------------------------------------


@dataclass(slots=True)
class ApplyReport:
    matched: int = 0
    banned: list[int] = field(default_factory=list)
    unbanned: list[int] = field(default_factory=list)
    new_flagged: list[int] = field(default_factory=list)
    skipped_staff: int = 0
    skipped_paying: int = 0


async def _paying(session: Any, user_ids: Iterable[int]) -> set[int]:
    """Users with an active subscription that is neither a trial nor complimentary."""
    ids = list(user_ids)
    if not ids:
        return set()
    rows = await session.scalars(
        select(Subscription.user_id)
        .where(
            Subscription.user_id.in_(ids),
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at > utcnow(),
            Subscription.is_trial.is_(False),
            Subscription.is_complimentary.is_(False),
        )
        .distinct()
    )
    return set(rows)


def _verdict(settings: Settings, user: User, *, staff: bool, paying: bool, mark: str | None) -> Verdict:
    if staff:
        return "staff"
    if settings.blacklist_action != "block" or mark == bans.MARK_EXEMPT:
        return "exempt" if mark == bans.MARK_EXEMPT else "flag"
    if user.is_banned:
        return "banned"
    if paying and not settings.blacklist_ban_paying:
        return "flag"
    return "ban"


async def _apply_user(
    session: Any, settings: Settings, user: User, paying: set[int], cache: BlacklistCache, report: ApplyReport
) -> Verdict:
    """Ban or flag one listed user (row locked); caller's transaction."""
    staff = await admins.resolve_role(session, settings, user.tg_id) is not None
    mark = await kv.get(session, bans.blacklist_mark_key(user.tg_id))
    verdict = _verdict(settings, user, staff=staff, paying=user.tg_id in paying, mark=mark)
    if verdict == "staff":
        report.skipped_staff += 1
    elif verdict == "ban":
        await bans.set_banned(session, user, True)
        await kv.set(session, bans.blacklist_mark_key(user.tg_id), bans.MARK_BANNED)
        await audit.log(
            session, 0, "user.blacklist_ban", "user", user.tg_id, "system", reason=cache.reason(user.tg_id, settings)
        )
        report.banned.append(user.tg_id)
    elif verdict in ("flag", "exempt"):
        if verdict == "flag" and user.tg_id in paying and settings.blacklist_action == "block":
            report.skipped_paying += 1
        if await kv.add(session, f"{FLAG_PREFIX}{user.tg_id}", utcnow().isoformat()):
            report.new_flagged.append(user.tg_id)
    return verdict


async def _known_users(session: Any, listed: frozenset[int]) -> list[int]:
    if not listed:
        return []
    total = await session.scalar(select(func.count()).select_from(User)) or 0
    if total <= len(listed):
        return [i for i in await session.scalars(select(User.tg_id)) if i in listed]
    ordered = sorted(listed)
    found: list[int] = []
    for start in range(0, len(ordered), _IN_CHUNK):
        part = ordered[start : start + _IN_CHUNK]
        found.extend(await session.scalars(select(User.tg_id).where(User.tg_id.in_(part))))
    return found


async def _markers(session: Any, prefix: str) -> dict[int, str]:
    rows = await session.execute(select(KeyValue.key, KeyValue.value).where(KeyValue.key.like(f"{prefix}%")))
    out: dict[int, str] = {}
    for key, value in rows:
        suffix = key[len(prefix) :]
        if suffix.isdigit():
            out[int(suffix)] = value
    return out


async def apply(session_factory: SessionFactory, settings: Settings, cache: BlacklistCache = CACHE) -> ApplyReport:
    """Bring bans and flags in line with the current list (idempotent; short transactions, no network I/O)."""
    report = ApplyReport()
    listed = cache.merged(settings)
    async with read_session(session_factory) as session, session.begin():
        known = await _known_users(session, listed)
        ban_marks = await _markers(session, bans.BLACKLIST_MARK_PREFIX)
        flag_marks = await _markers(session, FLAG_PREFIX)
    report.matched = len(known)

    # IDs that left every list: lift only the bans the blacklist imposed, forget flags and exemptions
    gone = sorted(i for i in ban_marks if i not in listed)
    for start in range(0, len(gone), BATCH):
        part = gone[start : start + BATCH]
        async with session_factory.begin() as session:
            for user in await session.scalars(select(User).where(User.tg_id.in_(part)).with_for_update()):
                mark = await kv.get(session, bans.blacklist_mark_key(user.tg_id))
                if mark == bans.MARK_BANNED and await bans.set_banned(session, user, False):
                    await audit.log(session, 0, "user.blacklist_unban", "user", user.tg_id, "system")
                    report.unbanned.append(user.tg_id)
            for tg_id in part:
                await kv.delete(session, bans.blacklist_mark_key(tg_id))
    stale_flags = sorted(i for i in flag_marks if i not in listed)
    for start in range(0, len(stale_flags), BATCH):
        async with session_factory.begin() as session:
            for tg_id in stale_flags[start : start + BATCH]:
                await kv.delete(session, f"{FLAG_PREFIX}{tg_id}")

    if not settings.blacklist_enabled:
        return report
    for start in range(0, len(known), BATCH):
        part = known[start : start + BATCH]
        async with session_factory.begin() as session:
            paying = await _paying(session, part)
            for user in await session.scalars(select(User).where(User.tg_id.in_(part)).with_for_update()):
                await _apply_user(session, settings, user, paying, cache, report)
    cache._verdicts.clear()
    return report


async def check_user(
    session_factory: SessionFactory, settings: Settings, tg_id: int, cache: BlacklistCache = CACHE
) -> tuple[bool, ApplyReport]:
    """Enforce the policy for one listed user seen in an update: (blocked, what was done). Cached per user."""
    report = ApplyReport()
    cached = cache.cached_verdict(tg_id)
    if cached is not None:
        return cached, report
    async with session_factory.begin() as session:
        user = await session.scalar(select(User).where(User.tg_id == tg_id).with_for_update())
        if user is None:
            return False, report
        verdict = await _apply_user(session, settings, user, await _paying(session, [tg_id]), cache, report)
    blocked = verdict in ("ban", "banned")
    if not blocked:
        # Only "let through" is remembered: a blocked user is dropped by the ban flag itself, and a remembered
        # block would keep dropping the updates of a user an administrator has just unbanned.
        cache.remember_verdict(tg_id, blocked)
    return blocked, report


async def status(session_factory: SessionFactory, settings: Settings, cache: BlacklistCache = CACHE) -> dict[str, Any]:
    """What the admin page shows: sources with counts and errors, totals, last refresh."""
    async with read_session(session_factory) as session, session.begin():
        meta = await _read_meta(session)
        ban_marks = await _markers(session, bans.BLACKLIST_MARK_PREFIX)
        flagged = await _markers(session, FLAG_PREFIX)
    sources = []
    for url in dict.fromkeys([*settings.blacklist_urls, *meta["sources"]]):
        info = meta["sources"].get(url) or {}
        sources.append(
            {
                "url": _safe_url(url),
                "configured": url in settings.blacklist_urls,
                "count": int(info.get("count") or 0),
                "skipped": int(info.get("skipped") or 0),
                "truncated": bool(info.get("truncated")),
                "fetched_at": _when(info.get("fetched_at")),
                "checked_at": _when(info.get("checked_at")),
                "error": info.get("error"),
                "error_at": _when(info.get("error_at")),
            }
        )
    return {
        "enabled": settings.blacklist_enabled,
        "action": settings.blacklist_action,
        "ban_paying": settings.blacklist_ban_paying,
        "refresh_minutes": settings.blacklist_refresh_minutes,
        "refreshed_at": _when(meta.get("refreshed_at")),
        "total_ids": len(cache.merged(settings)),
        "manual_ids": len(cache.manual(settings)),
        "banned_by_list": sum(1 for v in ban_marks.values() if v == bans.MARK_BANNED),
        "exempt": sum(1 for v in ban_marks.values() if v == bans.MARK_EXEMPT),
        "flagged": len(flagged),
        "flagged_ids": sorted(flagged)[:100],
        "sources": sources,
    }


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
