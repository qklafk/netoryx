"""Effective settings = environment values overlaid with admin-edited values stored in the database.

Overrides live in the ``kv`` table as JSON under ``setting:<key>`` (secrets encrypted with the application
:class:`~app.utils.crypto.SecretBox`). Every change bumps ``settings_version`` so other processes pick it up
with :meth:`SettingsProvider.poll_changes`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import SecretStr, TypeAdapter, ValidationError
from sqlalchemy import select

from app.config import Settings
from app.core.settings_registry import ENV_ONLY, REGISTRY, SettingSpec, get_spec
from app.db.models import KeyValue
from app.db.session import SessionFactory, read_session
from app.services import audit, kv
from app.utils.crypto import DecryptionError, SecretBox

log = logging.getLogger(__name__)

KV_PREFIX = "setting:"
VERSION_KEY = "settings_version"

Origin = Literal["env", "db", "default"]
Subscriber = Callable[[Settings], Awaitable[None]]


class SettingValidationError(ValueError):
    """The new value is rejected (wrong type, out of range or inconsistent with other settings)."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key
        self.message = message


def _validation_message(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False)
    if not errors:
        return str(exc)
    first = errors[0]
    msg = str(first.get("msg", "invalid value"))
    return msg.removeprefix("Value error, ")


class SettingsProvider:
    """Source of the current :class:`Settings`; safe to share between tasks."""

    def __init__(self, env_settings: Settings, session_factory: SessionFactory, secret_box: SecretBox) -> None:
        self._env = env_settings
        self._session_factory = session_factory
        self._box = secret_box
        self._overrides: dict[str, Any] = {}  # key -> plain (decrypted, JSON-compatible) value
        self._ephemeral: dict[str, Any] = {}
        self._current = env_settings
        self._version: str | None = None
        self._subscribers: list[Subscriber] = []
        self._lock = asyncio.Lock()

    # --- reading ---------------------------------------------------------------------------------
    @property
    def env(self) -> Settings:
        return self._env

    @property
    def version(self) -> str | None:
        return self._version

    def current(self) -> Settings:
        return self._current

    def overridden_keys(self) -> set[str]:
        return set(self._overrides)

    def effective(self, key: str) -> tuple[Any, Origin]:
        """Current value of ``key`` and where it comes from."""
        if key not in Settings.model_fields:
            raise KeyError(f"unknown setting {key!r}")
        value = getattr(self._current, key)
        if key in self._overrides:
            return value, "db"
        if key in self._env.model_fields_set or key in self._ephemeral:
            return value, "env"
        return value, "default"

    # --- loading ---------------------------------------------------------------------------------
    async def load(self) -> Settings:
        """(Re)read overrides from the database. Invalid stored values are skipped with an error in the log."""
        async with self._lock:
            async with read_session(self._session_factory) as session:
                rows = (await session.execute(select(KeyValue).where(KeyValue.key.like(f"{KV_PREFIX}%")))).all()
                version = await kv.get(session, VERSION_KEY)
            overrides: dict[str, Any] = {}
            for (row,) in rows:
                key = row.key[len(KV_PREFIX) :]
                spec = REGISTRY.get(key)
                if spec is None or key in ENV_ONLY:
                    log.warning("ignoring stored setting %r: not editable", key)
                    continue
                try:
                    overrides[key] = self._decode(spec, row.value)
                except (ValueError, DecryptionError) as exc:
                    log.error("ignoring stored setting %r: %s", key, exc)
            settings, applied = self._build_tolerant(overrides)
            self._overrides = applied
            self._version = version
            changed = self._swap(settings)
        if changed:
            await self._notify(settings)
        return settings

    async def poll_changes(self) -> bool:
        """Reload if another process changed settings; True if a reload happened."""
        async with read_session(self._session_factory) as session:
            version = await kv.get(session, VERSION_KEY)
        if version == self._version:
            return False
        await self.load()
        return True

    # --- writing ---------------------------------------------------------------------------------
    async def set(
        self,
        key: str,
        value: Any,
        actor_id: int,
        source: str = "bot",
        ip: str | None = None,
    ) -> Settings:
        """Validate and store a new value for ``key``; returns the new settings.

        Raises KeyError for unknown/environment-only keys and :class:`SettingValidationError` for bad values.
        """
        spec = get_spec(key)
        if isinstance(value, str) and spec.type not in ("str", "secret"):
            value = value.strip()
        if value == "" and spec.nullable:
            value = None
        async with self._lock:
            candidate = self._validate({**self._overrides, key: value}, key)
            normalized = getattr(candidate, key)
            _check_bounds(spec, normalized)
            plain = self._plain(spec, normalized)
            previous = self._plain(spec, getattr(self._current, key))
            stored = self._encode(spec, plain)
            new_version = uuid.uuid4().hex
            async with self._session_factory.begin() as session:
                await kv.set(session, KV_PREFIX + key, stored)
                await kv.set(session, VERSION_KEY, new_version)
                details: dict[str, Any] = (
                    {"value": audit.REDACTED} if spec.secret else {"value": plain, "previous": previous}
                )
                await audit.log(session, actor_id, "settings.set", "setting", key, source=source, ip=ip, **details)
            self._overrides[key] = plain
            self._version = new_version
            changed = self._swap(candidate)
        log.info("setting %s changed by %s", key, actor_id)
        if changed:
            await self._notify(candidate)
        return candidate

    async def reset(self, key: str, actor_id: int, source: str = "bot", ip: str | None = None) -> Settings:
        """Drop the stored override of ``key`` (back to the environment/default value)."""
        get_spec(key)
        async with self._lock:
            remaining = {k: v for k, v in self._overrides.items() if k != key}
            candidate = self._validate(remaining, key)
            new_version = uuid.uuid4().hex
            async with self._session_factory.begin() as session:
                existed = await kv.delete(session, KV_PREFIX + key)
                await kv.set(session, VERSION_KEY, new_version)
                await audit.log(session, actor_id, "settings.reset", "setting", key, source=source, ip=ip)
            if not existed:
                log.debug("setting %s had no stored override", key)
            self._overrides = remaining
            self._version = new_version
            changed = self._swap(candidate)
        if changed:
            await self._notify(candidate)
        return candidate

    def set_ephemeral(self, key: str, value: Any) -> Settings:
        """Override any field in memory only (e.g. a discovered development tunnel URL). Not persisted."""
        if key not in Settings.model_fields:
            raise KeyError(f"unknown setting {key!r}")
        candidate = self._validate(self._overrides, key, ephemeral={**self._ephemeral, key: value})
        self._ephemeral[key] = value
        self._current = candidate
        return candidate

    # --- subscriptions ---------------------------------------------------------------------------
    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Call ``callback(new_settings)`` after every change; returns an unsubscribe function."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    async def _notify(self, settings: Settings) -> None:
        for callback in list(self._subscribers):
            try:
                await callback(settings)
            except Exception:
                log.exception("settings subscriber %r failed", callback)

    # --- internals -------------------------------------------------------------------------------
    def _swap(self, settings: Settings) -> bool:
        changed = settings.model_dump() != self._current.model_dump()
        self._current = settings
        return changed

    def _merge(self, overrides: dict[str, Any], ephemeral: dict[str, Any] | None = None) -> dict[str, Any]:
        base = self._env.model_dump()
        base.update(self._ephemeral if ephemeral is None else ephemeral)
        base.update(overrides)
        return base

    def _validate(self, overrides: dict[str, Any], key: str, ephemeral: dict[str, Any] | None = None) -> Settings:
        try:
            return Settings.model_validate(self._merge(overrides, ephemeral))
        except ValidationError as exc:
            raise SettingValidationError(key, _validation_message(exc)) from exc

    def _build_tolerant(self, overrides: dict[str, Any]) -> tuple[Settings, dict[str, Any]]:
        """Settings with as many stored overrides as possible; inconsistent ones are dropped (and logged)."""
        try:
            return Settings.model_validate(self._merge(overrides)), dict(overrides)
        except ValidationError:
            pass
        applied: dict[str, Any] = {}
        for key, value in overrides.items():
            try:
                Settings.model_validate(self._merge({**applied, key: value}))
            except ValidationError as exc:
                log.error("ignoring stored setting %r: %s", key, _validation_message(exc))
                continue
            applied[key] = value
        return Settings.model_validate(self._merge(applied)), applied

    @staticmethod
    def _plain(spec: SettingSpec, value: Any) -> Any:
        """JSON-compatible representation of a validated value (secrets as plain strings)."""
        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        annotation = Settings.model_fields[spec.key].annotation
        return TypeAdapter(annotation).dump_python(value, mode="json")

    def _encode(self, spec: SettingSpec, plain: Any) -> str:
        if spec.secret and plain is not None:
            plain = self._box.encrypt(str(plain))
        return json.dumps(plain, ensure_ascii=False)

    def _decode(self, spec: SettingSpec, raw: str) -> Any:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not JSON: {exc}") from exc
        if spec.secret and isinstance(value, str):
            value = self._box.decrypt(value)
        return value


def _check_bounds(spec: SettingSpec, value: Any) -> None:
    if value is None:
        return
    if spec.type in ("int", "float") and not isinstance(value, bool):
        if spec.min is not None and value < spec.min:
            raise SettingValidationError(spec.key, f"must be at least {spec.min:g}")
        if spec.max is not None and value > spec.max:
            raise SettingValidationError(spec.key, f"must be at most {spec.max:g}")
    elif spec.type == "str" and spec.max is not None and len(value) > spec.max:
        raise SettingValidationError(spec.key, f"must be at most {spec.max:g} characters")
    elif spec.type == "list[int]":
        for item in value:
            if spec.min is not None and item < spec.min:
                raise SettingValidationError(spec.key, f"items must be at least {spec.min:g}")
            if spec.max is not None and item > spec.max:
                raise SettingValidationError(spec.key, f"items must be at most {spec.max:g}")
