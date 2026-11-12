"""Additional bot languages: plural rules, detection, fallback, picker and integrity of the translations."""

from __future__ import annotations

import tomllib
import warnings
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.core.i18n import (
    COMPLETE_LANGUAGES,
    FALLBACKS,
    LOCALES_DIR,
    MODULES_DIR,
    PLURAL_FORMS,
    REQUIRED_PLURAL_FORMS,
    RTL_LANGUAGES,
    I18n,
    check_html,
    format_amount,
    format_number,
    placeholders,
    plural_form,
)
from app.core.modules import BotCommandSpec
from app.db.models import User
from app.db.session import SessionFactory
from app.keyboards.common import language_kb
from app.services import users
from app.services.maintenance import _pick
from tests.conftest import make_settings
from tests.test_mod_miniapp_support import USER_ID, Env, miniapp_env

EXTRA = ("uk", "kk", "uz", "fa", "zh")
NBSP = " "


@pytest.fixture(scope="module")
def i18n() -> I18n:
    catalog = I18n(None)
    catalog.load_files()
    return catalog


# --- plural rules ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "form"),
    [
        (0, "many"),
        (1, "one"),
        (2, "few"),
        (4, "few"),
        (5, "many"),
        (11, "many"),
        (12, "many"),
        (21, "one"),
        (22, "few"),
        (25, "many"),
        (101, "one"),
        (111, "many"),
    ],
)
def test_ukrainian_plural_rule(n: int, form: str) -> None:
    assert plural_form(n, "uk") == form


@pytest.mark.parametrize("lang", ["kk", "uz"])
def test_one_other_plural_rule(lang: str) -> None:
    assert [plural_form(n, lang) for n in (0, 1, 2, 5, 21)] == ["other", "one", "other", "other", "other"]


def test_persian_and_chinese_plural_rules() -> None:
    assert [plural_form(n, "fa") for n in (0, 1, 2, 10)] == ["one", "one", "other", "other"]
    assert {plural_form(n, "zh") for n in (0, 1, 2, 5, 21)} == {"other"}


def test_plural_texts(i18n: I18n) -> None:
    assert i18n.plural(1, "uk", "plural.days") == "1 день"
    assert i18n.plural(3, "uk", "plural.days") == "3 дні"
    assert i18n.plural(11, "uk", "plural.days") == "11 днів"
    assert i18n.plural(5, "kk", "plural.devices") == "5 құрылғы"
    assert i18n.plural(2, "uz", "plural.months") == "2 oy"
    assert i18n.plural(1, "fa", "plural.days") == "1 روز"
    assert i18n.plural(1, "zh", "plural.days") == "1 天"
    assert i18n.duration(90, "zh") == "3 个月"


def test_number_and_date_formats(i18n: I18n) -> None:
    assert format_number(1490, "uk") == f"1{NBSP}490"
    assert format_amount(149050, "kk") == f"1{NBSP}490,50"
    assert format_number(1490, "zh") == "1,490"
    moment = datetime(2026, 12, 4, 19, 0, tzinfo=UTC)
    assert i18n.date(moment, "uk") == "4 грудня 2026"
    assert i18n.date(moment, "zh") == "2026年12月4日"
    assert i18n.date(moment, "uz") == "4-dekabr, 2026"


# --- language detection, enabled languages, fallback -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("uk", "uk"),
        ("uk-UA", "uk"),
        ("ua", "uk"),
        ("kk", "kk"),
        ("kk-KZ", "kk"),
        ("uz", "uz"),
        ("uz-Latn-UZ", "uz"),
        ("fa", "fa"),
        ("fa-IR", "fa"),
        ("zh", "zh"),
        ("zh-hans", "zh"),
        ("zh-CN", "zh"),
        ("zh_Hans_SG", "zh"),
        ("ru-RU", "ru"),
        ("en", "en"),
        ("de", None),
        ("", None),
        (None, None),
    ],
)
def test_detect_language(code: str | None, expected: str | None) -> None:
    assert users.detect_language(code) == expected


def test_pick_language_respects_enabled_languages() -> None:
    every = make_settings()
    assert users.enabled_languages(every) == users.LANGUAGES
    assert users.pick_language("zh-CN", every) == "zh"
    narrow = make_settings(enabled_languages="ru,en")
    assert users.enabled_languages(narrow) == ("ru", "en")
    assert users.pick_language("zh-CN", narrow) == "ru"
    assert users.pick_language("en-US", narrow) == "en"
    # The default language is always available, unknown codes are ignored, the order is the picker order.
    odd = make_settings(enabled_languages="fa, de, KK", default_language="uk")
    assert users.enabled_languages(odd) == ("uk", "kk", "fa")


def test_fallback_chain(i18n: I18n) -> None:
    assert i18n.fallback_chain("kk") == ("kk", "ru")
    assert i18n.fallback_chain("zh") == ("zh", "en", "ru")
    assert i18n.fallback_chain("ru") == ("ru",)
    assert i18n.fallback_chain("de") == ("ru",)
    assert set(FALLBACKS) == set(EXTRA)
    assert {"fa"} == RTL_LANGUAGES


def test_missing_texts_fall_back_to_a_related_language(tmp_path: Path) -> None:
    for lang, text in (("ru", "Привет"), ("en", "Hello"), ("kk", "Сәлем")):
        path = tmp_path / "locales" / lang / "a.toml"
        path.parent.mkdir(parents=True)
        path.write_text(f'hello = "{text}"\nonly = "{text}!"\n' if lang != "kk" else f'hello = "{text}"\n', "utf-8")
    catalog = I18n(None, locales_dir=tmp_path / "locales", modules_dir=None)
    catalog.load_files()
    assert catalog.t("hello", "kk") == "Сәлем"
    assert catalog.t("only", "kk") == "Привет!"  # Kazakh falls back to Russian
    assert catalog.t("only", "zh") == "Hello!"  # Chinese falls back to English
    assert catalog.check() == []  # translations may be incomplete
    assert catalog.missing("kk") == ["only"]


def test_ru_or_en_for_texts_that_exist_only_in_two_languages() -> None:
    expected = {"ru": "ru", "en": "en", "uk": "en", "kk": "ru", "uz": "ru", "fa": "en", "zh": "en"}
    assert {code: users.ru_or_en(code) for code in expected} == expected
    spec = BotCommandSpec("language", "Сменить язык", "Change language")
    assert spec.description("kk") == "Сменить язык" and spec.description("zh") == "Change language"
    assert _pick("uz", "Работы", "Maintenance") == "Работы"
    assert _pick("fa", "Работы", "Maintenance") == "Maintenance"


# --- the language picker ----------------------------------------------------------------------------------------------


def test_picker_lists_every_language_in_its_own_script(i18n: I18n) -> None:
    labels = {code: i18n.t(f"lang.{code}", "ru") for code in users.LANGUAGES}
    assert labels == {
        "ru": "🇷🇺 Русский",
        "en": "🇬🇧 English",
        "uk": "🇺🇦 Українська",
        "kk": "🇰🇿 Қазақша",
        "uz": "🇺🇿 Oʻzbekcha",
        "fa": "🇮🇷 فارسی",
        "zh": "🇨🇳 简体中文",
    }
    for lang in users.LANGUAGES:
        for code, label in labels.items():
            assert i18n.default(f"lang.{code}", lang) == label, (lang, code)
        assert i18n.default("lang.changed", lang), lang
    _ = i18n.translator("zh")
    keyboard = language_kb(_, users.enabled_languages(make_settings()), current="zh")
    texts = [row[0].text for row in keyboard.inline_keyboard[:-1]]
    assert texts == [labels[code] if code != "zh" else f"✓ {labels['zh']}" for code in users.LANGUAGES]
    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard[:-1]]
    assert callbacks == [f"nav:lang:{code}" for code in users.LANGUAGES]


# --- integrity of the translation files ------------------------------------------------------------------------------


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def _base(key: str) -> tuple[str, str | None]:
    base, _, form = key.rpartition(".")
    return (base, form) if form in PLURAL_FORMS and base else (key, None)


def _translation_pairs() -> list[tuple[str, Path, Path]]:
    pairs: list[tuple[str, Path, Path]] = []
    for lang in EXTRA:
        for en in sorted((LOCALES_DIR / "en").glob("*.toml")):
            pairs.append((lang, en, LOCALES_DIR / lang / en.name))
        for en in sorted(MODULES_DIR.glob("*/locales/en.toml")):
            pairs.append((lang, en, en.with_name(f"{lang}.toml")))
    return pairs


def _read(path: Path) -> dict[str, str]:
    return _flatten(tomllib.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("lang", EXTRA)
def test_translations_keep_placeholders_markup_and_plural_forms(lang: str) -> None:
    problems: list[str] = []
    for code, en_path, path in _translation_pairs():
        if code != lang or not path.exists():
            continue
        english: dict[str, set[str]] = {}
        for key, value in _read(en_path).items():
            english.setdefault(_base(key)[0], set()).update(placeholders(value))
        forms: dict[str, set[str]] = {}
        for key, value in _read(path).items():
            base, form = _base(key)
            where = f"{path.relative_to(MODULES_DIR.parent)}: {key}"
            if base not in english:
                problems.append(f"{where}: not in the English file")
                continue
            check_html(value)
            used = placeholders(value)
            if form is None and used != english[base]:
                problems.append(f"{where}: placeholders {sorted(used)} != {sorted(english[base])}")
            if form is not None:
                forms.setdefault(base, set()).add(form)
                if not used <= english[base]:
                    problems.append(f"{where}: unknown placeholders {sorted(used - english[base])}")
            if value.count("\n") != _english_value(en_path, key).count("\n") and form is None:
                problems.append(f"{where}: line breaks differ from English")
        for base, have in forms.items():
            if have != set(REQUIRED_PLURAL_FORMS[lang]):
                problems.append(f"{path.name}: {base}: plural forms {sorted(have)}")
    assert problems == []


def _english_value(path: Path, key: str) -> str:
    return _read(path).get(key, "")


def test_core_texts_are_complete_in_every_language(i18n: I18n) -> None:
    core = {info.key for info in i18n.keys(module="core")}
    bases = {_base(key)[0] for key in core}
    for lang in EXTRA:
        assert sorted(bases & set(i18n.missing(lang))) == [], lang


def test_catalog_check_stays_clean(i18n: I18n) -> None:
    assert COMPLETE_LANGUAGES == ("ru", "en")
    assert i18n.check() == []


def test_translation_coverage_report(i18n: I18n) -> None:
    """Reports (does not fail on) texts the additional languages still take from a fallback language."""
    for lang in EXTRA:
        missing = i18n.missing(lang)
        if missing:
            modules = sorted({key.split(".", 1)[0] for key in missing})
            warnings.warn(f"{lang}: {len(missing)} texts fall back ({', '.join(modules)})", stacklevel=1)


# --- Mini App ---------------------------------------------------------------------------------------------------------


@pytest.fixture
async def env(tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with miniapp_env(tmp_path, session_factory, monkeypatch, enabled_languages="ru,en,uk,fa") as environment:
        yield environment


async def test_miniapp_offers_enabled_languages(env: Env) -> None:
    me = await env.get("/me")
    assert me.json()["languages"] == ["ru", "en", "uk", "fa"]
    assert (await env.post("/me/language", {"language": "zh"})).status_code == 422
    resp = await env.post("/me/language", {"language": "uk"})
    assert resp.status_code == 200 and resp.json() == {"language": "uk"}
    async with env.ctx.session_factory() as session:
        user = await session.get(User, USER_ID)
        assert user is not None and user.language == "uk"
    missing = await env.get("/devices")
    assert missing.json()["message"] == "У вас ще немає підписки."
