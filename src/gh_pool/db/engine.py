from functools import cache

from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gh_pool.core import dsn
from gh_pool.core.config import settings


def url() -> URL:
    parts = conninfo_to_dict(dsn.libpq(settings.database_url))
    host = str(parts.get("host") or "") or None
    socket = host if host and host.startswith("/") else None
    port = parts.get("port")
    return URL.create(
        dsn.DRIVER,
        username=str(parts["user"]) if parts.get("user") else None,
        password=str(parts["password"]) if parts.get("password") else None,
        host=None if socket else host,
        port=int(port) if port else None,
        database=str(parts["dbname"]) if parts.get("dbname") else None,
        query={"host": socket} if socket else {},
    )


@cache
def engine() -> AsyncEngine:
    return create_async_engine(url(), pool_size=5, max_overflow=5, pool_recycle=300)


@cache
def session() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), expire_on_commit=False)


async def dispose() -> None:
    if engine.cache_info().currsize:
        await engine().dispose()
    session.cache_clear()
    engine.cache_clear()
