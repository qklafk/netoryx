"""Xray template extras: make every active 3x-ui panel match ``ADBLOCK_ENABLED`` and the YouTube exit settings.

The panel's Xray template gets (or loses) the ad-blocking rule and the YouTube exit outbound / rule in one
read-modify-write; Xray restarts at most once and only when the template actually changed. The outcome per panel
is kept in ``kv`` (key ``adblock:<panel id>``) for the admin to show.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db.models import Panel, PanelKind
from app.db.session import read_session
from app.services import kv
from app.services.panels.base import AdBlocker, TemplateExtras
from app.services.panels.xray_template import YoutubeExit
from app.utils.time import utcnow
from app.utils.xray_outbound import LinkError, outbound_from_link

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.session import SessionFactory
    from app.services.provisioning import PanelSource

log = logging.getLogger(__name__)

STATE_KEY_PREFIX = "adblock:"
PANEL_TIMEOUT_SEC = 60.0


@dataclass(frozen=True, slots=True)
class PanelAdBlocking:
    """State of one panel: ``supported``, ``enabled`` (as last seen on the panel) and the last error."""

    panel_id: int
    supported: bool
    enabled: bool | None = None
    error: str | None = None
    checked_at: str | None = None
    youtube: bool | None = None  # the YouTube exit is in the template as wanted (None: not managed / unknown)
    sniffing_fixed: int = 0  # inbounds whose sniffing was switched on during the last run


def youtube_exit_from_settings(settings: Settings) -> YoutubeExit | None:
    """The wanted exit, or None when disabled, without a link, or with a link that does not parse."""
    if not settings.youtube_exit_enabled or settings.youtube_exit_link is None:
        return None
    link = settings.youtube_exit_link.get_secret_value().strip()
    if not link:
        return None
    try:
        outbound = outbound_from_link(link, "youtube-exit")
    except LinkError:
        return None
    return YoutubeExit(outbound=outbound, domains=tuple(settings.youtube_exit_domains))


def _key(panel_id: int) -> str:
    return f"{STATE_KEY_PREFIX}{panel_id}"


async def _save(session_factory: SessionFactory, state: PanelAdBlocking) -> None:
    payload = json.dumps(
        {
            "enabled": state.enabled,
            "error": state.error,
            "at": state.checked_at,
            "youtube": state.youtube,
            "sniffing": state.sniffing_fixed,
        }
    )
    async with session_factory.begin() as session:
        await kv.set(session, _key(state.panel_id), payload)


async def load_state(session_factory: SessionFactory, panel_id: int, *, supported: bool = True) -> PanelAdBlocking:
    """The last recorded outcome for the panel (unknown values stay None)."""
    async with read_session(session_factory) as session:
        raw = await kv.get(session, _key(panel_id))
    data: dict[str, object] = {}
    if raw:
        try:
            loaded = json.loads(raw)
            data = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            data = {}
    enabled = data.get("enabled")
    error = data.get("error")
    at = data.get("at")
    youtube = data.get("youtube")
    sniffing = data.get("sniffing")
    return PanelAdBlocking(
        panel_id=panel_id,
        supported=supported,
        enabled=enabled if isinstance(enabled, bool) else None,
        error=error if isinstance(error, str) and error else None,
        checked_at=at if isinstance(at, str) else None,
        youtube=youtube if isinstance(youtube, bool) else None,
        sniffing_fixed=sniffing if isinstance(sniffing, int) and not isinstance(sniffing, bool) else 0,
    )


async def _active_xui_panel_ids(session_factory: SessionFactory) -> list[int]:
    async with read_session(session_factory) as session:
        stmt = select(Panel.id).where(Panel.kind == PanelKind.XUI, Panel.is_active.is_(True)).order_by(Panel.id)
        return list(await session.scalars(stmt))


async def converge_panel(
    session_factory: SessionFactory,
    registry: PanelSource,
    panel_id: int,
    wanted: bool,
    youtube: YoutubeExit | None = None,
) -> PanelAdBlocking:
    """Bring one panel to the wanted state. Never raises for panel problems: they are recorded and returned."""
    try:
        backend = await registry.get(panel_id)
        sniffing_fixed = 0
        youtube_state: bool | None = None
        if isinstance(backend, TemplateExtras):
            async with asyncio.timeout(PANEL_TIMEOUT_SEC):
                changed = await backend.apply_template_extras(ads=wanted, youtube=youtube)
                if youtube is not None:
                    sniffing_fixed = await backend.ensure_sniffing()
            youtube_state = youtube is not None
        elif isinstance(backend, AdBlocker):
            async with asyncio.timeout(PANEL_TIMEOUT_SEC):
                changed = await backend.set_ad_blocking(wanted)
        else:
            return PanelAdBlocking(panel_id=panel_id, supported=False)
        if changed:
            log.info("xray template extras changed on panel %s (ads=%s, youtube=%s)", panel_id, wanted, youtube_state)
        state = PanelAdBlocking(
            panel_id=panel_id,
            supported=True,
            enabled=wanted,
            checked_at=utcnow().isoformat(),
            youtube=youtube_state,
            sniffing_fixed=sniffing_fixed,
        )
    except (Exception, TimeoutError) as exc:
        log.warning("xray template extras: panel %s: %s", panel_id, exc)
        previous = await load_state(session_factory, panel_id)
        state = PanelAdBlocking(
            panel_id=panel_id,
            supported=True,
            enabled=previous.enabled,
            error=(str(exc) or type(exc).__name__)[:300],
            checked_at=utcnow().isoformat(),
            youtube=previous.youtube,
            sniffing_fixed=previous.sniffing_fixed,
        )
    try:
        await _save(session_factory, state)
    except Exception:
        log.exception("xray template extras: could not store the state of panel %s", panel_id)
    return state


async def converge(
    session_factory: SessionFactory, registry: PanelSource, wanted: bool, youtube: YoutubeExit | None = None
) -> list[PanelAdBlocking]:
    """Make every active 3x-ui panel match the settings (network I/O happens outside DB transactions)."""
    panel_ids = await _active_xui_panel_ids(session_factory)
    return list(
        await asyncio.gather(*(converge_panel(session_factory, registry, pid, wanted, youtube) for pid in panel_ids))
    )
