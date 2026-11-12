from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi import APIRouter

from app.core.modules import FeatureModule
from app.db.models import Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory
from app.modules.members.service import REMOVAL_KEY_PREFIX
from app.services import kv, users
from app.utils.time import utcnow
from app.web.auth import MiniAppUser
from app.web.tma import InitDataError, sign_init_data, validate_init_data
from tests.conftest import make_settings
from tests.test_web_support import AsgiClient, FakeCtx, make_ctx, web_app

BOT_TOKEN = "123456:TEST"
USER_ID = 777


def init_data(
    *,
    user: dict[str, Any] | None = None,
    auth_date: datetime | None = None,
    token: str = BOT_TOKEN,
    extra: dict[str, str] | None = None,
    tamper: bool = False,
) -> str:
    fields = {
        "query_id": "AAH",
        "user": json.dumps(user or {"id": USER_ID, "first_name": "Ann", "username": "Ann_A", "language_code": "en"}),
        "auth_date": str(int((auth_date or datetime.now(UTC)).timestamp())),
        **(extra or {}),
    }
    fields["hash"] = sign_init_data(fields, token)
    if tamper:
        fields["auth_date"] = str(int(fields["auth_date"]) + 1)
    return urlencode(fields)


# ---------------------------------------------------------------------------------------------
# initData validation
# ---------------------------------------------------------------------------------------------
def test_valid_init_data() -> None:
    data = validate_init_data(init_data(extra={"start_param": "ref_X", "signature": "sig"}), BOT_TOKEN)
    assert data.user.id == USER_ID and data.user.username == "Ann_A" and data.user.language_code == "en"
    assert data.start_param == "ref_X" and data.query_id == "AAH"
    assert "hash" not in data.fields


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "x" * 9000,
        "a=1&a=2",
        "user=%7B%7D&auth_date=1",  # no hash
        "noequals",
    ],
)
def test_malformed_init_data(raw: str) -> None:
    with pytest.raises(InitDataError):
        validate_init_data(raw, BOT_TOKEN)


def test_forged_or_foreign_init_data() -> None:
    with pytest.raises(InitDataError, match="hash"):
        validate_init_data(init_data(tamper=True), BOT_TOKEN)
    with pytest.raises(InitDataError, match="hash"):
        validate_init_data(init_data(token="999:OTHER"), BOT_TOKEN)


def test_init_data_age() -> None:
    now = datetime.now(UTC)
    with pytest.raises(InitDataError, match="expired"):
        validate_init_data(init_data(auth_date=now - timedelta(hours=25)), BOT_TOKEN)
    with pytest.raises(InitDataError, match="future"):
        validate_init_data(init_data(auth_date=now + timedelta(hours=1)), BOT_TOKEN)
    assert validate_init_data(init_data(auth_date=now - timedelta(hours=23)), BOT_TOKEN).user.id == USER_ID
    with pytest.raises(InitDataError, match="expired"):
        validate_init_data(init_data(auth_date=now - timedelta(hours=2)), BOT_TOKEN, max_age=timedelta(hours=1))


@pytest.mark.parametrize(
    "user",
    [
        {"id": 0},
        {"id": "5"},
        {"id": True},
        {"id": 5, "is_bot": True},
    ],
)
def test_invalid_user(user: dict[str, Any]) -> None:
    with pytest.raises(InitDataError):
        validate_init_data(init_data(user=user), BOT_TOKEN)


def test_user_must_be_object() -> None:
    fields = {"user": "[1]", "auth_date": str(int(datetime.now(UTC).timestamp()))}
    fields["hash"] = sign_init_data(fields, BOT_TOKEN)
    with pytest.raises(InitDataError, match="object"):
        validate_init_data(urlencode(fields), BOT_TOKEN)
    fields = {"auth_date": str(int(datetime.now(UTC).timestamp()))}
    fields["hash"] = sign_init_data(fields, BOT_TOKEN)
    with pytest.raises(InitDataError, match="missing"):
        validate_init_data(urlencode(fields), BOT_TOKEN)


# ---------------------------------------------------------------------------------------------
# miniapp_user dependency through a module router
# ---------------------------------------------------------------------------------------------
def _module() -> FeatureModule:
    router = APIRouter()

    @router.get("/me")
    async def me(user: MiniAppUser) -> dict[str, Any]:
        return {"id": user.tg_id, "username": user.username, "language": user.language}

    @router.get("/open")
    async def open_() -> dict[str, bool]:
        return {"ok": True}  # still protected by the router-level initData check

    return FeatureModule(name="probe", title_ru="p", title_en="p", miniapp_routers=[router])


@pytest.fixture
def ctx(session_factory: SessionFactory) -> FakeCtx:
    return make_ctx(make_settings(), session_factory, _module())


@pytest.fixture
def client(ctx: FakeCtx) -> AsgiClient:
    return AsgiClient(web_app(ctx))


def _auth(raw: str | None = None) -> dict[str, str]:
    return {"authorization": f"tma {raw or init_data()}"}


async def test_miniapp_registers_user(client: AsgiClient, ctx: FakeCtx) -> None:
    resp = await client.get("/api/app/probe/me", headers=_auth())
    assert resp.status == 200, resp.text
    assert resp.json() == {"id": USER_ID, "username": "ann_a", "language": "en"}
    again = await client.get("/api/app/probe/me", headers=_auth())
    assert again.json()["id"] == USER_ID
    async with ctx.session_factory() as s:
        assert await s.get(User, USER_ID) is not None


async def test_miniapp_auth_errors(client: AsgiClient) -> None:
    assert (await client.get("/api/app/probe/open")).status == 401
    assert (await client.get("/api/app/probe/open", headers={"authorization": "Bearer x"})).status == 401
    bad = await client.get("/api/app/probe/open", headers=_auth(init_data(tamper=True)))
    assert bad.status == 401 and bad.json()["error"] == "invalid_init_data"
    assert (await client.get("/api/app/probe/open", headers=_auth())).status == 200


async def test_banned_user(client: AsgiClient, ctx: FakeCtx) -> None:
    assert (await client.get("/api/app/probe/me", headers=_auth())).status == 200
    async with ctx.session_factory.begin() as s:
        user = await s.get(User, USER_ID)
        assert user is not None
        user.is_banned = True
    resp = await client.get("/api/app/probe/me", headers=_auth())
    assert resp.status == 403 and resp.json()["error"] == "banned"


async def test_personal_mode_does_not_register_strangers(client: AsgiClient, ctx: FakeCtx) -> None:
    ctx.settings = make_settings(app_mode="personal")
    resp = await client.get("/api/app/probe/me", headers=_auth())
    assert resp.status == 403 and resp.json()["error"] == "not_registered"


async def test_personal_mode_lets_only_members_and_admins_in(client: AsgiClient, ctx: FakeCtx) -> None:
    # A stranger refused by the bot still gets a user row; the Mini App must refuse them as well.
    async with ctx.session_factory.begin() as s:
        await users.get_or_create(s, ctx.settings, USER_ID, "ann", "Ann", "en")
    ctx.settings = make_settings(app_mode="personal")
    stranger = await client.get("/api/app/probe/me", headers=_auth())
    assert stranger.status == 403 and stranger.json()["error"] == "invite_only"

    async with ctx.session_factory.begin() as s:
        s.add(Subscription(user_id=USER_ID, client_uuid="u-777", status=SubscriptionStatus.ACTIVE, expires_at=utcnow()))
    assert (await client.get("/api/app/probe/me", headers=_auth())).status == 200

    # A member being removed loses the Mini App together with the bot.
    async with ctx.session_factory.begin() as s:
        await kv.set(s, f"{REMOVAL_KEY_PREFIX}{USER_ID}", utcnow().isoformat())
    removed = await client.get("/api/app/probe/me", headers=_auth())
    assert removed.status == 403 and removed.json()["error"] == "invite_only"

    ctx.settings = make_settings(app_mode="personal", admin_ids=[USER_ID])
    assert (await client.get("/api/app/probe/me", headers=_auth())).status == 200
    ctx.settings = make_settings()
    assert (await client.get("/api/app/probe/me", headers=_auth())).status == 200


async def test_miniapp_disabled(client: AsgiClient, ctx: FakeCtx) -> None:
    ctx.settings = make_settings(miniapp_enabled=False)
    assert (await client.get("/api/app/probe/me", headers=_auth())).status == 404
