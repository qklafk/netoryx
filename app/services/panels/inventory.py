"""Panel inventory: first-start bootstrap from env, inbound refresh and health checks.

All functions take a ``session_factory`` and keep DB transactions short: panel/network I/O always happens
with no transaction open.

Inbound refresh semantics
-------------------------
* VLESS inbounds on a supported transport (tcp/ws/grpc/httpupgrade/xhttp) and Shadowsocks inbounds are imported;
* a newly discovered inbound is created **inactive** (an admin decides whether users get it), unless the
  caller asks to activate it (env bootstrap);
* known inbounds get fresh link params; ``flow`` is cleared when Vision is no longer allowed;
* inbounds that disappeared from the panel, or were switched to an unsupported protocol/transport, are
  marked inactive. An empty inbound list is treated as suspicious and deactivates nothing (protects against
  a panel answering with a transient empty list).

Telegram proxy (``mtproto``) inbounds of 3x-ui v3.5+ are imported too, but never join the VPN rotation: their
rows always stay ``is_active=False`` (so VPN provisioning, links and endpoint probes never touch them). Whether
the Telegram proxy uses one is the ``tg_proxy`` flag in its params (see :func:`tg_proxy_enabled`); it survives
refreshes, is set for new inbounds only when ``activate_new`` is requested, and is cleared when the inbound
disappears from the panel. Their cached params hold ``fake_tls_domain``, ``local`` (served by this panel, not a
node), ``panel_enabled``, ``port`` and ``protocol``.

``shadowsocks`` inbounds with a multi-user 2022 method serve subscriptions like VLESS ones, but are never
activated automatically (``activate_new`` skips them: the operator decides). Other methods (deprecated ciphers, the
single-user chacha20 variant) and unsupported transports are imported inactive; the reason is in the cached params
(``unsupported``) and an admin cannot activate them.

``mixed`` (SOCKS5) inbounds are imported the same way for the personal SOCKS5 proxy: always ``is_active=False``,
selected only explicitly through the ``tg_socks`` flag (never by ``activate_new``: an existing mixed inbound may
serve something else), see :func:`tg_socks_enabled`.

``Inbound.params`` stores :attr:`InboundInfo.params` plus ``port`` and ``protocol``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy import func, select

from app.db.models import (
    Endpoint,
    EndpointKind,
    EndpointProfile,
    Inbound,
    Location,
    Panel,
    PanelKind,
    utcnow,
)
from app.services.panels import shadowsocks
from app.services.panels.base import InboundInfo, PanelBackend, PanelError
from app.services.panels.socks import SOCKS_PROTOCOLS
from app.services.panels.xui import MTPROTO, SUPPORTED_NETWORKS, VISION_FLOW

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import Settings
    from app.db.session import SessionFactory
    from app.utils.crypto import SecretBox

logger = logging.getLogger(__name__)


class PanelSource(Protocol):
    """What inventory needs from :class:`app.services.panels.registry.PanelRegistry` (fakes in tests)."""

    async def get(self, panel_id: int) -> PanelBackend: ...


TG_PROXY_FLAG: Final = "tg_proxy"  # Inbound.params key: the mtproto inbound serves the Telegram proxy
TG_SOCKS_FLAG: Final = "tg_socks"  # Inbound.params key: the mixed inbound serves the Telegram SOCKS5 proxy
TCP_PROBE_TIMEOUT_SEC: Final = 3.0
MAX_PARALLEL_PROBES: Final = 32
_MAX_ERROR_LEN: Final = 500


# ---------------------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HealthChange:
    """A health transition worth an admin alert."""

    kind: Literal["panel", "endpoint", "probes"]  # probes: no endpoint probe succeeded (id 0)
    id: int
    name: str
    ok: bool  # True = recovered, False = went down
    error: str | None = None
    panel_id: int | None = None


@dataclass(slots=True)
class RefreshResult:
    created: list[int] = field(default_factory=list)  # Inbound.id
    updated: list[int] = field(default_factory=list)
    deactivated: list[int] = field(default_factory=list)
    disabled_on_panel: list[int] = field(default_factory=list)  # still imported, but disabled in 3x-ui
    skipped_remote_ids: list[int] = field(default_factory=list)  # not VLESS / unsupported transport
    mtproto: list[int] = field(default_factory=list)  # Inbound.id of the Telegram proxy inbounds seen
    socks: list[int] = field(default_factory=list)  # Inbound.id of the mixed (SOCKS5) inbounds seen
    shadowsocks: list[int] = field(default_factory=list)  # Inbound.id of the Shadowsocks inbounds seen


@dataclass(slots=True)
class BootstrapResult:
    panel_ids: list[int] = field(default_factory=list)
    inbounds_imported: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def _short_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:_MAX_ERROR_LEN]


def _is_mtproto(info: InboundInfo) -> bool:
    return info.protocol == MTPROTO


def _is_socks(info: InboundInfo) -> bool:
    return info.protocol in SOCKS_PROTOCOLS


def _is_shadowsocks(info: InboundInfo) -> bool:
    return info.protocol == shadowsocks.SHADOWSOCKS


def _is_blocked(info: InboundInfo) -> bool:
    """A Shadowsocks inbound that cannot serve subscriptions (it is imported, but stays inactive)."""
    return _is_shadowsocks(info) and bool(info.params.get(shadowsocks.PARAM_UNSUPPORTED))


def _is_importable(info: InboundInfo) -> bool:
    if _is_mtproto(info) or _is_socks(info) or _is_shadowsocks(info):
        return True
    return info.protocol == "vless" and info.params.get("type") in SUPPORTED_NETWORKS


def _params_json(info: InboundInfo, *, tg_proxy: bool = False, tg_socks: bool = False) -> str:
    params = {**info.params, "port": info.port, "protocol": info.protocol}
    if _is_mtproto(info):
        params["panel_enabled"] = info.enabled
        params[TG_PROXY_FLAG] = tg_proxy
    elif _is_socks(info):
        params["panel_enabled"] = info.enabled
        params[TG_SOCKS_FLAG] = tg_socks
    return json.dumps(params, sort_keys=True)


def _stored_params(inbound: Inbound) -> dict[str, Any]:
    try:
        data = json.loads(inbound.params or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def is_mtproto_inbound(inbound: Inbound) -> bool:
    """The row is a 3x-ui Telegram proxy (``mtproto``) inbound."""
    return _stored_params(inbound).get("protocol") == MTPROTO


def tg_proxy_enabled(inbound: Inbound) -> bool:
    """The row is an mtproto inbound selected for the Telegram proxy, served locally and enabled on the panel."""
    params = _stored_params(inbound)
    return (
        params.get("protocol") == MTPROTO
        and params.get(TG_PROXY_FLAG) is True
        and params.get("local", True) is not False
        and params.get("panel_enabled", True) is not False
    )


def set_tg_proxy_flag(inbound: Inbound, enabled: bool) -> bool:
    """Select/deselect an mtproto inbound for the Telegram proxy (in the caller's transaction).

    Returns False (nothing changed) if the row is not an mtproto inbound.
    """
    params = _stored_params(inbound)
    if params.get("protocol") != MTPROTO:
        return False
    if params.get(TG_PROXY_FLAG) is not enabled:
        params[TG_PROXY_FLAG] = enabled
        inbound.params = json.dumps(params, sort_keys=True)
    return True


def is_socks_inbound(inbound: Inbound) -> bool:
    """The row is a 3x-ui ``mixed`` (SOCKS5) inbound."""
    return _stored_params(inbound).get("protocol") in SOCKS_PROTOCOLS


def is_shadowsocks_inbound(inbound: Inbound) -> bool:
    """The row is a 3x-ui Shadowsocks inbound."""
    return shadowsocks.is_shadowsocks(_stored_params(inbound))


def shadowsocks_block_reason(inbound: Inbound) -> str | None:
    """Why a Shadowsocks inbound cannot be activated (None: usable, or not a Shadowsocks inbound)."""
    return shadowsocks.blocked_reason(_stored_params(inbound))


def is_proxy_inbound(inbound: Inbound) -> bool:
    """The row is a Telegram proxy inbound (MTProto or SOCKS5): never part of the VPN rotation."""
    return _stored_params(inbound).get("protocol") in {MTPROTO, *SOCKS_PROTOCOLS}


def tg_socks_enabled(inbound: Inbound) -> bool:
    """The row is a mixed inbound selected for the SOCKS5 proxy, served locally and enabled on the panel."""
    params = _stored_params(inbound)
    return (
        params.get("protocol") in SOCKS_PROTOCOLS
        and params.get(TG_SOCKS_FLAG) is True
        and params.get("local", True) is not False
        and params.get("panel_enabled", True) is not False
    )


def set_tg_socks_flag(inbound: Inbound, enabled: bool) -> bool:
    """Select/deselect a mixed inbound for the SOCKS5 proxy (caller's transaction); False if it is not one."""
    params = _stored_params(inbound)
    if params.get("protocol") not in SOCKS_PROTOCOLS:
        return False
    if params.get(TG_SOCKS_FLAG) is not enabled:
        params[TG_SOCKS_FLAG] = enabled
        inbound.params = json.dumps(params, sort_keys=True)
    return True


def _default_flow(info: InboundInfo) -> str:
    params = info.params
    if params.get("flow_allowed") and params.get("type") == "tcp" and params.get("security") == "reality":
        return VISION_FLOW
    return ""


def _host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def inbound_port(inbound: Inbound) -> int | None:
    """The inbound's listening port from its cached params (None if unknown)."""
    try:
        port = json.loads(inbound.params or "{}").get("port")
    except (ValueError, AttributeError):
        return None
    return port if isinstance(port, int) and port > 0 else None


# ---------------------------------------------------------------------------------------------
# Inbound refresh
# ---------------------------------------------------------------------------------------------
async def refresh_inbounds(
    session_factory: SessionFactory,
    registry: PanelSource,
    panel_id: int,
    *,
    activate_new: bool = False,
    only_remote_ids: Collection[int] | None = None,
    location_id: int | None = None,
    endpoint_address: str | None = None,
) -> RefreshResult:
    """Sync ``Inbound`` rows of a 3x-ui panel with the panel's inbound list.

    ``only_remote_ids`` limits which inbounds are imported/updated (missing detection still covers all).
    ``activate_new``/``location_id``/``endpoint_address`` apply to newly created rows; with
    ``endpoint_address`` each new inbound also gets a DIRECT/STANDARD :class:`Endpoint`.
    Raises :class:`PanelError` if the panel cannot be queried (nothing is changed then).
    """
    backend = await registry.get(panel_id)
    infos = await backend.list_inbounds()  # network I/O before any transaction
    now = utcnow()
    result = RefreshResult()
    wanted = set(only_remote_ids) if only_remote_ids is not None else None
    async with session_factory.begin() as session:
        existing = await session.scalars(select(Inbound).where(Inbound.panel_id == panel_id))
        rows = {row.remote_id: row for row in existing}
        for info in infos:
            if wanted is not None and info.remote_id not in wanted:
                continue
            row = rows.get(info.remote_id)
            if not _is_importable(info):
                result.skipped_remote_ids.append(info.remote_id)
                # The inbound was edited on the panel into something we can't build links for: its cached
                # params are stale, so take it out of rotation instead of serving broken links.
                if row is not None and row.is_active:
                    row.is_active = False
                    result.deactivated.append(row.id)
                continue
            if row is None:
                row = _new_inbound(panel_id, info, now, active=activate_new, location_id=location_id)
                session.add(row)
                await session.flush()
                result.created.append(row.id)
                if endpoint_address and not (_is_mtproto(info) or _is_socks(info)):
                    session.add(_direct_endpoint(row, endpoint_address, location_id))
            else:
                was_active = row.is_active
                if _apply_info(row, info, now):
                    result.updated.append(row.id)
                if was_active and not row.is_active:
                    result.deactivated.append(row.id)
            if _is_mtproto(info):
                result.mtproto.append(row.id)
            elif _is_socks(info):
                result.socks.append(row.id)
            elif _is_shadowsocks(info):
                result.shadowsocks.append(row.id)
            if not info.enabled:
                result.disabled_on_panel.append(row.id)
        result.deactivated += _deactivate_missing(rows, {i.remote_id for i in infos}, panel_id)
    _log_refresh(panel_id, result)
    return result


def _new_inbound(panel_id: int, info: InboundInfo, now: datetime, *, active: bool, location_id: int | None) -> Inbound:
    mtproto = _is_mtproto(info)
    proxy = mtproto or _is_socks(info)
    return Inbound(
        panel_id=panel_id,
        remote_id=info.remote_id,
        location_id=location_id,
        name=(info.remark or f"inbound {info.remote_id}")[:64],
        params=_params_json(info, tg_proxy=active and mtproto),
        params_updated_at=now,
        flow="" if proxy else _default_flow(info),
        # Proxy inbounds never join the VPN rotation; Shadowsocks ones only when an operator turns them on.
        is_active=active and not proxy and not _is_shadowsocks(info),
        sort_order=info.remote_id,
    )


def _apply_info(row: Inbound, info: InboundInfo, now: datetime) -> bool:
    """Refresh cached params of an existing row; returns True if anything changed."""
    changed = False
    mtproto = _is_mtproto(info)
    proxy = mtproto or _is_socks(info)
    stored = _stored_params(row)
    params = _params_json(
        info,
        tg_proxy=mtproto and stored.get(TG_PROXY_FLAG) is True,
        tg_socks=_is_socks(info) and stored.get(TG_SOCKS_FLAG) is True,
    )
    if row.params != params:
        row.params = params
        changed = True
    if proxy and row.is_active:
        # Switched to a proxy protocol on the panel: VPN clients must never be pushed into it.
        row.is_active = False
        changed = True
    if row.is_active and _is_blocked(info):
        # Switched to a deprecated method or an unsupported transport: stop serving it.
        row.is_active = False
        changed = True
    if row.flow and (proxy or not info.params.get("flow_allowed")):
        row.flow = ""
        changed = True
    if changed:
        row.params_updated_at = now
    return changed


def _deactivate_missing(rows: dict[int, Inbound], present: set[int], panel_id: int) -> list[int]:
    if not present:
        if any(row.is_active for row in rows.values()):
            logger.warning("Panel %s returned no inbounds; not deactivating anything", panel_id)
        return []
    deactivated = []
    for remote_id, row in rows.items():
        if remote_id in present:
            continue
        if row.is_active:
            row.is_active = False
            deactivated.append(row.id)
        elif _stored_params(row).get(TG_PROXY_FLAG) is True:
            set_tg_proxy_flag(row, False)  # the Telegram proxy drains accounts of a vanished mtproto inbound
            deactivated.append(row.id)
        elif _stored_params(row).get(TG_SOCKS_FLAG) is True:
            set_tg_socks_flag(row, False)  # same for a vanished SOCKS5 inbound
            deactivated.append(row.id)
    return deactivated


def _direct_endpoint(inbound: Inbound, address: str, location_id: int | None) -> Endpoint:
    return Endpoint(
        inbound_id=inbound.id,
        location_id=location_id,
        kind=EndpointKind.DIRECT,
        profile=EndpointProfile.STANDARD,
        address=address,
        port=None,
        sort_order=0,
        is_active=True,
    )


def _log_refresh(panel_id: int, result: RefreshResult) -> None:
    if result.created or result.deactivated:
        logger.info(
            "Panel %s inbounds: created %s, updated %s, deactivated %s",
            panel_id,
            result.created,
            result.updated,
            result.deactivated,
        )


# ---------------------------------------------------------------------------------------------
# Bootstrap from env
# ---------------------------------------------------------------------------------------------
async def bootstrap_from_env(
    session_factory: SessionFactory,
    settings: Settings,
    secret_box: SecretBox,
    registry: PanelSource,
) -> BootstrapResult | None:
    """Create the first panels from ``XUI_*`` / ``REMNAWAVE_*`` settings when the DB has no panels.

    Returns None when nothing was done (panels already exist or nothing configured). Inbound import
    failures are reported in :attr:`BootstrapResult.errors`; the panel row stays, so the import can be
    retried later with :func:`refresh_inbounds`.
    """
    if not (settings.xui_host or settings.remnawave_url):
        return None
    async with session_factory.begin() as session:
        if await session.scalar(select(func.count()).select_from(Panel)):
            return None
        location = Location(name=settings.xui_location_name, flag=settings.xui_location_flag, sort_order=0)
        session.add(location)
        await session.flush()
        xui_panel = _add_xui_panel(session, settings, secret_box)
        rw_panel = _add_remnawave_panel(session, settings, secret_box, location.id)
        await session.flush()
        location_id = location.id
        panel_ids = [p.id for p in (xui_panel, rw_panel) if p is not None]

    result = BootstrapResult(panel_ids=panel_ids)
    if xui_panel is not None:
        await _import_xui_inbounds(session_factory, settings, registry, xui_panel.id, location_id, result)
    logger.info("Bootstrapped panels %s from environment (%d inbounds)", panel_ids, result.inbounds_imported)
    return result


def _add_xui_panel(session: AsyncSession, settings: Settings, secret_box: SecretBox) -> Panel | None:
    if not settings.xui_host:
        return None
    password = settings.xui_password.get_secret_value() if settings.xui_password else None
    token = settings.xui_api_token.get_secret_value() if settings.xui_api_token else None
    panel = Panel(
        name="3x-ui",
        kind=PanelKind.XUI,
        url=settings.xui_host,
        username=settings.xui_username,
        password=secret_box.encrypt(password) if password else None,
        api_token=secret_box.encrypt(token) if token else None,
        tls_verify=settings.xui_tls_verify,
        is_active=True,
    )
    session.add(panel)
    return panel


def _add_remnawave_panel(
    session: AsyncSession, settings: Settings, secret_box: SecretBox, location_id: int
) -> Panel | None:
    if not (settings.remnawave_url and settings.remnawave_api_token):
        return None
    options = {
        "squad_uuids": list(settings.remnawave_squad_uuids),
        "direct": settings.remnawave_direct,
        "location_id": location_id,
    }
    panel = Panel(
        name="Remnawave",
        kind=PanelKind.REMNAWAVE,
        url=settings.remnawave_url,
        api_token=secret_box.encrypt(settings.remnawave_api_token.get_secret_value()),
        tls_verify=True,
        options=json.dumps(options),
        is_active=True,
    )
    session.add(panel)
    return panel


async def _import_xui_inbounds(
    session_factory: SessionFactory,
    settings: Settings,
    registry: PanelSource,
    panel_id: int,
    location_id: int,
    result: BootstrapResult,
) -> None:
    address = settings.xui_public_host or _host_of(settings.xui_host or "")
    try:
        refreshed = await refresh_inbounds(
            session_factory,
            registry,
            panel_id,
            activate_new=True,
            only_remote_ids=settings.xui_inbound_ids or None,
            location_id=location_id,
            endpoint_address=address or None,
        )
    except (PanelError, LookupError) as exc:
        logger.warning("Could not import inbounds from 3x-ui panel %s: %s", panel_id, exc)
        result.errors.append(f"panel {panel_id}: {_short_error(exc)}")
        return
    proxy_inbounds = set(refreshed.mtproto) | set(refreshed.socks) | set(refreshed.shadowsocks)
    result.inbounds_imported = len([i for i in refreshed.created if i not in proxy_inbounds])
    if not result.inbounds_imported:
        result.errors.append(f"panel {panel_id}: no VLESS inbounds found to import")


# ---------------------------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _ProbeTarget:
    endpoint_id: int
    panel_id: int
    name: str
    address: str
    port: int | None


async def check_health(
    session_factory: SessionFactory, registry: PanelSource, settings: Settings
) -> list[HealthChange]:
    """Ping active panels and TCP-probe active endpoints; persist health and return transitions.

    A panel is reported down when its consecutive failures reach ``HEALTH_FAILURES_BEFORE_ALERT`` and
    recovered on the first success afterwards. An endpoint is auto-hidden at the same threshold and
    un-hidden on its first successful probe. A round in which no endpoint probe succeeds hides nothing (a
    probe-side outage must not empty every subscription): it raises one ``probes`` alert instead, and a
    ``probes`` recovery once probes succeed again.
    """
    threshold = max(1, settings.health_failures_before_alert)
    changes = await _check_panels(session_factory, registry, settings, threshold)
    changes += await _check_endpoints(session_factory, threshold)
    return changes


async def _check_panels(
    session_factory: SessionFactory, registry: PanelSource, settings: Settings, threshold: int
) -> list[HealthChange]:
    async with session_factory() as session:
        panel_ids = list(await session.scalars(select(Panel.id).where(Panel.is_active.is_(True))))
    timeout = max(1, settings.panel_request_timeout_sec) * 3  # a ping may need login + several requests
    outcomes = await asyncio.gather(*(_ping(registry, pid, timeout) for pid in panel_ids))
    now = utcnow()
    changes: list[HealthChange] = []
    async with session_factory.begin() as session:
        for panel_id, (version, error) in zip(panel_ids, outcomes, strict=True):
            panel = await session.get(Panel, panel_id)
            if panel is None:
                continue
            if change := _record_panel(panel, version, error, now, threshold):
                changes.append(change)
    return changes


async def _ping(registry: PanelSource, panel_id: int, timeout_sec: float) -> tuple[str | None, str | None]:
    """Returns (version, None) on success or (None, error)."""
    try:
        backend = await registry.get(panel_id)
        async with asyncio.timeout(timeout_sec):
            return await backend.ping(), None
    except TimeoutError:
        return None, f"no answer within {timeout_sec:.0f}s"
    except (PanelError, LookupError, OSError) as exc:
        return None, _short_error(exc)
    except Exception as exc:  # one misconfigured panel must not stop health checks of the others
        logger.exception("Health check of panel %s failed unexpectedly", panel_id)
        return None, _short_error(exc)


def _record_panel(
    panel: Panel, version: str | None, error: str | None, now: datetime, threshold: int
) -> HealthChange | None:
    previous = panel.consecutive_failures or 0
    if error is None:
        panel.version = (version or panel.version or "")[:32] or None
        panel.last_ok_at = now
        panel.last_error = None
        panel.consecutive_failures = 0
        if previous >= threshold:
            return HealthChange("panel", panel.id, panel.name, ok=True)
        return None
    panel.last_error = error
    panel.consecutive_failures = previous + 1
    if panel.consecutive_failures == threshold:
        return HealthChange("panel", panel.id, panel.name, ok=False, error=error)
    return None


async def _check_endpoints(session_factory: SessionFactory, threshold: int) -> list[HealthChange]:
    targets = await _load_probe_targets(session_factory)
    semaphore = asyncio.Semaphore(MAX_PARALLEL_PROBES)

    async def probe(target: _ProbeTarget) -> str | None:
        if target.port is None:
            return "unknown port (refresh the inbound)"
        async with semaphore:
            return await tcp_probe(target.address, target.port, TCP_PROBE_TIMEOUT_SEC)

    errors = await asyncio.gather(*(probe(t) for t in targets))
    # Probes run from the bot host. When none succeeds, the cause is most likely on the probing side (no
    # outbound connectivity, broken DNS, egress filtering) while users can still reach the servers; hiding
    # everything would hand every client an empty server list. Count the failures but hide nothing.
    probe_outage = bool(targets) and all(error is not None for error in errors)
    now = utcnow()
    changes: list[HealthChange] = []
    outage_reached = outage_ended = False
    async with session_factory.begin() as session:
        for target, error in zip(targets, errors, strict=True):
            endpoint = await session.get(Endpoint, target.endpoint_id)
            if endpoint is None:
                continue
            # Past the threshold yet visible: only a probe outage (or an admin un-hiding it) leaves it so.
            stalled = (endpoint.consecutive_failures or 0) >= threshold and not endpoint.auto_hidden
            if probe_outage:
                endpoint.last_error = error
                endpoint.consecutive_failures = (endpoint.consecutive_failures or 0) + 1
                outage_reached |= endpoint.consecutive_failures == threshold and not endpoint.auto_hidden
                continue
            outage_ended |= stalled
            if change := _record_endpoint(endpoint, target, error, now, threshold):
                changes.append(change)
    name = f"{len(targets)} endpoints"
    if outage_reached:
        error = (
            f"no endpoint is reachable from the bot host ({errors[0]}); endpoints are kept visible. "
            "Check the bot host's network and DNS, or whether every server is down"
        )
        changes.append(HealthChange("probes", 0, name, ok=False, error=error))
    elif outage_ended:
        changes.append(HealthChange("probes", 0, name, ok=True))
    return changes


async def _load_probe_targets(session_factory: SessionFactory) -> list[_ProbeTarget]:
    stmt = (
        select(Endpoint, Inbound)
        .join(Inbound, Endpoint.inbound_id == Inbound.id)
        .join(Panel, Inbound.panel_id == Panel.id)
        .where(Endpoint.is_active.is_(True), Inbound.is_active.is_(True), Panel.is_active.is_(True))
        .order_by(Endpoint.id)
    )
    async with session_factory() as session:
        rows = (await session.execute(stmt)).all()
    targets = []
    for endpoint, inbound in rows:
        port = endpoint.port or inbound_port(inbound)
        name = endpoint.remark or f"{inbound.name} · {endpoint.address}:{port or '?'}"
        targets.append(_ProbeTarget(endpoint.id, inbound.panel_id, name, endpoint.address, port))
    return targets


def _record_endpoint(
    endpoint: Endpoint, target: _ProbeTarget, error: str | None, now: datetime, threshold: int
) -> HealthChange | None:
    if error is None:
        endpoint.last_ok_at = now
        endpoint.last_error = None
        endpoint.consecutive_failures = 0
        if endpoint.auto_hidden:
            endpoint.auto_hidden = False
            return HealthChange("endpoint", endpoint.id, target.name, ok=True, panel_id=target.panel_id)
        return None
    endpoint.last_error = error
    endpoint.consecutive_failures = (endpoint.consecutive_failures or 0) + 1
    if endpoint.consecutive_failures >= threshold and not endpoint.auto_hidden:
        endpoint.auto_hidden = True
        return HealthChange("endpoint", endpoint.id, target.name, ok=False, error=error, panel_id=target.panel_id)
    return None


async def tcp_probe(host: str, port: int, timeout_sec: float = TCP_PROBE_TIMEOUT_SEC) -> str | None:
    """Try a TCP connect; returns None on success or a short error description."""
    try:
        async with asyncio.timeout(timeout_sec):
            _, writer = await asyncio.open_connection(host, port)
    except TimeoutError:
        return f"{host}:{port}: timeout after {timeout_sec:g}s"
    except OSError as exc:
        return f"{host}:{port}: {exc.strerror or type(exc).__name__}"
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return None
