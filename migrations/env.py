"""Alembic environment: async engines, batch mode on SQLite, type comparison for autogenerate.

The database URL is taken (in this order) from ``-x url=...``, the ``sqlalchemy.url`` option, the
``DATABASE_URL`` environment variable, or the application settings. When the application runs migrations
itself it passes an open connection in ``config.attributes["connection"]``.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection

from app.db.models import Base, UTCDateTime
from app.db.session import create_engine

config = context.config
target_metadata = Base.metadata

if config.config_file_name and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def database_url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or config.get_main_option("sqlalchemy.url")
    if url:
        return url
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    from app.config import get_settings

    return get_settings().database_url


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Render the application's column types as plain SQLAlchemy types (migrations must not import models)."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        render_item=render_item,
        **kwargs,
    )


def do_run_migrations(connection: Connection) -> None:
    sqlite = connection.dialect.name == "sqlite"
    # SQLite recreates tables for constraint changes. Keep referenced rows intact while the migration owns
    # the connection, then validate every reference before committing and restoring enforcement.
    if sqlite:
        connection.commit()
        cursor = connection.connection.dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()
    try:
        _configure(connection=connection, render_as_batch=sqlite)
        with context.begin_transaction():
            context.run_migrations()
        if sqlite:
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                raise RuntimeError("Database migration produced invalid foreign keys")
            connection.commit()
    except BaseException:
        if sqlite:
            connection.rollback()
        raise
    finally:
        if sqlite:
            cursor = connection.connection.dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()


async def run_async_migrations() -> None:
    engine = create_engine(database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_offline() -> None:
    url = database_url()
    _configure(url=url, literal_binds=True, render_as_batch=url.startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    shared = config.attributes.get("connection")
    if shared is not None:
        do_run_migrations(shared)
    else:
        asyncio.run(run_async_migrations())
