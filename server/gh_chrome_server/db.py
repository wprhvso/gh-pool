from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

MIGRATIONS = Path(__file__).parent / "migrations"


@dataclass(slots=True)
class Tx:
    conn: AsyncConnection[DictRow]
    hooks: list[Callable[[], None]] = field(default_factory=list)

    def after_commit(self, hook: Callable[[], None]) -> None:
        self.hooks.append(hook)


class Database:
    def __init__(self, url: str) -> None:
        self._pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
            url,
            open=False,
            connection_class=AsyncConnection[DictRow],
            kwargs={"row_factory": dict_row},
        )

    async def open(self) -> None:
        await self._pool.open(wait=True)
        await self.migrate()

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def conn(self) -> AsyncGenerator[AsyncConnection[DictRow]]:
        async with self._pool.connection() as conn:
            yield conn

    @asynccontextmanager
    async def tx(self) -> AsyncGenerator[Tx]:
        async with self._pool.connection() as conn:
            tx = Tx(conn)
            async with conn.transaction():
                yield tx
        for hook in tx.hooks:
            hook()

    async def migrate(self) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "create table if not exists schema_migrations ("
                "name text primary key, applied_at timestamptz not null default now())"
            )
            cur = await conn.execute("select name from schema_migrations")
            applied = {row["name"] for row in await cur.fetchall()}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name in applied:
                    continue
                await conn.execute(path.read_bytes())
                await conn.execute("insert into schema_migrations (name) values (%s)", (path.name,))
