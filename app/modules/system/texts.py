"""Localized texts of the system module (through the app's i18n, falling back to the bundled files)."""

from __future__ import annotations

import logging
import tomllib
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

LOCALES_DIR = Path(__file__).parent / "locales"


@cache
def bundled(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{lang}.toml"
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def t(ctx: AppContext, key: str, lang: str, **params: Any) -> str:
    """Text for ``key``: admin overrides and locale files via ``ctx.i18n``, else the bundled default."""
    try:
        text = ctx.i18n.t(key, lang, **params)
    except Exception:
        log.exception("i18n lookup of %s failed", key)
        text = key
    if text != key:
        return str(text)
    template = bundled(lang).get(key) or bundled("ru").get(key)
    if template is None:
        return key
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template
