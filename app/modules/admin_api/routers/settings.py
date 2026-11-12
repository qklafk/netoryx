"""Runtime settings: every editable setting grouped as declared in the registry, with validation on save.

Secrets are write-only: their value is never returned, only whether one is set.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import SecretStr, TypeAdapter

from app.config import Settings
from app.core.permissions import Permission
from app.core.settings_provider import SettingValidationError
from app.core.settings_registry import ENV_ONLY, GROUPS, REGISTRY, SettingSpec, by_group
from app.modules.admin_api.deps import Actor, bad_request, kick_jobs, not_found, require
from app.modules.admin_api.schemas.settings import EnvValueOut, GroupOut, SettingIn, SettingOut

router = APIRouter(prefix="/settings", tags=["admin: settings"])

Editor = Annotated[Actor, Depends(require(Permission.SETTINGS))]
Key = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")]

# Environment values that are safe to show (addresses and process layout, no credentials).
_VISIBLE_ENV = frozenset(
    {
        "public_base_url",
        "web_host",
        "web_port",
        "bot_mode",
        "role",
        "log_level",
        "log_json",
        "auto_migrate",
        "dev_mode",
        "backup_dir",
        "admin_ids",
        "shutdown_timeout_sec",
        "xui_host",
        "xui_public_host",
        "remnawave_url",
    }
)


def _plain(key: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, SecretStr):
        return None
    return TypeAdapter(Settings.model_fields[key].annotation).dump_python(value, mode="json")


def _is_set(value: Any) -> bool:
    if isinstance(value, SecretStr):
        return bool(value.get_secret_value())
    return value not in (None, "", [], {})


def _setting(actor: Actor, spec: SettingSpec, lang: str) -> SettingOut:
    provider = actor.ctx.settings_provider
    value, origin = provider.effective(spec.key)
    visible = actor.can(spec.permission)
    return SettingOut(
        key=spec.key,
        group=spec.group,
        type=spec.type,
        label=spec.label(lang),
        help=spec.help(lang),
        choices=list(spec.choices) if spec.choices else None,
        min=spec.min,
        max=spec.max,
        nullable=spec.nullable,
        secret=spec.secret,
        owner_only=spec.owner_only,
        restart_required=spec.restart_required,
        editable=visible and not spec.restart_required,
        value=None if spec.secret or not visible else _plain(spec.key, value),
        is_set=_is_set(value),
        origin=origin,
    )


def _spec(key: str) -> SettingSpec:
    spec = REGISTRY.get(key)
    if spec is None:
        raise not_found("Unknown or read-only setting")
    return spec


@router.get("", response_model=list[GroupOut])
async def list_settings(
    actor: Editor,
    lang: Annotated[str, Query(pattern=r"^(ru|en)$")] = "ru",
    group: Annotated[str | None, Query(max_length=64)] = None,
) -> list[GroupOut]:
    """All runtime settings by group with their current value and where it comes from (env, db, default)."""
    result = []
    for key, specs in by_group().items():
        if group is not None and key != group:
            continue
        title_ru, title_en = GROUPS[key]
        result.append(
            GroupOut(
                key=key,
                title=title_en if lang == "en" else title_ru,
                settings=[_setting(actor, spec, lang) for spec in specs],
            )
        )
    return result


@router.get("/env", response_model=list[EnvValueOut])
async def environment(actor: Editor) -> list[EnvValueOut]:
    """Values that can only be changed in the environment file (credentials are never shown)."""
    settings = actor.ctx.settings
    out = []
    for key in sorted(ENV_ONLY):
        value = getattr(settings, key, None)
        hidden = key not in _VISIBLE_ENV
        out.append(
            EnvValueOut(key=key, value=None if hidden else _plain(key, value), is_set=_is_set(value), hidden=hidden)
        )
    return out


def _after_change(actor: Actor, key: str) -> None:
    """Apply settings that live outside this process's memory (panels) without waiting for the next job run."""
    if key in ("adblock_enabled", "youtube_exit_enabled", "youtube_exit_link", "youtube_exit_domains"):
        kick_jobs(actor.ctx, "ad_blocking.converge")


@router.get("/{key}", response_model=SettingOut)
async def get_setting(key: Key, actor: Editor, lang: Annotated[str, Query(pattern=r"^(ru|en)$")] = "ru") -> SettingOut:
    return _setting(actor, _spec(key), lang)


@router.put("/{key}", response_model=SettingOut)
async def set_setting(
    key: Key, body: SettingIn, actor: Editor, lang: Annotated[str, Query(pattern=r"^(ru|en)$")] = "ru"
) -> SettingOut:
    """Validate and save a value (the same rules as the environment file); applies immediately."""
    spec = _spec(key)
    actor.require(spec.permission)
    if spec.restart_required:
        raise bad_request("read_only", "This setting is changed in the environment file and needs a restart")
    try:
        await actor.ctx.settings_provider.set(key, body.value, actor.tg_id, source="web", ip=actor.ip)
    except SettingValidationError as exc:
        raise bad_request("invalid_value", exc.message) from None
    except KeyError:
        raise not_found("Unknown or read-only setting") from None
    _after_change(actor, key)
    return _setting(actor, spec, lang)


@router.delete("/{key}", response_model=SettingOut)
async def reset_setting(
    key: Key, actor: Editor, lang: Annotated[str, Query(pattern=r"^(ru|en)$")] = "ru"
) -> SettingOut:
    """Remove the saved value: the environment value (or the default) applies again."""
    spec = _spec(key)
    actor.require(spec.permission)
    try:
        await actor.ctx.settings_provider.reset(key, actor.tg_id, source="web", ip=actor.ip)
    except SettingValidationError as exc:
        raise bad_request("invalid_value", exc.message) from None
    _after_change(actor, key)
    return _setting(actor, spec, lang)
