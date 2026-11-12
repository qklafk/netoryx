"""Personal subscription URL (``/sub/<token>``) rendering for Happ, INCY, v2RayTun, Hiddify, ...

The body is the de-facto standard every client accepts: base64 of newline-joined share links. Metadata
travels in HTTP headers understood by all of them (``profile-title: base64:...``,
``subscription-userinfo``, ``profile-update-interval``, ``support-url``, ``announce``, ...).

Entries:

* 3x-ui: one link per active :class:`~app.db.models.Endpoint` of an active inbound on an active panel,
  built locally (:mod:`app.services.links`); auto-hidden (failing) endpoints are skipped.
* Remnawave: links returned by the panel for the user's provision (cached for a minute). The last good links
  are also saved on the provision (``Provision.links_cache``, encrypted when a secret box is given), so a panel
  API outage serves them even after a restart or a revision bump; they are dropped once the client key changes.
  With a device limit the links come from the panel's own subscription (``/api/sub/<shortUuid>``) fetched on
  behalf of the client with its HWID headers, so the panel registers the device and enforces the limit (a device
  over the limit gets the panel's notice instead of servers). Those answers are cached per device and never
  handed to another one, not even during an outage.

Unreachable panels never stall the response: panels that health checks marked down
(:func:`~app.services.provisioning.is_panel_down`) are not called at all (their last known data is served),
failures are cached briefly so a dead panel is not retried on every request, and the informational usage
counters (``subscription-userinfo``) get a much shorter budget than links.

Order: location ``sort_order``, then standard before whitelist ("LTE"), then endpoint ``sort_order``.
A subscription that is expired, disabled, banned or missing yields a single placeholder entry
("⛔ Подписка истекла — продлите в боте") pointing nowhere, plus an announcement, so the client shows
why it stopped working instead of a silent empty list. An active subscription left with no entries at all
because a panel failed is flagged (:attr:`RenderedSubscription.servers_unavailable`): the endpoint should answer
503 so clients keep their previous server list instead of replacing it with an empty one.

No DB transaction is held during panel calls: data is read first, then panels are queried, and the saved
links are updated afterwards in a short transaction of their own.
"""

from __future__ import annotations

import asyncio
import base64
import enum
import hashlib
import json
import logging
import re
import time
import uuid as uuid_lib
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import quote

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    Endpoint,
    EndpointProfile,
    Inbound,
    Location,
    Panel,
    PanelKind,
    Provision,
    ProvisionState,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionFactory, read_session
from app.services import hwid, subscription_json
from app.services.links import (
    DESCRIPTION_MAX_LEN,
    SS_SUFFIX,
    build_ss,
    build_vless,
    compact_reality_hello,
    effective_params,
    effective_ss,
    entry_name,
    truncate,
)
from app.services.panels import shadowsocks
from app.services.panels.base import PanelBackend, PanelError, RemoteAccount
from app.services.panels.remnawave import is_placeholder_link
from app.services.provisioning import is_panel_down
from app.utils.crypto import DecryptionError, SecretBox

logger = logging.getLogger(__name__)

Profile = Literal["all", "standard", "whitelist"]

GIB = 1024**3
PROFILE_TITLE_MAX_LEN = 25
ANNOUNCE_MAX_LEN = 200
PANEL_CALL_TIMEOUT_SEC = 8.0
USAGE_CALL_TIMEOUT_SEC = 1.5  # usage is informational: it must not hold back the body
REMNAWAVE_LINKS_TTL_SEC = 60.0
USAGE_TTL_SEC = 300.0
PANEL_FAILURE_TTL_SEC = 30.0  # negative cache of Remnawave links: a dead panel is not retried per request
USAGE_FAILURE_TTL_SEC = 60.0  # negative cache of usage when every queried panel failed
PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000"
WHITELIST_TITLE_SUFFIX = " LTE"
MEDIA_TYPE = "text/plain; charset=utf-8"
_PRINTABLE_ASCII = "".join(chr(code) for code in range(32, 127))
# Client headers Remnawave uses to identify a device (forwarded with the client's address).
DEVICE_HEADERS = ("x-hwid", "x-device-os", "x-ver-os", "x-device-model")
# Panel answer headers that mark its notice in place of servers (device limit reached, app without HWID support).
DEVICE_NOTICE_HEADERS = ("x-hwid-max-devices-reached", "x-hwid-not-supported", "x-hwid-limit")
# Apps the panel's default response rules answer with YAML/JSON configs: their user agent is not forwarded,
# so the panel falls back to the base64 list this endpoint merges.
_CONFIG_FORMAT_AGENTS = re.compile(r"clash|mihomo|stash|sing-?box|karing|flowvy|murge|prizrak|rabbit|^sf[aimt]\b", re.I)


class AccessState(enum.StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"  # disabled by an admin or the user is banned
    NONE = "none"  # the user has no subscription yet


# Placeholder entry name and announcement per state and language.
PLACEHOLDER_NAMES: dict[AccessState, dict[str, str]] = {
    AccessState.EXPIRED: {
        "ru": "⛔ Подписка истекла — продлите в боте",
        "en": "⛔ Subscription expired — renew in the bot",
    },
    AccessState.DISABLED: {
        "ru": "⛔ Подписка приостановлена — напишите в поддержку",
        "en": "⛔ Subscription suspended — contact support",
    },
    AccessState.NONE: {
        "ru": "⛔ Нет активной подписки — оформите в боте",
        "en": "⛔ No active subscription — get one in the bot",
    },
}
PLACEHOLDER_ANNOUNCES: dict[AccessState, dict[str, str]] = {
    AccessState.EXPIRED: {
        "ru": "Подписка истекла. Продлите её в боте — серверы вернутся после обновления подписки.",
        "en": "Your subscription has expired. Renew it in the bot; servers return after a subscription refresh.",
    },
    AccessState.DISABLED: {
        "ru": "Подписка приостановлена. Напишите в поддержку, чтобы узнать подробности.",
        "en": "Your subscription is suspended. Please contact support for details.",
    },
    AccessState.NONE: {
        "ru": "У вас нет активной подписки. Оформите её в боте — серверы появятся после обновления.",
        "en": "You have no active subscription. Get one in the bot; servers appear after a refresh.",
    },
}

# Notices that replace the server list when the bot-side device limit applies (see :mod:`app.services.hwid`).
DEVICE_LIMIT_NAMES = {
    "ru": "⚠️ Достигнут лимит устройств ({limit}). Отвяжите старое устройство в боте",
    "en": "⚠️ Device limit reached ({limit}). Unlink an old device in the bot",
}
DEVICE_LIMIT_ANNOUNCES = {
    "ru": "Достигнут лимит устройств ({limit}). Отвяжите ненужное устройство в боте и обновите подписку.",
    "en": "Device limit ({limit}) reached. Unlink an unused device in the bot, then refresh the subscription.",
}
HWID_UNSUPPORTED_NAMES = {
    "ru": "⚠️ Приложение не поддерживает лимит устройств — используйте Happ или v2RayTun",
    "en": "⚠️ This app doesn't support device limits — use Happ or v2RayTun",
}
HWID_UNSUPPORTED_ANNOUNCES = {
    "ru": "Это приложение не передаёт идентификатор устройства. Откройте подписку в Happ, v2RayTun или другом "
    "приложении с поддержкой HWID.",
    "en": "This app does not send a device identifier. Open the subscription in Happ, v2RayTun or another app "
    "that supports HWID.",
}

# "RU direct" routing profile (Happ / INCY format): Russian domains and IPs bypass the VPN.
# Bump ROUTING_PROFILE_VERSION whenever the rules change so clients re-import the profile.
ROUTING_PROFILE_VERSION = "1793750400"
RU_DIRECT_SITES = [
    "geosite:category-ru",
    "regexp:.*\\.ru$",
    "regexp:.*\\.su$",
    "regexp:.*\\.xn--p1ai$",  # .рф
]
PRIVATE_IPS = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "224.0.0.0/4",
    "255.255.255.255",
]
_ROUTING_NAMESPACE = uuid_lib.UUID("8d3c3a52-6d0e-4a55-9a43-6f1f5f0a7c11")


class BackendRegistry(Protocol):
    """What this module needs from ``app.services.panels.registry.PanelRegistry``."""

    async def get(self, panel_id: int) -> PanelBackend: ...


@dataclass(frozen=True, slots=True)
class RenderedSubscription:
    body: str  # base64 of newline-joined links
    headers: dict[str, str]
    links: list[str]  # decoded entries, for previews (Mini App, admin)
    state: AccessState = AccessState.ACTIVE
    media_type: str = MEDIA_TYPE
    # Active subscription, yet no entries because a panel failed and nothing could be served in its place:
    # answer with an error status (503) so clients keep the list they already have.
    servers_unavailable: bool = False


# ---------------------------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------------------------
class _TTLCache:
    """Tiny in-process TTL cache that also remembers stale values (served when a panel is down)."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._data: dict[Hashable, tuple[float, Any]] = {}
        self._max = max_entries

    def get(self, key: Hashable) -> Any | None:
        item = self._data.get(key)
        if item is None or item[0] < time.monotonic():
            return None
        return item[1]

    def get_stale(self, key: Hashable) -> Any | None:
        item = self._data.get(key)
        return item[1] if item else None

    def set(self, key: Hashable, value: Any, ttl: float) -> None:
        if len(self._data) >= self._max and key not in self._data:
            self._data.pop(next(iter(self._data)))  # evict the oldest insertion
        self._data[key] = (time.monotonic() + ttl, value)

    def clear(self) -> None:
        self._data.clear()


_remnawave_links_cache = _TTLCache()  # successful answers only
_remnawave_failures = _TTLCache()  # negative cache, kept apart so failures never evict good links
_usage_cache = _TTLCache()


def clear_caches() -> None:
    """Drop cached panel data (tests, admin "refresh")."""
    _remnawave_links_cache.clear()
    _remnawave_failures.clear()
    _usage_cache.clear()


# ---------------------------------------------------------------------------------------------
# Data loading (one short read-only session)
# ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class _XuiRow:
    endpoint: Endpoint
    inbound: Inbound
    location: Location | None


@dataclass(slots=True)
class _RemnawaveRow:
    provision: Provision
    panel: Panel
    location: Location | None


@dataclass(slots=True)
class _Snapshot:
    user: User
    sub: Subscription | None
    xui: list[_XuiRow] = field(default_factory=list)
    remnawave: list[_RemnawaveRow] = field(default_factory=list)
    counts_devices: bool = False  # some of the subscription's servers cannot enforce the device limit themselves


# Panels whose accounts carry no native device limit: the bot counts devices for them.
BOT_COUNTED_KINDS = frozenset({PanelKind.XUI, PanelKind.MARZBAN, PanelKind.MARZNESHIN, PanelKind.HIDDIFY})


async def _load_snapshot(
    session_factory: SessionFactory, sub_token: str, location_id: int | None = None, bot_counts_xui: bool = False
) -> _Snapshot | None:
    async with read_session(session_factory) as session:
        sub = await session.scalar(select(Subscription).where(Subscription.sub_token == sub_token))
        if sub is not None:
            user = await session.get(User, sub.user_id)
        else:
            user = await session.scalar(select(User).where(User.sub_token == sub_token))
            if user is None:
                return None
            sub = await session.scalar(
                select(Subscription).where(Subscription.user_id == user.tg_id).order_by(Subscription.id).limit(1)
            )
            if sub is not None and sub.sub_token is not None:
                return None  # a rotated subscription token revokes the old user URL too
        if user is None:
            return None
        snapshot = _Snapshot(user=user, sub=sub)
        if sub is None:
            return snapshot
        locations = {loc.id: loc for loc in await session.scalars(select(Location))}
        if location_id is not None:
            selected_location = locations.get(location_id)
            if selected_location is None or not selected_location.is_active:
                return None
        snapshot.xui = await _load_xui_rows(session, locations)
        snapshot.remnawave = await _load_remnawave_rows(session, sub.id, locations)
        # Decided before a location filter narrows the rows, so a per-location URL cannot skip the limit.
        snapshot.counts_devices = (bot_counts_xui and bool(snapshot.xui)) or any(
            row.panel.kind in BOT_COUNTED_KINDS for row in snapshot.remnawave
        )
        if location_id is not None:
            snapshot.xui = [row for row in snapshot.xui if row.location and row.location.id == location_id]
            snapshot.remnawave = [row for row in snapshot.remnawave if row.location and row.location.id == location_id]
        return snapshot


async def _load_xui_rows(session: AsyncSession, locations: Mapping[int, Location]) -> list[_XuiRow]:
    stmt = (
        select(Endpoint, Inbound)
        .join(Inbound, Endpoint.inbound_id == Inbound.id)
        .join(Panel, Inbound.panel_id == Panel.id)
        .where(
            Endpoint.is_active.is_(True),
            Endpoint.auto_hidden.is_(False),
            Inbound.is_active.is_(True),
            Panel.is_active.is_(True),
            Panel.kind == PanelKind.XUI,
        )
    )
    rows: list[_XuiRow] = []
    for endpoint, inbound in await session.execute(stmt):
        location_id = endpoint.location_id or inbound.location_id
        location = locations.get(location_id) if location_id else None
        if location is not None and not location.is_active:
            continue
        rows.append(_XuiRow(endpoint, inbound, location))
    return rows


async def _load_remnawave_rows(
    session: AsyncSession, subscription_id: int, locations: Mapping[int, Location]
) -> list[_RemnawaveRow]:
    stmt = (
        select(Provision, Panel)
        .join(Panel, Provision.panel_id == Panel.id)
        .where(
            Provision.subscription_id == subscription_id,
            Provision.actual_state != ProvisionState.ABSENT,
            Provision.remote_email.is_not(None),
            Panel.is_active.is_(True),
            Panel.kind.in_((PanelKind.REMNAWAVE, PanelKind.MARZBAN, PanelKind.PASARGUARD, PanelKind.MARZNESHIN)),
        )
        .order_by(Panel.id)
    )
    rows: list[_RemnawaveRow] = []
    for provision, panel in await session.execute(stmt):
        location = locations.get(_panel_location_id(panel) or 0)
        if location is not None and not location.is_active:
            continue
        rows.append(_RemnawaveRow(provision, panel, location))
    return rows


def _panel_location_id(panel: Panel) -> int | None:
    try:
        options = json.loads(panel.options) if panel.options else {}
    except json.JSONDecodeError:
        return None
    value = options.get("location_id") if isinstance(options, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ---------------------------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------------------------
async def render(
    session_factory: SessionFactory,
    registry: BackendRegistry,
    settings: Settings,
    sub_token: str,
    *,
    profile: Profile = "all",
    client_headers: Mapping[str, str] | None = None,
    client_ip: str | None = None,
    now: datetime | None = None,
    secret_box: SecretBox | None = None,
    location_id: int | None = None,
    server_id: str | None = None,
    track_devices: bool = False,
) -> RenderedSubscription | None:
    """Render the subscription for ``sub_token``; ``None`` for an unknown token (HTTP 404).

    ``profile``: ``"standard"`` / ``"whitelist"`` restrict entries to that endpoint profile; ``"all"``
    includes both unless ``SUB_WHITELIST_SEPARATE_URL`` moves LTE entries to their own URL.
    ``client_headers`` (the client's request headers) select the routing-header dialect and are forwarded
    to Remnawave, with ``client_ip`` (the client's address) for its device accounting. ``secret_box`` encrypts
    the Remnawave links saved on provisions. ``track_devices`` is set for requests made by a client app: the
    device limit is then enforced for servers that cannot count devices themselves (see :mod:`app.services.hwid`).
    """
    if not sub_token:
        return None
    snapshot = await _load_snapshot(
        session_factory, sub_token, location_id, bot_counts_xui=settings.xui_device_limit == "hwid"
    )
    if snapshot is None:
        return None
    now = now or datetime.now(UTC)
    headers_in = {k.lower(): v for k, v in (client_headers or {}).items()}
    state = access_state(snapshot.user, snapshot.sub, now)
    sub = snapshot.sub
    unavailable = False
    notice: dict[str, str] = {}
    verdict = await _device_verdict(
        session_factory, settings, secret_box, snapshot, headers_in, state, track_devices, now
    )
    if verdict is not None and verdict != hwid.DeviceVerdict.ALLOWED:
        links, notice = _device_notice(verdict, snapshot.user.language, sub.device_limit if sub else 0)
        usage_bytes = (0, 0)
    elif state != AccessState.ACTIVE or sub is None:
        links = [placeholder_link(state, snapshot.user.language)]
        usage_bytes = (0, 0)
    else:
        profiles = _wanted_profiles(profile, settings)
        xui_entries = _xui_entries(snapshot.xui, sub, settings, profiles)
        remnawave_task = _remnawave_entries(
            session_factory, registry, settings, snapshot.remnawave, sub, headers_in, profiles, secret_box, client_ip
        )
        usage_task = usage(session_factory, registry, sub, settings)
        (remnawave_entries, failed), usage_bytes = await asyncio.gather(remnawave_task, usage_task)
        links = [link for _, link in sorted(xui_entries + remnawave_entries, key=lambda e: e[0])]
        if server_id is not None:
            links = [link for link in links if server_key(link) == server_id]
        unavailable = failed and not links
    headers = build_headers(settings, snapshot.user, sub, state, profile, usage_bytes, headers_in)
    headers.update(notice)
    body = base64.b64encode("\n".join(links).encode()).decode("ascii")
    media_type = MEDIA_TYPE
    if subscription_json.client_wants_json(settings, detect_client(headers_in)) and not unavailable:
        configs = subscription_json.build_configs(links, settings, snapshot.user.language)
        if configs:
            body, media_type = subscription_json.render_body(configs), subscription_json.MEDIA_TYPE
    return RenderedSubscription(
        body=body, headers=headers, links=links, state=state, media_type=media_type, servers_unavailable=unavailable
    )


async def _device_verdict(
    session_factory: SessionFactory,
    settings: Settings,
    secret_box: SecretBox | None,
    snapshot: _Snapshot,
    headers: Mapping[str, str],
    state: AccessState,
    track_devices: bool,
    now: datetime,
) -> hwid.DeviceVerdict | None:
    """The bot-side device limit decision; None when it does not apply to this request."""
    sub = snapshot.sub
    if not track_devices or state != AccessState.ACTIVE or sub is None or sub.device_limit <= 0:
        return None
    if not snapshot.counts_devices:
        return None
    box = secret_box
    if box is None and settings.secret_key is not None:
        box = SecretBox(settings.secret_key.get_secret_value())
    if box is None:
        return None
    try:
        check = await hwid.check_device(
            session_factory, box, sub.id, sub.device_limit, headers, require_hwid=settings.hwid_required, now=now
        )
    except Exception as exc:  # a failing device table must not take the subscription down
        logger.warning("subscription %s: device check failed: %s", sub.id, exc.__class__.__name__)
        return None
    return check.verdict


def _device_notice(verdict: hwid.DeviceVerdict, language: str | None, limit: int) -> tuple[list[str], dict[str, str]]:
    """The single placeholder entry and headers that tell the user why no servers are listed."""
    lang = language if language in DEVICE_LIMIT_NAMES else "ru"
    if verdict == hwid.DeviceVerdict.UNSUPPORTED:
        name, announce, flag = HWID_UNSUPPORTED_NAMES[lang], HWID_UNSUPPORTED_ANNOUNCES[lang], "x-hwid-not-supported"
    else:
        name = DEVICE_LIMIT_NAMES[lang].format(limit=limit)
        announce = DEVICE_LIMIT_ANNOUNCES[lang].format(limit=limit)
        flag = "x-hwid-max-devices-reached"
    link = build_vless(PLACEHOLDER_UUID, "0.0.0.0", 1, {"type": "tcp", "security": "none"}, name)
    return [link], {flag: "true", "announce": b64_header(truncate(announce, ANNOUNCE_MAX_LEN))}


def server_key(uri: str) -> str:
    """A stable public identifier for the address and port of one connection endpoint."""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(uri)
        address = f"{parsed.hostname or ''}:{parsed.port or 0}"
    except ValueError:
        address = uri.split("#", 1)[0]
    return hashlib.sha256(address.encode()).hexdigest()[:16]


def access_state(user: User, sub: Subscription | None, now: datetime) -> AccessState:
    """Whether the user may connect right now (``expires_at`` is the single source of truth)."""
    if user.is_banned:
        return AccessState.DISABLED
    if sub is None:
        return AccessState.NONE
    if sub.status == SubscriptionStatus.DISABLED:
        return AccessState.DISABLED
    if sub.status == SubscriptionStatus.EXPIRED or sub.expires_at <= now:
        return AccessState.EXPIRED
    return AccessState.ACTIVE


def placeholder_link(state: AccessState, language: str | None = "ru") -> str:
    """A link to ``0.0.0.0:1`` whose name tells the user why the list is empty."""
    names = PLACEHOLDER_NAMES.get(state, PLACEHOLDER_NAMES[AccessState.EXPIRED])
    name = names.get(language or "ru", names["ru"])
    return build_vless(PLACEHOLDER_UUID, "0.0.0.0", 1, {"type": "tcp", "security": "none"}, name)


def _wanted_profiles(profile: Profile, settings: Settings) -> frozenset[EndpointProfile]:
    if profile == "standard" or (profile == "all" and settings.sub_whitelist_separate_url):
        return frozenset({EndpointProfile.STANDARD})
    if profile == "whitelist":
        return frozenset({EndpointProfile.WHITELIST})
    return frozenset(EndpointProfile)


# Sort key: (has no location, location sort_order, location id, profile rank, source, order...)
SortKey = tuple[int, ...]


def _location_key(location: Location | None) -> tuple[int, int, int]:
    return (1, 0, 0) if location is None else (0, location.sort_order, location.id)


def _profile_rank(profile: EndpointProfile) -> int:
    return 0 if profile == EndpointProfile.STANDARD else 1


def _xui_entries(
    rows: Sequence[_XuiRow], sub: Subscription, settings: Settings, profiles: frozenset[EndpointProfile]
) -> list[tuple[SortKey, str]]:
    keyed: list[tuple[SortKey, _XuiRow]] = []
    for row in rows:
        if row.endpoint.profile not in profiles:
            continue
        ep, inbound = row.endpoint, row.inbound
        # Shadowsocks entries follow every VLESS / panel entry of their location.
        rank = _profile_rank(ep.profile) + (2 if _is_shadowsocks(inbound) else 0)
        key: SortKey = (*_location_key(row.location), rank, 0, ep.sort_order, inbound.sort_order, ep.id)
        keyed.append((key, row))
    keyed.sort(key=lambda item: item[0])

    entries: list[tuple[SortKey, str]] = []
    seen: dict[str, int] = {}
    for sort_key, row in keyed:
        link = _xui_link(row, sub, settings, seen)
        if link is not None:
            entries.append((sort_key, link))
    return entries


def _is_shadowsocks(inbound: Inbound) -> bool:
    params = _inbound_params(inbound)
    return params is not None and shadowsocks.is_shadowsocks(params)


def _xui_link(row: _XuiRow, sub: Subscription, settings: Settings, seen: dict[str, int]) -> str | None:
    ep, inbound = row.endpoint, row.inbound
    params = _inbound_params(inbound)
    if params is None:
        return None
    is_ss = shadowsocks.is_shadowsocks(params)
    try:
        if is_ss:
            address, port, link_params = effective_ss(params, ep)
        else:
            if settings.reality_compact_hello:
                params = compact_reality_hello(params)
            address, port, link_params = effective_params(params, ep)
    except ValueError as exc:
        logger.warning("Skipping endpoint %s: %s", ep.id, exc)
        return None
    tag = SS_SUFFIX if is_ss else ""
    name = entry_name(row.location, ep, settings, protocol_tag=tag)
    seen[name] = seen.get(name, 0) + 1
    if seen[name] > 1:
        name = entry_name(row.location, ep, settings, ordinal=seen[name], protocol_tag=tag)
    description = truncate(ep.description, DESCRIPTION_MAX_LEN) if ep.description else None
    if is_ss:
        method = str(params.get(shadowsocks.PARAM_METHOD) or "")
        server_key = str(params.get(shadowsocks.PARAM_PASSWORD) or "")
        if not shadowsocks.key_bytes(method) or not server_key:
            return None
        client_key = shadowsocks.derive_client_key(sub.client_uuid, inbound.id, method)
        return build_ss(method, server_key, client_key, address, port, link_params, name, description)
    return build_vless(sub.client_uuid, address, port, link_params, name, description, inbound.flow or "")


def _inbound_params(inbound: Inbound) -> dict[str, Any] | None:
    if not inbound.params:
        logger.warning("Inbound %s has no cached link params yet", inbound.id)
        return None
    try:
        params = json.loads(inbound.params)
    except json.JSONDecodeError:
        logger.warning("Inbound %s has malformed cached params", inbound.id)
        return None
    if not isinstance(params, dict):
        return None
    protocol = str(params.get("protocol") or "vless").lower()
    if protocol == "vless" or (protocol == shadowsocks.SHADOWSOCKS and shadowsocks.blocked_reason(params) is None):
        return params
    return None


# ---------------------------------------------------------------------------------------------
# Remnawave
# ---------------------------------------------------------------------------------------------
async def _remnawave_entries(
    session_factory: SessionFactory,
    registry: BackendRegistry,
    settings: Settings,
    rows: Sequence[_RemnawaveRow],
    sub: Subscription,
    client_headers: Mapping[str, str],
    profiles: frozenset[EndpointProfile],
    secret_box: SecretBox | None,
    client_ip: str | None = None,
) -> tuple[list[tuple[SortKey, str]], bool]:
    """Sorted entries of every Remnawave provision, and whether some panel failed with nothing to serve."""
    if EndpointProfile.STANDARD not in profiles or not rows:
        return [], False  # Remnawave hosts have no whitelist profile of ours
    results = await asyncio.gather(
        *(
            _remnawave_links(session_factory, registry, settings, row, sub, client_headers, secret_box, client_ip)
            for row in rows
        )
    )
    entries: list[tuple[SortKey, str]] = []
    failed = False
    for row, (links, row_failed) in zip(rows, results, strict=True):
        failed = failed or row_failed
        for index, link in enumerate(links):
            key = (*_location_key(row.location), _profile_rank(EndpointProfile.STANDARD), 1, row.panel.id, index)
            entries.append((key, link))
    return entries, failed


async def _remnawave_links(
    session_factory: SessionFactory,
    registry: BackendRegistry,
    settings: Settings,
    row: _RemnawaveRow,
    sub: Subscription,
    client_headers: Mapping[str, str],
    secret_box: SecretBox | None,
    client_ip: str | None = None,
) -> tuple[list[str], bool]:
    """``(links, failed with nothing to serve)`` for one provision.

    On a panel failure the freshest known links are served: the in-process copy for the current revision,
    else the ones saved on the provision (only if they were fetched for the current client key). With a device
    limit the panel's subscription is fetched instead (see :func:`_remnawave_device_links`).
    """
    fetch = await _device_checked_fetch(registry, row, sub)
    if fetch is not None:
        return await _remnawave_device_links(fetch, settings, row, sub, client_headers, client_ip)
    cache_key = (sub.id, sub.revision, row.panel.id)
    cached = _remnawave_links_cache.get(cache_key)
    if cached is not None:
        return list(cached), False
    saved = _load_saved_links(row.provision, sub, secret_box)
    if is_panel_down(row.panel, settings) or _remnawave_failures.get(cache_key) is not None:
        # Health checks say it is unreachable, or it has just failed: don't wait for another timeout.
        return _fallback_links(cache_key, saved)
    account = _remote_account(row.provision, sub)
    try:
        async with asyncio.timeout(PANEL_CALL_TIMEOUT_SEC):
            backend = await registry.get(row.panel.id)
            links = [link.strip() for link in await backend.get_links(account, client_headers) if link.strip()]
    except Exception as exc:  # a broken panel must not break the subscription endpoint
        logger.warning("Remnawave panel %s: get_links failed: %s", row.panel.id, exc.__class__.__name__)
        _remnawave_failures.set(cache_key, True, PANEL_FAILURE_TTL_SEC)
        return _fallback_links(cache_key, saved)
    _remnawave_links_cache.set(cache_key, tuple(links), REMNAWAVE_LINKS_TTL_SEC)
    if saved != links:
        await _save_links(session_factory, row.provision.id, sub.client_uuid, links, secret_box)
    return links, False


SubscriptionFetch = Callable[[str, Mapping[str, str]], Awaitable[tuple[str, dict[str, str]]]]


async def _device_checked_fetch(
    registry: BackendRegistry, row: _RemnawaveRow, sub: Subscription
) -> SubscriptionFetch | None:
    """The panel's public subscription fetcher when it must count devices; None for the connection-keys path."""
    if row.panel.kind != PanelKind.REMNAWAVE or sub.device_limit <= 0 or not row.provision.remote_short_id:
        return None  # unlimited devices: nothing to count
    try:
        backend = await registry.get(row.panel.id)  # a local lookup, no panel call
    except Exception:
        return None  # the connection-keys path reports the failure
    fetch = getattr(backend, "fetch_subscription", None)
    return fetch if callable(fetch) else None


async def _remnawave_device_links(
    fetch: SubscriptionFetch,
    settings: Settings,
    row: _RemnawaveRow,
    sub: Subscription,
    client_headers: Mapping[str, str],
    client_ip: str | None,
) -> tuple[list[str], bool]:
    """Links from the panel's own subscription, fetched as the client (``x-hwid``...) so the device is counted.

    Cached per device. On a failure only this device's earlier answer is served; with none the provision counts
    as failed (the endpoint may answer 503 and the client keeps its list): links that another device fetched
    are never handed out, or any number of devices could join while the panel is unreachable.
    """
    hwid = client_headers.get("x-hwid", "").strip()
    cache_key = (sub.id, sub.revision, row.panel.id, "device", hwid)
    failure_key = (sub.id, sub.revision, row.panel.id)
    cached = _remnawave_links_cache.get(cache_key)
    if cached is not None:
        return list(cached), False
    if is_panel_down(row.panel, settings) or _remnawave_failures.get(failure_key) is not None:
        return _device_fallback(cache_key)
    short_id = row.provision.remote_short_id or ""
    try:
        async with asyncio.timeout(PANEL_CALL_TIMEOUT_SEC):
            body, headers = await fetch(short_id, device_headers(client_headers, client_ip))
        links = panel_subscription_links(body, headers)
    except Exception as exc:  # a broken panel must not break the subscription endpoint
        logger.warning("Remnawave panel %s: subscription fetch failed: %s", row.panel.id, exc.__class__.__name__)
        _remnawave_failures.set(failure_key, True, PANEL_FAILURE_TTL_SEC)
        return _device_fallback(cache_key)
    _remnawave_links_cache.set(cache_key, tuple(links), REMNAWAVE_LINKS_TTL_SEC)
    return links, False


def _device_fallback(cache_key: Hashable) -> tuple[list[str], bool]:
    stale = _remnawave_links_cache.get_stale(cache_key)
    return (list(stale), False) if stale is not None else ([], True)


def device_headers(client_headers: Mapping[str, str], client_ip: str | None) -> dict[str, str]:
    """Headers to fetch the panel's subscription as the client: device identification and its address."""
    headers = {name: client_headers[name] for name in DEVICE_HEADERS if client_headers.get(name)}
    agent = client_headers.get("user-agent", "")
    if agent and not _CONFIG_FORMAT_AGENTS.search(agent):
        headers["user-agent"] = agent
    if client_ip:
        headers["x-remnawave-real-ip"] = client_ip
    return headers


def panel_subscription_links(body: str, headers: Mapping[str, str]) -> list[str]:
    """Share links of a panel subscription (base64 or plain list). Raises PanelError for other formats.

    Placeholder entries are dropped, unless they are all there is and the panel marked its answer as a device
    notice (limit reached, app without HWID support): then they tell the user why there are no servers.
    """
    text = body.strip()
    if text and "://" not in text:
        compact = "".join(text.split()).replace("-", "+").replace("_", "/")
        try:
            text = base64.b64decode(compact + "=" * (-len(compact) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise PanelError("the panel subscription is not a list of links") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    links = [line for line in lines if "://" in line]
    if lines and not links:
        raise PanelError("the panel subscription is not a list of links")
    servers = [link for link in links if not is_placeholder_link(link)]
    if servers:
        return servers
    notice = any(headers.get(name, "").strip().lower() == "true" for name in DEVICE_NOTICE_HEADERS)
    return links if notice else []


def _fallback_links(cache_key: Hashable, saved: list[str] | None) -> tuple[list[str], bool]:
    stale = _remnawave_links_cache.get_stale(cache_key)
    if stale is not None:
        return list(stale), False
    if saved is not None:
        return saved, False
    return [], True


def _load_saved_links(provision: Provision, sub: Subscription, secret_box: SecretBox | None) -> list[str] | None:
    """Links saved on the provision; None if missing, unreadable or fetched for another client key."""
    raw = provision.links_cache
    if not raw:
        return None
    try:
        data = json.loads(secret_box.decrypt(raw) if secret_box is not None else raw)
    except (DecryptionError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("uuid") != sub.client_uuid:
        return None
    links = data.get("links")
    if not isinstance(links, list) or not all(isinstance(link, str) for link in links):
        return None
    return list(links)


async def _save_links(
    session_factory: SessionFactory,
    provision_id: int,
    client_uuid: str,
    links: list[str],
    secret_box: SecretBox | None,
) -> None:
    """Remember the last good links on the provision (a short transaction of its own; failures are logged)."""
    text = json.dumps({"uuid": client_uuid, "links": links}, separators=(",", ":"))
    value = secret_box.encrypt(text) if secret_box is not None else text
    try:
        async with session_factory.begin() as session:
            await session.execute(update(Provision).where(Provision.id == provision_id).values(links_cache=value))
    except Exception as exc:  # the links were fetched: failing to remember them must not fail the response
        logger.warning("provision %s: saving links failed: %s", provision_id, exc.__class__.__name__)


def _remote_account(provision: Provision, sub: Subscription) -> RemoteAccount:
    return RemoteAccount(
        email=provision.remote_email or "",
        uuid=sub.client_uuid,
        enabled=provision.actual_state == ProvisionState.ENABLED,
        expires_at=sub.expires_at,
        device_limit=sub.device_limit,
        traffic_limit_bytes=sub.traffic_limit_bytes
        if sub.traffic_limit_bytes is not None
        else sub.traffic_limit_gb * GIB,
        remote_id=provision.remote_id,
        short_id=provision.remote_short_id,
    )


# ---------------------------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------------------------
async def usage(
    session_factory: SessionFactory,
    registry: BackendRegistry,
    sub: Subscription,
    settings: Settings | None = None,
) -> tuple[int, int]:
    """``(upload, download)`` bytes summed over every panel account of ``sub`` (cached 5 minutes).

    Panels report a single counter per account, so it is returned as download (upload is 0). Each panel
    gets ``USAGE_CALL_TIMEOUT_SEC``; panels that fail are skipped, and with ``settings`` panels marked down
    by health checks are not queried at all. If every queried panel fails, the last known value (or zero)
    is served and remembered for ``USAGE_FAILURE_TTL_SEC`` so dead panels are not retried per request.
    """
    cached = _usage_cache.get(sub.id)
    if cached is not None:
        return cached
    targets = await _usage_targets(session_factory, sub.id, settings)
    if not targets:
        return 0, 0
    results = await asyncio.gather(*(_account_used_bytes(registry, *target) for target in targets))
    known = [value for value in results if value is not None]
    if not known:
        stale = _usage_cache.get_stale(sub.id)
        fallback = stale if stale is not None else (0, 0)
        _usage_cache.set(sub.id, fallback, USAGE_FAILURE_TTL_SEC)  # negative cache
        return fallback
    value = (0, sum(known))
    _usage_cache.set(sub.id, value, USAGE_TTL_SEC)
    return value


async def _usage_targets(
    session_factory: SessionFactory, subscription_id: int, settings: Settings | None = None
) -> list[tuple[int, int | None, str]]:
    """``(panel_id, target, email)`` per existing account: target = 3x-ui inbound remote id, None for Remnawave.

    With ``settings``, accounts on panels marked down by health checks are left out.
    """
    stmt = (
        select(Panel, Inbound.remote_id, Provision.remote_email)
        .join(Panel, Provision.panel_id == Panel.id)
        .outerjoin(Inbound, Provision.inbound_id == Inbound.id)
        .where(
            Provision.subscription_id == subscription_id,
            Provision.actual_state != ProvisionState.ABSENT,
            Provision.remote_email.is_not(None),
            Panel.is_active.is_(True),
        )
        .order_by(Provision.id)
    )
    async with read_session(session_factory) as session:
        rows = (await session.execute(stmt)).all()
    targets: list[tuple[int, int | None, str]] = []
    for panel, remote_inbound_id, email in rows:
        if panel.kind == PanelKind.XUI and remote_inbound_id is None:
            continue
        if settings is not None and is_panel_down(panel, settings):
            continue
        targets.append((panel.id, remote_inbound_id if panel.kind == PanelKind.XUI else None, email or ""))
    return targets


async def _account_used_bytes(registry: BackendRegistry, panel_id: int, target: int | None, email: str) -> int | None:
    try:
        async with asyncio.timeout(USAGE_CALL_TIMEOUT_SEC):
            backend = await registry.get(panel_id)
            used = await backend.get_used_bytes(target, email)  # counters only: no full inbound download
    except Exception as exc:  # usage is informational; never fail the subscription because of it
        logger.info("Usage unavailable from panel %s: %s", panel_id, exc.__class__.__name__)
        return None
    return max(used, 0) if used is not None else 0


# ---------------------------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------------------------
def build_headers(
    settings: Settings,
    user: User,
    sub: Subscription | None,
    state: AccessState,
    profile: Profile,
    usage_bytes: tuple[int, int],
    client_headers: Mapping[str, str],
) -> dict[str, str]:
    """Response headers understood by Happ, INCY, v2RayTun and Hiddify. ``client_headers`` keys are lowercase."""
    title = profile_title(settings, profile)
    headers: dict[str, str] = {
        "profile-title": b64_header(title),
        "profile-update-interval": str(max(settings.sub_update_interval_hours, 1)),
        "content-disposition": content_disposition(title),
    }
    if sub is not None:
        headers["subscription-userinfo"] = subscription_userinfo(sub, usage_bytes)
    for name, value in (("support-url", settings.support_url), ("profile-web-page-url", settings.channel_url)):
        if value:
            headers[name] = header_safe(value)
    announce = _announce_text(settings, user, state)
    if announce:
        headers["announce"] = b64_header(truncate(announce, ANNOUNCE_MAX_LEN))
    if state == AccessState.ACTIVE and settings.sub_routing_ru_direct:
        routing = routing_header(detect_client(client_headers), settings)
        if routing:
            headers["routing"] = routing
    if settings.sub_happ_network_tags:
        headers["network-filter"] = "true"  # v2RayTun: names with "LTE" are shown on mobile data only
    return headers


def profile_title(settings: Settings, profile: Profile) -> str:
    # Not settings.profile_title: its plain [:25] slice can split an emoji.
    title = (settings.sub_profile_title or settings.brand_name).strip() or "VPN"
    if profile == "whitelist":
        return truncate(title, PROFILE_TITLE_MAX_LEN - len(WHITELIST_TITLE_SUFFIX)) + WHITELIST_TITLE_SUFFIX
    return truncate(title, PROFILE_TITLE_MAX_LEN)


def subscription_userinfo(sub: Subscription, usage_bytes: tuple[int, int]) -> str:
    """``upload=..; download=..; total=..; expire=..`` with ``expire`` in Unix seconds; total 0 = unlimited."""
    upload, download = usage_bytes
    total = sub.traffic_limit_bytes if sub.traffic_limit_bytes is not None else max(sub.traffic_limit_gb, 0) * GIB
    expire = int(sub.expires_at.timestamp())
    return f"upload={upload}; download={download}; total={total}; expire={expire}"


def b64_header(text: str) -> str:
    """``base64:<standard base64 of UTF-8>``: the form all clients accept for non-ASCII values."""
    return "base64:" + base64.b64encode(text.encode()).decode("ascii")


def content_disposition(title: str) -> str:
    """Name fallback for Hiddify/INCY: ASCII ``filename`` plus RFC 5987 ``filename*``."""
    ascii_name = "".join(ch for ch in title if 32 <= ord(ch) < 127 and ch not in '"\\;').strip() or "subscription"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(title, safe='')}"


def header_safe(value: str) -> str:
    """Strip line breaks and percent-encode non-ASCII so the value is a valid HTTP header."""
    value = value.replace("\r", "").replace("\n", "").strip()
    return quote(value, safe=_PRINTABLE_ASCII)


def _announce_text(settings: Settings, user: User, state: AccessState) -> str | None:
    if state == AccessState.ACTIVE:
        return settings.sub_announce or None
    texts = PLACEHOLDER_ANNOUNCES[state]
    return texts.get(user.language or "ru", texts["ru"])


# ---------------------------------------------------------------------------------------------
# Routing profiles
# ---------------------------------------------------------------------------------------------
ClientKind = Literal["happ", "incy", "v2raytun", "other"]


def detect_client(client_headers: Mapping[str, str]) -> ClientKind:
    """Guess the client app from request headers (keys lowercase)."""
    agent = client_headers.get("user-agent", "").lower()
    if client_headers.get("x-client", "").lower() == "incy" or agent.startswith("incy"):
        return "incy"
    if "happ" in agent:
        return "happ"
    if "v2raytun" in agent:
        return "v2raytun"
    return "other"


def routing_header(client: ClientKind, settings: Settings) -> str | None:
    """``routing`` header with the "RU direct" profile in the client's dialect; ``None`` if unsupported."""
    if client in ("happ", "incy"):
        encoded = base64.b64encode(_compact(happ_routing_profile(settings)).encode()).decode("ascii")
        return f"{client}://routing/onadd/{encoded}"
    if client == "v2raytun":
        return base64.b64encode(_compact(v2raytun_routing_profile(settings)).encode()).decode("ascii")
    return None


def happ_routing_profile(settings: Settings) -> dict[str, Any]:
    """Happ/INCY routing profile: everything proxied except Russian sites/IPs and private networks."""
    return {
        "Name": truncate(f"{settings.brand_name} RU", 32),
        "GlobalProxy": "true",
        "LastUpdated": ROUTING_PROFILE_VERSION,
        "RemoteDNSType": "DoH",
        "RemoteDNSDomain": "https://cloudflare-dns.com/dns-query",
        "RemoteDNSIP": "1.1.1.1",
        "DomesticDNSType": "DoU",
        "DomesticDNSDomain": "",
        "DomesticDNSIP": "77.88.8.8",
        "Geoipurl": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
        "Geositeurl": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat",
        "DnsHosts": {"cloudflare-dns.com": "1.1.1.1"},
        "DirectSites": list(RU_DIRECT_SITES),
        "DirectIp": ["geoip:ru", *PRIVATE_IPS],
        "ProxySites": [],
        "ProxyIp": [],
        "BlockSites": [],
        "BlockIp": [],
        "DomainStrategy": "IPIfNonMatch",
        "FakeDNS": "false",
    }


def v2raytun_routing_profile(settings: Settings) -> dict[str, Any]:
    """v2RayTun routing JSON (the format its "share routing" export produces)."""
    return {
        "name": truncate(f"{settings.brand_name} RU", 32),
        "id": str(uuid_lib.uuid5(_ROUTING_NAMESPACE, "v2raytun-profile")).upper(),
        "domainStrategy": "IPIfNonMatch",
        "domainMatcher": "hybrid",
        "balancers": [],
        "rules": [
            {
                "__name__": "Direct Russia",
                "id": str(uuid_lib.uuid5(_ROUTING_NAMESPACE, "v2raytun-direct-ru")).upper(),
                "type": "field",
                "outboundTag": "direct",
                "domain": list(RU_DIRECT_SITES),
                "ip": ["geoip:ru", "geoip:private"],
            }
        ],
    }


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
