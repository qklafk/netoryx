"""Tests for app.services.deeplinks: app registry, INCY crypt1 port, Happ crypt5 API client."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select

from app.db.models import KeyValue
from app.db.session import SessionFactory
from app.services import deeplinks
from app.services.deeplinks import (
    APPS,
    INCY_CRYPT1_KEY,
    INCY_CRYPT1_PREFIX,
    INCY_KEY_FINGERPRINT,
    Platform,
    apps_for_platform,
    get_app,
    happ_cache_key,
    happ_crypt_link,
    incy_crypt_link,
    incy_decrypt_link,
    incy_key_fingerprint,
)

SUB_URL = "https://bot.example.com/sub/abc123"


# ---------------------------------------------------------------------------------------------
# Apps registry
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("key", "name", "expected"),
    [
        ("happ", None, "happ://add/https://bot.example.com/sub/abc123"),
        ("happ", "Ignored", "happ://add/https://bot.example.com/sub/abc123"),
        ("incy", "X", "incy://add/https://bot.example.com/sub/abc123"),
        ("v2raytun", None, "v2raytun://import/https://bot.example.com/sub/abc123"),
        ("hiddify", "Мой VPN", "hiddify://import/https://bot.example.com/sub/abc123#%D0%9C%D0%BE%D0%B9%20VPN"),
        ("hiddify", None, "hiddify://import/https://bot.example.com/sub/abc123"),
        ("streisand", "VPN", "streisand://import/https://bot.example.com/sub/abc123#VPN"),
        ("shadowrocket", "A B", "shadowrocket://add/https://bot.example.com/sub/abc123#A%20B"),
        ("v2rayng", "VPN", "v2rayng://install-config?url=https%3A%2F%2Fbot.example.com%2Fsub%2Fabc123&name=VPN"),
        ("v2rayng", None, "v2rayng://install-config?url=https%3A%2F%2Fbot.example.com%2Fsub%2Fabc123"),
    ],
)
def test_deeplink_formats(key: str, name: str | None, expected: str) -> None:
    assert APPS[key].deeplink(SUB_URL, name) == expected


def test_empty_name_treated_as_none() -> None:
    assert APPS["hiddify"].deeplink(SUB_URL, "") == f"hiddify://import/{SUB_URL}"


@pytest.mark.parametrize("bad", ["", "ftp://x/y", "bot.example.com/sub/x", "https://a.b/x y", "https://a.b/\nx"])
def test_deeplink_rejects_non_http_urls(bad: str) -> None:
    with pytest.raises(ValueError):
        APPS["happ"].deeplink(bad)


def test_registry_order_and_contents() -> None:
    assert list(APPS)[:3] == ["happ", "incy", "v2raytun"]
    for app in APPS.values():
        assert app.downloads, app.key
        for download in app.downloads:
            assert download.url.startswith("https://"), download
            assert download.label
    assert APPS["happ"].supports_crypto_link and APPS["incy"].supports_crypto_link
    assert not APPS["v2raytun"].supports_crypto_link


def test_platforms_and_downloads_for() -> None:
    happ = APPS["happ"]
    assert happ.platforms[0] == Platform.IOS
    assert len(set(happ.platforms)) == len(happ.platforms)
    assert [d.label for d in happ.downloads_for(Platform.WINDOWS)] == ["Installer (x64)", "Installer (ARM64)"]
    assert happ.downloads_for(Platform.ANDROID)[0].url.endswith("/Happ.apk")
    assert APPS["shadowrocket"].downloads_for(Platform.ANDROID) == []


def test_apps_for_platform_and_get_app() -> None:
    ios = [a.key for a in apps_for_platform(Platform.IOS)]
    assert ios[:2] == ["happ", "incy"] and "v2rayng" not in ios
    assert "shadowrocket" not in [a.key for a in apps_for_platform(Platform.ANDROID)]
    assert [a.key for a in apps_for_platform(Platform.LINUX, [APPS["happ"], APPS["v2raytun"]])] == ["happ"]
    assert get_app("HAPP") is APPS["happ"] and get_app("nope") is None


# ---------------------------------------------------------------------------------------------
# INCY crypt1
# ---------------------------------------------------------------------------------------------
IV0 = bytes(range(12))
# Cross-platform vector pinned in INCY-DEV/incy-link-encoder test/index.test.ts (also checked by the
# iOS/Android/Desktop clients' interop tests).
UPSTREAM_VECTOR = (
    "incy://crypt1/AAECAwQFBgcICQoLNyIQL3rDwRZqnyoD8pGKSLXP6o8NdSXQVSSALNbbUyIr__tWGFUexdIfKvvmDnuDGbmBvuppfNef6aKN"
    "ZUwOm4c-Sg"
)
# Generated with an independent node:crypto implementation of the published algorithm (Node 26).
NAMED_VECTORS = [
    (
        "https://bot.example.com/sub/abc123",
        "TestVPN",
        "01080f161d242b323940474e",
        "incy://crypt1/AQgPFh0kKzI5QEdOMPza6MnaTEgZbRImCMvlHbgSXXnRW3YVETPityPVYMCDc02WwpbE5zC2eQHDjloKc9lsKHMsrWcL6jO_"
        "HTfiQaCZkKmSVd_dFeO_akheVCI",
    ),
    (
        "https://sub.example.org/sub/tok?profile=wl&x=1",
        "Мой VPN 🇩🇪",
        "020910171e252c333a41484f",
        "incy://crypt1/AgkQFx4lLDM6QUhPQF3QCl3LMVsHiDNCMiA4qrv_ONFAVBTSbmm0l0c-bYZ2o0F3zD3kPM7uwwxgjua1bp6ho9ldtRJwszBF"
        "AufZ_u1WduRd-JhLAUY0lBSRZ2WK6MtJGOhNK-Uoa00lrrS3rvJHJWt5rcs",
    ),
    (
        "https://a.b/c",
        "x" * 200,
        "030a11181f262d343b424950",
        "incy://crypt1/AwoRGB8mLTQ7QklQdJIpyuPCYOlXLpSabGIhZ73P-iwwEHgw8bDp14Lxed90sppTpLJonSMMhU5ogtSWGC53SD-ncAcFwhlb"
        "a-73oMy8JwMiAqtsTuNr4wz_1BGptqIlkqXGCEWgkx2PtHtlaG2DXuY9r5l2T1vh7uKFdGPTeH9wV1SiHil1R8Te_cpOcK8IiNgRBDyNfKbK"
        "rP1C_MNpmAODyTbXAmqZhOIH9Dzm3WzxbK9cZB1CmMMFcg7HjJIl",
    ),
    (
        'https://a.b/"q"s',
        "tab\tnew\nline",
        "040b121920272e353c434a51",
        "incy://crypt1/BAsSGSAnLjU8Q0pRPmlqFZhuR-Fa9pgopKc5URGgke0JGBoqhGWnwoOTGMl4yMB_SCBhgVqtMBicCo1uW53-YJMLPJmHvFDA"
        "00SFnC8cFhXmG-U",
    ),
]


def test_incy_key_fingerprint_matches_vendor() -> None:
    assert incy_key_fingerprint() == INCY_KEY_FINGERPRINT


def test_incy_upstream_pinned_vector() -> None:
    assert incy_crypt_link("https://sub.example.com/test-vector", iv=IV0) == UPSTREAM_VECTOR
    assert incy_decrypt_link(UPSTREAM_VECTOR) == ("https://sub.example.com/test-vector", None)


def test_incy_readme_example_prefix() -> None:
    link = incy_crypt_link("https://sub.your-provider.example/abc123token", iv=IV0)
    assert link.startswith("incy://crypt1/AAECAwQFBgcICQoLNyIQL3rDwRZqnyoD8pGK")


@pytest.mark.parametrize(("url", "name", "iv", "expected"), NAMED_VECTORS)
def test_incy_named_vectors(url: str, name: str, iv: str, expected: str) -> None:
    assert incy_crypt_link(url, name, iv=bytes.fromhex(iv)) == expected
    decoded_url, decoded_name = incy_decrypt_link(expected)
    assert decoded_url == url
    assert decoded_name == name[:128]


def test_incy_random_iv_roundtrip_and_uniqueness() -> None:
    first = incy_crypt_link(SUB_URL, "Brand")
    second = incy_crypt_link(SUB_URL, "Brand")
    assert first != second
    assert first.startswith(INCY_CRYPT1_PREFIX) and "=" not in first and "+" not in first and "/" not in first[14:]
    assert incy_decrypt_link(first) == incy_decrypt_link(second) == (SUB_URL, "Brand")


def test_incy_plaintext_layout() -> None:
    link = incy_crypt_link(SUB_URL, "N", iv=IV0)
    wire = base64.urlsafe_b64decode(link[len(INCY_CRYPT1_PREFIX) :] + "==")
    plaintext = AESGCM(INCY_CRYPT1_KEY).decrypt(wire[:12], wire[12:], None)
    assert plaintext == b'{"n":"N","url":"https://bot.example.com/sub/abc123","v":1}'


def test_incy_name_cut_at_128_utf16_units_without_splitting_surrogates() -> None:
    name = "a" * 127 + "😀" + "tail"  # the emoji would straddle the 128-unit boundary
    _, decoded = incy_decrypt_link(incy_crypt_link(SUB_URL, name))
    assert decoded == "a" * 127
    _, exact = incy_decrypt_link(incy_crypt_link(SUB_URL, "b" * 126 + "😀" + "z"))
    assert exact == "b" * 126 + "😀"


def test_incy_decrypt_tolerates_trailing_slash() -> None:
    assert incy_decrypt_link(UPSTREAM_VECTOR + "/")[0] == "https://sub.example.com/test-vector"


@pytest.mark.parametrize(
    "bad",
    [
        "happ://crypt5/abc",
        INCY_CRYPT1_PREFIX,
        INCY_CRYPT1_PREFIX + "AAEC",
        UPSTREAM_VECTOR[:-2] + "AA",  # tag mismatch
        INCY_CRYPT1_PREFIX + "!!!!",
    ],
)
def test_incy_decrypt_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        incy_decrypt_link(bad)


def test_incy_decrypt_rejects_plaintext_without_url() -> None:
    sealed = AESGCM(INCY_CRYPT1_KEY).encrypt(IV0, b'{"v":1}', None)
    link = INCY_CRYPT1_PREFIX + base64.urlsafe_b64encode(IV0 + sealed).rstrip(b"=").decode()
    with pytest.raises(ValueError, match="missing url"):
        incy_decrypt_link(link)


def test_incy_encrypt_argument_errors() -> None:
    with pytest.raises(ValueError):
        incy_crypt_link("")
    with pytest.raises(ValueError):
        incy_crypt_link(SUB_URL, iv=b"short")


# ---------------------------------------------------------------------------------------------
# Happ crypt5
# ---------------------------------------------------------------------------------------------
# Shape of a real API answer (one manual call on 2026-11-04): standard base64 after the prefix.
CRYPT5 = "happ://crypt5/fzvdoa44q2YnAWMTXOcClGoL111iom2iBXVOQ7K6Vg/2Uof52xp+xFwWDxLKv3I4zw5RxRxJT6XxvHMcU18byQ=="


@dataclass
class FakeHappApi:
    """Local stand-in for crypto.happ.su; records requests, response configurable per test."""

    status: int = 200
    payload: Any = field(default_factory=lambda: {"encrypted_link": CRYPT5})
    raw_body: str | None = None
    delay: float = 0.0
    requests: list[Any] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raw_body is not None:
            return web.Response(status=self.status, text=self.raw_body, content_type="text/html")
        return web.json_response(self.payload, status=self.status)


@pytest.fixture(autouse=True)
def _reset_backoff() -> None:
    deeplinks.happ_api_backoff.reset()


@pytest.fixture
async def happ_api() -> AsyncIterator[tuple[FakeHappApi, str]]:
    fake = FakeHappApi()
    app = web.Application()
    app.router.add_post("/api-v2.php", fake.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield fake, str(server.make_url("/api-v2.php"))
    finally:
        await server.close()


@pytest.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def test_happ_success_is_cached_in_kv(
    session_factory: SessionFactory, http: aiohttp.ClientSession, happ_api: tuple[FakeHappApi, str]
) -> None:
    fake, url = happ_api
    assert await happ_crypt_link(session_factory, http, SUB_URL, api_url=url) == CRYPT5
    assert fake.requests == [{"url": SUB_URL}]
    assert await happ_crypt_link(session_factory, http, SUB_URL, api_url=url) == CRYPT5
    assert len(fake.requests) == 1  # served from kv
    async with session_factory() as session:
        row = await session.get(KeyValue, happ_cache_key(SUB_URL))
    assert row is not None and row.value == CRYPT5


async def test_happ_cache_is_per_url(
    session_factory: SessionFactory, http: aiohttp.ClientSession, happ_api: tuple[FakeHappApi, str]
) -> None:
    fake, url = happ_api
    await happ_crypt_link(session_factory, http, SUB_URL, api_url=url)
    await happ_crypt_link(session_factory, http, SUB_URL + "x", api_url=url)
    assert [r["url"] for r in fake.requests] == [SUB_URL, SUB_URL + "x"]
    assert happ_cache_key(SUB_URL) != happ_cache_key(SUB_URL + "x")
    assert len(happ_cache_key("https://" + "a" * 5000)) <= 64


@pytest.mark.parametrize(
    "configure",
    [
        pytest.param({"status": 500}, id="http-500"),
        pytest.param({"status": 429}, id="http-429"),
        pytest.param({"payload": {"error": "bad url"}}, id="no-link"),
        pytest.param({"payload": {"encrypted_link": "https://evil.example"}}, id="wrong-scheme"),
        pytest.param({"payload": {"encrypted_link": "happ://crypt5/a b"}}, id="whitespace"),
        pytest.param({"payload": {"encrypted_link": "happ://crypt5/" + "a" * 9000}}, id="too-long"),
        pytest.param({"payload": ["happ://crypt5/x"]}, id="not-object"),
        pytest.param({"raw_body": "<html>cloudflare</html>"}, id="html"),
    ],
)
async def test_happ_bad_responses_return_none_and_trip_backoff(
    session_factory: SessionFactory,
    http: aiohttp.ClientSession,
    happ_api: tuple[FakeHappApi, str],
    configure: dict[str, Any],
) -> None:
    fake, url = happ_api
    for attr, value in configure.items():
        setattr(fake, attr, value)
    assert await happ_crypt_link(session_factory, http, SUB_URL, api_url=url) is None
    # Backoff: the next call does not hit the API at all.
    assert await happ_crypt_link(session_factory, http, SUB_URL, api_url=url) is None
    assert len(fake.requests) == 1
    async with session_factory() as session:
        assert (await session.scalars(select(KeyValue))).all() == []


async def test_happ_recovers_after_backoff(
    session_factory: SessionFactory, http: aiohttp.ClientSession, happ_api: tuple[FakeHappApi, str]
) -> None:
    fake, url = happ_api
    fake.status = 503
    assert await happ_crypt_link(session_factory, http, SUB_URL, api_url=url) is None
    deeplinks.happ_api_backoff.reset()
    fake.status = 200
    assert await happ_crypt_link(session_factory, http, SUB_URL, api_url=url) == CRYPT5


async def test_happ_cached_value_served_even_during_backoff(
    session_factory: SessionFactory, http: aiohttp.ClientSession, happ_api: tuple[FakeHappApi, str]
) -> None:
    _, url = happ_api
    assert await happ_crypt_link(session_factory, http, SUB_URL, api_url=url) == CRYPT5
    deeplinks.happ_api_backoff.trip(60)
    assert await happ_crypt_link(session_factory, http, SUB_URL, api_url=url) == CRYPT5


async def test_happ_timeout_returns_none(
    session_factory: SessionFactory, http: aiohttp.ClientSession, happ_api: tuple[FakeHappApi, str]
) -> None:
    fake, url = happ_api
    fake.delay = 1.0
    assert await happ_crypt_link(session_factory, http, SUB_URL, api_url=url, timeout_sec=0.05) is None


async def test_happ_dropped_connection_returns_none(
    session_factory: SessionFactory, http: aiohttp.ClientSession
) -> None:
    def drop(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(drop, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await happ_crypt_link(session_factory, http, SUB_URL, api_url=f"http://127.0.0.1:{port}/api") is None
    finally:
        server.close()
        await server.wait_closed()


async def test_happ_rejects_bad_sub_url(session_factory: SessionFactory, http: aiohttp.ClientSession) -> None:
    with pytest.raises(ValueError):
        await happ_crypt_link(session_factory, http, "not-a-url")


async def test_happ_concurrent_requests_store_once(
    session_factory: SessionFactory, http: aiohttp.ClientSession, happ_api: tuple[FakeHappApi, str]
) -> None:
    fake, url = happ_api
    fake.delay = 0.05
    results = await asyncio.gather(*(happ_crypt_link(session_factory, http, SUB_URL, api_url=url) for _ in range(3)))
    assert results == [CRYPT5] * 3
    async with session_factory() as session:
        rows = (await session.scalars(select(KeyValue))).all()
    assert [r.key for r in rows] == [happ_cache_key(SUB_URL)]


async def test_store_kv_swallows_integrity_error(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent insert of the same key between our check and commit is harmless."""
    async with session_factory.begin() as session:
        session.add(KeyValue(key="k", value="first"))

    async def always_missing(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncSession.get", always_missing)
    await deeplinks._store_kv(session_factory, "k", "second")
    monkeypatch.undo()
    async with session_factory() as session:
        row = await session.get(KeyValue, "k")
    assert row is not None and row.value == "first"
