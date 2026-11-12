"""Client of the "Мой налог" web cabinet API (lknpd.nalog.ru) for self-employed income receipts.

The tax service has no official public API; this is the API of its web cabinet, the same one the widely used
open-source clients talk to. Authentication returns an access ``token`` and a ``refreshToken``; the refresh
token is rotated on every successful refresh, so the caller must persist the returned :class:`NpdSession`.

Times are sent as Moscow wall-clock time with an offset (the service records the Moscow time).
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger(__name__)

BASE_URL = "https://lknpd.nalog.ru/api"
MOSCOW = ZoneInfo("Europe/Moscow")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_2_2) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/88.0.4324.192 Safari/537.36"
)
APP_VERSION = "1.0.0"
CANCEL_REFUND = "Возврат средств"
CANCEL_MISTAKE = "Чек сформирован ошибочно"
DEFAULT_TIMEOUT = 20.0


class NpdError(Exception):
    """The request failed. ``maybe_applied``: the service may have acted on it (timeout, 5xx, broken reply)."""

    def __init__(self, message: str, *, status: int | None = None, maybe_applied: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.maybe_applied = maybe_applied


class NpdAuthError(NpdError):
    """Credentials or tokens were rejected; an admin has to log in again."""


@dataclass(slots=True)
class NpdSession:
    """Tokens of a logged-in cabinet (store encrypted)."""

    token: str
    refresh_token: str
    inn: str | None = None

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, raw: str) -> NpdSession:
        data = json.loads(raw)
        return cls(token=str(data["token"]), refresh_token=str(data["refresh_token"]), inn=data.get("inn"))


@dataclass(frozen=True, slots=True)
class Income:
    """One registered income from the cabinet's list."""

    receipt_uuid: str
    name: str
    total_kop: int
    operation_time: str
    cancelled: bool


def new_device_id() -> str:
    """A random 21-character device id, like the web cabinet generates."""
    return secrets.token_hex(11)[:21]


def moscow_iso(moment: datetime) -> str:
    """``2026-11-09T15:04:05+03:00`` for an aware datetime."""
    return moment.astimezone(MOSCOW).isoformat(timespec="seconds")


def kop_to_amount(kop: int) -> str:
    return f"{Decimal(kop) / 100:.2f}"


def receipt_url(inn: str, receipt_uuid: str, base_url: str = BASE_URL) -> str:
    """Public printable receipt (no login needed)."""
    return f"{base_url.rstrip('/')}/v1/receipt/{inn}/{receipt_uuid}/print"


def _session_from(data: Any, fallback_inn: str | None) -> NpdSession:
    if not isinstance(data, dict) or not data.get("token") or not data.get("refreshToken"):
        raise NpdError("authentication reply has no tokens", maybe_applied=False)
    profile = data.get("profile")
    inn = (str(profile.get("inn") or "") if isinstance(profile, dict) else "") or fallback_inn
    return NpdSession(token=str(data["token"]), refresh_token=str(data["refreshToken"]), inn=inn)


class NpdClient:
    """One aiohttp session per client; close it with :meth:`close` (or use ``async with``)."""

    def __init__(self, device_id: str, *, base_url: str = BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._device_id = device_id
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._http: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> NpdClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    @property
    def base_url(self) -> str:
        return self._base

    def _device_info(self) -> dict[str, Any]:
        return {
            "sourceType": "WEB",
            "sourceDeviceId": self._device_id,
            "appVersion": APP_VERSION,
            "metaDetails": {"userAgent": USER_AGENT},
        }

    def _client(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Referer": "https://lknpd.nalog.ru/auth/login",
                },
            )
        return self._http

    async def _call(
        self,
        method: str,
        path: str,
        *,
        version: str = "v1",
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> Any:
        url = f"{self._base}/{version}{path}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with self._client().request(method, url, json=body, params=params, headers=headers) as resp:
                text = await resp.text()
                status = resp.status
        except aiohttp.ClientConnectorError as exc:
            raise NpdError(f"cannot connect: {type(exc).__name__}", maybe_applied=False) from exc
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise NpdError(f"request failed: {type(exc).__name__}", maybe_applied=True) from exc
        if status == 401:
            raise NpdAuthError("unauthorized", status=status)
        if status >= 400:
            raise NpdError(f"HTTP {status}: {_error_text(text)}", status=status, maybe_applied=status >= 500)
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as exc:
            raise NpdError("reply is not JSON", status=status, maybe_applied=True) from exc

    # --- authentication ----------------------------------------------------------------------------------------
    async def login_password(self, inn: str, password: str) -> NpdSession:
        """``POST /v1/auth/lkfl`` with the INN and the cabinet password."""
        try:
            data = await self._call(
                "POST",
                "/auth/lkfl",
                body={"username": inn, "password": password, "deviceInfo": self._device_info()},
            )
        except NpdError as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                raise NpdAuthError(f"login rejected (HTTP {exc.status})", status=exc.status) from exc
            raise
        return _session_from(data, inn)

    async def start_sms(self, phone: str) -> tuple[str, int]:
        """``POST /v2/auth/challenge/sms/start``; returns ``(challenge_token, expires_in_sec)``."""
        data = await self._call(
            "POST", "/auth/challenge/sms/start", version="v2", body={"phone": phone, "requireTpToBeActive": True}
        )
        if not isinstance(data, dict) or not data.get("challengeToken"):
            raise NpdError("no challenge token in the reply")
        return str(data["challengeToken"]), int(data.get("expireIn") or 0)

    async def verify_sms(self, phone: str, challenge_token: str, code: str) -> NpdSession:
        """``POST /v1/auth/challenge/sms/verify`` with the code from the SMS."""
        data = await self._call(
            "POST",
            "/auth/challenge/sms/verify",
            body={
                "phone": phone,
                "code": code,
                "challengeToken": challenge_token,
                "deviceInfo": self._device_info(),
            },
        )
        return _session_from(data, None)

    async def refresh(self, refresh_token: str, inn: str | None = None) -> NpdSession:
        """``POST /v1/auth/token``; the reply carries a new refresh token, which replaces the old one."""
        try:
            data = await self._call(
                "POST", "/auth/token", body={"deviceInfo": self._device_info(), "refreshToken": refresh_token}
            )
        except NpdError as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                raise NpdAuthError(f"refresh rejected (HTTP {exc.status})", status=exc.status) from exc
            raise
        return _session_from(data, inn)

    # --- incomes -----------------------------------------------------------------------------------------------
    async def create_income(
        self, token: str, name: str, amount_kop: int, operation_time: datetime, request_time: datetime
    ) -> str:
        """Register income from an individual (``POST /v1/income``); returns the receipt uuid."""
        amount = kop_to_amount(amount_kop)
        body = {
            "operationTime": moscow_iso(operation_time),
            "requestTime": moscow_iso(request_time),
            "services": [{"name": name, "amount": amount, "quantity": 1}],
            "totalAmount": amount,
            "client": {"contactPhone": None, "displayName": None, "incomeType": "FROM_INDIVIDUAL", "inn": None},
            "paymentType": "CASH",
            "ignoreMaxTotalIncomeRestriction": False,
        }
        data = await self._call("POST", "/income", body=body, token=token)
        uuid = data.get("approvedReceiptUuid") if isinstance(data, dict) else None
        if not uuid:
            raise NpdError("no receipt uuid in the reply", maybe_applied=True)
        return str(uuid)

    async def list_incomes(self, token: str, since: datetime, until: datetime, limit: int = 100) -> list[Income]:
        """Incomes registered between ``since`` and ``until`` (``GET /v1/incomes``, newest first)."""
        data = await self._call(
            "GET",
            "/incomes",
            params={
                "from": moscow_iso(since),
                "to": moscow_iso(until),
                "offset": 0,
                "limit": max(1, min(limit, 100)),
                "sortBy": "operation_time:desc",
            },
            token=token,
        )
        content = data.get("content") if isinstance(data, dict) else None
        result: list[Income] = []
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict) or not item.get("approvedReceiptUuid"):
                continue
            try:
                total = int((Decimal(str(item.get("totalAmount") or 0)) * 100).to_integral_value())
            except ArithmeticError:
                continue
            result.append(
                Income(
                    receipt_uuid=str(item["approvedReceiptUuid"]),
                    name=str(item.get("name") or ""),
                    total_kop=total,
                    operation_time=str(item.get("operationTime") or ""),
                    cancelled=item.get("cancellationInfo") is not None,
                )
            )
        return result

    async def cancel_income(self, token: str, receipt_uuid: str, now: datetime, comment: str = CANCEL_REFUND) -> None:
        """Annul a receipt (``POST /v1/cancel``)."""
        await self._call(
            "POST",
            "/cancel",
            body={
                "operationTime": moscow_iso(now),
                "requestTime": moscow_iso(now),
                "comment": comment,
                "receiptUuid": receipt_uuid,
                "partnerCode": None,
            },
            token=token,
        )


def _error_text(text: str) -> str:
    """A short error description from the reply (never the request)."""
    try:
        data = json.loads(text)
    except ValueError:
        return text[:200]
    if isinstance(data, dict):
        for key in ("message", "code", "exceptionMessage"):
            if data.get(key):
                return str(data[key])[:200]
    return text[:200]
