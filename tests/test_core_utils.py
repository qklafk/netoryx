from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.utils import bytes as ubytes
from app.utils import money, security, time
from app.utils.crypto import DecryptionError, SecretBox, is_encrypted
from tests.conftest import make_settings


# --- money ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kop", "text"),
    [(0, "0"), (15000, "150"), (15050, "150.50"), (15005, "150.05"), (5, "0.05"), (-15050, "-150.50"), (-5, "-0.05")],
)
def test_rub_formats_kopecks(kop: int, text: str) -> None:
    assert money.rub(kop) == text


@pytest.mark.parametrize(
    ("value", "kop"),
    [
        (150, 15000),
        (0, 0),
        (Decimal("199.00"), 19900),
        (Decimal("0.01"), 1),
        ("199.00", 19900),
        (" 199,5 ", 19950),
        ("1e2", 10000),
        (199.5, 19950),
        (0.1, 10),
        (-5, -500),
    ],
)
def test_rub_to_kop_exact(value: int | Decimal | str | float, kop: int) -> None:
    assert money.rub_to_kop(value) == kop


@pytest.mark.parametrize("value", ["1.005", "abc", "", "NaN", "inf", Decimal("Infinity"), float("nan"), "1.2.3"])
def test_rub_to_kop_rejects_bad_values(value: object) -> None:
    with pytest.raises(ValueError):
        money.rub_to_kop(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, None, [1], object()])
def test_rub_to_kop_rejects_bad_types(value: object) -> None:
    with pytest.raises(TypeError):
        money.rub_to_kop(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "rub"),
    [("150", 150), (" 150 ", 150), ("0", 0), ("0150", 150), ("9999999", 9_999_999), ("1", 1)],
)
def test_parse_rub_accepts_whole_rubles(text: str, rub: int) -> None:
    assert money.parse_rub(text) == rub


@pytest.mark.parametrize(
    "text", [None, "", " ", "10000000", "-5", "+5", "1.5", "1,5", "1 000", "١٢٣", "１２３", "12a", "150₽"]
)
def test_parse_rub_rejects_everything_else(text: str | None) -> None:
    assert money.parse_rub(text) is None


# --- security -------------------------------------------------------------------------------------
def test_random_token_is_urlsafe_and_unique() -> None:
    tokens = {security.random_token() for _ in range(200)}
    assert len(tokens) == 200
    for token in tokens:
        assert len(token) == 32
        assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    with pytest.raises(ValueError):
        security.random_token(0)


def test_random_code_alphabet_has_no_ambiguous_chars() -> None:
    code = "".join(security.random_code(64) for _ in range(20))
    assert set(code) <= set(security.CODE_ALPHABET)
    assert not set(code) & set("01OIL")
    assert len(security.random_code()) == 8
    with pytest.raises(ValueError):
        security.random_code(0)


def test_random_sub_id_format() -> None:
    sub_id = security.random_sub_id()
    assert len(sub_id) == 16
    assert set(sub_id) <= set("abcdefghijklmnopqrstuvwxyz0123456789")


def test_sha256_and_const_eq() -> None:
    assert security.sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert security.const_eq("секрет", "секрет")
    assert security.const_eq(b"x", "x")
    assert not security.const_eq("a", "b")
    assert not security.const_eq("a", "aa")


# --- crypto ---------------------------------------------------------------------------------------
def test_secretbox_roundtrip_and_prefix() -> None:
    box = SecretBox("key-1")
    token = box.encrypt("p@ss — пароль")
    assert token.startswith("enc:") and is_encrypted(token)
    assert "p@ss" not in token
    assert box.decrypt(token) == "p@ss — пароль"
    assert box.encrypt(token) == token  # already encrypted values are not double-wrapped
    assert box.encrypt("same") != box.encrypt("same")  # random IV


def test_secretbox_plaintext_passthrough() -> None:
    assert SecretBox("k").decrypt("legacy-plaintext") == "legacy-plaintext"
    assert SecretBox("k").decrypt("") == ""


def test_secretbox_is_deterministic_per_key_and_rejects_other_keys() -> None:
    token = SecretBox("key-1").encrypt("v")
    assert SecretBox("key-1").decrypt(token) == "v"  # same key derives the same Fernet key
    with pytest.raises(DecryptionError):
        SecretBox("key-2").decrypt(token)
    with pytest.raises(DecryptionError):
        SecretBox("key-1").decrypt("enc:garbage")


def test_secretbox_requires_key_and_hides_it() -> None:
    with pytest.raises(ValueError):
        SecretBox("")
    assert "key" not in repr(SecretBox("super-secret-key")).replace("SecretBox", "")


# --- time -----------------------------------------------------------------------------------------
def test_formatting_uses_configured_zone() -> None:
    settings = make_settings(tz="Europe/Moscow")
    dt = datetime(2026, 12, 4, 16, 0, tzinfo=UTC)
    assert time.fmt_date(dt, settings) == "04.12.2026"
    assert time.fmt_dt(dt, settings) == "04.12.2026 19:00"
    assert time.local(dt, settings).utcoffset() == timedelta(hours=3)
    late = datetime(2026, 12, 31, 22, 30, tzinfo=UTC)  # already Jan 1 in Moscow
    assert time.today_local(settings, late) == date(2027, 1, 1)
    assert time.local_day_start(date(2027, 1, 1), settings) == datetime(2026, 12, 31, 21, 0, tzinfo=UTC)


def test_naive_datetimes_are_rejected() -> None:
    settings = make_settings()
    with pytest.raises(ValueError):
        time.fmt_date(datetime(2026, 1, 1), settings)
    with pytest.raises(ValueError):
        time.days_left(datetime(2026, 1, 1), time.utcnow())


def test_utcnow_is_aware() -> None:
    assert time.utcnow().tzinfo is UTC
    assert isinstance(time.today_local(make_settings()), date)


@pytest.mark.parametrize(
    ("delta", "days"),
    [
        (timedelta(days=3), 3),
        (timedelta(days=2, seconds=1), 3),
        (timedelta(seconds=1), 1),
        (timedelta(0), 0),
        (timedelta(days=-5), 0),
        (timedelta(days=30), 30),
    ],
)
def test_days_left_rounds_up_and_floors_at_zero(delta: timedelta, days: int) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert time.days_left(now + delta, now) == days


# --- bytes ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("n", "text"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1 KB"),
        (1536, "1.5 KB"),
        (1024**3 + 1024**3 // 2, "1.5 GB"),
        (10 * 1024**3, "10 GB"),
        (1024**2 - 1, "1 MB"),  # rounds up across the unit boundary
        (5 * 1024**4, "5 TB"),
        (3 * 1024**6, "3072 PB"),
        (-1536, "-1.5 KB"),
    ],
)
def test_fmt_bytes(n: int, text: str) -> None:
    assert ubytes.fmt_bytes(n) == text


def test_gb_to_bytes() -> None:
    assert ubytes.gb_to_bytes(0) == 0
    assert ubytes.gb_to_bytes(1) == 1024**3
    assert ubytes.gb_to_bytes(1.5) == 1024**3 * 3 // 2
    with pytest.raises(ValueError):
        ubytes.gb_to_bytes(-1)
