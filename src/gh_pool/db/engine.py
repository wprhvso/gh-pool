from functools import cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gh_pool.core import dsn
from gh_pool.core.config import settings


def url() -> str:
    return dsn.sqlalchemy(settings.database_url)


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
