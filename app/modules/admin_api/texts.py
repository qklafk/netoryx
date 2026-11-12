"""Texts the web admin sends to users through the bot (admin overrides via ``ctx.i18n``, else the bundled files)."""

from __future__ import annotations

import logging
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

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


def t(ctx: Any, key: str, lang: str | None, **params: Any) -> str:
    """Text for ``key`` in ``lang`` (values are inserted as given: escape them first)."""
    lang = lang if lang in ("ru", "en") else "ru"
    i18n = getattr(ctx, "i18n", None)
    if i18n is not None:
        try:
            text = i18n.t(key, lang, **params)
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
