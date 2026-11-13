"""Telegram-only subscription: one shared link that makes Telegram reachable.

A shared Xray JSON subscription (``/sub/tg/<token>``, introduced by the landing page ``/tg``) whose config sends
only Telegram through the VPN servers and connects everything else directly. The server enforces the same: on every
eligible 3x-ui panel one dedicated client (``tg-only``) lives on a VLESS inbound, and the panel's Xray template routes
that client to Telegram's addresses and domains and blocks the rest
(:func:`app.services.panels.xray_template.set_tg_only`), so the link cannot be used as a general VPN.

Which servers are offered: active 3x-ui panels whose first suitable inbound (VLESS with Reality or TLS) sits in a
location whose country (taken from the location's flag emoji) is not in ``TG_ONLY_EXCLUDED_COUNTRIES``. A location
without a recognisable flag is left out unless ``TG_ONLY_ALLOW_UNKNOWN_COUNTRY`` is on. Other panel kinds do not
support it yet.

State lives in ``kv``: the subscription token (``tgonly:token``), one client id per panel (``tgonly:uuid:<panel>``)
and what was applied on each panel (``tgonly:panel:<panel>``). A panel is offered only after the template rules are in
place and the client exists, in that order, so the client never exists without its restrictions. Panel I/O happens with
no database transaction open. The link is shared on purpose (the operator publishes it so people can reach Telegram
at all); the single token and client are rotated when it spreads too far. Nothing here logs the token.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select

from app.db.models import Endpoint, EndpointProfile, Inbound, KeyValue, Location, Panel, PanelKind
from app.db.session import read_session
from app.services import kv, tg_only_config
from app.services.links import build_vless, compact_reality_hello, effective_params, location_title
from app.services.panels.base import AccountSpec, PanelBackend, PanelError, TgOnlyRouting
from app.services.provisioning import account_flow, derive_sub_id, parse_params
from app.utils.security import random_token
from app.utils.time import utcnow
from app.utils.xray_outbound import LinkError, outbound_from_link

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import Settings
    from app.db.session import SessionFactory
    from app.services.provisioning import PanelSource

log = logging.getLogger(__name__)

EMAIL: Final = "tg-only"  # the client's email on every panel (unique per panel by construction)
COMMENT: Final = "Telegram-only"
TOKEN_KEY: Final = "tgonly:token"
NOTIFIED_PREFIX: Final = "tgonly:notified:"
PANEL_TIMEOUT_SEC: Final = 60.0
GIB: Final = 1024**3
RESET_PREFIX: Final = "tgonly:reset:"
USAGE_KEY: Final = "tgonly:usage"
ALERT_KEY: Final = "tgonly:alert"
TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_\-]{16,64}$")


def _uuid_key(panel_id: int) -> str:
    return f"tgonly:uuid:{panel_id}"


def _panel_key(panel_id: int) -> str:
    return f"tgonly:panel:{panel_id}"


# ---------------------------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------------------------
def country_of_flag(flag: str) -> str | None:
    """ISO 3166-1 alpha-2 code from a flag emoji (two regional indicator symbols), else ``None``."""
    letters: list[str] = []
    for char in flag or "":
        code = ord(char)
        if 0x1F1E6 <= code <= 0x1F1FF:
            letters.append(chr(code - 0x1F1E6 + ord("A")))
            if len(letters) == 2:
                return "".join(letters)
        else:
            letters.clear()
    return None


def flag_of_country(code: str) -> str | None:
    """The flag emoji of a two-letter country code, else ``None``."""
    code = (code or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        return None
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in code)


@dataclass(frozen=True, slots=True)
class EndpointInfo:
    endpoint_id: int
    address: str
    port: int
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Server:
    """One eligible 3x-ui server: the panel, the inbound that carries the client and how to reach it."""

    panel_id: int
    panel_name: str
    inbound_id: int
    remote_id: int  # inbound id inside 3x-ui
    flow: str  # client flow ("" where Vision cannot run)
    location_id: int | None
    title: str
    country: str | None
    endpoints: tuple[EndpointInfo, ...]


@dataclass(frozen=True, slots=True)
class Skipped:
    """A 3x-ui server that is not offered, with the reason (shown to admins)."""

    panel_id: int
    panel_name: str
    reason: str  # "country" | "unknown_country" | "no_inbound" | "no_endpoint"
    country: str | None = None


def _usable_inbound(inbound: Inbound) -> dict[str, Any] | None:
    params = parse_params(inbound.params)
    if str(params.get("protocol") or "vless").lower() != "vless":
        return None
    return params if str(params.get("security") or "") in ("reality", "tls") else None


async def load_servers(session_factory: SessionFactory, settings: Settings) -> tuple[list[Server], list[Skipped]]:
    """Eligible servers and the 3x-ui servers left out (with the reason). Empty when the feature is off."""
    if not settings.tg_only_enabled:
        return [], []
    excluded = {c.upper() for c in settings.tg_only_excluded_countries}
    async with read_session(session_factory) as session:
        panels = list(
            await session.scalars(
                select(Panel).where(Panel.kind == PanelKind.XUI, Panel.is_active.is_(True)).order_by(Panel.id)
            )
        )
        locations = {loc.id: loc for loc in await session.scalars(select(Location))}
        inbounds = list(
            await session.scalars(
                select(Inbound).where(Inbound.is_active.is_(True)).order_by(Inbound.sort_order, Inbound.id)
            )
        )
        endpoints = list(
            await session.scalars(
                select(Endpoint)
                .where(
                    Endpoint.is_active.is_(True),
                    Endpoint.auto_hidden.is_(False),
                    Endpoint.profile == EndpointProfile.STANDARD,
                )
                .order_by(Endpoint.sort_order, Endpoint.id)
            )
        )
    by_inbound: dict[int, list[Endpoint]] = {}
    for endpoint in endpoints:
        by_inbound.setdefault(endpoint.inbound_id, []).append(endpoint)
    servers: list[Server] = []
    skipped: list[Skipped] = []
    for panel in panels:
        chosen = next(
            ((ib, params) for ib in inbounds if ib.panel_id == panel.id and (params := _usable_inbound(ib))), None
        )
        if chosen is None:
            skipped.append(Skipped(panel.id, panel.name, "no_inbound"))
            continue
        inbound, params = chosen
        eps = by_inbound.get(inbound.id, [])
        location = _location_of(inbound, eps, locations)
        country = country_of_flag(location.flag) if location is not None else None
        if country is None and not settings.tg_only_allow_unknown_country:
            skipped.append(Skipped(panel.id, panel.name, "unknown_country"))
            continue
        if country is not None and country in excluded:
            skipped.append(Skipped(panel.id, panel.name, "country", country))
            continue
        infos = _endpoint_infos(params, eps, settings)
        if not infos:
            skipped.append(Skipped(panel.id, panel.name, "no_endpoint", country))
            continue
        servers.append(
            Server(
                panel_id=panel.id,
                panel_name=panel.name,
                inbound_id=inbound.id,
                remote_id=inbound.remote_id,
                flow=account_flow(inbound),
                location_id=location.id if location is not None else None,
                title=location_title(location, settings) if location is not None else panel.name,
                country=country,
                endpoints=tuple(infos),
            )
        )
    return servers, skipped


def _location_of(inbound: Inbound, endpoints: list[Endpoint], locations: dict[int, Location]) -> Location | None:
    ids = [inbound.location_id, *(ep.location_id for ep in endpoints)]
    for location_id in ids:
        location = locations.get(location_id) if location_id else None
        if location is not None and location.is_active:
            return location
    return None


def _endpoint_infos(params: dict[str, Any], endpoints: list[Endpoint], settings: Settings) -> list[EndpointInfo]:
    base = compact_reality_hello(params) if settings.reality_compact_hello else params
    result: list[EndpointInfo] = []
    for endpoint in endpoints:
        try:
            address, port, link_params = effective_params(base, endpoint)
        except ValueError as exc:
            log.warning("Telegram-only: endpoint %s skipped: %s", endpoint.id, exc)
            continue
        result.append(EndpointInfo(endpoint.id, address, port, link_params))
    return result


# ---------------------------------------------------------------------------------------------
# Token and client ids
# ---------------------------------------------------------------------------------------------
async def get_token(session_factory: SessionFactory) -> str:
    """The subscription token, created on first use (concurrent callers agree on one)."""
    async with read_session(session_factory) as session:
        existing = await kv.get(session, TOKEN_KEY)
    if existing:
        return existing
    async with session_factory.begin() as session:
        await kv.add(session, TOKEN_KEY, random_token(24))
        token = await kv.get(session, TOKEN_KEY, fresh=True)
    if not token:  # pragma: no cover - the row vanished between insert and read
        raise RuntimeError("could not persist the Telegram-only token")
    return token


async def token_matches(session_factory: SessionFactory, candidate: str) -> bool:
    """Constant-time check of a presented token (a token is never created by a lookup)."""
    if not TOKEN_RE.match(candidate):
        return False
    async with read_session(session_factory) as session:
        current = await kv.get(session, TOKEN_KEY)
    return current is not None and hmac.compare_digest(current.encode(), candidate.encode())


def page_url(settings: Settings, base: str | None = None) -> str:
    """The stable public landing page; it always shows the current subscription link."""
    return f"{(base or settings.public_base_url or '').rstrip('/')}/tg"


def subscription_url(settings: Settings, token: str, base: str | None = None) -> str:
    root = (base or settings.public_base_url or "").rstrip("/")
    return f"{root}/sub/tg/{token}"


async def _client_uuid(session_factory: SessionFactory, panel_id: int) -> str:
    key = _uuid_key(panel_id)
    async with read_session(session_factory) as session:
        existing = await kv.get(session, key)
    if existing:
        return existing
    async with session_factory.begin() as session:
        await kv.add(session, key, str(uuid_lib.uuid4()))
        value = await kv.get(session, key, fresh=True)
    if not value:  # pragma: no cover
        raise RuntimeError("could not persist the Telegram-only client id")
    return value


# ---------------------------------------------------------------------------------------------
# Convergence of the panels
# ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class PanelState:
    """What the last reconcile left on one panel."""

    panel_id: int
    remote_id: int | None = None  # inbound the client lives on
    ok: bool = False
    error: str | None = None
    at: str | None = None


@dataclass(slots=True)
class ReconcileReport:
    ready: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    failed: dict[int, str] = field(default_factory=dict)


async def load_states(session_factory: SessionFactory, panel_ids: list[int]) -> dict[int, PanelState]:
    states: dict[int, PanelState] = {}
    async with read_session(session_factory) as session:
        for panel_id in panel_ids:
            raw = await kv.get(session, _panel_key(panel_id))
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            remote = data.get("remote_id")
            states[panel_id] = PanelState(
                panel_id=panel_id,
                remote_id=remote if isinstance(remote, int) and not isinstance(remote, bool) else None,
                ok=data.get("ok") is True,
                error=data["error"] if isinstance(data.get("error"), str) else None,
                at=data["at"] if isinstance(data.get("at"), str) else None,
            )
    return states


async def _save_state(session_factory: SessionFactory, state: PanelState | None, panel_id: int) -> None:
    async with session_factory.begin() as session:
        if state is None:
            await kv.delete(session, _panel_key(panel_id))
            return
        payload = {"remote_id": state.remote_id, "ok": state.ok, "error": state.error, "at": state.at}
        await kv.set(session, _panel_key(panel_id), json.dumps(payload))


async def _keys_with_prefix(session: AsyncSession, prefix: str) -> list[str]:
    return list(await session.scalars(select(KeyValue.key).where(KeyValue.key.startswith(prefix, autoescape=True))))


async def _managed_panel_ids(session_factory: SessionFactory) -> list[int]:
    async with read_session(session_factory) as session:
        keys = await _keys_with_prefix(session, "tgonly:panel:")
    ids: list[int] = []
    for key in keys:
        tail = key.rsplit(":", 1)[-1]
        if tail.isdigit():
            ids.append(int(tail))
    return sorted(ids)


async def reconcile(session_factory: SessionFactory, registry: PanelSource, settings: Settings) -> ReconcileReport:
    """Bring every panel to the wanted state: eligible ones get rules and a client, the others lose both.

    Never raises for panel problems: they are recorded per panel and returned.
    """
    servers, _ = await load_servers(session_factory, settings)
    report = ReconcileReport()
    wanted = {server.panel_id for server in servers}
    results = await asyncio.gather(*(_apply_server(session_factory, registry, server, settings) for server in servers))
    for server, error in zip(servers, results, strict=True):
        if error is None:
            report.ready.append(server.panel_id)
        else:
            report.failed[server.panel_id] = error
    for panel_id in await _managed_panel_ids(session_factory):
        if panel_id in wanted:
            continue
        error = await _remove_panel(session_factory, registry, panel_id)
        if error is None:
            report.removed.append(panel_id)
        else:
            report.failed[panel_id] = error
    return report


async def _apply_server(
    session_factory: SessionFactory, registry: PanelSource, server: Server, settings: Settings
) -> str | None:
    """Rules first, then the client. Returns an error text, or ``None`` when the server is ready."""
    previous = (await load_states(session_factory, [server.panel_id])).get(server.panel_id)
    state = PanelState(server.panel_id, server.remote_id, previous.ok if previous else False)
    try:
        backend = await registry.get(server.panel_id)
        if not isinstance(backend, TgOnlyRouting):
            raise PanelError("this panel type does not support the Telegram-only subscription")
        client_id = await _client_uuid(session_factory, server.panel_id)
        async with asyncio.timeout(PANEL_TIMEOUT_SEC):
            await backend.apply_tg_only([EMAIL])
            await backend.ensure_sniffing()
            await _upsert_client(backend, server, client_id, settings, session_factory)
        state.ok, state.error = True, None
    except (Exception, TimeoutError) as exc:
        log.warning("Telegram-only: panel %s: %s", server.panel_id, exc)
        state.error = (str(exc) or type(exc).__name__)[:300]
    state.at = utcnow().isoformat()
    try:
        await _save_state(session_factory, state, server.panel_id)
    except Exception:
        log.exception("Telegram-only: could not store the state of panel %s", server.panel_id)
    return state.error


async def _upsert_client(
    backend: PanelBackend, server: Server, client_id: str, settings: Settings, session_factory: SessionFactory
) -> None:
    """Create or update the client; its traffic limit follows ``TG_ONLY_MONTHLY_LIMIT_GB`` (no Xray restart)."""
    limit = max(0, settings.tg_only_monthly_limit_gb) * GIB
    used = 0
    if limit:
        await _monthly_reset(backend, server, session_factory)
        used = await backend.get_used_bytes(server.remote_id, EMAIL) or 0
    spec = AccountSpec(
        email=EMAIL,
        uuid=client_id,
        enabled=not limit or used < limit,  # 3x-ui switches an exhausted client off; do not switch it back on
        expires_at=None,
        device_limit=0,
        traffic_limit_bytes=limit,
        tg_id=0,
        sub_id=derive_sub_id(client_id, EMAIL),
        comment=COMMENT,
        flow=server.flow,
    )
    await backend.upsert_account(server.remote_id, spec)


async def _monthly_reset(backend: PanelBackend, server: Server, session_factory: SessionFactory) -> None:
    """Zero the client's counter when a new calendar month (UTC) has begun since the last reset."""
    key = f"{RESET_PREFIX}{server.panel_id}"
    month = utcnow().strftime("%Y-%m")
    async with read_session(session_factory) as session:
        last = await kv.get(session, key)
    if last == month:
        return
    if last is not None:
        await backend.reset_traffic(server.remote_id, EMAIL)
    async with session_factory.begin() as session:
        await kv.set(session, key, month)


async def _remove_panel(session_factory: SessionFactory, registry: PanelSource, panel_id: int) -> str | None:
    """Delete the client first, then the rules (the other order would leave an unrestricted client)."""
    state = (await load_states(session_factory, [panel_id])).get(panel_id)
    try:
        backend = await registry.get(panel_id)
        async with asyncio.timeout(PANEL_TIMEOUT_SEC):
            if state is not None and state.remote_id is not None:
                await backend.delete_account(state.remote_id, EMAIL, "")
            if isinstance(backend, TgOnlyRouting):
                await backend.apply_tg_only([])
    except (Exception, TimeoutError) as exc:
        log.warning("Telegram-only: removing from panel %s failed: %s", panel_id, exc)
        return (str(exc) or type(exc).__name__)[:300]
    await _save_state(session_factory, None, panel_id)
    async with session_factory.begin() as session:
        await kv.delete(session, _uuid_key(panel_id))
        await kv.delete(session, f"{RESET_PREFIX}{panel_id}")
    return None


async def rotate(session_factory: SessionFactory, registry: PanelSource, settings: Settings) -> str:
    """New token and new client ids: the old link stops working and the old client ids are revoked on the panels.

    Returns the new token. The panels are updated before returning (failures are recorded per panel and retried by
    the periodic reconcile).
    """
    token = random_token(24)
    async with session_factory.begin() as session:
        await kv.set(session, TOKEN_KEY, token)
        for key in await _keys_with_prefix(session, "tgonly:uuid:"):
            await kv.delete(session, key)
    await reconcile(session_factory, registry, settings)
    return token


# ---------------------------------------------------------------------------------------------
# What admins and clients see
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ServerView:
    panel_id: int
    title: str
    country: str | None
    ready: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class AdminView:
    enabled: bool
    token: str
    url: str  # the raw subscription URL
    page: str  # the public landing page
    servers: list[ServerView]
    skipped: list[Skipped]

    @property
    def ready(self) -> bool:
        return any(server.ready for server in self.servers)


async def admin_view(session_factory: SessionFactory, settings: Settings) -> AdminView:
    servers, skipped = await load_servers(session_factory, settings)
    token = await get_token(session_factory)
    states = await load_states(session_factory, [s.panel_id for s in servers])
    views = [
        ServerView(
            s.panel_id,
            s.title,
            s.country,
            bool(states.get(s.panel_id) and states[s.panel_id].ok),
            states[s.panel_id].error if s.panel_id in states else None,
        )
        for s in servers
    ]
    return AdminView(
        settings.tg_only_enabled, token, subscription_url(settings, token), page_url(settings), views, skipped
    )


@dataclass(frozen=True, slots=True)
class Rendered:
    """The Xray JSON configs and how many servers they cover."""

    configs: list[dict[str, Any]]
    servers: int


async def render_configs(session_factory: SessionFactory, settings: Settings, language: str | None) -> Rendered | None:
    """The subscription's configs, or ``None`` when no server is ready (the caller answers with an error)."""
    servers, _ = await load_servers(session_factory, settings)
    states = await load_states(session_factory, [s.panel_id for s in servers])
    outbounds: list[dict[str, Any]] = []
    used = 0
    for server in servers:
        state = states.get(server.panel_id)
        if state is None or not state.ok:
            continue  # rules and client are not in place yet: never hand out a half-restricted client
        client_id = await _client_uuid(session_factory, server.panel_id)
        built = 0
        for endpoint in server.endpoints:
            link = build_vless(client_id, endpoint.address, endpoint.port, endpoint.params, EMAIL, None, server.flow)
            try:
                outbounds.append(outbound_from_link(link, f"proxy-{len(outbounds) + 1}"))
                built += 1
            except LinkError as exc:
                log.info("Telegram-only: an entry of panel %s is left out: %s", server.panel_id, exc)
        used += 1 if built else 0
    if not outbounds:
        return None
    return Rendered([tg_only_config.build_config(outbounds, settings, language)], used)


# ---------------------------------------------------------------------------------------------
# One-time message to the owners
# ---------------------------------------------------------------------------------------------
async def pending_owner_notices(session_factory: SessionFactory, settings: Settings) -> list[int]:
    """Owners who have not been sent the link yet, once at least one server is ready."""
    if not settings.tg_only_enabled or not settings.admin_ids:
        return []
    servers, _ = await load_servers(session_factory, settings)
    states = await load_states(session_factory, [s.panel_id for s in servers])
    if not any(state.ok for state in states.values()):
        return []
    async with read_session(session_factory) as session:
        return [owner for owner in settings.admin_ids if await kv.get(session, f"{NOTIFIED_PREFIX}{owner}") is None]


async def mark_notified(session_factory: SessionFactory, tg_id: int) -> None:
    async with session_factory.begin() as session:
        await kv.set(session, f"{NOTIFIED_PREFIX}{tg_id}", utcnow().isoformat())


# ---------------------------------------------------------------------------------------------
# Traffic alert
# ---------------------------------------------------------------------------------------------
async def check_daily_traffic(
    session_factory: SessionFactory, registry: PanelSource, settings: Settings, now: datetime | None = None
) -> int | None:
    """Bytes moved by the shared client since the daily baseline when that exceeds ``TG_ONLY_DAILY_ALERT_GB``.

    Returns ``None`` when there is nothing to report. The baseline (total counter and time) is renewed once it is a day
    old or when the counters were reset; an alert is reported at most once per UTC day. A panel that cannot be read
    skips the check.
    """
    threshold = max(0, settings.tg_only_daily_alert_gb) * GIB
    if not threshold or not settings.tg_only_enabled:
        return None
    now = now or utcnow()
    servers, _ = await load_servers(session_factory, settings)
    states = await load_states(session_factory, [s.panel_id for s in servers])
    total = 0
    for server in servers:
        if not (states.get(server.panel_id) and states[server.panel_id].ok):
            continue
        try:
            backend = await registry.get(server.panel_id)
            async with asyncio.timeout(PANEL_TIMEOUT_SEC):
                total += await backend.get_used_bytes(server.remote_id, EMAIL) or 0
        except (Exception, TimeoutError) as exc:
            log.info("Telegram-only: traffic of panel %s is unreadable: %s", server.panel_id, type(exc).__name__)
            return None
    async with read_session(session_factory) as session:
        raw_base = await kv.get(session, USAGE_KEY)
        last_alert = await kv.get(session, ALERT_KEY)
    base_at, base_bytes = _parse_base(raw_base)
    result: int | None = None
    renew = base_at is None or total < base_bytes or now - base_at >= timedelta(days=1)
    today = now.strftime("%Y-%m-%d")
    if base_at is not None and total >= base_bytes and total - base_bytes > threshold and last_alert != today:
        result = total - base_bytes
    async with session_factory.begin() as session:
        if result is not None:
            await kv.set(session, ALERT_KEY, today)
        if renew:
            await kv.set(session, USAGE_KEY, json.dumps({"at": now.isoformat(), "bytes": total}))
    return result


def _parse_base(raw: str | None) -> tuple[datetime | None, int]:
    if not raw:
        return None, 0
    try:
        data = json.loads(raw)
        return datetime.fromisoformat(data["at"]), int(data["bytes"])
    except (ValueError, KeyError, TypeError):
        return None, 0
