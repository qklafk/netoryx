from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import BalanceTransaction, Event, TxKind, User
from app.db.session import SessionFactory
from app.services import users
from app.utils.security import CODE_ALPHABET
from app.utils.time import utcnow
from tests.conftest import make_settings


async def _register(
    session_factory: SessionFactory,
    settings: Settings,
    tg_id: int,
    username: str | None = None,
    first_name: str | None = "Ann",
    language_code: str | None = "ru",
) -> tuple[User, bool]:
    async with session_factory.begin() as s:
        return await users.get_or_create(s, settings, tg_id, username, first_name, language_code)


async def test_first_call_creates_user_with_codes(session_factory: SessionFactory, settings: Settings) -> None:
    user, created = await _register(session_factory, settings, 10, "@Alice_X", "Alice", "en-US")
    assert created
    assert user.username == "alice_x"
    assert user.first_name == "Alice"
    assert user.language == "en"
    assert user.balance_kop == 0
    assert len(user.referral_code) == 8 and set(user.referral_code) <= set(CODE_ALPHABET)
    assert len(user.sub_token) >= 32
    assert user.last_seen_at is not None
    async with session_factory() as s:
        types = (await s.scalars(select(Event.type).where(Event.user_id == 10))).all()
        assert types == ["user.registered"]
        assert (await s.scalars(select(BalanceTransaction))).all() == []  # welcome bonus disabled by default


async def test_second_call_returns_existing_and_refreshes_profile(
    session_factory: SessionFactory, settings: Settings
) -> None:
    first, _ = await _register(session_factory, settings, 10, "old", "Old")
    again, created = await _register(session_factory, settings, 10, "New", "New name", "en")
    assert not created
    assert (again.username, again.first_name) == ("new", "New name")
    assert again.language == "ru"  # language is chosen once, then only changed explicitly
    assert (again.referral_code, again.sub_token) == (first.referral_code, first.sub_token)
    async with session_factory() as s:
        assert len((await s.scalars(select(Event).where(Event.type == "user.registered"))).all()) == 1


async def test_any_update_from_user_clears_bot_blocked(session_factory: SessionFactory, settings: Settings) -> None:
    await _register(session_factory, settings, 10, "name")
    async with session_factory.begin() as s:
        user = await users.get(s, 10)
        assert user is not None
        user.bot_blocked = True
    again, _ = await _register(session_factory, settings, 10, "name")
    assert again.bot_blocked is False
    async with session_factory() as s:
        stored = await users.get(s, 10)
        assert stored is not None and stored.bot_blocked is False


async def test_username_removed_in_telegram_is_cleared(session_factory: SessionFactory, settings: Settings) -> None:
    await _register(session_factory, settings, 10, "name")
    user, _ = await _register(session_factory, settings, 10, None)
    assert user.username is None


async def test_username_moved_to_another_account_is_released(
    session_factory: SessionFactory, settings: Settings
) -> None:
    await _register(session_factory, settings, 10, "shared")
    await _register(session_factory, settings, 20, "Shared")
    async with session_factory() as s:
        assert (await users.get(s, 10)).username is None  # type: ignore[union-attr]
        found = await users.find(s, "@shared")
        assert found is not None and found.tg_id == 20


async def test_last_seen_is_throttled(session_factory: SessionFactory, settings: Settings) -> None:
    await _register(session_factory, settings, 10)
    past = utcnow() - users.LAST_SEEN_GRANULARITY - timedelta(seconds=1)
    recent = utcnow() - timedelta(seconds=10)
    async with session_factory.begin() as s:
        user = await users.get(s, 10)
        assert user is not None
        user.last_seen_at = recent
    user, _ = await _register(session_factory, settings, 10)
    assert user.last_seen_at == recent
    async with session_factory.begin() as s:
        user = await users.get(s, 10)
        assert user is not None
        user.last_seen_at = past
    user, _ = await _register(session_factory, settings, 10)
    assert user.last_seen_at is not None and user.last_seen_at > past


async def test_welcome_bonus_is_credited_once(session_factory: SessionFactory) -> None:
    settings = make_settings(welcome_bonus_rub=25)
    user, _ = await _register(session_factory, settings, 10)
    assert user.balance_kop == 2_500
    user, _ = await _register(session_factory, settings, 10)
    assert user.balance_kop == 2_500
    async with session_factory() as s:
        kinds = (await s.scalars(select(BalanceTransaction.kind))).all()
        assert kinds == [TxKind.WELCOME_BONUS]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ru", "ru"),
        ("en", "en"),
        ("en-GB", "en"),
        ("EN_us", "en"),
        ("de", "ru"),
        ("uk", "uk"),
        ("zh-Hans", "zh"),
        (None, "ru"),
        ("", "ru"),
    ],
)
def test_pick_language(code: str | None, expected: str) -> None:
    assert users.pick_language(code, make_settings()) == expected
    assert users.pick_language("de", make_settings(default_language="en")) == "en"


async def test_concurrent_first_updates_create_one_user(session_factory: SessionFactory, settings: Settings) -> None:
    results = await asyncio.gather(*(_register(session_factory, settings, 77, "racer") for _ in range(10)))
    assert sum(created for _, created in results) == 1
    assert len({u.referral_code for u, _ in results}) == 1
    async with session_factory() as s:
        assert len((await s.scalars(select(User))).all()) == 1
        assert len((await s.scalars(select(Event))).all()) == 1


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("123", 123),
        (" 123 ", 123),
        ("@Name_1", "name_1"),
        ("name_1", "name_1"),
        ("t.me/Name_1", "name_1"),
        ("https://t.me/name_1", "name_1"),
        ("http://telegram.me/name_1/", "name_1"),
        ("https://t.me/name_1?start=x", "name_1"),
        ("", None),
        ("two words", None),
        ("@", None),
        ("имя", None),
        ("-100123", -100123),
        ("9223372036854775807", 2**63 - 1),
        ("-9223372036854775808", -(2**63)),
        ("9223372036854775808", None),  # outside BIGINT
        ("-9223372036854775809", None),
        ("12345678901234567890", None),
    ],
)
def test_parse_query(query: str, expected: int | str | None) -> None:
    assert users.parse_query(query) == expected


async def test_find_by_id_and_username(session_factory: SessionFactory, settings: Settings) -> None:
    await _register(session_factory, settings, 10, "Alice")
    async with session_factory() as s:
        for query in ("10", "@ALICE", "alice", "t.me/alice", "https://t.me/Alice"):
            found = await users.find(s, query)
            assert found is not None and found.tg_id == 10, query
        assert await users.find(s, "11") is None
        assert await users.find(s, "bob") is None
        assert await users.find(s, "!!") is None


@pytest.mark.parametrize("query", ["12345678901234567890", "9223372036854775808", "-9223372036854775809"])
async def test_find_out_of_range_id_returns_none(session_factory: SessionFactory, query: str) -> None:
    async with session_factory() as s:
        assert await users.find(s, query) is None


async def test_rotate_sub_token(session_factory: SessionFactory, settings: Settings) -> None:
    user, _ = await _register(session_factory, settings, 10)
    old = user.sub_token
    async with session_factory.begin() as s:
        user = await users.get(s, 10)  # type: ignore[assignment]
        new = await users.rotate_sub_token(s, user)
    assert new != old
    async with session_factory() as s:
        assert await users.get_by_sub_token(s, old) is None
        assert (await users.get_by_sub_token(s, new)).tg_id == 10  # type: ignore[union-attr]
        assert await users.get_by_sub_token(s, "") is None


async def test_set_language(session_factory: SessionFactory, settings: Settings) -> None:
    await _register(session_factory, settings, 10)
    async with session_factory.begin() as s:
        user = await users.get(s, 10)
        assert user is not None
        await users.set_language(s, user, "en")
        with pytest.raises(ValueError):
            await users.set_language(s, user, "de")
    async with session_factory() as s:
        assert (await users.get(s, 10)).language == "en"  # type: ignore[union-attr]


async def test_get_by_referral_code_is_case_insensitive(session_factory: SessionFactory, settings: Settings) -> None:
    user, _ = await _register(session_factory, settings, 10)
    async with session_factory() as s:
        found = await users.get_by_referral_code(s, f" {user.referral_code.lower()} ")
        assert found is not None and found.tg_id == 10
        assert await users.get_by_referral_code(s, "  ") is None
