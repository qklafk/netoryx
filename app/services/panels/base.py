"""Panel backend interface shared by 3x-ui, Remnawave, Marzban, Hiddify and PasarGuard implementations.

A *target* addresses where an account lives inside a panel:
* 3x-ui     -> the inbound's remote id (int); one client per (inbound, subscription)
* Remnawave -> ``None``; one user per subscription (servers are chosen by internal squads)
* Marzban   -> ``None``; one user per subscription (servers are chosen by inbound tags)
* Hiddify   -> ``None``; one user per subscription managed through its administrator API
* PasarGuard -> ``None``; one user per subscription managed through its administrator API

Every write is idempotent: ``upsert_account`` creates or fully updates; ``delete_account`` treats
"not found" as success. Implementations never raise on "already enabled/disabled"-style responses.

Optional capabilities are separate protocols checked with ``isinstance``: :class:`ServerMonitor`,
:class:`CoreRestarter`, and the device protocols of :mod:`app.services.devices` (``list_devices`` /
``delete_device``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.db.models import PanelKind

if TYPE_CHECKING:
    from app.services.panels.xray_template import YoutubeExit


class PanelError(Exception):
    """Any panel failure (network, unexpected response, API error)."""


class PanelAuthError(PanelError):
    """Bad credentials / expired token."""


class PanelUnsupportedError(PanelError):
    """Panel version is not supported."""


@dataclass(frozen=True, slots=True)
class AccountSpec:
    """Desired state of one account on one target."""

    email: str  # 3x-ui client email / Remnawave username (unique per panel)
    uuid: str  # VLESS id (an account with a ``password`` is identified by that instead)
    enabled: bool
    expires_at: datetime | None  # None = never expires
    device_limit: int  # 0 = unlimited (3x-ui limitIp / Remnawave hwidDeviceLimit)
    traffic_limit_bytes: int  # 0 = unlimited
    tg_id: int
    sub_id: str  # 3x-ui subId (16 [a-z0-9]); ignored by Remnawave
    comment: str = ""
    flow: str = ""  # 3x-ui flow, e.g. "xtls-rprx-vision"
    password: str = ""  # 3x-ui Shadowsocks client key; set -> a Shadowsocks client (no id / flow)


@dataclass(slots=True)
class RemoteAccount:
    email: str
    uuid: str
    enabled: bool
    expires_at: datetime | None
    device_limit: int
    traffic_limit_bytes: int
    used_bytes: int = 0
    remote_id: str | None = None  # Remnawave numeric id (as str)
    short_id: str | None = None  # Remnawave shortUuid
    subscription_url: str | None = None  # Remnawave subscriptionUrl
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InboundInfo:
    """3x-ui inbound with normalized link parameters."""

    remote_id: int
    remark: str
    protocol: str  # "vless", ...
    port: int
    enabled: bool
    # Normalized link params, e.g. {"type": "tcp", "security": "reality", "pbk": ..., "sni": ..., "sid": ...,
    # "fp": ..., "spx": ..., "path": ..., "host": ..., "mode": ..., "extra": {...}, "serviceName": ...,
    # "alpn": ..., "flow_allowed": bool}
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NodeStatus:
    """One node of a multi-node panel (Remnawave)."""

    name: str
    online: bool
    address: str = ""
    disabled: bool = False
    users_online: int | None = None
    uptime_sec: int | None = None
    traffic_used_bytes: int | None = None
    traffic_limit_bytes: int | None = None  # 0 or None = unlimited
    core_version: str | None = None
    country: str | None = None
    message: str | None = None  # last status message reported by the panel


@dataclass(frozen=True, slots=True)
class ServerStatus:
    """Load and health of the server behind a panel. Unknown values are ``None``.

    Sizes are bytes, speeds bytes per second, ``uptime_sec`` is the host uptime.
    """

    cpu_percent: float | None = None
    cpu_cores: int | None = None
    mem_used: int | None = None
    mem_total: int | None = None
    swap_used: int | None = None
    swap_total: int | None = None
    disk_used: int | None = None
    disk_total: int | None = None
    load: tuple[float, ...] = ()
    uptime_sec: int | None = None
    net_up_bps: int | None = None
    net_down_bps: int | None = None
    traffic_sent: int | None = None
    traffic_recv: int | None = None
    tcp_connections: int | None = None
    core_state: str | None = None  # Xray: "running" / "stop" / "error"
    core_version: str | None = None
    core_error: str | None = None
    online_users: int | None = None
    nodes: tuple[NodeStatus, ...] = ()

    @property
    def core_running(self) -> bool | None:
        if self.core_state is None:
            return None
        return self.core_state.lower() == "running"

    @property
    def mem_percent(self) -> float | None:
        return _percent(self.mem_used, self.mem_total)

    @property
    def swap_percent(self) -> float | None:
        return _percent(self.swap_used, self.swap_total)

    @property
    def disk_percent(self) -> float | None:
        return _percent(self.disk_used, self.disk_total)

    @property
    def nodes_offline(self) -> tuple[NodeStatus, ...]:
        """Enabled nodes that are not connected."""
        return tuple(n for n in self.nodes if not n.online and not n.disabled)


def _percent(used: int | None, total: int | None) -> float | None:
    if used is None or not total or total <= 0:
        return None
    return max(0.0, min(100.0, used * 100.0 / total))


@runtime_checkable
class ServerMonitor(Protocol):
    """Optional backend capability: report server load (3x-ui server status, Remnawave nodes)."""

    async def server_status(self) -> ServerStatus: ...


@runtime_checkable
class CoreRestarter(Protocol):
    """Optional backend capability: restart the proxy core (3x-ui Xray, Remnawave nodes)."""

    async def restart_core(self) -> None: ...


@runtime_checkable
class AdBlocker(Protocol):
    """Optional backend capability: block ad / tracker domains in the panel's own Xray routing (3x-ui)."""

    async def get_ad_blocking(self) -> bool: ...

    async def set_ad_blocking(self, on: bool) -> bool:
        """Make the panel match ``on``; returns True if its config changed (and the core was restarted)."""
        ...


@runtime_checkable
class TemplateExtras(Protocol):
    """Optional backend capability: ad blocking and the YouTube exit edited together in the panel's Xray template."""

    async def get_youtube_exit(self, wanted: YoutubeExit | None) -> bool:
        """Whether the template already matches ``wanted`` (``None``: none of our entries)."""
        ...

    async def apply_template_extras(self, *, ads: bool, youtube: YoutubeExit | None) -> bool:
        """One template write and at most one core restart; returns True if the template changed."""
        ...

    async def ensure_sniffing(self) -> int:
        """Make VLESS inbounds sniff domains (needed for domain routing); returns the number of inbounds changed."""
        ...


@runtime_checkable
class TgOnlyRouting(Protocol):
    """Optional backend capability: confine named clients to Telegram in the panel's Xray template (3x-ui)."""

    async def apply_tg_only(self, emails: Sequence[str]) -> bool:
        """Make the template route exactly ``emails`` as Telegram-only clients (none: remove the rules).

        Returns True if the template changed (and the core was restarted).
        """
        ...

    async def get_tg_only(self, emails: Sequence[str]) -> bool:
        """Whether the template already holds the rules for ``emails``."""
        ...

    async def ensure_sniffing(self) -> int: ...


@runtime_checkable
class PanelBackend(Protocol):
    kind: PanelKind

    async def ping(self) -> str:
        """Check connectivity/auth. Returns the panel version string. Raises PanelError."""
        ...

    async def close(self) -> None: ...

    async def list_inbounds(self) -> list[InboundInfo]:
        """3x-ui: all inbounds with link params. Remnawave: []."""
        ...

    async def list_accounts(self, target: int | None) -> list[RemoteAccount]:
        """All accounts on the target (for reconciliation / orphan detection)."""
        ...

    async def get_account(self, target: int | None, email: str) -> RemoteAccount | None: ...

    async def get_used_bytes(self, target: int | None, email: str) -> int | None:
        """Traffic used by the account (None if it is not on the target). Cheaper than :meth:`get_account`."""
        ...

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        """Create the account or bring every field to `spec` (full replace). Idempotent."""
        ...

    async def delete_account(self, target: int | None, email: str, uuid: str) -> None:
        """Delete; "not found" is success."""
        ...

    async def get_links(self, account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        """Remnawave: share links for the user (placeholders filtered). 3x-ui: [] (links are built locally)."""
        ...

    async def reset_devices(self, account: RemoteAccount) -> None:
        """Remnawave: delete all HWID devices. 3x-ui: clear client IPs. Best effort."""
        ...

    async def reset_traffic(self, target: int | None, email: str) -> None:
        """Zero the account's used traffic (a new paid period starts) and lift a traffic-limit block.

        "Not found" is success (nothing to reset). Raises PanelError on other failures.
        """
        ...
