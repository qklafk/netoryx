"""First-run setup of the servers that live next to the application (the one-line installer's bootstrap step).

:func:`bootstrap_local` brings the database to the state a fresh all-in-one install needs and is safe to repeat:

* a location;
* the local 3x-ui panel record (credentials are kept up to date) with its VLESS + Reality inbound that borrows the
  site ``vpn.<domain>`` (created on the panel only when no inbound listens on that port yet), imported, activated and
  given one endpoint on the public TLS port;
* optionally a password-protected ``mixed`` (SOCKS5) inbound selected for the Telegram SOCKS5 proxy, plus the panel
  options that tell users where to connect;
* optionally the local telemt panel record (Telegram MTProto proxy);
* alternatively to 3x-ui, one of the panels the installer can deploy (Remnawave, Marzban, PasarGuard, Marzneshin):
  see :mod:`app.services.panel_setup` for the entry point created on the panel, here the panel is registered with the
  credentials it produced.

Nothing is duplicated on a second run and nothing secret is returned: callers print :class:`LocalSetupResult` as is.
Panel I/O happens with no database transaction open.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select

from app.db.models import Endpoint, EndpointKind, EndpointProfile, Inbound, Location, Panel, PanelKind
from app.services import panel_setup
from app.services.panel_setup import PanelHttp, PanelInstall, PanelSetupError
from app.services.panels import inventory
from app.services.panels.base import InboundInfo, PanelBackend, PanelError
from app.services.panels.socks import SOCKS_PROTOCOLS

if TYPE_CHECKING:
    from app.db.session import SessionFactory
    from app.utils.crypto import SecretBox

log = logging.getLogger(__name__)

XUI_PANEL_NAME: Final = "3x-ui"
TELEMT_PANEL_NAME: Final = "Telegram proxy"
REALITY_REMARK: Final = "Reality"
SOCKS_REMARK: Final = "Telegram SOCKS5"
SHADOWSOCKS_REMARK: Final = "Shadowsocks"
PING_ATTEMPTS: Final = 5
PING_DELAY_SEC: Final = 3.0


class SetupError(Exception):
    """The setup cannot continue; the message is safe to show (no secrets)."""


@dataclass(frozen=True, slots=True)
class XuiSetup:
    url: str  # panel URL incl. the web base path
    username: str
    password: str
    reality_port: int  # port of the Reality inbound on the host (the public 443 is routed to it by the edge)
    reality_sni: str  # server name clients use, e.g. vpn.<domain>
    reality_dest: str  # where Reality forwards unknown visitors, e.g. 127.0.0.1:8444
    endpoint_address: str  # public address of this server written into links
    endpoint_port: int = 443
    socks_port: int | None = None
    socks_host: str | None = None  # public address of the SOCKS5 proxy (defaults to the endpoint address)
    ss_port: int | None = None  # optional Shadowsocks-2022 inbound (not TLS: it needs a port of its own)
    tls_verify: bool = False


@dataclass(frozen=True, slots=True)
class TelemtSetup:
    url: str
    api_token: str  # exact value of the API ``auth_header``
    public_host: str
    public_port: int = 443


@dataclass(frozen=True, slots=True)
class LocalSetup:
    location_name: str = "Main"
    location_flag: str = "🌍"
    xui: XuiSetup | None = None
    telemt: TelemtSetup | None = None
    panel: PanelInstall | None = None


@dataclass(slots=True)
class LocalSetupResult:
    """What was done; contains ids, ports and statuses only."""

    location_id: int | None = None
    xui: dict[str, Any] = field(default_factory=dict)
    telemt: dict[str, Any] = field(default_factory=dict)
    panel: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "xui": self.xui,
            "panel": self.panel,
            "telemt": self.telemt,
            "warnings": self.warnings,
        }


def _options(panel: Panel) -> dict[str, Any]:
    try:
        data = json.loads(panel.options) if panel.options else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _ensure_location(session_factory: SessionFactory, name: str, flag: str) -> int:
    async with session_factory.begin() as session:
        location = await session.scalar(select(Location).where(Location.name == name).order_by(Location.id))
        if location is None:
            location = Location(name=name, flag=flag, sort_order=0)
            session.add(location)
            await session.flush()
        return location.id


async def _ensure_panel(
    session_factory: SessionFactory,
    secret_box: SecretBox,
    *,
    kind: PanelKind,
    name: str,
    url: str,
    username: str | None = None,
    password: str | None = None,
    api_token: str | None = None,
    tls_verify: bool,
    options: dict[str, Any],
) -> tuple[int, str]:
    """Create the panel row or bring its connection data and the given options up to date."""
    async with session_factory.begin() as session:
        panel = await session.scalar(select(Panel).where(Panel.kind == kind, Panel.name == name).order_by(Panel.id))
        if panel is None:
            panel = Panel(
                name=name,
                kind=kind,
                url=url,
                username=username,
                password=secret_box.encrypt(password) if password else None,
                api_token=secret_box.encrypt(api_token) if api_token else None,
                tls_verify=tls_verify,
                options=json.dumps(options, sort_keys=True) if options else None,
                is_active=True,
            )
            session.add(panel)
            await session.flush()
            return panel.id, "created"
        changed = False
        if panel.url != url:
            panel.url, changed = url, True
        if (panel.username or None) != username:
            panel.username, changed = username, True
        if panel.tls_verify != tls_verify:
            panel.tls_verify, changed = tls_verify, True
        for column, plain in (("password", password), ("api_token", api_token)):
            stored = getattr(panel, column)
            if plain and not _same_secret(secret_box, stored, plain):
                setattr(panel, column, secret_box.encrypt(plain))
                changed = True
        merged = {**_options(panel), **options}
        if merged != _options(panel):
            panel.options = json.dumps(merged, sort_keys=True)
            changed = True
        return panel.id, "updated" if changed else "unchanged"


def _same_secret(secret_box: SecretBox, stored: str | None, plain: str) -> bool:
    if not stored:
        return False
    try:
        return secret_box.decrypt(stored) == plain
    except Exception:  # a value that cannot be decrypted is simply replaced
        return False


async def _ping(backend: PanelBackend, what: str, sleep: Callable[[float], Awaitable[Any]]) -> str:
    last: PanelError | None = None
    for attempt in range(PING_ATTEMPTS):
        try:
            return await backend.ping()
        except PanelError as exc:
            last = exc
            if attempt + 1 < PING_ATTEMPTS:
                await sleep(PING_DELAY_SEC)
    raise SetupError(f"cannot connect to {what}: {str(last)[:300]}")


def _is_reality(info: InboundInfo) -> bool:
    return info.protocol == "vless" and info.params.get("security") == "reality"


async def _ensure_remote_inbound(
    infos: list[InboundInfo],
    port: int,
    kind: str,
    create: Callable[[], Awaitable[InboundInfo]] | None,
) -> tuple[InboundInfo, bool]:
    """The inbound of ``kind`` ("reality" | "socks") on ``port``: the existing one, or a newly created one."""
    existing = next((i for i in infos if i.port == port), None)
    if existing is not None:
        matches = _is_reality(existing) if kind == "reality" else existing.protocol in SOCKS_PROTOCOLS
        if not matches:
            raise SetupError(f"port {port} on the panel is already used by another inbound ({existing.protocol})")
        return existing, False
    if create is None:
        raise SetupError("this panel cannot create inbounds")
    return await create(), True


async def bootstrap_local(
    session_factory: SessionFactory,
    registry: inventory.PanelSource,
    secret_box: SecretBox,
    setup: LocalSetup,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    http_factory: Callable[[PanelInstall], PanelHttp] | None = None,
) -> LocalSetupResult:
    """Apply ``setup`` (see the module docstring). Raises :class:`SetupError` when it cannot be completed."""
    result = LocalSetupResult()
    result.location_id = await _ensure_location(session_factory, setup.location_name, setup.location_flag)
    if setup.xui is not None:
        result.xui = await _setup_xui(session_factory, registry, secret_box, setup, setup.xui, result, sleep)
    if setup.panel is not None:
        result.panel = await _setup_panel(
            session_factory, registry, secret_box, setup.panel, result, sleep, http_factory or _default_http
        )
    if setup.telemt is not None:
        result.telemt = await _setup_telemt(session_factory, registry, secret_box, setup, setup.telemt, result, sleep)
    return result


async def _setup_xui(
    session_factory: SessionFactory,
    registry: inventory.PanelSource,
    secret_box: SecretBox,
    setup: LocalSetup,
    xui: XuiSetup,
    result: LocalSetupResult,
    sleep: Callable[[float], Awaitable[Any]],
) -> dict[str, Any]:
    location_id = result.location_id
    socks_host = xui.socks_host or xui.endpoint_address
    options: dict[str, Any] = {}
    if xui.socks_port is not None:
        options = {"tg_socks_host": socks_host, "tg_socks_port": xui.socks_port}
    panel_id, status = await _ensure_panel(
        session_factory,
        secret_box,
        kind=PanelKind.XUI,
        name=XUI_PANEL_NAME,
        url=xui.url,
        username=xui.username,
        password=xui.password,
        tls_verify=xui.tls_verify,
        options=options,
    )
    out: dict[str, Any] = {"panel_id": panel_id, "panel": status}
    try:
        backend = await registry.get(panel_id)
    except (PanelError, LookupError) as exc:
        raise SetupError(f"cannot use the 3x-ui panel: {str(exc)[:300]}") from None
    out["version"] = await _ping(backend, "the 3x-ui panel", sleep)
    async with session_factory.begin() as session:
        panel = await session.get(Panel, panel_id)
        if panel is not None:
            panel.version = out["version"]

    try:
        infos = await backend.list_inbounds()
        make_reality = getattr(backend, "create_reality_inbound", None)
        make_mixed = getattr(backend, "create_mixed_inbound", None)
        reality, reality_new = await _ensure_remote_inbound(
            infos,
            xui.reality_port,
            "reality",
            (
                (lambda: make_reality(xui.reality_port, xui.reality_sni, dest=xui.reality_dest, remark=REALITY_REMARK))
                if callable(make_reality)
                else None
            ),
        )
        socks: InboundInfo | None = None
        socks_new = False
        if xui.socks_port is not None:
            socks_port = xui.socks_port
            socks, socks_new = await _ensure_remote_inbound(
                infos,
                socks_port,
                "socks",
                (lambda: make_mixed(socks_port, remark=SOCKS_REMARK)) if callable(make_mixed) else None,
            )
    except PanelError as exc:
        raise SetupError(f"3x-ui refused the request: {str(exc)[:300]}") from None

    shadowsocks: InboundInfo | None = None
    ss_new = False
    if xui.ss_port is not None:
        make_ss = getattr(backend, "create_shadowsocks_inbound", None)
        ss_port = xui.ss_port
        existing_ss = next((i for i in infos if i.port == ss_port), None)
        try:
            if existing_ss is not None and existing_ss.protocol != "shadowsocks":
                raise SetupError(f"port {ss_port} on the panel is already used by another inbound")
            if existing_ss is not None:
                shadowsocks = existing_ss
            elif callable(make_ss):
                shadowsocks, ss_new = await make_ss(ss_port, remark=SHADOWSOCKS_REMARK), True
            else:
                out["shadowsocks"] = {"skipped": "not supported by this version"}
                result.warnings.append("Shadowsocks skipped: not supported by this version")
        except PanelError as exc:
            raise SetupError(f"3x-ui refused the request: {str(exc)[:300]}") from None

    wanted = {reality.remote_id} | ({socks.remote_id} if socks is not None else set())
    if shadowsocks is not None:
        wanted.add(shadowsocks.remote_id)
    try:
        await inventory.refresh_inbounds(
            session_factory,
            registry,
            panel_id,
            activate_new=True,
            only_remote_ids=wanted,
            location_id=location_id,
        )
    except PanelError as exc:
        raise SetupError(f"could not import the inbounds: {str(exc)[:300]}") from None

    async with session_factory.begin() as session:
        rows = {
            row.remote_id: row
            for row in await session.scalars(
                select(Inbound).where(Inbound.panel_id == panel_id, Inbound.remote_id.in_(wanted))
            )
        }
        row = rows.get(reality.remote_id)
        if row is None:
            raise SetupError("the Reality inbound was not imported")
        if row.location_id is None:
            row.location_id = location_id
        endpoints = list(await session.scalars(select(Endpoint).where(Endpoint.inbound_id == row.id)))
        endpoint_status = "unchanged"
        if not endpoints:
            session.add(
                Endpoint(
                    inbound_id=row.id,
                    location_id=location_id,
                    kind=EndpointKind.DIRECT,
                    profile=EndpointProfile.STANDARD,
                    address=xui.endpoint_address,
                    port=xui.endpoint_port,
                    sort_order=0,
                    is_active=True,
                )
            )
            endpoint_status = "created"
        out["reality"] = {
            "inbound_id": row.id,
            "remote_id": reality.remote_id,
            "port": xui.reality_port,
            "sni": xui.reality_sni,
            "inbound": "created" if reality_new else "existing",
            "endpoint": endpoint_status,
            "active": row.is_active,
        }
        if reality.params.get("sni") not in (None, "", xui.reality_sni):
            result.warnings.append(
                f"the Reality inbound on port {xui.reality_port} uses the server name {reality.params.get('sni')}, "
                f"not {xui.reality_sni}"
            )
        if shadowsocks is not None:
            ss_row = rows.get(shadowsocks.remote_id)
            if ss_row is None:
                out["shadowsocks"] = {"skipped": "the panel inbound could not be imported by this version"}
                result.warnings.append("Shadowsocks skipped: the inbound could not be imported by this version")
            else:
                if ss_row.location_id is None:
                    ss_row.location_id = location_id
                if ss_new and not shadowsocks.params.get("unsupported"):
                    ss_row.is_active = (
                        True  # the operator asked for it: imports never activate Shadowsocks by themselves
                    )
                ss_endpoint = "unchanged"
                if not await session.scalar(select(Endpoint.id).where(Endpoint.inbound_id == ss_row.id).limit(1)):
                    session.add(
                        Endpoint(
                            inbound_id=ss_row.id,
                            location_id=location_id,
                            kind=EndpointKind.DIRECT,
                            profile=EndpointProfile.STANDARD,
                            address=xui.endpoint_address,
                            port=None,
                            sort_order=0,
                            is_active=True,
                        )
                    )
                    ss_endpoint = "created"
                out["shadowsocks"] = {
                    "inbound_id": ss_row.id,
                    "remote_id": shadowsocks.remote_id,
                    "port": xui.ss_port,
                    "inbound": "created" if ss_new else "existing",
                    "endpoint": ss_endpoint,
                    "active": ss_row.is_active,
                }
        if socks is not None:
            socks_row = rows.get(socks.remote_id)
            if socks_row is None:
                raise SetupError("the SOCKS5 inbound was not imported")
            inventory.set_tg_socks_flag(socks_row, True)
            if socks_row.location_id is None:
                socks_row.location_id = location_id
            out["socks"] = {
                "inbound_id": socks_row.id,
                "remote_id": socks.remote_id,
                "port": xui.socks_port,
                "inbound": "created" if socks_new else "existing",
            }
    return out


async def _setup_telemt(
    session_factory: SessionFactory,
    registry: inventory.PanelSource,
    secret_box: SecretBox,
    setup: LocalSetup,
    telemt: TelemtSetup,
    result: LocalSetupResult,
    sleep: Callable[[float], Awaitable[Any]],
) -> dict[str, Any]:
    panel_id, status = await _ensure_panel(
        session_factory,
        secret_box,
        kind=PanelKind.TELEMT,
        name=TELEMT_PANEL_NAME,
        url=telemt.url,
        api_token=telemt.api_token,
        tls_verify=False,
        options={
            "public_host": telemt.public_host,
            "public_port": telemt.public_port,
            "location_id": result.location_id,
        },
    )
    out: dict[str, Any] = {"panel_id": panel_id, "panel": status}
    try:
        backend = await registry.get(panel_id)
        out["version"] = await _ping(backend, "the Telegram proxy", sleep)
    except (SetupError, PanelError, LookupError) as exc:
        # The row is stored; the proxy may still be starting. Do not fail the whole setup for it.
        result.warnings.append(f"the Telegram proxy is not reachable yet: {str(exc)[:200]}")
        out["reachable"] = False
    else:
        out["reachable"] = True
    return out


def _default_http(install: PanelInstall) -> PanelHttp:
    headers = {}
    if install.kind == PanelKind.REMNAWAVE:
        # Remnawave only talks to clients that arrive through its HTTPS reverse proxy; inside the Docker network we
        # say so ourselves. The administrator session (used once, to create the bot's API token) is only accepted from
        # the dashboard's client type; API-token requests ignore this header.
        headers = {"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https", "X-Remnawave-Client-Type": "browser"}
    return panel_setup.AiohttpPanelHttp(install.url, headers)


async def _stored_token(
    session_factory: SessionFactory, secret_box: SecretBox, kind: PanelKind, name: str
) -> str | None:
    async with session_factory.begin() as session:
        panel = await session.scalar(select(Panel).where(Panel.kind == kind, Panel.name == name).order_by(Panel.id))
        stored = panel.api_token if panel is not None else None
    if not stored:
        return None
    try:
        return secret_box.decrypt(stored)
    except Exception:  # an unreadable token is replaced by a new one
        return None


async def _setup_panel(
    session_factory: SessionFactory,
    registry: inventory.PanelSource,
    secret_box: SecretBox,
    install: PanelInstall,
    result: LocalSetupResult,
    sleep: Callable[[float], Awaitable[Any]],
    http_factory: Callable[[PanelInstall], PanelHttp],
) -> dict[str, Any]:
    name = panel_setup.PANEL_NAMES[install.kind]
    if install.existing_token is None:
        token = await _stored_token(session_factory, secret_box, install.kind, name)
        if token:
            install = replace(install, existing_token=token)
    http = http_factory(install)
    out: dict[str, Any] = {"kind": str(install.kind)}
    failure = ""
    try:
        provision = await panel_setup.provision_panel(http, install, sleep)
    except PanelSetupError as exc:
        if install.kind not in panel_setup.BEST_EFFORT:
            raise SetupError(f"{name}: {str(exc)[:300]}") from None
        failure = str(exc)[:300]
        # The entry point of these panels has not been checked against a running instance: keep going and tell the
        # operator to finish it in the panel.
        provision = panel_setup.PanelProvision(
            username=install.admin_user if install.kind == PanelKind.MARZNESHIN else None,
            password=install.admin_password if install.kind == PanelKind.MARZNESHIN else None,
            api_token=install.existing_token,
        )
        result.warnings.append(
            f"{name}: the VPN entry point was not created automatically ({str(exc)[:200]}). "
            "Add a VLESS Reality inbound and a host for it in the panel"
        )
        out["entry_point"] = "manual"
    finally:
        await http.close()
    out.update(provision.summary)
    result.warnings.extend(provision.warnings)
    if not (provision.api_token or (provision.username and provision.password)):
        raise SetupError(f"{name}: {failure or 'no credentials for the bot could be obtained; see the panel log'}")
    options = {**provision.options, "location_id": result.location_id}
    panel_id, status = await _ensure_panel(
        session_factory,
        secret_box,
        kind=install.kind,
        name=name,
        url=install.url,
        username=provision.username,
        password=provision.password,
        api_token=provision.api_token,
        tls_verify=False,
        options=options,
    )
    out.update(panel_id=panel_id, panel=status)
    try:
        backend = await registry.get(panel_id)
        out["version"] = await _ping(backend, name, sleep)
    except (SetupError, PanelError, LookupError) as exc:
        result.warnings.append(f"{name} is stored but not reachable yet: {str(exc)[:200]}")
        out["reachable"] = False
    else:
        out["reachable"] = True
    return out
