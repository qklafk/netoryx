"""Broadcasts: drafts, audience segments, scheduling and a resumable, rate-limited sender.

A broadcast is either a copy of a message composed in the bot (``source_chat_id`` + ``source_message_id``) or an
HTML text with optional media and URL buttons (web admin). Its audience segment, schedule and media are stored next
to the row in ``kv`` (``broadcast:<id>``, see :class:`BroadcastOptions`).

Lifecycle: DRAFT -> RUNNING -> DONE, or CANCELED from DRAFT/RUNNING. :func:`run` walks the audience in ``tg_id``
order and persists counters and the cursor (``last_user_id``) every few messages, so a restart resumes where it
stopped (at most :data:`PERSIST_EVERY` users may get the message twice after a crash). Cancelling only flips the
status; the runner notices it at its next checkpoint. Banned users and users who blocked the bot are never
included; a user found to have blocked the bot is marked ``bot_blocked``.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urlsplit

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from sqlalchemy import CursorResult, Select, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Broadcast,
    BroadcastStatus,
    Inbound,
    Payment,
    PaymentStatus,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import read_session
from app.services import audit, kv
from app.utils.time import utcnow

if TYPE_CHECKING:
    from aiogram import Bot

    from app.db.session import SessionFactory

log = logging.getLogger(__name__)

DEFAULT_RATE_PER_SEC = 25.0
BATCH_SIZE = 200
PERSIST_EVERY = 25
MAX_RETRY_AFTER_ATTEMPTS = 3
TRANSIENT_RETRIES = 3  # network errors / Bot API 5xx: retries with a growing pause before giving up for now
TRANSIENT_BACKOFF_SEC = 1.0
MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
MAX_BUTTON_ROWS = 10
MAX_BUTTONS_PER_ROW = 8
MAX_BUTTON_TEXT = 64
MAX_CUSTOM_IDS = 10_000
MAX_EXPIRING_DAYS = 365
_OPTIONS_KEY = "broadcast:{id}"
_URL_SCHEMES = ("http", "https", "tg")

# Broadcasts being sent by this process (the scheduler and an admin "send now" must not run one twice).
_IN_PROGRESS: set[int] = set()


class Audience(enum.StrEnum):
    ALL = "all"
    ACTIVE = "active"  # subscription active now (trials included)
    EXPIRED = "expired"  # had a subscription, it ran out
    TRIAL = "trial"  # active trial
    NEVER_PAID = "never_paid"  # no credited payment (donations do not count)
    NO_SUBSCRIPTION = "no_subscription"  # never had a subscription
    EXPIRING = "expiring_in_days"  # active and ending within ``days``
    LANGUAGE = "language"
    PLAN = "plan"
    PANEL = "panel"  # has an account on the panel
    LOCATION = "location"  # has an account on an inbound of the location
    CUSTOM = "custom"  # explicit list of user ids


class MediaType(enum.StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    ANIMATION = "animation"
    DOCUMENT = "document"


class BroadcastErrorCode(enum.StrEnum):
    BAD_AUDIENCE = "bad_audience"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    BAD_BUTTONS = "bad_buttons"
    BAD_MEDIA = "bad_media"
    BAD_SCHEDULE = "bad_schedule"
    NOT_FOUND = "not_found"
    BAD_STATUS = "bad_status"


class BroadcastError(Exception):
    def __init__(self, code: BroadcastErrorCode, message: str | None = None) -> None:
        super().__init__(message or code.value)
        self.code = code


class Delivery(enum.StrEnum):
    SENT = "sent"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"  # Telegram unreachable or failing (network, 5xx): worth trying again later


class Interrupted(Exception):
    """Raised by a progress callback to stop sending after the current checkpoint (the broadcast stays RUNNING)."""


# ---------------------------------------------------------------------------------------------
# Audience
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AudienceSpec:
    kind: Audience = Audience.ALL
    days: int | None = None  # EXPIRING
    language: str | None = None  # LANGUAGE
    plan_id: int | None = None  # PLAN
    panel_id: int | None = None  # PANEL
    location_id: int | None = None  # LOCATION
    user_ids: tuple[int, ...] = ()  # CUSTOM

    def validate(self) -> None:
        """Raise :class:`BroadcastError` (BAD_AUDIENCE) if a required parameter is missing or out of range."""
        problems = {
            Audience.EXPIRING: self.days is None or not 0 < self.days <= MAX_EXPIRING_DAYS,
            Audience.LANGUAGE: not self.language,
            Audience.PLAN: self.plan_id is None,
            Audience.PANEL: self.panel_id is None,
            Audience.LOCATION: self.location_id is None,
            Audience.CUSTOM: not self.user_ids or len(self.user_ids) > MAX_CUSTOM_IDS,
        }
        if problems.get(self.kind, False):
            raise BroadcastError(BroadcastErrorCode.BAD_AUDIENCE, f"bad parameters for audience {self.kind}")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind.value}
        for key in ("days", "language", "plan_id", "panel_id", "location_id"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.user_ids:
            data["user_ids"] = list(self.user_ids)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AudienceSpec:
        try:
            return cls(
                kind=Audience(data.get("kind", Audience.ALL)),
                days=_opt_int(data.get("days")),
                language=data.get("language") or None,
                plan_id=_opt_int(data.get("plan_id")),
                panel_id=_opt_int(data.get("panel_id")),
                location_id=_opt_int(data.get("location_id")),
                user_ids=tuple(int(x) for x in data.get("user_ids") or ()),
            )
        except (TypeError, ValueError) as exc:
            raise BroadcastError(BroadcastErrorCode.BAD_AUDIENCE, str(exc)) from exc


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    return int(value)


def parse_user_ids(text: str) -> tuple[int, ...]:
    """User ids from free text ("1, 2 3\\n4"); invalid tokens are ignored, duplicates removed (order kept)."""
    seen: dict[int, None] = {}
    for token in text.replace(",", " ").replace(";", " ").split():
        if token.lstrip("-").isdigit():
            seen.setdefault(int(token), None)
    return tuple(seen)


def _sub_exists(*where: Any) -> Any:
    return select(Subscription.id).where(Subscription.user_id == User.tg_id, *where).exists()


def _provision_exists(*where: Any) -> Any:
    return (
        select(Provision.id)
        .join(Subscription, Subscription.id == Provision.subscription_id)
        .where(Subscription.user_id == User.tg_id, Provision.desired_state != ProvisionState.ABSENT, *where)
        .exists()
    )


def audience_filter(spec: AudienceSpec, now: datetime) -> Any:
    """SQL condition on ``User`` selecting the segment (without the blocked/banned exclusion)."""
    active = and_(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at > now)
    match spec.kind:
        case Audience.ALL:
            return True
        case Audience.ACTIVE:
            return _sub_exists(active)
        case Audience.TRIAL:
            return _sub_exists(active, Subscription.is_trial.is_(True))
        case Audience.EXPIRED:
            return _sub_exists(
                or_(
                    Subscription.status == SubscriptionStatus.EXPIRED,
                    and_(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at <= now),
                )
            )
        case Audience.NO_SUBSCRIPTION:
            return ~_sub_exists()
        case Audience.NEVER_PAID:
            paid = (
                select(Payment.id)
                .where(
                    Payment.user_id == User.tg_id,
                    Payment.status == PaymentStatus.SUCCEEDED,
                    Payment.credited_at.is_not(None),
                    Payment.is_donation.is_(False),
                )
                .exists()
            )
            return ~paid
        case Audience.EXPIRING:
            return _sub_exists(active, Subscription.expires_at <= now + timedelta(days=spec.days or 0))
        case Audience.LANGUAGE:
            return User.language == spec.language
        case Audience.PLAN:
            return _sub_exists(Subscription.plan_id == spec.plan_id)
        case Audience.PANEL:
            return _provision_exists(Provision.panel_id == spec.panel_id)
        case Audience.LOCATION:
            on_location = select(Inbound.id).where(Inbound.location_id == spec.location_id)
            return _provision_exists(Provision.inbound_id.in_(on_location))
        case Audience.CUSTOM:
            return User.tg_id.in_(spec.user_ids)
    raise BroadcastError(BroadcastErrorCode.BAD_AUDIENCE)  # pragma: no cover - exhaustive match


def audience_query(spec: AudienceSpec, now: datetime) -> Select[int]:
    """Recipients' ids (banned users and users who blocked the bot excluded), unordered."""
    spec.validate()
    return select(User.tg_id).where(User.is_banned.is_(False), User.bot_blocked.is_(False), audience_filter(spec, now))


async def count_audience(session: AsyncSession, spec: AudienceSpec, now: datetime | None = None) -> int:
    sub = audience_query(spec, now or utcnow()).subquery()
    return int(await session.scalar(select(func.count()).select_from(sub)) or 0)


# ---------------------------------------------------------------------------------------------
# Content & options
# ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class BroadcastOptions:
    """Everything about a broadcast that is not a column of ``broadcasts`` (stored as JSON in kv)."""

    audience: AudienceSpec = field(default_factory=AudienceSpec)
    scheduled_at: datetime | None = None
    media_type: MediaType | None = None
    media: str | None = None  # Telegram file_id or HTTPS URL
    disable_preview: bool = True

    def to_json(self) -> str:
        return json.dumps(
            {
                "audience": self.audience.to_dict(),
                "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
                "media_type": self.media_type.value if self.media_type else None,
                "media": self.media,
                "disable_preview": self.disable_preview,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str | None, fallback_kind: str | None = None) -> BroadcastOptions:
        data: dict[str, Any] = {}
        if raw:
            try:
                loaded = json.loads(raw)
                data = loaded if isinstance(loaded, dict) else {}
            except ValueError:
                log.warning("broadcast options are not valid JSON; using defaults")
        audience_data = data.get("audience") or {"kind": fallback_kind or Audience.ALL}
        scheduled = data.get("scheduled_at")
        media_type = data.get("media_type")
        return cls(
            audience=AudienceSpec.from_dict(audience_data),
            scheduled_at=datetime.fromisoformat(scheduled) if scheduled else None,
            media_type=MediaType(media_type) if media_type else None,
            media=data.get("media") or None,
            disable_preview=bool(data.get("disable_preview", True)),
        )


def _options_key(broadcast_id: int) -> str:
    return _OPTIONS_KEY.format(id=broadcast_id)


async def get_options(session: AsyncSession, broadcast: Broadcast) -> BroadcastOptions:
    return BroadcastOptions.from_json(await kv.get(session, _options_key(broadcast.id)), broadcast.audience)


async def _save_options(session: AsyncSession, broadcast_id: int, options: BroadcastOptions) -> None:
    await kv.set(session, _options_key(broadcast_id), options.to_json())


def validate_buttons(buttons: Sequence[Sequence[dict[str, str]]] | None) -> list[list[dict[str, str]]]:
    """Normalize URL buttons ``[[{"text", "url"}]]``; raises BAD_BUTTONS."""
    if not buttons:
        return []
    if len(buttons) > MAX_BUTTON_ROWS:
        raise BroadcastError(BroadcastErrorCode.BAD_BUTTONS, "too many rows")
    rows: list[list[dict[str, str]]] = []
    for row in buttons:
        if not row or len(row) > MAX_BUTTONS_PER_ROW:
            raise BroadcastError(BroadcastErrorCode.BAD_BUTTONS, "bad row size")
        clean_row = []
        for button in row:
            text = str(button.get("text", "")).strip()
            url = str(button.get("url", "")).strip()
            if not 0 < len(text) <= MAX_BUTTON_TEXT or urlsplit(url).scheme not in _URL_SCHEMES:
                raise BroadcastError(BroadcastErrorCode.BAD_BUTTONS, f"bad button {text!r}")
            clean_row.append({"text": text, "url": url})
        rows.append(clean_row)
    return rows


def parse_buttons(text: str) -> list[list[dict[str, str]]]:
    """Buttons from admin input: one row per line, buttons separated by ``|``, each ``Title - https://url``."""
    rows: list[list[dict[str, str]]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        row = []
        for part in line.split("|"):
            title, sep, url = part.rpartition(" - ")
            if not sep:
                raise BroadcastError(BroadcastErrorCode.BAD_BUTTONS, f"expected 'Title - URL': {part.strip()!r}")
            row.append({"text": title.strip(), "url": url.strip()})
        rows.append(row)
    return validate_buttons(rows)


def keyboard(buttons_json: str | None) -> InlineKeyboardMarkup | None:
    if not buttons_json:
        return None
    try:
        rows = validate_buttons(json.loads(buttons_json))
    except (ValueError, BroadcastError):
        log.warning("broadcast buttons are invalid; sending without them")
        return None
    if not rows:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=b["text"], url=b["url"]) for b in row] for row in rows]
    )


def _validate_content(
    text: str | None,
    source: tuple[int | None, int | None],
    media_type: MediaType | None,
    media: str | None,
) -> None:
    has_copy = source[0] is not None and source[1] is not None
    if (media_type is None) != (media is None):
        raise BroadcastError(BroadcastErrorCode.BAD_MEDIA, "media type and media go together")
    if media is not None and has_copy:
        raise BroadcastError(BroadcastErrorCode.BAD_MEDIA, "a copied message cannot have extra media")
    if not has_copy and not (text and text.strip()) and media is None:
        raise BroadcastError(BroadcastErrorCode.EMPTY)
    limit = MAX_CAPTION_LENGTH if media is not None else MAX_TEXT_LENGTH
    if text and len(text) > limit:
        raise BroadcastError(BroadcastErrorCode.TOO_LONG)


# ---------------------------------------------------------------------------------------------
# Drafts & lifecycle
# ---------------------------------------------------------------------------------------------
async def create_draft(
    session: AsyncSession,
    created_by: int,
    audience: AudienceSpec,
    *,
    text: str | None = None,
    buttons: Sequence[Sequence[dict[str, str]]] | None = None,
    source_chat_id: int | None = None,
    source_message_id: int | None = None,
    media_type: MediaType | None = None,
    media: str | None = None,
    disable_preview: bool = True,
    source: str = "bot",
    ip: str | None = None,
) -> Broadcast:
    """Create a DRAFT. Content: a message to copy, or HTML ``text`` (caption with ``media``) + URL buttons."""
    audience.validate()
    _validate_content(text, (source_chat_id, source_message_id), media_type, media)
    rows = validate_buttons(buttons)
    broadcast = Broadcast(
        created_by=created_by,
        audience=audience.kind.value,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        text=text,
        buttons=json.dumps(rows, ensure_ascii=False) if rows else None,
        status=BroadcastStatus.DRAFT,
    )
    session.add(broadcast)
    await session.flush()
    options = BroadcastOptions(audience, None, media_type, media, disable_preview)
    await _save_options(session, broadcast.id, options)
    await audit.log(
        session, created_by, "broadcast.create", "broadcast", broadcast.id, source, ip, audience=audience.kind
    )
    return broadcast


async def _get(session: AsyncSession, broadcast_id: int, *, lock: bool = False) -> Broadcast:
    stmt = select(Broadcast).where(Broadcast.id == broadcast_id).execution_options(populate_existing=True)
    broadcast = await session.scalar(stmt.with_for_update() if lock else stmt)
    if broadcast is None:
        raise BroadcastError(BroadcastErrorCode.NOT_FOUND)
    return broadcast


async def schedule(
    session: AsyncSession,
    broadcast_id: int,
    when: datetime | None,
    actor_id: int,
    *,
    now: datetime | None = None,
    source: str = "bot",
    ip: str | None = None,
) -> Broadcast:
    """Set (or clear with ``None``) the time a DRAFT starts automatically (see :func:`run_due`)."""
    broadcast = await _get(session, broadcast_id, lock=True)
    if broadcast.status != BroadcastStatus.DRAFT:
        raise BroadcastError(BroadcastErrorCode.BAD_STATUS)
    if when is not None and (when.tzinfo is None or when <= (now or utcnow())):
        raise BroadcastError(BroadcastErrorCode.BAD_SCHEDULE, "the time must be aware and in the future")
    options = await get_options(session, broadcast)
    options.scheduled_at = when
    await _save_options(session, broadcast.id, options)
    scheduled_at = when.isoformat() if when else None
    await audit.log(
        session, actor_id, "broadcast.schedule", "broadcast", broadcast.id, source, ip, scheduled_at=scheduled_at
    )
    return broadcast


async def start(
    session: AsyncSession,
    broadcast_id: int,
    actor_id: int | None = None,
    *,
    now: datetime | None = None,
    source: str = "bot",
    ip: str | None = None,
) -> Broadcast:
    """DRAFT -> RUNNING: count the audience and mark it for the runner. RUNNING is returned unchanged."""
    broadcast = await _get(session, broadcast_id, lock=True)
    if broadcast.status == BroadcastStatus.RUNNING:
        return broadcast
    if broadcast.status != BroadcastStatus.DRAFT:
        raise BroadcastError(BroadcastErrorCode.BAD_STATUS)
    now = now or utcnow()
    options = await get_options(session, broadcast)
    broadcast.total = await count_audience(session, options.audience, now)
    broadcast.status = BroadcastStatus.RUNNING
    broadcast.started_at = now
    await session.flush()
    if actor_id is not None:
        await audit.log(
            session, actor_id, "broadcast.start", "broadcast", broadcast.id, source, ip, total=broadcast.total
        )
    return broadcast


async def cancel(
    session: AsyncSession, broadcast_id: int, actor_id: int, *, source: str = "bot", ip: str | None = None
) -> bool:
    """Cancel a DRAFT or RUNNING broadcast; False if it already finished. The runner stops at its next checkpoint."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Broadcast)
            .where(
                Broadcast.id == broadcast_id,
                Broadcast.status.in_((BroadcastStatus.DRAFT, BroadcastStatus.RUNNING)),
            )
            .values(status=BroadcastStatus.CANCELED, finished_at=utcnow())
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        return False
    await audit.log(session, actor_id, "broadcast.cancel", "broadcast", broadcast_id, source, ip)
    return True


async def history(session: AsyncSession, *, limit: int = 20, before_id: int | None = None) -> list[Broadcast]:
    stmt = select(Broadcast).order_by(Broadcast.id.desc()).limit(max(1, min(limit, 200)))
    if before_id is not None:
        stmt = stmt.where(Broadcast.id < before_id)
    return list((await session.scalars(stmt)).all())


async def due_ids(session: AsyncSession, now: datetime) -> list[int]:
    """RUNNING broadcasts (to resume) and DRAFTs whose scheduled time has come, oldest first."""
    running = list(
        (
            await session.scalars(
                select(Broadcast.id).where(Broadcast.status == BroadcastStatus.RUNNING).order_by(Broadcast.id)
            )
        ).all()
    )
    drafts = list((await session.scalars(select(Broadcast).where(Broadcast.status == BroadcastStatus.DRAFT))).all())
    scheduled = []
    for draft in drafts:
        options = await get_options(session, draft)
        if options.scheduled_at is not None and options.scheduled_at <= now:
            scheduled.append(draft.id)
    return running + sorted(scheduled)


# ---------------------------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Content:
    """What to send to every recipient."""

    text: str | None
    keyboard: InlineKeyboardMarkup | None
    source_chat_id: int | None = None
    source_message_id: int | None = None
    media_type: MediaType | None = None
    media: str | None = None
    disable_preview: bool = True

    @classmethod
    def of(cls, broadcast: Broadcast, options: BroadcastOptions) -> Content:
        return cls(
            text=broadcast.text,
            keyboard=keyboard(broadcast.buttons),
            source_chat_id=broadcast.source_chat_id,
            source_message_id=broadcast.source_message_id,
            media_type=options.media_type,
            media=options.media,
            disable_preview=options.disable_preview,
        )


async def _send_once(bot: Bot, chat_id: int, content: Content) -> None:
    markup = content.keyboard
    if content.source_chat_id is not None and content.source_message_id is not None:
        await bot.copy_message(chat_id, content.source_chat_id, content.source_message_id, reply_markup=markup)
        return
    if content.media is not None:
        caption = content.text or None
        match content.media_type:
            case MediaType.PHOTO:
                await bot.send_photo(chat_id, content.media, caption=caption, parse_mode="HTML", reply_markup=markup)
            case MediaType.VIDEO:
                await bot.send_video(chat_id, content.media, caption=caption, parse_mode="HTML", reply_markup=markup)
            case MediaType.ANIMATION:
                await bot.send_animation(
                    chat_id, content.media, caption=caption, parse_mode="HTML", reply_markup=markup
                )
            case _:
                await bot.send_document(chat_id, content.media, caption=caption, parse_mode="HTML", reply_markup=markup)
        return
    await bot.send_message(
        chat_id,
        content.text or "",
        parse_mode="HTML",
        reply_markup=markup,
        link_preview_options=LinkPreviewOptions(is_disabled=content.disable_preview),
    )


async def deliver(bot: Bot, chat_id: int, content: Content) -> Delivery:
    """Send to one chat, never raising. Waits on flood limits (RetryAfter) and retries a few times.

    Network errors and Bot API 5xx are retried with a growing pause; if they persist the result is
    ``UNAVAILABLE`` (not the user's fault: the runner pauses instead of skipping them).
    """
    flood_waits = transient = 0
    while True:
        try:
            await _send_once(bot, chat_id, content)
            return Delivery.SENT
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)
            flood_waits += 1
            if flood_waits >= MAX_RETRY_AFTER_ATTEMPTS:
                return Delivery.FAILED
        except (TelegramNetworkError, TelegramServerError) as exc:
            if transient >= TRANSIENT_RETRIES:
                log.warning("broadcast to %s: Telegram is unavailable: %s", chat_id, exc)
                return Delivery.UNAVAILABLE
            await asyncio.sleep(TRANSIENT_BACKOFF_SEC * 2**transient)
            transient += 1
        except (TelegramForbiddenError, TelegramNotFound):
            return Delivery.BLOCKED
        except TelegramBadRequest as exc:
            if "chat not found" in str(exc).lower() or "user is deactivated" in str(exc).lower():
                return Delivery.BLOCKED
            log.warning("broadcast to %s failed: %s", chat_id, exc)
            return Delivery.FAILED
        except Exception:
            log.exception("broadcast to %s failed", chat_id)
            return Delivery.FAILED


async def send_preview(session_factory: SessionFactory, bot: Bot, broadcast_id: int, chat_id: int) -> Delivery:
    """Send the broadcast to one chat (the admin) to check how it looks."""
    async with read_session(session_factory) as session:
        broadcast = await _get(session, broadcast_id)
        content = Content.of(broadcast, await get_options(session, broadcast))
    return await deliver(bot, chat_id, content)


@dataclass(slots=True)
class Progress:
    broadcast_id: int
    status: BroadcastStatus
    total: int
    sent: int
    failed: int
    blocked: int

    @property
    def processed(self) -> int:
        return self.sent + self.failed + self.blocked


ProgressCallback = Callable[[Progress], Awaitable[None]]


class RunnerContext(Protocol):
    """What :func:`run` needs from the application context."""

    @property
    def bot(self) -> Bot: ...

    @property
    def session_factory(self) -> SessionFactory: ...


class _Pacer:
    """Spaces sends evenly at ``rate`` per second."""

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate if rate > 0 else 0.0
        self._next = 0.0

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        loop = asyncio.get_running_loop()
        delay = self._next - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next = max(self._next, loop.time()) + self._interval


async def _checkpoint(
    session_factory: SessionFactory, progress: Progress, cursor: int | None, blocked_ids: list[int]
) -> bool:
    """Persist counters + cursor and mark blocked users. False if the broadcast is no longer RUNNING."""
    async with session_factory.begin() as session:
        if blocked_ids:
            await session.execute(
                update(User)
                .where(User.tg_id.in_(blocked_ids))
                .values(bot_blocked=True)
                .execution_options(synchronize_session=False)
            )
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(Broadcast)
                .where(Broadcast.id == progress.broadcast_id, Broadcast.status == BroadcastStatus.RUNNING)
                .values(
                    sent=progress.sent,
                    failed=progress.failed,
                    blocked=progress.blocked,
                    last_user_id=cursor,
                    total=max(progress.total, progress.processed),
                )
                .execution_options(synchronize_session=False)
            ),
        )
    blocked_ids.clear()
    return result.rowcount == 1


async def _report(callback: ProgressCallback | None, progress: Progress) -> None:
    """Call the progress callback; its errors are logged, except :class:`Interrupted`, which propagates."""
    if callback is None:
        return
    try:
        await callback(progress)
    except Interrupted:
        raise
    except Exception:
        log.exception("broadcast progress callback failed")


async def run(
    ctx: RunnerContext,
    broadcast_id: int,
    *,
    rate_per_sec: float = DEFAULT_RATE_PER_SEC,
    on_progress: ProgressCallback | None = None,
    now: datetime | None = None,
) -> Progress | None:
    """Send (or resume sending) a broadcast; returns the final progress, or None if it is not runnable.

    A DRAFT is started first. No DB transaction is open while messages are sent. Returns early (None) when another
    task of this process is already sending it. The result is still RUNNING when sending paused at a checkpoint
    (Telegram unavailable, or ``on_progress`` raised :class:`Interrupted`); the next run resumes from there.
    """
    if broadcast_id in _IN_PROGRESS:
        return None
    _IN_PROGRESS.add(broadcast_id)
    try:
        return await _run(ctx.session_factory, ctx.bot, broadcast_id, rate_per_sec, on_progress, now or utcnow())
    finally:
        _IN_PROGRESS.discard(broadcast_id)


async def _run(
    session_factory: SessionFactory,
    bot: Bot,
    broadcast_id: int,
    rate: float,
    on_progress: ProgressCallback | None,
    now: datetime,
) -> Progress | None:
    async with session_factory.begin() as session:
        try:
            broadcast = await _get(session, broadcast_id, lock=True)
        except BroadcastError:
            return None
        if broadcast.status not in (BroadcastStatus.DRAFT, BroadcastStatus.RUNNING):
            return None
        broadcast = await start(session, broadcast_id, now=now)
        options = await get_options(session, broadcast)
        content = Content.of(broadcast, options)
        progress = Progress(
            broadcast.id, broadcast.status, broadcast.total, broadcast.sent, broadcast.failed, broadcast.blocked
        )
        cursor = broadcast.last_user_id
    query = audience_query(options.audience, now).order_by(User.tg_id)
    log.info("broadcast %s: sending to ~%d users (resuming after %s)", broadcast_id, progress.total, cursor)
    try:
        return await _send_all(session_factory, bot, query, content, progress, cursor, rate, on_progress)
    except Interrupted as exc:
        log.info("broadcast %s paused after %d messages: %s", broadcast_id, progress.processed, exc)
        return progress


async def _send_all(
    session_factory: SessionFactory,
    bot: Bot,
    query: Select[int],
    content: Content,
    progress: Progress,
    cursor: int | None,
    rate: float,
    on_progress: ProgressCallback | None,
) -> Progress:
    broadcast_id = progress.broadcast_id
    pacer = _Pacer(rate)
    blocked_ids: list[int] = []
    since_checkpoint = 0
    while True:
        async with read_session(session_factory) as session:
            stmt = query if cursor is None else query.where(User.tg_id > cursor)
            batch = list((await session.scalars(stmt.limit(BATCH_SIZE))).all())
        if not batch:
            break
        for user_id in batch:
            await pacer.wait()
            outcome = await deliver(bot, user_id, content)
            if outcome == Delivery.UNAVAILABLE:
                # Keep the cursor before this user; the scheduler's next run resumes from here.
                if not await _checkpoint(session_factory, progress, cursor, blocked_ids):
                    progress.status = BroadcastStatus.CANCELED
                    return progress
                await _report(on_progress, progress)
                raise Interrupted("Telegram is unavailable")
            if outcome == Delivery.SENT:
                progress.sent += 1
            elif outcome == Delivery.BLOCKED:
                progress.blocked += 1
                blocked_ids.append(user_id)
            else:
                progress.failed += 1
            cursor = user_id
            since_checkpoint += 1
            if since_checkpoint >= PERSIST_EVERY:
                since_checkpoint = 0
                if not await _checkpoint(session_factory, progress, cursor, blocked_ids):
                    progress.status = BroadcastStatus.CANCELED
                    await _report(on_progress, progress)
                    log.info("broadcast %s canceled after %d messages", broadcast_id, progress.processed)
                    return progress
                await _report(on_progress, progress)
    if not await _checkpoint(session_factory, progress, cursor, blocked_ids):
        progress.status = BroadcastStatus.CANCELED
        return progress
    async with session_factory.begin() as session:
        await session.execute(
            update(Broadcast)
            .where(Broadcast.id == broadcast_id, Broadcast.status == BroadcastStatus.RUNNING)
            .values(status=BroadcastStatus.DONE, finished_at=utcnow(), total=progress.processed)
            .execution_options(synchronize_session=False)
        )
    progress.status = BroadcastStatus.DONE
    progress.total = progress.processed
    with contextlib.suppress(Interrupted):  # already finished: nothing left to pause
        await _report(on_progress, progress)
    log.info(
        "broadcast %s done: sent=%d failed=%d blocked=%d",
        broadcast_id,
        progress.sent,
        progress.failed,
        progress.blocked,
    )
    return progress


async def run_due(
    ctx: RunnerContext, *, rate_per_sec: float = DEFAULT_RATE_PER_SEC, now: datetime | None = None
) -> list[Progress]:
    """Scheduler job: resume RUNNING broadcasts and start scheduled ones whose time has come (one at a time)."""
    now = now or utcnow()
    async with read_session(ctx.session_factory) as session:
        ids = await due_ids(session, now)
    results = []
    for broadcast_id in ids:
        progress = await run(ctx, broadcast_id, rate_per_sec=rate_per_sec, now=now)
        if progress is not None:
            results.append(progress)
    return results
