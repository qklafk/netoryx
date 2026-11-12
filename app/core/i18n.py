"""Localized bot texts.

Strings live in TOML files: ``app/locales/<lang>/*.toml`` for the core and ``app/modules/<name>/locales/<lang>.toml``
for feature modules (module keys start with ``<name>.``). Tables are flattened into dotted keys. Values are
Telegram HTML with named ``{placeholders}``; ``{{`` and ``}}`` are literal braces.

Admins can override any text per language (``TextOverride`` rows). Overrides are validated: only the placeholders
the default text knows about and only the HTML subset Telegram accepts.

Rendering never escapes values: callers pass pre-escaped strings (see :meth:`I18n.esc`).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import string
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import TextOverride
from app.db.session import SessionFactory, read_session
from app.services import audit, kv
from app.services.users import LANGUAGE_FALLBACKS, LANGUAGES
from app.utils.security import random_token

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
LOCALES_DIR = APP_DIR / "locales"
MODULES_DIR = APP_DIR / "modules"

CORE_MODULE = "core"
VERSION_KEY = "texts_version"
MAX_TEXT_LENGTH = 4096
MAX_KEY_LENGTH = 128  # TextOverride.key column
PLURAL_FORMS = ("one", "few", "many", "other")
REQUIRED_PLURAL_FORMS: Mapping[str, tuple[str, ...]] = {
    "ru": ("one", "few", "many"),
    "uk": ("one", "few", "many"),
    "en": ("one", "other"),
    "kk": ("one", "other"),
    "uz": ("one", "other"),
    "fa": ("one", "other"),
    "zh": ("other",),
}
# Languages that must define every key; the others are translations whose missing texts fall back.
COMPLETE_LANGUAGES = ("ru", "en")
# Where a translation looks for a text it lacks before the default language.
FALLBACKS: Mapping[str, str] = LANGUAGE_FALLBACKS
RTL_LANGUAGES = frozenset({"fa"})

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RENDER = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


class LocaleError(Exception):
    """Locale files are broken (syntax, duplicate keys, invalid templates)."""


class TextValidationError(ValueError):
    """A text template is not acceptable (placeholders or HTML)."""


@dataclass(frozen=True, slots=True)
class KeyInfo:
    """A translatable key as shown in the admin text editor."""

    key: str
    placeholders: frozenset[str]
    default_ru: str | None
    default_en: str | None
    module: str


# --- template validation ---------------------------------------------------------------------------------------------


def placeholders(template: str) -> frozenset[str]:
    """Names of the ``{placeholders}`` in ``template``.

    Only plain named fields are allowed: no positional ``{}``/``{0}``, attribute or index access, conversions or
    format specs (values are formatted by the caller). Raises :class:`TextValidationError` otherwise.
    """
    names: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise TextValidationError(f"unbalanced braces: {exc}") from None
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if not _IDENTIFIER.fullmatch(field):
            raise TextValidationError(f"invalid placeholder {{{field}}}: use a plain name like {{amount}}")
        if conversion or spec:
            raise TextValidationError(f"placeholder {{{field}}} must not have a conversion or format spec")
        names.add(field)
    return frozenset(names)


# Telegram HTML subset: tag -> allowed attributes.
_ALLOWED_TAGS: Mapping[str, frozenset[str]] = {
    "b": frozenset(),
    "strong": frozenset(),
    "i": frozenset(),
    "em": frozenset(),
    "u": frozenset(),
    "ins": frozenset(),
    "s": frozenset(),
    "strike": frozenset(),
    "del": frozenset(),
    "a": frozenset({"href"}),
    "code": frozenset({"class"}),
    "pre": frozenset(),
    "blockquote": frozenset({"expandable"}),
    "tg-spoiler": frozenset(),
    "span": frozenset({"class"}),
    "tg-emoji": frozenset({"emoji-id"}),
}
_LINK_SCHEMES = ("http://", "https://", "tg://", "mailto:")
_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s+[A-Za-z-]+(?:\s*=\s*(?:\"[^\"<>]*\"|'[^'<>]*'))?)*)\s*>")
_ATTR = re.compile(r"([A-Za-z-]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'))?")
_ENTITY = re.compile(r"&(?:lt|gt|amp|quot|#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6});")


def check_html(text: str) -> None:
    """Raise :class:`TextValidationError` unless ``text`` is HTML that Telegram will accept.

    Allowed: ``b strong i em u ins s strike del a[href] code[class] pre blockquote[expandable] tg-spoiler
    span[class=tg-spoiler] tg-emoji[emoji-id]``, properly nested; ``<`` and ``&`` outside tags must be entities
    (``&lt; &gt; &amp; &quot;`` or numeric); Telegram treats a bare ``>`` as text.
    """
    stack: list[str] = []
    pos = 0
    while pos < len(text):
        ch = text[pos]
        if ch == "<":
            match = _TAG.match(text, pos)
            if match is None:
                raise TextValidationError(f"unexpected '<' at position {pos}: write &lt; for a literal '<'")
            closing, name, attrs = match.group(1) == "/", match.group(2).lower(), match.group(3)
            if name not in _ALLOWED_TAGS:
                raise TextValidationError(f"tag <{name}> is not supported by Telegram")
            if closing:
                if attrs.strip():
                    raise TextValidationError(f"closing tag </{name}> must not have attributes")
                if not stack or stack[-1] != name:
                    raise TextValidationError(f"unexpected </{name}>")
                stack.pop()
            else:
                _check_open_tag(name, attrs, stack)
                stack.append(name)
            pos = match.end()
        elif ch == "&":
            match = _ENTITY.match(text, pos)
            if match is None:
                raise TextValidationError(f"unexpected '&' at position {pos}: write &amp; for a literal '&'")
            pos = match.end()
        else:
            pos += 1
    if stack:
        raise TextValidationError(f"unclosed <{stack[-1]}>")


def _check_open_tag(name: str, attrs: str, stack: list[str]) -> None:
    values: dict[str, str] = {}
    for match in _ATTR.finditer(attrs):
        attr = match.group(1).lower()
        if attr not in _ALLOWED_TAGS[name]:
            raise TextValidationError(f"attribute {attr!r} is not allowed on <{name}>")
        values[attr] = match.group(2) if match.group(2) is not None else (match.group(3) or "")
    if stack and stack[-1] in ("code", "pre") and not (name == "code" and stack[-1] == "pre"):
        raise TextValidationError(f"<{name}> cannot be placed inside <{stack[-1]}>")
    if name == "blockquote" and "blockquote" in stack:
        raise TextValidationError("<blockquote> cannot be nested")
    if name == "a":
        href = values.get("href", "").strip()
        if not href:
            raise TextValidationError("<a> needs an href")
        if not href.startswith("{") and not href.lower().startswith(_LINK_SCHEMES):
            raise TextValidationError(f"unsupported link {href!r}: use http(s)://, tg:// or mailto:")
    elif name == "span" and values.get("class") != "tg-spoiler":
        raise TextValidationError('<span> is only allowed as <span class="tg-spoiler">')
    elif name == "code" and "class" in values and not re.fullmatch(r"language-[\w+#.-]+", values["class"]):
        raise TextValidationError('<code class="..."> must be "language-<name>"')
    elif name == "tg-emoji" and not values.get("emoji-id", "").isdigit():
        raise TextValidationError("<tg-emoji> needs a numeric emoji-id")


def validate_template(template: str, allowed: Iterable[str] | None = None) -> frozenset[str]:
    """Validate placeholders (optionally against ``allowed``) and HTML; return the placeholders used."""
    if len(template) > MAX_TEXT_LENGTH:
        raise TextValidationError(f"text is longer than {MAX_TEXT_LENGTH} characters")
    used = placeholders(template)
    if allowed is not None:
        unknown = used - frozenset(allowed)
        if unknown:
            names = ", ".join("{" + name + "}" for name in sorted(unknown))
            raise TextValidationError(f"unknown placeholders: {names}")
    check_html(template)
    return used


def render(template: str, values: Mapping[str, Any]) -> str:
    """Substitute ``{name}`` with ``str(values[name])``; unknown names stay as is, ``{{``/``}}`` become braces."""

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name is None:
            return match.group(0)[0]
        if name in values:
            return str(values[name])
        return match.group(0)

    return _RENDER.sub(_sub, template)


# --- plural rules and formatting ---------------------------------------------------------------------------------


def plural_form(n: int, lang: str) -> str:
    """CLDR plural category of an integer.

    Russian and Ukrainian: ``one``/``few``/``many``; Chinese: ``other`` only; English, Kazakh, Uzbek and Persian:
    ``one``/``other`` (Persian also treats 0 as ``one``).
    """
    n = abs(n)
    if lang == "zh":
        return "other"
    if lang == "fa":
        return "one" if n in (0, 1) else "other"
    if lang in ("ru", "uk"):
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    return "one" if n == 1 else "other"


_SEPARATORS: Mapping[str, tuple[str, str]] = {  # (thousands, decimal)
    "ru": (" ", ","),
    "uk": (" ", ","),
    "kk": (" ", ","),
    "uz": (" ", ","),
    "en": (",", "."),
    "fa": (",", "."),
    "zh": (",", "."),
}


def format_number(n: int, lang: str) -> str:
    """``1490 -> "1 490"`` (ru, no-break space) / ``"1,490"`` (en)."""
    group = _SEPARATORS.get(lang, _SEPARATORS["en"])[0]
    sign = "-" if n < 0 else ""
    return sign + f"{abs(n):,}".replace(",", group)


def format_amount(kop: int, lang: str) -> str:
    """Kopecks as a ruble amount without the currency sign: ``149050 -> "1 490,50"`` (ru) / ``"1,490.50"`` (en)."""
    decimal = _SEPARATORS.get(lang, _SEPARATORS["en"])[1]
    whole, frac = divmod(abs(kop), 100)
    text = format_number(whole, lang)
    if frac:
        text += f"{decimal}{frac:02d}"
    return f"-{text}" if kop < 0 else text


# --- the catalog -------------------------------------------------------------------------------------------------


class I18n:
    """Text catalog: locale files + admin overrides. Load once at startup with :meth:`load`."""

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        *,
        default_lang: str = "ru",
        languages: Iterable[str] = LANGUAGES,
        locales_dir: Path = LOCALES_DIR,
        modules_dir: Path | None = MODULES_DIR,
    ) -> None:
        self._session_factory = session_factory
        self._languages: tuple[str, ...] = tuple(languages)
        if default_lang not in self._languages:
            raise ValueError(f"default language {default_lang!r} is not one of {self._languages}")
        self._default = default_lang
        self._locales_dir = locales_dir
        self._modules_dir = modules_dir
        self._files: dict[str, dict[str, str]] = {lang: {} for lang in self._languages}
        self._overrides: dict[str, dict[str, str]] = {lang: {} for lang in self._languages}
        self._modules: dict[str, str] = {}
        self._allowed: dict[str, frozenset[str]] = {}
        self._version: str | None = None
        self._missing_logged: set[str] = set()

    # -- properties ---------------------------------------------------------------------------------------------------

    @property
    def default_lang(self) -> str:
        return self._default

    @default_lang.setter
    def default_lang(self, lang: str) -> None:
        """Change the fallback language at runtime (e.g. after the DEFAULT_LANGUAGE setting changed)."""
        if lang not in self._languages:
            raise ValueError(f"unsupported language: {lang!r}")
        self._default = lang

    @property
    def languages(self) -> tuple[str, ...]:
        return self._languages

    def normalize(self, lang: str | None) -> str:
        """``lang`` if supported, else the default language."""
        return lang if lang in self._languages else self._default

    # -- loading ------------------------------------------------------------------------------------------------------

    async def load(self) -> None:
        """Read locale files and DB overrides."""
        await asyncio.to_thread(self.load_files)
        for problem in self.check():
            log.warning("locale: %s", problem)
        await self.reload_overrides()

    def load_files(self) -> None:
        """Read every locale file; raise :class:`LocaleError` listing all problems found."""
        files: dict[str, dict[str, str]] = {lang: {} for lang in self._languages}
        origin: dict[tuple[str, str], str] = {}
        modules: dict[str, str] = {}
        problems: list[str] = []
        for module, lang, path in self._locale_paths():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                problems.append(f"{path}: {exc}")
                continue
            for key, value in _flatten(data, "", path, problems):
                where = f"{path}: {key}"
                if module != CORE_MODULE and not key.startswith(f"{module}."):
                    problems.append(f"{where}: module keys must start with '{module}.'")
                    continue
                if len(key) > MAX_KEY_LENGTH:
                    problems.append(f"{where}: key is longer than {MAX_KEY_LENGTH} characters")
                    continue
                if (lang, key) in origin:
                    problems.append(f"{where}: duplicate key (already defined in {origin[lang, key]})")
                    continue
                if modules.setdefault(key, module) != module:
                    problems.append(f"{where}: key already belongs to module {modules[key]!r}")
                    continue
                try:
                    validate_template(value)
                except TextValidationError as exc:
                    problems.append(f"{where}: {exc}")
                    continue
                origin[lang, key] = str(path)
                files[lang][key] = value
        if problems:
            raise LocaleError("invalid locale files:\n" + "\n".join(problems))
        allowed: dict[str, set[str]] = {}
        for texts in files.values():
            for key, value in texts.items():
                allowed.setdefault(key, set()).update(placeholders(value))
        self._files = files
        self._modules = modules
        self._allowed = {key: frozenset(names) for key, names in allowed.items()}
        self._missing_logged.clear()

    def _locale_paths(self) -> list[tuple[str, str, Path]]:
        paths: list[tuple[str, str, Path]] = []
        for lang in self._languages:
            lang_dir = self._locales_dir / lang
            if lang_dir.is_dir():
                paths.extend((CORE_MODULE, lang, path) for path in sorted(lang_dir.glob("*.toml")))
        if self._modules_dir is not None and self._modules_dir.is_dir():
            for module_dir in sorted(p for p in self._modules_dir.iterdir() if p.is_dir()):
                for lang in self._languages:
                    path = module_dir / "locales" / f"{lang}.toml"
                    if path.is_file():
                        paths.append((module_dir.name, lang, path))
        return paths

    def check(self) -> list[str]:
        """Consistency problems between languages: missing keys, missing plural forms, placeholder mismatches."""
        problems: list[str] = []
        plain: dict[str, dict[str, str]] = {}
        plural_bases: dict[str, set[str]] = {}
        for lang, texts in self._files.items():
            for key, value in texts.items():
                base, _, form = key.rpartition(".")
                if form in PLURAL_FORMS and base:
                    plural_bases.setdefault(base, set()).add(lang)
                else:
                    plain.setdefault(key, {})[lang] = value
        for key, by_lang in sorted(plain.items()):
            for lang in self._languages:
                if lang in COMPLETE_LANGUAGES and lang not in by_lang:
                    problems.append(f"{key}: missing in {lang!r}")
            if len({placeholders(v) for v in by_lang.values()}) > 1:
                problems.append(f"{key}: placeholders differ between languages")
        for base in sorted(plural_bases):
            for lang in self._languages:
                if lang not in COMPLETE_LANGUAGES and lang not in plural_bases[base]:
                    continue  # not translated yet: falls back as a whole
                required = REQUIRED_PLURAL_FORMS.get(lang, ("other",))
                missing = [form for form in required if f"{base}.{form}" not in self._files[lang]]
                if missing:
                    problems.append(f"{base}: plural forms {missing} missing in {lang!r}")
        return problems

    def missing(self, lang: str) -> list[str]:
        """Keys (plural forms as their base key) that ``lang`` lacks and takes from a fallback language."""
        texts = self._files.get(lang, {})
        have = {_plural_base(key) for key in texts}
        return sorted({_plural_base(key) for key in self._modules} - have)

    def fallback_chain(self, lang: str) -> tuple[str, ...]:
        """Languages consulted for a text, in order: ``lang``, its related language, the default language."""
        lang = self.normalize(lang)
        chain = [lang]
        related = FALLBACKS.get(lang)
        if related is not None and related in self._languages:
            chain.append(related)
        chain.append(self._default)
        return tuple(dict.fromkeys(chain))

    async def reload_overrides(self) -> None:
        """Re-read admin overrides from the database (skipping rows that no longer validate)."""
        if self._session_factory is None:
            return
        async with read_session(self._session_factory) as session, session.begin():
            version = await kv.get(session, VERSION_KEY)
            rows = (await session.execute(select(TextOverride.key, TextOverride.language, TextOverride.value))).all()
        overrides: dict[str, dict[str, str]] = {lang: {} for lang in self._languages}
        for key, lang, value in rows:
            if lang not in overrides or key not in self._allowed:
                continue
            try:
                validate_template(value, self._allowed[key])
            except TextValidationError as exc:
                log.warning("ignoring text override %s [%s]: %s", key, lang, exc)
                continue
            overrides[lang][key] = value
        self._overrides = overrides
        self._version = version

    async def poll_changes(self) -> bool:
        """Reload overrides if another instance changed them; True if reloaded."""
        if self._session_factory is None:
            return False
        async with read_session(self._session_factory) as session, session.begin():
            version = await kv.get(session, VERSION_KEY)
        if version == self._version:
            return False
        await self.reload_overrides()
        return True

    # -- lookup -------------------------------------------------------------------------------------------------------

    def template(self, key: str, lang: str) -> str | None:
        """Effective template: override then file text of ``lang``, its related language, then the default one."""
        for code in self.fallback_chain(lang):
            value = self._overrides[code].get(key)
            if value is None:
                value = self._files[code].get(key)
            if value is not None:
                return value
        return None

    def t(self, key: str, lang: str, /, **kw: Any) -> str:
        """Rendered text for ``key`` in ``lang``; the key itself if it is unknown. Values are NOT escaped."""
        value = self.template(key, lang)
        if value is None:
            if key not in self._missing_logged:
                self._missing_logged.add(key)
                log.warning("missing text key %r", key)
            return key
        return render(value, kw)

    def has(self, key: str) -> bool:
        return key in self._modules

    def default(self, key: str, lang: str) -> str | None:
        """Built-in (file) text of ``key`` in ``lang``, ignoring overrides."""
        return self._files.get(lang, {}).get(key)

    def override(self, key: str, lang: str) -> str | None:
        """Admin override of ``key`` in ``lang``, if any."""
        return self._overrides.get(lang, {}).get(key)

    def allowed_placeholders(self, key: str) -> frozenset[str]:
        return self._allowed.get(key, frozenset())

    def keys(self, module: str | None = None) -> list[KeyInfo]:
        """All known keys (optionally of one module), sorted."""
        return [
            KeyInfo(
                key=key,
                placeholders=self._allowed.get(key, frozenset()),
                default_ru=self._files.get("ru", {}).get(key),
                default_en=self._files.get("en", {}).get(key),
                module=owner,
            )
            for key, owner in sorted(self._modules.items())
            if module is None or owner == module
        ]

    def plural(self, n: int, lang: str, key_base: str, /, **kw: Any) -> str:
        """Plural text: ``plural(5, "ru", "plural.days")`` -> ``"5 дней"``. ``{n}`` is the formatted number."""
        lang = self.normalize(lang)
        values = {"n": format_number(n, lang), **kw}
        # The form is chosen by the rules of the language that actually supplies the text, so a Russian text used
        # as a fallback for Kazakh still gets Russian "few" / "many" forms.
        for code in self.fallback_chain(lang):
            form = plural_form(n, code)
            for candidate in dict.fromkeys((form, "other", "many", "one")):
                key = f"{key_base}.{candidate}"
                value = self._overrides[code].get(key)
                if value is None:
                    value = self._files[code].get(key)
                if value is not None:
                    return render(value, values)
        return self.t(f"{key_base}.{plural_form(n, lang)}", lang, **values)

    # -- formatting ---------------------------------------------------------------------------------------------------

    def money(self, kop: int, lang: str) -> str:
        """``149000 -> "1 490 ₽"``."""
        return self.t("fmt.money", lang, amount=format_amount(kop, self.normalize(lang)))

    def stars(self, amount: int, lang: str) -> str:
        """Telegram Stars amount: ``"150 ⭐️"``."""
        return self.t("fmt.stars", lang, amount=format_number(amount, self.normalize(lang)))

    def date(self, dt: datetime, lang: str, tz: tzinfo | None = None) -> str:
        """``"4 декабря 2026"`` / ``"December 4, 2026"`` in ``tz`` (UTC by default; naive values are UTC)."""
        local = _localize(dt, tz)
        return self.t(
            "fmt.date",
            lang,
            day=local.day,
            month=self.t(f"fmt.month.{local.month}", lang),
            year=local.year,
        )

    def datetime(self, dt: datetime, lang: str, tz: tzinfo | None = None) -> str:
        """``"4 декабря 2026, 19:00"``."""
        local = _localize(dt, tz)
        return self.t("fmt.datetime", lang, date=self.date(local, lang, local.tzinfo), time=f"{local:%H:%M}")

    def duration(self, days: int, lang: str) -> str:
        """Plan-like period: whole years, else whole months (30 days), else days: ``90 -> "3 месяца"``."""
        if days >= 365 and days % 365 == 0:
            return self.plural(days // 365, lang, "plural.years")
        if days >= 30 and days % 30 == 0:
            return self.plural(days // 30, lang, "plural.months")
        return self.plural(days, lang, "plural.days")

    @staticmethod
    def esc(value: object) -> str:
        """HTML-escape a value for interpolation into a text."""
        return html.escape(str(value), quote=True)

    def translator(self, lang: str | None, tz: tzinfo | None = None) -> Translator:
        return Translator(self, self.normalize(lang), tz)

    # -- admin overrides ----------------------------------------------------------------------------------------------

    async def set_override(
        self,
        key: str,
        lang: str,
        value: str,
        actor_id: int,
        *,
        source: str = "bot",
        ip: str | None = None,
    ) -> None:
        """Save an admin override after validation (raises :class:`TextValidationError` / ``KeyError``)."""
        if lang not in self._languages:
            raise TextValidationError(f"unsupported language: {lang!r}")
        if key not in self._modules:
            raise KeyError(key)
        validate_template(value, self._allowed[key])
        if not value.strip():
            raise TextValidationError("text must not be empty")
        factory = self._require_factory()
        version = random_token(8)
        async with factory.begin() as session:
            row = await session.scalar(
                select(TextOverride).where(TextOverride.key == key, TextOverride.language == lang).with_for_update()
            )
            if row is None:
                try:
                    async with session.begin_nested():
                        session.add(TextOverride(key=key, language=lang, value=value, updated_by=actor_id))
                except IntegrityError:
                    row = await session.scalar(
                        select(TextOverride)
                        .where(TextOverride.key == key, TextOverride.language == lang)
                        .with_for_update()
                    )
            if row is not None:
                row.value = value
                row.updated_by = actor_id
            await audit.log(
                session,
                actor_id,
                "text.override_set",
                target_type="text",
                target_id=f"{key}:{lang}",
                source=source,
                ip=ip,
                key=key,
                language=lang,
                length=len(value),
            )
            await kv.set(session, VERSION_KEY, version)
        self._overrides[lang][key] = value
        self._version = version

    async def reset_override(
        self,
        key: str,
        lang: str,
        actor_id: int,
        *,
        source: str = "bot",
        ip: str | None = None,
    ) -> bool:
        """Remove an override (back to the built-in text); True if there was one."""
        factory = self._require_factory()
        version = random_token(8)
        async with factory.begin() as session:
            row = await session.scalar(
                select(TextOverride).where(TextOverride.key == key, TextOverride.language == lang).with_for_update()
            )
            if row is None:
                existed = False
            else:
                existed = True
                await session.delete(row)
                await audit.log(
                    session,
                    actor_id,
                    "text.override_reset",
                    target_type="text",
                    target_id=f"{key}:{lang}",
                    source=source,
                    ip=ip,
                    key=key,
                    language=lang,
                )
                await kv.set(session, VERSION_KEY, version)
        if existed:
            self._overrides.get(lang, {}).pop(key, None)
            self._version = version
        return existed

    def _require_factory(self) -> SessionFactory:
        if self._session_factory is None:
            raise RuntimeError("I18n was created without a session factory")
        return self._session_factory


class Translator:
    """:class:`I18n` bound to one language (and time zone); injected into handlers as ``_``."""

    __slots__ = ("i18n", "lang", "tz")

    def __init__(self, i18n: I18n, lang: str, tz: tzinfo | None = None) -> None:
        self.i18n = i18n
        self.lang = lang
        self.tz = tz

    def __call__(self, key: str, /, **kw: Any) -> str:
        return self.i18n.t(key, self.lang, **kw)

    def plural(self, n: int, key_base: str, /, **kw: Any) -> str:
        return self.i18n.plural(n, self.lang, key_base, **kw)

    def money(self, kop: int) -> str:
        return self.i18n.money(kop, self.lang)

    def stars(self, amount: int) -> str:
        return self.i18n.stars(amount, self.lang)

    def number(self, n: int) -> str:
        return format_number(n, self.lang)

    def date(self, dt: datetime) -> str:
        return self.i18n.date(dt, self.lang, self.tz)

    def datetime(self, dt: datetime) -> str:
        return self.i18n.datetime(dt, self.lang, self.tz)

    def duration(self, days: int) -> str:
        return self.i18n.duration(days, self.lang)

    esc = staticmethod(I18n.esc)

    def __repr__(self) -> str:
        return f"Translator(lang={self.lang!r})"


def _localize(dt: datetime, tz: tzinfo | None) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz or UTC)


def _plural_base(key: str) -> str:
    base, _, form = key.rpartition(".")
    return base if form in PLURAL_FORMS and base else key


def _flatten(data: Mapping[str, Any], prefix: str, path: Path, problems: list[str]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for name, value in data.items():
        key = f"{prefix}{name}"
        if isinstance(value, Mapping):
            items.extend(_flatten(value, f"{key}.", path, problems))
        elif isinstance(value, str):
            items.append((key, value))
        else:
            problems.append(f"{path}: {key}: value must be a string")
    return items
