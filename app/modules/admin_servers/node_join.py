"""``POST /api/node/join``: a new server registers itself with a one-time join code (see :mod:`app.services.nodes`)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.services import audit, nodes, tg_only
from app.web.deps import Ctx
from app.web.errors import ApiError
from app.web.security import client_ip

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

JOIN_ATTEMPTS_PER_IP = 10
JOIN_WINDOW_SEC = 3600


class JoinIn(BaseModel):
    token: str = Field(min_length=16, max_length=128)
    panel_url: str = Field(max_length=512)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    address: str = Field(max_length=253)
    reality_sni: str = Field(max_length=253)
    location_name: str = Field(min_length=1, max_length=64)
    location_flag: str = Field(default="", max_length=16)
    country: str = Field(default="", max_length=2)  # two-letter code; gives the flag when no flag is sent


@router.post("/api/node/join")
async def node_join(body: JoinIn, request: Request, ctx: Ctx) -> dict[str, Any]:
    ip = client_ip(request, ctx.settings)
    result = await request.app.state.rate_limiter.hit(f"node-join:{ip}", JOIN_ATTEMPTS_PER_IP, JOIN_WINDOW_SEC)
    if not result.allowed:
        raise ApiError(429, "too_many_requests", "Too many attempts", headers={"Retry-After": str(result.retry_after)})
    join = nodes.NodeJoin(
        panel_url=body.panel_url,
        username=body.username,
        password=body.password,
        address=body.address,
        reality_sni=body.reality_sni,
        location_name=body.location_name,
        location_flag=body.location_flag or tg_only.flag_of_country(body.country) or "🌍",
    )
    try:
        done = await nodes.join(ctx.session_factory, ctx.panels, ctx.secret_box, body.token, join)
    except nodes.JoinError as exc:
        log.warning("node join from %s refused: %s", ip, exc)
        raise ApiError(400, "join_refused", str(exc)) from None
    async with ctx.session_factory.begin() as session:
        await audit.log(session, 0, "node.joined", "panel", done.xui.get("panel_id"), "system", address=body.address)
    log.info("node %s joined as panel %s", body.address, done.xui.get("panel_id"))
    return nodes.describe(done)
