"""Tests for app.services.payments.base: money conversions, IP resolution, the HTTP helper."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.services.payments.base import (
    GatewayError,
    HttpGateway,
    WebhookRejected,
    amount_to_kop,
    client_ip,
    get_header,
    ip_in,
    json_body,
    kop_to_number,
    kop_to_plain,
    kop_to_str,
    parse_ip,
    parse_networks,
    require,
)

PROXIES = parse_networks(["127.0.0.1", "::1", "10.0.0.0/8", "172.16.0.0/12"])


# ---------------------------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kop", "as_str", "plain", "number"),
    [
        (19900, "199.00", "199", 199),
        (19950, "199.50", "199.5", 199.5),
        (19901, "199.01", "199.01", 199.01),
        (1, "0.01", "0.01", 0.01),
        (0, "0.00", "0", 0),
        (10_000_000, "100000.00", "100000", 100000),
    ],
)
def test_kop_formats(kop: int, as_str: str, plain: str, number: float) -> None:
    assert kop_to_str(kop) == as_str
    assert kop_to_plain(kop) == plain
    assert kop_to_number(kop) == number
    assert isinstance(kop_to_number(19900), int)


@pytest.mark.parametrize(
    ("value", "kop"),
    [
        ("199.00", 19900),
        ("199", 19900),
        ("199.5", 19950),
        (" 199.50 ", 19950),
        ("199.999", 19999),  # fractions of a kopeck are rounded down
        (199, 19900),
        (199.5, 19950),
        (Decimal("0.01"), 1),
        ("0", 0),
    ],
)
def test_amount_to_kop(value: object, kop: int) -> None:
    assert amount_to_kop(value) == kop  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", "abc", "-1", "NaN", "Infinity", "-0.01", True, "1,5"])
def test_amount_to_kop_rejects(value: object) -> None:
    with pytest.raises(ValueError):
        amount_to_kop(value)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# Headers / IPs
# ---------------------------------------------------------------------------------------------
def test_get_header_is_case_insensitive() -> None:
    headers = {"X-Secret": "a", "content-type": "b"}
    assert get_header(headers, "x-secret") == "a"
    assert get_header(headers, "Content-Type") == "b"
    assert get_header(headers, "missing") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.2.3.4", "1.2.3.4"),
        (" 1.2.3.4 ", "1.2.3.4"),
        ("1.2.3.4:8080", "1.2.3.4"),
        ("::1", "::1"),
        ("[2a02:5180::1]:443", "2a02:5180::1"),
        ("::ffff:185.71.76.1", "185.71.76.1"),
        ("not-an-ip", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_ip(raw: str | None, expected: str | None) -> None:
    ip = parse_ip(raw)
    assert (str(ip) if ip else None) == expected


def test_parse_networks_skips_invalid() -> None:
    nets = parse_networks(["10.0.0.0/8", "garbage", "2a02:5180::/32", "1.2.3.4"])
    assert [str(n) for n in nets] == ["10.0.0.0/8", "2a02:5180::/32", "1.2.3.4/32"]


def test_ip_in_handles_families() -> None:
    nets = parse_networks(["10.0.0.0/8", "2a02:5180::/32"])
    assert ip_in(parse_ip("10.1.2.3"), nets)
    assert ip_in(parse_ip("2a02:5180:1::5"), nets)
    assert not ip_in(parse_ip("11.0.0.1"), nets)
    assert not ip_in(parse_ip("2a02:5181::1"), nets)
    assert not ip_in(None, nets)


@pytest.mark.parametrize(
    ("remote", "headers", "expected"),
    [
        # direct (untrusted) peer: forwarded headers are ignored
        ("8.8.8.8", {"X-Forwarded-For": "185.71.76.1"}, "8.8.8.8"),
        # trusted proxy: rightmost untrusted hop wins
        ("127.0.0.1", {"X-Forwarded-For": "185.71.76.1"}, "185.71.76.1"),
        ("10.0.0.2", {"X-Forwarded-For": "1.1.1.1, 185.71.76.1, 10.0.0.5"}, "185.71.76.1"),
        # a client prepending a fake entry cannot spoof
        ("127.0.0.1", {"X-Forwarded-For": "185.71.76.1, 6.6.6.6"}, "6.6.6.6"),
        # only proxies in the chain: the leftmost is the client
        ("127.0.0.1", {"X-Forwarded-For": "10.0.0.7, 10.0.0.8"}, "10.0.0.7"),
        # X-Real-IP as a fallback, from a trusted proxy only
        ("::1", {"X-Real-IP": "2a02:5180::7"}, "2a02:5180::7"),
        ("9.9.9.9", {"X-Real-IP": "2a02:5180::7"}, "9.9.9.9"),
        # trusted proxy without headers: the proxy itself
        ("127.0.0.1", {}, "127.0.0.1"),
        # garbage in the chain -> unresolvable
        ("127.0.0.1", {"X-Forwarded-For": "1.1.1.1, garbage"}, None),
        (None, {"X-Forwarded-For": "185.71.76.1"}, None),
        ("::ffff:127.0.0.1", {"x-forwarded-for": "185.71.76.9"}, "185.71.76.9"),
    ],
)
def test_client_ip(remote: str | None, headers: dict[str, str], expected: str | None) -> None:
    ip = client_ip(remote, headers, PROXIES)
    assert (str(ip) if ip else None) == expected


def test_require_and_json_body() -> None:
    assert require({"a": {"b": 1}}, "a", "b") == 1
    with pytest.raises(GatewayError):
        require({"a": 1}, "a", "b")
    with pytest.raises(GatewayError):
        require(None, "a")
    assert json_body(b'{"x": 1}') == {"x": 1}
    for raw in (b"not json", b"[1, 2]", b"\xff\xfe"):
        with pytest.raises(WebhookRejected) as exc:
            json_body(raw)
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------------------------
# HttpGateway
# ---------------------------------------------------------------------------------------------
class _Flaky:
    """Fails with HTTP 503 ``failures`` times, then answers ``body``."""

    def __init__(self, failures: int, body: str = '{"ok": true}', status: int = 200) -> None:
        self.failures = failures
        self.body = body
        self.status = status
        self.calls = 0

    async def __call__(self, request: web.Request) -> web.Response:
        self.calls += 1
        if self.calls <= self.failures:
            return web.Response(status=503, text="busy")
        return web.Response(status=self.status, text=self.body, content_type="application/json")


@pytest.fixture
async def flaky_server() -> AsyncIterator[tuple[_Flaky, str]]:
    handler = _Flaky(failures=0)
    app = web.Application()

    async def route(request: web.Request) -> web.Response:
        return await handler(request)

    app.router.add_route("*", "/{tail:.*}", route)
    server = TestServer(app)
    await server.start_server()
    try:
        yield handler, str(server.make_url("/x"))
    finally:
        await server.close()


async def test_request_retries_transient_errors(flaky_server: tuple[_Flaky, str]) -> None:
    handler, url = flaky_server
    handler.failures = 2
    gw = HttpGateway(timeout=5, retry_delay=0)
    try:
        assert await gw._request_json("GET", url, retries=2) == {"ok": True}
        assert handler.calls == 3
    finally:
        await gw.close()


async def test_request_without_retries_fails_fast(flaky_server: tuple[_Flaky, str]) -> None:
    handler, url = flaky_server
    handler.failures = 1
    gw = HttpGateway(timeout=5, retry_delay=0)
    try:
        with pytest.raises(GatewayError, match="503"):
            await gw._request_json("POST", url)
        assert handler.calls == 1
    finally:
        await gw.close()


async def test_request_errors(flaky_server: tuple[_Flaky, str]) -> None:
    handler, url = flaky_server
    gw = HttpGateway(timeout=5, retry_delay=0)
    try:
        handler.body, handler.status = "nope", 200
        with pytest.raises(GatewayError, match="invalid JSON"):
            await gw._request_json("GET", url)
        handler.body, handler.status = '{"error": "x"}', 404
        with pytest.raises(GatewayError, match="HTTP 404"):
            await gw._request_json("GET", url + "?token=secret")
        handler.body, handler.status = "", 200
        assert await gw._request_json("GET", url) is None
    finally:
        await gw.close()


async def test_error_messages_hide_query_strings(flaky_server: tuple[_Flaky, str]) -> None:
    handler, url = flaky_server
    handler.body, handler.status = "{}", 400
    gw = HttpGateway(timeout=5)
    try:
        with pytest.raises(GatewayError) as exc:
            await gw._request_json("GET", url + "?token=secret")
        assert "secret" not in str(exc.value)
    finally:
        await gw.close()


async def test_connection_error_is_gateway_error() -> None:
    with socket.socket() as sock:  # grab a free port and close it -> connection refused
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    gw = HttpGateway(timeout=2, retry_delay=0)
    try:
        with pytest.raises(GatewayError):
            await gw._request_json("GET", f"http://127.0.0.1:{port}/", retries=1)
    finally:
        await gw.close()


async def test_close_is_idempotent_and_session_recreated(flaky_server: tuple[_Flaky, str]) -> None:
    _, url = flaky_server
    gw = HttpGateway(timeout=5)
    await gw.close()
    assert await gw._request_json("GET", url) == {"ok": True}
    await gw.close()
    await gw.close()
    assert await gw._request_json("GET", url) == {"ok": True}
    await gw.close()


@pytest.mark.parametrize(
    ("remote", "headers", "expected"),
    [
        # both headers present and disagreeing: one of them is client-supplied -> unresolvable
        ("127.0.0.1", {"X-Forwarded-For": "185.71.76.1", "X-Real-IP": "203.0.113.9"}, None),
        ("127.0.0.1", {"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "185.71.76.1"}, None),
        ("172.17.0.1", {"X-Forwarded-For": "6.6.6.6, 185.71.76.1", "X-Real-IP": "203.0.113.9"}, None),
        # both set by the proxy: they agree
        ("127.0.0.1", {"X-Forwarded-For": "185.71.76.1, 203.0.113.9", "X-Real-IP": "203.0.113.9"}, "203.0.113.9"),
        # X-Real-IP naming an inner trusted proxy carries no client information
        ("10.0.0.2", {"X-Forwarded-For": "203.0.113.9, 10.0.0.1", "X-Real-IP": "10.0.0.1"}, "203.0.113.9"),
        ("10.0.0.2", {"X-Real-IP": "10.0.0.1"}, "10.0.0.2"),
        # garbage X-Real-IP next to a valid chain -> unresolvable
        ("127.0.0.1", {"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "garbage"}, None),
    ],
)
def test_client_ip_conflicting_forwarding_headers(remote: str, headers: dict[str, str], expected: str | None) -> None:
    ip = client_ip(remote, headers, PROXIES)
    assert (str(ip) if ip else None) == expected


@pytest.mark.parametrize("status", [200, 403, 502])
async def test_non_utf8_response_is_gateway_error(status: int) -> None:
    """A WAF / proxy error page in cp1251 without a charset must not escape as UnicodeDecodeError."""
    page = "<h1>Доступ запрещён</h1>".encode("cp1251")
    app = web.Application()

    async def route(request: web.Request) -> web.Response:
        return web.Response(status=status, body=page, headers={"Content-Type": "text/html"})

    app.router.add_route("*", "/{tail:.*}", route)
    server = TestServer(app)
    await server.start_server()
    gw = HttpGateway(timeout=5, retry_delay=0)
    try:
        with pytest.raises(GatewayError):
            await gw._request_json("GET", str(server.make_url("/x")))
    finally:
        await gw.close()
        await server.close()
