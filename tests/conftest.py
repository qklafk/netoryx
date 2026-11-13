from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.db.models import Base
from app.db.session import SessionFactory, create_engine, create_session_factory
from tests.fakes import FakePanel


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "bot_token": "123456:TEST",
        "admin_ids": [1000],
        "database_url": "sqlite+aiosqlite:///:memory:",
        "public_base_url": "https://bot.example.com",
        "secret_key": "test-secret-key-0123456789abcdef",
        "brand_name": "TestVPN",
        "backup_enabled": False,
        # The Telegram proxies are off by default; the tests of their code turn them on explicitly.
        "tg_proxy_access": "everyone",
        "tg_socks_enabled": True,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def panel() -> FakePanel:
    return FakePanel()
