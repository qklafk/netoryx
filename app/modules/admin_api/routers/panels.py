"""Servers: panels (3x-ui, Remnawave, telemt) with connection tests, health, sync, and their inbounds."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
from collections.abc import Awaitable
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.db.models import (
    Endpoint,
    Inbound,
    Location,
    Panel,
    PanelKind,
    Provision,
    ProvisionState,
    Subscription,
    TgProxyAccess,
    TgSocksAccess,
)
from app.db.session import read_session
from app.modules.admin_api.deps import Actor, bad_request, conflict, kick_reconcile, not_found, require
from app.modules.admin_api.schemas.common import Ok
from app.modules.admin_api.schemas.panels import (
    AdBlockingOut,
    InboundInfoOut,
    InboundOut,
    InboundUpdate,
    PanelDeleteOut,
    PanelIn,
    PanelOut,
    PanelTestIn,
    PanelTestOut,
    PanelUpdate,
    RealityInboundIn,
    RefreshIn,
    RefreshOut,
    ServerStatusOut,
    ShadowsocksInboundIn,
    SniCandidate,
    SniCheckIn,
    SniCheckOut,
    SquadOut,
    SyncNowOut,
)
from app.services import ad_blocking, provisioning
from app.services.panels import inventory, shadowsocks
from app.services.panels.base import (
    AdBlocker,
    CoreRestarter,
    InboundInfo,
    PanelBackend,
    PanelError,
    ServerMonitor,
    TemplateExtras,
)
from app.services.panels.registry import build_backend, panel_options
from app.services.panels.socks import SocksProxyBackend
from app.utils.time import utcnow
from app.web.deps import ReadDb

log = logging.getLogger(__name__)

router = APIRouter(tags=["admin: servers"])

Admin = Annotated[Actor, Depends(require(Permission.PANELS))]
Owner = Annotated[Actor, Depends(require(Permission.PANELS, Permission.SETTINGS_SECRETS))]

MASK = "***"
TEST_TIMEOUT_SEC = 30.0
DELETE_CONCURRENCY = 8
_MASKED_OPTION_MAPS = ("headers", "cookies")

# Well-known sites that are commonly used as Reality targets. Check reachability from the server itself.
REALITY_SNI_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("www.microsoft.com", "Microsoft"),
    ("www.apple.com", "Apple"),
    ("dl.google.com", "Google downloads"),
    ("www.samsung.com", "Samsung"),
    ("www.nvidia.com", "NVIDIA"),
    ("www.amd.com", "AMD"),
    ("www.intel.com", "Intel"),
    ("addons.mozilla.org", "Mozilla add-ons"),
    ("www.speedtest.net", "Speedtest"),
    ("cdn.jsdelivr.net", "jsDelivr CDN"),
)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def _masked_options(panel: Panel) -> dict[str, Any]:
    options = copy.deepcopy(panel_options(panel))
    for name in _MASKED_OPTION_MAPS:
        value = options.get(name)
        if isinstance(value, dict):
            options[name] = {k: MASK for k in value}
    return options


def _merge_options(current: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """New options; masked header/cookie values keep their stored value."""
    merged = copy.deepcopy(new)
    for name in _MASKED_OPTION_MAPS:
        incoming = merged.get(name)
        stored = current.get(name)
        if isinstance(incoming, dict) and isinstance(stored, dict):
            merged[name] = {k: (stored.get(k, "") if v == MASK else v) for k, v in incoming.items()}
    return merged


def _options_json(options: dict[str, Any]) -> str | None:
    if not options:
        return None
    try:
        return json.dumps(options, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise bad_request("bad_options", f"options must be JSON: {exc}") from None


async def _panel_out(session: AsyncSession, panel: Panel, settings: Any) -> PanelOut:
    inbounds = int(await session.scalar(select(func.count(Inbound.id)).where(Inbound.panel_id == panel.id)) or 0)
    accounts = int(
        await session.scalar(
            select(func.count(Provision.id)).where(
                Provision.panel_id == panel.id, Provision.actual_state != ProvisionState.ABSENT
            )
        )
        or 0
    )
    return PanelOut(
        id=panel.id,
        name=panel.name,
        kind=panel.kind,
        url=panel.url,
        username=panel.username,
        has_password=bool(panel.password),
        has_api_token=bool(panel.api_token),
        tls_verify=panel.tls_verify,
        options=_masked_options(panel),
        is_active=panel.is_active,
        healthy=panel.is_active and not provisioning.is_panel_down(panel, settings),
        version=panel.version,
        last_ok_at=panel.last_ok_at,
        last_error=panel.last_error,
        consecutive_failures=panel.consecutive_failures,
        created_at=panel.created_at,
        inbounds=inbounds,
        accounts=accounts,
    )


async def _get_panel(session: AsyncSession, panel_id: int) -> Panel:
    panel = await session.get(Panel, panel_id)
    if panel is None:
        raise not_found("Panel not found")
    return panel


def _encrypt(actor: Actor, value: str | None) -> str | None:
    return actor.ctx.secret_box.encrypt(value) if value else None


def _info_out(info: InboundInfo) -> InboundInfoOut:
    return InboundInfoOut(
        remote_id=info.remote_id,
        remark=info.remark,
        protocol=info.protocol,
        port=info.port,
        enabled=info.enabled,
        network=str(info.params.get("type") or "") or None,
        security=str(info.params.get("security") or "") or None,
    )


async def _probe(backend: PanelBackend) -> PanelTestOut:
    """Connect, read the version and what the panel offers (inbounds / squads). Never raises."""
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SEC):
            version = await backend.ping()
            inbounds = [_info_out(i) for i in await backend.list_inbounds()] if backend.kind == PanelKind.XUI else []
            squads: list[SquadOut] = []
            list_squads = getattr(backend, "list_squads", None)
            if backend.kind == PanelKind.REMNAWAVE and callable(list_squads):
                squads = [SquadOut(uuid=s.uuid, name=s.name) for s in await list_squads()]
    except TimeoutError:
        return PanelTestOut(ok=False, error=f"no answer within {TEST_TIMEOUT_SEC:.0f}s")
    except (PanelError, OSError, ValueError) as exc:
        return PanelTestOut(ok=False, error=str(exc)[:500] or type(exc).__name__)
    except Exception as exc:  # a broken panel must give a readable answer, not a 500
        log.warning("panel test failed unexpectedly: %s", exc, exc_info=True)
        return PanelTestOut(ok=False, error=f"{type(exc).__name__}: {str(exc)[:300]}")
    return PanelTestOut(ok=True, version=version, inbounds=inbounds, squads=squads)


async def _test_transient(actor: Actor, panel: Panel) -> PanelTestOut:
    try:
        backend = build_backend(panel, actor.ctx.secret_box, actor.ctx.settings)
    except PanelError as exc:
        return PanelTestOut(ok=False, error=str(exc)[:500])
    try:
        return await _probe(backend)
    finally:
        try:
            await backend.close()
        except Exception:
            log.debug("closing a test backend failed", exc_info=True)


# ---------------------------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------------------------
@router.get("/panels", response_model=list[PanelOut])
async def list_panels(actor: Admin, session: ReadDb) -> list[PanelOut]:
    """All panels with their health and account counts."""
    rows = (await session.scalars(select(Panel).order_by(Panel.id))).all()
    return [await _panel_out(session, p, actor.ctx.settings) for p in rows]


@router.get("/panels/{panel_id}", response_model=PanelOut)
async def get_panel(panel_id: int, actor: Admin, session: ReadDb) -> PanelOut:
    return await _panel_out(session, await _get_panel(session, panel_id), actor.ctx.settings)


@router.post("/panels/test", response_model=PanelTestOut)
async def test_panel(body: PanelTestIn, actor: Admin) -> PanelTestOut:
    """Try to connect with the given settings (nothing is saved). With ``panel_id`` stored values fill the gaps."""
    if body.panel_id is not None:
        async with read_session(actor.ctx.session_factory) as session:
            stored = await _get_panel(session, body.panel_id)
        if (body.password or body.api_token) and not actor.can(Permission.SETTINGS_SECRETS):
            raise bad_request("forbidden_secret", "Only the owner can try new credentials")
        stored_options = panel_options(stored)
        options = _merge_options(stored_options, body.options) if body.options is not None else stored_options
        # The stored password / token / headers are sent to whatever target is tested, so pointing them at another
        # address (or changing how they are sent) is owner-only, like editing these fields.
        retargeted = (
            (bool(body.url) and body.url != stored.url)
            or (body.username is not None and body.username != (stored.username or ""))
            or (body.tls_verify is not None and body.tls_verify != stored.tls_verify)
            or options != stored_options
        )
        if retargeted:
            actor.require(Permission.SETTINGS_SECRETS)
        panel = Panel(
            name=stored.name,
            kind=stored.kind,
            url=body.url or stored.url,
            username=body.username if body.username is not None else stored.username,
            password=_encrypt(actor, body.password) if body.password else stored.password,
            api_token=_encrypt(actor, body.api_token) if body.api_token else stored.api_token,
            tls_verify=stored.tls_verify if body.tls_verify is None else body.tls_verify,
            options=_options_json(options) if body.options is not None else stored.options,
        )
    else:
        if body.kind is None or not body.url:
            raise bad_request("missing_fields", "kind and url are required")
        # A free-form target makes this server connect anywhere (internal addresses included): owner-only, like adding.
        actor.require(Permission.SETTINGS_SECRETS)
        panel = Panel(
            name="test",
            kind=body.kind,
            url=body.url,
            username=body.username,
            password=_encrypt(actor, body.password),
            api_token=_encrypt(actor, body.api_token),
            tls_verify=True if body.tls_verify is None else body.tls_verify,
            options=_options_json(body.options or {}),
        )
    return await _test_transient(actor, panel)


@router.post("/panels", response_model=PanelOut, status_code=201)
async def create_panel(body: PanelIn, actor: Owner) -> PanelOut:
    """Add a panel (tested first unless ``test=false``); 3x-ui inbounds are imported right away."""
    ctx = actor.ctx
    draft = Panel(
        name=body.name.strip(),
        kind=body.kind,
        url=body.url,
        username=body.username or None,
        password=_encrypt(actor, body.password),
        api_token=_encrypt(actor, body.api_token),
        tls_verify=body.tls_verify,
        options=_options_json(body.options),
        is_active=body.is_active,
    )
    test: PanelTestOut | None = None
    if body.test:
        test = await _test_transient(actor, draft)
        if not test.ok:
            raise conflict("connection_failed", f"Could not connect to the panel: {test.error}")
        draft.version = test.version
    async with ctx.session_factory.begin() as session:
        if body.location_id is not None and await session.get(Location, body.location_id) is None:
            raise bad_request("unknown_location", "No such location")
        session.add(draft)
        await session.flush()
        await actor.audit(
            session, "panel.create", "panel", draft.id, name=draft.name, kind=draft.kind.value, url=draft.url
        )
        panel_id = draft.id
    if body.kind == PanelKind.XUI and body.import_inbounds and body.is_active:
        try:
            await inventory.refresh_inbounds(
                ctx.session_factory,
                ctx.panels,
                panel_id,
                activate_new=True,
                location_id=body.location_id,
                endpoint_address=body.endpoint_address,
            )
        except PanelError as exc:
            log.warning("panel %s: importing inbounds failed: %s", panel_id, exc)
    kick_reconcile(ctx)
    async with read_session(ctx.session_factory) as session:
        return await _panel_out(session, await _get_panel(session, panel_id), ctx.settings)


@router.patch("/panels/{panel_id}", response_model=PanelOut)
async def update_panel(panel_id: int, body: PanelUpdate, actor: Admin) -> PanelOut:
    """Edit a panel. Changing the URL or credentials, or turning off TLS checks, needs the owner role."""
    ctx = actor.ctx
    data = body.model_dump(exclude_unset=True)
    sensitive = {"url", "username", "password", "api_token", "options"} & set(data)
    if sensitive:
        actor.require(Permission.SETTINGS_SECRETS)
    async with ctx.session_factory.begin() as session:
        panel = await _get_panel(session, panel_id)
        if data.get("tls_verify") is False and panel.tls_verify:
            # Without certificate checks the stored credentials could be captured on the way to the panel.
            actor.require(Permission.SETTINGS_SECRETS)
        changed: list[str] = []
        for key in ("name", "url", "username", "tls_verify", "is_active"):
            if key in data and data[key] is not None and getattr(panel, key) != data[key]:
                setattr(panel, key, data[key].strip() if isinstance(data[key], str) else data[key])
                changed.append(key)
        for key in ("password", "api_token"):
            if key in data and data[key] is not None:
                setattr(panel, key, _encrypt(actor, data[key]))
                changed.append(key)
        if body.options is not None:
            panel.options = _options_json(_merge_options(panel_options(panel), body.options))
            changed.append("options")
        if not changed:
            raise bad_request("nothing_to_change", "Nothing to change")
        if "options" in changed or "is_active" in changed:
            await provisioning.mark_dirty(session, panel_id=panel_id)
        await actor.audit(session, "panel.update", "panel", panel_id, fields=changed)
    await ctx.panels.invalidate(panel_id)
    kick_reconcile(ctx)
    async with read_session(ctx.session_factory) as session:
        return await _panel_out(session, await _get_panel(session, panel_id), ctx.settings)


async def _remove_panel_accounts(actor: Actor, panel: Panel) -> tuple[int, list[str]]:
    """Delete our VPN accounts and Telegram proxy users from the panel; (removed, errors)."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        rows = (
            await session.execute(
                select(Provision.remote_email, Inbound.remote_id, Subscription.client_uuid)
                .join(Subscription, Subscription.id == Provision.subscription_id)
                .outerjoin(Inbound, Inbound.id == Provision.inbound_id)
                .where(Provision.panel_id == panel.id, Provision.actual_state != ProvisionState.ABSENT)
            )
        ).all()
        proxy_rows = (
            await session.execute(
                select(TgProxyAccess.remote_name, Inbound.remote_id)
                .select_from(TgProxyAccess)
                .outerjoin(Inbound, Inbound.id == TgProxyAccess.inbound_id)
                .where(TgProxyAccess.panel_id == panel.id, TgProxyAccess.actual_state != ProvisionState.ABSENT)
            )
        ).all()
        socks_rows = (
            await session.execute(
                select(TgSocksAccess.username, Inbound.remote_id)
                .select_from(TgSocksAccess)
                .join(Inbound, Inbound.id == TgSocksAccess.inbound_id)
                .where(TgSocksAccess.panel_id == panel.id, TgSocksAccess.actual_state != ProvisionState.ABSENT)
            )
        ).all()
    if not rows and not proxy_rows and not socks_rows:
        return 0, []
    try:
        backend = await ctx.panels.get(panel.id)
    except (PanelError, LookupError) as exc:
        return 0, [str(exc)[:300]]
    semaphore = asyncio.Semaphore(DELETE_CONCURRENCY)
    removed = 0
    errors: list[str] = []

    async def run(name: str, call: Awaitable[None]) -> None:
        nonlocal removed
        async with semaphore:
            try:
                await call
                removed += 1
            except (PanelError, OSError, TimeoutError) as exc:
                errors.append(f"{name}: {str(exc)[:200] or type(exc).__name__}")

    calls: list[Awaitable[None]] = []
    for email, target, uuid in rows:
        if email:
            calls.append(
                run(email, backend.delete_account(target if panel.kind == PanelKind.XUI else None, email, uuid))
            )
    delete_proxy_user = getattr(backend, "delete_proxy_user", None)
    for name, target in proxy_rows:
        if callable(delete_proxy_user):
            calls.append(run(name, delete_proxy_user(target, name)))
        else:
            errors.append(f"{name}: this panel cannot remove Telegram proxy users")
    for name, target in socks_rows:
        if isinstance(backend, SocksProxyBackend):
            calls.append(run(name, backend.delete_socks_user(target, name)))
        else:
            errors.append(f"{name}: this panel cannot remove SOCKS5 users")
    await asyncio.gather(*calls)
    return removed, errors


@router.delete("/panels/{panel_id}", response_model=PanelDeleteOut)
async def delete_panel(
    panel_id: int, actor: Admin, remove_accounts: bool = False, force: bool = False
) -> PanelDeleteOut:
    """Delete a panel. With ``remove_accounts`` our accounts are deleted from it first (``force`` ignores failures)."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        panel = await _get_panel(session, panel_id)
    removed, errors = 0, list[str]()
    if remove_accounts:
        removed, errors = await _remove_panel_accounts(actor, panel)
        if errors and not force:
            raise conflict("panel_failed", "Some accounts could not be removed: " + "; ".join(errors[:5]))
    async with ctx.session_factory.begin() as session:
        await session.execute(delete(Panel).where(Panel.id == panel_id).execution_options(synchronize_session=False))
        await actor.audit(
            session,
            "panel.delete",
            "panel",
            panel_id,
            name=panel.name,
            remove_accounts=remove_accounts,
            accounts_removed=removed,
            errors=len(errors),
        )
    await ctx.panels.invalidate(panel_id)
    return PanelDeleteOut(deleted=True, accounts_removed=removed, errors=errors)


@router.post("/panels/{panel_id}/check", response_model=PanelTestOut)
async def check_panel(panel_id: int, actor: Admin) -> PanelTestOut:
    """Connect to a saved panel now and record the result in its health fields."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        await _get_panel(session, panel_id)
    try:
        backend = await ctx.panels.get(panel_id)
    except (PanelError, LookupError) as exc:
        return PanelTestOut(ok=False, error=str(exc)[:500])
    result = await _probe(backend)
    async with ctx.session_factory.begin() as session:
        panel = await _get_panel(session, panel_id)
        if result.ok:
            panel.version = result.version
            panel.last_ok_at = utcnow()
            panel.last_error = None
            panel.consecutive_failures = 0
        else:
            panel.last_error = result.error
    return result


@router.post("/panels/{panel_id}/sync", response_model=SyncNowOut)
async def sync_panel(panel_id: int, actor: Admin) -> SyncNowOut:
    """Re-push every account of the panel now."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        await _get_panel(session, panel_id)
        marked = await provisioning.mark_dirty(session, panel_id=panel_id)
        await actor.audit(session, "panel.sync", "panel", panel_id, marked=marked)
    return SyncNowOut(marked=marked, started_jobs=kick_reconcile(ctx))


@router.post("/panels/{panel_id}/inbounds/refresh", response_model=RefreshOut)
async def refresh_panel_inbounds(panel_id: int, body: RefreshIn, actor: Admin) -> RefreshOut:
    """Import new inbounds of a 3x-ui panel and update the cached link parameters of known ones."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        panel = await _get_panel(session, panel_id)
    if panel.kind != PanelKind.XUI:
        raise bad_request("not_xui", "Only 3x-ui panels have inbounds")
    try:
        result = await inventory.refresh_inbounds(
            ctx.session_factory,
            ctx.panels,
            panel_id,
            activate_new=body.activate_new,
            location_id=body.location_id,
            endpoint_address=body.endpoint_address,
        )
    except PanelError as exc:
        raise conflict("panel_failed", str(exc)[:500]) from None
    async with ctx.session_factory.begin() as session:
        await actor.audit(session, "panel.refresh_inbounds", "panel", panel_id, created=result.created)
    if result.created or result.deactivated:
        kick_reconcile(ctx)
    return RefreshOut(
        created=result.created,
        updated=result.updated,
        deactivated=result.deactivated,
        disabled_on_panel=result.disabled_on_panel,
        skipped_remote_ids=result.skipped_remote_ids,
    )


@router.get("/panels/{panel_id}/status", response_model=ServerStatusOut)
async def panel_server_status(panel_id: int, actor: Admin) -> ServerStatusOut:
    """CPU, memory, disk, traffic and core state of the server (where the panel reports it)."""
    ctx = actor.ctx
    try:
        backend = await ctx.panels.get(panel_id)
    except LookupError:
        raise not_found("Panel not found") from None
    except PanelError as exc:
        return ServerStatusOut(supported=True, error=str(exc)[:500])
    if not isinstance(backend, ServerMonitor):
        return ServerStatusOut(supported=False)
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SEC):
            status = await backend.server_status()
    except (PanelError, OSError, TimeoutError) as exc:
        return ServerStatusOut(supported=True, error=str(exc)[:500] or type(exc).__name__)
    data = dataclasses.asdict(status)
    data.update(
        core_running=status.core_running,
        mem_percent=status.mem_percent,
        disk_percent=status.disk_percent,
        swap_percent=status.swap_percent,
    )
    return ServerStatusOut(supported=True, status=data)


@router.get("/panels/{panel_id}/ad-blocking", response_model=AdBlockingOut)
async def panel_ad_blocking(panel_id: int, actor: Admin) -> AdBlockingOut:
    """Ad blocking and YouTube exit state of the panel (3x-ui only; other panels manage their own Xray config)."""
    ctx = actor.ctx
    wanted = ctx.settings.adblock_enabled
    youtube = ad_blocking.youtube_exit_from_settings(ctx.settings)
    try:
        backend = await ctx.panels.get(panel_id)
    except LookupError:
        raise not_found("Panel not found") from None
    except PanelError as exc:
        return AdBlockingOut(supported=True, wanted=wanted, youtube_wanted=youtube is not None, error=str(exc)[:500])
    if not isinstance(backend, AdBlocker):
        return AdBlockingOut(supported=False, wanted=wanted, youtube_wanted=youtube is not None)
    recorded = await ad_blocking.load_state(ctx.session_factory, panel_id)
    youtube_now: bool | None = (
        None if recorded.youtube is None else recorded.youtube == (youtube is not None)
    )  # last recorded run
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SEC):
            enabled: bool | None = await backend.get_ad_blocking()
            if isinstance(backend, TemplateExtras):
                youtube_now = await backend.get_youtube_exit(youtube)
        error = recorded.error
    except (PanelError, OSError, TimeoutError) as exc:
        enabled, error = recorded.enabled, str(exc)[:500] or type(exc).__name__
    return AdBlockingOut(
        supported=True,
        wanted=wanted,
        enabled=enabled,
        error=error,
        checked_at=recorded.checked_at,
        youtube_wanted=youtube is not None,
        youtube_synced=youtube_now,
        sniffing_fixed=recorded.sniffing_fixed,
    )


@router.post("/panels/{panel_id}/restart-core", response_model=Ok)
async def restart_core(panel_id: int, actor: Admin) -> Ok:
    """Restart Xray (3x-ui) / the nodes' core; connected users drop for a moment."""
    ctx = actor.ctx
    try:
        backend = await ctx.panels.get(panel_id)
    except LookupError:
        raise not_found("Panel not found") from None
    except PanelError as exc:
        raise conflict("panel_failed", str(exc)[:500]) from None
    if not isinstance(backend, CoreRestarter):
        raise bad_request("not_supported", "This panel cannot restart its core")
    try:
        await backend.restart_core()
    except PanelError as exc:
        raise conflict("panel_failed", str(exc)[:500]) from None
    async with ctx.session_factory.begin() as session:
        await actor.audit(session, "panel.restart_core", "panel", panel_id)
    return Ok()


# ---------------------------------------------------------------------------------------------
# Inbounds
# ---------------------------------------------------------------------------------------------
def _inbound_out(inbound: Inbound, endpoints: int) -> InboundOut:
    try:
        params = json.loads(inbound.params) if inbound.params else {}
    except ValueError:
        params = {}
    params = params if isinstance(params, dict) else {}
    mtproto = inventory.is_mtproto_inbound(inbound)
    socks = inventory.is_socks_inbound(inbound)
    return InboundOut(
        id=inbound.id,
        panel_id=inbound.panel_id,
        remote_id=inbound.remote_id,
        location_id=inbound.location_id,
        name=inbound.name,
        protocol=params.get("protocol"),
        port=inventory.inbound_port(inbound),
        network=params.get("type"),
        security=params.get("security"),
        flow=inbound.flow,
        is_active=inbound.is_active,
        sort_order=inbound.sort_order,
        tg_proxy=inventory.tg_proxy_enabled(inbound) if mtproto else None,
        tg_socks=inventory.tg_socks_enabled(inbound) if socks else None,
        ss_method=params.get("ss_method") if shadowsocks.is_shadowsocks(params) else None,
        unsupported_reason=shadowsocks.blocked_reason(params),
        socks_auth=params.get("socks_auth") if socks else None,
        params_updated_at=inbound.params_updated_at,
        endpoints=endpoints,
    )


async def _endpoint_counts(session: AsyncSession) -> dict[int, int]:
    rows = await session.execute(select(Endpoint.inbound_id, func.count(Endpoint.id)).group_by(Endpoint.inbound_id))
    return {int(i): int(n) for i, n in rows}


@router.get("/inbounds", response_model=list[InboundOut])
async def list_inbounds(actor: Admin, session: ReadDb, panel_id: int | None = None) -> list[InboundOut]:
    stmt = select(Inbound).order_by(Inbound.panel_id, Inbound.sort_order, Inbound.id)
    if panel_id is not None:
        stmt = stmt.where(Inbound.panel_id == panel_id)
    counts = await _endpoint_counts(session)
    return [_inbound_out(i, counts.get(i.id, 0)) for i in (await session.scalars(stmt)).all()]


@router.patch("/inbounds/{inbound_id}", response_model=InboundOut)
async def update_inbound(inbound_id: int, body: InboundUpdate, actor: Admin) -> InboundOut:
    """Location, name, order, flow, on/off; MTProto / mixed inbounds can be (de)selected for the Telegram proxy."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        inbound = await session.get(Inbound, inbound_id)
        if inbound is None:
            raise not_found("Inbound not found")
        if body.is_active and inventory.is_proxy_inbound(inbound):
            raise bad_request("proxy_inbound", "Telegram proxy inbounds never carry VPN subscriptions")
        if body.is_active and (reason := inventory.shadowsocks_block_reason(inbound)):
            raise bad_request("shadowsocks_unsupported", f"This Shadowsocks inbound cannot be used: {reason}")
        changes: dict[str, Any] = {}
        if body.clear_location:
            changes["location_id"] = None
        elif body.location_id is not None:
            if await session.get(Location, body.location_id) is None:
                raise bad_request("unknown_location", "No such location")
            changes["location_id"] = body.location_id
        for key in ("name", "flow", "is_active", "sort_order"):
            value = getattr(body, key)
            if value is not None:
                changes[key] = value
        for key, value in changes.items():
            setattr(inbound, key, value)
        if body.tg_proxy is not None:
            if not inventory.set_tg_proxy_flag(inbound, body.tg_proxy):
                raise bad_request("not_mtproto", "Only MTProto inbounds can serve the Telegram proxy")
            changes["tg_proxy"] = body.tg_proxy
        if body.tg_socks is not None:
            if not inventory.set_tg_socks_flag(inbound, body.tg_socks):
                raise bad_request("not_socks", "Only mixed (SOCKS5) inbounds can serve the Telegram SOCKS5 proxy")
            changes["tg_socks"] = body.tg_socks
        if not changes:
            raise bad_request("nothing_to_change", "Nothing to change")
        if {"flow", "is_active"} & set(changes):
            await provisioning.mark_dirty(session, panel_id=inbound.panel_id, inbound_id=inbound.id)
        await actor.audit(session, "inbound.update", "inbound", inbound_id, **changes)
        count = int(await session.scalar(select(func.count(Endpoint.id)).where(Endpoint.inbound_id == inbound_id)) or 0)
        out = _inbound_out(inbound, count)
    kick_reconcile(ctx)
    return out


@router.get("/reality/sni", response_model=list[SniCandidate])
async def reality_sni_candidates(actor: Admin) -> list[SniCandidate]:
    """Popular Reality targets; pick one that the server reaches quickly."""
    return [SniCandidate(sni=sni, note=note) for sni, note in REALITY_SNI_CANDIDATES]


@router.post("/reality/sni/check", response_model=SniCheckOut)
async def check_sni(body: SniCheckIn, actor: Admin) -> SniCheckOut:
    """TCP reachability of a Reality target from this server (the panel's server should be checked too)."""
    error = await inventory.tcp_probe(body.sni, body.port)
    return SniCheckOut(sni=body.sni, reachable=error is None, error=error)


@router.post("/panels/{panel_id}/inbounds/shadowsocks", response_model=InboundOut, status_code=201)
async def create_shadowsocks_inbound(panel_id: int, body: ShadowsocksInboundIn, actor: Admin) -> InboundOut:
    """Create a multi-user Shadowsocks-2022 inbound on a 3x-ui panel (fresh server key) and import it."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        panel = await _get_panel(session, panel_id)
        if body.location_id is not None and await session.get(Location, body.location_id) is None:
            raise bad_request("unknown_location", "No such location")
    if panel.kind != PanelKind.XUI:
        raise bad_request("not_xui", "Inbounds can be created on 3x-ui panels only")
    try:
        backend = await ctx.panels.get(panel_id)
    except (PanelError, LookupError) as exc:
        raise conflict("panel_failed", str(exc)[:500]) from None
    create = getattr(backend, "create_shadowsocks_inbound", None)
    if not callable(create):
        raise bad_request("not_supported", "This panel cannot create inbounds")
    try:
        info: InboundInfo = await create(body.port, body.method, remark=body.remark or f"ss-{body.port}")
    except (PanelError, ValueError) as exc:
        raise conflict("panel_failed", str(exc)[:500]) from None
    if not info.remote_id:
        raise conflict("panel_failed", "The panel did not report the new inbound")
    try:
        await inventory.refresh_inbounds(
            ctx.session_factory,
            ctx.panels,
            panel_id,
            only_remote_ids={info.remote_id},
            location_id=body.location_id,
            endpoint_address=body.endpoint_address,
        )
    except PanelError as exc:
        raise conflict("panel_failed", f"Created on the panel but could not import it: {exc}"[:500]) from None
    async with ctx.session_factory.begin() as session:
        inbound = await session.scalar(
            select(Inbound).where(Inbound.panel_id == panel_id, Inbound.remote_id == info.remote_id)
        )
        if inbound is None:
            raise conflict("import_failed", "The new inbound could not be imported")
        if body.activate and inventory.shadowsocks_block_reason(inbound) is None:
            inbound.is_active = True  # imports never activate Shadowsocks inbounds on their own
        await actor.audit(
            session,
            "inbound.create_shadowsocks",
            "inbound",
            inbound.id,
            panel_id=panel_id,
            port=body.port,
            method=body.method,
        )
        count = int(await session.scalar(select(func.count(Endpoint.id)).where(Endpoint.inbound_id == inbound.id)) or 0)
        out = _inbound_out(inbound, count)
    kick_reconcile(ctx)
    return out


@router.post("/panels/{panel_id}/inbounds/reality", response_model=InboundOut, status_code=201)
async def create_reality_inbound(panel_id: int, body: RealityInboundIn, actor: Admin) -> InboundOut:
    """Create a VLESS + Reality inbound on a 3x-ui panel (fresh X25519 keys) and import it."""
    ctx = actor.ctx
    async with read_session(ctx.session_factory) as session:
        panel = await _get_panel(session, panel_id)
        if body.location_id is not None and await session.get(Location, body.location_id) is None:
            raise bad_request("unknown_location", "No such location")
    if panel.kind != PanelKind.XUI:
        raise bad_request("not_xui", "Inbounds can be created on 3x-ui panels only")
    try:
        backend = await ctx.panels.get(panel_id)
    except (PanelError, LookupError) as exc:
        raise conflict("panel_failed", str(exc)[:500]) from None
    create = getattr(backend, "create_reality_inbound", None)
    if not callable(create):
        raise bad_request("not_supported", "This panel cannot create inbounds")
    try:
        info: InboundInfo = await create(
            body.port, body.sni, dest=body.dest, remark=body.remark or f"vless-{body.port}"
        )
    except (PanelError, ValueError) as exc:
        raise conflict("panel_failed", str(exc)[:500]) from None
    if not info.remote_id:
        raise conflict("panel_failed", "The panel did not report the new inbound")
    try:
        await inventory.refresh_inbounds(
            ctx.session_factory,
            ctx.panels,
            panel_id,
            activate_new=body.activate,
            only_remote_ids={info.remote_id},
            location_id=body.location_id,
            endpoint_address=body.endpoint_address,
        )
    except PanelError as exc:
        raise conflict("panel_failed", f"Created on the panel but could not import it: {exc}"[:500]) from None
    async with ctx.session_factory.begin() as session:
        inbound = await session.scalar(
            select(Inbound).where(Inbound.panel_id == panel_id, Inbound.remote_id == info.remote_id)
        )
        if inbound is None:
            raise conflict("import_failed", "The new inbound could not be imported")
        flow = str(info.params.get("flow") or "")
        if flow and not inbound.flow:
            inbound.flow = flow
        await actor.audit(
            session, "inbound.create_reality", "inbound", inbound.id, panel_id=panel_id, port=body.port, sni=body.sni
        )
        count = int(await session.scalar(select(func.count(Endpoint.id)).where(Endpoint.inbound_id == inbound.id)) or 0)
        out = _inbound_out(inbound, count)
    kick_reconcile(ctx)
    return out
