"""Asynchronous client for the PasarGuard administrator API."""

from __future__ import annotations

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

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class PasarGuardApiError(PanelError):
    def __init__(self, status: int) -> None:
        super().__init__(f"PasarGuard API returned HTTP {status}")
        self.status = status


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return ""


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _account_uuid(user: Mapping[str, Any]) -> str:
    settings = user.get("proxy_settings")
    if not isinstance(settings, Mapping):
        return ""
    for protocol in ("vless", "vmess"):
        item = settings.get(protocol)
        if isinstance(item, Mapping) and (value := _uuid(item.get("id"))):
            return value
    return ""


def parse_account(user: Mapping[str, Any]) -> RemoteAccount:
    status = str(user.get("status") or "")
    return RemoteAccount(
        email=str(user.get("username") or ""),
        uuid=_account_uuid(user),
        enabled=status == "active",
        expires_at=_datetime(user.get("expire")),
        device_limit=_integer(user.get("hwid_limit")),
        traffic_limit_bytes=_integer(user.get("data_limit")),
        used_bytes=_integer(user.get("used_traffic")),
        remote_id=str(user.get("id")) if user.get("id") is not None else None,
        subscription_url=str(user.get("subscription_url") or "") or None,
        raw=dict(user),
    )


class PasarGuardBackend:
    """PasarGuard uses an ``ApiKey`` Authorization header and exact byte traffic limits."""

    kind = PanelKind.PASARGUARD

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        tls_verify: bool = True,
        timeout: float = 15,
        extra_headers: Mapping[str, str] | None = None,
        group_ids: list[int] | None = None,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("PasarGuard requires an HTTP(S) URL without embedded credentials")
        if not api_key.strip():
            raise ValueError("PasarGuard needs an API key")
        self._url, self._api_key = url.rstrip("/"), api_key
        self._tls_verify, self._timeout = tls_verify, timeout
        self._headers = dict(extra_headers or {})
        self._group_ids = list(group_ids or [])
        self._session: aiohttp.ClientSession | None = None

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout))
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        session = await self._client()
        headers = {**self._headers, "Accept": "application/json", "Authorization": f"ApiKey {self._api_key}"}
        try:
            async with session.request(
                method, self._url + path, headers=headers, ssl=self._tls_verify, allow_redirects=False, **kwargs
            ) as response:
                if response.status in (401, 403):
                    raise PanelAuthError(f"PasarGuard access denied (HTTP {response.status})")
                if response.status >= 300:
                    raise PasarGuardApiError(response.status)
                if response.status == 204:
                    return {}
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise PanelError("PasarGuard response is too large")
                if not body:
                    return {}
                try:
                    return json.loads(body)
                except ValueError as exc:
                    raise PanelError("PasarGuard returned invalid JSON") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise PanelError(f"PasarGuard connection failed: {type(exc).__name__}") from exc

    async def ping(self) -> str:
        data = await self._request("GET", "/api/system/resources")
        return str(data.get("version") or "unknown") if isinstance(data, Mapping) else "unknown"

    async def list_inbounds(self) -> list[InboundInfo]:
        return []

    async def list_accounts(self, target: int | None) -> list[RemoteAccount]:
        data = await self._request("GET", "/api/users", params={"offset": 0, "limit": 10000})
        users = data.get("users") if isinstance(data, Mapping) else None
        if not isinstance(users, list):
            raise PanelError("PasarGuard returned an invalid users list")
        return [account for row in users if isinstance(row, Mapping) and (account := parse_account(row)).email]

    async def get_account(self, target: int | None, email: str) -> RemoteAccount | None:
        try:
            data = await self._request("GET", f"/api/user/{quote(email, safe='')}")
        except PasarGuardApiError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, Mapping):
            raise PanelError("PasarGuard returned an invalid user")
        account = parse_account(data)
        if account.email != email:
            raise PanelError("PasarGuard returned a user with an unexpected identity")
        return account

    async def get_used_bytes(self, target: int | None, email: str) -> int | None:
        account = await self.get_account(target, email)
        return account.used_bytes if account else None

    @staticmethod
    def _payload(spec: AccountSpec) -> dict[str, Any]:
        return {
            "status": "active" if spec.enabled else "disabled",
            "expire": spec.expires_at.astimezone(UTC).isoformat() if spec.expires_at else 0,
            "data_limit": max(0, spec.traffic_limit_bytes),
            "data_limit_reset_strategy": "no_reset",
            "hwid_limit": max(0, spec.device_limit),
            "note": spec.comment[:500] or None,
        }

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        current = await self.get_account(target, spec.email)
        payload = self._payload(spec)
        if current is None:
            payload["username"] = spec.email
            payload["proxy_settings"] = {"vmess": {"id": spec.uuid}, "vless": {"id": spec.uuid}}
            if self._group_ids:
                payload["group_ids"] = self._group_ids
            data = await self._request("POST", "/api/user", json=payload)
        else:
            data = await self._request("PUT", f"/api/user/{quote(spec.email, safe='')}", json=payload)
        if not isinstance(data, Mapping):
            raise PanelError("PasarGuard returned an invalid user")
        account = parse_account(data)
        if account.email != spec.email or account.uuid.lower() != spec.uuid.lower():
            raise PanelError("PasarGuard returned a user with an unexpected identity")
        return account

    async def delete_account(self, target: int | None, email: str, uuid: str) -> None:
        account = await self.get_account(target, email)
        if account is None:
            return
        if account.uuid.lower() != uuid.lower():
            raise PanelError("PasarGuard account identity changed; refusing to delete it")
        try:
            await self._request("DELETE", f"/api/user/{quote(email, safe='')}")
        except PasarGuardApiError as exc:
            if exc.status != 404:
                raise

    async def get_links(self, account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        current = await self.get_account(None, account.email)
        if current is None or not current.subscription_url:
            return []
        if current.uuid.lower() != account.uuid.lower():
            raise PanelError("PasarGuard account identity changed; refusing to return its links")
        return await sublinks.fetch_links(
            await self._client(),
            self._url,
            current.subscription_url,
            headers=self._headers,
            tls_verify=self._tls_verify,
            name="PasarGuard",
        )

    async def reset_devices(self, account: RemoteAccount) -> None:
        raise PanelUnsupportedError("PasarGuard does not expose per-user device reset through its administrator API")

    async def reset_traffic(self, target: int | None, email: str) -> None:
        try:
            await self._request("POST", f"/api/user/{quote(email, safe='')}/reset")
        except PasarGuardApiError as exc:
            if exc.status != 404:
                raise

    # --- optional capability: Telegram-only account (core config rules) -----------------------------
    async def _edit_cores(self, users: list[str]) -> None:
        """Write the Telegram-only rules (matching ``users``, the Xray emails) into every Xray core, or remove them
        when ``users`` is empty. A changed core is saved with ``restart_nodes`` (routing edits apply live)."""
        data = await self._request("GET", "/api/cores")
        cores = data.get("cores") if isinstance(data, Mapping) else None
        if not isinstance(cores, list):
            raise PanelError("PasarGuard returned an invalid core list")
        for core in cores:
            if not isinstance(core, Mapping) or not isinstance(core.get("config"), dict):
                continue
            if core.get("type") not in (None, "xray"):
                continue
            try:
                updated, changed = set_tg_only_config(dict(core["config"]), users)
            except TemplateError as exc:
                raise PanelError(f"PasarGuard core {core.get('name')!r}: {exc}") from exc
            if not changed:
                continue
            body = {
                "name": core.get("name"),
                "config": updated,
                "type": core.get("type"),
                "exclude_inbound_tags": list(core.get("exclude_inbound_tags") or []),
                "fallbacks_inbound_tags": list(core.get("fallbacks_inbound_tags") or []),
            }
            await self._request("PUT", f"/api/core/{int(core['id'])}", params={"restart_nodes": "true"}, json=body)

    async def ensure_tg_only(self, spec: AccountSpec) -> TgOnlyAccess:
        """PasarGuard names the Xray client after the numeric user id: the user is created disabled, its id goes into
        the cores' rules, then the user is enabled."""
        current = await self.get_account(None, spec.email)
        if current is not None and current.uuid.lower() != spec.uuid.lower():  # rotated id: the user is recreated
            await self.delete_account(None, spec.email, current.uuid)
            current = None
        fresh = current is None
        account = await self.upsert_account(None, replace(spec, enabled=spec.enabled and not fresh))
        if account.remote_id is None:
            raise PanelError("PasarGuard returned a user without an id")
        await self._edit_cores([account.remote_id])
        if fresh and spec.enabled:
            account = await self.upsert_account(None, spec)
        return TgOnlyAccess(tuple(await self.get_links(account)))

    async def remove_tg_only(self, email: str) -> None:
        """Delete the user first, then the rules."""
        account = await self.get_account(None, email)
        if account is not None:
            await self.delete_account(None, email, account.uuid)
        await self._edit_cores([])
