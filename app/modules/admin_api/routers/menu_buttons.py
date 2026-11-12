"""Custom main menu buttons: list, create, edit, delete, reorder; hiding built-in entries."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.permissions import Permission
from app.core.settings_provider import SettingValidationError
from app.db.models import MenuButton
from app.modules.admin_api.deps import Actor, bad_request, not_found, require
from app.modules.admin_api.schemas.common import Ok
from app.modules.admin_api.schemas.menu_buttons import (
    BuiltinButton,
    BuiltinsIn,
    MenuButtonIn,
    MenuButtonOut,
    MenuButtonUpdate,
    MoveIn,
    ReorderIn,
)
from app.services import menu_buttons
from app.services.menu_buttons import BUILTIN_KEYS, KIND_WEBAPP, MenuButtonError
from app.web.deps import ReadDb

router = APIRouter(prefix="/menu-buttons", tags=["admin: menu"])

Viewer = Annotated[Actor, Depends(require(Permission.VIEW_DASHBOARD))]
Editor = Annotated[Actor, Depends(require(Permission.TEXTS))]
SettingsEditor = Annotated[Actor, Depends(require(Permission.TEXTS, Permission.SETTINGS))]


def _webapp_guard(actor: Actor, *kinds: str | None) -> None:
    """A web-app button hands the Telegram sign-in data of every visitor to its site: owners only."""
    if KIND_WEBAPP in kinds:
        actor.require(Permission.SETTINGS_SECRETS)


def _out(rows: list[MenuButton]) -> list[MenuButtonOut]:
    return [MenuButtonOut.model_validate(b) for b in rows]


def _invalid(exc: MenuButtonError) -> Exception:
    return bad_request("invalid_button", str(exc))


@router.get("", response_model=list[MenuButtonOut])
async def list_buttons(actor: Viewer, session: ReadDb) -> list[MenuButtonOut]:
    """Every custom button (inactive ones too) in display order."""
    return _out(await menu_buttons.list_all(session))


@router.get("/builtins", response_model=list[BuiltinButton])
async def builtins(actor: Viewer) -> list[BuiltinButton]:
    """Built-in menu entries and whether the owner hid them."""
    hidden = set(actor.ctx.settings.menu_hidden_buttons)
    return [BuiltinButton(key=key, hidden=key in hidden) for key in BUILTIN_KEYS]


@router.put("/builtins", response_model=list[BuiltinButton])
async def set_builtins(body: BuiltinsIn, actor: SettingsEditor) -> list[BuiltinButton]:
    unknown = sorted(set(body.hidden) - set(BUILTIN_KEYS))
    if unknown:
        raise bad_request("invalid_button", f"unknown built-in buttons: {', '.join(unknown)}")
    hidden = [key for key in BUILTIN_KEYS if key in body.hidden]
    try:
        await actor.ctx.settings_provider.set("menu_hidden_buttons", hidden, actor.tg_id, source="web", ip=actor.ip)
    except SettingValidationError as exc:
        raise bad_request("invalid_setting", str(exc)) from None
    return [BuiltinButton(key=key, hidden=key in hidden) for key in BUILTIN_KEYS]


@router.get("/{button_id}", response_model=MenuButtonOut)
async def get_button(button_id: int, actor: Viewer, session: ReadDb) -> MenuButtonOut:
    button = await menu_buttons.get(session, button_id)
    if button is None:
        raise not_found("Button not found")
    return MenuButtonOut.model_validate(button)


@router.post("", response_model=MenuButtonOut, status_code=201)
async def create_button(body: MenuButtonIn, actor: Editor) -> MenuButtonOut:
    _webapp_guard(actor, body.kind)
    factory = actor.ctx.session_factory
    async with factory.begin() as session:
        try:
            button = await menu_buttons.create(session, **body.model_dump())
        except MenuButtonError as exc:
            raise _invalid(exc) from None
        await actor.audit(session, "menu_button.create", "menu_button", button.id, kind=button.kind, title=button.title)
        out = MenuButtonOut.model_validate(button)
    menu_buttons.invalidate(factory)
    return out


@router.patch("/{button_id}", response_model=MenuButtonOut)
async def update_button(button_id: int, body: MenuButtonUpdate, actor: Editor) -> MenuButtonOut:
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    if not data:
        raise bad_request("nothing_to_change", "Nothing to change")
    factory = actor.ctx.session_factory
    async with factory.begin() as session:
        button = await menu_buttons.get(session, button_id, lock=True)
        if button is None:
            raise not_found("Button not found")
        _webapp_guard(actor, button.kind, data.get("kind"))
        try:
            await menu_buttons.update(session, button, **data)
        except MenuButtonError as exc:
            raise _invalid(exc) from None
        await actor.audit(session, "menu_button.update", "menu_button", button.id, fields=sorted(data))
        out = MenuButtonOut.model_validate(button)
    menu_buttons.invalidate(factory)
    return out


@router.delete("/{button_id}", response_model=Ok)
async def delete_button(button_id: int, actor: Editor) -> Ok:
    factory = actor.ctx.session_factory
    async with factory.begin() as session:
        button = await menu_buttons.get(session, button_id, lock=True)
        if button is None:
            raise not_found("Button not found")
        await actor.audit(session, "menu_button.delete", "menu_button", button.id, title=button.title)
        await menu_buttons.delete(session, button)
    menu_buttons.invalidate(factory)
    return Ok()


@router.post("/reorder", response_model=list[MenuButtonOut])
async def reorder(body: ReorderIn, actor: Editor) -> list[MenuButtonOut]:
    """Set the row and position of the listed buttons."""
    factory = actor.ctx.session_factory
    async with factory.begin() as session:
        try:
            rows = await menu_buttons.reorder(session, [(p.id, p.row, p.sort_order) for p in body.items])
        except MenuButtonError as exc:
            raise _invalid(exc) from None
        await actor.audit(session, "menu_button.reorder", "menu_button", None, ids=[p.id for p in body.items])
        out = _out(rows)
    menu_buttons.invalidate(factory)
    return out


@router.post("/{button_id}/move", response_model=list[MenuButtonOut])
async def move(button_id: int, body: MoveIn, actor: Editor) -> list[MenuButtonOut]:
    """Move a button one place up or down (into the neighbouring row at a row boundary)."""
    factory = actor.ctx.session_factory
    async with factory.begin() as session:
        button = await menu_buttons.get(session, button_id)
        if button is None:
            raise not_found("Button not found")
        rows = await menu_buttons.move(session, button, -1 if body.direction == "up" else 1)
        await actor.audit(session, "menu_button.move", "menu_button", button_id, direction=body.direction)
        out = _out(rows)
    menu_buttons.invalidate(factory)
    return out
