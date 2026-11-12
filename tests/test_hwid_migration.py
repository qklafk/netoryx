"""Migration 0014 (devices seen on subscription links): upgrade, uniqueness, cascade and downgrade."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from app.db.session import create_engine
from app.runtime import alembic_config, upgrade_database

STAMP = "2026-01-01 00:00:00"


async def test_upgrade_constraints_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        assert await upgrade_database(engine, "0013") == "0013"
        async with engine.connect() as conn:
            assert "subscription_devices" not in await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert await upgrade_database(engine, "0014") == "0014"
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.execute(
                text("INSERT INTO users (tg_id, referral_code, sub_token, created_at) VALUES (1, 'R1', 't1', :t)"),
                {"t": STAMP},
            )
            await conn.execute(
                text(
                    "INSERT INTO subscriptions (id, user_id, client_uuid, status, expires_at, created_at, updated_at,"
                    " revision, device_limit, traffic_limit_gb) VALUES (1, 1, 'u-1', 'ACTIVE', :t, :t, :t, 1, 0, 0)"
                ),
                {"t": STAMP},
            )
            insert = text(
                "INSERT INTO subscription_devices (subscription_id, hwid_hash, first_seen_at, last_seen_at)"
                " VALUES (1, 'h1', :t, :t)"
            )
            await conn.execute(insert, {"t": STAMP})
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(insert, {"t": STAMP})
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.execute(text("DELETE FROM subscriptions WHERE id = 1"))
            assert (await conn.execute(text("SELECT COUNT(*) FROM subscription_devices"))).scalar_one() == 0
        async with engine.connect() as conn:

            def downgrade(connection: Connection) -> None:
                cfg = alembic_config()
                cfg.attributes["connection"] = connection
                command.downgrade(cfg, "0013")

            await conn.run_sync(downgrade)
            await conn.commit()
            assert "subscription_devices" not in await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    finally:
        await engine.dispose()
