"""The /cabinet command: a one-time link to the web cabinet."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.db.session import SessionFactory
from app.modules.web_cabinet import module as cabinet_module
from app.services import cabinet
from tests.test_middlewares_support import message_update
from tests.test_mod_personal_support import Harness, harnesses

USER = 4242


@pytest.fixture
async def harness_factory(session_factory: SessionFactory, tmp_path: Path) -> AsyncIterator[Any]:
    async with harnesses(session_factory, tmp_path) as factory:
        yield factory


async def test_command_sends_a_working_login_link(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(cabinet_module, cabinet_enabled=True, public_base_url="https://bot.example.com")
    await h.feed(message_update(USER, "/cabinet"))
    urls = [b.url for b in h.buttons() if getattr(b, "url", None)]
    assert urls and urls[0].startswith("https://bot.example.com/cabinet/login/")
    token = urls[0].rsplit("/", 1)[1]
    async with session_factory.begin() as session:
        data = await cabinet.redeem_token(session, cabinet.LOGIN_PREFIX, token)
    assert data is not None and data["user_id"] == USER and data["method"] == "bot"


async def test_command_explains_when_the_cabinet_is_off(harness_factory: Any, session_factory: SessionFactory) -> None:
    h: Harness = harness_factory(cabinet_module, cabinet_enabled=True, cabinet_login_bot=False)
    await h.feed(message_update(USER, "/cabinet"))
    assert not [b for b in h.buttons() if getattr(b, "url", None)]
    assert h.last_text()
