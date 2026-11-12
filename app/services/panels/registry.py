"""Cache of live panel backends built from ``Panel`` rows.

``PanelRegistry.get(panel_id)`` returns a ready :class:`PanelBackend` (credentials decrypted). Backends are
cached per panel and rebuilt automatically when any connection-relevant column of the row changes
(URL, credentials, TLS verification, options, kind), so admin edits take effect without a restart.
Backends keep HTTP sessions open; call :meth:`PanelRegistry.close` on shutdown.

``Panel.options`` (JSON object) understood here:

* ``headers``  - extra HTTP headers for every request (e.g. a reverse-proxy ``X-Api-Key``);
* ``cookies``  - extra cookies (Remnawave behind a cookie-protected proxy);
* Remnawave: ``squad_uuids`` (list), ``username_prefix`` (default ``"tg_"``), ``direct`` (default true).
* Hiddify: the URL includes the configured administrator proxy path; ``api_token`` is its API key.
* PasarGuard: ``api_token`` is an API key with user and system read permissions; ``group_ids`` (list) are the
  groups new users join (they decide which inbounds a user gets).
* telemt: ``public_host`` / ``public_port`` (override the host/port of the links telemt reports); the API
  ``auth_header`` value is stored (encrypted) in ``api_token``. See :mod:`app.services.tg_proxy` for the
  proxy-specific options.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select

from app.db.models import Panel, PanelKind
from app.db.session import read_session
from app.services.panels.base import PanelAuthError, PanelBackend, PanelError
from app.services.panels.xui import XuiBackend

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory

logger = logging.getLogger(__name__)


class SecretDecryptor(Protocol):
    """The part of :class:`app.utils.crypto.SecretBox` the registry needs."""

    def decrypt(self, value: str) -> str: ...


def panel_options(panel: Panel) -> dict[str, Any]:
    """Parse ``Panel.options``; invalid or non-object JSON yields ``{}`` (logged)."""
    if not panel.options:
        return {}
    try:
        data = json.loads(panel.options)
    except ValueError:
        logger.warning("Panel %s: options is not valid JSON; ignoring", panel.id)
        return {}
    if not isinstance(data, dict):
        logger.warning("Panel %s: options must be a JSON object; ignoring", panel.id)
        return {}
    return data


def _str_map(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(k): str(v) for k, v in value.items()}


def _secret(value: str | None, box: SecretDecryptor) -> str | None:
    return box.decrypt(value) if value else None


def build_backend(panel: Panel, secret_box: SecretDecryptor, settings: Settings) -> PanelBackend:
    """Instantiate the backend for ``panel`` (no network I/O). Raises :class:`PanelError` on bad config."""
    options = panel_options(panel)
    timeout = settings.panel_request_timeout_sec
    headers = _str_map(options.get("headers"))
    if panel.kind == PanelKind.XUI:
        try:
            return XuiBackend(
                panel.url,
                username=panel.username,
                password=_secret(panel.password, secret_box),
                api_token=_secret(panel.api_token, secret_box),
                tls_verify=panel.tls_verify,
                timeout=timeout,
                extra_headers=headers,
            )
        except ValueError as exc:
            raise PanelAuthError(f"panel {panel.id} ({panel.name}): {exc}") from exc
    if panel.kind == PanelKind.REMNAWAVE:
        # Imported lazily: deployments without Remnawave never load it.
        from app.services.panels.remnawave import RemnawaveBackend

        squads = options.get("squad_uuids") or []
        try:
            token = _secret(panel.api_token, secret_box)  # DecryptionError is a ValueError
            if not token:
                raise ValueError("Remnawave needs an API token")
            return RemnawaveBackend(
                panel.url,
                token,
                squad_uuids=[str(s) for s in squads] if isinstance(squads, list) else [],
                username_prefix=str(options.get("username_prefix") or "tg_"),
                direct=bool(options.get("direct", True)),
                tls_verify=panel.tls_verify,
                timeout=timeout,
                extra_headers=headers,
                cookies=_str_map(options.get("cookies")),
                traffic_limit_strategy=str(options.get("traffic_limit_strategy") or "NO_RESET"),
            )
        except ValueError as exc:
            raise PanelAuthError(f"panel {panel.id} ({panel.name}): {exc}") from exc
    if panel.kind == PanelKind.TELEMT:
        from app.services.panels.telemt import TelemtBackend

        port = options.get("public_port")
        try:
            return TelemtBackend(
                panel.url,
                auth_header=_secret(panel.api_token, secret_box),  # the exact [server.api] auth_header value
                tls_verify=panel.tls_verify,
                timeout=timeout,
                extra_headers=headers,
                public_host=str(options.get("public_host") or "") or None,
                public_port=port if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536 else None,
            )
        except ValueError as exc:
            raise PanelAuthError(f"panel {panel.id} ({panel.name}): {exc}") from exc
    if panel.kind == PanelKind.MARZBAN:
        from app.services.panels.marzban import MarzbanBackend

        tags = options.get("inbound_tags") or []
        try:
            return MarzbanBackend(
                panel.url,
                username=panel.username,
                password=_secret(panel.password, secret_box),
                api_token=_secret(panel.api_token, secret_box),
                tls_verify=panel.tls_verify,
                timeout=timeout,
                extra_headers=headers,
                inbound_tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
            )
        except ValueError as exc:
            raise PanelAuthError(f"panel {panel.id} ({panel.name}): {exc}") from exc
    if panel.kind == PanelKind.HIDDIFY:
        from app.services.panels.hiddify import HiddifyBackend

        try:
            token = _secret(panel.api_token, secret_box)
            if not token:
                raise ValueError("Hiddify needs an API key")
            return HiddifyBackend(
                panel.url,
                token,
                tls_verify=panel.tls_verify,
                timeout=timeout,
                extra_headers=headers,
            )
        except ValueError as exc:
            raise PanelAuthError(f"panel {panel.id} ({panel.name}): {exc}") from exc
    if panel.kind == PanelKind.PASARGUARD:
        from app.services.panels.pasarguard import PasarGuardBackend

        groups = options.get("group_ids") or []
        try:
            token = _secret(panel.api_token, secret_box)
            if not token:
                raise ValueError("PasarGuard needs an API key")
            return PasarGuardBackend(
                panel.url,
                token,
                tls_verify=panel.tls_verify,
                timeout=timeout,
                extra_headers=headers,
                group_ids=[v for v in groups if isinstance(v, int) and not isinstance(v, bool)],
            )
        except ValueError as exc:
            raise PanelAuthError(f"panel {panel.id} ({panel.name}): {exc}") from exc
    if panel.kind == PanelKind.MARZNESHIN:
        from app.services.panels.marzneshin import MarzneshinBackend

        try:
            ids = options.get("service_ids") or []
            return MarzneshinBackend(
                panel.url,
                username=panel.username,
                password=_secret(panel.password, secret_box),
                tls_verify=panel.tls_verify,
                timeout=timeout,
                extra_headers=headers,
                service_ids=[value for value in ids if isinstance(value, int) and not isinstance(value, bool)],
            )
        except ValueError as exc:
            raise PanelAuthError(f"panel {panel.id} ({panel.name}): {exc}") from exc
    raise PanelError(f"panel {panel.id}: unsupported kind {panel.kind!r}")


def panel_fingerprint(panel: Panel) -> str:
    """Hash of every column that affects how the backend connects; changes -> rebuild."""
    material = json.dumps(
        [str(panel.kind), panel.url, panel.username, panel.password, panel.api_token, panel.tls_verify, panel.options],
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class _Entry:
    fingerprint: str
    backend: PanelBackend


class PanelRegistry:
    """Builds, caches and closes panel backends. Safe for concurrent use from many tasks."""

    def __init__(self, session_factory: SessionFactory, settings: Settings, secret_box: SecretDecryptor) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._secret_box = secret_box
        self._entries: dict[int, _Entry] = {}
        self._lock = asyncio.Lock()

    async def _load(self, panel_id: int) -> Panel:
        async with read_session(self._session_factory) as session:
            panel = await session.scalar(select(Panel).where(Panel.id == panel_id))
        if panel is None:
            raise LookupError(f"panel {panel_id} not found")
        return panel

    async def get(self, panel_id: int) -> PanelBackend:
        """Backend for the panel (cached; rebuilt if the row changed). Raises LookupError if the row is gone."""
        try:
            panel = await self._load(panel_id)
        except LookupError:
            await self.invalidate(panel_id)
            raise
        fingerprint = panel_fingerprint(panel)
        stale: PanelBackend | None = None
        async with self._lock:
            entry = self._entries.get(panel_id)
            if entry is not None and entry.fingerprint == fingerprint:
                return entry.backend
            backend = build_backend(panel, self._secret_box, self._settings)
            self._entries[panel_id] = _Entry(fingerprint, backend)
            stale = entry.backend if entry else None
        if stale is not None:
            logger.info("Panel %s changed; backend rebuilt", panel_id)
            await _close_quietly(stale)
        return backend

    async def invalidate(self, panel_id: int) -> None:
        """Drop (and close) the cached backend; the next :meth:`get` rebuilds it."""
        async with self._lock:
            entry = self._entries.pop(panel_id, None)
        if entry is not None:
            await _close_quietly(entry.backend)

    async def close(self) -> None:
        """Close every cached backend (application shutdown)."""
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        await asyncio.gather(*(_close_quietly(e.backend) for e in entries))


async def _close_quietly(backend: PanelBackend) -> None:
    try:
        await backend.close()
    except Exception:  # closing must never break the caller
        logger.exception("Error while closing panel backend %r", backend)
