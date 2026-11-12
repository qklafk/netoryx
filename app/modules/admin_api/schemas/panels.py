"""Servers (panels), inbounds, locations and endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.db.models import EndpointKind, EndpointProfile, PanelKind
from app.modules.admin_api.schemas.common import OrmModel


def _url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError("the URL must start with http:// or https://")
    return value


# ---------------------------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------------------------
class PanelOut(BaseModel):
    id: int
    name: str
    kind: PanelKind
    url: str
    username: str | None
    has_password: bool
    has_api_token: bool
    tls_verify: bool
    options: dict[str, Any]
    is_active: bool
    healthy: bool
    version: str | None
    last_ok_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    created_at: datetime
    inbounds: int
    accounts: int


class PanelIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: PanelKind
    url: str = Field(min_length=8, max_length=512)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=1024)
    api_token: str | None = Field(default=None, max_length=4096)
    tls_verify: bool = True
    options: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    test: bool = Field(default=True, description="Connect to the panel before saving and refuse on failure")
    import_inbounds: bool = Field(default=True, description="3x-ui: import its inbounds right away")
    location_id: int | None = Field(default=None, description="Location of imported inbounds")
    endpoint_address: str | None = Field(
        default=None, max_length=255, description="Host clients connect to; creates a direct endpoint per inbound"
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return _url(value)


class PanelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: str | None = Field(default=None, min_length=8, max_length=512)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=1024, description="Write-only; empty string clears it")
    api_token: str | None = Field(default=None, max_length=4096, description="Write-only; empty string clears it")
    tls_verify: bool | None = None
    options: dict[str, Any] | None = Field(default=None, description="Header/cookie values shown as *** are kept")
    is_active: bool | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return _url(value)


class PanelTestIn(BaseModel):
    """Connection test for a new panel, or for an existing one (``panel_id``) with optional changed fields."""

    panel_id: int | None = None
    kind: PanelKind | None = None
    url: str | None = Field(default=None, max_length=512)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=1024)
    api_token: str | None = Field(default=None, max_length=4096)
    tls_verify: bool | None = None
    options: dict[str, Any] | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return _url(value)


class InboundInfoOut(BaseModel):
    remote_id: int
    remark: str
    protocol: str
    port: int
    enabled: bool
    network: str | None
    security: str | None


class SquadOut(BaseModel):
    uuid: str
    name: str


class PanelTestOut(BaseModel):
    ok: bool
    version: str | None = None
    error: str | None = None
    inbounds: list[InboundInfoOut] = Field(default_factory=list)
    squads: list[SquadOut] = Field(default_factory=list)


class PanelDeleteOut(BaseModel):
    deleted: bool
    accounts_removed: int
    errors: list[str]


class RefreshIn(BaseModel):
    activate_new: bool = False
    location_id: int | None = None
    endpoint_address: str | None = Field(default=None, max_length=255)


class RefreshOut(BaseModel):
    created: list[int]
    updated: list[int]
    deactivated: list[int]
    disabled_on_panel: list[int]
    skipped_remote_ids: list[int]


class SyncNowOut(BaseModel):
    marked: int
    started_jobs: list[str] = Field(default_factory=list)


class ServerStatusOut(BaseModel):
    supported: bool
    status: dict[str, Any] | None = None
    error: str | None = None


class AdBlockingOut(BaseModel):
    supported: bool
    wanted: bool = False  # the ADBLOCK_ENABLED setting
    enabled: bool | None = None  # what the panel's Xray template does now (None: unknown)
    error: str | None = None  # live read error, else the last convergence error
    checked_at: str | None = None
    youtube_wanted: bool = False  # YOUTUBE_EXIT_ENABLED with a usable link
    youtube_synced: bool | None = None  # the template matches the YouTube exit setting (None: unknown)
    sniffing_fixed: int = 0  # inbounds switched to domain sniffing by the last run


# ---------------------------------------------------------------------------------------------
# Inbounds
# ---------------------------------------------------------------------------------------------
class InboundOut(BaseModel):
    id: int
    panel_id: int
    remote_id: int
    location_id: int | None
    name: str
    protocol: str | None
    port: int | None
    network: str | None
    security: str | None
    flow: str
    is_active: bool
    sort_order: int
    tg_proxy: bool | None  # None: not an MTProto inbound
    tg_socks: bool | None = None  # None: not a mixed (SOCKS5) inbound
    ss_method: str | None = None  # Shadowsocks inbounds: the cipher
    unsupported_reason: str | None = None  # Shadowsocks inbounds that cannot serve subscriptions: why
    socks_auth: str | None = None  # mixed inbounds: the panel's auth mode ("password" is enforced when serving)
    params_updated_at: datetime | None
    endpoints: int


class InboundUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    location_id: int | None = None
    clear_location: bool = False
    flow: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None
    sort_order: int | None = None
    tg_proxy: bool | None = Field(default=None, description="MTProto inbounds: serve the Telegram proxy")
    tg_socks: bool | None = Field(
        default=None,
        description="Mixed (SOCKS5) inbounds: serve the personal Telegram SOCKS5 proxy "
        "(the inbound's account list is managed by the bot and its auth is forced to password)",
    )


class RealityInboundIn(BaseModel):
    port: int = Field(ge=1, le=65535)
    sni: str = Field(min_length=3, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")
    dest: str | None = Field(default=None, max_length=260, description="Defaults to <sni>:443")
    remark: str = Field(default="", max_length=64)
    location_id: int | None = None
    endpoint_address: str | None = Field(
        default=None, max_length=255, description="Host clients connect to (a direct endpoint is created)"
    )
    activate: bool = True


class ShadowsocksInboundIn(BaseModel):
    port: int = Field(ge=1, le=65535)
    method: Literal["2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm"] = "2022-blake3-aes-128-gcm"
    remark: str = Field(default="", max_length=64)
    location_id: int | None = None
    endpoint_address: str | None = Field(
        default=None, max_length=255, description="Host clients connect to (a direct endpoint is created)"
    )
    activate: bool = True


class SniCandidate(BaseModel):
    sni: str
    note: str


class SniCheckIn(BaseModel):
    sni: str = Field(min_length=3, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")
    port: int = Field(default=443, ge=1, le=65535)


class SniCheckOut(BaseModel):
    sni: str
    reachable: bool
    error: str | None


# ---------------------------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------------------------
class LocationOut(OrmModel):
    id: int
    name: str
    flag: str
    sort_order: int
    is_active: bool


class LocationIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    flag: str = Field(default="", max_length=16)
    sort_order: int | None = None
    is_active: bool = True


class LocationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    flag: str | None = Field(default=None, max_length=16)
    sort_order: int | None = None
    is_active: bool | None = None


class ReorderIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=1000)


# ---------------------------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------------------------
class EndpointOut(BaseModel):
    id: int
    inbound_id: int
    panel_id: int
    location_id: int | None
    kind: EndpointKind
    profile: EndpointProfile
    address: str
    port: int | None
    overrides: dict[str, Any]
    remark: str | None
    description: str | None
    sort_order: int
    is_active: bool
    last_ok_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    auto_hidden: bool


class EndpointIn(BaseModel):
    inbound_id: int
    location_id: int | None = None
    kind: EndpointKind = EndpointKind.DIRECT
    profile: EndpointProfile = EndpointProfile.STANDARD
    address: str = Field(min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    overrides: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=64)
    sort_order: int = 0
    is_active: bool = True


class EndpointUpdate(BaseModel):
    location_id: int | None = None
    clear_location: bool = False
    kind: EndpointKind | None = None
    profile: EndpointProfile | None = None
    address: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    clear_port: bool = False
    overrides: dict[str, Any] | None = None
    remark: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=64)
    sort_order: int | None = None
    is_active: bool | None = None


class EndpointPreset(BaseModel):
    key: str
    title_ru: str
    title_en: str
    kind: EndpointKind
    profile: EndpointProfile
    overrides: dict[str, Any]
    hint_ru: str
    hint_en: str


class PreviewIn(BaseModel):
    """A link preview for an existing endpoint (``endpoint_id``) or for a draft."""

    endpoint_id: int | None = None
    draft: EndpointIn | None = None


class PreviewOut(BaseModel):
    ok: bool
    link: str | None = None
    address: str | None = None
    port: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ProbeOut(BaseModel):
    ok: bool
    error: str | None
    checked_at: datetime
