"""Server management logic used by the bot screens: connection tests, panel removal with cleanup, Reality
inbound creation, SNI reachability checks, endpoint presets and link previews.

Every panel call happens with no database transaction open.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from sqlalchemy import delete, select

from app.db.models import (
    Endpoint,
    EndpointKind,
    EndpointProfile,
    Inbound,
    Location,
    Panel,
    PanelKind,
    Provision,
    Subscription,
    TgProxyAccess,
    TgSocksAccess,
)
from app.db.session import read_session
from app.services import audit, links, provisioning
from app.services.panels import inventory, shadowsocks
from app.services.panels.base import InboundInfo, PanelBackend, PanelError
from app.services.panels.registry import build_backend
from app.services.panels.socks import SocksProxyBackend

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_SEC = 25.0
SNI_TIMEOUT_SEC = 5.0
PREVIEW_UUID = "00000000-0000-4000-8000-000000000000"
RECONCILE_JOB = "provisioning.reconcile"
HEALTH_JOB = "panels.health"
MAX_ERROR = 300

# Camouflage sites for VLESS-Reality: big TLS 1.3 + HTTP/2 sites without redirects on the root page.
SNI_GLOBAL: tuple[str, ...] = (
    "www.microsoft.com",
    "www.apple.com",
    "dl.google.com",
    "www.cloudflare.com",
    "www.amazon.com",
    "addons.mozilla.org",
    "www.samsung.com",
    "www.nvidia.com",
)
# For servers in Russia: domestic sites (useful on mobile networks with allowlists).
SNI_RU: tuple[str, ...] = (
    "ya.ru",
    "www.ozon.ru",
    "www.wildberries.ru",
    "vk.com",
    "www.avito.ru",
    "www.gosuslugi.ru",
)


@runtime_checkable
class RealityCreator(Protocol):
    async def create_reality_inbound(
        self, port: int, sni: str, dest: str | None = None, remark: str = "", flow: str = ...
    ) -> InboundInfo: ...


@runtime_checkable
class ProxyUserRemover(Protocol):
    async def delete_proxy_user(self, target: int | None, name: str) -> None: ...


def short_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if len(text) <= MAX_ERROR else text[: MAX_ERROR - 1] + "…"


def normalize_url(raw: str) -> str | None:
    """``https://host:port/path`` without a trailing slash, or None if it is not an HTTP(S) URL."""
    value = raw.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or " " in value:
        return None
    return value


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


# --- drafts & connection tests ----------------------------------------------------------------------------------------
@dataclass(slots=True)
class PanelDraft:
    """A panel being added or edited; secrets are kept encrypted (``SecretBox``)."""

    kind: PanelKind
    name: str = ""
    url: str = ""
    username: str | None = None
    password: str | None = None  # encrypted
    api_token: str | None = None  # encrypted
    tls_verify: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "url": self.url,
            "username": self.username,
            "password": self.password,
            "api_token": self.api_token,
            "tls_verify": self.tls_verify,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PanelDraft:
        return cls(
            kind=PanelKind(data["kind"]),
            name=str(data.get("name") or ""),
            url=str(data.get("url") or ""),
            username=data.get("username"),
            password=data.get("password"),
            api_token=data.get("api_token"),
            tls_verify=bool(data.get("tls_verify", True)),
        )

    def default_options(self) -> dict[str, Any]:
        if self.kind == PanelKind.REMNAWAVE:
            # Direct calls to the container need X-Forwarded-* headers; a public HTTPS address does not.
            return {"squad_uuids": [], "direct": self.url.startswith("http://")}
        return {}

    def as_panel(self, options: str | None = None) -> Panel:
        defaults = self.default_options()
        return Panel(
            name=self.name or self.kind.value,
            kind=self.kind,
            url=self.url,
            username=self.username,
            password=self.password,
            api_token=self.api_token,
            tls_verify=self.tls_verify,
            options=options if options is not None else (json.dumps(defaults) if defaults else None),
            is_active=True,
        )


@dataclass(slots=True)
class ConnectionResult:
    ok: bool
    version: str | None = None
    inbounds: int | None = None
    error: str | None = None


async def test_connection(ctx: AppContext, draft: PanelDraft, options: str | None = None) -> ConnectionResult:
    """Log in with the draft's credentials and read the version (and the inbound count for 3x-ui)."""
    try:
        backend = build_backend(draft.as_panel(options), ctx.secret_box, ctx.settings)
    except PanelError as exc:
        return ConnectionResult(False, error=short_error(exc))
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT_SEC):
            version = await backend.ping()
            count = len(await backend.list_inbounds()) if draft.kind == PanelKind.XUI else None
        return ConnectionResult(True, version=version, inbounds=count)
    except TimeoutError:
        return ConnectionResult(False, error=f"no answer within {CONNECT_TIMEOUT_SEC:.0f}s")
    except (PanelError, OSError, ValueError) as exc:
        return ConnectionResult(False, error=short_error(exc))
    finally:
        try:
            await backend.close()
        except Exception:
            log.debug("closing a test backend failed", exc_info=True)


async def ping_panel(ctx: AppContext, panel_id: int) -> ConnectionResult:
    try:
        backend = await ctx.panels.get(panel_id)
        async with asyncio.timeout(CONNECT_TIMEOUT_SEC):
            version = await backend.ping()
        return ConnectionResult(True, version=version)
    except TimeoutError:
        return ConnectionResult(False, error=f"no answer within {CONNECT_TIMEOUT_SEC:.0f}s")
    except (PanelError, LookupError, OSError, ValueError) as exc:
        return ConnectionResult(False, error=short_error(exc))


async def create_panel(ctx: AppContext, draft: PanelDraft, actor_id: int, version: str | None) -> int:
    panel = draft.as_panel()
    panel.version = (version or "")[:32] or None
    async with ctx.session_factory.begin() as session:
        session.add(panel)
        await session.flush()
        await audit.log(session, actor_id, "panel.create", "panel", panel.id, name=panel.name, kind=panel.kind.value)
        return panel.id


# --- removal ----------------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class DeleteReport:
    removed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


async def _accounts_on(
    ctx: AppContext, panel_id: int
) -> tuple[list[tuple[int | None, str, str]], list[tuple[int | None, str]], list[tuple[int | None, str]]]:
    """(VPN accounts: target, email, uuid), (Telegram proxy users: target, name), (SOCKS5 users: target, name)
    the bot created on the panel."""
    async with read_session(ctx.session_factory) as session:
        rows = (
            await session.execute(
                select(Inbound.remote_id, Provision.remote_email, Subscription.client_uuid)
                .select_from(Provision)
                .join(Subscription, Subscription.id == Provision.subscription_id)
                .outerjoin(Inbound, Inbound.id == Provision.inbound_id)
                .where(Provision.panel_id == panel_id, Provision.remote_email.is_not(None))
            )
        ).all()
        proxy_rows = (
            await session.execute(
                select(Inbound.remote_id, TgProxyAccess.remote_name)
                .select_from(TgProxyAccess)
                .outerjoin(Inbound, Inbound.id == TgProxyAccess.inbound_id)
                .where(TgProxyAccess.panel_id == panel_id)
            )
        ).all()
        socks_rows = (
            await session.execute(
                select(Inbound.remote_id, TgSocksAccess.username)
                .select_from(TgSocksAccess)
                .join(Inbound, Inbound.id == TgSocksAccess.inbound_id)
                .where(TgSocksAccess.panel_id == panel_id)
            )
        ).all()
    accounts: list[tuple[int | None, str, str]] = [(target, str(email), str(uuid)) for target, email, uuid in rows]
    proxies: list[tuple[int | None, str]] = [(target, str(name)) for target, name in proxy_rows]
    socks: list[tuple[int | None, str]] = [(target, str(name)) for target, name in socks_rows]
    return accounts, proxies, socks


async def count_accounts(ctx: AppContext, panel_id: int) -> int:
    accounts, proxies, socks = await _accounts_on(ctx, panel_id)
    return len(accounts) + len(proxies) + len(socks)


async def delete_panel(ctx: AppContext, panel_id: int, actor_id: int, *, cleanup: bool) -> DeleteReport:
    """Delete the panel row (inbounds, endpoints and provisions go with it); with ``cleanup`` first remove every
    account the bot created on the panel. Unreachable accounts are counted in ``failed`` (the row is deleted anyway).
    """
    report = DeleteReport()
    if cleanup:
        accounts, proxies, socks = await _accounts_on(ctx, panel_id)
        try:
            backend: PanelBackend | None = await ctx.panels.get(panel_id)
        except (PanelError, LookupError) as exc:
            backend = None
            report.failed = len(accounts) + len(proxies) + len(socks)
            report.errors.append(short_error(exc))
        if backend is not None:
            for target, email, uuid in accounts:
                await _remove(report, backend.delete_account(target, email, uuid))
            for target, name in proxies:
                if isinstance(backend, ProxyUserRemover):
                    await _remove(report, backend.delete_proxy_user(target, name))
                else:
                    report.failed += 1
            for target, name in socks:
                if isinstance(backend, SocksProxyBackend):
                    await _remove(report, backend.delete_socks_user(target, name))
                else:
                    report.failed += 1
    async with ctx.session_factory.begin() as session:
        panel = await session.get(Panel, panel_id, with_for_update=True)
        if panel is None:
            return report
        name = panel.name
        # Inbounds, endpoints, provisions and proxy accounts go with it (ON DELETE CASCADE).
        await session.execute(delete(Panel).where(Panel.id == panel_id).execution_options(synchronize_session=False))
        await audit.log(
            session,
            actor_id,
            "panel.delete",
            "panel",
            panel_id,
            name=name,
            cleanup=cleanup,
            removed=report.removed,
            failed=report.failed,
        )
    await ctx.panels.invalidate(panel_id)
    return report


async def _remove(report: DeleteReport, call: Any) -> None:
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT_SEC):
            await call
        report.removed += 1
    except (PanelError, OSError, TimeoutError, ValueError) as exc:
        report.failed += 1
        if len(report.errors) < 3:
            report.errors.append(short_error(exc))


# --- sync -------------------------------------------------------------------------------------------------------------
def kick_job(ctx: AppContext, job_id: str) -> bool:
    """Run a background job now if this process runs jobs; False when another process will run it later."""
    scheduler = getattr(ctx, "scheduler", None)
    if scheduler is None or job_id not in getattr(scheduler, "job_ids", ()):
        return False
    try:
        return bool(scheduler.run_now(job_id))
    except Exception:
        log.exception("could not start job %s", job_id)
        return False


async def sync_now(ctx: AppContext, panel_id: int, actor_id: int) -> tuple[int, bool]:
    """Mark every account of the panel for re-push and start the sync job; (accounts marked, started now)."""
    async with ctx.session_factory.begin() as session:
        marked = await provisioning.mark_dirty(session, panel_id=panel_id)
        await audit.log(session, actor_id, "panel.sync", "panel", panel_id, marked=marked)
    return marked, kick_job(ctx, RECONCILE_JOB)


async def refresh(ctx: AppContext, panel_id: int, **kwargs: Any) -> inventory.RefreshResult:
    return await inventory.refresh_inbounds(ctx.session_factory, ctx.panels, panel_id, **kwargs)


# --- Reality inbounds -------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SniCheck:
    host: str
    ok: bool
    latency_ms: int | None = None
    problem: str | None = None  # "tls13" | "h2" | "unreachable"


async def check_sni(host: str, *, port: int = 443, limit_sec: float = SNI_TIMEOUT_SEC) -> SniCheck:
    """Can ``host`` serve as a Reality camouflage target: TLS 1.3 and HTTP/2 from this server?"""
    context = ssl.create_default_context()
    context.set_alpn_protocols(["h2", "http/1.1"])
    started = time.monotonic()
    try:
        async with asyncio.timeout(limit_sec):
            _reader, writer = await asyncio.open_connection(host, port, ssl=context, server_hostname=host)
    except (OSError, TimeoutError, ssl.SSLError, ValueError):
        return SniCheck(host, False, problem="unreachable")
    latency = int((time.monotonic() - started) * 1000)
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        version = ssl_object.version() if ssl_object else None
        alpn = ssl_object.selected_alpn_protocol() if ssl_object else None
    finally:
        writer.close()
        try:
            async with asyncio.timeout(1):
                await writer.wait_closed()
        except (OSError, TimeoutError, ssl.SSLError):
            pass
    if version != "TLSv1.3":
        return SniCheck(host, False, latency, "tls13")
    if alpn != "h2":
        return SniCheck(host, False, latency, "h2")
    return SniCheck(host, True, latency)


async def check_snis(hosts: Sequence[str]) -> list[SniCheck]:
    results = await asyncio.gather(*(check_sni(host) for host in hosts))
    return sorted(results, key=lambda r: (not r.ok, r.latency_ms or 10**6))


async def used_ports(ctx: AppContext, panel_id: int) -> set[int]:
    backend = await ctx.panels.get(panel_id)
    return {info.port for info in await backend.list_inbounds()}


async def suggest_endpoint_address(ctx: AppContext, panel_id: int) -> str:
    """Where clients should connect to a new inbound of this panel.

    The panel URL is where the bot reaches the API and is often an internal address or a proxied domain, so prefer
    the address of the panel's existing direct endpoints, then ``XUI_PUBLIC_HOST`` for the panel configured through
    ``XUI_HOST``, and only then the URL host.
    """
    async with read_session(ctx.session_factory) as session:
        panel = await session.get(Panel, panel_id)
        addresses = (
            await session.scalars(
                select(Endpoint.address)
                .join(Inbound, Inbound.id == Endpoint.inbound_id)
                .where(Inbound.panel_id == panel_id, Endpoint.kind == EndpointKind.DIRECT)
                .order_by(Endpoint.is_active.desc(), Endpoint.id)
            )
        ).all()
    if addresses:
        return Counter(addresses).most_common(1)[0][0]
    url_host = host_of(panel.url) if panel else ""
    settings = ctx.settings
    if settings.xui_public_host and url_host and url_host == host_of(settings.xui_host or ""):
        return settings.xui_public_host
    return url_host


async def create_reality(
    ctx: AppContext,
    panel_id: int,
    actor_id: int,
    *,
    port: int,
    sni: str,
    remark: str,
    location_id: int | None,
    endpoint_address: str | None = None,
) -> inventory.RefreshResult:
    """Create a VLESS-Reality inbound on a 3x-ui panel and import it (active, with a direct endpoint at
    ``endpoint_address``, by default :func:`suggest_endpoint_address`)."""
    backend = await ctx.panels.get(panel_id)
    if not isinstance(backend, RealityCreator):
        raise PanelError("this panel cannot create inbounds")
    address = endpoint_address or await suggest_endpoint_address(ctx, panel_id)
    info = await backend.create_reality_inbound(port, sni, remark=remark)
    result = await refresh(
        ctx,
        panel_id,
        activate_new=True,
        only_remote_ids=[info.remote_id] if info.remote_id else None,
        location_id=location_id,
        endpoint_address=address or None,
    )
    async with ctx.session_factory.begin() as session:
        await audit.log(
            session, actor_id, "panel.create_inbound", "panel", panel_id, port=port, sni=sni, remote_id=info.remote_id
        )
    return result


# --- endpoints --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Preset:
    key: str
    kind: EndpointKind
    profile: EndpointProfile
    overrides: dict[str, Any]
    needs_port: bool = False
    needs_sni: bool = False


CDN_XHTTP_OVERRIDES: dict[str, Any] = {
    "mode": "packet-up",
    "alpn": "h2,http/1.1",
    "fp": "chrome",
    "extra": {
        "mode": "packet-up",
        "uplinkHTTPMethod": "GET",
        "uplinkDataPlacement": "header",
        "uplinkDataKey": "X-Data",
        "scMaxEachPostBytes": "4096",
        "xPaddingBytes": "100-1000",
    },
}

PRESETS: dict[str, Preset] = {
    "direct": Preset("direct", EndpointKind.DIRECT, EndpointProfile.STANDARD, {}),
    "relay": Preset("relay", EndpointKind.RELAY, EndpointProfile.WHITELIST, {}, needs_port=True),
    "cdn": Preset("cdn", EndpointKind.CDN, EndpointProfile.WHITELIST, CDN_XHTTP_OVERRIDES),
    "ru_sni": Preset("ru_sni", EndpointKind.RELAY, EndpointProfile.WHITELIST, {}, needs_port=True, needs_sni=True),
}


def inbound_params(inbound: Inbound) -> dict[str, Any]:
    try:
        data = json.loads(inbound.params or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def preview_link(inbound: Inbound, endpoint: Endpoint, location: Location | None, settings: Any) -> str:
    """The link a user would get for this endpoint (with a placeholder id). Raises ValueError on bad overrides."""
    base = inbound_params(inbound)
    if shadowsocks.is_shadowsocks(base):
        name = links.entry_name(location, endpoint, settings, protocol_tag=links.SS_SUFFIX)
        return links.preview_ss(base, endpoint, name, PREVIEW_UUID)[0]
    if getattr(settings, "reality_compact_hello", False):
        base = links.compact_reality_hello(base)  # the same link a user receives
    address, port, params = links.effective_params(base, endpoint)
    name = links.entry_name(location, endpoint, settings)
    return links.build_vless(PREVIEW_UUID, address, port, params, name, endpoint.description, inbound.flow)


def parse_host_port(text: str) -> tuple[str, int | None] | None:
    """``1.2.3.4``, ``relay.example.ru:8443`` or ``[2001:db8::1]:443`` -> (host, port)."""
    value = text.strip()
    if not value or " " in value or "/" in value:
        return None
    if value.startswith("["):
        host, _sep, rest = value[1:].partition("]")
        port_text = rest[1:] if rest.startswith(":") else ""
    elif value.count(":") == 1:
        host, port_text = value.split(":", 1)
    else:
        host, port_text = value, ""
    if not host:
        return None
    if not port_text:
        return host, None
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        return None
    return host, int(port_text)


async def probe_endpoint(inbound: Inbound, endpoint: Endpoint) -> str | None:
    """TCP check of the endpoint's address; None if it accepts connections, else the error."""
    port = endpoint.port or inventory.inbound_port(inbound)
    if port is None:
        return "no port"
    return await inventory.tcp_probe(endpoint.address, port)
