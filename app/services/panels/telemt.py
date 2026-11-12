"""telemt MTProto proxy backend (https://github.com/telemt/telemt), Control API v1.

One telemt user per account (target is always ``None``). The API answers ``{"ok": true, "data": ..., "revision"}``
or ``{"ok": false, "error": {"code", "message"}}``; the ``Authorization`` header must equal the server's
``[server.api] auth_header`` byte for byte, and the caller's IP must be in ``[server.api] whitelist``.

Mapping of the generic :class:`~app.services.panels.base.PanelBackend` calls:

* ``upsert_account``  -> ``POST /v1/users`` or a minimal ``PATCH /v1/users/{name}`` (only changed fields, so
  metadata edits never drop live sessions; the secret is sent only when it really changes);
* ``delete_account``  -> ``DELETE`` ("not found" is success; telemt refuses to delete its last user, that user
  is disabled instead);
* ``get_links``       -> the ``tg://proxy`` links telemt derives from its config;
* ``reset_devices``   -> disable + enable (telemt closes the user's sessions, which frees its unique-IP slots);
* ``reset_traffic``   -> ``POST /v1/users/{name}/reset-quota``.

``device_limit`` maps to ``max_unique_ips`` and ``traffic_limit_bytes`` to ``data_quota_bytes``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import quote

import aiohttp

from app.db.models import PanelKind
from app.services.panels.base import (
    AccountSpec,
    InboundInfo,
    PanelAuthError,
    PanelError,
    RemoteAccount,
)
from app.services.panels.mtproto import (
    ProxyAccount,
    ProxyLink,
    ProxyUserSpec,
    is_ad_tag,
    is_key,
    link_key,
    parse_proxy_link,
)

logger = logging.getLogger(__name__)

USERNAME_RE: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_API: Final = "/v1"
_NETWORK_RETRIES: Final = 2
_RETRY_BASE_DELAY: Final = 0.5
_MAX_ERROR_BODY: Final = 200
_EXPIRY_TOLERANCE: Final = timedelta(seconds=1)


class TelemtApiError(PanelError):
    """telemt answered ``ok: false`` (or a non-2xx status)."""

    def __init__(self, method: str, path: str, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"telemt {method} {path}: HTTP {status} {code}: {message}".rstrip(": "))

    @property
    def not_found(self) -> bool:
        return self.status == 404 or self.code == "not_found"


# ---------------------------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------------------------
def to_rfc3339(value: datetime) -> str:
    """Aware datetime -> ``2026-12-01T10:00:00Z`` (whole seconds, UTC)."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_rfc3339(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _str_list(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def check_username(name: str) -> str:
    """telemt usernames are ``[A-Za-z0-9_.-]{1,64}``."""
    if not USERNAME_RE.fullmatch(name):
        raise PanelError(f"invalid telemt username {name!r}")
    return name


def user_links(user: Mapping[str, Any]) -> list[ProxyLink]:
    """Every native link of a ``UserInfo`` (Fake-TLS first, then extra TLS domains, secure, classic), de-duplicated."""
    links = user.get("links")
    if not isinstance(links, Mapping):
        return []
    urls = _str_list(links.get("tls"))
    for item in links.get("tls_domains") or []:
        if isinstance(item, Mapping) and isinstance(item.get("link"), str):
            urls.append(item["link"])
    urls += _str_list(links.get("secure")) + _str_list(links.get("classic"))
    result: list[ProxyLink] = []
    for url in urls:
        link = parse_proxy_link(url)
        if link is not None and link not in result:
            result.append(link)
    return result


def _override_host(links: list[ProxyLink], host: str | None, port: int | None) -> list[ProxyLink]:
    if not host and not port:
        return links
    result: list[ProxyLink] = []
    for link in links:
        moved = ProxyLink(host or link.server, port or link.port, link.secret)
        if moved not in result:
            result.append(moved)
    return result


def _rate(user: Mapping[str, Any]) -> int:
    return max(_int(user.get("rate_limit_down_bps")), _int(user.get("rate_limit_up_bps")))


def _same_expiry(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= _EXPIRY_TOLERANCE


def _create_body(spec: ProxyUserSpec) -> dict[str, Any]:
    body: dict[str, Any] = {"username": spec.name}
    if spec.key:
        body["secret"] = spec.key.lower()
    if not spec.enabled:
        body["enabled"] = False
    if spec.expires_at is not None:
        body["expiration_rfc3339"] = to_rfc3339(spec.expires_at)
    if spec.max_unique_ips > 0:
        body["max_unique_ips"] = spec.max_unique_ips
    if spec.traffic_limit_bytes > 0:
        body["data_quota_bytes"] = spec.traffic_limit_bytes
    if spec.rate_limit_bps > 0:
        body["rate_limit_up_bps"] = body["rate_limit_down_bps"] = spec.rate_limit_bps
    if spec.ad_tag:
        body["user_ad_tag"] = spec.ad_tag.lower()
    return body


def _limit_patch(patch: dict[str, Any], field: str, current: Any, desired: int) -> None:
    if _int(current) != max(0, desired):
        patch[field] = desired if desired > 0 else None


def user_patch(user: Mapping[str, Any], spec: ProxyUserSpec) -> dict[str, Any]:
    """The minimal ``PatchUserRequest`` turning ``user`` into ``spec`` (empty if nothing changes)."""
    patch: dict[str, Any] = {}
    if bool(user.get("enabled", True)) != spec.enabled:
        patch["enabled"] = spec.enabled
    if not _same_expiry(from_rfc3339(user.get("expiration_rfc3339")), spec.expires_at):
        patch["expiration_rfc3339"] = to_rfc3339(spec.expires_at) if spec.expires_at else None
    _limit_patch(patch, "max_unique_ips", user.get("max_unique_ips"), spec.max_unique_ips)
    if _int(user.get("data_quota_bytes")) != max(0, spec.traffic_limit_bytes):
        patch["data_quota_bytes"] = spec.traffic_limit_bytes if spec.traffic_limit_bytes > 0 else None
        if spec.traffic_limit_bytes <= 0 and user.get("quota_period"):
            patch["quota_period"] = None  # a period needs a quota
    for direction in ("up", "down"):
        name = f"rate_limit_{direction}_bps"
        _limit_patch(patch, name, user.get(name), spec.rate_limit_bps)
    current_tag = user.get("user_ad_tag")
    current_tag = current_tag.lower() if isinstance(current_tag, str) and current_tag else None
    desired_tag = spec.ad_tag.lower() if spec.ad_tag else None
    if current_tag != desired_tag:
        patch["user_ad_tag"] = desired_tag
    if spec.key:
        links = user_links(user)
        current_key = link_key(links[0]) if links else None
        if current_key != spec.key.lower():
            patch["secret"] = spec.key.lower()  # unknown current key: sending the same one changes nothing
    return patch


# ---------------------------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------------------------
class TelemtBackend:
    """:class:`~app.services.panels.base.PanelBackend` + :class:`~app.services.panels.mtproto.MtprotoProxyBackend`
    for one telemt instance. Targets are always ``None``.

    ``public_host`` / ``public_port`` override the host and port of the links telemt reports (useful when its
    auto-detected address is wrong, e.g. behind NAT or a relay).
    """

    kind = PanelKind.TELEMT

    def __init__(
        self,
        url: str,
        *,
        auth_header: str | None = None,
        tls_verify: bool = True,
        timeout: float = 15,
        extra_headers: Mapping[str, str] | None = None,
        public_host: str | None = None,
        public_port: int | None = None,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("telemt API URL must start with http:// or https://")
        self.base = url.rstrip("/")
        self._auth = auth_header or None
        self._tls_verify = tls_verify
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._extra_headers = dict(extra_headers or {})
        self.public_host = public_host or None
        self.public_port = public_port or None
        self._http: aiohttp.ClientSession | None = None
        self._closed = False
        self.version: str | None = None

    def __repr__(self) -> str:
        return f"TelemtBackend({self.base!r})"

    # --- transport -------------------------------------------------------------------------------
    def _session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise PanelError(f"telemt {self.base}: backend is closed")
        if self._http is None or self._http.closed:
            headers = {"Accept": "application/json", **self._extra_headers}
            if self._auth:
                headers["Authorization"] = self._auth
            connector = aiohttp.TCPConnector(ssl=False) if not self._tls_verify else None
            self._http = aiohttp.ClientSession(connector=connector, timeout=self._timeout, headers=headers)
        return self._http

    async def close(self) -> None:
        self._closed = True
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    async def _send(self, method: str, path: str, body: Any) -> tuple[int, bytes]:
        attempt = 0
        while True:
            try:
                async with self._session().request(method, self.base + path, json=body) as resp:
                    return resp.status, await resp.read()
            except (aiohttp.ClientError, TimeoutError) as exc:
                # Nothing was sent on a connect error, so a mutation may be repeated too.
                retryable = method == "GET" or isinstance(exc, aiohttp.ClientConnectorError)
                if not retryable or attempt >= _NETWORK_RETRIES:
                    detail = str(exc) or "timed out"
                    raise PanelError(f"telemt {method} {path}: {type(exc).__name__}: {detail}") from exc
                await asyncio.sleep(_RETRY_BASE_DELAY * 2**attempt)
                attempt += 1

    async def request(self, method: str, path: str, body: Any = None) -> Any:
        """Call the API and return ``data``. Raises PanelAuthError / TelemtApiError / PanelError."""
        full = _API + path
        status, raw = await self._send(method, full, body)
        try:
            envelope = json.loads(raw) if raw else None
        except ValueError:
            envelope = None
        if not isinstance(envelope, dict) or "ok" not in envelope:
            snippet = raw[:_MAX_ERROR_BODY].decode("utf-8", "replace").strip()
            if status in (401, 403):
                raise PanelAuthError(f"telemt {full}: HTTP {status}")
            raise PanelError(f"telemt {method} {full}: HTTP {status}, not a Control API answer: {snippet!r}")
        if envelope.get("ok") is True and 200 <= status < 300:
            return envelope.get("data")
        error_obj = envelope.get("error")
        error: dict[str, Any] = error_obj if isinstance(error_obj, dict) else {}
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
        if status == 401 or code == "unauthorized":
            raise PanelAuthError(f"telemt {full}: unauthorized (check the API auth header)")
        if status == 403 and code == "forbidden":
            raise PanelAuthError(f"telemt {full}: forbidden (the bot's IP is not in [server.api] whitelist)")
        raise TelemtApiError(method, full, status, code, message)

    # --- users -----------------------------------------------------------------------------------
    @staticmethod
    def _user_path(name: str, action: str = "") -> str:
        path = f"/users/{quote(check_username(name), safe='')}"
        return f"{path}/{action}" if action else path

    async def _get_user(self, name: str) -> dict[str, Any] | None:
        try:
            data = await self.request("GET", self._user_path(name))
        except TelemtApiError as exc:
            if exc.not_found:
                return None
            raise
        return data if isinstance(data, dict) else None

    def _account(self, user: Mapping[str, Any], key: str | None = None) -> ProxyAccount:
        links = _override_host(user_links(user), self.public_host, self.public_port)
        if key is None or not is_key(key):
            key = link_key(links[0]) if links else None
        tag = user.get("user_ad_tag")
        return ProxyAccount(
            name=str(user.get("username") or ""),
            enabled=bool(user.get("enabled", True)),
            key=key.lower() if key else None,
            links=links,
            expires_at=from_rfc3339(user.get("expiration_rfc3339")),
            max_unique_ips=_int(user.get("max_unique_ips")),
            traffic_limit_bytes=_int(user.get("data_quota_bytes")),
            rate_limit_bps=_rate(user),
            ad_tag=tag.lower() if isinstance(tag, str) and is_ad_tag(tag) else None,
            used_bytes=_int(user.get("total_octets")),
            current_connections=_int(user.get("current_connections")),
            active_unique_ips=_int(user.get("active_unique_ips")),
            recent_unique_ips=_int(user.get("recent_unique_ips")),
            raw=dict(user),
        )

    async def upsert_proxy_user(self, target: int | None, spec: ProxyUserSpec) -> ProxyAccount:
        check_username(spec.name)
        if spec.key is not None and not is_key(spec.key):
            raise PanelError("telemt secret must be 32 hex chars")
        if spec.ad_tag is not None and not is_ad_tag(spec.ad_tag):
            raise PanelError("telemt ad tag must be 32 hex chars")
        current = await self._get_user(spec.name)
        if current is None:
            try:
                data = await self.request("POST", "/users", _create_body(spec))
            except TelemtApiError as exc:
                if exc.code != "user_exists":
                    raise
                current = await self._get_user(spec.name)  # created concurrently: update it below
                if current is None:
                    raise
            else:
                if isinstance(data, dict) and isinstance(data.get("user"), dict):
                    return self._account(data["user"], data.get("secret"))
                raise PanelError("telemt POST /v1/users: unexpected response")
        patch = user_patch(current, spec)
        if not patch:
            return self._account(current, spec.key)
        data = await self.request("PATCH", self._user_path(spec.name), patch)
        if not isinstance(data, dict):
            raise PanelError(f"telemt PATCH {self._user_path(spec.name)}: unexpected response")
        return self._account(data, spec.key)

    async def get_proxy_user(self, target: int | None, name: str) -> ProxyAccount | None:
        user = await self._get_user(name)
        return self._account(user) if user is not None else None

    async def list_proxy_users(self, target: int | None) -> list[ProxyAccount]:
        data = await self.request("GET", "/users")
        return [self._account(u) for u in data or [] if isinstance(u, dict)]

    async def delete_proxy_user(self, target: int | None, name: str) -> None:
        try:
            await self.request("DELETE", self._user_path(name))
        except TelemtApiError as exc:
            if exc.not_found:
                return
            if exc.code != "last_user_forbidden":
                raise
            # telemt keeps at least one user in its config: leave this one disabled (no access) instead.
            logger.warning("telemt %s: cannot delete the last user, disabling it instead", self.base)
            await self._set_enabled(name, False)

    async def rotate_secret(self, name: str, key: str | None = None) -> ProxyAccount:
        """Replace the user's secret (random when ``key`` is None); old links stop working at once."""
        if key is not None and not is_key(key):
            raise PanelError("telemt secret must be 32 hex chars")
        body = {"secret": key.lower()} if key else None
        data = await self.request("POST", self._user_path(name, "rotate-secret"), body)
        if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
            raise PanelError("telemt rotate-secret: unexpected response")
        return self._account(data["user"], data.get("secret"))

    async def _set_enabled(self, name: str, enabled: bool) -> None:
        await self.request("POST", self._user_path(name, "enable" if enabled else "disable"))

    async def set_enabled(self, name: str, enabled: bool) -> None:
        """Enable/disable the user (idempotent; disabling closes its live sessions)."""
        await self._set_enabled(name, enabled)

    # --- PanelBackend ----------------------------------------------------------------------------
    async def ping(self) -> str:
        """Check connectivity, auth and write access; returns the telemt version."""
        health = await self.request("GET", "/health")
        if isinstance(health, dict) and health.get("read_only") is True:
            raise PanelError(f"telemt {self.base}: the API is read-only ([server.api] read_only = true)")
        info = await self.request("GET", "/system/info")
        version = info.get("version") if isinstance(info, dict) else None
        self.version = version if isinstance(version, str) and version else "unknown"
        return self.version

    async def list_inbounds(self) -> list[InboundInfo]:
        return []

    @staticmethod
    def _remote(account: ProxyAccount) -> RemoteAccount:
        return RemoteAccount(
            email=account.name,
            uuid=account.key or "",
            enabled=account.enabled,
            expires_at=account.expires_at,
            device_limit=account.max_unique_ips,
            traffic_limit_bytes=account.traffic_limit_bytes,
            used_bytes=account.used_bytes,
            raw=account.raw,
        )

    async def list_accounts(self, target: int | None) -> list[RemoteAccount]:
        return [self._remote(a) for a in await self.list_proxy_users(None)]

    async def get_account(self, target: int | None, email: str) -> RemoteAccount | None:
        account = await self.get_proxy_user(None, email)
        return self._remote(account) if account is not None else None

    async def get_used_bytes(self, target: int | None, email: str) -> int | None:
        user = await self._get_user(email)
        return _int(user.get("total_octets")) if user is not None else None

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        proxy_spec = ProxyUserSpec(
            name=spec.email,
            enabled=spec.enabled,
            expires_at=spec.expires_at,
            max_unique_ips=max(0, spec.device_limit),
            traffic_limit_bytes=max(0, spec.traffic_limit_bytes),
            tg_id=spec.tg_id,
            comment=spec.comment,
        )
        return self._remote(await self.upsert_proxy_user(None, proxy_spec))

    async def delete_account(self, target: int | None, email: str, uuid: str) -> None:
        await self.delete_proxy_user(None, email)

    async def get_links(self, account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        links = _override_host(user_links(account.raw), self.public_host, self.public_port)
        return [link.tg_url for link in links]

    async def reset_devices(self, account: RemoteAccount) -> None:
        """Drop the user's live sessions (frees its unique-IP slots); the account stays as it was."""
        if not account.enabled:
            return
        await self._set_enabled(account.email, False)
        await self._set_enabled(account.email, True)

    async def reset_traffic(self, target: int | None, email: str) -> None:
        try:
            await self.request("POST", self._user_path(email, "reset-quota"))
        except TelemtApiError as exc:
            if not exc.not_found:
                raise
