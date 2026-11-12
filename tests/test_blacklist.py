"""External blacklist: parsing, downloading, storage, policy and the update middleware."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiogram import Router
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

from app.config import Settings
from app.db.models import Admin, AdminRole, Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory
from app.middlewares import setup_middlewares
from app.modules.blacklist import service as blacklist_service
from app.services import bans, blacklist, kv, users
from app.utils.time import utcnow
from tests.conftest import make_settings
from tests.test_middlewares_support import Recorder, make_ctx, message_update

OWNER = 1000  # in ADMIN_IDS


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"blacklist_enabled": True, "blacklist_action": "block"}
    base.update(overrides)
    return make_settings(**base)


async def _user(sf: SessionFactory, tg_id: int, settings: Settings) -> None:
    async with sf.begin() as s:
        await users.get_or_create(s, settings, tg_id, None, "Ann", "ru")


async def _get(sf: SessionFactory, tg_id: int) -> User:
    async with sf() as s:
        user = await s.get(User, tg_id)
        assert user is not None
        return user


async def _paid_subscription(sf: SessionFactory, tg_id: int, *, trial: bool = False) -> None:
    async with sf.begin() as s:
        s.add(
            Subscription(
                user_id=tg_id,
                client_uuid=f"00000000-0000-0000-0000-{tg_id:012d}",
                status=SubscriptionStatus.ACTIVE,
                expires_at=utcnow() + timedelta(days=30),
                is_trial=trial,
            )
        )


def _cache(entries: dict[int, str]) -> blacklist.BlacklistCache:
    cache = blacklist.BlacklistCache()
    cache.set_entries(entries, "v1")
    return cache


# --- parsing -----------------------------------------------------------------------------------------------------
def test_parse_list_handles_comments_reasons_and_junk() -> None:
    text = (
        "﻿# header comment\n"
        "\n"
        "123456789\n"
        "  222 spam bot  \n"
        "333,fraud # trailing comment\n"
        "444\tchargeback\n"
        "@someone\n"
        "not an id\n"
        "0\n"
        "-100500\n"
        "99999999999999999999\n"  # beyond a signed 64-bit integer
        "9223372036854775807\n"  # the largest one that fits
        "// another comment\n"
        "123456789 duplicate\n"
    )
    parsed = blacklist.parse_list(text)
    assert parsed.entries == {
        123456789: "duplicate",
        222: "spam bot",
        333: "fraud",
        444: "chargeback",
        9223372036854775807: "",
    }
    assert parsed.skipped == 5
    assert not parsed.truncated


def test_parse_list_truncates_and_reasons_are_sanitised() -> None:
    parsed = blacklist.parse_list("1\n2\n3\n", max_ids=2)
    assert set(parsed.entries) == {1, 2} and parsed.truncated
    long = blacklist.parse_list("5 " + "x" * 500 + "\x07")
    assert len(long.entries[5]) == blacklist.MAX_REASON_LENGTH


def test_encoded_entries_round_trip_compactly() -> None:
    entries = {i: ("reason" if i % 1000 == 0 else "") for i in range(5_000_000_000, 5_000_100_000, 7)}
    blob = blacklist.encode_entries(entries)
    assert blacklist.decode_entries(blob) == entries
    assert len(blob) < len(entries) * 2  # delta encoding + deflate


def test_settings_accept_text_lists() -> None:
    s = make_settings(
        blacklist_urls="https://a.example/l.txt, https://b.example/x\nhttps://c.example/y",
        blacklist_manual_ids="1, 2\n3",
    )
    assert s.blacklist_urls == ["https://a.example/l.txt", "https://b.example/x", "https://c.example/y"]
    assert s.blacklist_manual_ids == [1, 2, 3]
    with pytest.raises(ValueError):
        make_settings(blacklist_urls="ftp://example.com/list")


# --- downloading -------------------------------------------------------------------------------------------------
class ListServer:
    def __init__(self) -> None:
        self.body = b"111\n222 spam\n"
        self.status = 200
        self.etag: str | None = None
        self.seen_etags: list[str | None] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.seen_etags.append(request.headers.get("If-None-Match"))
        if self.etag and request.headers.get("If-None-Match") == self.etag:
            return web.Response(status=304)
        headers = {"ETag": self.etag} if self.etag else {}
        return web.Response(status=self.status, body=self.body, headers=headers)


@pytest.fixture
async def server() -> AsyncIterator[tuple[ListServer, str, aiohttp.ClientSession]]:
    state = ListServer()
    app = web.Application()
    app.router.add_get("/list.txt", state.handle)
    srv = TestServer(app)
    await srv.start_server()
    http = aiohttp.ClientSession()
    try:
        yield state, str(srv.make_url("/list.txt")), http
    finally:
        await http.close()
        await srv.close()


async def test_size_limit_is_enforced(server: Any) -> None:
    state, url, http = server
    state.body = b"1234567\n" * 1000
    result = await blacklist.fetch_source(http, url, max_bytes=1000)
    assert result.status == "error" and "larger" in (result.error or "")
    ok = await blacklist.fetch_source(http, url, max_bytes=100_000)
    assert ok.status == "ok"


async def test_failure_keeps_the_last_good_list(server: Any, session_factory: SessionFactory) -> None:
    state, url, http = server
    settings = _settings(blacklist_urls=[url])
    cache = blacklist.BlacklistCache()
    report = await blacklist.refresh(session_factory, http, settings, cache)
    assert report.updated == 1 and cache.ids == {111, 222} and cache.reason(222, settings) == "spam"

    state.status = 500
    report = await blacklist.refresh(session_factory, http, settings, cache)
    assert report.failed == 1 and cache.ids == {111, 222}

    state.status, state.body = 200, b"<html><body>Service unavailable</body></html>"
    report = await blacklist.refresh(session_factory, http, settings, cache)
    assert report.failed == 1 and cache.ids == {111, 222}  # an error page is not an empty list

    fresh = blacklist.BlacklistCache()  # another process loads the stored copy
    await fresh.reload(session_factory)
    assert fresh.ids == {111, 222}
    info = await blacklist.status(session_factory, settings, fresh)
    assert info["sources"][0]["count"] == 2 and info["sources"][0]["error"]


async def test_conditional_requests_and_removed_sources(server: Any, session_factory: SessionFactory) -> None:
    state, url, http = server
    state.etag = '"abc"'
    settings = _settings(blacklist_urls=[url])
    cache = blacklist.BlacklistCache()
    await blacklist.refresh(session_factory, http, settings, cache)
    report = await blacklist.refresh(session_factory, http, settings, cache)
    assert report.unchanged == 1 and state.seen_etags[-1] == '"abc"' and cache.ids == {111, 222}

    report = await blacklist.refresh(session_factory, http, _settings(blacklist_urls=[]), cache)
    assert report.removed == 1 and cache.ids == frozenset()


async def test_large_list_is_stored_in_chunks(server: Any, session_factory: SessionFactory) -> None:
    state, url, http = server
    state.body = "\n".join(str(10**9 + i * 7919) for i in range(200_000)).encode()
    settings = _settings(blacklist_urls=[url])
    cache = blacklist.BlacklistCache()
    await blacklist.refresh(session_factory, http, settings, cache)
    fresh = blacklist.BlacklistCache()
    await fresh.reload(session_factory)
    assert len(fresh.ids) == 200_000 and fresh.ids == cache.ids


# --- policy ------------------------------------------------------------------------------------------------------
async def test_block_bans_listed_users_but_not_staff(session_factory: SessionFactory) -> None:
    settings = _settings()
    for tg_id in (OWNER, 2002, 3003, 4004):
        await _user(session_factory, tg_id, settings)
    async with session_factory.begin() as s:
        s.add(Admin(tg_id=2002, role=AdminRole.SUPPORT))
    await _paid_subscription(session_factory, 3003, trial=True)  # a trial is not a paid subscription
    cache = _cache({OWNER: "", 2002: "", 3003: "fraud", 9999: ""})

    report = await blacklist.apply(session_factory, settings, cache)
    assert report.banned == [3003] and report.skipped_staff == 2 and report.matched == 3
    assert (await _get(session_factory, 3003)).is_banned
    assert not (await _get(session_factory, OWNER)).is_banned
    assert not (await _get(session_factory, 2002)).is_banned
    async with session_factory() as s:
        assert await kv.get(s, bans.blacklist_mark_key(3003)) == bans.MARK_BANNED
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == 3003))
        assert sub is not None and sub.revision == 2  # the panels are told to disable the account

    again = await blacklist.apply(session_factory, settings, cache)
    assert again.banned == [] and again.unbanned == []  # idempotent


async def test_paying_users_are_only_flagged_by_default(session_factory: SessionFactory) -> None:
    settings = _settings()
    await _user(session_factory, 5005, settings)
    await _paid_subscription(session_factory, 5005)
    cache = _cache({5005: ""})

    report = await blacklist.apply(session_factory, settings, cache)
    assert report.banned == [] and report.new_flagged == [5005] and report.skipped_paying == 1
    assert not (await _get(session_factory, 5005)).is_banned
    assert (await blacklist.apply(session_factory, settings, cache)).new_flagged == []  # told once

    report = await blacklist.apply(session_factory, _settings(blacklist_ban_paying=True), cache)
    assert report.banned == [5005]


async def test_flag_action_never_bans(session_factory: SessionFactory) -> None:
    settings = _settings(blacklist_action="flag")
    await _user(session_factory, 6006, settings)
    report = await blacklist.apply(session_factory, settings, _cache({6006: ""}))
    assert report.banned == [] and report.new_flagged == [6006]
    assert not (await _get(session_factory, 6006)).is_banned


async def test_only_blacklist_bans_are_lifted(session_factory: SessionFactory) -> None:
    settings = _settings()
    for tg_id in (7007, 8008):
        await _user(session_factory, tg_id, settings)
    async with session_factory.begin() as s:  # 8008 was banned by an administrator before
        user = await s.get(User, 8008)
        assert user is not None
        await bans.set_banned_manually(s, user, True)
    await blacklist.apply(session_factory, settings, _cache({7007: "", 8008: ""}))
    assert (await _get(session_factory, 7007)).is_banned

    report = await blacklist.apply(session_factory, settings, _cache({}))
    assert report.unbanned == [7007]
    assert not (await _get(session_factory, 7007)).is_banned
    assert (await _get(session_factory, 8008)).is_banned
    async with session_factory() as s:
        assert await kv.get(s, bans.blacklist_mark_key(7007)) is None


async def test_manual_unban_is_respected(session_factory: SessionFactory) -> None:
    settings = _settings()
    await _user(session_factory, 9009, settings)
    cache = _cache({9009: ""})
    await blacklist.apply(session_factory, settings, cache)
    async with session_factory.begin() as s:
        user = await s.get(User, 9009)
        assert user is not None
        await bans.set_banned_manually(s, user, False)
    report = await blacklist.apply(session_factory, settings, cache)
    assert report.banned == [] and not (await _get(session_factory, 9009)).is_banned


async def test_manual_unban_of_admin_ban_is_respected(session_factory: SessionFactory) -> None:
    settings = _settings()
    await _user(session_factory, 9010, settings)
    for banned in (True, False):  # banned and later unbanned by an administrator, never by the list
        async with session_factory.begin() as s:
            user = await s.get(User, 9010)
            assert user is not None
            await bans.set_banned_manually(s, user, banned)
    report = await blacklist.apply(session_factory, settings, _cache({9010: ""}))
    assert report.banned == [] and not (await _get(session_factory, 9010)).is_banned


# --- middleware --------------------------------------------------------------------------------------------------
@pytest.fixture
def harness(session_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    blacklist.CACHE.clear()
    notices: list[str] = []

    def fake_notify(ctx: Any, event: Any, text: str, **kw: Any) -> bool:
        notices.append(text)
        return True

    monkeypatch.setattr(blacklist_service, "notify", fake_notify)

    def factory(**settings: Any) -> Any:
        ctx, api = make_ctx(session_factory, tmp_path, blacklist_enabled=True, **settings)
        seen = Recorder()
        router = Router()
        router.message.register(seen.handle)
        ctx.dispatcher.include_router(router)
        mw = setup_middlewares(ctx.dispatcher, ctx)  # type: ignore[arg-type]
        return ctx, api, seen, notices, mw

    yield factory
    blacklist.CACHE.clear()


async def _feed(ctx: Any, mw: Any, update: Any) -> None:
    await ctx.dispatcher.feed_update(ctx.bot, update)
    await mw.replayer.wait()


async def test_middleware_blocks_listed_users_on_start(harness: Any, session_factory: SessionFactory) -> None:
    ctx, api, seen, notices, mw = harness(blacklist_action="block", blacklist_manual_ids=[5001, OWNER])
    await _feed(ctx, mw, message_update(5001, "/start"))
    await _feed(ctx, mw, message_update(5001, "again"))
    assert not seen.seen
    assert (await _get(session_factory, 5001)).is_banned
    assert api.texts().count("BANNED") == 1
    assert notices and "5001" in notices[0]

    await _feed(ctx, mw, message_update(OWNER, "/start"))  # administrators are never blocked
    assert len(seen.seen) == 1
    assert not (await _get(session_factory, OWNER)).is_banned


async def test_middleware_flag_lets_users_through(harness: Any, session_factory: SessionFactory) -> None:
    ctx, _api, seen, notices, mw = harness(blacklist_action="flag", blacklist_manual_ids=[5002])
    await _feed(ctx, mw, message_update(5002, "/start"))
    await _feed(ctx, mw, message_update(5002, "hi"))
    assert len(seen.seen) == 2 and not (await _get(session_factory, 5002)).is_banned
    assert len(notices) == 1


async def test_middleware_ignores_the_list_when_disabled(harness: Any, session_factory: SessionFactory) -> None:
    ctx, _api, seen, notices, mw = harness(blacklist_manual_ids=[5003])
    ctx.current = ctx.current.model_copy(update={"blacklist_enabled": False})
    await _feed(ctx, mw, message_update(5003, "/start"))
    assert len(seen.seen) == 1 and not notices
