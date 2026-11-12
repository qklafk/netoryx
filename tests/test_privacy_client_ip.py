"""Customer IP addresses are recorded only as far as the owner's CLIENT_IP_LOGGING policy allows."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI

from app.db.models import CabinetSession, User
from app.db.session import SessionFactory
from app.services import cabinet
from app.utils.privacy import client_ip_for_record
from app.web.security import RequestContextMiddleware
from tests.conftest import make_settings
from tests.test_web_support import AsgiClient


@pytest.mark.parametrize(
    ("ip", "mode", "expected"),
    [
        ("203.0.113.45", "off", None),
        ("203.0.113.45", "masked", "203.0.113.x"),
        ("203.0.113.45", "full", "203.0.113.45"),
        ("2001:db8:1:2::5", "masked", "2001:db8:1::/48"),
        ("not-an-ip", "masked", None),
        (None, "full", None),
    ],
)
def test_client_ip_policy(ip: str | None, mode: str, expected: str | None) -> None:
    assert client_ip_for_record(ip, mode) == expected


@pytest.mark.parametrize(("mode", "shown"), [("off", "-"), ("masked", "198.51.100.x"), ("full", "198.51.100.7")])
async def test_access_log_follows_the_policy(mode: str, shown: str, caplog: pytest.LogCaptureFixture) -> None:
    settings = make_settings(client_ip_logging=mode)
    app = FastAPI()

    @app.get("/sub/{token}")
    async def sub(token: str) -> dict[str, str]:
        return {"ok": token[:1]}

    app.add_middleware(RequestContextMiddleware, settings_getter=lambda: settings)
    with caplog.at_level(logging.INFO, logger="app.web.access"):
        await AsgiClient(app, client_ip="198.51.100.7").request("GET", "/sub/abcdefghijklmnopqrstuvwxyz0123")
    line = next(r.getMessage() for r in caplog.records if r.name == "app.web.access")
    assert line.split(" ", 1)[0] == shown
    assert "abcdefghijklmnopqrstuvwxyz0123" not in line


async def test_cabinet_session_keeps_no_ip_or_browser_when_logging_is_off(session_factory: SessionFactory) -> None:
    async with session_factory.begin() as session:
        session.add(User(tg_id=77, referral_code="r77", sub_token="t77"))
        await session.flush()
        _, off = await cabinet.create_session(
            session, make_settings(client_ip_logging="off"), 77, "bot", ip="198.51.100.7", user_agent="Firefox"
        )
        _, masked = await cabinet.create_session(
            session, make_settings(client_ip_logging="masked"), 77, "bot", ip="198.51.100.7", user_agent="Firefox"
        )
    async with session_factory() as session:
        stored_off = await session.get(CabinetSession, off.id)
        stored_masked = await session.get(CabinetSession, masked.id)
    assert stored_off is not None and stored_off.ip is None and stored_off.user_agent is None
    assert stored_masked is not None and stored_masked.ip == "198.51.100.x" and stored_masked.user_agent == "Firefox"
