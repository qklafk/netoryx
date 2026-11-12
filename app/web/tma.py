"""Telegram Mini App ``initData`` validation (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

from app.utils.security import const_eq

INIT_DATA_MAX_AGE = timedelta(hours=24)
CLOCK_SKEW = timedelta(minutes=5)
MAX_INIT_DATA_LENGTH = 8192


class InitDataError(ValueError):
    """``initData`` is malformed, forged or expired."""


@dataclass(frozen=True, slots=True)
class WebAppUser:
    id: int
    first_name: str | None
    last_name: str | None
    username: str | None
    language_code: str | None
    is_premium: bool = False


@dataclass(frozen=True, slots=True)
class WebAppInitData:
    user: WebAppUser
    auth_date: datetime
    query_id: str | None
    start_param: str | None
    chat_type: str | None
    fields: dict[str, str]


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """The ``hash`` Telegram would put into ``initData`` with ``fields`` (also handy for tests and dev tools)."""
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    return hmac.new(_secret_key(bot_token), data_check.encode(), hashlib.sha256).hexdigest()


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: timedelta = INIT_DATA_MAX_AGE,
    now: datetime | None = None,
) -> WebAppInitData:
    """Verify the HMAC of ``initData`` and its freshness; returns the parsed data or raises InitDataError."""
    if not init_data or len(init_data) > MAX_INIT_DATA_LENGTH:
        raise InitDataError("empty or oversized initData")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("malformed initData") from exc
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise InitDataError(f"duplicate field {key!r}")
        fields[key] = value
    received_hash = fields.pop("hash", "")
    if not received_hash:
        raise InitDataError("hash is missing")
    expected = sign_init_data(fields, bot_token)
    if not const_eq(expected, received_hash.lower()):
        raise InitDataError("hash mismatch")

    try:
        auth_date = datetime.fromtimestamp(int(fields.get("auth_date", "")), UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise InitDataError("invalid auth_date") from exc
    current = now or datetime.now(UTC)
    if auth_date > current + CLOCK_SKEW:
        raise InitDataError("auth_date is in the future")
    if current - auth_date > max_age:
        raise InitDataError("initData expired")

    user = _parse_user(fields.get("user"))
    return WebAppInitData(
        user=user,
        auth_date=auth_date,
        query_id=fields.get("query_id"),
        start_param=fields.get("start_param"),
        chat_type=fields.get("chat_type"),
        fields=fields,
    )


def _parse_user(raw: str | None) -> WebAppUser:
    if not raw:
        raise InitDataError("user is missing")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InitDataError("user is not valid JSON") from exc
    if not isinstance(data, dict):
        raise InitDataError("user is not an object")
    uid = data.get("id")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise InitDataError("user.id is invalid")
    if data.get("is_bot"):
        raise InitDataError("bots cannot use the Mini App")

    def text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value else None

    return WebAppUser(
        id=uid,
        first_name=text("first_name"),
        last_name=text("last_name"),
        username=text("username"),
        language_code=text("language_code"),
        is_premium=data.get("is_premium") is True,
    )
