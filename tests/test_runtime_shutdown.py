"""Startup seeding across processes and a graceful shutdown: in-flight updates finish, open streams do not block."""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from app.core.context import AppContext
from app.runtime import Runtime, _UvicornServer
from tests.test_runtime_boot import FakeTelegram, factories, settings_for


class OneUpdateTelegram(FakeTelegram):
    """Delivers a single text message on the first ``getUpdates``."""

    def __init__(self) -> None:
        super().__init__()
        self.delivered = False

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, GetUpdates) and not self.delivered:
            self.calls.append(method)
            self.delivered = True
            user = TgUser(id=5, is_bot=False, first_name="U")
            message = Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=5, type="private"),
                from_user=user,
                text="pay",
            )
            return [Update(update_id=1, message=message)]
        return await super().make_request(bot, method, timeout)


async def test_panel_bootstrap_runs_when_another_process_seeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_for(tmp_path, role="worker", xui_host="https://panel.example.com/base", xui_api_token="t")
    runtime = Runtime(settings, role="worker")
    started: list[Any] = []

    async def bootstrap(self: Runtime, ctx: Any) -> None:
        started.append(ctx)

    monkeypatch.setattr(Runtime, "_bootstrap_panels", bootstrap)

    class SeedLockHeldElsewhere:
        @asynccontextmanager
        async def job_lock(self, name: str, ttl: float) -> AsyncIterator[bool]:
            yield False

    ctx = types.SimpleNamespace(settings=settings, locks=SeedLockHeldElsewhere())
    await runtime._seed(ctx)  # type: ignore[arg-type]
    assert "panel bootstrap" in runtime._tasks
    await asyncio.gather(*runtime._tasks.values())
    assert started == [ctx]
    await runtime.stop()


async def test_polling_shutdown_waits_for_updates_in_progress(tmp_path: Path) -> None:
    telegram = OneUpdateTelegram()
    entered = asyncio.Event()
    finished: list[str] = []

    async def slow_handler(message: Message) -> None:
        entered.set()
        await asyncio.sleep(0.5)  # e.g. crediting a payment
        finished.append(message.text or "")

    base = factories(telegram, [])

    async def middlewares(dispatcher: Dispatcher, ctx: AppContext) -> None:
        dispatcher.message.register(slow_handler)

    base.middlewares = middlewares
    runtime = Runtime(settings_for(tmp_path, role="bot"), factories=base)
    try:
        await runtime.start()
        await asyncio.wait_for(entered.wait(), 5)
        runtime.request_stop()
    finally:
        await runtime.stop()
    assert finished == ["pay"]


async def test_open_stream_does_not_block_web_shutdown(tmp_path: Path) -> None:
    stopped: list[bool] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        stopped.append(True)  # where pending webhook updates are drained

    app = FastAPI(lifespan=lifespan)

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def stream() -> AsyncIterator[bytes]:
            while True:
                yield b"data: tick\n\n"
                await asyncio.sleep(0.05)

        return StreamingResponse(stream(), media_type="text/event-stream")

    server = _UvicornServer(app, settings_for(tmp_path, shutdown_timeout_sec=5))
    await server.start()
    async with aiohttp.ClientSession() as http, http.get(f"http://127.0.0.1:{server.port}/events") as resp:
        assert await resp.content.readline()
        await asyncio.wait_for(server.stop(), 4)
    assert stopped == [True]
