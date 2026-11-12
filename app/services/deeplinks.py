"""One-tap import links for VPN client apps, store/download URLs, and encrypted subscription links.

Telegram inline buttons accept only ``http(s)://`` and ``tg://`` URLs, so the bot opens these app schemes
through an HTTPS redirect page; the deeplinks themselves are built here.

* :data:`APPS` - supported clients with platforms, download URLs and a ``deeplink(sub_url)`` builder.
* :func:`happ_crypt_link` - ``happ://crypt5/...`` from Happ's official API (cached in the ``kv`` table,
  ``None`` when the API is unavailable; the caller falls back to ``happ://add/``).
* :func:`incy_crypt_link` - ``incy://crypt1/...``, a local port of ``@incy/link-encoder``.

Sources (checked 2026-11-04): happ.su dev-docs (crypto-link, routing, app-management), docs.incy.cc
(deep-links, subscription-format), docs.v2raytun.com (deep-link), Hiddify URL-scheme wiki, Remnawave
subscription-page templates (Streisand, Shadowrocket, v2rayNG).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import aiohttp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.exc import IntegrityError

from app.db.models import KeyValue
from app.db.session import SessionFactory, read_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------------------
# Client apps
# ---------------------------------------------------------------------------------------------
class Platform(StrEnum):
    IOS = "ios"
    ANDROID = "android"
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    APPLE_TV = "apple_tv"
    ANDROID_TV = "android_tv"


@dataclass(frozen=True, slots=True)
class Download:
    platform: Platform
    label: str  # "App Store", "Google Play", "APK", "Installer (x64)", ...
    url: str


DeeplinkBuilder = Callable[[str, str | None], str]


@dataclass(frozen=True, slots=True)
class ClientApp:
    key: str
    title: str
    downloads: tuple[Download, ...]
    builder: DeeplinkBuilder = field(repr=False)
    supports_crypto_link: bool = False

    @property
    def platforms(self) -> tuple[Platform, ...]:
        """Platforms with at least one download, in declaration order."""
        return tuple(dict.fromkeys(d.platform for d in self.downloads))

    def downloads_for(self, platform: Platform) -> list[Download]:
        return [d for d in self.downloads if d.platform == platform]

    def deeplink(self, sub_url: str, name: str | None = None) -> str:
        """Link that opens the app and imports ``sub_url``. ``name`` is used where the scheme supports it.

        Raises ``ValueError`` unless ``sub_url`` is an absolute http(s) URL without whitespace.
        """
        _check_sub_url(sub_url)
        return self.builder(sub_url, name or None)


def _check_sub_url(sub_url: str) -> None:
    if not sub_url.startswith(("https://", "http://")) or any(ch.isspace() for ch in sub_url):
        raise ValueError(f"not an absolute http(s) URL: {sub_url!r}")


def _enc(value: str) -> str:
    return quote(value, safe="")


def _with_name(link: str, name: str | None) -> str:
    return f"{link}#{_enc(name)}" if name else link


def _raw(prefix: str) -> DeeplinkBuilder:
    """``<prefix><sub_url>``: the URL is passed raw, as the apps' official examples do."""
    return lambda url, _name: f"{prefix}{url}"


def _raw_named(prefix: str) -> DeeplinkBuilder:
    return lambda url, name: _with_name(f"{prefix}{url}", name)


def _install_config(scheme: str) -> DeeplinkBuilder:
    def build(url: str, name: str | None) -> str:
        query = f"url={_enc(url)}"
        return f"{scheme}://install-config?{query}&name={_enc(name)}" if name else f"{scheme}://install-config?{query}"

    return build


_HAPP_GH = "https://github.com/Happ-proxy"
_INCY_GH = "https://github.com/INCY-DEV/incy-platforms/releases"
_HIDDIFY_GH = "https://github.com/hiddify/hiddify-app/releases/latest"

APPS: dict[str, ClientApp] = {
    app.key: app
    for app in (
        ClientApp(
            key="happ",
            title="Happ",
            builder=_raw("happ://add/"),
            supports_crypto_link=True,
            downloads=(
                Download(Platform.IOS, "App Store", "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"),
                Download(Platform.ANDROID, "APK", f"{_HAPP_GH}/happ-android/releases/latest/download/Happ.apk"),
                Download(
                    Platform.WINDOWS,
                    "Installer (x64)",
                    f"{_HAPP_GH}/happ-desktop/releases/latest/download/setup-Happ.x64.exe",
                ),
                Download(
                    Platform.WINDOWS,
                    "Installer (ARM64)",
                    f"{_HAPP_GH}/happ-desktop/releases/latest/download/setup-Happ.arm64.exe",
                ),
                Download(
                    Platform.MACOS, "DMG", f"{_HAPP_GH}/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg"
                ),
                Download(Platform.LINUX, "GitHub", f"{_HAPP_GH}/happ-desktop/releases/latest"),
                Download(
                    Platform.APPLE_TV,
                    "App Store",
                    "https://apps.apple.com/us/app/happ-proxy-utility-for-tv/id6748297274",
                ),
                Download(Platform.ANDROID_TV, "APK", f"{_HAPP_GH}/happ-android/releases/latest/download/Happ.apk"),
            ),
        ),
        ClientApp(
            key="incy",
            title="INCY",
            builder=_raw("incy://add/"),
            supports_crypto_link=True,
            downloads=(
                Download(Platform.IOS, "App Store", "https://apps.apple.com/us/app/incy/id6756943388"),
                Download(
                    Platform.ANDROID, "Google Play", "https://play.google.com/store/apps/details?id=llc.itdev.incy"
                ),
                Download(Platform.ANDROID, "APK", f"{_INCY_GH}/download/latest/Incy.apk"),
                Download(Platform.WINDOWS, "Installer", f"{_INCY_GH}/download/latest/incy-windows-setup.exe"),
                Download(Platform.MACOS, "App Store", "https://apps.apple.com/us/app/incy/id6756943388"),
                Download(Platform.MACOS, "DMG (Apple Silicon)", f"{_INCY_GH}/download/latest/incy-macos-arm64.dmg"),
                Download(Platform.MACOS, "DMG (Intel)", f"{_INCY_GH}/download/latest/incy-macos-intel.dmg"),
                Download(Platform.LINUX, "GitHub", f"{_INCY_GH}/latest"),
                Download(Platform.APPLE_TV, "App Store", "https://apps.apple.com/us/app/incy/id6756943388"),
                Download(
                    Platform.ANDROID_TV, "Google Play", "https://play.google.com/store/apps/details?id=llc.itdev.incy"
                ),
            ),
        ),
        ClientApp(
            key="v2raytun",
            title="v2RayTun",
            builder=_raw("v2raytun://import/"),
            downloads=(
                Download(Platform.IOS, "App Store", "https://apps.apple.com/us/app/v2raytun/id6476628951"),
                Download(
                    Platform.ANDROID,
                    "Google Play",
                    "https://play.google.com/store/apps/details?id=com.v2raytun.android",
                ),
            ),
        ),
        ClientApp(
            key="hiddify",
            title="Hiddify",
            builder=_raw_named("hiddify://import/"),
            downloads=(
                Download(Platform.IOS, "App Store", "https://apps.apple.com/us/app/hiddify-proxy-vpn/id6596777532"),
                Download(
                    Platform.ANDROID, "Google Play", "https://play.google.com/store/apps/details?id=app.hiddify.com"
                ),
                Download(Platform.ANDROID, "GitHub", _HIDDIFY_GH),
                Download(Platform.WINDOWS, "GitHub", _HIDDIFY_GH),
                Download(Platform.MACOS, "App Store", "https://apps.apple.com/us/app/hiddify-proxy-vpn/id6596777532"),
                Download(Platform.LINUX, "GitHub", _HIDDIFY_GH),
            ),
        ),
        ClientApp(
            key="streisand",
            title="Streisand",
            builder=_raw_named("streisand://import/"),
            downloads=(
                Download(Platform.IOS, "App Store", "https://apps.apple.com/us/app/streisand/id6450534064"),
                Download(Platform.MACOS, "App Store", "https://apps.apple.com/us/app/streisand/id6450534064"),
            ),
        ),
        ClientApp(
            key="shadowrocket",
            title="Shadowrocket",
            builder=_raw_named("shadowrocket://add/"),
            downloads=(Download(Platform.IOS, "App Store", "https://apps.apple.com/us/app/shadowrocket/id932747118"),),
        ),
        ClientApp(
            key="v2rayng",
            title="v2rayNG",
            builder=_install_config("v2rayng"),
            downloads=(
                Download(
                    Platform.ANDROID, "Google Play", "https://play.google.com/store/apps/details?id=com.v2ray.ang"
                ),
                Download(Platform.ANDROID, "GitHub", "https://github.com/2dust/v2rayNG/releases/latest"),
            ),
        ),
    )
}


def get_app(key: str) -> ClientApp | None:
    return APPS.get(key.lower())


def apps_for_platform(platform: Platform, apps: Iterable[ClientApp] | None = None) -> list[ClientApp]:
    """Apps that can be installed on ``platform``, in registry order (recommended apps first)."""
    return [app for app in (apps if apps is not None else APPS.values()) if platform in app.platforms]


# ---------------------------------------------------------------------------------------------
# Happ crypt5 (official API)
# ---------------------------------------------------------------------------------------------
HAPP_CRYPTO_API = "https://crypto.happ.su/api-v2.php"
HAPP_CRYPT_PREFIX = "happ://crypt"
HAPP_API_TIMEOUT_SEC = 10.0
HAPP_API_BACKOFF_SEC = 60.0  # after a failure, do not call the API again for this long
_HAPP_CACHE_PREFIX = "happ_crypt5:"
_MAX_CRYPT_LINK_LEN = 8192


class _Backoff:
    """Process-wide pause after an API failure so a dead endpoint is not hit on every request."""

    def __init__(self) -> None:
        self.until = 0.0

    def active(self) -> bool:
        return time.monotonic() < self.until

    def trip(self, seconds: float) -> None:
        self.until = time.monotonic() + seconds

    def reset(self) -> None:
        self.until = 0.0


happ_api_backoff = _Backoff()


def happ_cache_key(sub_url: str) -> str:
    """``kv`` key for the cached crypt link (fits ``KeyValue.key``, 64 chars)."""
    return _HAPP_CACHE_PREFIX + hashlib.sha256(sub_url.encode()).hexdigest()[:48]


async def happ_crypt_link(
    session_factory: SessionFactory,
    http: aiohttp.ClientSession,
    sub_url: str,
    *,
    api_url: str = HAPP_CRYPTO_API,
    timeout_sec: float = HAPP_API_TIMEOUT_SEC,
) -> str | None:
    """Encrypted ``happ://crypt5/...`` link for ``sub_url``, or ``None`` if it cannot be obtained now.

    The result is cached forever in the ``kv`` table (the link is deterministic for a URL; a rotated
    subscription token yields a new URL and thus a new entry). Never raises on network/API errors; after a
    failure the API is not called again for ``HAPP_API_BACKOFF_SEC``. No DB transaction is held during the
    HTTP request.
    """
    _check_sub_url(sub_url)
    key = happ_cache_key(sub_url)
    async with read_session(session_factory) as session:
        cached = await session.get(KeyValue, key)
        if cached is not None:
            return cached.value
    if happ_api_backoff.active():
        return None
    link = await _request_happ_crypt_link(http, sub_url, api_url, timeout_sec)
    if link is None:
        happ_api_backoff.trip(HAPP_API_BACKOFF_SEC)
        return None
    await _store_kv(session_factory, key, link)
    return link


async def _request_happ_crypt_link(
    http: aiohttp.ClientSession, sub_url: str, api_url: str, timeout_sec: float
) -> str | None:
    try:
        request_timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with http.post(api_url, json={"url": sub_url}, timeout=request_timeout) as response:
            if response.status != 200:
                logger.warning("Happ crypto API returned HTTP %s", response.status)
                return None
            data: Any = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.warning("Happ crypto API request failed: %s", exc.__class__.__name__)
        return None
    link = data.get("encrypted_link") if isinstance(data, dict) else None
    if not _valid_happ_link(link):
        logger.warning("Happ crypto API returned an unexpected payload")
        return None
    return str(link)


def _valid_happ_link(link: object) -> bool:
    return (
        isinstance(link, str)
        and link.startswith(HAPP_CRYPT_PREFIX)
        and len(link) <= _MAX_CRYPT_LINK_LEN
        and not any(ch.isspace() for ch in link)
    )


async def _store_kv(session_factory: SessionFactory, key: str, value: str) -> None:
    """Insert-if-absent; a concurrent writer wins harmlessly (both values are valid)."""
    try:
        async with session_factory.begin() as session:
            if await session.get(KeyValue, key) is None:
                session.add(KeyValue(key=key, value=value))
    except IntegrityError:
        logger.debug("kv key %s was stored concurrently", key)


# ---------------------------------------------------------------------------------------------
# INCY crypt1 (port of @incy/link-encoder 1.3.0)
# ---------------------------------------------------------------------------------------------
# Ported from the npm package @incy/link-encoder@1.3.0 (MIT, https://github.com/INCY-DEV/incy-link-encoder,
# tarball integrity sha512-Fa/We3cDwKaS/LhfMP3LWq8rZ6p2APiSc5v08I7XLJXvM3uZPDdg1J5x038VGgojrB89pevyF2uozKC7y1NezA==).
# K = SHA-256("incy" "deep" "crypt1" "v2026.06" || keymatA[1024:1056] || keymatB[2048:2080]); the vendor
# publishes SHA-256(K) as KEY_FINGERPRINT. The key is shared with every INCY client and is public by
# design: crypt1 is obfuscation against chat scanners, not secrecy.
# Wire format: "incy://crypt1/" + base64url_nopad(iv[12] || ciphertext || tag[16]);
# plaintext: compact JSON with sorted keys {"n": name[:128], "url": url, "v": 1} ("n" only if non-empty).
INCY_CRYPT1_PREFIX = "incy://crypt1/"
INCY_CRYPT1_KEY = bytes.fromhex("f6d40ea0c8a8899d7c682d09ba0d4165dfe2b3dd45e6bb3e25cb233cf00c2462")
INCY_KEY_FINGERPRINT = "b6bf708471cc90043232967660aade86a50b4e57929db2e53c5fa34db624c08c"
INCY_NAME_MAX_UTF16 = 128
_IV_LEN = 12
_TAG_LEN = 16


def incy_crypt_link(sub_url: str, name: str | None = None, *, iv: bytes | None = None) -> str:
    """Encrypted ``incy://crypt1/...`` deeplink, byte-compatible with ``@incy/link-encoder``.

    ``iv`` is for deterministic tests only; production calls use a fresh random 12-byte IV.
    Raises ``ValueError`` for an empty URL or an IV of the wrong length.
    """
    if not sub_url:
        raise ValueError("sub_url must be a non-empty string")
    nonce = os.urandom(_IV_LEN) if iv is None else iv
    if len(nonce) != _IV_LEN:
        raise ValueError("iv must be 12 bytes")
    sealed = AESGCM(INCY_CRYPT1_KEY).encrypt(nonce, _incy_plaintext(sub_url, name), None)
    return INCY_CRYPT1_PREFIX + _b64url(nonce + sealed)


def incy_decrypt_link(link: str) -> tuple[str, str | None]:
    """Inverse of :func:`incy_crypt_link`: ``(url, name)``. Raises ``ValueError`` on any malformed input."""
    if not link.startswith(INCY_CRYPT1_PREFIX):
        raise ValueError(f"expected {INCY_CRYPT1_PREFIX} prefix")
    payload = link[len(INCY_CRYPT1_PREFIX) :].rstrip("/")
    try:
        wire = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    except ValueError as exc:
        raise ValueError("payload is not base64url") from exc
    if len(wire) < _IV_LEN + _TAG_LEN + 1:
        raise ValueError("payload too short")
    try:
        plaintext = AESGCM(INCY_CRYPT1_KEY).decrypt(wire[:_IV_LEN], wire[_IV_LEN:], None)
        data = json.loads(plaintext)
    except Exception as exc:  # InvalidTag, UnicodeDecodeError, JSONDecodeError
        raise ValueError("authentication or decoding failed") from exc
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url:
        raise ValueError("missing url field")
    name = data.get("n")
    return url, name if isinstance(name, str) and name else None


def incy_key_fingerprint() -> str:
    """SHA-256 of the embedded key; equals the vendor's published ``KEY_FINGERPRINT``."""
    return hashlib.sha256(INCY_CRYPT1_KEY).hexdigest()


def _incy_plaintext(url: str, name: str | None) -> bytes:
    payload: dict[str, Any] = {"url": url, "v": 1}
    if name:
        payload["n"] = _utf16_prefix(name, INCY_NAME_MAX_UTF16)
    # JSON.stringify-compatible: compact, raw UTF-8, keys sorted (n < url < v).
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _utf16_prefix(text: str, units: int) -> str:
    """First ``units`` UTF-16 code units, like JS ``String.slice``, without splitting a surrogate pair.

    (JS would keep a lone high surrogate at the cut; dropping it is the only divergence from the npm
    package and only affects names longer than 128 units that are cut inside an emoji.)
    """
    out: list[str] = []
    used = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > units:
            break
        out.append(char)
        used += width
    return "".join(out)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
