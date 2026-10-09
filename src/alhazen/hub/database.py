"""Engine construction and transactions, the same way on both backends.

Hides: dialect quirks. PostgreSQL gets a small connection pool and row locks
(``SELECT ... FOR UPDATE``) that serialize writers to one row across any
number of processes. SQLite (local development, one process) takes the
whole-database write lock at the start of every transaction
(``BEGIN IMMEDIATE``), so a read-then-write sequence cannot deadlock or lose
an update; ``FOR UPDATE`` is not rendered there and is not needed.

Every service operation runs inside ``Database.transaction()``: it commits
on success and rolls back on any exception, so a refusal raised half-way
leaves no partial rows.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from alhazen.hub.errors import HubError
from alhazen.hub.schema import SchemaError, check_version
from alhazen.hub.settings import HubSettings

log = logging.getLogger(__name__)


def engine_url(settings: HubSettings) -> str:
    url = settings.database_url
    if url.startswith("postgresql://"):
        # psycopg 3 is the supported driver; name it so SQLAlchemy does not
        # look for psycopg2.
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def make_engine(settings: HubSettings) -> Engine:
    url = engine_url(settings)
    if settings.is_sqlite:
        engine = create_engine(
            url, connect_args={"check_same_thread": False, "timeout": 30}, future=True
        )

        @event.listens_for(engine, "connect")
        def _sqlite_connect(dbapi_connection: Any, _record: Any) -> None:
            # Let SQLAlchemy's begin event (below) issue BEGIN itself rather
            # than pysqlite's deferred implicit transactions.
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn: Connection) -> None:
            # A read snapshot (Database.snapshot) begins deferred; everything
            # else takes the write lock up front.
            deferred = conn.get_execution_options().get("hub_snapshot", False)
            conn.exec_driver_sql("BEGIN DEFERRED" if deferred else "BEGIN IMMEDIATE")

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, future=True)


class Database:
    """The service's handle on its relational store."""

    def __init__(self, settings: HubSettings, engine: Engine | None = None) -> None:
        self.settings = settings
        self.engine = engine if engine is not None else make_engine(settings)

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        try:
            with self.engine.begin() as conn:
                yield conn
        except DBAPIError as exc:
            if exc.connection_invalidated or _is_unavailable(exc):
                raise HubError(
                    503,
                    "database_unavailable",
                    "The hub database is unavailable; retry shortly",
                    headers={"Retry-After": "5"},
                ) from exc
            raise

    @contextmanager
    def snapshot(self) -> Iterator[Connection]:
        """A read-only transaction that sees ONE committed state for all its
        statements (REPEATABLE READ on PostgreSQL; a deferred transaction on
        SQLite), for reads that must not straddle a concurrent rewrite, such
        as a streamed export racing a rebuild of the same rows."""
        options: dict[str, Any] = (
            {"hub_snapshot": True}
            if self.settings.is_sqlite
            else {"isolation_level": "REPEATABLE READ"}
        )
        try:
            with self.engine.connect() as raw:
                conn = raw.execution_options(**options)
                with conn.begin():
                    yield conn
        except DBAPIError as exc:
            if exc.connection_invalidated or _is_unavailable(exc):
                raise HubError(
                    503,
                    "database_unavailable",
                    "The hub database is unavailable; retry shortly",
                    headers={"Retry-After": "5"},
                ) from exc
            raise

    def check_schema(self) -> None:
        with self.engine.connect() as conn:
            check_version(conn)

    def ping(self) -> bool:
        """Whether the database answers at the expected schema (for /readyz)."""
        try:
            with self.engine.connect() as conn:
                check_version(conn)
        except (SQLAlchemyError, SchemaError) as exc:
            log.warning("hub database not ready: %s", type(exc).__name__)
            return False
        return True

    def dispose(self) -> None:
        self.engine.dispose()


def _is_unavailable(exc: DBAPIError) -> bool:
    name = type(exc.orig).__name__ if exc.orig is not None else ""
    return name in ("OperationalError",) and "locked" not in str(exc.orig).lower()
