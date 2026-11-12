from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

SessionFactory = async_sessionmaker[AsyncSession]

# Execution option marking a connection as read-only (see :func:`read_session`).
READ_ONLY_OPTION = "vpnbot_read_only"


def create_engine(database_url: str) -> AsyncEngine:
    url = make_url(database_url)
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    is_sqlite = url.get_backend_name() == "sqlite"
    if is_sqlite:
        kwargs["connect_args"] = {"timeout": 5}
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url, **kwargs)

    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
            # pysqlite only emits BEGIN before DML, so a SAVEPOINT issued first would itself open the
            # transaction and its RELEASE would COMMIT it. Disable the driver's implicit transaction handling
            # and emit BEGIN ourselves (see SQLAlchemy docs, "Serializable isolation / Savepoints").
            dbapi_conn.isolation_level = None

        @event.listens_for(engine.sync_engine, "begin")
        def _sqlite_begin(conn: Any) -> None:
            if conn.get_execution_options().get(READ_ONLY_OPTION):
                # Pure reads: a deferred transaction is a WAL reader that neither waits for nor blocks writers.
                conn.exec_driver_sql("BEGIN")
                return
            # IMMEDIATE takes the write lock up front: a deferred transaction that reads and then writes
            # fails with "database is locked" (SQLITE_BUSY_SNAPSHOT) under WAL instead of waiting.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


def read_session(session_factory: SessionFactory) -> AsyncSession:
    """A session for pure reads from ``session_factory`` (same engine and pool): ``async with read_session(f) as s``.

    On SQLite it starts a deferred transaction instead of ``BEGIN IMMEDIATE``, so hot read paths (subscription
    page, panel lookups, pre-checkout, dashboards) run concurrently with writers instead of queueing for the write
    lock. Never write through it. On PostgreSQL/MySQL it is an ordinary session.
    """
    engine = session_factory.kw.get("bind")
    if not isinstance(engine, AsyncEngine):
        return session_factory()
    # An option engine is a cheap view (~µs) sharing the pool and the "begin" listener.
    return session_factory(bind=engine.execution_options(**{READ_ONLY_OPTION: True}))
