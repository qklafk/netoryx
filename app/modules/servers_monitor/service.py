"""Server monitoring: live status of the servers behind the panels and load alerts.

The panels' own reachability is watched by the health job; this module looks inside: CPU, memory, disk, the
proxy core (Xray) and Remnawave nodes. A problem is reported once it was seen on :data:`CONFIRM_CHECKS` checks in
a row (short spikes are ignored) and a "back to normal" note follows when it clears. The state survives restarts
(``kv`` key ``mon:<panel id>``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import KeyValue, Panel, PanelKind
from app.db.session import read_session
from app.services import kv
from app.services.panels.base import CoreRestarter, ServerMonitor, ServerStatus

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory
    from app.services.provisioning import PanelSource

log = logging.getLogger(__name__)

MONITORED_KINDS = (PanelKind.XUI, PanelKind.REMNAWAVE)
STATUS_TIMEOUT_SEC = 15.0
CONFIRM_CHECKS = 2
STATE_KEY_PREFIX = "mon:"


class NotSupported(Exception):
    """The panel cannot report server status (or restart its core)."""


@dataclass(frozen=True, slots=True)
class Thresholds:
    cpu: float = 90.0
    mem: float = 90.0
    disk: float = 90.0


def thresholds_from(settings: Settings) -> Thresholds:
    """Alert thresholds in percent (``MONITOR_CPU/RAM/DISK_ALERT_PERCENT``)."""
    return Thresholds(
        cpu=float(settings.monitor_cpu_alert_percent),
        mem=float(settings.monitor_ram_alert_percent),
        disk=float(settings.monitor_disk_alert_percent),
    )


def interval_from(settings: Settings) -> int:
    return settings.monitor_interval_min


def monitoring_enabled(settings: Settings) -> bool:
    return settings.monitor_alerts_enabled


async def monitored_panels(session_factory: SessionFactory, *, active_only: bool = False) -> list[Panel]:
    async with read_session(session_factory) as session:
        stmt = select(Panel).where(Panel.kind.in_(MONITORED_KINDS)).order_by(Panel.id)
        if active_only:
            stmt = stmt.where(Panel.is_active.is_(True))
        return list(await session.scalars(stmt))


async def get_panel(session_factory: SessionFactory, panel_id: int) -> Panel | None:
    async with read_session(session_factory) as session:
        panel = await session.get(Panel, panel_id)
    return panel if panel is not None and panel.kind in MONITORED_KINDS else None


async def fetch_status(registry: PanelSource, panel_id: int) -> ServerStatus:
    """Live status of the panel's server. Raises :class:`NotSupported`, ``PanelError``, ``LookupError``, timeouts."""
    backend = await registry.get(panel_id)
    if not isinstance(backend, ServerMonitor):
        raise NotSupported(f"panel {panel_id} does not report server status")
    async with asyncio.timeout(STATUS_TIMEOUT_SEC):
        return await backend.server_status()


async def can_restart(registry: PanelSource, panel_id: int) -> bool:
    try:
        return isinstance(await registry.get(panel_id), CoreRestarter)
    except Exception:
        return False


async def restart_core(registry: PanelSource, panel_id: int) -> None:
    backend = await registry.get(panel_id)
    if not isinstance(backend, CoreRestarter):
        raise NotSupported(f"panel {panel_id} cannot restart its core")
    async with asyncio.timeout(STATUS_TIMEOUT_SEC * 2):
        await backend.restart_core()


# --- alerts ----------------------------------------------------------------------------------------------------------


def problems(status: ServerStatus, limits: Thresholds) -> dict[str, dict[str, Any]]:
    """Problems seen in ``status``: key -> values for the alert text (``cpu``, ``mem``, ``disk``, ``core``,
    ``node:<name>``)."""
    found: dict[str, dict[str, Any]] = {}
    if status.cpu_percent is not None and status.cpu_percent >= limits.cpu:
        found["cpu"] = {"value": round(status.cpu_percent)}
    if (mem := status.mem_percent) is not None and mem >= limits.mem:
        found["mem"] = {"value": round(mem)}
    if (disk := status.disk_percent) is not None and disk >= limits.disk:
        found["disk"] = {"value": round(disk)}
    if status.core_running is False:
        found["core"] = {"state": status.core_state or "?", "error": (status.core_error or "")[:200]}
    for node in status.nodes_offline:
        found[f"node:{node.name}"] = {"node": node.name, "error": (node.message or "")[:200]}
    return found


@dataclass(slots=True)
class AlertState:
    """Persisted per panel: confirmed problems and how many checks in a row each candidate was seen."""

    active: dict[str, dict[str, Any]] = field(default_factory=dict)
    seen: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"active": self.active, "seen": self.seen}, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | None) -> AlertState:
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        active: dict[str, Any] = data["active"] if isinstance(data.get("active"), dict) else {}
        seen: dict[str, Any] = data["seen"] if isinstance(data.get("seen"), dict) else {}
        return cls(
            {str(k): v if isinstance(v, dict) else {} for k, v in active.items()},
            {str(k): int(v) for k, v in seen.items() if isinstance(v, int)},
        )


@dataclass(frozen=True, slots=True)
class Transition:
    key: str
    values: dict[str, Any]
    raised: bool  # True: new problem; False: back to normal


def advance(state: AlertState, current: dict[str, dict[str, Any]], confirm: int = CONFIRM_CHECKS) -> list[Transition]:
    """Feed one check into ``state`` (mutated); returns the alerts to send."""
    transitions: list[Transition] = []
    for key, values in current.items():
        if key in state.active:
            state.active[key] = values
            continue
        state.seen[key] = state.seen.get(key, 0) + 1
        if state.seen[key] >= max(1, confirm):
            state.active[key] = values
            state.seen.pop(key, None)
            transitions.append(Transition(key, values, raised=True))
    for key in [k for k in state.seen if k not in current]:
        del state.seen[key]
    for key in [k for k in state.active if k not in current]:
        transitions.append(Transition(key, state.active.pop(key), raised=False))
    return transitions


def state_key(panel_id: int) -> str:
    return f"{STATE_KEY_PREFIX}{panel_id}"


async def load_state(session_factory: SessionFactory, panel_id: int) -> AlertState:
    async with read_session(session_factory) as session:
        return AlertState.from_json(await kv.get(session, state_key(panel_id)))


async def save_state(session_factory: SessionFactory, panel_id: int, state: AlertState) -> None:
    async with session_factory.begin() as session:
        if state.active or state.seen:
            await kv.set(session, state_key(panel_id), state.to_json())
        else:
            await kv.delete(session, state_key(panel_id))


async def forget_missing(session_factory: SessionFactory, keep: Iterable[int]) -> None:
    """Drop alert states of panels that no longer exist or are no longer monitored."""
    keep_keys = {state_key(panel_id) for panel_id in keep}
    async with session_factory.begin() as session:
        keys = await session.scalars(
            select(KeyValue.key).where(KeyValue.key.startswith(STATE_KEY_PREFIX, autoescape=True))
        )
        for key in [k for k in keys if k not in keep_keys]:
            await kv.delete(session, key)
