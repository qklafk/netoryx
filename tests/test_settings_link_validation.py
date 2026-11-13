"""Public links in the settings are rendered as hrefs, so only web and Telegram schemes are accepted."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings

LINK_FIELDS = ("support_url", "channel_url", "terms_url", "privacy_url", "required_channel_url")


@pytest.mark.parametrize("field", LINK_FIELDS)
@pytest.mark.parametrize("bad", ["javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "//evil.example"])
def test_link_settings_reject_script_schemes(field: str, bad: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: bad})


@pytest.mark.parametrize("field", LINK_FIELDS)
@pytest.mark.parametrize("good", ["https://t.me/support", "http://example.com/a", "tg://resolve?domain=x"])
def test_link_settings_accept_web_and_telegram_links(field: str, good: str) -> None:
    assert getattr(make_settings(**{field: good}), field) == good


def test_link_settings_may_be_empty() -> None:
    assert make_settings(support_url=None).support_url is None


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        ("t.me/support", "https://t.me/support"),
        ("@support", "https://t.me/support"),
        ("example.com/a", "https://example.com/a"),
    ],
)
def test_links_without_a_scheme_still_work(written: str, stored: str) -> None:
    # an existing .env with such a value must not stop the application from starting
    assert make_settings(support_url=written).support_url == stored
