"""Administrators: role resolution and owner-only management."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import AdminAuditLog, AdminRole, AdminSession
from app.db.session import SessionFactory
from app.services import admins
from app.services.admins import AdminError, AdminErrorCode
from app.utils.time import utcnow
from tests.conftest import make_settings

OWNER = 1000  # ADMIN_IDS


@pytest.fixture
def settings() -> Settings:
    return make_settings(admin_ids=[OWNER])


async def test_resolve_role(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await admins.add(s, settings, OWNER, 20, "admin")
    async with session_factory() as s:
        assert await admins.resolve_role(s, settings, OWNER) is AdminRole.OWNER
        assert await admins.resolve_role(s, settings, 20) is AdminRole.ADMIN
        assert await admins.resolve_role(s, settings, 21) is None


async def test_add_list_and_ids(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await admins.add(s, settings, OWNER, 30, AdminRole.VIEWER, note="  analyst  ")
        await admins.add(s, settings, OWNER, 20, AdminRole.SUPPORT, source="web", ip="10.0.0.1")
        await admins.add(s, settings, OWNER, 40, AdminRole.OWNER)
    async with session_factory() as s:
        entries = await admins.list_admins(s, settings)
        assert [(e.tg_id, e.role, e.from_env) for e in entries] == [
            (OWNER, AdminRole.OWNER, True),
            (40, AdminRole.OWNER, False),
            (20, AdminRole.SUPPORT, False),
            (30, AdminRole.VIEWER, False),
        ]
        assert entries[-1].note == "analyst" and entries[-1].added_by == OWNER
        assert await admins.ids_with_roles(s, settings, [AdminRole.OWNER]) == [OWNER, 40]
        assert await admins.ids_with_roles(s, settings, [AdminRole.SUPPORT, AdminRole.VIEWER]) == [20, 30]
        audit = (await s.scalars(select(AdminAuditLog).where(AdminAuditLog.target_id == "20"))).one()
        assert audit.action == "admin.add" and audit.source == "web" and audit.ip == "10.0.0.1"


@pytest.mark.parametrize(
    ("actor", "target", "role", "code"),
    [
        (20, 50, "viewer", AdminErrorCode.FORBIDDEN),  # admins cannot manage admins
        (777, 50, "viewer", AdminErrorCode.FORBIDDEN),
        (OWNER, OWNER, "viewer", AdminErrorCode.ENV_OWNER),
        (OWNER, 20, "viewer", AdminErrorCode.EXISTS),
        (OWNER, 50, "superuser", AdminErrorCode.INVALID),
        (OWNER, -100, "viewer", AdminErrorCode.INVALID),
    ],
)
async def test_add_refusals(
    session_factory: SessionFactory, settings: Settings, actor: int, target: int, role: str, code: AdminErrorCode
) -> None:
    async with session_factory.begin() as s:
        await admins.add(s, settings, OWNER, 20, AdminRole.ADMIN)
    async with session_factory.begin() as s:
        with pytest.raises(AdminError) as err:
            await admins.add(s, settings, actor, target, role)
        assert err.value.code is code


async def test_update_role_and_note(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await admins.add(s, settings, OWNER, 20, AdminRole.SUPPORT, note="old")
    async with session_factory.begin() as s:
        admin = await admins.update(s, settings, OWNER, 20, role="admin", note=None)
        assert admin.role is AdminRole.ADMIN and admin.note is None
        same = await admins.update(s, settings, OWNER, 20, role=AdminRole.ADMIN)
        assert same.role is AdminRole.ADMIN
    async with session_factory() as s:
        actions = (await s.scalars(select(AdminAuditLog.action).order_by(AdminAuditLog.id))).all()
        assert actions == ["admin.add", "admin.update"]  # the no-op update is not audited
    async with session_factory.begin() as s:
        with pytest.raises(AdminError) as err:
            await admins.update(s, settings, OWNER, 99, role="viewer")
        assert err.value.code is AdminErrorCode.NOT_FOUND
        with pytest.raises(AdminError) as err:
            await admins.update(s, settings, OWNER, OWNER, role="viewer")
        assert err.value.code is AdminErrorCode.ENV_OWNER


async def test_last_owner_is_protected(session_factory: SessionFactory) -> None:
    settings = make_settings(admin_ids=[])
    async with session_factory.begin() as s:
        from app.db.models import Admin

        s.add(Admin(tg_id=10, role=AdminRole.OWNER))
    async with session_factory.begin() as s:
        with pytest.raises(AdminError) as err:
            await admins.update(s, settings, 10, 10, role="admin")
        assert err.value.code is AdminErrorCode.LAST_OWNER
        with pytest.raises(AdminError) as err:
            await admins.remove(s, settings, 10, 10)
        assert err.value.code is AdminErrorCode.LAST_OWNER
        await admins.add(s, settings, 10, 11, AdminRole.OWNER)
        assert await admins.remove(s, settings, 11, 10)  # with a second owner it is fine


async def test_remove_revokes_web_sessions(session_factory: SessionFactory, settings: Settings) -> None:
    async with session_factory.begin() as s:
        await admins.add(s, settings, OWNER, 20, AdminRole.ADMIN)
        s.add(AdminSession(admin_id=20, token_hash="a" * 64, expires_at=utcnow() + timedelta(hours=1)))
        s.add(AdminSession(admin_id=OWNER, token_hash="b" * 64, expires_at=utcnow() + timedelta(hours=1)))
    async with session_factory.begin() as s:
        assert await admins.remove(s, settings, OWNER, 20)
        assert not await admins.remove(s, settings, OWNER, 20)
        with pytest.raises(AdminError):
            await admins.remove(s, settings, OWNER, OWNER)
    async with session_factory() as s:
        revoked = dict((await s.execute(select(AdminSession.admin_id, AdminSession.revoked))).all())
        assert revoked == {20: True, OWNER: False}
        assert await admins.get(s, 20) is None


def test_role_helpers() -> None:
    assert admins.outranks(AdminRole.OWNER, AdminRole.ADMIN)
    assert not admins.outranks(AdminRole.SUPPORT, AdminRole.SUPPORT)
    assert admins.at_least(AdminRole.ADMIN, AdminRole.SUPPORT)
    assert not admins.at_least(None, AdminRole.VIEWER)
    assert admins.parse_role(" Owner ") is AdminRole.OWNER
