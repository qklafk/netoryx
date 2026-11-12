"""Locations ("🇩🇪 Germany") and endpoints (where clients connect to an inbound: direct, relay, CDN, LTE)."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.db.models import Endpoint, EndpointKind, EndpointProfile, Inbound, Location
from app.modules.admin_api.deps import Actor, bad_request, not_found, require
from app.modules.admin_api.schemas.common import Ok
from app.modules.admin_api.schemas.panels import (
    EndpointIn,
    EndpointOut,
    EndpointPreset,
    EndpointUpdate,
    LocationIn,
    LocationOut,
    LocationUpdate,
    PreviewIn,
    PreviewOut,
    ProbeOut,
    ReorderIn,
)
from app.services import links
from app.services.panels import inventory, shadowsocks
from app.utils.time import utcnow
from app.web.deps import DbSession, ReadDb

router = APIRouter(tags=["admin: servers"])

Admin = Annotated[Actor, Depends(require(Permission.PANELS))]

PREVIEW_UUID = "00000000-0000-4000-8000-000000000000"

ENDPOINT_PRESETS: tuple[EndpointPreset, ...] = (
    EndpointPreset(
        key="relay_l4",
        title_ru="Relay (L4-проброс)",
        title_en="Relay (L4 forwarding)",
        kind=EndpointKind.RELAY,
        profile=EndpointProfile.STANDARD,
        overrides={},
        hint_ru="Адрес и порт промежуточного сервера, который пробрасывает TCP на этот inbound. Остальные "
        "параметры (Reality/TLS) не меняются.",
        hint_en="Address and port of a relay that forwards TCP to this inbound. Every other parameter "
        "(Reality/TLS) stays the same.",
    ),
    EndpointPreset(
        key="yandex_cdn_xhttp",
        title_ru="Яндекс CDN · XHTTP packet-up (GET)",
        title_en="Yandex CDN · XHTTP packet-up (GET)",
        kind=EndpointKind.CDN,
        profile=EndpointProfile.WHITELIST,
        overrides={
            "type": "xhttp",
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
        },
        hint_ru="Адрес — домен ресурса CDN (например, cdn.example.ru). Inbound должен быть XHTTP за TLS на "
        "origin-сервере; CDN не должен буферизовать ответы.",
        hint_en="Address is the CDN resource hostname (e.g. cdn.example.ru). The inbound must be XHTTP behind TLS "
        "on the origin; the CDN must not buffer responses.",
    ),
    EndpointPreset(
        key="reality_ru_sni",
        title_ru="Reality с российским SNI (LTE)",
        title_en="Reality with a Russian SNI (LTE)",
        kind=EndpointKind.DIRECT,
        profile=EndpointProfile.WHITELIST,
        overrides={"sni": "ya.ru"},
        hint_ru="Для мобильных сетей с белыми списками: inbound должен принимать этот SNI в serverNames, а "
        "target — вести на сайт из белого списка.",
        hint_en="For mobile networks with allowlists: the inbound must accept this SNI in serverNames and point "
        "its target to an allowlisted site.",
    ),
)


# ---------------------------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------------------------
async def _location(session: AsyncSession, location_id: int) -> Location:
    location = await session.get(Location, location_id)
    if location is None:
        raise not_found("Location not found")
    return location


@router.get("/locations", response_model=list[LocationOut])
async def list_locations(actor: Admin, session: ReadDb) -> list[LocationOut]:
    rows = (await session.scalars(select(Location).order_by(Location.sort_order, Location.id))).all()
    return [LocationOut.model_validate(r) for r in rows]


@router.post("/locations", response_model=LocationOut, status_code=201)
async def create_location(body: LocationIn, actor: Admin, session: DbSession) -> LocationOut:
    sort_order = body.sort_order
    if sort_order is None:
        sort_order = int(await session.scalar(select(func.coalesce(func.max(Location.sort_order), -1))) or 0) + 1
    location = Location(name=body.name.strip(), flag=body.flag.strip(), sort_order=sort_order, is_active=body.is_active)
    session.add(location)
    await session.flush()
    await actor.audit(session, "location.create", "location", location.id, name=location.name, flag=location.flag)
    return LocationOut.model_validate(location)


@router.patch("/locations/{location_id}", response_model=LocationOut)
async def update_location(location_id: int, body: LocationUpdate, actor: Admin, session: DbSession) -> LocationOut:
    location = await _location(session, location_id)
    changes = {
        k: (v.strip() if isinstance(v, str) else v)
        for k, v in body.model_dump(exclude_unset=True).items()
        if v is not None
    }
    if not changes:
        raise bad_request("nothing_to_change", "Nothing to change")
    for key, value in changes.items():
        setattr(location, key, value)
    await actor.audit(session, "location.update", "location", location_id, **changes)
    return LocationOut.model_validate(location)


@router.post("/locations/reorder", response_model=list[LocationOut])
async def reorder_locations(body: ReorderIn, actor: Admin, session: DbSession) -> list[LocationOut]:
    rows = list((await session.scalars(select(Location).order_by(Location.sort_order, Location.id))).all())
    by_id = {r.id: r for r in rows}
    if len(set(body.ids)) != len(body.ids) or any(i not in by_id for i in body.ids):
        raise bad_request("bad_ids", "Ids must be unique existing locations")
    wanted = set(body.ids)
    ordered = [by_id[i] for i in body.ids] + [r for r in rows if r.id not in wanted]
    for index, location in enumerate(ordered):
        location.sort_order = index
    await actor.audit(session, "location.reorder", "location", None, order=[r.id for r in ordered])
    return [LocationOut.model_validate(r) for r in ordered]


@router.delete("/locations/{location_id}", response_model=Ok)
async def delete_location(location_id: int, actor: Admin, session: DbSession) -> Ok:
    """Delete a location; its inbounds and endpoints stay, without a location."""
    location = await _location(session, location_id)
    await session.delete(location)
    await actor.audit(session, "location.delete", "location", location_id, name=location.name)
    return Ok()


# ---------------------------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------------------------
def _overrides(raw: str | None) -> dict[str, Any]:
    try:
        return links.parse_overrides(raw)
    except ValueError:
        return {}


def _endpoint_out(endpoint: Endpoint, panel_id: int) -> EndpointOut:
    return EndpointOut(
        id=endpoint.id,
        inbound_id=endpoint.inbound_id,
        panel_id=panel_id,
        location_id=endpoint.location_id,
        kind=endpoint.kind,
        profile=endpoint.profile,
        address=endpoint.address,
        port=endpoint.port,
        overrides=_overrides(endpoint.overrides),
        remark=endpoint.remark,
        description=endpoint.description,
        sort_order=endpoint.sort_order,
        is_active=endpoint.is_active,
        last_ok_at=endpoint.last_ok_at,
        last_error=endpoint.last_error,
        consecutive_failures=endpoint.consecutive_failures,
        auto_hidden=endpoint.auto_hidden,
    )


def _overrides_json(overrides: dict[str, Any]) -> str | None:
    if not overrides:
        return None
    try:
        return json.dumps(overrides, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise bad_request("bad_overrides", f"overrides must be JSON: {exc}") from None


async def _inbound(session: AsyncSession, inbound_id: int) -> Inbound:
    inbound = await session.get(Inbound, inbound_id)
    if inbound is None:
        raise bad_request("unknown_inbound", "No such inbound")
    return inbound


async def _check_location(session: AsyncSession, location_id: int | None) -> None:
    if location_id is not None and await session.get(Location, location_id) is None:
        raise bad_request("unknown_location", "No such location")


async def _endpoint(session: AsyncSession, endpoint_id: int) -> tuple[Endpoint, Inbound]:
    row = (
        await session.execute(
            select(Endpoint, Inbound).join(Inbound, Inbound.id == Endpoint.inbound_id).where(Endpoint.id == endpoint_id)
        )
    ).first()
    if row is None:
        raise not_found("Endpoint not found")
    return row[0], row[1]


@router.get("/endpoints/presets", response_model=list[EndpointPreset])
async def endpoint_presets(actor: Admin) -> list[EndpointPreset]:
    """Ready-made override sets: L4 relay, Russian CDN with XHTTP, Reality with an allowlisted SNI."""
    return list(ENDPOINT_PRESETS)


@router.get("/endpoints", response_model=list[EndpointOut])
async def list_endpoints(
    actor: Admin,
    session: ReadDb,
    inbound_id: int | None = None,
    panel_id: int | None = None,
    location_id: int | None = None,
    profile: EndpointProfile | None = None,
) -> list[EndpointOut]:
    stmt = select(Endpoint, Inbound.panel_id).join(Inbound, Inbound.id == Endpoint.inbound_id)
    if inbound_id is not None:
        stmt = stmt.where(Endpoint.inbound_id == inbound_id)
    if panel_id is not None:
        stmt = stmt.where(Inbound.panel_id == panel_id)
    if location_id is not None:
        stmt = stmt.where(Endpoint.location_id == location_id)
    if profile is not None:
        stmt = stmt.where(Endpoint.profile == profile)
    rows = (await session.execute(stmt.order_by(Endpoint.sort_order, Endpoint.id))).all()
    return [_endpoint_out(e, pid) for e, pid in rows]


def _preview(inbound: Inbound, endpoint: Endpoint, settings: Any, location: Location | None) -> PreviewOut:
    try:
        params = json.loads(inbound.params) if inbound.params else None
    except ValueError:
        params = None
    if not isinstance(params, dict):
        return PreviewOut(ok=False, error="The inbound has no link parameters yet; refresh the panel's inbounds")
    if shadowsocks.is_shadowsocks(params):
        name = links.entry_name(location, endpoint, settings, protocol_tag=links.SS_SUFFIX)
        try:
            ss_link, address, port, link_params = links.preview_ss(params, endpoint, name, PREVIEW_UUID)
        except ValueError as exc:
            return PreviewOut(ok=False, error=str(exc))
        return PreviewOut(ok=True, link=ss_link, address=address, port=port, params=link_params)
    if getattr(settings, "reality_compact_hello", False):
        params = links.compact_reality_hello(params)  # the same link a user receives
    try:
        address, port, link_params = links.effective_params(params, endpoint)
    except ValueError as exc:
        return PreviewOut(ok=False, error=str(exc))
    name = links.entry_name(location, endpoint, settings)
    link = links.build_vless(PREVIEW_UUID, address, port, link_params, name, endpoint.description, inbound.flow or "")
    return PreviewOut(ok=True, link=link, address=address, port=port, params=link_params)


@router.post("/endpoints/preview", response_model=PreviewOut)
async def preview_endpoint(body: PreviewIn, actor: Admin, session: ReadDb) -> PreviewOut:
    """The share link this endpoint produces (with a placeholder client id)."""
    if body.endpoint_id is not None:
        endpoint, inbound = await _endpoint(session, body.endpoint_id)
    elif body.draft is not None:
        inbound = await _inbound(session, body.draft.inbound_id)
        endpoint = Endpoint(
            id=0,
            inbound_id=inbound.id,
            location_id=body.draft.location_id,
            kind=body.draft.kind,
            profile=body.draft.profile,
            address=body.draft.address,
            port=body.draft.port,
            overrides=_overrides_json(body.draft.overrides),
            remark=body.draft.remark,
            description=body.draft.description,
        )
    else:
        raise bad_request("missing_fields", "Give endpoint_id or draft")
    location_id = endpoint.location_id or inbound.location_id
    location = await session.get(Location, location_id) if location_id else None
    return _preview(inbound, endpoint, actor.ctx.settings, location)


@router.post("/endpoints", response_model=EndpointOut, status_code=201)
async def create_endpoint(body: EndpointIn, actor: Admin, session: DbSession) -> EndpointOut:
    inbound = await _inbound(session, body.inbound_id)
    await _check_location(session, body.location_id)
    endpoint = Endpoint(
        inbound_id=inbound.id,
        location_id=body.location_id,
        kind=body.kind,
        profile=body.profile,
        address=body.address.strip(),
        port=body.port,
        overrides=_overrides_json(body.overrides),
        remark=body.remark or None,
        description=body.description or None,
        sort_order=body.sort_order,
        is_active=body.is_active,
    )
    preview = _preview(inbound, endpoint, actor.ctx.settings, None)
    if not preview.ok and inbound.params:
        raise bad_request("invalid_endpoint", preview.error or "invalid endpoint")
    session.add(endpoint)
    await session.flush()
    await actor.audit(
        session,
        "endpoint.create",
        "endpoint",
        endpoint.id,
        inbound_id=inbound.id,
        kind=body.kind.value,
        address=endpoint.address,
    )
    return _endpoint_out(endpoint, inbound.panel_id)


@router.patch("/endpoints/{endpoint_id}", response_model=EndpointOut)
async def update_endpoint(endpoint_id: int, body: EndpointUpdate, actor: Admin, session: DbSession) -> EndpointOut:
    endpoint, inbound = await _endpoint(session, endpoint_id)
    data = body.model_dump(exclude_unset=True)
    changes: dict[str, Any] = {}
    if data.pop("clear_location", False):
        changes["location_id"] = None
    elif data.get("location_id") is not None:
        await _check_location(session, data["location_id"])
        changes["location_id"] = data["location_id"]
    if data.pop("clear_port", False):
        changes["port"] = None
    elif data.get("port") is not None:
        changes["port"] = data["port"]
    for key in ("kind", "profile", "sort_order", "is_active"):
        if data.get(key) is not None:
            changes[key] = data[key]
    if data.get("address") is not None:
        changes["address"] = data["address"].strip()
    for key in ("remark", "description"):
        if key in data:
            changes[key] = data[key] or None
    if "overrides" in data and data["overrides"] is not None:
        changes["overrides"] = _overrides_json(data["overrides"])
    if not changes:
        raise bad_request("nothing_to_change", "Nothing to change")
    for key, value in changes.items():
        setattr(endpoint, key, value)
    if {"address", "port", "kind"} & set(changes):
        endpoint.auto_hidden = False
        endpoint.consecutive_failures = 0
    preview = _preview(inbound, endpoint, actor.ctx.settings, None)
    if not preview.ok and inbound.params:
        raise bad_request("invalid_endpoint", preview.error or "invalid endpoint")
    await actor.audit(session, "endpoint.update", "endpoint", endpoint_id, fields=sorted(changes))
    return _endpoint_out(endpoint, inbound.panel_id)


@router.delete("/endpoints/{endpoint_id}", response_model=Ok)
async def delete_endpoint(endpoint_id: int, actor: Admin, session: DbSession) -> Ok:
    endpoint, _ = await _endpoint(session, endpoint_id)
    await session.delete(endpoint)
    await actor.audit(session, "endpoint.delete", "endpoint", endpoint_id, address=endpoint.address)
    return Ok()


@router.post("/endpoints/{endpoint_id}/probe", response_model=ProbeOut)
async def probe_endpoint(endpoint_id: int, actor: Admin) -> ProbeOut:
    """TCP-connect to the endpoint now and record the result (a recovered endpoint is shown again)."""
    ctx = actor.ctx
    async with ctx.session_factory.begin() as session:
        endpoint, inbound = await _endpoint(session, endpoint_id)
        port = endpoint.port or inventory.inbound_port(inbound)
        address = endpoint.address
    if not port:
        raise bad_request("no_port", "The endpoint has no port and the inbound's port is unknown")
    error = await inventory.tcp_probe(address, port)
    now = utcnow()
    async with ctx.session_factory.begin() as session:
        endpoint, _ = await _endpoint(session, endpoint_id)
        if error is None:
            endpoint.last_ok_at = now
            endpoint.last_error = None
            endpoint.consecutive_failures = 0
            endpoint.auto_hidden = False
        else:
            endpoint.last_error = error
            endpoint.consecutive_failures += 1
    return ProbeOut(ok=error is None, error=error, checked_at=now)
