import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, LiteralString

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from gh_pool.core import dsn

PROBE_TIMEOUT = 5.0

Params = tuple[Any, ...]


@dataclass(slots=True)
class Tx:
    conn: AsyncConnection[DictRow]
    hooks: list[Callable[[], None]] = field(default_factory=list)

    def after_commit(self, hook: Callable[[], None]) -> None:
        self.hooks.append(hook)

    async def run(self, sql: LiteralString, params: Params = ()) -> None:
        await self.conn.execute(sql, params)

    async def one(self, sql: LiteralString, params: Params = ()) -> DictRow | None:
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def rows(self, sql: LiteralString, params: Params = ()) -> list[DictRow]:
        cur = await self.conn.execute(sql, params)
        return list(await cur.fetchall())


class Database:
    def __init__(self, url: str) -> None:
        self._pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
            dsn.libpq(url),
            open=False,
            connection_class=AsyncConnection[DictRow],
            kwargs={"row_factory": dict_row},
        )

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    async def probe(self) -> None:
        async with asyncio.timeout(PROBE_TIMEOUT), self._pool.connection() as conn:
            await conn.execute("select 1")

    @asynccontextmanager
    async def tx(self) -> AsyncGenerator[Tx]:
        async with self._pool.connection() as conn:
            tx = Tx(conn)
            async with conn.transaction():
                yield tx
        for hook in tx.hooks:
            hook()

    async def one(self, sql: LiteralString, params: Params = ()) -> DictRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    async def rows(self, sql: LiteralString, params: Params = ()) -> list[DictRow]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return list(await cur.fetchall())
