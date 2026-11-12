from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.core.i18n import (
    CORE_MODULE,
    I18n,
    LocaleError,
    TextValidationError,
    Translator,
    check_html,
    format_amount,
    format_number,
    placeholders,
    plural_form,
    render,
    validate_template,
)
from app.db.models import AdminAuditLog, TextOverride
from app.db.session import SessionFactory

NBSP = " "


@pytest.fixture(scope="module")
def i18n() -> I18n:
    catalog = I18n(None)
    catalog.load_files()
    return catalog


# --- built-in locale files ---------------------------------------------------------------------------------------


def test_core_locales_are_complete_and_consistent(i18n: I18n) -> None:
    assert i18n.check() == []
    infos = i18n.keys()
    keys = {info.key for info in infos}
    required = {
        "nav.back",
        "nav.cancel",
        "nav.menu",
        "menu.title",
        "menu.my_subscription",
        "menu.buy",
        "menu.connect",
        "menu.balance",
        "menu.referral",
        "menu.gift",
        "menu.support",
        "menu.language",
        "menu.instructions",
        "error.generic",
        "error.expired_button",
        "access.banned",
        "access.throttled",
        "access.maintenance",
        "access.maintenance_eta",
        "access.invite_only",
        "access.channel.text",
        "access.channel.check",
        "consent.text",
        "consent.accept",
        "fmt.money",
        "fmt.date",
        "fmt.datetime",
        "plural.days.one",
        "plural.devices.many",
        "plural.gb.other",
    }
    assert required <= keys
    for lang in ("ru", "en"):
        for month in range(1, 13):
            assert i18n.default(f"fmt.month.{month}", lang)


def test_keys_info(i18n: I18n) -> None:
    infos = i18n.keys()
    info = {k.key: k for k in infos}["access.maintenance_eta"]
    assert info.placeholders == frozenset({"eta"})
    assert info.module == CORE_MODULE
    assert info.default_ru is not None and "{eta}" in info.default_ru
    assert info.default_en is not None and "{eta}" in info.default_en
    assert i18n.keys(module="nope") == []
    assert i18n.allowed_placeholders("menu.title") == frozenset({"brand"})


def test_t_renders_and_falls_back(i18n: I18n) -> None:
    assert i18n.t("menu.title", "ru", brand="VPN").startswith("<b>VPN</b>")
    assert i18n.t("menu.title", "en", brand="VPN").startswith("<b>VPN</b>")
    assert i18n.t("nav.back", "de") == i18n.t("nav.back", "ru")  # unsupported -> default language
    assert i18n.t("no.such.key", "ru") == "no.such.key"
    assert i18n.t("access.maintenance_eta", "en").count("{eta}") == 1  # missing value stays visible
    assert i18n.has("nav.back") and not i18n.has("no.such.key")


def test_translator(i18n: I18n) -> None:
    _ = i18n.translator("en", ZoneInfo("Europe/Moscow"))
    assert isinstance(_, Translator)
    assert _("nav.back") == "◀️ Back"
    assert _.plural(3, "plural.devices") == "3 devices"
    assert _.money(19900) == "199 ₽"
    assert _.stars(1500) == "1,500 ⭐️"
    assert _.number(1234567) == "1,234,567"
    assert _.date(datetime(2026, 12, 31, 22, 0, tzinfo=UTC)) == "January 1, 2027"
    assert _.datetime(datetime(2026, 12, 4, 16, 5, tzinfo=UTC)) == "December 4, 2026, 19:05"
    assert _.duration(180) == "6 months"
    assert _.esc("<a&b>") == "&lt;a&amp;b&gt;"
    assert repr(_) == "Translator(lang='en')"
    assert i18n.translator(None).lang == "ru"


# --- plural rules & formatting -----------------------------------------------------------------------------------


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
        (14, "many"),
        (21, "one"),
        (22, "few"),
        (25, "many"),
        (101, "one"),
        (111, "many"),
        (112, "many"),
        (1004, "few"),
        (-1, "one"),
    ],
)
def test_ru_plural_rule(n: int, form: str) -> None:
    assert plural_form(n, "ru") == form


def test_en_plural_rule() -> None:
    assert [plural_form(n, "en") for n in (0, 1, 2, 21)] == ["other", "one", "other", "other"]


def test_plural_texts(i18n: I18n) -> None:
    assert i18n.plural(1, "ru", "plural.days") == "1 день"
    assert i18n.plural(3, "ru", "plural.days") == "3 дня"
    assert i18n.plural(11, "ru", "plural.days") == "11 дней"
    assert i18n.plural(21, "ru", "plural.months") == "21 месяц"
    assert i18n.plural(5, "ru", "plural.years") == "5 лет"
    assert i18n.plural(2, "ru", "plural.devices") == "2 устройства"
    assert i18n.plural(1500, "ru", "plural.gb") == f"1{NBSP}500 ГБ"
    assert i18n.plural(1, "en", "plural.days") == "1 day"
    assert i18n.plural(0, "en", "plural.days") == "0 days"
    assert i18n.plural(5, "ru", "no.such") == "no.such.many"


def test_durations(i18n: I18n) -> None:
    cases = {7: "7 дней", 30: "1 месяц", 90: "3 месяца", 180: "6 месяцев", 365: "1 год", 730: "2 года", 45: "45 дней"}
    for days, text in cases.items():
        assert i18n.duration(days, "ru") == text
    assert i18n.duration(1, "en") == "1 day"
    assert i18n.duration(365, "en") == "1 year"


def test_money_and_numbers(i18n: I18n) -> None:
    assert format_number(1234567, "ru") == f"1{NBSP}234{NBSP}567"
    assert format_number(-1000, "en") == "-1,000"
    assert format_amount(149050, "ru") == f"1{NBSP}490,50"
    assert format_amount(149050, "en") == "1,490.50"
    assert format_amount(-5, "ru") == "-0,05"
    assert format_amount(0, "en") == "0"
    assert i18n.money(19900, "ru") == "199 ₽"
    assert i18n.money(100000, "ru") == f"1{NBSP}000 ₽"


def test_dates(i18n: I18n) -> None:
    moscow = ZoneInfo("Europe/Moscow")
    dt = datetime(2026, 3, 8, 21, 30, tzinfo=UTC)
    assert i18n.date(dt, "ru", moscow) == "9 марта 2026"
    assert i18n.datetime(dt, "ru", moscow) == "9 марта 2026, 00:30"
    assert i18n.date(dt, "ru") == "8 марта 2026"  # UTC by default
    assert i18n.date(datetime(2026, 5, 1, 12), "en") == "May 1, 2026"  # naive = UTC
    assert i18n.datetime(dt, "en", timezone(timedelta(hours=-5))) == "March 8, 2026, 16:30"


# --- template validation -----------------------------------------------------------------------------------------


def test_placeholders() -> None:
    assert placeholders("Hi {name}, {{literal}} {amount}") == frozenset({"name", "amount"})
    assert placeholders("plain") == frozenset()
    for bad in ("{}", "{0}", "{user.name}", "{x[0]}", "{x!r}", "{x:>10}", "{x", "x}", "{ name }"):
        with pytest.raises(TextValidationError):
            placeholders(bad)


def test_render() -> None:
    assert render("Hi {name}! {{x}} {missing}", {"name": "<b>A</b>"}) == "Hi <b>A</b>! {x} {missing}"
    assert render("{n}", {"n": 5}) == "5"
    assert render("{{name}}", {"name": "X"}) == "{name}"


@pytest.mark.parametrize(
    "text",
    [
        "plain text",
        "<b>bold</b> <i>it</i> <u>u</u> <s>s</s> <strong>x</strong> <em>y</em> <ins>i</ins> <del>d</del>",
        '<a href="https://example.com/?a=1&amp;b=2">link</a>',
        '<a href="{url}">link</a>',
        "<a href='tg://user?id=1'>me</a>",
        '<code>x</code> <pre>y</pre> <pre><code class="language-python">z</code></pre>',
        "<blockquote>q</blockquote> <blockquote expandable>long</blockquote>",
        '<tg-spoiler>s</tg-spoiler> <span class="tg-spoiler">s</span>',
        '<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>',
        "<b>nested <i>tags <u>ok</u></i></b>",
        "1 &lt; 2 &amp;&amp; 3 &gt; 2 &quot;q&quot; &#128077; &#x1F44D;",
        "<B>upper</B>",
        "{brand} — 100%",
        "a -> b",
    ],
)
def test_check_html_accepts(text: str) -> None:
    check_html(text)


@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script>",
        "<div>x</div>",
        "<b>unclosed",
        "closing</b>",
        "<b><i>crossed</b></i>",
        "<a>no href</a>",
        '<a href="javascript:alert(1)">x</a>',
        '<b onclick="x">x</b>',
        '<span class="other">x</span>',
        '<code class="evil">x</code>',
        "<pre><b>x</b></pre>",
        "<code><i>x</i></code>",
        "<blockquote><blockquote>x</blockquote></blockquote>",
        '<tg-emoji emoji-id="abc">x</tg-emoji>',
        "1 < 2",
        "Tom & Jerry",
        "&nbsp;",
        "<b",
        "</b attr>",
    ],
)
def test_check_html_rejects(text: str) -> None:
    with pytest.raises(TextValidationError):
        check_html(text)


def test_validate_template_allowed_and_length() -> None:
    assert validate_template("<b>{a}</b>", {"a", "b"}) == frozenset({"a"})
    with pytest.raises(TextValidationError, match="unknown placeholders"):
        validate_template("{a} {c}", {"a"})
    with pytest.raises(TextValidationError):
        validate_template("x" * 4097)


# --- module locale files -----------------------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _catalog(tmp_path: Path) -> I18n:
    return I18n(None, locales_dir=tmp_path / "locales", modules_dir=tmp_path / "modules")


def test_module_locales_are_loaded(tmp_path: Path) -> None:
    _write(tmp_path / "locales/ru/a.toml", 'greeting = "Привет"\n')
    _write(tmp_path / "locales/en/a.toml", 'greeting = "Hi"\n')
    _write(tmp_path / "modules/shop/locales/ru.toml", '[shop]\ntitle = "Магазин {n}"\n"buy.ok" = "Ок"\n')
    _write(tmp_path / "modules/shop/locales/en.toml", '[shop]\ntitle = "Shop {n}"\n')
    (tmp_path / "modules/empty").mkdir()
    catalog = _catalog(tmp_path)

    catalog.load_files()

    assert catalog.t("shop.title", "en", n=1) == "Shop 1"
    assert catalog.t("shop.buy.ok", "en") == "Ок"  # falls back to the default language
    infos = catalog.keys()
    assert {k.key: k.module for k in infos} == {
        "greeting": CORE_MODULE,
        "shop.buy.ok": "shop",
        "shop.title": "shop",
    }
    assert catalog.check() == ["shop.buy.ok: missing in 'en'"]


@pytest.mark.parametrize(
    ("ru", "en", "problem"),
    [
        ('title = "x"\n', 'title = "x"\n', "must start with 'shop.'"),
        ("[shop\n", "", "shop"),
        ("[shop]\ncount = 5\n", "", "must be a string"),
        ('[shop]\nt = "<div>"\n', "", "not supported"),
        ('[shop]\nt = "{0}"\n', "", "invalid placeholder"),
    ],
)
def test_broken_module_locales(tmp_path: Path, ru: str, en: str, problem: str) -> None:
    _write(tmp_path / "modules/shop/locales/ru.toml", ru)
    if en:
        _write(tmp_path / "modules/shop/locales/en.toml", en)
    with pytest.raises(LocaleError, match=problem):
        _catalog(tmp_path).load_files()


def test_duplicate_keys_across_files(tmp_path: Path) -> None:
    _write(tmp_path / "locales/ru/a.toml", 'x = "1"\n')
    _write(tmp_path / "locales/ru/b.toml", 'x = "2"\n')
    with pytest.raises(LocaleError, match="duplicate key"):
        _catalog(tmp_path).load_files()


def test_check_reports_placeholder_and_plural_problems(tmp_path: Path) -> None:
    _write(tmp_path / "locales/ru/a.toml", 'x = "{a}"\n[p]\none = "{n}"\nfew = "{n}"\n')
    _write(tmp_path / "locales/en/a.toml", 'x = "{b}"\n[p]\none = "{n}"\nother = "{n}"\n')
    catalog = _catalog(tmp_path)
    catalog.load_files()
    assert catalog.check() == ["x: placeholders differ between languages", "p: plural forms ['many'] missing in 'ru'"]
    assert catalog.allowed_placeholders("x") == frozenset({"a", "b"})


def test_constructor_rejects_unknown_default() -> None:
    with pytest.raises(ValueError):
        I18n(None, default_lang="de")
    catalog = I18n(None)
    catalog.default_lang = "en"
    assert catalog.default_lang == "en"
    with pytest.raises(ValueError):
        catalog.default_lang = "de"


# --- admin overrides ---------------------------------------------------------------------------------------------


async def _loaded(session_factory: SessionFactory) -> I18n:
    catalog = I18n(session_factory)
    await catalog.load()
    return catalog


async def test_set_override_and_fallback_chain(session_factory: SessionFactory) -> None:
    catalog = await _loaded(session_factory)

    await catalog.set_override(
        "menu.title", "ru", "🏠 <b>{brand}</b> — меню", actor_id=1000, source="web", ip="1.2.3.4"
    )

    assert catalog.t("menu.title", "ru", brand="X") == "🏠 <b>X</b> — меню"
    assert catalog.t("menu.title", "en", brand="X").startswith("<b>X</b>\n\nHow can I help")  # file[en] wins
    assert catalog.override("menu.title", "ru") == "🏠 <b>{brand}</b> — меню"
    assert catalog.default("menu.title", "ru") != catalog.override("menu.title", "ru")

    # override[default] is used before file[default] when the key is missing in the user's language
    catalog._files["en"].pop("nav.back")
    await catalog.set_override("nav.back", "ru", "⬅️ Назад", actor_id=1000)
    assert catalog.t("nav.back", "en") == "⬅️ Назад"

    async with session_factory() as session:
        rows = (await session.scalars(select(TextOverride).order_by(TextOverride.id))).all()
        audit = (await session.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id))).all()
    assert [(r.key, r.language, r.updated_by) for r in rows] == [("menu.title", "ru", 1000), ("nav.back", "ru", 1000)]
    assert audit[0].action == "text.override_set"
    assert audit[0].target_id == "menu.title:ru"
    assert audit[0].source == "web" and audit[0].ip == "1.2.3.4"


async def test_set_override_updates_existing_row(session_factory: SessionFactory) -> None:
    catalog = await _loaded(session_factory)
    await catalog.set_override("nav.back", "en", "Back", actor_id=1)
    await catalog.set_override("nav.back", "en", "Go back", actor_id=2)

    async with session_factory() as session:
        rows = (await session.scalars(select(TextOverride))).all()
    assert [(r.value, r.updated_by) for r in rows] == [("Go back", 2)]
    assert catalog.t("nav.back", "en") == "Go back"


@pytest.mark.parametrize(
    ("key", "lang", "value", "error"),
    [
        ("menu.title", "ru", "{brand} {secret}", TextValidationError),
        ("menu.title", "ru", "{brand.__class__}", TextValidationError),
        ("menu.title", "ru", "<script>x</script>", TextValidationError),
        ("menu.title", "ru", "   ", TextValidationError),
        ("menu.title", "de", "x", TextValidationError),
        ("no.such.key", "ru", "x", KeyError),
    ],
)
async def test_set_override_validation(
    session_factory: SessionFactory, key: str, lang: str, value: str, error: type[Exception]
) -> None:
    catalog = await _loaded(session_factory)
    with pytest.raises(error):
        await catalog.set_override(key, lang, value, actor_id=1)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TextOverride)) == 0


async def test_reset_override(session_factory: SessionFactory) -> None:
    catalog = await _loaded(session_factory)
    await catalog.set_override("nav.back", "ru", "Назад!", actor_id=1)

    assert await catalog.reset_override("nav.back", "ru", actor_id=1) is True
    assert await catalog.reset_override("nav.back", "ru", actor_id=1) is False

    assert catalog.t("nav.back", "ru") == "◀️ Назад"
    async with session_factory() as session:
        actions = (await session.scalars(select(AdminAuditLog.action).order_by(AdminAuditLog.id))).all()
    assert actions == ["text.override_set", "text.override_reset"]


async def test_other_instance_picks_up_changes(session_factory: SessionFactory) -> None:
    first = await _loaded(session_factory)
    second = await _loaded(session_factory)
    assert await second.poll_changes() is False

    await first.set_override("nav.back", "en", "Return", actor_id=1)
    assert await second.poll_changes() is True
    assert second.t("nav.back", "en") == "Return"
    assert await second.poll_changes() is False

    await first.reset_override("nav.back", "en", actor_id=1)
    assert await second.poll_changes() is True
    assert second.t("nav.back", "en") == "◀️ Back"


async def test_invalid_stored_overrides_are_ignored(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as session:
        session.add_all(
            [
                TextOverride(key="nav.back", language="en", value="{gone} back"),  # placeholder no longer allowed
                TextOverride(key="removed.key", language="en", value="x"),
                TextOverride(key="nav.menu", language="de", value="x"),
                TextOverride(key="nav.cancel", language="en", value="Stop"),
            ]
        )
    catalog = await _loaded(session_factory)

    assert catalog.t("nav.back", "en") == "◀️ Back"
    assert catalog.t("nav.cancel", "en") == "Stop"


async def test_overrides_need_a_session_factory(i18n: I18n) -> None:
    assert await i18n.poll_changes() is False
    with pytest.raises(RuntimeError):
        await i18n.set_override("nav.back", "ru", "x", actor_id=1)


def test_text_keys_used_in_code_exist() -> None:
    """Every literal text key passed to a translator in the application code has a default text."""
    import re

    root = Path(__file__).resolve().parents[1] / "app"
    pattern = re.compile(r"""(?:\b_|\.t|_KEY)\s*[(=]\s*["']([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)["']""")
    i18n = I18n(None)
    i18n.load_files()
    missing: set[str] = set()
    for path in root.rglob("*.py"):
        for key in pattern.findall(path.read_text(encoding="utf-8")):
            for lang in ("ru", "en"):
                if i18n.default(key, lang) is None:
                    missing.add(f"{path.relative_to(root)}: {key} [{lang}]")
    assert not missing, sorted(missing)
