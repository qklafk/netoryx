from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.db.session import SessionFactory
from app.services import tickets
from app.services.notifications import AdminEvent
from tests.test_mod_miniapp_support import USER_ID, Env, add_user, miniapp_env


@pytest.fixture
async def env(tmp_path: Path, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with miniapp_env(tmp_path, session_factory, monkeypatch) as environment:
        await add_user(environment.ctx.session_factory, environment.settings)
        yield environment


async def test_ticket_lifecycle(env: Env) -> None:
    created = await env.post("/tickets", {"text": "Не подключается на iPhone <b>", "subject": "iPhone"})
    assert created.status_code == 201, created.text
    view = created.json()
    ticket_id = view["ticket"]["id"]
    assert view["ticket"]["subject"] == "iPhone" and view["ticket"]["status"] == "open"
    assert (
        view["messages"][0]["text"] == "Не подключается на iPhone <b>" and view["messages"][0]["from_support"] is False
    )
    event, text = env.notified[-1]
    assert event is AdminEvent.TICKETS and f"#{ticket_id}" in text and "&lt;b&gt;" in text

    async with env.ctx.session_factory.begin() as session:
        await tickets.reply(session, ticket_id, 1000, "Обновите приложение", is_admin=True)
    assert (await env.get("/me")).json()["unread_tickets"] == 1
    listing = (await env.get("/tickets")).json()
    assert [t["id"] for t in listing["items"]] == [ticket_id] and listing["items"][0]["status"] == "answered"
    assert listing["open_count"] == 1 and listing["max_open"] == tickets.MAX_OPEN_TICKETS_PER_USER
    assert (await env.get("/me")).json()["unread_tickets"] == 0  # opening the list marks replies as read

    reply = await env.post(f"/tickets/{ticket_id}/reply", {"text": "Помогло, спасибо!"})
    assert reply.status_code == 200
    messages = reply.json()["messages"]
    assert [m["from_support"] for m in messages] == [False, True, False]
    assert reply.json()["ticket"]["status"] == "open"
    assert env.notified[-1][0] is AdminEvent.TICKETS

    closed = await env.post(f"/tickets/{ticket_id}/close")
    assert closed.status_code == 200 and closed.json()["status"] == "closed"
    assert (await env.get(f"/tickets/{ticket_id}")).json()["ticket"]["status"] == "closed"


async def test_foreign_and_missing_tickets(env: Env) -> None:
    ticket_id = (await env.post("/tickets", {"text": "Вопрос"})).json()["ticket"]["id"]
    await add_user(env.ctx.session_factory, env.settings, 777)
    for resp in (
        await env.get(f"/tickets/{ticket_id}", user_id=777),
        await env.post(f"/tickets/{ticket_id}/reply", {"text": "hi"}, user_id=777),
        await env.post(f"/tickets/{ticket_id}/close", user_id=777),
        await env.get("/tickets/999"),
    ):
        assert resp.status_code == 404 and resp.json()["error"] == "ticket_not_found"
    assert (await env.get("/tickets", user_id=777)).json()["items"] == []


async def test_ticket_validation_and_limits(env: Env) -> None:
    assert (await env.post("/tickets", {"text": ""})).status_code == 422
    assert (await env.post("/tickets", {"text": "x" * 4001})).status_code == 422
    blank = await env.post("/tickets", {"text": "   "})
    assert blank.status_code == 422  # stripped to empty
    for n in range(tickets.MAX_OPEN_TICKETS_PER_USER):
        assert (await env.post("/tickets", {"text": f"Вопрос {n}"})).status_code == 201
    too_many = await env.post("/tickets", {"text": "Ещё один"})
    assert too_many.status_code == 409 and too_many.json()["error"] == "ticket_too_many_open"
    assert "3 открытых" in too_many.json()["message"]
    assert len([e for e, _ in env.notified if e is AdminEvent.TICKETS]) == tickets.MAX_OPEN_TICKETS_PER_USER
    assert (await env.post("/tickets", {"text": "И ещё"})).status_code == 409  # 5th attempt in 10 minutes
    limited = await env.post("/tickets", {"text": "И ещё раз"})
    assert limited.status_code == 429


async def test_tickets_can_be_switched_off(env: Env) -> None:
    env.ctx.box.update(disabled_modules=["tickets"])
    resp = await env.get("/tickets")
    assert resp.status_code == 404 and resp.json()["error"] == "feature_disabled"
    me = (await env.get("/me")).json()
    assert me["unread_tickets"] == 0 and me["id"] == USER_ID and me["features"]["tickets"] is False
