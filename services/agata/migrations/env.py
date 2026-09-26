from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def refuse_downgrade_by_default() -> None:
    """Protect the tenant schema from CLI downgrades, including the published baseline."""
    command = getattr(getattr(config, "cmd_opts", None), "cmd", None)
    command_name = getattr(command, "__name__", "")
    # Alembic versions expose the parsed command inconsistently: some leave cmd
    # unset in env.py. The CLI argv check is therefore the durable guard.
    if command_name == "downgrade" or "downgrade" in sys.argv[1:]:
        raise RuntimeError(
            "AGATA downgrades are disabled: no safe downgrade may remove or expose tenant data"
        )


def database_url() -> str:
    value = os.environ.get("AGATA_MIGRATOR_DATABASE_URL") or os.environ.get("AGATA_DATABASE_URL")
    if not value:
        raise RuntimeError("AGATA_DATABASE_URL or AGATA_MIGRATOR_DATABASE_URL is required")
    return value.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


def run_migrations_offline() -> None:
    refuse_downgrade_by_default()
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema="agata",
        compare_type=True,
    )
    context.execute("CREATE SCHEMA IF NOT EXISTS agata")
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    refuse_downgrade_by_default()
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS agata")
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema="agata",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
