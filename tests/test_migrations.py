"""Migrations produce exactly the schema declared by the models.

SQLite runs always. PostgreSQL and MySQL run with ``CORE_DB_INTEGRATION=1`` (Docker, testcontainers); optional
``CORE_IT_DATABASES`` / ``CORE_IT_POSTGRES_IMAGE`` / ``CORE_IT_MYSQL_IMAGE`` as in the other database tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import Base
from app.db.session import create_engine
from app.runtime import alembic_config, head_revision, migration_status, upgrade_database


def _diff(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return list(compare_metadata(context, Base.metadata))


def _server_defaults(connection: Connection) -> dict[tuple[str, str], bool]:
    insp = inspect(connection)
    return {
        (table, column["name"]): column.get("default") is not None
        for table in insp.get_table_names()
        if table != "alembic_version"
        for column in insp.get_columns(table)
    }


def _expected_server_defaults() -> dict[tuple[str, str], bool]:
    return {
        (table.name, column.name): column.server_default is not None
        for table in Base.metadata.sorted_tables
        for column in table.columns
    }


def _constraint_names(connection: Connection) -> set[str]:
    insp = inspect(connection)
    names: set[str] = set()
    for table in insp.get_table_names():
        names.update(str(ix["name"]) for ix in insp.get_indexes(table))
        names.update(str(uq["name"]) for uq in insp.get_unique_constraints(table) if uq.get("name"))
        names.update(str(fk["name"]) for fk in insp.get_foreign_keys(table) if fk.get("name"))
    return names


async def _assert_matches_models(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        diff = await conn.run_sync(_diff)
        assert diff == [], diff
        assert await conn.run_sync(_server_defaults) == _expected_server_defaults()
        names = await conn.run_sync(_constraint_names)
    # Every named index/constraint follows the naming convention (stable names for later migrations).
    assert names, "no indexes reflected"
    assert all(n.startswith(("ix_", "uq_", "fk_")) for n in names), sorted(names)


def test_single_head() -> None:
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert head_revision() == script.get_current_head()


async def test_upgrade_on_empty_sqlite_matches_models(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        assert await upgrade_database(engine) == head_revision()
        await _assert_matches_models(engine)
        current, head = await migration_status(engine)
        assert current == head
        # Running again is a no-op.
        assert await upgrade_database(engine) == head
    finally:
        await engine.dispose()


async def test_downgrade_to_base_and_up_again(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        await upgrade_database(engine)
        async with engine.connect() as conn:

            def downgrade(connection: Connection) -> None:
                from alembic import command

                cfg = alembic_config()
                cfg.attributes["connection"] = connection
                command.downgrade(cfg, "base")

            await conn.run_sync(downgrade)
            await conn.commit()
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert tables <= {"alembic_version"}
        await upgrade_database(engine)
        await _assert_matches_models(engine)
    finally:
        await engine.dispose()


async def test_server_defaults_work_without_orm(tmp_path: Path) -> None:
    """Rows inserted with plain SQL get the declared defaults (booleans render portably)."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        await upgrade_database(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (tg_id, referral_code, sub_token, created_at) "
                    "VALUES (1, 'R1', 't1', '2026-01-01 00:00:00')"
                )
            )
            row = (await conn.execute(text("SELECT language, balance_kop, trial_used, is_banned FROM users"))).one()
        assert tuple(row) == ("ru", 0, 0, 0)
    finally:
        await engine.dispose()


async def test_upgrade_of_populated_database_preserves_subscription_history_and_provisions(tmp_path: Path) -> None:
    """The independent-subscriptions migration must not lose rows while SQLite rebuilds its tables."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    stamp = "2026-01-01 00:00:00"
    try:
        assert await upgrade_database(engine, "0001") == "0001"
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (tg_id, referral_code, sub_token, created_at) "
                    "VALUES (1, 'R1', 'legacy-token', :stamp)"
                ),
                {"stamp": stamp},
            )
            await conn.execute(
                text(
                    "INSERT INTO plans (id, title, duration_days, price_kop, device_limit, traffic_limit_gb, "
                    "sort_order, created_at) VALUES (1, 'Month', 30, 10000, 3, 0, 0, :stamp)"
                ),
                {"stamp": stamp},
            )
            await conn.execute(
                text(
                    "INSERT INTO panels (id, name, kind, url, consecutive_failures, created_at) "
                    "VALUES (1, 'Panel', 'xui', 'https://panel.invalid', 0, :stamp)"
                ),
                {"stamp": stamp},
            )
            await conn.execute(
                text(
                    "INSERT INTO subscriptions (id, user_id, plan_id, client_uuid, status, expires_at, "
                    "device_limit, traffic_limit_gb, revision, created_at, updated_at) "
                    "VALUES (1, 1, 1, '11111111-1111-4111-8111-111111111111', 'active', "
                    "'2026-02-01 00:00:00', 3, 0, 1, :stamp, :stamp)"
                ),
                {"stamp": stamp},
            )
            await conn.execute(
                text(
                    "INSERT INTO provisions (id, subscription_id, panel_id, inbound_key, desired_state, actual_state, "
                    "synced_revision, attempts, updated_at) VALUES (1, 1, 1, 0, 'enabled', 'enabled', 1, 0, :stamp)"
                ),
                {"stamp": stamp},
            )
            await conn.execute(
                text(
                    "INSERT INTO payments (id, provider, user_id, amount_kop, plan_id, status, created_at, updated_at) "
                    "VALUES ('payment-1', 'test', 1, 10000, 1, 'succeeded', :stamp, :stamp)"
                ),
                {"stamp": stamp},
            )
            await conn.execute(
                text(
                    "INSERT INTO purchases (id, user_id, plan_id, kind, price_kop, days, expires_after, payment_id, "
                    "created_at) VALUES (1, 1, 1, 'purchase', 10000, 30, '2026-02-01 00:00:00', 'payment-1', :stamp)"
                ),
                {"stamp": stamp},
            )

        assert await upgrade_database(engine) == head_revision()
        async with engine.connect() as conn:
            subscription = (
                await conn.execute(text("SELECT label, sub_token, is_legacy FROM subscriptions WHERE id = 1"))
            ).one()
            assert subscription.sub_token == "legacy-token" and bool(subscription.is_legacy)
            assert subscription.label
            assert (await conn.scalar(text("SELECT subscription_id FROM payments WHERE id = 'payment-1'"))) == 1
            assert (await conn.scalar(text("SELECT subscription_id FROM purchases WHERE id = 1"))) == 1
            assert (await conn.scalar(text("SELECT subscription_id FROM provisions WHERE id = 1"))) == 1
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'public_api_keys'")
                )
                == 1
            )
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'subscription_addon_purchases'"
                    )
                )
                == 1
            )
            assert await conn.scalar(text("SELECT receipt_file_id FROM payments WHERE id = 'payment-1'")) is None
            assert await conn.scalar(text("SELECT availability_rule FROM plans WHERE id = 1")) == "all"

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO subscriptions (id, user_id, label, sub_token, is_legacy, client_uuid, status, "
                    "expires_at, "
                    "device_limit, traffic_limit_gb, revision, created_at, updated_at) VALUES "
                    "(2, 1, 'Second', 'new-token', 0, '22222222-2222-4222-8222-222222222222', 'active', "
                    "'2026-02-01 00:00:00', 3, 0, 1, :stamp, :stamp)"
                ),
                {"stamp": stamp},
            )
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM subscriptions WHERE user_id = 1")) == 2
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO purchases (user_id, subscription_id, kind, price_kop, days, expires_after, "
                        "created_at) "
                        "VALUES (1, 999, 'purchase', 0, 1, '2026-02-01 00:00:00', :stamp)"
                    ),
                    {"stamp": stamp},
                )
    finally:
        await engine.dispose()


async def test_refuses_unversioned_database_with_tables(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        with pytest.raises(RuntimeError, match="no migration history"):
            await upgrade_database(engine)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------
# PostgreSQL / MySQL (Docker)
# ---------------------------------------------------------------------------------------------
_DATABASES = [d.strip() for d in os.environ.get("CORE_IT_DATABASES", "postgres,mysql").split(",") if d.strip()]
integration = pytest.mark.skipif(
    os.environ.get("CORE_DB_INTEGRATION") != "1", reason="set CORE_DB_INTEGRATION=1 to run (needs Docker)"
)


@pytest.fixture(scope="module", params=_DATABASES)
def server_url(request: pytest.FixtureRequest) -> Iterator[str]:
    if os.environ.get("CORE_DB_INTEGRATION") != "1":
        pytest.skip("set CORE_DB_INTEGRATION=1 to run (needs Docker)")
    container: Any
    if request.param == "postgres":
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer(os.environ.get("CORE_IT_POSTGRES_IMAGE", "postgres:18.4"), driver="asyncpg")
    elif request.param == "mysql":
        from testcontainers.community.mysql import MySqlContainer

        container = MySqlContainer(os.environ.get("CORE_IT_MYSQL_IMAGE", "mysql:8.4"), dialect="aiomysql")
    else:  # pragma: no cover - configuration error
        raise ValueError(f"unknown database {request.param!r}")
    with container:
        yield container.get_connection_url()


@integration
async def test_upgrade_on_server_databases_matches_models(server_url: str) -> None:
    engine = create_engine(server_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        assert await upgrade_database(engine) == head_revision()
        async with engine.connect() as conn:
            diff = await conn.run_sync(_diff)
        assert diff == [], diff
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (tg_id, referral_code, sub_token, created_at) "
                    "VALUES (1, 'R1', 't1', '2026-01-01 00:00:00')"
                )
            )
            row = (await conn.execute(text("SELECT balance_kop, trial_used FROM users"))).one()
        assert row[0] == 0 and not row[1]
    finally:
        await engine.dispose()
