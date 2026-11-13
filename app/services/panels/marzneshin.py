"""Async Marzneshin API adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID

import aiohttp

from app.db.models import PanelKind
from app.services.panels import sublinks
from app.services.panels.base import (
    AccountSpec,
    InboundInfo,
    PanelAuthError,
    PanelError,
    PanelUnsupportedError,
    RemoteAccount,
    TgOnlyAccess,
)
from app.services.panels.xray_template import TemplateError, set_tg_only_config

PAGE_SIZE = 100
MAX_USERS = 100_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class MarzneshinApiError(PanelError):
    def __init__(self, status: int) -> None:
        super().__init__(f"Marzneshin API returned HTTP {status}")
        self.status = status


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return ""


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    except ValueError:
        return None


def parse_account(user: Mapping[str, Any]) -> RemoteAccount:
    return RemoteAccount(
        email=str(user.get("username") or ""),
        uuid=_uuid(user.get("key")),
        enabled=bool(user.get("enabled")),
        expires_at=_date(user.get("expire_date")),
        device_limit=0,
        traffic_limit_bytes=max(0, int(user.get("data_limit") or 0)),
        used_bytes=max(0, int(user.get("used_traffic") or 0)),
        remote_id=str(user.get("id") or "") or None,
        subscription_url=str(user.get("subscription_url") or "") or None,
        raw=dict(user),
    )


class MarzneshinBackend:
    kind = PanelKind.MARZNESHIN

    def __init__(
        self,
        url: str,
        *,
        username: str | None,
        password: str | None,
        tls_verify: bool = True,
        timeout: float = 15,
        extra_headers: Mapping[str, str] | None = None,
        service_ids: list[int] | None = None,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Marzneshin requires an HTTP(S) URL without embedded credentials")
        if not username or not password:
            raise ValueError("Marzneshin needs a username and password")
        self._url, self._username, self._password = url.rstrip("/"), username, password
        self._tls_verify, self._timeout, self._headers, self._service_ids = (
            tls_verify,
            timeout,
            dict(extra_headers or {}),
            service_ids or [],
        )
        self._token: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout))
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def _send(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> Any:
        headers = {**self._headers, "Accept": "application/json"}
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with (await self._client()).request(
                method, self._url + path, headers=headers, ssl=self._tls_verify, allow_redirects=False, **kwargs
            ) as response:
                if response.status in (401, 403):
                    raise PanelAuthError(f"Marzneshin access denied (HTTP {response.status})")
                if response.status >= 300:
                    raise MarzneshinApiError(response.status)
                if response.status == 204:
                    return {}
                body = await response.read()
                if len(body) > MAX_RESPONSE_BYTES:
                    raise PanelError("Marzneshin response is too large")
                try:
                    return json.loads(body)
                except ValueError as exc:
                    raise PanelError("Marzneshin returned invalid JSON") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise PanelError(f"Marzneshin connection failed: {type(exc).__name__}") from exc

    async def _login(self, stale: str | None = None) -> None:
        async with self._lock:
            if self._token and self._token != stale:
                return
            data = await self._send(
                "POST", "/api/admins/token", auth=False, data={"username": self._username, "password": self._password}
            )
            token = data.get("access_token") if isinstance(data, Mapping) else None
            if not isinstance(token, str) or not token:
                raise PanelAuthError("Marzneshin returned no access token")
            self._token = token

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self._token:
            await self._login()
        token = self._token
        try:
            return await self._send(method, path, **kwargs)
        except PanelAuthError:
            await self._login(token)
            return await self._send(method, path, **kwargs)

    async def ping(self) -> str:
        await self._request("GET", "/api/admins/current")
        return "connected"

    async def list_inbounds(self) -> list[InboundInfo]:
        return []

    async def list_accounts(self, target: int | None) -> list[RemoteAccount]:
        accounts: list[RemoteAccount] = []
        for page in range(1, MAX_USERS // PAGE_SIZE + 1):
            data = await self._request("GET", "/api/users", params={"page": page, "size": PAGE_SIZE})
            items = data.get("items") if isinstance(data, Mapping) else None
            if not isinstance(items, list):
                raise PanelError("Marzneshin returned an invalid users list")
            accounts.extend(a for row in items if isinstance(row, Mapping) and (a := parse_account(row)).email)
            pages = data.get("pages") if isinstance(data, Mapping) else None
            if not items or (isinstance(pages, int) and page >= pages) or len(items) < PAGE_SIZE:
                return accounts
        raise PanelError("Marzneshin users list exceeds the import limit")

    async def get_account(self, target: int | None, email: str) -> RemoteAccount | None:
        try:
            data = await self._request("GET", f"/api/users/{quote(email, safe='')}")
        except MarzneshinApiError as exc:
            if exc.status == 404:
                return None
            raise
        return parse_account(data) if isinstance(data, Mapping) else None

    async def get_used_bytes(self, target: int | None, email: str) -> int | None:
        account = await self.get_account(target, email)
        return account.used_bytes if account else None

    def _payload(self, spec: AccountSpec) -> dict[str, Any]:
        return {
            "expire_strategy": "fixed_date" if spec.expires_at else "never",
            "expire_date": spec.expires_at.astimezone(UTC).isoformat() if spec.expires_at else None,
            "data_limit": spec.traffic_limit_bytes,
            "data_limit_reset_strategy": "no_reset",
            "note": spec.comment[:500] or None,
            "service_ids": self._service_ids,
        }

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        current = await self.get_account(target, spec.email)
        body = self._payload(spec)
        if current is None:
            data = await self._request(
                "POST", "/api/users", json={**body, "username": spec.email, "key": spec.uuid.replace("-", "")}
            )
            if not spec.enabled:
                data = await self._request("POST", f"/api/users/{quote(spec.email, safe='')}/disable")
        else:
            data = await self._request("PUT", f"/api/users/{quote(spec.email, safe='')}", json=body)
            if current.enabled != spec.enabled:
                data = await self._request(
                    "POST", f"/api/users/{quote(spec.email, safe='')}/{'enable' if spec.enabled else 'disable'}"
                )
        account = parse_account(data)
        if account.email != spec.email or account.uuid.lower() != spec.uuid.lower():
            raise PanelError("Marzneshin returned a user with an unexpected identity")
        return account

    async def delete_account(self, target: int | None, email: str, uuid: str) -> None:
        account = await self.get_account(target, email)
        if account is None:
            return
        if account.uuid.lower() != uuid.lower():
            raise PanelError("Marzneshin account identity changed; refusing to delete it")
        await self._request("DELETE", f"/api/users/{quote(email, safe='')}")

    async def get_links(self, account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        current = await self.get_account(None, account.email)
        if current is None or not current.subscription_url:
            return []
        if current.uuid.lower() != account.uuid.lower():
            raise PanelError("Marzneshin account identity changed; refusing to return its links")
        return await sublinks.fetch_links(
            await self._client(),
            self._url,
            current.subscription_url,
            headers=self._headers,
            tls_verify=self._tls_verify,
            name="Marzneshin",
        )

    async def reset_devices(self, account: RemoteAccount) -> None:
        raise PanelUnsupportedError("Marzneshin has no per-user device reset API")

    async def reset_traffic(self, target: int | None, email: str) -> None:
        try:
            await self._request("POST", f"/api/users/{quote(email, safe='')}/reset")
        except MarzneshinApiError as exc:
            if exc.status != 404:
                raise

    # --- optional capability: Telegram-only account (node Xray config rules) -------------------------
    async def _xray_nodes(self) -> list[tuple[int, str]]:
        """``(node id, backend name)`` of every enabled node's Xray backend."""
        found: list[tuple[int, str]] = []
        for page in range(1, MAX_USERS // PAGE_SIZE + 1):
            data = await self._request("GET", "/api/nodes", params={"page": page, "size": PAGE_SIZE})
            items = data.get("items") if isinstance(data, Mapping) else None
            if not isinstance(items, list):
                raise PanelError("Marzneshin returned an invalid node list")
            for node in items:
                if not isinstance(node, Mapping) or node.get("status") == "disabled" or node.get("id") is None:
                    continue
                for backend in node.get("backends") or []:
                    if isinstance(backend, Mapping) and backend.get("backend_type") == "xray" and backend.get("name"):
                        found.append((int(node["id"]), str(backend["name"])))
            pages = data.get("pages") if isinstance(data, Mapping) else None
            if not items or (isinstance(pages, int) and page >= pages) or len(items) < PAGE_SIZE:
                return found
        return found

    async def _edit_nodes(self, users: list[str]) -> None:
        """Write the Telegram-only rules (matching ``users``, the Xray emails) into every node's Xray config, or
        remove them when ``users`` is empty. A changed config is saved through the node, which restarts Xray."""
        for node_id, backend in await self._xray_nodes():
            path = f"/api/nodes/{node_id}/{quote(backend, safe='')}/config"
            data = await self._request("GET", path)
            try:
                config = json.loads(str(data.get("config") or "")) if isinstance(data, Mapping) else None
            except ValueError:
                config = None
            if not isinstance(config, dict):
                raise PanelError(f"Marzneshin node {node_id}: the Xray config is not JSON")
            try:
                updated, changed = set_tg_only_config(config, users)
            except TemplateError as exc:
                raise PanelError(f"Marzneshin node {node_id}: {exc}") from exc
            if changed:
                body = {"config": json.dumps(updated, ensure_ascii=False, indent=2), "format": 1}
                await self._request("PUT", path, json=body)

    async def ensure_tg_only(self, spec: AccountSpec) -> TgOnlyAccess:
        """marznode names the Xray client ``<id>.<username>``: the user is created disabled, then the nodes get the
        rules for that exact name, then the user is enabled."""
        current = await self.get_account(None, spec.email)
        if current is not None and current.uuid.lower() != spec.uuid.lower():  # rotated id: the user is recreated
            await self.delete_account(None, spec.email, current.uuid)
            current = None
        fresh = current is None
        account = await self.upsert_account(None, replace(spec, enabled=spec.enabled and not fresh))
        if not account.remote_id:
            raise PanelError("Marzneshin returned a user without an id")
        await self._edit_nodes([f"{account.remote_id}.{account.email}"])
        if fresh and spec.enabled:
            account = await self.upsert_account(None, spec)
        return TgOnlyAccess(tuple(await self.get_links(account)))

    async def remove_tg_only(self, email: str) -> None:
        """Delete the user first, then the rules."""
        account = await self.get_account(None, email)
        if account is not None:
            await self.delete_account(None, email, account.uuid)
        await self._edit_nodes([])
