"""Alembic migration environment for Pustakalaya."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context

from app.config import resolve_database_url
from app.infrastructure.database import create_db_engine
from app.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


_DEFAULT_INI_URL = "sqlite:///instance/pustakalaya.db"


def _get_url() -> str:
    override_url = config.attributes.get("database_url")
    if override_url:
        return resolve_database_url(str(override_url))
    cfg_url = config.get_main_option("sqlalchemy.url")
    if cfg_url and cfg_url != _DEFAULT_INI_URL:
        return resolve_database_url(cfg_url)
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return resolve_database_url(env_url)
    return resolve_database_url(cfg_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        context.configure(
            connection=provided_connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_db_engine(_get_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
            connection.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
