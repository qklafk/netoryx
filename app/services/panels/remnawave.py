"""Remnawave (>= 3.0) panel backend.

One Remnawave *user* per subscription; the servers it can use are chosen by internal squads, so the
``target`` argument of the :class:`~app.services.panels.base.PanelBackend` methods is always ``None``.

API notes (verified against Remnawave 3.4.8, see ``libs/contract`` in the Remnawave backend sources):

* Every response is wrapped as ``{"response": ...}``; errors look like
  ``{"statusCode", "message", "errorCode": "A0xx"}``. ``DELETE`` answers ``204 No Content``.
* Users are addressed by their numeric ``id`` (3.0 removed user UUIDs). ``vlessUuid`` is the VLESS id;
  it can only be chosen at creation time, so a changed ``AccountSpec.uuid`` recreates the user.
* ``PATCH /api/users`` refuses an ``expireAt`` in the past; enabling/disabling goes through
  ``/actions/enable|disable`` which answer A030/A029 ("already enabled/disabled") - treated as success.
* When the backend container is called directly (not through its reverse proxy) every request must carry
  ``X-Forwarded-For`` and ``X-Forwarded-Proto: https``, otherwise the socket is closed without a reply.
* Webhooks are signed: ``X-Remnawave-Signature = hex(HMAC_SHA256(WEBHOOK_SECRET_HEADER, raw_body))``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, TypeVar
from urllib.parse import quote, urlsplit

import aiohttp

from app.db.models import PanelKind
from app.services.panels.base import (
    AccountSpec,
    InboundInfo,
    NodeStatus,
    PanelAuthError,
    PanelError,
    PanelUnsupportedError,
    RemoteAccount,
    ServerStatus,
)

if TYPE_CHECKING:
    from app.services.devices import DeviceInfo

logger = logging.getLogger(__name__)

T = TypeVar("T")

MIN_VERSION: Final = (3, 0)

# Remnawave requires an expiry date; "never expires" is stored as a far-future date and read back as None.
# Only the sentinel itself (give or take a day for panels that shift it) means "never": a real expiry pushed by a
# very long plan can land beyond 2099 and must read back as pushed.
NEVER_EXPIRES: Final = datetime(2099, 12, 31, tzinfo=UTC)
_NEVER_EXPIRES_WINDOW: Final = timedelta(days=1)
# PATCH rejects dates that are not in the future (by the panel's clock); keep a margin for clock skew.
_EXPIRY_SAFETY_MARGIN: Final = timedelta(seconds=60)

USERNAME_RE: Final = re.compile(r"^[A-Za-z0-9_-]{3,36}$")
TRAFFIC_STRATEGIES: Final = frozenset({"NO_RESET", "DAY", "WEEK", "MONTH", "MONTH_ROLLING"})

SIGNATURE_HEADER: Final = "X-Remnawave-Signature"
TIMESTAMP_HEADER: Final = "X-Remnawave-Timestamp"

# Error codes (libs/contract/constants/errors/errors.ts).
ERR_USERNAME_EXISTS: Final = "A019"
ERR_USER_NOT_FOUND: Final = "A025"
ERR_ALREADY_DISABLED: Final = "A029"
ERR_ALREADY_ENABLED: Final = "A030"
ERR_USERS_NOT_FOUND: Final = "A062"
ERR_NOT_FOUND_BY_FIELDS: Final = "A063"
ERR_ENABLED_NODES_NOT_FOUND: Final = "A054"
_NOT_FOUND_CODES: Final = frozenset({ERR_USER_NOT_FOUND, ERR_USERS_NOT_FOUND, ERR_NOT_FOUND_BY_FIELDS})

# Request headers of a subscription client that Remnawave uses for HWID accounting and response rules.
FORWARDED_SUBSCRIPTION_HEADERS: Final[tuple[str, ...]] = (
    "user-agent",
    "accept",
    "accept-language",
    "x-hwid",
    "x-device-os",
    "x-ver-os",
    "x-device-model",
    "x-remnawave-real-ip",
)
# Response headers that describe the transport, not the subscription; never relayed to the client.
_DROPPED_RESPONSE_HEADERS: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-length",
        "content-encoding",
        "date",
        "server",
        "x-powered-by",
        "etag",
        "vary",
        "set-cookie",
        "strict-transport-security",
        "x-robots-tag",
    }
)
_RETRYABLE_STATUSES: Final = frozenset({502, 503, 504})


class RemnawaveApiError(PanelError):
    """The panel answered with an HTTP error status."""

    def __init__(self, status: int, code: str | None, message: str, *, method: str, path: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.method = method
        self.path = path
        label = f"{status} {code}" if code else str(status)
        super().__init__(f"Remnawave API error {label}: {message} ({method} {path})")

    @property
    def is_not_found(self) -> bool:
        """True for "user not found"-style answers (not for an unknown route)."""
        return self.status == 404 and self.code in _NOT_FOUND_CODES


class RemnawaveWebhookError(ValueError):
    """A webhook body that is not a valid Remnawave event."""


@dataclass(frozen=True, slots=True)
class RemnawaveSquad:
    uuid: str
    name: str
    members_count: int = 0
    inbounds_count: int = 0


@dataclass(frozen=True, slots=True)
class RemnawaveEvent:
    """A parsed webhook. ``user_*`` fields are set for ``user.*`` and ``user_hwid_devices.*`` events."""

    event: str  # e.g. "user.expired", "user_hwid_devices.added", "node.connection_lost"
    user_id: int | None
    username: str | None
    telegram_id: int | None
    data: Mapping[str, Any]  # the event's "data" object as sent
    scope: str = ""  # "user", "user_hwid_devices", "node", "service", "errors", "crm", ...
    timestamp: datetime | None = None
    meta: Mapping[str, Any] | None = field(default=None)


# --------------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------------


def parse_version(version: str) -> tuple[int, int, int]:
    """``"3.4.8"`` / ``"v3.5.0-beta"`` -> ``(3, 4, 8)``. Raises ValueError for unparseable strings."""
    match = re.match(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not match:
        raise ValueError(f"unrecognized version: {version!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def format_datetime(dt: datetime) -> str:
    """Aware (or naive = UTC) datetime -> ``2026-12-04T00:00:00.000Z``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_datetime(value: str | None) -> datetime | None:
    """ISO-8601 from the API -> aware UTC datetime (None for empty input)."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def is_placeholder_link(link: str) -> bool:
    """True for the ``0.0.0.0:1`` stubs Remnawave emits instead of real servers ("No hosts found", ...)."""
    if link.startswith("vmess://"):
        return _is_placeholder_vmess(link)
    try:
        parts = urlsplit(link)
        return parts.hostname == "0.0.0.0" and parts.port == 1
    except ValueError:
        return False


def _is_placeholder_vmess(link: str) -> bool:
    payload = link.removeprefix("vmess://").split("#", 1)[0]
    try:
        decoded = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4)))
    except (binascii.Error, ValueError):
        return False
    return isinstance(decoded, dict) and decoded.get("add") == "0.0.0.0" and str(decoded.get("port")) == "1"


def _squad_uuids(user: Mapping[str, Any]) -> set[str]:
    squads = user.get("activeInternalSquads") or []
    return {s["uuid"] if isinstance(s, Mapping) else str(s) for s in squads}


def _normalized_expiry(value: str | None) -> str | None:
    dt = parse_datetime(value)
    return format_datetime(dt) if dt else None


def _to_account(user: Mapping[str, Any]) -> RemoteAccount:
    """API user -> RemoteAccount. ``enabled`` is False only for DISABLED (LIMITED/EXPIRED are runtime states)."""
    expires_at = parse_datetime(user.get("expireAt"))
    if expires_at is not None and abs(expires_at - NEVER_EXPIRES) <= _NEVER_EXPIRES_WINDOW:
        expires_at = None
    traffic = user.get("userTraffic") or {}
    used = traffic.get("usedTrafficBytes", user.get("usedTrafficBytes", 0))
    return RemoteAccount(
        email=str(user["username"]),
        uuid=str(user.get("vlessUuid", "")),
        enabled=user.get("status") != "DISABLED",
        expires_at=expires_at,
        device_limit=int(user.get("hwidDeviceLimit") or 0),
        traffic_limit_bytes=int(user.get("trafficLimitBytes") or 0),
        used_bytes=int(used or 0),
        remote_id=str(user["id"]),
        short_id=user.get("shortUuid"),
        subscription_url=user.get("subscriptionUrl"),
        raw=dict(user),
    )


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_datetime(value: Any) -> datetime | None:
    try:
        return parse_datetime(value) if isinstance(value, str) else None
    except ValueError:
        return None


def parse_node(node: Mapping[str, Any]) -> NodeStatus:
    """One ``GET /api/nodes`` entry -> :class:`NodeStatus` (field names differ slightly between versions)."""
    traffic_used = node.get("trafficUsedBytes")
    if traffic_used is None:
        traffic_used = _mapping(node.get("nodeTraffic")).get("usedTrafficBytes")
    return NodeStatus(
        name=str(node.get("name") or node.get("address") or node.get("uuid") or "?"),
        online=bool(node.get("isConnected")) and not bool(node.get("isDisabled")),
        address=str(node.get("address") or ""),
        disabled=bool(node.get("isDisabled")),
        users_online=_opt_int(node.get("usersOnline")),
        uptime_sec=_opt_int(node.get("xrayUptime")),
        traffic_used_bytes=_opt_int(traffic_used),
        traffic_limit_bytes=_opt_int(node.get("trafficLimitBytes")),
        core_version=str(node["xrayVersion"]) if node.get("xrayVersion") else None,
        country=str(node["countryCode"]) if node.get("countryCode") else None,
        message=str(node["lastStatusMessage"]) if node.get("lastStatusMessage") else None,
    )


def parse_system_stats(stats: Mapping[str, Any], nodes: list[NodeStatus]) -> ServerStatus:
    """``GET /api/system/stats`` (the panel host) plus the nodes summary -> :class:`ServerStatus`."""
    cpu = _mapping(stats.get("cpu"))
    memory = _mapping(stats.get("memory"))
    online = _mapping(stats.get("onlineStats"))
    mem_total = _opt_int(memory.get("total"))
    mem_used = _opt_int(memory.get("used"))
    if mem_total and memory.get("available") is not None:
        available = _opt_int(memory.get("available"))
        mem_used = mem_total - available if available is not None else mem_used
    users_online = _opt_int(online.get("onlineNow"))
    if users_online is None and nodes:
        users_online = sum(n.users_online or 0 for n in nodes)
    return ServerStatus(
        cpu_cores=_opt_int(cpu.get("cores")),
        mem_used=mem_used,
        mem_total=mem_total,
        uptime_sec=_opt_int(stats.get("uptime")),
        online_users=users_online,
        nodes=tuple(nodes),
    )


def _error_message(payload: Any, fallback: str) -> tuple[str | None, str]:
    """Extract ``(errorCode, human message)`` from an error body (JSON already decoded or None)."""
    if not isinstance(payload, Mapping):
        return None, fallback
    code = payload.get("errorCode")
    message = str(payload.get("message") or fallback)
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        details = []
        for item in errors:
            if isinstance(item, Mapping):
                path = ".".join(str(p) for p in item.get("path") or [])
                details.append(f"{path}: {item.get('message')}" if path else str(item.get("message")))
            else:
                details.append(str(item))
        message = f"{message}: {'; '.join(details)}"
    return (str(code) if code else None), message


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body) if body else None
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------------------------------


class RemnawaveBackend:
    """:class:`~app.services.panels.base.PanelBackend` for Remnawave >= 3.0 (REST API, API token)."""

    kind = PanelKind.REMNAWAVE

    #: Pauses between attempts of idempotent requests on connection errors / 502-504.
    retry_delays: tuple[float, ...] = (0.5, 1.5)
    #: Page size for ``GET /api/users`` (API maximum is 1000).
    page_size: int = 500
    #: How long the "all squads" list (used when ``squad_uuids`` is empty) is cached, seconds.
    squads_ttl: float = 600.0

    def __init__(
        self,
        url: str,
        api_token: str,
        *,
        squad_uuids: list[str],
        username_prefix: str = "tg_",
        direct: bool = True,
        tls_verify: bool = True,
        timeout: float = 15,
        extra_headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        traffic_limit_strategy: str = "NO_RESET",
    ) -> None:
        """
        :param url: panel base URL, e.g. ``http://remnawave:3000`` or ``https://panel.example.com``
            (a trailing ``/api`` is tolerated).
        :param squad_uuids: internal squads assigned to every user; empty = all squads of the panel.
        :param username_prefix: identifies users created by the bot (used by :meth:`list_accounts`).
        :param direct: send ``X-Forwarded-For/Proto`` (required when calling the backend container directly).
        :param extra_headers: sent with every request (e.g. ``X-Api-Key`` of a Caddy-with-auth setup).
        :param cookies: sent with every request (e.g. a reverse-proxy auth cookie).
        :param traffic_limit_strategy: Remnawave reset period for ``trafficLimitBytes`` (default: never).
        """
        if traffic_limit_strategy not in TRAFFIC_STRATEGIES:
            raise ValueError(f"traffic_limit_strategy must be one of {sorted(TRAFFIC_STRATEGIES)}")
        base = url.strip().rstrip("/")
        self.base_url = base.removesuffix("/api")
        self.squad_uuids = list(squad_uuids)
        self.username_prefix = username_prefix
        self.direct = direct
        self.traffic_limit_strategy = traffic_limit_strategy
        self._token = api_token
        self._ssl: bool = tls_verify
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._headers = self._base_headers(direct, extra_headers)
        self._cookies = dict(cookies or {})
        self._session: aiohttp.ClientSession | None = None
        self._closed = False  # set by close(): a closed backend never opens a new session
        self._version: str | None = None
        self._all_squads: tuple[float, list[str]] | None = None

    def __repr__(self) -> str:  # never expose the token
        return f"RemnawaveBackend({self.base_url!r}, version={self._version!r})"

    @staticmethod
    def _base_headers(direct: bool, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "vpn-bot"}
        if direct:
            headers["X-Forwarded-For"] = "127.0.0.1"
            headers["X-Forwarded-Proto"] = "https"
        headers.update(extra or {})
        return headers

    # ------------------------------------------------------------------ lifecycle

    @property
    def version(self) -> str | None:
        """Panel version seen by the last successful :meth:`ping` (None before the first call)."""
        return self._version

    def _http(self) -> aiohttp.ClientSession:
        if self._closed:
            # The registry closes replaced backends that callers may still hold; reopening would leak a
            # session nobody closes (and keep using a stale token).
            raise PanelError(f"Remnawave {self.base_url}: backend is closed")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers=self._headers,
                cookies=self._cookies,
            )
        return self._session

    async def close(self) -> None:
        """Close the session for good: later requests raise :class:`PanelError`."""
        self._closed = True
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> RemnawaveBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, str | int] | None = None,
        idempotent: bool = True,
    ) -> Any:
        """Authenticated API call; returns the unwrapped ``response`` (None for empty bodies)."""
        headers = {"Authorization": f"Bearer {self._token}"}
        _, _, body = await self._send(
            method, path, headers=headers, json_body=json_body, params=params, idempotent=idempotent
        )
        payload = _decode_json(body)
        if body and payload is None:
            raise PanelError(f"Remnawave returned a non-JSON response for {method} {path}")
        if isinstance(payload, Mapping) and "response" in payload:
            return payload["response"]
        return payload

    async def _send(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        params: Mapping[str, str | int] | None = None,
        idempotent: bool = True,
    ) -> tuple[int, Mapping[str, str], bytes]:
        """Send with retries (idempotent requests only); raise on HTTP errors; return (status, headers, body)."""
        attempts = len(self.retry_delays) + 1 if idempotent else 1
        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            try:
                status, resp_headers, body = await self._send_once(method, path, headers, json_body, params)
            except (aiohttp.ClientConnectionError, TimeoutError) as exc:
                if last_attempt:
                    raise self._connection_error(method, path, exc) from exc
            except aiohttp.ClientError as exc:  # malformed response, bad URL, ...: retrying will not help
                raise PanelError(f"Remnawave {method} {path}: {type(exc).__name__}: {exc}") from exc
            else:
                if status < 400:
                    return status, resp_headers, body
                if status not in _RETRYABLE_STATUSES or last_attempt:
                    raise self._http_error(status, body, method, path)
            logger.debug("Remnawave %s %s failed (attempt %d), retrying", method, path, attempt + 1)
            await asyncio.sleep(self.retry_delays[attempt])
        raise AssertionError("unreachable")  # pragma: no cover

    async def _send_once(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str] | None,
        json_body: Any,
        params: Mapping[str, str | int] | None,
    ) -> tuple[int, Mapping[str, str], bytes]:
        async with self._http().request(
            method,
            self.base_url + path,
            headers=headers,
            json=json_body,
            params=params,
            ssl=self._ssl,
        ) as resp:
            body = await resp.read()
            logger.debug("Remnawave %s %s -> %d", method, path, resp.status)
            return resp.status, dict(resp.headers), body

    def _connection_error(self, method: str, path: str, exc: BaseException) -> PanelError:
        detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
        hint = ""
        if isinstance(exc, aiohttp.ServerDisconnectedError) and not self.direct:
            hint = (
                " - the panel closed the connection; if the bot talks to the backend container directly "
                "(not via its HTTPS reverse proxy), enable direct mode so X-Forwarded-* headers are sent"
            )
        return PanelError(f"Remnawave {method} {path}: {detail}{hint}")

    @staticmethod
    def _http_error(status: int, body: bytes, method: str, path: str) -> PanelError:
        code, message = _error_message(_decode_json(body), body[:200].decode("utf-8", "replace") or "no body")
        if status == 401:
            return PanelAuthError(f"Remnawave rejected the API token (401): {message}")
        if status == 403:
            return PanelAuthError(f"Remnawave API token lacks a required scope for {method} {path} (403): {message}")
        return RemnawaveApiError(status, code, message, method=method, path=path)

    # ------------------------------------------------------------------ system

    async def ping(self) -> str:
        """Return the panel version; raise PanelUnsupportedError for Remnawave < 3.0."""
        try:
            meta = await self._request("GET", "/api/system/metadata")
        except RemnawaveApiError as exc:
            if exc.status == 404:
                raise PanelUnsupportedError(
                    "GET /api/system/metadata not found: Remnawave < 3.0 is not supported (or the URL is wrong)"
                ) from exc
            raise
        version = str(meta.get("version", "")) if isinstance(meta, Mapping) else ""
        try:
            parsed = parse_version(version)
        except ValueError as exc:
            raise PanelError(f"Remnawave reported an unrecognized version {version!r}") from exc
        if parsed[:2] < MIN_VERSION:
            raise PanelUnsupportedError(f"Remnawave {version} is not supported; version 3.0 or newer is required")
        self._version = version
        return version

    async def _ensure_supported(self) -> None:
        if self._version is None:
            await self.ping()

    async def list_squads(self) -> list[RemnawaveSquad]:
        """All internal squads of the panel (for the admin UI and the "all squads" default)."""
        await self._ensure_supported()
        data = await self._request("GET", "/api/internal-squads")
        squads = data.get("internalSquads", []) if isinstance(data, Mapping) else []
        return [
            RemnawaveSquad(
                uuid=str(s["uuid"]),
                name=str(s.get("name", "")),
                members_count=int((s.get("info") or {}).get("membersCount", 0)),
                inbounds_count=int((s.get("info") or {}).get("inboundsCount", 0)),
            )
            for s in squads
        ]

    async def _target_squads(self) -> list[str]:
        if self.squad_uuids:
            return self.squad_uuids
        now = time.monotonic()
        if self._all_squads is None or now - self._all_squads[0] > self.squads_ttl:
            uuids = [s.uuid for s in await self.list_squads()]
            if not uuids:
                logger.warning("Remnawave %s has no internal squads: users will get no servers", self.base_url)
            self._all_squads = (now, uuids)
        return self._all_squads[1]

    def invalidate_squads(self) -> None:
        """Forget the cached "all squads" list (provisioning calls it when the panel's squads changed)."""
        self._all_squads = None

    async def list_inbounds(self) -> list[InboundInfo]:
        return []

    # ------------------------------------------------------------------ users: reads

    async def _find_user(self, username: str) -> dict[str, Any] | None:
        try:
            user = await self._request("GET", f"/api/users/by-username/{quote(username, safe='')}")
        except RemnawaveApiError as exc:
            if exc.is_not_found:
                return None
            raise
        return dict(user)

    async def get_account(self, target: int | None, email: str) -> RemoteAccount | None:
        _check_target(target)
        await self._ensure_supported()
        user = await self._find_user(email)
        return _to_account(user) if user else None

    async def get_used_bytes(self, target: int | None, email: str) -> int | None:
        account = await self.get_account(target, email)
        return account.used_bytes if account is not None else None

    async def list_accounts(self, target: int | None) -> list[RemoteAccount]:
        """Users whose username starts with ``username_prefix`` (i.e. created by this bot)."""
        _check_target(target)
        await self._ensure_supported()
        accounts: dict[str, RemoteAccount] = {}
        start = 0
        while True:
            page = await self._request("GET", "/api/users", params={"start": start, "size": self.page_size})
            users = page.get("users", []) if isinstance(page, Mapping) else []
            for user in users:
                if str(user.get("username", "")).startswith(self.username_prefix):
                    account = _to_account(user)
                    accounts[account.remote_id or account.email] = account
            start += len(users)
            total = int(page.get("total", 0)) if isinstance(page, Mapping) else 0
            if len(users) < self.page_size or start >= total:
                return list(accounts.values())

    # ------------------------------------------------------------------ users: writes

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        """Create the user or bring it to ``spec``. A different ``spec.uuid`` recreates the user."""
        _check_target(target)
        _check_username(spec.email)
        await self._ensure_supported()
        squads = await self._target_squads()
        current = await self._find_user(spec.email)
        if current is not None and str(current.get("vlessUuid", "")).lower() != spec.uuid.lower():
            logger.info("Remnawave user %s: VLESS id changed, recreating", spec.email)
            await self._delete_user(int(current["id"]))
            current = None
        if current is None:
            try:
                return _to_account(await self._create_user(spec, squads))
            except RemnawaveApiError as exc:
                if exc.code != ERR_USERNAME_EXISTS:
                    raise
                current = await self._find_user(spec.email)  # created concurrently / by an earlier retry
                if current is None:
                    raise
        return _to_account(await self._update_user(current, spec, squads))

    def _desired_fields(self, spec: AccountSpec, squads: list[str]) -> dict[str, Any]:
        return {
            "trafficLimitBytes": spec.traffic_limit_bytes,
            "trafficLimitStrategy": self.traffic_limit_strategy,
            "hwidDeviceLimit": spec.device_limit,
            "telegramId": spec.tg_id if spec.tg_id > 0 else None,
            "description": spec.comment,
            "activeInternalSquads": list(squads),
        }

    async def _create_user(self, spec: AccountSpec, squads: list[str]) -> Mapping[str, Any]:
        body = {
            "username": spec.email,
            "vlessUuid": spec.uuid,
            "status": "ACTIVE" if spec.enabled else "DISABLED",
            "expireAt": format_datetime(spec.expires_at or NEVER_EXPIRES),
            **self._desired_fields(spec, squads),
        }
        # Not retried blindly: a retry after a lost response would hit A019, which upsert handles.
        return await self._request("POST", "/api/users", json_body=body, idempotent=False)

    async def _update_user(self, current: Mapping[str, Any], spec: AccountSpec, squads: list[str]) -> Mapping[str, Any]:
        expire_at, want_enabled = _update_expiry(spec)
        changes = _changed_fields(current, self._desired_fields(spec, squads), expire_at)
        user = current
        if changes:
            user = await self._request("PATCH", "/api/users", json_body={"id": int(current["id"]), **changes})
        return await self._apply_status(user, want_enabled)

    async def _apply_status(self, user: Mapping[str, Any], want_enabled: bool) -> Mapping[str, Any]:
        """Enable/disable via actions. LIMITED (traffic exceeded) is left alone when enabling."""
        status = user.get("status")
        if want_enabled and status in ("DISABLED", "EXPIRED"):
            return await self._user_action(int(user["id"]), "enable")
        if not want_enabled and status != "DISABLED":
            return await self._user_action(int(user["id"]), "disable")
        return user

    async def _user_action(self, user_id: int, action: str) -> Mapping[str, Any]:
        try:
            return await self._request("POST", f"/api/users/{user_id}/actions/{action}")
        except RemnawaveApiError as exc:
            if exc.code not in (ERR_ALREADY_ENABLED, ERR_ALREADY_DISABLED):
                raise
        return await self._request("GET", f"/api/users/{user_id}")

    async def delete_account(self, target: int | None, email: str, uuid: str) -> None:
        """Delete the user by username; "not found" is success. ``uuid`` is not needed by Remnawave."""
        _check_target(target)
        await self._ensure_supported()
        user = await self._find_user(email)
        if user is not None:
            await self._delete_user(int(user["id"]))

    async def reset_traffic(self, target: int | None, email: str) -> None:
        """Zero the user's used traffic; a user LIMITED by traffic is made ACTIVE again. Missing user: no-op."""
        _check_target(target)
        await self._ensure_supported()
        user = await self._find_user(email)
        if user is None:
            return
        user_id = int(user["id"])
        try:
            result = await self._request("POST", f"/api/users/{user_id}/actions/reset-traffic")
        except RemnawaveApiError as exc:
            if exc.is_not_found:
                return
            raise
        if isinstance(result, Mapping) and result.get("status") == "LIMITED":
            await self._user_action(user_id, "enable")

    async def _delete_user(self, user_id: int) -> None:
        try:
            await self._request("DELETE", f"/api/users/{user_id}")
        except RemnawaveApiError as exc:
            if not exc.is_not_found:
                raise

    # ------------------------------------------------------------------ links & devices

    async def get_links(self, account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        """Enabled share links (``connection-keys``), placeholders removed.

        ``client_headers`` are ignored: connection keys bypass HWID accounting. Use
        :meth:`fetch_subscription` to proxy the real subscription when HWID limits must be enforced.
        """
        await self._ensure_supported()

        async def fetch(user_id: int) -> list[str]:
            data = await self._request("GET", f"/api/subscriptions/connection-keys/{user_id}")
            keys = data.get("enabledKeys", []) if isinstance(data, Mapping) else []
            return [k for k in keys if isinstance(k, str) and k and not is_placeholder_link(k)]

        return await self._for_user(account, fetch) or []

    async def reset_devices(self, account: RemoteAccount) -> None:
        """Delete every HWID device of the user (needs the ``hwid-user-devices`` token scope)."""
        await self._ensure_supported()

        async def delete_all(user_id: int) -> None:
            await self._request("POST", "/api/hwid/devices/delete-all", json_body={"userId": user_id})

        await self._for_user(account, delete_all)

    async def list_devices(self, account: RemoteAccount) -> list[DeviceInfo]:
        """HWID devices registered by the user's apps (needs the ``hwid-user-devices`` token scope)."""
        from app.services.devices import DeviceInfo

        await self._ensure_supported()

        async def fetch(user_id: int) -> list[DeviceInfo]:
            data = await self._request("GET", f"/api/hwid/devices/{user_id}")
            items = data.get("devices", []) if isinstance(data, Mapping) else data if isinstance(data, list) else []
            devices: list[DeviceInfo] = []
            for item in items:
                if not isinstance(item, Mapping) or not item.get("hwid"):
                    continue
                devices.append(
                    DeviceInfo(
                        id=str(item["hwid"]),
                        platform=str(item["platform"]) if item.get("platform") else None,
                        model=str(item["deviceModel"]) if item.get("deviceModel") else None,
                        os_version=str(item["osVersion"]) if item.get("osVersion") else None,
                        user_agent=str(item["userAgent"]) if item.get("userAgent") else None,
                        ip=str(item["requestIp"]) if item.get("requestIp") else None,
                        created_at=_safe_datetime(item.get("createdAt")),
                    )
                )
            return devices

        return await self._for_user(account, fetch) or []

    async def delete_device(self, account: RemoteAccount, device_id: str) -> None:
        """Unlink one HWID device; an unknown user or device is not an error."""
        await self._ensure_supported()

        async def delete_one(user_id: int) -> None:
            try:
                await self._request(
                    "POST", "/api/hwid/devices/delete", json_body={"userId": user_id, "hwid": device_id}
                )
            except RemnawaveApiError as exc:
                if exc.status != 404:
                    raise

        await self._for_user(account, delete_one)

    async def server_status(self) -> ServerStatus:
        """Nodes summary (connection, online users, traffic) plus the panel host's stats when readable.

        Needs the ``nodes:read`` scope; ``system:read`` adds memory/uptime of the panel host.
        """
        await self._ensure_supported()
        data = await self._request("GET", "/api/nodes")
        items = data.get("nodes", []) if isinstance(data, Mapping) else data if isinstance(data, list) else []
        nodes = [parse_node(item) for item in items if isinstance(item, Mapping)]
        try:
            stats = await self._request("GET", "/api/system/stats")
        except PanelError as exc:
            logger.debug("Remnawave %s: system stats unavailable: %s", self.base_url, exc)
            stats = None
        return parse_system_stats(stats if isinstance(stats, Mapping) else {}, nodes)

    async def restart_core(self) -> None:
        """Restart Xray on every node (``POST /api/nodes/actions/restart-all``); no enabled nodes is not an error."""
        await self._ensure_supported()
        try:
            await self._request(
                "POST", "/api/nodes/actions/restart-all", json_body={"forceRestart": False}, idempotent=False
            )
        except RemnawaveApiError as exc:
            if exc.code != ERR_ENABLED_NODES_NOT_FOUND:
                raise
            logger.info("Remnawave %s: no enabled nodes to restart", self.base_url)

    async def _for_user(self, account: RemoteAccount, op: Callable[[int], Awaitable[T]]) -> T | None:
        """Run ``op(user_id)`` using the cached remote id, falling back to a username lookup.

        Returns None when the user does not exist on the panel.
        """
        if account.remote_id and account.remote_id.isdigit():
            try:
                return await op(int(account.remote_id))
            except RemnawaveApiError as exc:
                if not exc.is_not_found:
                    raise
        user = await self._find_user(account.email)
        if user is None:
            return None
        return await op(int(user["id"]))

    async def fetch_subscription(
        self, short_id: str, forward_headers: Mapping[str, str] | None = None
    ) -> tuple[str, dict[str, str]]:
        """Fetch the public subscription ``/api/sub/{shortUuid}`` on behalf of a client.

        Only HWID / client-identification headers (:data:`FORWARDED_SUBSCRIPTION_HEADERS`) are forwarded;
        pass the client's address as ``x-remnawave-real-ip``. Returns ``(body, headers)`` with
        lower-cased subscription headers (transport headers removed). The API token is not sent.
        """
        wanted = {k.lower(): v for k, v in (forward_headers or {}).items()}
        headers = {name: wanted[name] for name in FORWARDED_SUBSCRIPTION_HEADERS if name in wanted}
        _, resp_headers, body = await self._send("GET", f"/api/sub/{quote(short_id, safe='')}", headers=headers)
        relayed = {k.lower(): v for k, v in resp_headers.items() if k.lower() not in _DROPPED_RESPONSE_HEADERS}
        return body.decode("utf-8", "replace"), relayed

    # ------------------------------------------------------------------ webhooks

    @staticmethod
    def verify_webhook(raw: bytes, signature: str | None, secret: str) -> bool:
        """Check ``X-Remnawave-Signature`` (hex HMAC-SHA256 of the raw body with WEBHOOK_SECRET_HEADER)."""
        if not signature or not secret:
            return False
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected.encode(), signature.strip().lower().encode("utf-8", "replace"))

    @staticmethod
    def parse_webhook(raw: bytes | str) -> RemnawaveEvent:
        """Parse a (verified) webhook body. Raises :class:`RemnawaveWebhookError` on malformed input."""
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RemnawaveWebhookError("webhook body is not valid JSON") from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("event"), str) or not payload["event"]:
            raise RemnawaveWebhookError("webhook body has no 'event'")
        event: str = payload["event"]
        scope = str(payload.get("scope") or event.split(".", 1)[0])
        raw_data = payload.get("data")
        data: Mapping[str, Any] = raw_data if isinstance(raw_data, Mapping) else {}
        user = _event_user(scope, data)
        try:
            timestamp = parse_datetime(payload.get("timestamp"))
        except (TypeError, ValueError):
            timestamp = None
        meta = payload.get("meta")
        return RemnawaveEvent(
            event=event,
            user_id=_int_or_none(user.get("id")),
            username=str(user["username"]) if user.get("username") is not None else None,
            telegram_id=_int_or_none(user.get("telegramId")),
            data=data,
            scope=scope,
            timestamp=timestamp,
            meta=meta if isinstance(meta, Mapping) else None,
        )


# --------------------------------------------------------------------------------------------------
# Upsert helpers
# --------------------------------------------------------------------------------------------------


def _check_target(target: int | None) -> None:
    if target is not None:
        raise ValueError("Remnawave users are not bound to inbounds: target must be None")


def _check_username(username: str) -> None:
    if not USERNAME_RE.match(username):
        raise PanelError(f"invalid Remnawave username {username!r}: 3-36 characters of [A-Za-z0-9_-] required")


def _update_expiry(spec: AccountSpec) -> tuple[str | None, bool]:
    """``(expireAt to PATCH or None, enabled)`` for an existing user.

    The API cannot move ``expireAt`` into the past, so an already expired spec keeps the remote date
    and is disabled instead - the account stops working either way.
    """
    if spec.expires_at is None:
        return format_datetime(NEVER_EXPIRES), spec.enabled
    expires_at = spec.expires_at if spec.expires_at.tzinfo else spec.expires_at.replace(tzinfo=UTC)
    if expires_at > datetime.now(UTC) + _EXPIRY_SAFETY_MARGIN:
        return format_datetime(expires_at), spec.enabled
    return None, False


def _changed_fields(current: Mapping[str, Any], desired: Mapping[str, Any], expire_at: str | None) -> dict[str, Any]:
    """The subset of ``desired`` (+ ``expireAt``) that differs from the user as stored on the panel."""
    changes: dict[str, Any] = {}
    for key in ("trafficLimitBytes", "trafficLimitStrategy", "hwidDeviceLimit", "telegramId"):
        if current.get(key) != desired[key]:
            changes[key] = desired[key]
    if (current.get("description") or "") != (desired["description"] or ""):
        changes["description"] = desired["description"]
    if _squad_uuids(current) != set(desired["activeInternalSquads"]):
        changes["activeInternalSquads"] = desired["activeInternalSquads"]
    if expire_at is not None and _normalized_expiry(current.get("expireAt")) != expire_at:
        changes["expireAt"] = expire_at
    return changes


def _event_user(scope: str, data: Mapping[str, Any]) -> Mapping[str, Any]:
    if scope == "user":
        return data
    nested = data.get("user")
    return nested if isinstance(nested, Mapping) else {}


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
