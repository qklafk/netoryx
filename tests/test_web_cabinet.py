"""Web cabinet: login methods, sessions, CSRF/origin checks, e-mail flows and the /cabinet page."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter
from sqlalchemy import select

from app.core.modules import FeatureModule
from app.db.models import CabinetSession, Subscription, SubscriptionStatus, User, UserEmail
from app.db.session import SessionFactory
from app.modules.web_cabinet import module as cabinet_module
from app.services import cabinet, kv, mailer, users
from app.utils.time import utcnow
from app.web.auth import MiniAppUser
from tests.conftest import make_settings
from tests.test_web_miniapp import BOT_TOKEN, init_data
from tests.test_web_support import AsgiClient, FakeCtx, Resp, make_ctx, web_app

ORIGIN = "https://bot.example.com"
USER_ID = 777
OTHER_ID = 778
SETTINGS: dict[str, Any] = {
    "cabinet_enabled": True,
    "cabinet_login_email": True,
    "cabinet_login_telegram": True,
    "smtp_host": "smtp.example.com",
    "smtp_from": "VPN <no-reply@example.com>",
}


def probe_module() -> FeatureModule:
    router = APIRouter()

    @router.get("/me")
    async def me(user: MiniAppUser) -> dict[str, Any]:
        return {"id": user.tg_id}

    @router.post("/act")
    async def act(user: MiniAppUser) -> dict[str, Any]:
        return {"id": user.tg_id}

    return FeatureModule(name="probe", title_ru="p", title_en="p", miniapp_routers=[router])


class Mailbox:
    """Collects the mail the cabinet would send."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, settings: Any, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))

    def link(self, index: int = -1) -> str:
        match = re.search(r"https://\S+", self.sent[index][2])
        assert match is not None
        return match.group(0)

    def token(self, index: int = -1) -> str:
        return self.link(index).rsplit("/", 1)[1]


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> Mailbox:
    box = Mailbox()
    monkeypatch.setattr(mailer, "send_mail", box.send)
    return box


class StubI18n:
    def t(self, key: str, lang: str, /, **kw: Any) -> str:
        return f"{key} {kw.get('link', '')}"


@pytest.fixture
def ctx(session_factory: SessionFactory) -> FakeCtx:
    fake = make_ctx(make_settings(**SETTINGS), session_factory, probe_module(), cabinet_module)
    fake.i18n = StubI18n()  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def client(ctx: FakeCtx) -> AsgiClient:
    return AsgiClient(web_app(ctx))


async def make_user(ctx: FakeCtx, tg_id: int = USER_ID, **fields: Any) -> None:
    async with ctx.session_factory.begin() as session:
        user, _ = await users.get_or_create(session, ctx.settings, tg_id, f"u{tg_id}", "Ann", "en")
        for name, value in fields.items():
            setattr(user, name, value)


async def login(client: AsgiClient, ctx: FakeCtx, tg_id: int = USER_ID, method: str = "bot") -> dict[str, str]:
    """Log ``client`` in through a one-time link; returns the session info."""
    token = await cabinet.issue_token(
        ctx.session_factory, cabinet.LOGIN_PREFIX, {"user_id": tg_id, "method": method}, cabinet.LOGIN_TTL
    )
    resp = await client.post("/api/cabinet/auth/redeem", json={"token": token}, headers={"origin": ORIGIN})
    assert resp.status == 200, resp.text
    data: dict[str, str] = resp.json()
    return data


def csrf(token: str) -> dict[str, str]:
    return {"x-csrf-token": token, "origin": ORIGIN}


# ---------------------------------------------------------------------------------------------
# Login link
# ---------------------------------------------------------------------------------------------
async def test_login_link_sets_a_lax_httponly_cookie_and_authenticates(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx)
    token = await cabinet.issue_token(
        ctx.session_factory, cabinet.LOGIN_PREFIX, {"user_id": USER_ID, "method": "bot"}, cabinet.LOGIN_TTL
    )
    resp = await client.post("/api/cabinet/auth/redeem", json={"token": token}, headers={"origin": ORIGIN})
    assert resp.status == 200 and resp.json()["user_id"] == USER_ID
    cookie = next(iter(resp.set_cookies().values())).lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie
    assert next(iter(resp.set_cookies())).startswith("__Host-")
    me = await client.get("/api/app/probe/me")
    assert me.status == 200 and me.json() == {"id": USER_ID}
    # The token only works once.
    again = await AsgiClient(client.app).post(
        "/api/cabinet/auth/redeem", json={"token": token}, headers={"origin": ORIGIN}
    )
    assert again.status == 401 and again.json()["error"] == "invalid_token"


async def test_expired_and_unknown_login_links(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx)
    old = datetime.now(UTC) - timedelta(hours=1)
    expired = await cabinet.issue_token(
        ctx.session_factory, cabinet.LOGIN_PREFIX, {"user_id": USER_ID}, cabinet.LOGIN_TTL, now=old
    )
    for token in (expired, "x" * 43, "short"):
        resp = await client.post("/api/cabinet/auth/redeem", json={"token": token}, headers={"origin": ORIGIN})
        assert resp.status == 401 and resp.json()["error"] == "invalid_token"


async def test_login_requires_same_site_origin(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx)
    token = await cabinet.issue_token(
        ctx.session_factory, cabinet.LOGIN_PREFIX, {"user_id": USER_ID}, cabinet.LOGIN_TTL
    )
    bad = await client.post("/api/cabinet/auth/redeem", json={"token": token}, headers={"origin": "https://evil.test"})
    assert bad.status == 403 and bad.json()["error"] == "csrf_failed"
    assert (await client.get("/api/app/probe/me")).status == 401  # nothing was granted
    # The refused request did not burn the token.
    ok = await client.post("/api/cabinet/auth/redeem", json={"token": token}, headers={"origin": ORIGIN})
    assert ok.status == 200


async def test_login_attempts_are_limited_per_network(client: AsgiClient) -> None:
    statuses = [
        (await client.post("/api/cabinet/auth/redeem", json={"token": f"{i:043d}"}, headers={"origin": ORIGIN})).status
        for i in range(12)
    ]
    assert statuses[:10] == [401] * 10 and statuses[10] == 429


async def test_methods_can_be_switched_off(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx)
    ctx.settings = make_settings(**{**SETTINGS, "cabinet_login_bot": False})
    token = await cabinet.issue_token(
        ctx.session_factory, cabinet.LOGIN_PREFIX, {"user_id": USER_ID, "method": "bot"}, cabinet.LOGIN_TTL
    )
    resp = await client.post("/api/cabinet/auth/redeem", json={"token": token}, headers={"origin": ORIGIN})
    assert resp.status == 403 and resp.json()["error"] == "method_disabled"
    config = (await client.get("/api/cabinet/auth/config")).json()
    assert config["link"] is False and config["telegram"] is True and config["email"] is True


async def test_disabled_cabinet_answers_404(session_factory: SessionFactory) -> None:
    fake = make_ctx(make_settings(), session_factory, probe_module(), cabinet_module)
    client = AsgiClient(web_app(fake))
    assert (await client.get("/api/cabinet/auth/config")).status == 404
    assert (await client.get("/api/app/probe/me")).status == 401  # only Mini App initData is accepted


# ---------------------------------------------------------------------------------------------
# Sessions, CSRF, initData
# ---------------------------------------------------------------------------------------------
async def test_mutating_requests_need_csrf_and_origin(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx)
    session = await login(client, ctx)
    assert (await client.post("/api/app/probe/act")).status == 403
    no_header = await client.post("/api/app/probe/act", headers={"origin": ORIGIN})
    assert no_header.status == 403 and no_header.json()["error"] == "csrf_failed"
    wrong = await client.post("/api/app/probe/act", headers=csrf("0" * 43))
    assert wrong.status == 403
    foreign = await client.post(
        "/api/app/probe/act", headers={"x-csrf-token": session["csrf_token"], "origin": "https://evil.test"}
    )
    assert foreign.status == 403
    cross = await client.post(
        "/api/app/probe/act", headers={**csrf(session["csrf_token"]), "sec-fetch-site": "cross-site"}
    )
    assert cross.status == 403
    assert (await client.post("/api/app/probe/act", headers=csrf(session["csrf_token"]))).status == 200
    got = await client.get("/api/cabinet/auth/session")
    assert got.status == 200 and got.json()["csrf_token"] == session["csrf_token"]


async def test_init_data_path_is_unchanged(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx)
    await login(client, ctx)
    assert BOT_TOKEN
    resp = await client.get("/api/app/probe/me", headers={"authorization": f"tma {init_data()}"})
    assert resp.status == 200  # initData wins and needs no CSRF header
    assert (await client.post("/api/app/probe/act", headers={"authorization": f"tma {init_data()}"})).status == 200
    bad = await client.get("/api/app/probe/me", headers={"authorization": f"tma {init_data(tamper=True)}"})
    assert bad.status == 401 and bad.json()["error"] == "invalid_init_data"  # the cookie never rescues a bad header
    plain = AsgiClient(client.app)
    assert (await plain.get("/api/app/probe/me")).status == 401


async def test_logout_revokes_and_logout_all_ends_every_session(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx)
    first = await login(client, ctx)
    other = AsgiClient(client.app)
    second = await login(other, ctx)
    no_csrf = await client.post("/api/cabinet/auth/logout", headers={"origin": ORIGIN})
    assert no_csrf.status == 403
    out = await client.post("/api/cabinet/auth/logout", headers=csrf(first["csrf_token"]))
    assert out.status == 200
    assert (await client.get("/api/app/probe/me")).status == 401
    assert (await other.get("/api/app/probe/me")).status == 200  # another device is still in
    everywhere = await other.post("/api/cabinet/auth/logout-all", headers=csrf(second["csrf_token"]))
    assert everywhere.status == 200
    assert (await other.get("/api/app/probe/me")).status == 401
    async with ctx.session_factory() as db:
        rows = (await db.scalars(select(CabinetSession))).all()
    assert rows and all(row.revoked for row in rows)


async def test_expired_session_stops_working(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx)
    await login(client, ctx)
    async with ctx.session_factory.begin() as db:
        for row in (await db.scalars(select(CabinetSession))).all():
            row.expires_at = utcnow() - timedelta(minutes=1)
    resp = await client.get("/api/app/probe/me")
    assert resp.status == 401 and resp.json()["error"] == "session_expired"


async def test_banned_user_cannot_log_in_and_loses_the_session(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx)
    await login(client, ctx)
    async with ctx.session_factory.begin() as db:
        user = await db.get(User, USER_ID)
        assert user is not None
        user.is_banned = True
    resp = await client.get("/api/app/probe/me")
    assert resp.status == 403 and resp.json()["error"] == "banned"
    token = await cabinet.issue_token(
        ctx.session_factory, cabinet.LOGIN_PREFIX, {"user_id": USER_ID}, cabinet.LOGIN_TTL
    )
    fresh = await AsgiClient(client.app).post(
        "/api/cabinet/auth/redeem", json={"token": token}, headers={"origin": ORIGIN}
    )
    assert fresh.status == 403 and fresh.json()["error"] == "banned"


async def test_personal_mode_admits_members_only(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx)
    ctx.settings = make_settings(**SETTINGS, app_mode="personal")
    token = await cabinet.issue_token(
        ctx.session_factory, cabinet.LOGIN_PREFIX, {"user_id": USER_ID}, cabinet.LOGIN_TTL
    )
    stranger = await client.post("/api/cabinet/auth/redeem", json={"token": token}, headers={"origin": ORIGIN})
    assert stranger.status == 403 and stranger.json()["error"] == "invite_only"
    async with ctx.session_factory.begin() as db:
        db.add(Subscription(user_id=USER_ID, client_uuid="u-1", status=SubscriptionStatus.ACTIVE, expires_at=utcnow()))
    await login(client, ctx)
    assert (await client.get("/api/app/probe/me")).status == 200


# ---------------------------------------------------------------------------------------------
# Telegram Login Widget
# ---------------------------------------------------------------------------------------------
def widget_payload(
    *, user_id: int = USER_ID, age: timedelta = timedelta(seconds=5), tamper: bool = False
) -> dict[str, str]:
    fields = {
        "id": str(user_id),
        "first_name": "Ann",
        "username": "ann",
        "auth_date": str(int((datetime.now(UTC) - age).timestamp())),
    }
    fields["hash"] = cabinet.sign_widget_login(fields, BOT_TOKEN)
    if tamper:
        fields["first_name"] = "Eve"
    return fields


def test_widget_signature_matches_the_documented_algorithm() -> None:
    import hashlib
    import hmac

    fields = {"id": "1", "first_name": "A", "auth_date": "100"}
    expected = hmac.new(
        hashlib.sha256(BOT_TOKEN.encode()).digest(), b"auth_date=100\nfirst_name=A\nid=1", hashlib.sha256
    ).hexdigest()
    assert cabinet.sign_widget_login(fields, BOT_TOKEN) == expected


async def test_widget_login_creates_the_user_and_a_session(client: AsgiClient, ctx: FakeCtx) -> None:
    resp = await client.post("/api/cabinet/auth/telegram", json=widget_payload(), headers={"origin": ORIGIN})
    assert resp.status == 200, resp.text
    assert (await client.get("/api/app/probe/me")).json() == {"id": USER_ID}
    async with ctx.session_factory() as db:
        assert await db.get(User, USER_ID) is not None


async def test_widget_rejects_forged_stale_and_replayed_payloads(client: AsgiClient, ctx: FakeCtx) -> None:
    forged = await client.post(
        "/api/cabinet/auth/telegram", json=widget_payload(tamper=True), headers={"origin": ORIGIN}
    )
    assert forged.status == 401 and forged.json()["error"] == "invalid_login"
    stale = await client.post(
        "/api/cabinet/auth/telegram", json=widget_payload(age=timedelta(days=2)), headers={"origin": ORIGIN}
    )
    assert stale.status == 401
    future = await client.post(
        "/api/cabinet/auth/telegram", json=widget_payload(age=timedelta(hours=-3)), headers={"origin": ORIGIN}
    )
    assert future.status == 401
    wrong_key = widget_payload()
    wrong_key["hash"] = cabinet.sign_widget_login(wrong_key, "999:OTHER")
    assert (await client.post("/api/cabinet/auth/telegram", json=wrong_key, headers={"origin": ORIGIN})).status == 401
    missing = widget_payload()
    del missing["hash"]
    assert (await client.post("/api/cabinet/auth/telegram", json=missing, headers={"origin": ORIGIN})).status == 401
    good = widget_payload()
    assert (await client.post("/api/cabinet/auth/telegram", json=good, headers={"origin": ORIGIN})).status == 200
    replay = await AsgiClient(client.app).post("/api/cabinet/auth/telegram", json=good, headers={"origin": ORIGIN})
    assert replay.status == 401


async def test_widget_login_respects_ban_personal_mode_and_switch(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx, is_banned=True)
    banned = await client.post("/api/cabinet/auth/telegram", json=widget_payload(), headers={"origin": ORIGIN})
    assert banned.status == 403 and banned.json()["error"] == "banned"
    ctx.settings = make_settings(**SETTINGS, app_mode="personal")
    stranger = await client.post(
        "/api/cabinet/auth/telegram", json=widget_payload(user_id=OTHER_ID), headers={"origin": ORIGIN}
    )
    assert stranger.status == 403 and stranger.json()["error"] == "not_registered"
    ctx.settings = make_settings(**{**SETTINGS, "cabinet_login_telegram": False})
    off = await client.post("/api/cabinet/auth/telegram", json=widget_payload(), headers={"origin": ORIGIN})
    assert off.status == 403 and off.json()["error"] == "method_disabled"


# ---------------------------------------------------------------------------------------------
# E-mail
# ---------------------------------------------------------------------------------------------
async def confirm_email(client: AsgiClient, ctx: FakeCtx, box: Mailbox, tg_id: int, email: str) -> None:
    session = await login(client, ctx, tg_id)
    start = await client.post("/api/app/cabinet/email", json={"email": email}, headers=csrf(session["csrf_token"]))
    assert start.status == 200, start.text
    done = await AsgiClient(client.app).post(
        "/api/cabinet/auth/verify-email", json={"token": box.token()}, headers={"origin": ORIGIN}
    )
    assert done.status == 200, done.text


async def test_email_confirmation_and_login(client: AsgiClient, ctx: FakeCtx, mailbox: Mailbox) -> None:
    await make_user(ctx)
    await confirm_email(client, ctx, mailbox, USER_ID, "  Ann@Example.COM ")
    assert mailbox.sent[0][0] == "ann@example.com"
    assert mailbox.link().startswith("https://bot.example.com/cabinet/verify/")
    got = await client.get("/api/app/cabinet/email")
    assert got.json() == {"email": "ann@example.com", "available": True}

    guest = AsgiClient(client.app)
    asked = await guest.post(
        "/api/cabinet/auth/email/request", json={"email": "ANN@example.com"}, headers={"origin": ORIGIN}
    )
    assert asked.status == 200 and len(mailbox.sent) == 2
    assert mailbox.link().startswith("https://bot.example.com/cabinet/login/")
    redeemed = await guest.post("/api/cabinet/auth/redeem", json={"token": mailbox.token()}, headers={"origin": ORIGIN})
    assert redeemed.status == 200
    assert (await guest.get("/api/app/probe/me")).json() == {"id": USER_ID}
    async with ctx.session_factory() as db:
        row = (await db.scalars(select(CabinetSession).order_by(CabinetSession.id.desc()))).first()
    assert row is not None and row.method == "email"


async def test_email_login_does_not_reveal_whether_the_address_exists(
    client: AsgiClient, ctx: FakeCtx, mailbox: Mailbox
) -> None:
    await make_user(ctx)
    await confirm_email(client, ctx, mailbox, USER_ID, "ann@example.com")
    sent = len(mailbox.sent)
    guest = AsgiClient(client.app)
    known = await guest.post(
        "/api/cabinet/auth/email/request", json={"email": "ann@example.com"}, headers={"origin": ORIGIN}
    )
    unknown = await guest.post(
        "/api/cabinet/auth/email/request", json={"email": "nobody@example.com"}, headers={"origin": ORIGIN}
    )
    assert known.status == unknown.status == 200 and known.body == unknown.body
    assert len(mailbox.sent) == sent + 1  # only the known address got a message


async def test_email_requests_are_rate_limited(client: AsgiClient, ctx: FakeCtx, mailbox: Mailbox) -> None:
    results: list[Resp] = []
    for _ in range(4):
        results.append(
            await client.post(
                "/api/cabinet/auth/email/request", json={"email": "x@example.com"}, headers={"origin": ORIGIN}
            )
        )
    assert [r.status for r in results] == [200, 200, 200, 429]  # three per address and window
    other = [
        await client.post(
            "/api/cabinet/auth/email/request", json={"email": f"u{i}@example.com"}, headers={"origin": ORIGIN}
        )
        for i in range(3)
    ]
    assert [r.status for r in other] == [200, 429, 429]  # five per client network and window (4 used)
    bad = await AsgiClient(client.app, client_ip="198.51.100.7").post(
        "/api/cabinet/auth/email/request", json={"email": "not-an-email"}, headers={"origin": ORIGIN}
    )
    assert bad.status == 400


async def test_email_is_unique_and_a_foreign_address_gets_no_hint(
    client: AsgiClient, ctx: FakeCtx, mailbox: Mailbox
) -> None:
    await make_user(ctx)
    await make_user(ctx, OTHER_ID)
    await confirm_email(client, ctx, mailbox, USER_ID, "ann@example.com")
    sent = len(mailbox.sent)
    other = AsgiClient(client.app)
    session = await login(other, ctx, OTHER_ID)
    resp = await other.post(
        "/api/app/cabinet/email", json={"email": "ANN@example.com"}, headers=csrf(session["csrf_token"])
    )
    assert resp.status == 200 and len(mailbox.sent) == sent  # same answer, no mail
    # A confirmation link issued before the address was taken cannot steal it.
    token = await cabinet.issue_token(
        ctx.session_factory,
        cabinet.VERIFY_PREFIX,
        {"user_id": OTHER_ID, "email": "ann@example.com"},
        cabinet.VERIFY_TTL,
    )
    taken = await other.post("/api/cabinet/auth/verify-email", json={"token": token}, headers={"origin": ORIGIN})
    assert taken.status == 409 and taken.json()["error"] == "email_taken"
    async with ctx.session_factory() as db:
        rows = (await db.scalars(select(UserEmail))).all()
    assert [(r.user_id, r.email) for r in rows] == [(USER_ID, "ann@example.com")]


async def test_email_verification_link_is_single_use_and_removal(
    client: AsgiClient, ctx: FakeCtx, mailbox: Mailbox
) -> None:
    await make_user(ctx)
    await confirm_email(client, ctx, mailbox, USER_ID, "ann@example.com")
    reuse = await AsgiClient(client.app).post(
        "/api/cabinet/auth/verify-email", json={"token": mailbox.token()}, headers={"origin": ORIGIN}
    )
    assert reuse.status == 401
    session = (await client.get("/api/cabinet/auth/session")).json()
    removed = await client.request("DELETE", "/api/app/cabinet/email", headers=csrf(session["csrf_token"]))
    assert removed.status == 200
    assert (await client.get("/api/app/cabinet/email")).json()["email"] is None


async def test_email_login_is_off_without_smtp(client: AsgiClient, ctx: FakeCtx, mailbox: Mailbox) -> None:
    ctx.settings = make_settings(**{**SETTINGS, "smtp_host": None})
    assert (await client.get("/api/cabinet/auth/config")).json()["email"] is False
    resp = await client.post(
        "/api/cabinet/auth/email/request", json={"email": "ann@example.com"}, headers={"origin": ORIGIN}
    )
    assert resp.status == 403 and not mailbox.sent


async def test_login_link_for_mini_app_users(client: AsgiClient, ctx: FakeCtx) -> None:
    await make_user(ctx)
    resp = await client.post("/api/app/cabinet/login-link", headers={"authorization": f"tma {init_data()}"})
    assert resp.status == 200, resp.text
    url = resp.json()["url"]
    assert url.startswith("https://bot.example.com/cabinet/login/")
    web = AsgiClient(client.app)
    done = await web.post("/api/cabinet/auth/redeem", json={"token": url.rsplit("/", 1)[1]}, headers={"origin": ORIGIN})
    assert done.status == 200


# ---------------------------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Ann@Example.com ", "ann@example.com"),
        ("a.b+c@sub.example.org", "a.b+c@sub.example.org"),
        ("no-at.example.com", None),
        ("a@b", None),
        ("a b@example.com", None),
        ("a@example.com\nBcc: x@y.z", None),
        ("<a@example.com>", None),
        ("a@@example.com", None),
        ("x" * 250 + "@example.com", None),
    ],
)
def test_normalize_email(raw: str, expected: str | None) -> None:
    assert cabinet.normalize_email(raw) == expected


def test_mailer_refuses_header_injection() -> None:
    settings = make_settings(**SETTINGS)
    assert mailer.smtp_configured(settings)
    assert not mailer.smtp_configured(make_settings())
    message = mailer.build_message(settings, "a@example.com", "Hi\r\nBcc: x@y.z", "body")
    assert "\n" not in message["Subject"] and message["Bcc"] is None
    with pytest.raises(mailer.MailError):
        mailer.build_message(settings, "a@example.com\r\nBcc: x@y.z", "s", "b")


async def test_cleanup_removes_expired_tokens_and_sessions(ctx: FakeCtx) -> None:
    await make_user(ctx)
    old = datetime.now(UTC) - timedelta(days=30)
    await cabinet.issue_token(ctx.session_factory, cabinet.LOGIN_PREFIX, {"user_id": 1}, cabinet.LOGIN_TTL, now=old)
    fresh = await cabinet.issue_token(ctx.session_factory, cabinet.LOGIN_PREFIX, {"user_id": 1}, cabinet.LOGIN_TTL)
    async with ctx.session_factory.begin() as db:
        _, row = await cabinet.create_session(db, ctx.settings, USER_ID, "bot", ip=None, user_agent=None, now=old)
        row.expires_at = old + timedelta(days=1)
    assert await cabinet.purge_expired_tokens(ctx.session_factory) == 1
    assert await cabinet.purge_sessions(ctx.session_factory) == 1
    async with ctx.session_factory() as db:
        assert await kv.get(db, cabinet._key(cabinet.LOGIN_PREFIX, fresh)) is not None


# ---------------------------------------------------------------------------------------------
# /cabinet page and Stars
# ---------------------------------------------------------------------------------------------
def build_dir(tmp_path: Path) -> Path:
    app_dir = tmp_path / "app"
    (app_dir / "assets").mkdir(parents=True)
    (app_dir / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    (app_dir / "index.html").write_text(
        '<!doctype html><html><head><script src="https://telegram.org/js/telegram-web-app.js"></script>'
        '<script type="module" src="/app/assets/index-abc.js"></script></head>'
        '<body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    return tmp_path


async def test_cabinet_page_serves_the_mini_app_build(ctx: FakeCtx, tmp_path: Path) -> None:
    client = AsgiClient(web_app(ctx, static_dir=build_dir(tmp_path)))
    page = await client.get("/cabinet/login/" + "a" * 43)
    assert page.status == 200
    assert "/cabinet/assets/index-abc.js" in page.text and "/app/assets" not in page.text
    assert "telegram-web-app" not in page.text
    csp = page.headers["content-security-policy"]
    assert "frame-src https://oauth.telegram.org" in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-frame-options"] == "DENY"
    asset = await client.get("/cabinet/assets/index-abc.js")
    assert asset.status == 200 and asset.text == "console.log(1)"
    assert (await client.get("/cabinet/assets/missing.js")).status == 404
    assert (await client.get("/cabinet/")).status == 200
    # The Mini App itself is still served at /app with its own page.
    assert "telegram-web-app" in (await client.get("/app/")).text
    ctx.settings = make_settings()
    assert (await client.get("/cabinet/")).status == 404


async def test_payment_return_page(ctx: FakeCtx) -> None:
    client = AsgiClient(web_app(ctx))
    resp = await client.get("/pay/return")
    assert resp.status == 200
    assert "/cabinet/" in resp.text and "https://t.me/test_vpn_bot" in resp.text


def test_stars_are_not_offered_in_the_cabinet() -> None:
    from app.modules.miniapp_api import shop

    class Gateway:
        def __init__(self, key: str) -> None:
            self.key = key

    registry = SimpleNamespace(
        enabled=lambda: [Gateway("stars"), Gateway("platega")],
        get=lambda key: {"stars": Gateway("stars"), "platega": Gateway("platega")}.get(key),
    )
    ctx = SimpleNamespace(gateways=registry)
    in_telegram = SimpleNamespace(state=SimpleNamespace())
    in_browser = SimpleNamespace(state=SimpleNamespace(cabinet_session_id=5))
    assert [g.key for g in shop._gateways(ctx, in_telegram)] == ["stars", "platega"]  # type: ignore[arg-type]
    assert [g.key for g in shop._gateways(ctx, in_browser)] == ["platega"]  # type: ignore[arg-type]
    assert shop._gateway(ctx, None, "stars", in_telegram).key == "stars"  # type: ignore[arg-type]
