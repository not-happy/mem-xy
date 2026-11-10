"""Alembic environment.

There is no ORM metadata on purpose: the schema is hand-written SQL (plan §4.1) and
migrations execute it verbatim, so `target_metadata` stays None and autogenerate is
not used.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import pool
from sqlalchemy.engine import URL, Connection
from sqlalchemy import create_engine

from app.core.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _sqlalchemy_url() -> URL:
    parts = conninfo_to_dict(get_settings().dsn)
    return URL.create(
        drivername="postgresql+psycopg",
        username=parts.get("user"),
        password=parts.get("password"),
        host=parts.get("host"),
        port=int(parts["port"]) if parts.get("port") else None,
        database=parts.get("dbname"),
        query={k: v for k, v in parts.items()
               if k not in ("user", "password", "host", "port", "dbname")},
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_sqlalchemy_url(),
        target_metadata=target_metadata,
        literal_bindings=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_sqlalchemy_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=False)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
