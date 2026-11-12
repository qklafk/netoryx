"""Set up the VPN entry point on a panel that was installed next to the application (installer, ``--panel``).

For Remnawave, Marzban, PasarGuard and Marzneshin the installer starts the official containers and
:func:`provision_panel` talks to the panel's own HTTP API (from inside the Docker network) to:

* log in as the administrator the installer created (Remnawave: register the first one);
* create what the bot's backend needs (Remnawave API token, PasarGuard API key; Marzban and Marzneshin use the
  administrator login);
* create one VLESS + Reality inbound that listens on ``reality_port`` inside the panel's node container and borrows
  the site ``vpn.<domain>`` served by Caddy, a node record where the panel has nodes, and a public host entry
  (``endpoint_address:443`` with the Reality server name) so links point at the shared TLS port.

Every step first looks for what already exists, so running it again changes nothing. Secrets are never returned.
HTTP goes through the small :class:`PanelHttp` protocol so tests can replace the network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.db.models import PanelKind

log = logging.getLogger(__name__)

INBOUND_TAG: Final = "netoryx-reality"
PROFILE_NAME: Final = "netoryx"
HOST_REMARK: Final = "Netoryx"
NODE_NAME: Final = "Main node"
API_TOKEN_NAME: Final = "netoryx-bot"
NODE_PORTS: Final = {PanelKind.REMNAWAVE: 2222, PanelKind.PASARGUARD: 62050, PanelKind.MARZNESHIN: 53042}
READY_ATTEMPTS: Final = 40
READY_DELAY_SEC: Final = 3.0
FINGERPRINT: Final = "firefox"  # compact ClientHello: no post-quantum key share, survives picky networks

INSTALLABLE: Final = (PanelKind.REMNAWAVE, PanelKind.MARZBAN, PanelKind.PASARGUARD, PanelKind.MARZNESHIN)
PANEL_NAMES: Final = {
    PanelKind.REMNAWAVE: "Remnawave",
    PanelKind.MARZBAN: "Marzban",
    PanelKind.PASARGUARD: "PasarGuard",
    PanelKind.MARZNESHIN: "Marzneshin",
}
# Panels whose entry point could not be checked against a running instance: failures there are warnings, not errors.
BEST_EFFORT: Final = (PanelKind.PASARGUARD, PanelKind.MARZNESHIN)


class PanelSetupError(Exception):
    """A step failed; the message is safe to show (no secrets)."""


class PanelHttp(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        form: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> tuple[int, Any]:
        """Returns ``(status, parsed JSON or None)``; ``status`` 0 means the panel did not answer."""
        ...

    async def close(self) -> None: ...


class AiohttpPanelHttp:
    """:class:`PanelHttp` over aiohttp; ``headers`` are sent with every request."""

    def __init__(self, base_url: str, headers: Mapping[str, str] | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._headers = dict(headers or {})
        self._session: aiohttp.ClientSession | None = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        form: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> tuple[int, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        headers = {**self._headers, "Accept": "application/json"}
        if auth is not None:
            headers["Authorization"] = f"{auth[0]} {auth[1]}"
        try:
            async with self._session.request(
                method, self._base + path, json=json, data=dict(form) if form else None, headers=headers
            ) as response:
                body = await response.read()
                try:
                    return response.status, (_loads(body) if body else None)
                except ValueError:
                    return response.status, None
        except (aiohttp.ClientError, TimeoutError):
            return 0, None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def _loads(body: bytes) -> Any:
    return json.loads(body)


# --------------------------------------------------------------------------------------------------------------
# Xray configuration
# --------------------------------------------------------------------------------------------------------------
def reality_keypair() -> tuple[str, str]:
    """``(private, public)`` X25519 keys in the unpadded base64url form Xray uses."""
    key = X25519PrivateKey.generate()
    raw_private = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    raw_public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64(raw_private), _b64(raw_public)


def reality_public_key(private_key: str) -> str:
    """The public key that belongs to an Xray Reality private key."""
    raw = base64.urlsafe_b64decode(private_key + "=" * (-len(private_key) % 4))
    public = X25519PrivateKey.from_private_bytes(raw).public_key()
    return _b64(public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def reality_inbound(port: int, sni: str, dest: str, private_key: str, short_id: str, tag: str = INBOUND_TAG) -> dict:
    """A VLESS + Reality inbound without clients (the panel adds them)."""
    return {
        "tag": tag,
        "listen": "0.0.0.0",
        "port": port,
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": dest,
                "xver": 0,
                "serverNames": [sni],
                "privateKey": private_key,
                "shortIds": [short_id],
            },
        },
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
    }


def xray_config(inbound: dict) -> dict:
    """A complete Xray configuration around one inbound (private addresses blocked)."""
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [inbound],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}, {"protocol": "blackhole", "tag": "BLOCK"}],
        "routing": {"rules": [{"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}]},
    }


def new_reality_config(port: int, sni: str, dest: str) -> dict:
    private, _public = reality_keypair()
    return xray_config(reality_inbound(port, sni, dest, private, secrets.token_hex(4)))


# --------------------------------------------------------------------------------------------------------------
# Setup description and result
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PanelInstall:
    kind: PanelKind
    url: str  # API address reachable from the application container, e.g. http://remnawave:3000
    admin_user: str
    admin_password: str
    reality_port: int
    reality_sni: str
    reality_dest: str
    endpoint_address: str
    endpoint_port: int = 443
    node_address: str = ""  # container name of the node (Remnawave, PasarGuard, Marzneshin)
    node_secret_out: str | None = None  # Remnawave: where to write the node SECRET_KEY
    node_cert: str | None = None  # PasarGuard: certificate of the node
    node_api_key: str | None = None  # PasarGuard: API key the node expects (a UUID)
    existing_token: str | None = None  # API token stored by an earlier run


@dataclass(slots=True)
class PanelProvision:
    """What the bot must store for the panel, plus a safe summary."""

    username: str | None = None
    password: str | None = None
    api_token: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


Sleep = Callable[[float], Awaitable[Any]]


def _data(body: Any) -> Any:
    """Remnawave wraps every answer in ``{"response": ...}``."""
    return body.get("response") if isinstance(body, dict) and "response" in body else body


def _brief(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            value = body.get(key)
            if isinstance(value, str):
                return value[:120]
    return ""


async def _call(
    http: PanelHttp,
    method: str,
    path: str,
    *,
    ok: tuple[int, ...] = (200, 201, 204),
    **kwargs: Any,
) -> Any:
    status, body = await http.request(method, path, **kwargs)
    if status not in ok:
        detail = _brief(body)
        raise PanelSetupError(f"{method} {path} -> HTTP {status}" + (f" ({detail})" if detail else ""))
    return body


async def _wait_ready(http: PanelHttp, path: str, what: str, sleep: Sleep) -> None:
    for attempt in range(READY_ATTEMPTS):
        status, _ = await http.request("GET", path)
        if status != 0 and status < 500:
            return
        if attempt + 1 < READY_ATTEMPTS:
            await sleep(READY_DELAY_SEC)
    raise PanelSetupError(f"{what} did not answer at {path}")


async def _login_form(http: PanelHttp, path: str, install: PanelInstall) -> str:
    body = await _call(
        http, "POST", path, form={"username": install.admin_user, "password": install.admin_password}, ok=(200,)
    )
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise PanelSetupError("the panel returned no access token")
    return token


def _write_secret(path: str, value: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(mode=0o600)
    target.write_text(value, encoding="utf-8")
    target.chmod(0o600)


# --------------------------------------------------------------------------------------------------------------
# Remnawave
# --------------------------------------------------------------------------------------------------------------
async def _provision_remnawave(http: PanelHttp, install: PanelInstall, sleep: Sleep) -> PanelProvision:
    out = PanelProvision(options={"direct": True})
    await _wait_ready(http, "/api/auth/status", "Remnawave", sleep)
    token = install.existing_token
    if token:
        status, _ = await http.request("GET", "/api/config-profiles", auth=("Bearer", token))
        if status != 200:
            token = None
    if not token:
        state = _data(await _call(http, "GET", "/api/auth/status"))
        creds = {"username": install.admin_user, "password": install.admin_password}
        allowed = isinstance(state, dict) and state.get("isRegisterAllowed")
        answer = await _call(http, "POST", "/api/auth/register" if allowed else "/api/auth/login", json=creds)
        jwt = (_data(answer) or {}).get("accessToken")
        if not isinstance(jwt, str):
            raise PanelSetupError("Remnawave returned no access token")
        created = _data(
            await _call(
                http,
                "POST",
                "/api/tokens",
                json={"name": API_TOKEN_NAME, "expiresInDays": 3650},
                auth=("Bearer", jwt),
            )
        )
        token = created.get("token") if isinstance(created, dict) else None
        if not isinstance(token, str):
            raise PanelSetupError("Remnawave returned no API token")
        out.summary["api_token"] = "created"
    else:
        out.summary["api_token"] = "existing"
    out.api_token = token
    bearer = ("Bearer", token)

    if install.node_secret_out:
        key = _data(await _call(http, "GET", "/api/keygen", auth=bearer))
        secret_key = key.get("secretKey") if isinstance(key, dict) else None
        if not isinstance(secret_key, str):
            raise PanelSetupError("Remnawave returned no node key")
        _write_secret(install.node_secret_out, secret_key)

    profiles = _data(await _call(http, "GET", "/api/config-profiles", auth=bearer))
    items = profiles.get("configProfiles", []) if isinstance(profiles, dict) else []
    profile = next((p for p in items if isinstance(p, dict) and p.get("name") == PROFILE_NAME), None)
    created_profile = profile is None
    if profile is None:
        profile = _data(
            await _call(
                http,
                "POST",
                "/api/config-profiles",
                json={
                    "name": PROFILE_NAME,
                    "config": new_reality_config(install.reality_port, install.reality_sni, install.reality_dest),
                },
                auth=bearer,
            )
        )
    profile_uuid = str((profile or {}).get("uuid") or "")
    inbound_uuid = next(
        (str(i.get("uuid")) for i in (profile or {}).get("inbounds", []) if i.get("tag") == INBOUND_TAG), ""
    )
    if not profile_uuid or not inbound_uuid:
        raise PanelSetupError("the Remnawave config profile has no Reality inbound")
    out.summary["profile"] = "created" if created_profile else "existing"

    nodes = _data(await _call(http, "GET", "/api/nodes", auth=bearer))
    if not any(isinstance(n, dict) and n.get("name") == NODE_NAME for n in nodes or []):
        await _call(
            http,
            "POST",
            "/api/nodes",
            json={
                "name": NODE_NAME,
                "address": install.node_address or "remnanode",
                "port": NODE_PORTS[PanelKind.REMNAWAVE],
                "isTrafficTrackingActive": True,
                "countryCode": "XX",
                "configProfile": {"activeConfigProfileUuid": profile_uuid, "activeInbounds": [inbound_uuid]},
            },
            auth=bearer,
        )
        out.summary["node"] = "created"
    else:
        out.summary["node"] = "existing"

    hosts = _data(await _call(http, "GET", "/api/hosts", auth=bearer))
    if not any(isinstance(h, dict) and h.get("remark") == HOST_REMARK for h in hosts or []):
        await _call(
            http,
            "POST",
            "/api/hosts",
            json={
                "inbound": {"configProfileUuid": profile_uuid, "configProfileInboundUuid": inbound_uuid},
                "remark": HOST_REMARK,
                "address": install.endpoint_address,
                "port": install.endpoint_port,
                "sni": install.reality_sni,
                "fingerprint": FINGERPRINT,
            },
            auth=bearer,
        )
        out.summary["host"] = "created"
    else:
        out.summary["host"] = "existing"

    squads = _data(await _call(http, "GET", "/api/internal-squads", auth=bearer))
    squad_items = squads.get("internalSquads", []) if isinstance(squads, dict) else []
    if squad_items:
        squad = squad_items[0]
        current = [str(i.get("uuid") if isinstance(i, dict) else i) for i in squad.get("inbounds", [])]
        if inbound_uuid not in current:
            await _call(
                http,
                "PATCH",
                "/api/internal-squads",
                json={"uuid": squad["uuid"], "inbounds": [*current, inbound_uuid]},
                auth=bearer,
            )
        out.summary["squad"] = "updated" if inbound_uuid not in current else "existing"
    else:
        out.warnings.append("Remnawave has no internal squad: add the Reality inbound to a squad in the panel")
    return out


# --------------------------------------------------------------------------------------------------------------
# Marzban
# --------------------------------------------------------------------------------------------------------------
async def _provision_marzban(http: PanelHttp, install: PanelInstall, sleep: Sleep) -> PanelProvision:
    out = PanelProvision(
        username=install.admin_user, password=install.admin_password, options={"inbound_tags": [INBOUND_TAG]}
    )
    await _wait_ready(http, "/api/admin/token", "Marzban", sleep)
    bearer = ("Bearer", await _login_form(http, "/api/admin/token", install))
    config = await _call(http, "GET", "/api/core/config", auth=bearer)
    if not isinstance(config, dict):
        raise PanelSetupError("Marzban returned no core configuration")
    inbounds = [i for i in config.get("inbounds", []) if isinstance(i, dict)]
    ours = next((i for i in inbounds if i.get("tag") == INBOUND_TAG), None)
    if ours is not None:
        out.summary["inbound"] = "existing"
        # Marzban derives the public key by parsing `xray x25519`, whose output changed in newer Xray releases;
        # with publicKey in the settings it does not need to.
        reality = (ours.get("streamSettings") or {}).get("realitySettings") or {}
        if reality.get("privateKey") and not reality.get("publicKey"):
            reality["publicKey"] = reality_public_key(reality["privateKey"])
            await _call(http, "PUT", "/api/core/config", json=config, auth=bearer)
            out.summary["inbound"] = "updated"
    else:
        private, public = reality_keypair()
        # the sample inbound of a fresh installation listens on a port nobody can reach: drop it
        kept = [i for i in inbounds if i.get("tag") != "Shadowsocks TCP"]
        inbound = reality_inbound(
            install.reality_port, install.reality_sni, install.reality_dest, private, secrets.token_hex(4)
        )
        inbound["streamSettings"]["realitySettings"]["publicKey"] = public
        await _call(http, "PUT", "/api/core/config", json={**config, "inbounds": [*kept, inbound]}, auth=bearer)
        out.summary["inbound"] = "created"
    hosts = await _call(http, "GET", "/api/hosts", auth=bearer)
    # Marzban adds a default host ("{SERVER_IP}", inbound port) for every new inbound; only ours counts.
    tagged = hosts.get(INBOUND_TAG) if isinstance(hosts, dict) else None
    if any(isinstance(h, dict) and h.get("remark") == HOST_REMARK for h in tagged or []):
        out.summary["host"] = "existing"
    else:
        host = {
            "remark": HOST_REMARK,
            "address": install.endpoint_address,
            "port": install.endpoint_port,
            "sni": install.reality_sni,
            "fingerprint": FINGERPRINT,
            "security": "inbound_default",
        }
        await _call(http, "PUT", "/api/hosts", json={INBOUND_TAG: [host]}, auth=bearer)
        out.summary["host"] = "created"
    return out


# --------------------------------------------------------------------------------------------------------------
# PasarGuard
# --------------------------------------------------------------------------------------------------------------
async def _provision_pasarguard(http: PanelHttp, install: PanelInstall, sleep: Sleep) -> PanelProvision:
    out = PanelProvision(options={})
    await _wait_ready(http, "/api/admin/token", "PasarGuard", sleep)
    bearer = ("Bearer", await _login_form(http, "/api/admin/token", install))

    cores = _data(await _call(http, "GET", "/api/cores", auth=bearer))
    core_items = cores.get("cores", []) if isinstance(cores, dict) else []
    core = next((c for c in core_items if isinstance(c, dict) and c.get("name") == PROFILE_NAME), None)
    if core is None:
        core = await _call(
            http,
            "POST",
            "/api/core",
            json={
                "name": PROFILE_NAME,
                "config": new_reality_config(install.reality_port, install.reality_sni, install.reality_dest),
                "exclude_inbound_tags": [],
                "fallbacks_inbound_tags": [],
            },
            auth=bearer,
        )
        out.summary["core"] = "created"
    else:
        out.summary["core"] = "existing"

    group_ids: list[int] = []
    groups = _data(await _call(http, "GET", "/api/groups", auth=bearer))
    group = next(
        (g for g in (groups.get("groups", []) if isinstance(groups, dict) else []) if g.get("name") == PROFILE_NAME),
        None,
    )
    if group is None:
        group = await _call(
            http, "POST", "/api/group", json={"name": PROFILE_NAME, "inbound_tags": [INBOUND_TAG]}, auth=bearer
        )
        out.summary["group"] = "created"
    else:
        out.summary["group"] = "existing"
    if isinstance(group, dict) and isinstance(group.get("id"), int):
        group_ids.append(group["id"])
        out.options["group_ids"] = group_ids

    nodes = _data(await _call(http, "GET", "/api/nodes", auth=bearer))
    node_items = nodes.get("nodes", []) if isinstance(nodes, dict) else []
    if not any(isinstance(n, dict) and n.get("name") == NODE_NAME for n in node_items):
        if not (install.node_cert and install.node_api_key and isinstance(core, dict) and core.get("id")):
            raise PanelSetupError("the node certificate or key is missing")
        await _call(
            http,
            "POST",
            "/api/node",
            json={
                "name": NODE_NAME,
                "address": install.node_address or "pg-node.internal",
                "port": NODE_PORTS[PanelKind.PASARGUARD],
                "connection_type": "grpc",
                "server_ca": install.node_cert,
                "keep_alive": 20,
                "core_config_id": core["id"],
                "api_key": install.node_api_key,
            },
            auth=bearer,
        )
        out.summary["node"] = "created"
    else:
        out.summary["node"] = "existing"

    hosts = _data(await _call(http, "GET", "/api/hosts", auth=bearer))
    if not any(isinstance(h, dict) and h.get("remark") == HOST_REMARK for h in hosts or []):
        await _call(
            http,
            "POST",
            "/api/host/",
            json={
                "remark": HOST_REMARK,
                "inbound_tag": INBOUND_TAG,
                "address": [install.endpoint_address],
                "port": install.endpoint_port,
                "sni": [install.reality_sni],
                "fingerprint": FINGERPRINT,
                "security": "inbound_default",
                "priority": 0,
            },
            auth=bearer,
        )
        out.summary["host"] = "created"
    else:
        out.summary["host"] = "existing"

    token = install.existing_token
    if token:
        status, _ = await http.request("GET", "/api/system/resources", auth=("ApiKey", token))
        if status != 200:
            token = None
    if not token:
        key = await _call(http, "POST", "/api/api_key", json={"name": API_TOKEN_NAME}, auth=bearer)
        token = key.get("api_key") if isinstance(key, dict) else None
        if not isinstance(token, str):
            raise PanelSetupError("PasarGuard returned no API key")
        out.summary["api_key"] = "created"
    else:
        out.summary["api_key"] = "existing"
    out.api_token = token
    return out


# --------------------------------------------------------------------------------------------------------------
# Marzneshin
# --------------------------------------------------------------------------------------------------------------
def _items(body: Any) -> list[dict[str, Any]]:
    raw = body.get("items") if isinstance(body, dict) else body
    return [i for i in raw or [] if isinstance(i, dict)]


async def _provision_marzneshin(http: PanelHttp, install: PanelInstall, sleep: Sleep) -> PanelProvision:
    out = PanelProvision(username=install.admin_user, password=install.admin_password, options={})
    await _wait_ready(http, "/api/admins/token", "Marzneshin", sleep)
    bearer = ("Bearer", await _login_form(http, "/api/admins/token", install))

    nodes = _items(await _call(http, "GET", "/api/nodes", auth=bearer))
    if not any(n.get("name") == NODE_NAME for n in nodes):
        await _call(
            http,
            "POST",
            "/api/nodes",
            json={
                "name": NODE_NAME,
                "address": install.node_address or "marznode",
                "port": NODE_PORTS[PanelKind.MARZNESHIN],
                "connection_backend": "grpclib",
                "usage_coefficient": 1,
            },
            auth=bearer,
        )
        out.summary["node"] = "created"
    else:
        out.summary["node"] = "existing"

    inbound: dict[str, Any] | None = None
    for attempt in range(READY_ATTEMPTS):  # the node reports its inbounds once it is connected
        found = _items(await _call(http, "GET", "/api/inbounds", auth=bearer))
        inbound = next((i for i in found if i.get("tag") == INBOUND_TAG), None)
        if inbound is not None:
            break
        if attempt + 1 < READY_ATTEMPTS:
            await sleep(READY_DELAY_SEC)
    if inbound is None or not isinstance(inbound.get("id"), int):
        raise PanelSetupError("the node has not reported the Reality inbound yet")
    inbound_id = inbound["id"]

    hosts = _items(await _call(http, "GET", f"/api/inbounds/{inbound_id}/hosts", auth=bearer))
    if not any(h.get("remark") == HOST_REMARK for h in hosts):
        await _call(
            http,
            "POST",
            f"/api/inbounds/{inbound_id}/hosts",
            json={
                "remark": HOST_REMARK,
                "address": install.endpoint_address,
                "port": install.endpoint_port,
                "sni": install.reality_sni,
                "fingerprint": FINGERPRINT,
                "security": "inbound_default",
            },
            auth=bearer,
        )
        out.summary["host"] = "created"
    else:
        out.summary["host"] = "existing"
    # The panel adds a default host for a new inbound: the server address and the inbound's internal port (no port
    # means the inbound port), which no client can reach. Drop such hosts so subscriptions carry only the public
    # entry point.
    for host in hosts:
        if host.get("remark") != HOST_REMARK and host.get("port") in (None, install.reality_port) and host.get("id"):
            await _call(http, "DELETE", f"/api/inbounds/hosts/{host['id']}", auth=bearer)
            out.summary["default_host"] = "removed"

    services = _items(await _call(http, "GET", "/api/services", auth=bearer))
    service = next((s for s in services if s.get("name") == PROFILE_NAME), None)
    if service is None:
        service = await _call(
            http, "POST", "/api/services", json={"name": PROFILE_NAME, "inbound_ids": [inbound_id]}, auth=bearer
        )
        out.summary["service"] = "created"
    else:
        out.summary["service"] = "existing"
        if inbound_id not in service.get("inbound_ids", []):
            await _call(
                http,
                "PUT",
                f"/api/services/{service['id']}",
                json={"inbound_ids": [*service.get("inbound_ids", []), inbound_id]},
                auth=bearer,
            )
    if isinstance(service, dict) and isinstance(service.get("id"), int):
        out.options["service_ids"] = [service["id"]]
    return out


_PROVISIONERS: Final = {
    PanelKind.REMNAWAVE: _provision_remnawave,
    PanelKind.MARZBAN: _provision_marzban,
    PanelKind.PASARGUARD: _provision_pasarguard,
    PanelKind.MARZNESHIN: _provision_marzneshin,
}


async def provision_panel(http: PanelHttp, install: PanelInstall, sleep: Sleep = asyncio.sleep) -> PanelProvision:
    """Run the panel-specific steps; raises :class:`PanelSetupError`."""
    try:
        provisioner = _PROVISIONERS[install.kind]
    except KeyError:
        raise PanelSetupError(f"{install.kind} cannot be installed by the installer") from None
    return await provisioner(http, install, sleep)


def new_node_api_key() -> str:
    """A UUID for the PasarGuard node ``API_KEY``."""
    return str(uuid.uuid4())
