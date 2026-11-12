"""In-memory test doubles implementing the same interfaces as the real services."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from app.db.models import PanelKind
from app.services.devices import DeviceInfo
from app.services.panels.base import AccountSpec, InboundInfo, PanelError, RemoteAccount, ServerStatus


def default_inbound(remote_id: int = 1, port: int = 443) -> InboundInfo:
    return InboundInfo(
        remote_id=remote_id,
        remark=f"in-{port}",
        protocol="vless",
        port=port,
        enabled=True,
        params={
            "type": "tcp",
            "security": "reality",
            "pbk": "PUBKEY",
            "sni": "www.example.com",
            "sid": "ab",
            "fp": "chrome",
            "spx": "/",
            "flow_allowed": True,
        },
    )


@dataclass
class FakePanel:
    """Implements `app.services.panels.base.PanelBackend` in memory.

    Accounts are keyed by (target, email). Set `fail_next` to make the next N calls raise PanelError,
    or `down=True` to fail every call.
    """

    kind: PanelKind = PanelKind.XUI
    version: str = "3.11.0"
    inbounds: list[InboundInfo] = field(default_factory=lambda: [default_inbound()])
    accounts: dict[tuple[int | None, str], RemoteAccount] = field(default_factory=dict)
    fail_next: int = 0
    down: bool = False
    calls: list[tuple[str, int | None, str]] = field(default_factory=list)

    def _maybe_fail(self, op: str) -> None:
        if self.down:
            raise PanelError(f"panel down ({op})")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise PanelError(f"fake failure in {op}")

    async def ping(self) -> str:
        self._maybe_fail("ping")
        return self.version

    async def close(self) -> None:
        return None

    async def list_inbounds(self) -> list[InboundInfo]:
        self._maybe_fail("list_inbounds")
        return list(self.inbounds) if self.kind == PanelKind.XUI else []

    async def list_accounts(self, target: int | None) -> list[RemoteAccount]:
        self._maybe_fail("list_accounts")
        return [replace(a) for (t, _), a in self.accounts.items() if t == target]

    async def get_account(self, target: int | None, email: str) -> RemoteAccount | None:
        self._maybe_fail("get_account")
        acc = self.accounts.get((target, email))
        return replace(acc) if acc else None

    async def get_used_bytes(self, target: int | None, email: str) -> int | None:
        self._maybe_fail("get_used_bytes")
        acc = self.accounts.get((target, email))
        return acc.used_bytes if acc else None

    async def upsert_account(self, target: int | None, spec: AccountSpec) -> RemoteAccount:
        self.calls.append(("upsert", target, spec.email))
        self._maybe_fail("upsert_account")
        existing = self.accounts.get((target, spec.email))
        acc = RemoteAccount(
            email=spec.email,
            uuid=spec.password or spec.uuid,
            enabled=spec.enabled,
            expires_at=spec.expires_at,
            device_limit=spec.device_limit,
            traffic_limit_bytes=spec.traffic_limit_bytes,
            used_bytes=existing.used_bytes if existing else 0,
            remote_id=existing.remote_id if existing else str(len(self.accounts) + 1),
            short_id=existing.short_id if existing else f"short{len(self.accounts) + 1}",
            subscription_url=None,
        )
        self.accounts[(target, spec.email)] = acc
        return replace(acc)

    async def delete_account(self, target: int | None, email: str, uuid: str) -> None:
        self.calls.append(("delete", target, email))
        self._maybe_fail("delete_account")
        self.accounts.pop((target, email), None)

    async def get_links(self, account: RemoteAccount, client_headers: Mapping[str, str] | None = None) -> list[str]:
        self._maybe_fail("get_links")
        if self.kind != PanelKind.REMNAWAVE:
            return []
        return [f"vless://{account.uuid}@rw.example.com:443?type=tcp&security=reality#RW"]

    async def reset_devices(self, account: RemoteAccount) -> None:
        self.calls.append(("reset_devices", None, account.email))

    async def reset_traffic(self, target: int | None, email: str) -> None:
        self.calls.append(("reset_traffic", target, email))
        self._maybe_fail("reset_traffic")
        account = self.accounts.get((target, email))
        if account is not None:
            account.used_bytes = 0


@dataclass
class FakeCapablePanel(FakePanel):
    """:class:`FakePanel` with the optional capabilities: devices, server status, core restart, inbound creation."""

    devices: dict[str, list[DeviceInfo]] = field(default_factory=dict)  # account email -> devices
    status: ServerStatus = field(default_factory=lambda: ServerStatus(cpu_percent=5.0, core_state="running"))
    restarts: int = 0
    can_delete_device: bool = True

    async def list_devices(self, account: RemoteAccount) -> list[DeviceInfo]:
        self._maybe_fail("list_devices")
        return list(self.devices.get(account.email, []))

    async def delete_device(self, account: RemoteAccount, device_id: str) -> None:
        self.calls.append(("delete_device", None, f"{account.email}:{device_id}"))
        self._maybe_fail("delete_device")
        self.devices[account.email] = [d for d in self.devices.get(account.email, []) if d.id != device_id]

    async def server_status(self) -> ServerStatus:
        self._maybe_fail("server_status")
        return self.status

    async def restart_core(self) -> None:
        self.calls.append(("restart_core", None, ""))
        self._maybe_fail("restart_core")
        self.restarts += 1

    async def create_reality_inbound(
        self, port: int, sni: str, dest: str | None = None, remark: str = "", flow: str = "xtls-rprx-vision"
    ) -> InboundInfo:
        self.calls.append(("create_reality_inbound", port, sni))
        self._maybe_fail("create_reality_inbound")
        if any(i.port == port for i in self.inbounds):
            raise PanelError(f"port {port} is already used")
        info = default_inbound(max((i.remote_id for i in self.inbounds), default=0) + 1, port)
        info.remark = remark or f"reality-{port}"
        info.params["sni"] = sni
        info.params["flow"] = flow
        self.inbounds.append(info)
        return info

    async def create_shadowsocks_inbound(
        self, port: int, method: str = "2022-blake3-aes-128-gcm", remark: str = ""
    ) -> InboundInfo:
        self.calls.append(("create_shadowsocks_inbound", port, method))
        self._maybe_fail("create_shadowsocks_inbound")
        if any(i.port == port for i in self.inbounds):
            raise PanelError(f"port {port} is already used")
        info = InboundInfo(
            remote_id=max((i.remote_id for i in self.inbounds), default=0) + 1,
            remark=remark or f"ss-{port}",
            protocol="shadowsocks",
            port=port,
            enabled=True,
            params={"type": "tcp", "security": "none", "ss_method": method, "ss_password": "c2VydmVyLWtleS0xMjM0NQ=="},
        )
        self.inbounds.append(info)
        return info
