"""Custom buttons of the bot's main menu: validation, storage and a per-process cache for rendering.

Buttons are edited rarely and the menu is rendered on almost every interaction, so readers go through
:class:`MenuCache`: it keeps the active buttons in memory and checks a version stamp in ``kv`` at most every
``ttl`` seconds. Every change bumps the stamp, so other processes notice it on their next check; the process that
made the change calls :func:`invalidate` after its commit to see it at once.
"""

from __future__ import annotations

import re
import time
import uuid
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import MAX_TEXT_LENGTH, TextValidationError, check_html
from app.db.models import MenuButton
from app.db.session import SessionFactory, read_session
from app.services import kv

VERSION_KEY = "menu_buttons_version"

KIND_URL = "url"
KIND_WEBAPP = "webapp"
KIND_COPY = "copy_text"
KIND_SCREEN = "text_screen"
KINDS = (KIND_URL, KIND_WEBAPP, KIND_COPY, KIND_SCREEN)

AUDIENCE_ALL = "all"
AUDIENCE_SUBSCRIBERS = "subscribers"
AUDIENCE_NON_SUBSCRIBERS = "non_subscribers"
AUDIENCE_ADMINS = "admins"
AUDIENCES = (AUDIENCE_ALL, AUDIENCE_SUBSCRIBERS, AUDIENCE_NON_SUBSCRIBERS, AUDIENCE_ADMINS)

# Built-in main menu entries that the owner may hide (``MENU_HIDDEN_BUTTONS``). The admin entry is always shown.
BUILTIN_KEYS = (
    "trial",
    "subscription",
    "connect",
    "buy",
    "balance",
    "referral",
    "gift",
    "tg_proxy",
    "support",
    "instructions",
    "language",
    "mini_app",
)

MAX_TITLE = 64
MAX_URL = 2048
MAX_COPY_TEXT = 256  # Telegram CopyTextButton limit
MAX_ROW = 99
MAX_SORT_ORDER = 9999
MAX_BUTTONS = 50
_LANG = re.compile(r"^[a-z]{2,8}$")
_URL_SCHEMES = ("http", "https", "tg")


class MenuButtonError(ValueError):
    """A button that Telegram would reject or that makes no sense."""


# --- validation -------------------------------------------------------------------------------------------------------
def clean_title(title: str) -> str:
    value = (title or "").strip()
    if not value:
        raise MenuButtonError("title must not be empty")
    if len(value) > MAX_TITLE:
        raise MenuButtonError(f"title is longer than {MAX_TITLE} characters")
    if any(ch in value for ch in "\r\n\t"):
        raise MenuButtonError("title must be a single line")
    return value


def clean_titles(titles: Mapping[str, str] | None) -> dict[str, str]:
    """Per-language titles; empty values are dropped."""
    clean: dict[str, str] = {}
    for lang, title in (titles or {}).items():
        if not _LANG.match(str(lang)):
            raise MenuButtonError(f"unsupported language code: {lang!r}")
        if title is None or not str(title).strip():
            continue
        clean[str(lang)] = clean_title(str(title))
    return clean


def check_url(url: str, *, https_only: bool = False) -> str:
    """``url`` stripped if it is an absolute http(s):// or tg:// link (only https:// with ``https_only``)."""
    value = (url or "").strip()
    if not value:
        raise MenuButtonError("link must not be empty")
    if len(value) > MAX_URL:
        raise MenuButtonError(f"link is longer than {MAX_URL} characters")
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise MenuButtonError("link must not contain spaces")
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    allowed = ("https",) if https_only else _URL_SCHEMES
    if scheme not in allowed:
        raise MenuButtonError("only https:// links are allowed" if https_only else "use an http(s):// or tg:// link")
    if scheme in ("http", "https") and not parts.hostname:
        raise MenuButtonError("link has no host")
    if scheme == "tg" and not (parts.netloc or parts.path):
        raise MenuButtonError("tg:// link is empty")
    return value


def clean_payload(kind: str, payload: str) -> str:
    if kind == KIND_URL:
        return check_url(payload)
    if kind == KIND_WEBAPP:
        return check_url(payload, https_only=True)
    if kind == KIND_COPY:
        value = payload or ""
        if not value.strip():
            raise MenuButtonError("text to copy must not be empty")
        if len(value) > MAX_COPY_TEXT:
            raise MenuButtonError(f"text to copy is longer than {MAX_COPY_TEXT} characters")
        return value
    if kind == KIND_SCREEN:
        value = (payload or "").strip()
        if not value:
            raise MenuButtonError("screen text must not be empty")
        if len(value) > MAX_TEXT_LENGTH:
            raise MenuButtonError(f"screen text is longer than {MAX_TEXT_LENGTH} characters")
        try:
            check_html(value)
        except TextValidationError as exc:
            raise MenuButtonError(str(exc)) from None
        return value
    raise MenuButtonError(f"unknown button kind: {kind!r}")


def _check_choice(value: str, choices: Sequence[str], what: str) -> str:
    if value not in choices:
        raise MenuButtonError(f"{what} must be one of: {', '.join(choices)}")
    return value


def _check_range(value: int, low: int, high: int, what: str) -> int:
    if not low <= value <= high:
        raise MenuButtonError(f"{what} must be {low}..{high}")
    return value


# --- cached view ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MenuItem:
    """An immutable copy of an active :class:`MenuButton` used for rendering."""

    id: int
    kind: str
    title: str
    payload: str
    row: int = 0
    sort_order: int = 0
    audience: str = AUDIENCE_ALL
    titles: Mapping[str, str] = field(default_factory=dict)

    def title_for(self, lang: str) -> str:
        return self.titles.get(lang) or self.title

    def visible_to(self, *, subscribed: bool, is_admin: bool) -> bool:
        if self.audience == AUDIENCE_SUBSCRIBERS:
            return subscribed
        if self.audience == AUDIENCE_NON_SUBSCRIBERS:
            return not subscribed
        if self.audience == AUDIENCE_ADMINS:
            return is_admin
        return True


def to_item(button: MenuButton) -> MenuItem:
    return MenuItem(
        id=button.id,
        kind=button.kind,
        title=button.title,
        payload=button.payload,
        row=button.row,
        sort_order=button.sort_order,
        audience=button.audience,
        titles=dict(button.titles or {}),
    )


def _order(stmt: Any) -> Any:
    return stmt.order_by(MenuButton.row, MenuButton.sort_order, MenuButton.id)


async def load_active(session: AsyncSession) -> tuple[MenuItem, ...]:
    rows = (await session.scalars(_order(select(MenuButton).where(MenuButton.is_active.is_(True))))).all()
    return tuple(to_item(b) for b in rows)


class MenuCache:
    """Active buttons of one database, refreshed when the ``kv`` version stamp changes."""

    def __init__(
        self, session_factory: SessionFactory, *, ttl: float = 15.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._session_factory = session_factory
        self._ttl = ttl
        self._clock = clock
        self._items: tuple[MenuItem, ...] | None = None
        self._version: str | None = None
        self._checked_at = 0.0

    def invalidate(self) -> None:
        self._items = None

    async def items(self) -> tuple[MenuItem, ...]:
        now = self._clock()
        if self._items is not None and now - self._checked_at < self._ttl:
            return self._items
        async with read_session(self._session_factory) as session, session.begin():
            version = await kv.get(session, VERSION_KEY)
            if self._items is None or version != self._version:
                self._items = await load_active(session)
                self._version = version
        self._checked_at = now
        return self._items


_caches: weakref.WeakKeyDictionary[Any, MenuCache] = weakref.WeakKeyDictionary()


def cache_for(session_factory: SessionFactory) -> MenuCache:
    """The process-wide cache for ``session_factory``'s database."""
    cache = _caches.get(session_factory)
    if cache is None:
        cache = _caches[session_factory] = MenuCache(session_factory)
    return cache


def invalidate(session_factory: SessionFactory) -> None:
    """Drop this process's cached buttons (call after committing a change)."""
    cache = _caches.get(session_factory)
    if cache is not None:
        cache.invalidate()


# --- storage ----------------------------------------------------------------------------------------------------------
async def _bump(session: AsyncSession) -> None:
    await kv.set(session, VERSION_KEY, uuid.uuid4().hex)


async def list_all(session: AsyncSession) -> list[MenuButton]:
    return list((await session.scalars(_order(select(MenuButton)))).all())


async def get(session: AsyncSession, button_id: int, *, lock: bool = False) -> MenuButton | None:
    stmt = select(MenuButton).where(MenuButton.id == button_id)
    if lock:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def create(
    session: AsyncSession,
    *,
    kind: str,
    title: str,
    payload: str,
    titles: Mapping[str, str] | None = None,
    row: int = 0,
    sort_order: int | None = None,
    audience: str = AUDIENCE_ALL,
    is_active: bool = True,
) -> MenuButton:
    """Validate and add a button (at the end of ``row`` unless ``sort_order`` is given)."""
    kind = _check_choice(kind, KINDS, "kind")
    count = len(await list_all(session))
    if count >= MAX_BUTTONS:
        raise MenuButtonError(f"at most {MAX_BUTTONS} custom buttons")
    _check_range(row, 0, MAX_ROW, "row")
    if sort_order is None:
        orders = [b.sort_order for b in await list_all(session) if b.row == row]
        sort_order = min(max(orders, default=-1) + 1, MAX_SORT_ORDER)
    button = MenuButton(
        kind=kind,
        title=clean_title(title),
        titles=clean_titles(titles),
        payload=clean_payload(kind, payload),
        row=row,
        sort_order=_check_range(sort_order, 0, MAX_SORT_ORDER, "sort_order"),
        audience=_check_choice(audience, AUDIENCES, "audience"),
        is_active=is_active,
    )
    session.add(button)
    await session.flush()
    await _bump(session)
    return button


_UPDATABLE = frozenset({"kind", "title", "titles", "payload", "row", "sort_order", "audience", "is_active"})


async def update(session: AsyncSession, button: MenuButton, **fields: Any) -> MenuButton:
    """Change fields (``None`` values are ignored); the payload is re-checked against the resulting kind."""
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise MenuButtonError(f"unknown fields: {', '.join(sorted(unknown))}")
    values = {k: v for k, v in fields.items() if v is not None}
    kind = _check_choice(values.get("kind", button.kind), KINDS, "kind")
    payload = clean_payload(kind, values.get("payload", button.payload))
    title = clean_title(values["title"]) if "title" in values else button.title
    titles = clean_titles(values["titles"]) if "titles" in values else button.titles
    row = _check_range(int(values.get("row", button.row)), 0, MAX_ROW, "row")
    sort_order = _check_range(int(values.get("sort_order", button.sort_order)), 0, MAX_SORT_ORDER, "sort_order")
    audience = _check_choice(values.get("audience", button.audience), AUDIENCES, "audience")
    button.kind, button.payload, button.title, button.titles = kind, payload, title, dict(titles)
    button.row, button.sort_order, button.audience = row, sort_order, audience
    if "is_active" in values:
        button.is_active = bool(values["is_active"])
    await session.flush()
    await _bump(session)
    return button


async def delete(session: AsyncSession, button: MenuButton) -> None:
    await session.delete(button)
    await session.flush()
    await _bump(session)


async def reorder(session: AsyncSession, positions: Sequence[tuple[int, int, int]]) -> list[MenuButton]:
    """Set ``(button_id, row, sort_order)`` for each listed button; unknown ids are rejected."""
    buttons = {b.id: b for b in await list_all(session)}
    missing = [bid for bid, _, _ in positions if bid not in buttons]
    if missing:
        raise MenuButtonError(f"unknown buttons: {', '.join(map(str, missing))}")
    for bid, row, order in positions:
        buttons[bid].row = _check_range(row, 0, MAX_ROW, "row")
        buttons[bid].sort_order = _check_range(order, 0, MAX_SORT_ORDER, "sort_order")
    await session.flush()
    await _bump(session)
    return await list_all(session)


async def move(session: AsyncSession, button: MenuButton, direction: int) -> list[MenuButton]:
    """Swap ``button`` with its neighbour before (``-1``) or after (``+1``) it in display order.

    Moving across a row boundary moves the button into that row. Orders inside each row are renumbered first so
    equal ``sort_order`` values do not make a swap a no-op.
    """
    if direction not in (-1, 1):
        raise MenuButtonError("direction must be -1 or 1")
    buttons = await list_all(session)
    per_row: dict[int, int] = {}
    for b in buttons:
        b.sort_order = per_row.get(b.row, 0)
        per_row[b.row] = b.sort_order + 1
    index = next(i for i, b in enumerate(buttons) if b.id == button.id)
    other_index = index + direction
    if 0 <= other_index < len(buttons):
        other = buttons[other_index]
        if other.row == button.row:
            button.sort_order, other.sort_order = other.sort_order, button.sort_order
        else:
            # Join the neighbouring row at its near end.
            button.row = other.row
            button.sort_order = other.sort_order + (1 if direction < 0 else 0)
            if direction > 0:
                for b in buttons:
                    if b.row == other.row and b.id != button.id:
                        b.sort_order += 1
    await session.flush()
    await _bump(session)
    return await list_all(session)
