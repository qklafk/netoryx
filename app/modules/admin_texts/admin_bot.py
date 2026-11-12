"""Bot texts: find any text by key or wording, edit it per language (placeholders and HTML are validated), preview,
reset to the built-in version."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from app.core.context import AppContext
from app.core.i18n import KeyInfo, TextValidationError, Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, answer_callback, render, send_screen
from app.keyboards import Button, back, kb, paginate
from app.modules.admin.common import AdminInputCb, admin_input_group, admin_router, input_button

log = logging.getLogger(__name__)

router = admin_router("texts_admin")
router.message.filter(HasPermission(Permission.TEXTS))
router.callback_query.filter(HasPermission(Permission.TEXTS))

PAGE_SIZE = 10
MAX_RESULTS = 200
PREVIEW_CHARS = 1400
SEARCH = "s"  # list sources kept in the FSM data
CHANGED = "c"
BACK_TO_KEY = "tx_back"  # keeps the edit state (see app.modules.admin.common)


class TxCb(CallbackData, prefix="atxt"):
    """``a`` = action; ``m`` = module or list source; ``i`` = index of the key in that list; ``p`` = page;
    ``l`` = language."""

    a: str
    m: str = ""
    i: int = 0
    p: int = 0
    l: str = ""  # noqa: E741 - one letter keeps callback data short


@admin_input_group
class TextStates(StatesGroup):
    query = State()
    value = State()


def _cb(action: str, module: str = "", index: int = 0, page: int = 0, lang: str = "") -> TxCb:
    return TxCb(a=action, m=module, i=index, p=page, l=lang)


def _clip(text: str, limit: int = PREVIEW_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _modules(ctx: AppContext) -> list[str]:
    return sorted({info.module for info in ctx.i18n.keys(None)})


def search_keys(ctx: AppContext, query: str) -> list[str]:
    """Keys whose name, built-in text or override (any language) contains ``query`` (case-insensitive)."""
    needle = query.strip().lower()
    if not needle:
        return []
    found: list[str] = []
    for info in ctx.i18n.keys(None):
        haystack = [info.key, info.default_ru or "", info.default_en or ""]
        haystack += [ctx.i18n.override(info.key, lang) or "" for lang in ctx.i18n.languages]
        if any(needle in item.lower() for item in haystack):
            found.append(info.key)
            if len(found) >= MAX_RESULTS:
                break
    return found


def changed_keys(ctx: AppContext) -> list[str]:
    return [
        info.key
        for info in ctx.i18n.keys(None)
        if any(ctx.i18n.override(info.key, lang) is not None for lang in ctx.i18n.languages)
    ]


async def _resolve(ctx: AppContext, state: FSMContext, source: str, index: int) -> str | None:
    """The key behind a list button: from a saved search / changed list, or the n-th key of a module."""
    if source in (SEARCH, CHANGED):
        keys = (await state.get_data()).get(f"keys_{source}") or []
        return str(keys[index]) if 0 <= index < len(keys) else None
    infos = ctx.i18n.keys(source)
    return infos[index].key if 0 <= index < len(infos) else None


def _info(ctx: AppContext, key: str) -> KeyInfo | None:
    return next((info for info in ctx.i18n.keys(None) if info.key == key), None)


# --- screens ----------------------------------------------------------------------------------------------------------
def home_screen(ctx: AppContext, _: Translator) -> Screen:
    changed = len(changed_keys(ctx))
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        [Button.cb(_("admin_texts.btn.search"), _cb("find"), style="primary")],
        [Button.cb(_("admin_texts.btn.modules"), _cb("mods"))],
        [Button.cb(_("admin_texts.btn.changed", count=changed), _cb("chg")) if changed else None],
        [back("admin", _)],
    ]
    return Screen(_("admin_texts.home", total=len(ctx.i18n.keys(None))), kb(rows))


def list_screen(_: Translator, keys: list[str], source: str, page: int, title: str, back_to: TxCb) -> Screen:
    indexed = list(enumerate(keys))
    page_data = paginate(
        indexed,
        page,
        PAGE_SIZE,
        lambda item: Button.cb(_clip(item[1], 60), _cb("key", source, item[0], page)),
        lambda p: _cb("list", source, page=p),
    )
    rows: list[Sequence[InlineKeyboardButton | None]] = [*page_data.rows, [Button.cb(_("nav.back"), back_to)]]
    return Screen(title, kb(rows))


def key_screen(ctx: AppContext, _: Translator, key: str, back_to: TxCb, note: str | None = None) -> Screen:
    info = _info(ctx, key)
    if info is None:
        return Screen(_("admin_texts.not_found"), kb([[Button.cb(_("nav.back"), _cb("home"))]]))
    blocks = []
    for lang in ctx.i18n.languages:
        override = ctx.i18n.override(key, lang)
        current = override if override is not None else (ctx.i18n.default(key, lang) or "")
        marker = _("admin_texts.changed") if override is not None else _("admin_texts.builtin")
        blocks.append(_("admin_texts.lang_block", lang=lang.upper(), marker=marker, text=_.esc(_clip(current))))
    placeholders = ", ".join(f"{{{name}}}" for name in sorted(info.placeholders)) or _("admin_texts.no_placeholders")
    text = _(
        "admin_texts.key",
        key=_.esc(key),
        module=_.esc(info.module),
        placeholders=_.esc(placeholders),
        texts="\n\n".join(blocks),
    )
    if note:
        text += "\n\n" + note
    edit_row = [
        Button.cb(_("admin_texts.btn.edit", lang=lang.upper()), _cb("edit", lang=lang)) for lang in ctx.i18n.languages
    ]
    preview_row = [
        Button.cb(_("admin_texts.btn.preview", lang=lang.upper()), _cb("prev", lang=lang))
        for lang in ctx.i18n.languages
    ]
    reset_row = [
        Button.cb(_("admin_texts.btn.reset", lang=lang.upper()), _cb("reset", lang=lang))
        for lang in ctx.i18n.languages
        if ctx.i18n.override(key, lang) is not None
    ]
    rows: list[Sequence[InlineKeyboardButton | None]] = [
        edit_row,
        preview_row,
        reset_row,
        [Button.cb(_("nav.back"), back_to)],
    ]
    return Screen(_clip(text, 4000), kb(rows))


async def _current_key(state: FSMContext) -> tuple[str | None, TxCb]:
    data = await state.get_data()
    key = data.get("key")
    back_to = TxCb.unpack(str(data["back"])) if data.get("back") else _cb("home")
    return (str(key) if key else None), back_to


# --- navigation -------------------------------------------------------------------------------------------------------
@router.callback_query(TxCb.filter(F.a == "home"))
async def open_home(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    await state.clear()
    await render(callback, home_screen(ctx, _))


@router.callback_query(TxCb.filter(F.a == "mods"))
async def open_modules(callback: CallbackQuery, callback_data: TxCb, ctx: AppContext, _: Translator) -> None:
    names = _modules(ctx)
    page_data = paginate(
        names,
        callback_data.p,
        PAGE_SIZE * 2,
        lambda name: Button.cb(f"{name} ({len(ctx.i18n.keys(name))})", _cb("list", name)),
        lambda p: _cb("mods", page=p),
        columns=2,
    )
    rows: list[Sequence[InlineKeyboardButton | None]] = [*page_data.rows, [Button.cb(_("nav.back"), _cb("home"))]]
    await render(callback, Screen(_("admin_texts.modules"), kb(rows)))


@router.callback_query(TxCb.filter(F.a == "chg"))
async def open_changed(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    keys = changed_keys(ctx)
    await state.update_data(keys_c=keys)
    await render(
        callback, list_screen(_, keys, CHANGED, 0, _("admin_texts.changed_list", count=len(keys)), _cb("home"))
    )


@router.callback_query(TxCb.filter(F.a == "list"))
async def open_list(
    callback: CallbackQuery, callback_data: TxCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    source = callback_data.m
    if source in (SEARCH, CHANGED):
        keys = [str(k) for k in (await state.get_data()).get(f"keys_{source}") or []]
        title = (
            _("admin_texts.results", count=len(keys))
            if source == SEARCH
            else _("admin_texts.changed_list", count=len(keys))
        )
        back_to = _cb("home")
    else:
        keys = [info.key for info in ctx.i18n.keys(source)]
        title = _("admin_texts.module", module=_.esc(source), count=len(keys))
        back_to = _cb("mods")
    await render(callback, list_screen(_, keys, source, callback_data.p, title, back_to))


@router.callback_query(TxCb.filter(F.a == "key"))
async def open_key(
    callback: CallbackQuery, callback_data: TxCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    key = await _resolve(ctx, state, callback_data.m, callback_data.i)
    if key is None:
        await answer_callback(callback, _("admin_texts.list_expired"), alert=True)
        return
    back_to = _cb("list", callback_data.m, page=callback_data.p)
    await state.update_data(key=key, back=back_to.pack())
    await render(callback, key_screen(ctx, _, key, back_to))


# --- search -----------------------------------------------------------------------------------------------------------
@router.callback_query(TxCb.filter(F.a == "find"))
async def ask_query(callback: CallbackQuery, _: Translator, state: FSMContext) -> None:
    await state.set_state(TextStates.query)
    await render(callback, Screen(_("admin_texts.ask_query"), kb([[Button.cb(_("nav.cancel"), _cb("home"))]])))


@router.message(TextStates.query, F.text)
async def got_query(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    query = (message.text or "").strip()
    keys = search_keys(ctx, query)
    if not keys:
        await render(
            message, Screen(_("admin_texts.nothing", query=_.esc(query)), kb([[Button.cb(_("nav.back"), _cb("home"))]]))
        )
        return
    await state.set_state(None)
    await state.update_data(keys_s=keys)
    if len(keys) == 1:
        back_to = _cb("home")
        await state.update_data(key=keys[0], back=back_to.pack())
        await render(message, key_screen(ctx, _, keys[0], back_to))
        return
    await render(message, list_screen(_, keys, SEARCH, 0, _("admin_texts.results", count=len(keys)), _cb("home")))


# --- edit / preview / reset -------------------------------------------------------------------------------------------
@router.callback_query(TxCb.filter(F.a == "edit"))
async def ask_value(
    callback: CallbackQuery, callback_data: TxCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    key, _back = await _current_key(state)
    if key is None or callback_data.l not in ctx.i18n.languages:
        await answer_callback(callback, _("admin_texts.list_expired"), alert=True)
        return
    await state.set_state(TextStates.value)
    await state.update_data(lang=callback_data.l)
    current = ctx.i18n.template(key, callback_data.l) or ""
    text = _("admin_texts.ask_value", lang=callback_data.l.upper(), key=_.esc(key), current=_.esc(_clip(current, 3000)))
    await render(callback, Screen(_clip(text, 4000), kb([[input_button(_("nav.back"), BACK_TO_KEY)]])))


@router.callback_query(AdminInputCb.filter(F.action == BACK_TO_KEY))
async def back_to_key(callback: CallbackQuery, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    key, back_to = await _current_key(state)
    await state.set_state(None)
    if key is None:
        await render(callback, home_screen(ctx, _))
        return
    await render(callback, key_screen(ctx, _, key, back_to))


def _typed_text(message: Message) -> str:
    """The template the admin typed: raw text (tags typed by hand), or HTML of Telegram formatting if used."""
    if message.entities:
        return message.html_text
    return message.text or ""


@router.message(TextStates.value, F.text)
async def got_value(message: Message, ctx: AppContext, _: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    key, back_to = await _current_key(state)
    lang = str(data.get("lang") or "")
    if key is None or lang not in ctx.i18n.languages:
        await state.clear()
        await render(message, Screen(_("admin_texts.list_expired")))
        return
    value = _typed_text(message)
    try:
        await ctx.i18n.set_override(key, lang, value, message.chat.id)
    except TextValidationError as exc:
        await render(
            message,
            Screen(_("admin_texts.invalid", error=_.esc(exc)), kb([[input_button(_("nav.back"), BACK_TO_KEY)]])),
        )
        return
    except KeyError:
        await state.set_state(None)
        await render(message, Screen(_("admin_texts.not_found")))
        return
    await state.set_state(None)
    await render(message, key_screen(ctx, _, key, back_to, _("admin_texts.saved", lang=lang.upper())))


@router.callback_query(TxCb.filter(F.a == "prev"))
async def preview(
    callback: CallbackQuery, callback_data: TxCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    key, _back = await _current_key(state)
    template = ctx.i18n.template(key, callback_data.l) if key else None
    if not template:
        await answer_callback(callback, _("admin_texts.list_expired"), alert=True)
        return
    await answer_callback(callback)
    try:
        await send_screen(ctx.bot, callback.from_user.id, Screen(template))
    except TelegramBadRequest as exc:
        await send_screen(ctx.bot, callback.from_user.id, Screen(_("admin_texts.preview_failed", error=_.esc(exc))))


@router.callback_query(TxCb.filter(F.a == "reset"))
async def reset(
    callback: CallbackQuery, callback_data: TxCb, ctx: AppContext, _: Translator, state: FSMContext
) -> None:
    key, back_to = await _current_key(state)
    if key is None:
        await answer_callback(callback, _("admin_texts.list_expired"), alert=True)
        return
    existed = await ctx.i18n.reset_override(key, callback_data.l, callback.from_user.id)
    note = _("admin_texts.reset_done", lang=callback_data.l.upper()) if existed else None
    await render(callback, key_screen(ctx, _, key, back_to, note))
