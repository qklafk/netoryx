"""Asynchronous Marzban API client. Existing proxy settings and subscription tokens are retained on updates."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID

import aiohttp

from app.db.models import PanelKind
from app.services.panels.base import (
    AccountSpec,
    InboundInfo,
    PanelAuthError,
    PanelError,
    PanelUnsupportedError,
    RemoteAccount,
    ServerStatus,
    TgOnlyAccess,
)
from app.services.panels.xray_template import (
    TemplateError,
    marzban_user_match,
    regexp_user_supported,
    set_tg_only_config,
)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_@.\-]{3,32}$")
PAGE_SIZE = 200
MAX_USERS = 100_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class MarzbanApiError(PanelError):
    def __init__(self, status: int) -> None:
        super().__init__(f"Marzban API returned HTTP {status}")
        self.status = status


def user_uuid(user: Mapping[str, Any]) -> str | None:
    """The VLESS identity of an account, if it has a usable VLESS configuration."""
    proxies = user.get("proxies")
    vless = proxies.get("vless") if isinstance(proxies, Mapping) else None
    value = vless.get("id") if isinstance(vless, Mapping) else None
    try:
        return str(UUID(str(value))) if value else None
    except ValueError:
        return None


def parse_account(user: Mapping[str, Any]) -> RemoteAccount:
    expiry = user.get("expire")
    try:
        expires_at = datetime.fromtimestamp(int(expiry), UTC) if expiry else None
    except (ValueError, TypeError, OverflowError, OSError):
        expires_at = None
    return RemoteAccount(
        email=str(user.get("username") or ""),
        uuid=user_uuid(user) or "",
        enabled=user.get("status") == "active",
        expires_at=expires_at,
        device_limit=0,
        traffic_limit_bytes=max(0, int(user.get("data_limit") or 0)),
        used_bytes=max(0, int(user.get("used_traffic") or 0)),
        remote_id=str(user.get("username") or ""),
        subscription_url=str(user.get("subscription_url") or "") or None,
        raw=dict(user),
    )


class MarzbanBackend:
    """One reusable HTTP session, OAuth password login or a supplied bearer token, and bounded pagination."""

    kind = PanelKind.MARZBAN

    def __init__(
        self,
        url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        api_token: str | None = None,
        tls_verify: bool = True,
        timeout: float = 15,
        extra_headers: Mapping[str, str] | None = None,
        inbound_tags: list[str] | None = None,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Marzban requires an HTTP(S) URL without embedded credentials")
        if not (api_token or (username and password)):
            raise ValueError("Marzban needs an API token or username and password")
        self._url = url.rstrip("/")
        self._username, self._password, self._token = username, password, api_token
        self._tls_verify, self._timeout = tls_verify, timeout
        self._headers = dict(extra_headers or {})
        self._inbound_tags = list(inbound_tags or [])
        self._session: aiohttp.ClientSession | None = None
        self._auth_lock = asyncio.Lock()

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout))
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def _login(self, stale_token: str | None = None) -> None:
        async with self._auth_lock:
            if self._token and self._token != stale_token:
                return
            if not self._username or not self._password:
                raise PanelAuthError("Marzban bearer token was rejected")
            result = await self._send(
                "POST",
                "/api/admin/token",
                auth=False,
                data={"username": self._username, "password": self._password},
            )
            token = result.get("access_token") if isinstance(result, dict) else None
            if not isinstance(token, str) or not token:
                raise PanelAuthError("Marzban returned no access token")
            self._token = token

    async def _send(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> Any:
        session = await self._client()
        headers = {**self._headers, "Accept": "application/json"}
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with session.request(
                method, self._url + path, headers=headers, ssl=self._tls_verify, allow_redirects=False, **kwargs
            ) as response:
                if response.status in (401, 403):
                    raise PanelAuthError(f"Marzban access denied (HTTP {response.status})")
                if response.status >= 300:
                    raise MarzbanApiError(response.status)
                if response.status == 204:
                    return {}
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise PanelError("Marzban response is too large")
                if not body:
                    return {}
                try:
                    return json.loads(body)
                except ValueError as exc:
                    raise PanelError("Marzban returned invalid JSON") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise PanelError(f"Marzban connection failed: {type(exc).__name__}") from exc

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self._token:
            await self._login()
        token = self._token
        try:
            return await self._send(method, path, **kwargs)
        except PanelAuthError:
            if not self._username or not self._password:
                raise
            await self._login(token)
            return await self._send(method, path, **kwargs)

    async def ping(self) -> str:
        data = await self._request("GET", "/api/system")
        return str(data.get("version") or "unknown")

    async def list_inbounds(self) -> list[InboundInfo]:
        return []

    async def inbound_tags(self) -> dict[str, list[str]]:
        data = await self._request("GET", "/api/inbounds")
        return {
            str(protocol): [str(item["tag"]) for item in items if isinstance(item, dict) and item.get("tag")]
            for protocol, items in data.items()
            if isinstance(items, list)
        }

    async def list_users(self) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        while len(users) < MAX_USERS:
            page = await self._request("GET", "/api/users", params={"offset": len(users), "limit": PAGE_SIZE})
            items = page.get("users") if isinstance(page, dict) else None
            if not isinstance(items, list):
                raise PanelError("Marzban returned an invalid users list")
            users.extend(item for item in items if isinstance(item, dict))
            if (
                not items
                or len(items) < PAGE_SIZE
                or (page.get("total") is not None and len(users) >= int(page["total"]))
            ):
                return users
        raise PanelError("Marzban users list exceeds the import limit")

    async def list_accounts(self, target: int | None) -> list[RemoteAccount]:
        return [parse_account(user) for user in await self.list_users() if user_uuid(user)]

    async def get_account(self, target: int | None, email: str) -> RemoteAccount | None:
        try:
            data = await self._request("GET", f"/api/user/{quote(email, safe='')}")
        except MarzbanApiError as exc:
            if exc.status == 404:
                return None
            raise
        return parse_account(data)

    async def get_used_bytes(self, target: int | None, email: str) -> int | None:
        account = await self.get_account(target, email)
        return account.used_bytes if account else None

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        if not USERNAME_RE.fullmatch(spec.email):
            raise PanelError("Marzban account names must contain 3 to 32 supported characters")
        current = await self.get_account(target, spec.email)
        proxies = dict(current.raw.get("proxies") or {}) if current else {}
        proxies["vless"] = {**dict(proxies.get("vless") or {}), "id": spec.uuid}
        if spec.flow:
            proxies["vless"]["flow"] = spec.flow
        expiry = int(spec.expires_at.timestamp()) if spec.expires_at and spec.expires_at.year < 2099 else 0
        body: dict[str, Any] = {
            "proxies": proxies,
            "expire": expiry,
            "data_limit": spec.traffic_limit_bytes,
            "status": "active" if spec.enabled else "disabled",
        }
        if current is None:
            body.update(username=spec.email, note=spec.comment[:500], data_limit_reset_strategy="no_reset")
            # Explicitly send the field: the API's validator selects all available inbounds for an empty list.
            # Omitting it leaves Pydantic's default unchanged on some releases, producing an account with no links.
            body["inbounds"] = {"vless": self._inbound_tags}
            # The create API accepts active/on_hold only; disable in a second request when needed.
            body["status"] = "active"
            try:
                data = await self._request("POST", "/api/user", json=body)
            except MarzbanApiError as exc:
                if exc.status != 409:
                    raise
                data = await self._request(
                    "PUT",
                    f"/api/user/{quote(spec.email, safe='')}",
                    json={k: v for k, v in body.items() if k != "username"},
                )
            if not spec.enabled:
                data = await self._request(
                    "PUT", f"/api/user/{quote(spec.email, safe='')}", json={"status": "disabled"}
                )
        else:
            # Do not replace inbounds, notes, other protocols or native subscription tokens of imported users.
            data = await self._request("PUT", f"/api/user/{quote(spec.email, safe='')}", json=body)
        return parse_account(data)

    async def delete_account(self, target: int | None, email: str, uuid: str) -> None:
        account = await self.get_account(target, email)
        if account is None:
            return
        if account.uuid.lower() != uuid.lower():
            raise PanelError("Marzban account identity changed; refusing to delete it")
        try:
            await self._request("DELETE", f"/api/user/{quote(email, safe='')}")
        except MarzbanApiError as exc:
            if exc.status != 404:
                raise

    async def get_links(self, account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        current = await self.get_account(None, account.email)
        if current is None:
            return []
        if current.uuid.lower() != account.uuid.lower():
            raise PanelError("Marzban account identity changed; refusing to return its links")
        links = current.raw.get("links")
        return [value for value in links or [] if isinstance(value, str) and value.startswith("vless://")]

    async def reset_traffic(self, target: int | None, email: str) -> None:
        try:
            await self._request("POST", f"/api/user/{quote(email, safe='')}/reset")
        except MarzbanApiError as exc:
            if exc.status != 404:
                raise

    async def reset_devices(self, account: RemoteAccount) -> None:
        raise PanelUnsupportedError("Marzban does not expose device unbinding")

    async def server_status(self) -> ServerStatus:
        data, core = await asyncio.gather(self._request("GET", "/api/system"), self._request("GET", "/api/core"))
        started = bool(core.get("started"))
        return ServerStatus(
            cpu_percent=float(data.get("cpu_usage") or 0),
            cpu_cores=data.get("cpu_cores"),
            mem_used=data.get("mem_used"),
            mem_total=data.get("mem_total"),
            net_up_bps=data.get("outgoing_bandwidth_speed"),
            net_down_bps=data.get("incoming_bandwidth_speed"),
            traffic_sent=data.get("outgoing_bandwidth"),
            traffic_recv=data.get("incoming_bandwidth"),
            online_users=data.get("online_users"),
            core_version=str(core.get("version") or ""),
            core_state="running" if started else "stop",
            uptime_sec=None,  # the core stats only carry a running flag
        )

    async def restart_core(self) -> None:
        await self._request("POST", "/api/core/restart")

    # --- optional capability: Telegram-only account (core config rules) -----------------------------
    async def _require_regexp_users(self) -> None:
        """Marzban's Xray email is ``<id>.<username>`` and the id is not exposed: the rule uses a ``regexp:`` user
        match, which only Xray >= 24.10.16 understands. Refuse to proceed on an older core (here or on a node)."""
        core = await self._request("GET", "/api/core")
        versions = [str(core.get("version") or "") if isinstance(core, Mapping) else ""]
        nodes = await self._request("GET", "/api/nodes")
        for node in nodes if isinstance(nodes, list) else []:
            if isinstance(node, Mapping) and node.get("status") != "disabled":
                versions.append(str(node.get("xray_version") or ""))
        old = [v for v in versions if not regexp_user_supported(v)]
        if old:
            raise PanelError(
                f"Marzban: the Xray core ({old[0] or 'unknown version'}) is older than 24.10.16; "
                "update it to use the Telegram-only subscription"
            )

    async def _edit_core_config(self, editor: Callable[[dict[str, Any]], tuple[dict[str, Any], bool]]) -> bool:
        """Read the core config, apply ``editor``, save it when it changed (the core restarts); returns the change."""
        config = await self._request("GET", "/api/core/config")
        if not isinstance(config, dict):
            raise PanelError("Marzban returned an invalid core config")
        try:
            updated, changed = editor(config)
        except TemplateError as exc:
            raise PanelError(f"Marzban: {exc}") from exc
        if changed:
            await self._request("PUT", "/api/core/config", json=updated)
        return changed

    async def ensure_tg_only(self, spec: AccountSpec) -> TgOnlyAccess:
        """The user is created disabled, the rules go into the core config, then the user is enabled."""
        await self._require_regexp_users()
        if await self.get_account(None, spec.email) is None:
            await self.upsert_account(None, replace(spec, enabled=False))
        match = marzban_user_match(spec.email)
        await self._edit_core_config(lambda config: set_tg_only_config(config, [match]))
        account = await self.upsert_account(None, spec)
        return TgOnlyAccess(tuple(await self.get_links(account)))

    async def remove_tg_only(self, email: str) -> None:
        """Delete the user first, then the rules."""
        account = await self.get_account(None, email)
        if account is not None:
            await self.delete_account(None, email, account.uuid)
        await self._edit_core_config(lambda config: set_tg_only_config(config, []))
