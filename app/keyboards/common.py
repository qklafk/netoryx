"""Keyboards used by the core (access screens, confirmations, language picker)."""

from __future__ import annotations

from collections.abc import Iterable

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup

from app.keyboards.builders import Btn, Button, Translate, kb, to_menu
from app.keyboards.callbacks import Nav


def channel_gate_kb(_: Translate, channel_url: str, check: CallbackData | str) -> InlineKeyboardMarkup:
    """Link to the channel + the "I've joined" re-check button."""
    return kb([[Button.url(_("access.channel.subscribe"), channel_url)], [Button.cb(_("access.channel.check"), check)]])


def consent_kb(
    _: Translate,
    accept: CallbackData | str,
    *,
    terms_url: str | None = None,
    privacy_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Links to the legal documents + "Accept"."""
    return kb(
        [
            [
                Button.url(_("consent.terms"), terms_url) if terms_url else None,
                Button.url(_("consent.privacy"), privacy_url) if privacy_url else None,
            ],
            [Button.cb(_("consent.accept"), accept, style="success")],
        ]
    )


def confirm_kb(
    _: Translate,
    yes: CallbackData | str,
    no: CallbackData | str,
    *,
    danger: bool = False,
) -> InlineKeyboardMarkup:
    """Yes / No; ``danger`` paints "Yes" red for destructive actions."""
    return kb([[Button.cb(_("common.yes"), yes, style="danger" if danger else None), Button.cb(_("common.no"), no)]])


def language_kb(
    _: Translate,
    languages: Iterable[str],
    make_data: type[CallbackData] | None = None,
    *,
    current: str | None = None,
) -> InlineKeyboardMarkup:
    """One button per language (``lang.<code>`` label, current marked); callback ``make_data(lang=code)`` or
    ``Nav(to="lang", arg=code)``."""
    rows: list[list[Btn]] = []
    for code in languages:
        label = _(f"lang.{code}")
        if code == current:
            label = f"✓ {label}"
        data: CallbackData = make_data(lang=code) if make_data is not None else Nav(to="lang", arg=code)
        rows.append([Button.cb(label, data)])
    rows.append([to_menu(_)])
    return kb(rows)
