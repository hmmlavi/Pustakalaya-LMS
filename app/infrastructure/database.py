"""SQLAlchemy engine, session factory, and SQLite PRAGMA configuration."""

from __future__ import annotations

from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import BASE_DIR, resolve_database_url


def create_db_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create a configured SQLAlchemy Engine with strict SQLite integrity & concurrency settings."""
    is_sqlite = database_url.startswith("sqlite")
    is_memory = ":memory:" in database_url or "mode=memory" in database_url

    engine_kwargs: dict[str, Any] = {
        "echo": echo,
        "future": True,
    }
    if is_sqlite:
        connect_args: dict[str, Any] = {
            "check_same_thread": False,
            "timeout": 15.0,
        }
        engine_kwargs["connect_args"] = connect_args
        if is_memory:
            engine_kwargs["poolclass"] = StaticPool

    engine = create_engine(database_url, **engine_kwargs)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
            # Let SQLAlchemy manage BEGIN / BEGIN IMMEDIATE / COMMIT / ROLLBACK explicitly
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 15000")
            if not is_memory:
                cursor.execute("PRAGMA journal_mode = WAL")
                cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _do_begin(conn) -> None:
            immediate = conn.get_execution_options().get("sqlite_immediate", False)
            if immediate:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.exec_driver_sql("BEGIN")

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a configured sessionmaker with expire_on_commit=False for service layer safety."""
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


def apply_migrations(database_url: str | None = None, revision: str = "head") -> str:
    """Apply Alembic versioned migrations against the target database URL.

    Returns the resolved database URL after upgrading.
    """
    resolved_url = resolve_database_url(database_url)
    alembic_cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    alembic_cfg.attributes["database_url"] = resolved_url
    alembic_cfg.attributes["configure_logger"] = False
    command.upgrade(alembic_cfg, revision)
    return resolved_url
