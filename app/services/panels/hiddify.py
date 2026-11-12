"""Asynchronous client for the Hiddify Manager v2 administrator API."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
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
)

BYTES_PER_GIB = 1024**3
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class HiddifyApiError(PanelError):
    def __init__(self, status: int) -> None:
        super().__init__(f"Hiddify API returned HTTP {status}")
        self.status = status


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return ""


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_account(user: Mapping[str, Any]) -> RemoteAccount:
    start = _date(user.get("start_date"))
    try:
        days = int(user.get("package_days") or 0)
    except (TypeError, ValueError):
        days = 0
    expires_at = (
        datetime.combine(start, datetime.min.time(), UTC) + timedelta(days=days) if start and days > 0 else None
    )
    limit = _number(user.get("usage_limit_GB"))
    used = _number(user.get("current_usage_GB"))
    return RemoteAccount(
        email=str(user.get("name") or ""),
        uuid=_uuid(user.get("uuid")),
        enabled=bool(user.get("enable", user.get("is_active", False))),
        expires_at=expires_at,
        device_limit=0,
        traffic_limit_bytes=max(0, round(limit * BYTES_PER_GIB)),
        used_bytes=max(0, round(used * BYTES_PER_GIB)),
        remote_id=_uuid(user.get("uuid")) or None,
        raw=dict(user),
    )


class HiddifyBackend:
    """Hiddify's configured administrator path is part of ``url``; the API key stays in a header."""

    kind = PanelKind.HIDDIFY

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        tls_verify: bool = True,
        timeout: float = 15,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Hiddify requires an HTTP(S) URL without embedded credentials")
        if not api_key.strip():
            raise ValueError("Hiddify needs an API key")
        self._url, self._api_key = url.rstrip("/"), api_key
        self._tls_verify, self._timeout = tls_verify, timeout
        self._headers = dict(extra_headers or {})
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
        headers = {**self._headers, "Accept": "application/json", "Hiddify-API-Key": self._api_key}
        try:
            async with session.request(
                method, self._url + path, headers=headers, ssl=self._tls_verify, allow_redirects=False, **kwargs
            ) as response:
                if response.status in (401, 403):
                    raise PanelAuthError(f"Hiddify access denied (HTTP {response.status})")
                if response.status >= 300:
                    raise HiddifyApiError(response.status)
                if response.status == 204:
                    return {}
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise PanelError("Hiddify response is too large")
                if not body:
                    return {}
                try:
                    return json.loads(body)
                except ValueError as exc:
                    raise PanelError("Hiddify returned invalid JSON") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise PanelError(f"Hiddify connection failed: {type(exc).__name__}") from exc

    async def ping(self) -> str:
        data = await self._request("GET", "/api/v2/panel/info/")
        return str(data.get("version") or "unknown") if isinstance(data, Mapping) else "unknown"

    async def list_inbounds(self) -> list[InboundInfo]:
        return []

    async def list_accounts(self, target: int | None) -> list[RemoteAccount]:
        data = await self._request("GET", "/api/v2/admin/user/")
        if not isinstance(data, list):
            raise PanelError("Hiddify returned an invalid users list")
        return [account for row in data if isinstance(row, Mapping) and (account := parse_account(row)).uuid]

    async def get_account(self, target: int | None, email: str) -> RemoteAccount | None:
        return next((account for account in await self.list_accounts(None) if account.email == email), None)

    async def get_used_bytes(self, target: int | None, email: str) -> int | None:
        account = await self.get_account(target, email)
        return account.used_bytes if account else None

    @staticmethod
    def _payload(spec: AccountSpec, *, now: datetime) -> dict[str, Any]:
        if spec.expires_at is None or spec.expires_at.year >= 2099:
            start_date, package_days = None, 0
        else:
            remaining = max(1, math.ceil((spec.expires_at - now).total_seconds() / 86400))
            start_date, package_days = now.date().isoformat(), remaining
        return {
            "name": spec.email,
            "enable": spec.enabled,
            "usage_limit_GB": spec.traffic_limit_bytes / BYTES_PER_GIB,
            "package_days": package_days,
            "start_date": start_date,
            "mode": "no_reset",
            "comment": spec.comment[:500] or None,
            "telegram_id": spec.tg_id,
        }

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        current = await self.get_account(target, spec.email)
        payload = self._payload(spec, now=datetime.now(UTC))
        if current is None:
            data = await self._request("POST", "/api/v2/admin/user/", json={"uuid": spec.uuid, **payload})
        else:
            data = await self._request("PATCH", f"/api/v2/admin/user/{quote(current.uuid, safe='')}/", json=payload)
        if not isinstance(data, Mapping):
            raise PanelError("Hiddify returned an invalid user")
        account = parse_account(data)
        if account.uuid.lower() != spec.uuid.lower():
            raise PanelError("Hiddify returned a user with an unexpected identity")
        return account

    async def delete_account(self, target: int | None, email: str, uuid: str) -> None:
        account = await self.get_account(target, email)
        if account is None:
            return
        if account.uuid.lower() != uuid.lower():
            raise PanelError("Hiddify account identity changed; refusing to delete it")
        try:
            await self._request("DELETE", f"/api/v2/admin/user/{quote(account.uuid, safe='')}/")
        except HiddifyApiError as exc:
            if exc.status != 404:
                raise

    async def get_links(self, account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        return []

    async def reset_devices(self, account: RemoteAccount) -> None:
        raise PanelUnsupportedError("Hiddify does not expose per-user device reset through its administrator API")

    async def reset_traffic(self, target: int | None, email: str) -> None:
        account = await self.get_account(target, email)
        if account is None:
            return
        await self._request(
            "PATCH", f"/api/v2/admin/user/{quote(account.uuid, safe='')}/", json={"current_usage_GB": 0}
        )
