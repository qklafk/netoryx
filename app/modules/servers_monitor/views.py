"""Screens and formatting of the server monitoring section."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton

from app.core.i18n import Translator
from app.core.ui import Screen
from app.db.models import Panel, PanelKind
from app.keyboards import Button, kb, nav
from app.modules.admin.common import to_admin
from app.services.panels.base import NodeStatus, ServerStatus

NAV = "adm_mon"  # Nav target: the servers list (admin menu entry)
BAR_CELLS = 10
_UNITS = ("b", "kb", "mb", "gb", "tb", "pb")


class MonCb(CallbackData, prefix="mon"):
    """``action``: show | restart | restart_ok; ``id``: panel id; ``n``: nonce of one restart confirmation."""

    action: str
    id: int
    n: str = ""


def decimal(value: float, lang: str) -> str:
    """``1.5`` / ``1,5``: one decimal below 10, whole numbers above."""
    text = f"{value:.1f}" if value < 10 else f"{value:.0f}"
    text = text.removesuffix(".0")
    return text.replace(".", ",") if lang == "ru" else text


def fmt_bytes(_: Translator, size: int | None) -> str:
    if size is None:
        return "—"
    value = float(max(0, size))
    unit = 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    return _(f"servers_monitor.unit.{_UNITS[unit]}", n=decimal(value, _.lang))


def fmt_speed(_: Translator, bps: int | None) -> str:
    return _("servers_monitor.per_second", value=fmt_bytes(_, bps)) if bps is not None else "—"


def fmt_uptime(_: Translator, seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return _("servers_monitor.uptime_days", d=days, h=hours)
    return _("servers_monitor.uptime_hours", h=hours, m=rest // 60)


def bar(percent: float | None) -> str:
    if percent is None:
        return ""
    filled = round(max(0.0, min(100.0, percent)) / 100 * BAR_CELLS)
    return "▰" * filled + "▱" * (BAR_CELLS - filled)


def _kind(panel: Panel) -> str:
    return "3x-ui" if panel.kind == PanelKind.XUI else "Remnawave"


def health_icon(panel: Panel, threshold: int) -> str:
    if not panel.is_active:
        return "⚪️"
    if panel.consecutive_failures >= max(1, threshold):
        return "🔴"
    if panel.consecutive_failures > 0 or panel.last_ok_at is None:
        return "🟡"
    return "🟢"


def list_screen(_: Translator, panels: Sequence[Panel], threshold: int) -> Screen:
    text = _("servers_monitor.title") + "\n\n"
    text += _("servers_monitor.intro") if panels else _("servers_monitor.empty")
    rows: list[list[InlineKeyboardButton | None]] = []
    for panel in panels:
        label = f"{health_icon(panel, threshold)} {panel.name} · {_kind(panel)}"
        rows.append([Button.cb(label[:64], MonCb(action="show", id=panel.id))])
    rows.append([nav(_("nav.refresh"), NAV)])
    rows.append([to_admin(_)])
    return Screen(text, kb(rows))


def _percent_line(_: Translator, key: str, used: int | None, total: int | None, percent: float | None) -> str | None:
    if total is None or used is None:
        return None
    pct = decimal(percent, _.lang) if percent is not None else "—"
    return _(key, used=fmt_bytes(_, used), total=fmt_bytes(_, total), percent=pct, bar=bar(percent))


def _node_line(_: Translator, node: NodeStatus) -> str:
    name = _.esc(node.name)
    if node.disabled:
        return _("servers_monitor.node_disabled", name=name)
    if not node.online:
        reason = f" — {_.esc(node.message[:100])}" if node.message else ""
        return _("servers_monitor.node_down", name=name, reason=reason)
    details = []
    if node.users_online is not None:
        details.append(_("servers_monitor.node_online", count=node.users_online))
    if node.traffic_used_bytes is not None:
        details.append(fmt_bytes(_, node.traffic_used_bytes))
    suffix = " · " + " · ".join(details) if details else ""
    return _("servers_monitor.node_up", name=name, details=suffix)


def status_text(_: Translator, panel: Panel, status: ServerStatus) -> str:
    version = f" {_.esc(panel.version)}" if panel.version else ""
    lines = [_("servers_monitor.server_title", name=_.esc(panel.name), kind=_kind(panel), version=version), ""]
    if status.core_state is not None:
        core_version = f" {_.esc(status.core_version)}" if status.core_version else ""
        if status.core_running:
            lines.append(_("servers_monitor.core_ok", version=core_version))
        else:
            error = f": {_.esc(status.core_error[:200])}" if status.core_error else ""
            lines.append(_("servers_monitor.core_down", state=_.esc(status.core_state), error=error))
    if status.cpu_percent is not None:
        cores = _("servers_monitor.cores", n=status.cpu_cores) if status.cpu_cores else ""
        lines.append(
            _(
                "servers_monitor.cpu",
                percent=decimal(status.cpu_percent, _.lang),
                cores=cores,
                bar=bar(status.cpu_percent),
            )
        )
    elif status.cpu_cores:
        lines.append(_("servers_monitor.cpu_cores_only", n=status.cpu_cores))
    if status.load:
        lines.append(_("servers_monitor.load", load=" / ".join(decimal(v, _.lang) for v in status.load[:3])))
    for line in (
        _percent_line(_, "servers_monitor.mem", status.mem_used, status.mem_total, status.mem_percent),
        _percent_line(_, "servers_monitor.swap", status.swap_used, status.swap_total, status.swap_percent)
        if status.swap_total
        else None,
        _percent_line(_, "servers_monitor.disk", status.disk_used, status.disk_total, status.disk_percent),
    ):
        if line:
            lines.append(line)
    if status.net_up_bps is not None or status.net_down_bps is not None:
        lines.append(
            _("servers_monitor.net", up=fmt_speed(_, status.net_up_bps), down=fmt_speed(_, status.net_down_bps))
        )
    if status.traffic_sent is not None or status.traffic_recv is not None:
        lines.append(
            _("servers_monitor.traffic", up=fmt_bytes(_, status.traffic_sent), down=fmt_bytes(_, status.traffic_recv))
        )
    if status.online_users is not None:
        lines.append(_("servers_monitor.online", count=status.online_users))
    if status.tcp_connections is not None:
        lines.append(_("servers_monitor.connections", count=status.tcp_connections))
    if status.uptime_sec is not None:
        lines.append(_("servers_monitor.uptime", value=fmt_uptime(_, status.uptime_sec)))
    if status.nodes:
        online = sum(1 for n in status.nodes if n.online)
        lines += ["", _("servers_monitor.nodes", online=online, total=len(status.nodes))]
        lines += [_node_line(_, node) for node in status.nodes]
    return "\n".join(lines)


def status_screen(_: Translator, panel: Panel, status: ServerStatus, *, can_restart: bool) -> Screen:
    rows: list[list[InlineKeyboardButton | None]] = [
        [
            Button.cb(_("nav.refresh"), MonCb(action="show", id=panel.id)),
            Button.cb(_(_restart_key(panel)), MonCb(action="restart", id=panel.id)) if can_restart else None,
        ],
        [nav(_("nav.back"), NAV)],
    ]
    return Screen(status_text(_, panel, status), kb(rows))


def error_screen(_: Translator, panel: Panel, key: str, error: str = "") -> Screen:
    text = _(key, name=_.esc(panel.name), error=_.esc(error[:300]))
    rows = [[Button.cb(_("nav.retry"), MonCb(action="show", id=panel.id))], [nav(_("nav.back"), NAV)]]
    return Screen(text, kb(rows))


def _restart_key(panel: Panel) -> str:
    return "servers_monitor.btn.restart_nodes" if panel.kind == PanelKind.REMNAWAVE else "servers_monitor.btn.restart"


def restart_confirm_screen(_: Translator, panel: Panel, nonce: str) -> Screen:
    key = (
        "servers_monitor.restart_nodes_confirm"
        if panel.kind == PanelKind.REMNAWAVE
        else "servers_monitor.restart_confirm"
    )
    rows = [
        [
            Button.cb(
                _("servers_monitor.btn.restart_ok"), MonCb(action="restart_ok", id=panel.id, n=nonce), style="danger"
            )
        ],
        [Button.cb(_("nav.cancel"), MonCb(action="show", id=panel.id))],
    ]
    return Screen(_(key, name=_.esc(panel.name)), kb(rows))
