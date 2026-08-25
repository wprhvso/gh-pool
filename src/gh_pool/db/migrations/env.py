import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from gh_pool.db import base, sessions, tasks
from gh_pool.db import engine as engine_mod

MAPPED_ONTO_METADATA = (sessions, tasks)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = base.Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=engine_mod.url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(engine_mod.url(), poolclass=NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_run)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
