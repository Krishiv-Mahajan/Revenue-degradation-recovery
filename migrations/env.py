import os
import sys
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# Add src to path so models can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.infrastructure.db_safety import (
    INTERMEDIATE_REVISION_ASSERTION_SQL,
    assert_connected_database,
    final_state_requires_assertion,
    validate_alembic_target,
)
from src.infrastructure.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

OFFLINE = context.is_offline_mode()
# There is no default URL: DATABASE_URL must be explicit, valid, and never port 5433.
# Online runs also require ALEMBIC_TARGET_DATABASE (and an opt-in for non-test targets).
TARGET_URL = validate_alembic_target(os.environ, offline=OFFLINE)

# Heads after each applied step, as reported by Alembic; the last entry is the run's end state.
_applied_heads = []


def _record_heads(ctx, step, heads, run_args) -> None:
    _applied_heads.append(frozenset(heads))


def _final_heads(online_context) -> frozenset:
    if _applied_heads:
        return _applied_heads[-1]
    if online_context is not None:
        return frozenset(online_context.get_current_heads())
    return frozenset()


def _enforce_final_state(online_context=None) -> None:
    """Runs inside the migration transaction, before COMMIT."""
    if final_state_requires_assertion(_final_heads(online_context), TARGET_URL, offline=OFFLINE):
        context.execute(INTERMEDIATE_REVISION_ASSERTION_SQL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Renders SQL only; no engine is created and no connection is made.
    """
    context.configure(
        url=TARGET_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        on_version_apply=_record_heads,
    )

    with context.begin_transaction():
        context.run_migrations()
        _enforce_final_state()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        on_version_apply=_record_heads,
    )

    with context.begin_transaction():
        # Verified inside Alembic's own transaction: a query issued before configure() would leave
        # the connection in an autobegun transaction that Alembic treats as external and never commits.
        actual_database = connection.execute(text("SELECT current_database()")).scalar_one()
        assert_connected_database(actual_database, TARGET_URL)
        context.run_migrations()
        _enforce_final_state(context.get_context())


async def run_async_migrations() -> None:
    connectable = create_async_engine(TARGET_URL, poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
