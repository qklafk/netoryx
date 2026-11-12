from __future__ import annotations

from sqlalchemy import select

from app.db.models import PublicApiKey, User
from app.services import public_api
from tests.test_web_support import AsgiClient, make_ctx, web_app


async def test_public_key_lifecycle(session_factory) -> None:
    async with session_factory.begin() as session:
        session.add(User(tg_id=42, referral_code="R", sub_token="S", username="alice", balance_kop=123))
        token, row = await public_api.create_key(session, 42, "test")
        assert token.startswith("vpb_") and row.token_hash not in token and row.token_hint == token[-6:]
    async with session_factory.begin() as session:
        row = await public_api.authenticate(session, token)
        assert row is not None and row.user_id == 42
        await public_api.revoke_key(session, row)
    async with session_factory.begin() as session:
        assert await public_api.authenticate(session, token) is None


async def test_public_me_is_key_scoped(session_factory, settings) -> None:
    async with session_factory.begin() as session:
        session.add(User(tg_id=42, referral_code="R", sub_token="S", username="alice", balance_kop=123))
        token, _ = await public_api.create_key(session, 42, "test")
    client = AsgiClient(web_app(make_ctx(settings, session_factory)))
    denied = await client.get("/api/public/me")
    assert denied.status == 401
    response = await client.get("/api/public/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status == 200
    assert response.json() == {"telegram_id": 42, "username": "alice", "balance_kop": 123, "language": "ru"}
    async with session_factory() as session:
        assert await session.scalar(select(PublicApiKey.last_used_at).where(PublicApiKey.user_id == 42)) is not None
