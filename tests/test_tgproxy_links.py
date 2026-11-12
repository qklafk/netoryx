"""MTProto secrets and proxy links."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.panels.mtproto import (
    ProxyLink,
    fake_tls_secret,
    is_ad_tag,
    is_key,
    link_key,
    new_key,
    parse_proxy_link,
    parse_secret,
    secure_secret,
    web_proxy_url,
)

KEY = "0123456789abcdef0123456789abcdef"


def test_new_key_is_random_hex() -> None:
    a, b = new_key(), new_key()
    assert is_key(a) and is_key(b) and a != b and len(a) == 32


@pytest.mark.parametrize("value", ["", "xyz", KEY[:-1], KEY + "0", 42, None, "g" * 32])
def test_is_key_rejects_garbage(value: object) -> None:
    assert not is_key(value)


def test_is_key_accepts_upper_case_and_ad_tags() -> None:
    assert is_key(KEY.upper())
    assert is_ad_tag(KEY) and not is_ad_tag("abc")


def test_fake_tls_secret_layout() -> None:
    secret = fake_tls_secret(KEY.upper(), "petrovich.ru")
    assert secret == "ee" + KEY + b"petrovich.ru".hex()
    assert parse_secret(secret) == parse_secret(secret.upper())
    parsed = parse_secret(secret)
    assert parsed is not None and parsed.mode == "tls" and parsed.key == KEY and parsed.domain == "petrovich.ru"


@pytest.mark.parametrize("domain", ["", "bad domain", "домен.рф", "a" * 300])
def test_fake_tls_secret_rejects_bad_domains(domain: str) -> None:
    with pytest.raises(ValueError):
        fake_tls_secret(KEY, domain)


def test_secret_requires_a_key() -> None:
    with pytest.raises(ValueError):
        secure_secret("nope")
    with pytest.raises(ValueError):
        fake_tls_secret("nope", "example.com")


def test_parse_classic_secure_and_base64() -> None:
    assert parse_secret(KEY) is not None and parse_secret(KEY).mode == "classic"  # type: ignore[union-attr]
    secure = parse_secret(secure_secret(KEY))
    assert secure is not None and secure.mode == "secure" and secure.key == KEY
    raw = bytes.fromhex(fake_tls_secret(KEY, "wb.ru"))
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    parsed = parse_secret(b64)
    assert parsed is not None and parsed.domain == "wb.ru" and parsed.key == KEY


@pytest.mark.parametrize("secret", ["", "zz", "ee" + KEY, "aa" + KEY, "ee" + KEY + "ff", "dd" + KEY + "00"])
def test_parse_secret_rejects_malformed(secret: str) -> None:
    assert parse_secret(secret) is None


def test_link_urls_are_escaped_and_round_trip() -> None:
    link = ProxyLink("proxy.example.com", 443, fake_tls_secret(KEY, "petrovich.ru"))
    assert link.tg_url.startswith("tg://proxy?server=proxy.example.com&port=443&secret=ee")
    assert link.tme_url.startswith("https://t.me/proxy?server=proxy.example.com&port=443&secret=ee")
    assert parse_proxy_link(link.tg_url) == link
    assert parse_proxy_link(link.tme_url) == link
    assert link.domain == "petrovich.ru" and link_key(link) == KEY
    odd = ProxyLink("h&st", 8443, KEY)
    assert parse_qs(urlsplit(odd.tme_url).query)["server"] == ["h&st"]
    assert ProxyLink.from_json(link.to_json()) == link


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/proxy?server=a&port=1&secret=" + KEY,
        "tg://socks?server=a&port=1",
        "tg://proxy?server=a&secret=" + KEY,
        "tg://proxy?server=a&port=x&secret=" + KEY,
        "tg://proxy?server=a&port=70000&secret=" + KEY,
        "tg://proxy?port=443&secret=" + KEY,
        "mailto:x",
    ],
)
def test_parse_proxy_link_rejects_incomplete(url: str) -> None:
    assert parse_proxy_link(url) is None


@pytest.mark.parametrize("data", [None, [], {"server": "a", "port": True, "secret": KEY}, {"server": "", "port": 1}])
def test_link_from_json_rejects_garbage(data: object) -> None:
    assert ProxyLink.from_json(data) is None


def test_web_proxy_url_uses_secure_secret_without_port() -> None:
    url = web_proxy_url("tg.example.com", KEY)
    assert url == f"tg://webproxy?server=tg.example.com&secret=dd{KEY}"
