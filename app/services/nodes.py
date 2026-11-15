"""Adding another VPN server ("node") with one command on that server.

On the main server ``netoryx node-token`` issues a one-time join code (:func:`issue_token`). The installer on the new
server (``install.sh --node --join <main domain>/<code>``) sets up 3x-ui behind its own HTTPS and calls
``POST /api/node/join`` with the panel's address and credentials. :func:`join` spends the code and registers the node
like the installer registers a local panel: the panel record, a VLESS + Reality inbound on the node's port 443 created
through the panel API, a location and an endpoint. Customers then get the new server in their subscriptions.

A code is random, single-use and expires (``NODE_TOKEN_TTL_MIN``); only its hash is stored. Panel I/O happens with no
database transaction open.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from app.services import kv
from app.services.local_setup import LocalSetup, LocalSetupResult, SetupError, XuiSetup, bootstrap_local
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.db.session import SessionFactory
    from app.services.panels import inventory
    from app.utils.crypto import SecretBox

TOKEN_TTL: Final = timedelta(hours=1)
NODE_REALITY_PORT: Final = 443
_KEY_PREFIX: Final = "node_join:"
_HOST = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9-]{2,63}$")
_IPV4 = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


class JoinError(Exception):
    """The join request is refused; the message is safe to show to the installer (no secrets)."""


@dataclass(frozen=True, slots=True)
class NodeJoin:
    """What the installer on the new server reports."""

    panel_url: str  # https URL of the node's 3x-ui incl. the web base path
    username: str
    password: str
    address: str  # public address customers connect to (IP or domain)
    reality_sni: str  # server name the Reality inbound borrows
    location_name: str
    location_flag: str = "🌍"


def _key(token: str) -> str:
    return _KEY_PREFIX + hashlib.sha256(token.encode()).hexdigest()[:48]  # keys are at most 64 characters


async def issue_token(session_factory: SessionFactory, *, now: datetime | None = None) -> str:
    """A new one-time join code (shown once; only its hash is kept)."""
    token = secrets.token_urlsafe(24)
    expires = (now or utcnow()) + TOKEN_TTL
    async with session_factory.begin() as session:
        await kv.add(session, _key(token), json.dumps({"expires_at": expires.isoformat()}))
    return token


async def _spend(session_factory: SessionFactory, token: str, now: datetime) -> bool:
    """Use the code up; False when it is unknown, already used or expired."""
    if not token or len(token) > 128:
        return False
    key = _key(token)
    async with session_factory.begin() as session:
        raw = await kv.get(session, key, fresh=True)
        if raw is None or not await kv.delete(session, key):
            return False
    try:
        expires = datetime.fromisoformat(json.loads(raw)["expires_at"])
    except (ValueError, KeyError, TypeError):
        return False
    return now < expires


def validate(request: NodeJoin) -> None:
    """Refuse malformed input before the code is spent (raises :class:`JoinError`)."""
    parts = urlsplit(request.panel_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise JoinError("the panel URL must be an https:// address without credentials")
    for name, value in (("address", request.address), ("Reality server name", request.reality_sni)):
        if not (_HOST.match(value) or _IPV4.match(value)):
            raise JoinError(f"invalid {name}")
    if not request.username or not request.password or len(request.password) > 256:
        raise JoinError("the panel credentials are missing")
    if not request.location_name.strip() or len(request.location_name) > 64 or len(request.location_flag) > 16:
        raise JoinError("invalid location")


async def join(
    session_factory: SessionFactory,
    registry: inventory.PanelSource,
    secret_box: SecretBox,
    token: str,
    request: NodeJoin,
    *,
    now: datetime | None = None,
) -> LocalSetupResult:
    """Spend ``token`` and register the node (see the module docstring). Raises :class:`JoinError`."""
    validate(request)
    if not await _spend(session_factory, token, now or utcnow()):
        raise JoinError("the join code is invalid, used or expired: issue a new one with `netoryx node-token`")
    setup = LocalSetup(
        location_name=request.location_name.strip(),
        location_flag=request.location_flag or "🌍",
        xui=XuiSetup(
            url=request.panel_url.rstrip("/"),
            username=request.username,
            password=request.password,
            reality_port=NODE_REALITY_PORT,
            reality_sni=request.reality_sni,
            reality_dest=f"{request.reality_sni}:443",
            endpoint_address=request.address,
            endpoint_port=NODE_REALITY_PORT,
            tls_verify=True,
            panel_name=f"3x-ui {request.location_name.strip()} {request.address}"[:64],
        ),
    )
    try:
        return await bootstrap_local(session_factory, registry, secret_box, setup)
    except SetupError as exc:
        raise JoinError(str(exc)) from None


def join_string(domain: str, token: str) -> str:
    """What the operator passes to the installer on the new server."""
    return f"{domain}/{token}"


def describe(result: LocalSetupResult) -> dict[str, Any]:
    """The non-secret part of the result for the installer."""
    return {"location_id": result.location_id, "panel": result.xui, "warnings": result.warnings}
