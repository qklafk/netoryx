"""Maintenance mode: full or partial (selected features off), manual or scheduled windows.

The state is one JSON document in ``kv`` (``maintenance_state``); reads are cached for a few seconds because the
bot checks it on every update. Users who hit maintenance are remembered (``kv`` rows ``mhit:<user id>``) so they
can be told when the bot works again.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app.db.models import KeyValue
from app.db.session import SessionFactory, read_session
from app.services import audit, kv
from app.services.users import ru_or_en

log = logging.getLogger(__name__)

STATE_KEY = "maintenance_state"
HIT_PREFIX = "mhit:"
MAX_MESSAGE_LENGTH = 1000
MAX_WINDOWS = 20
DEFAULT_CACHE_TTL_SEC = 10.0


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _clean_message(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if len(text) > MAX_MESSAGE_LENGTH:
        raise ValueError(f"message is longer than {MAX_MESSAGE_LENGTH} characters")
    return text or None


def _pick(lang: str, ru: str | None, en: str | None) -> str | None:
    first, second = (ru, en) if ru_or_en(lang) == "ru" else (en, ru)
    return first or second


@dataclass(slots=True)
class MaintenanceWindow:
    """A planned maintenance period; the bot is closed between ``starts_at`` and ``ends_at``."""

    id: str
    starts_at: datetime
    ends_at: datetime
    announce_minutes: int = 60
    message_ru: str | None = None
    message_en: str | None = None
    announced: bool = False  # the advance notice was sent
    started: bool = False  # the "maintenance started" notice was sent

    def is_active(self, now: datetime) -> bool:
        return self.starts_at <= now < self.ends_at

    def announce_at(self) -> datetime:
        return self.starts_at - timedelta(minutes=self.announce_minutes)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "starts_at": _iso(self.starts_at),
            "ends_at": _iso(self.ends_at),
            "announce_minutes": self.announce_minutes,
            "message_ru": self.message_ru,
            "message_en": self.message_en,
            "announced": self.announced,
            "started": self.started,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MaintenanceWindow:
        starts, ends = _dt(data.get("starts_at")), _dt(data.get("ends_at"))
        if starts is None or ends is None:
            raise ValueError("window without start/end")
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            starts_at=starts,
            ends_at=ends,
            announce_minutes=int(data.get("announce_minutes") or 0),
            message_ru=data.get("message_ru"),
            message_en=data.get("message_en"),
            announced=bool(data.get("announced")),
            started=bool(data.get("started")),
        )


@dataclass(slots=True)
class MaintenanceState:
    enabled: bool = False  # manual full maintenance
    message_ru: str | None = None
    message_en: str | None = None
    eta: datetime | None = None  # expected end of the manual maintenance
    allowlist: list[int] = field(default_factory=list)  # testers who keep full access
    disabled_modules: list[str] = field(default_factory=list)  # partial maintenance: these features are off
    windows: list[MaintenanceWindow] = field(default_factory=list)
    started_at: datetime | None = None
    started_by: int | None = None

    # --- queries ---------------------------------------------------------------------------------
    def active_window(self, now: datetime) -> MaintenanceWindow | None:
        return next((w for w in self.windows if w.is_active(now)), None)

    def is_active(self, now: datetime) -> bool:
        """Full maintenance in effect (manual or a scheduled window)."""
        return self.enabled or self.active_window(now) is not None

    def is_exempt(self, user_id: int | None, *, is_admin: bool) -> bool:
        return is_admin or (user_id is not None and user_id in self.allowlist)

    def blocks(self, user_id: int | None, *, is_admin: bool, now: datetime) -> bool:
        """True if this user must get the maintenance notice instead of the bot."""
        return self.is_active(now) and not self.is_exempt(user_id, is_admin=is_admin)

    def module_blocked(self, module: str, user_id: int | None, *, is_admin: bool) -> bool:
        """True if ``module`` is switched off for this user by a partial maintenance."""
        return module in self.disabled_modules and not self.is_exempt(user_id, is_admin=is_admin)

    def effective_eta(self, now: datetime) -> datetime | None:
        if self.enabled:
            return self.eta
        window = self.active_window(now)
        return window.ends_at if window else None

    def message(self, lang: str, now: datetime) -> str | None:
        """Custom text for the current maintenance (None = use the default localized text)."""
        window = self.active_window(now)
        if self.enabled or window is None:
            return _pick(lang, self.message_ru, self.message_en)
        return _pick(lang, window.message_ru, window.message_en) or _pick(lang, self.message_ru, self.message_en)

    def upcoming(self, now: datetime) -> MaintenanceWindow | None:
        """The next window whose announcement period has begun (for a banner/notice)."""
        pending = [w for w in self.windows if now < w.starts_at and w.announce_at() <= now]
        return min(pending, key=lambda w: w.starts_at) if pending else None

    def epoch(self, now: datetime) -> str | None:
        """Identifies the current maintenance period (hits are de-duplicated per period)."""
        if self.enabled:
            return f"manual:{_iso(self.started_at)}"
        window = self.active_window(now)
        return f"window:{window.id}" if window else None

    # --- serialization ---------------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "message_ru": self.message_ru,
            "message_en": self.message_en,
            "eta": _iso(self.eta),
            "allowlist": self.allowlist,
            "disabled_modules": self.disabled_modules,
            "windows": [w.to_json() for w in self.windows],
            "started_at": _iso(self.started_at),
            "started_by": self.started_by,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MaintenanceState:
        windows: list[MaintenanceWindow] = []
        for item in data.get("windows") or []:
            try:
                windows.append(MaintenanceWindow.from_json(item))
            except (TypeError, ValueError) as exc:
                log.warning("skipping malformed maintenance window: %s", exc)
        return cls(
            enabled=bool(data.get("enabled")),
            message_ru=data.get("message_ru"),
            message_en=data.get("message_en"),
            eta=_dt(data.get("eta")),
            allowlist=[int(x) for x in data.get("allowlist") or []],
            disabled_modules=[str(x) for x in data.get("disabled_modules") or []],
            windows=windows,
            started_at=_dt(data.get("started_at")),
            started_by=data.get("started_by"),
        )

    @classmethod
    def parse(cls, raw: str | None) -> MaintenanceState:
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            log.error("corrupted maintenance state; treating as off")
            return cls()
        return cls.from_json(data) if isinstance(data, dict) else cls()


Mutator = Callable[[MaintenanceState], None]


class MaintenanceService:
    """Cached access to the maintenance state and its admin actions (each audited)."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        cache_ttl: float = DEFAULT_CACHE_TTL_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._ttl = cache_ttl
        self._clock = clock
        self._cached: MaintenanceState | None = None
        self._cached_at = 0.0
        self._hit_epoch: str | None = None
        self._hits_seen: set[int] = set()

    # --- reading ---------------------------------------------------------------------------------
    async def state(self, *, fresh: bool = False) -> MaintenanceState:
        if not fresh and self._cached is not None and self._clock() - self._cached_at < self._ttl:
            return self._cached
        async with read_session(self._session_factory) as session:
            raw = await kv.get(session, STATE_KEY)
        return self._remember(MaintenanceState.parse(raw))

    def cached(self) -> MaintenanceState | None:
        """Last loaded state without I/O (may be a few seconds old)."""
        return self._cached

    def invalidate(self) -> None:
        self._cached = None

    def _remember(self, state: MaintenanceState) -> MaintenanceState:
        self._cached = state
        self._cached_at = self._clock()
        return state

    async def check(self, user_id: int | None, *, is_admin: bool, now: datetime | None = None) -> bool:
        """True if the user is blocked by maintenance right now; remembers them for the "we're back" notice."""
        now = now or datetime.now(UTC)
        state = await self.state()
        if not state.blocks(user_id, is_admin=is_admin, now=now):
            return False
        if user_id is not None:
            await self.record_hit(user_id, state.epoch(now))
        return True

    async def module_blocked(self, module: str, user_id: int | None, *, is_admin: bool) -> bool:
        return (await self.state()).module_blocked(module, user_id, is_admin=is_admin)

    # --- admin actions ---------------------------------------------------------------------------
    async def update(
        self,
        mutate: Mutator,
        *,
        actor_id: int | None,
        action: str,
        source: str = "bot",
        ip: str | None = None,
        **details: Any,
    ) -> MaintenanceState:
        """Read-modify-write the state under a row lock; audited when ``actor_id`` is given."""
        async with self._session_factory.begin() as session:
            await kv.add(session, STATE_KEY, "{}")
            row = await session.scalar(select(KeyValue).where(KeyValue.key == STATE_KEY).with_for_update())
            state = MaintenanceState.parse(row.value if row else None)
            mutate(state)
            await kv.set(session, STATE_KEY, json.dumps(state.to_json(), ensure_ascii=False))
            # Keep the plain flag in sync for code that only checks on/off.
            await kv.set_maintenance(session, state.enabled)
            if actor_id is not None:
                await audit.log(session, actor_id, action, "maintenance", None, source=source, ip=ip, **details)
        return self._remember(state)

    async def enable(
        self,
        actor_id: int,
        *,
        message_ru: str | None = None,
        message_en: str | None = None,
        eta: datetime | None = None,
        source: str = "bot",
        ip: str | None = None,
    ) -> MaintenanceState:
        message_ru, message_en = _clean_message(message_ru), _clean_message(message_en)
        eta = _aware(eta, "eta") if eta else None

        def mutate(state: MaintenanceState) -> None:
            if not state.enabled:
                state.started_at = datetime.now(UTC)
                state.started_by = actor_id
            state.enabled = True
            state.message_ru, state.message_en, state.eta = message_ru, message_en, eta

        log.warning("maintenance mode enabled by %s", actor_id)
        return await self.update(
            mutate, actor_id=actor_id, action="maintenance.enable", source=source, ip=ip, eta=_iso(eta)
        )

    async def disable(self, actor_id: int, *, source: str = "bot", ip: str | None = None) -> MaintenanceState:
        def mutate(state: MaintenanceState) -> None:
            state.enabled = False
            state.eta = None
            state.started_at = None
            state.started_by = None

        log.warning("maintenance mode disabled by %s", actor_id)
        return await self.update(mutate, actor_id=actor_id, action="maintenance.disable", source=source, ip=ip)

    async def set_message(
        self,
        actor_id: int,
        *,
        message_ru: str | None,
        message_en: str | None,
        eta: datetime | None = None,
        source: str = "bot",
        ip: str | None = None,
    ) -> MaintenanceState:
        message_ru, message_en = _clean_message(message_ru), _clean_message(message_en)
        eta = _aware(eta, "eta") if eta else None

        def mutate(state: MaintenanceState) -> None:
            state.message_ru, state.message_en, state.eta = message_ru, message_en, eta

        return await self.update(mutate, actor_id=actor_id, action="maintenance.message", source=source, ip=ip)

    async def set_allowlist(
        self, actor_id: int, user_ids: list[int], *, source: str = "bot", ip: str | None = None
    ) -> MaintenanceState:
        ids = sorted({int(x) for x in user_ids})

        def mutate(state: MaintenanceState) -> None:
            state.allowlist = ids

        return await self.update(
            mutate, actor_id=actor_id, action="maintenance.allowlist", source=source, ip=ip, user_ids=ids
        )

    async def set_disabled_modules(
        self, actor_id: int, modules: list[str], *, source: str = "bot", ip: str | None = None
    ) -> MaintenanceState:
        names = sorted({m.strip() for m in modules if m.strip()})

        def mutate(state: MaintenanceState) -> None:
            state.disabled_modules = names

        return await self.update(
            mutate, actor_id=actor_id, action="maintenance.modules", source=source, ip=ip, modules=names
        )

    async def schedule(
        self,
        actor_id: int,
        starts_at: datetime,
        ends_at: datetime,
        *,
        announce_minutes: int = 60,
        message_ru: str | None = None,
        message_en: str | None = None,
        source: str = "bot",
        ip: str | None = None,
    ) -> MaintenanceWindow:
        starts_at, ends_at = _aware(starts_at, "starts_at"), _aware(ends_at, "ends_at")
        if ends_at <= starts_at:
            raise ValueError("maintenance window must end after it starts")
        if ends_at <= datetime.now(UTC):
            raise ValueError("maintenance window is already over")
        if announce_minutes < 0:
            raise ValueError("announce_minutes must not be negative")
        window = MaintenanceWindow(
            id=uuid.uuid4().hex[:8],
            starts_at=starts_at,
            ends_at=ends_at,
            announce_minutes=announce_minutes,
            message_ru=_clean_message(message_ru),
            message_en=_clean_message(message_en),
        )

        def mutate(state: MaintenanceState) -> None:
            if len(state.windows) >= MAX_WINDOWS:
                raise ValueError(f"at most {MAX_WINDOWS} maintenance windows can be planned")
            state.windows.append(window)
            state.windows.sort(key=lambda w: w.starts_at)

        await self.update(
            mutate,
            actor_id=actor_id,
            action="maintenance.schedule",
            source=source,
            ip=ip,
            window_id=window.id,
            starts_at=_iso(starts_at),
            ends_at=_iso(ends_at),
        )
        return window

    async def cancel_window(self, actor_id: int, window_id: str, *, source: str = "bot", ip: str | None = None) -> bool:
        removed: list[MaintenanceWindow] = []

        def mutate(state: MaintenanceState) -> None:
            removed.extend(w for w in state.windows if w.id == window_id)
            state.windows = [w for w in state.windows if w.id != window_id]

        await self.update(
            mutate, actor_id=actor_id, action="maintenance.cancel_window", source=source, ip=ip, window_id=window_id
        )
        return bool(removed)

    async def mark_window(self, window_id: str, *, announced: bool | None = None, started: bool | None = None) -> None:
        """Record that a window's notices were sent (no audit: done by the scheduler)."""

        def mutate(state: MaintenanceState) -> None:
            for window in state.windows:
                if window.id == window_id:
                    if announced is not None:
                        window.announced = announced
                    if started is not None:
                        window.started = started

        await self.update(mutate, actor_id=None, action="maintenance.mark_window")

    async def prune_windows(self, now: datetime | None = None) -> list[MaintenanceWindow]:
        """Drop finished windows; returns them (the caller announces the end of those that had started)."""
        now = now or datetime.now(UTC)
        state = await self.state(fresh=True)
        if not any(w.ends_at <= now for w in state.windows):
            return []
        finished: list[MaintenanceWindow] = []

        def mutate(current: MaintenanceState) -> None:
            finished.extend(w for w in current.windows if w.ends_at <= now)
            current.windows = [w for w in current.windows if w.ends_at > now]

        await self.update(mutate, actor_id=None, action="maintenance.prune")
        return finished

    # --- users who hit maintenance ---------------------------------------------------------------
    async def record_hit(self, user_id: int, epoch: str | None = None) -> None:
        """Remember a blocked user (once per maintenance period and process)."""
        if epoch != self._hit_epoch:
            self._hit_epoch = epoch
            self._hits_seen.clear()
        if user_id in self._hits_seen:
            return
        self._hits_seen.add(user_id)
        try:
            async with self._session_factory.begin() as session:
                await kv.add(session, f"{HIT_PREFIX}{user_id}", "1")
        except Exception:  # best effort: never break the update handling
            self._hits_seen.discard(user_id)
            log.exception("failed to record maintenance hit for %s", user_id)

    async def pending_hits(self) -> int:
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(select(KeyValue.key).where(KeyValue.key.like(f"{HIT_PREFIX}%")))
            return len(list(rows))

    async def pop_hits(self, limit: int = 1000) -> list[int]:
        """Take up to ``limit`` remembered users (removing them); call repeatedly until empty."""
        async with self._session_factory.begin() as session:
            keys = list(
                await session.scalars(
                    select(KeyValue.key).where(KeyValue.key.like(f"{HIT_PREFIX}%")).order_by(KeyValue.key).limit(limit)
                )
            )
            if keys:
                await session.execute(
                    delete(KeyValue).where(KeyValue.key.in_(keys)).execution_options(synchronize_session=False)
                )
        self._hits_seen.clear()
        ids: list[int] = []
        for key in keys:
            try:
                ids.append(int(key[len(HIT_PREFIX) :]))
            except ValueError:
                continue
        return ids
