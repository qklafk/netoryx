"""Bot texts: every i18n key with its default and admin override per language; edit with validation; reset."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from app.core.i18n import MAX_KEY_LENGTH, TextValidationError, render, validate_template
from app.core.permissions import Permission
from app.modules.admin_api.deps import Actor, bad_request, not_found, require
from app.modules.admin_api.schemas.common import Ok, Page
from app.modules.admin_api.schemas.texts import ModuleTexts, PreviewIn, PreviewOut, TextIn, TextOut
from app.web.deps import PageParams

router = APIRouter(prefix="/texts", tags=["admin: texts"])

Editor = Annotated[Actor, Depends(require(Permission.TEXTS))]
Key = Annotated[str, Path(min_length=1, max_length=MAX_KEY_LENGTH, pattern=r"^[A-Za-z0-9_.\-]+$")]
Lang = Annotated[str, Path(min_length=2, max_length=8, pattern=r"^[a-z]{2,8}$")]


def _i18n(actor: Actor) -> Any:
    i18n = getattr(actor.ctx, "i18n", None)
    if i18n is None:
        raise bad_request("unavailable", "Texts are not loaded")
    return i18n


def _languages(i18n: Any) -> tuple[str, ...]:
    return tuple(getattr(i18n, "languages", ("ru", "en")))


def _out(i18n: Any, info: Any) -> TextOut:
    langs = _languages(i18n)
    return TextOut(
        key=info.key,
        module=info.module,
        placeholders=sorted(info.placeholders),
        default={lang: i18n.default(info.key, lang) for lang in langs},
        override={lang: i18n.override(info.key, lang) for lang in langs},
        effective={lang: i18n.template(info.key, lang) for lang in langs},
    )


def _find(i18n: Any, key: str) -> Any:
    if not i18n.has(key):
        raise not_found("Unknown text key")
    module = key.split(".", 1)[0]
    for info in i18n.keys(module if module else None):
        if info.key == key:
            return info
    for info in i18n.keys(None):
        if info.key == key:
            return info
    raise not_found("Unknown text key")


@router.get("/modules", response_model=list[ModuleTexts])
async def modules(actor: Editor) -> list[ModuleTexts]:
    """Text groups (the core and each feature module) with how many texts are customized."""
    i18n = _i18n(actor)
    langs = _languages(i18n)
    stats: dict[str, list[int]] = {}
    for info in i18n.keys(None):
        entry = stats.setdefault(info.module, [0, 0])
        entry[0] += 1
        if any(i18n.override(info.key, lang) is not None for lang in langs):
            entry[1] += 1
    return [ModuleTexts(module=m, keys=k, overridden=o) for m, (k, o) in sorted(stats.items())]


@router.get("", response_model=Page[TextOut])
async def list_texts(
    actor: Editor,
    page: PageParams,
    module: Annotated[str | None, Query(max_length=64)] = None,
    q: Annotated[str | None, Query(max_length=200, description="Search in keys and texts")] = None,
    overridden: bool | None = None,
) -> Page[TextOut]:
    i18n = _i18n(actor)
    langs = _languages(i18n)
    needle = (q or "").strip().lower()
    items = []
    for info in i18n.keys(module):
        is_overridden = any(i18n.override(info.key, lang) is not None for lang in langs)
        if overridden is not None and overridden != is_overridden:
            continue
        if needle:
            haystack = [info.key, *(i18n.template(info.key, lang) or "" for lang in langs)]
            if not any(needle in part.lower() for part in haystack):
                continue
        items.append(info)
    window = items[page.offset : page.offset + page.limit]
    return Page(items=[_out(i18n, i) for i in window], total=len(items), limit=page.limit, offset=page.offset)


@router.get("/{key}", response_model=TextOut)
async def get_text(key: Key, actor: Editor) -> TextOut:
    i18n = _i18n(actor)
    return _out(i18n, _find(i18n, key))


@router.put("/{key}/{lang}", response_model=TextOut)
async def set_text(key: Key, lang: Lang, body: TextIn, actor: Editor) -> TextOut:
    """Save a custom text (only the key's placeholders and Telegram-safe HTML are accepted)."""
    i18n = _i18n(actor)
    info = _find(i18n, key)
    try:
        await i18n.set_override(key, lang, body.value, actor.tg_id, source="web", ip=actor.ip)
    except TextValidationError as exc:
        raise bad_request("invalid_text", str(exc)) from None
    except KeyError:
        raise not_found("Unknown text key") from None
    return _out(i18n, info)


@router.delete("/{key}/{lang}", response_model=TextOut)
async def reset_text(key: Key, lang: Lang, actor: Editor) -> TextOut:
    """Go back to the built-in text."""
    i18n = _i18n(actor)
    info = _find(i18n, key)
    await i18n.reset_override(key, lang, actor.tg_id, source="web", ip=actor.ip)
    return _out(i18n, info)


@router.post("/{key}/{lang}/preview", response_model=PreviewOut)
async def preview(key: Key, lang: Lang, body: PreviewIn, actor: Editor) -> PreviewOut:
    """Validate a text and render it with sample values (unknown placeholders show as ``{name}``)."""
    i18n = _i18n(actor)
    info = _find(i18n, key)
    template = body.value if body.value is not None else i18n.template(key, lang)
    if template is None:
        return PreviewOut(ok=False, error="No text in this language", placeholders=sorted(info.placeholders))
    try:
        validate_template(template, info.placeholders)
    except TextValidationError as exc:
        return PreviewOut(ok=False, error=str(exc), placeholders=sorted(info.placeholders))
    values = {name: body.values.get(name, "{" + name + "}") for name in info.placeholders}
    return PreviewOut(ok=True, rendered=render(template, values), placeholders=sorted(info.placeholders))


@router.post("/reload", response_model=Ok)
async def reload(actor: Editor) -> Ok:
    """Re-read custom texts from the database (after changes made by another process)."""
    await _i18n(actor).reload_overrides()
    return Ok()
