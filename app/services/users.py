"""Users: registration, lookup, profile refresh and the personal subscription token."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TxKind, User
from app.services import events, wallet
from app.utils.security import random_code, random_token
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)

# Bot languages, in picker order. Russian and English are complete; missing texts of the others fall back to a
# related language (see ``app.core.i18n.FALLBACKS``).
LANGUAGES = ("ru", "en", "uk", "kk", "uz", "fa", "zh")
# Telegram ``language_code`` aliases (BCP 47) that map to a supported language.
_LANGUAGE_ALIASES = {"ua": "uk", "kz": "kk", "pes": "fa", "prs": "fa", "cmn": "zh"}
# The related language a translation borrows missing texts from (Kazakh and Uzbek speakers mostly read Russian).
LANGUAGE_FALLBACKS = {"uk": "en", "kk": "ru", "uz": "ru", "fa": "en", "zh": "en"}
REFERRAL_CODE_LENGTH = 8
SUB_TOKEN_BYTES = 24
# Refresh last_seen_at at most this often, so per-update middleware does not write on every message.
LAST_SEEN_GRANULARITY = timedelta(minutes=5)
_MAX_CREATE_ATTEMPTS = 5
_FIND_LIMIT = 20

_USERNAME = re.compile(r"[A-Za-z0-9_]{1,64}", re.ASCII)
_TG_ID = re.compile(r"-?[0-9]{1,20}", re.ASCII)
_BIGINT_MIN, _BIGINT_MAX = -(2**63), 2**63 - 1  # users.tg_id is a signed 64-bit column
_LINK_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/", re.IGNORECASE)


def normalize_username(username: str | None) -> str | None:
    """Lower-cased username without ``@``; ``None`` for empty input."""
    if not username:
        return None
    cleaned = username.strip().lstrip("@").lower()[:64]
    return cleaned or None


def detect_language(language_code: str | None) -> str | None:
    """Supported language of a Telegram IETF ``language_code`` (``"en-US"`` -> ``"en"``, ``"zh-Hans"`` -> ``"zh"``)."""
    if not language_code:
        return None
    primary = language_code.strip().lower().replace("_", "-").split("-", 1)[0]
    primary = _LANGUAGE_ALIASES.get(primary, primary)
    return primary if primary in LANGUAGES else None


def ru_or_en(lang: str) -> str:
    """``"ru"`` or ``"en"``: which of the two complete languages suits ``lang`` for texts that exist only in them."""
    if lang in ("ru", "en"):
        return lang
    return LANGUAGE_FALLBACKS.get(lang, "en")


def enabled_languages(settings: Settings) -> tuple[str, ...]:
    """Languages offered to users (``ENABLED_LANGUAGES``, always including the default), in picker order."""
    wanted = {code.strip().lower() for code in settings.enabled_languages} | {settings.default_language}
    return tuple(code for code in LANGUAGES if code in wanted)


def pick_language(language_code: str | None, settings: Settings) -> str:
    """Bot language from Telegram's ``language_code`` if that language is enabled, else the default."""
    code = detect_language(language_code)
    if code is not None and code in enabled_languages(settings):
        return code
    return settings.default_language


async def get(session: AsyncSession, tg_id: int) -> User | None:
    """User by Telegram id."""
    return await session.get(User, tg_id)


async def get_or_create(
    session: AsyncSession,
    settings: Settings,
    tg_id: int,
    username: str | None,
    first_name: str | None,
    language_code: str | None,
) -> tuple[User, bool]:
    """Return ``(user, created)``; refreshes username/first name/last seen of existing users.

    A new user gets a unique referral code, a subscription token, a language from ``language_code`` and the
    welcome bonus (if configured), and a ``user.registered`` event. Safe against concurrent first updates
    from the same user: the loser of the insert race returns the winner's row with ``created=False``.
    """
    uname = normalize_username(username)
    fname = first_name[:256] if first_name else None
    user = await session.get(User, tg_id)
    if user is not None:
        await _refresh_profile(session, user, uname, fname)
        return user, False

    for _ in range(_MAX_CREATE_ATTEMPTS):
        created = await _try_insert(session, settings, tg_id, uname, fname, language_code)
        if created is not None:
            await _on_registered(session, settings, created)
            return created, True
        # Shared locking read: sees the row committed after our snapshot (MySQL REPEATABLE READ) without
        # deadlocking against other losers of the same insert race (they all hold shared locks on the key).
        existing = await session.get(User, tg_id, with_for_update={"read": True}, populate_existing=True)
        if existing is not None:  # another request registered the same user concurrently
            await _refresh_profile(session, existing, uname, fname)
            return existing, False
    raise RuntimeError(f"could not allocate unique codes for user {tg_id}")


async def _try_insert(
    session: AsyncSession,
    settings: Settings,
    tg_id: int,
    username: str | None,
    first_name: str | None,
    language_code: str | None,
) -> User | None:
    """Insert a user in a SAVEPOINT; ``None`` on any unique conflict (tg_id, referral code, token)."""
    now = utcnow()
    user = User(
        tg_id=tg_id,
        username=username,
        first_name=first_name,
        language=pick_language(language_code, settings),
        balance_kop=0,
        referral_code=random_code(REFERRAL_CODE_LENGTH),
        sub_token=random_token(SUB_TOKEN_BYTES),
        created_at=now,
        last_seen_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(user)
            await session.flush()
    except IntegrityError:
        return None
    if username:
        await _release_username(session, username, tg_id)
    return user


async def _on_registered(session: AsyncSession, settings: Settings, user: User) -> None:
    if settings.welcome_bonus_kop > 0:
        await wallet.apply(session, user.tg_id, settings.welcome_bonus_kop, TxKind.WELCOME_BONUS, "welcome")
    await events.track(session, "user.registered", user_id=user.tg_id, language=user.language)
    log.info("registered user %s", user.tg_id)


async def _refresh_profile(session: AsyncSession, user: User, username: str | None, first_name: str | None) -> None:
    now = utcnow()
    if user.username != username:
        if username:
            await _release_username(session, username, user.tg_id)
        user.username = username
    if first_name is not None and user.first_name != first_name:
        user.first_name = first_name
    if user.last_seen_at is None or now - user.last_seen_at >= LAST_SEEN_GRANULARITY:
        user.last_seen_at = now
    if user.bot_blocked:  # the user wrote to the bot, so it can message them again
        user.bot_blocked = False


async def _release_username(session: AsyncSession, username: str, owner_id: int) -> None:
    """Usernames move between accounts in Telegram: clear a stale copy held by another user."""
    await session.execute(
        update(User)
        .where(User.username == username, User.tg_id != owner_id)
        .values(username=None)
        .execution_options(synchronize_session="fetch")
    )


def parse_query(query: str) -> int | str | None:
    """Classify an admin search string: Telegram id (int), username (str) or ``None`` if unusable.

    Accepts ``"123"``, ``"@Name"``, ``"name"``, ``"t.me/name"``, ``"https://t.me/name"``. Numbers outside the
    BIGINT range give ``None`` (binding them would raise in the DB driver).
    """
    text = query.strip()
    if _TG_ID.fullmatch(text):
        number = int(text)
        return number if _BIGINT_MIN <= number <= _BIGINT_MAX else None
    text = _LINK_PREFIX.sub("", text).lstrip("@").split("?", 1)[0].strip("/")
    return text.lower() if _USERNAME.fullmatch(text) else None


async def find(session: AsyncSession, query: str) -> User | None:
    """Find a user by id or username (case-insensitive); the most recently seen one wins on duplicates."""
    key = parse_query(query)
    if key is None:
        return None
    if isinstance(key, int):
        return await session.get(User, key)
    rows = (await session.scalars(select(User).where(User.username == key).limit(_FIND_LIMIT))).all()
    if not rows:
        return None
    return max(rows, key=lambda u: (u.last_seen_at is not None, u.last_seen_at or u.created_at))


async def rotate_sub_token(session: AsyncSession, user: User) -> str:
    """Issue a new subscription token; the old subscription URL stops working immediately."""
    from app.services import subscriptions

    sub = await subscriptions.get_for_user(session, user.tg_id)
    if sub is not None:
        return await subscriptions.rotate_token(session, user, sub)
    user.sub_token = random_token(SUB_TOKEN_BYTES)
    await session.flush()
    return user.sub_token


async def set_language(session: AsyncSession, user: User, lang: str) -> None:
    """Set the bot language (one of :data:`LANGUAGES`)."""
    if lang not in LANGUAGES:
        raise ValueError(f"unsupported language: {lang!r}")
    user.language = lang
    await session.flush()


async def get_by_sub_token(session: AsyncSession, sub_token: str) -> User | None:
    """User owning a subscription token (exact match)."""
    if not sub_token:
        return None
    return await session.scalar(select(User).where(User.sub_token == sub_token))


async def get_by_referral_code(session: AsyncSession, code: str) -> User | None:
    """User owning a referral code (case-insensitive; codes are stored upper-case)."""
    normalized = code.strip().upper()
    if not normalized:
        return None
    return await session.scalar(select(User).where(User.referral_code == normalized))
