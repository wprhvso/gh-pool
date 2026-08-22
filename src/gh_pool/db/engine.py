import logging
import os
from functools import cache

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

log = logging.getLogger(__name__)

DRIVER = "postgresql+psycopg"
DEFAULT_URL = f"{DRIVER}://pool:pool@localhost/pool"


def normalize(raw: str) -> str:
    """Привести URL к psycopg, каким бы драйвером он ни был записан.

    До слияния половины ходили в базу разными драйверами: сервер задач через
    asyncpg, браузерная часть — голым psycopg без диалекта в схеме. Обе формы
    лежат в проде в переменных окружения, и молча упасть на импорте
    несуществующего драйвера — худший способ это обнаружить.
    """
    if raw.startswith(f"{DRIVER}://"):
        return raw
    for legacy in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if raw.startswith(legacy):
            if legacy != "postgresql://":
                log.warning(
                    "DATABASE_URL записан как %s — читаю его как %s://; "
                    "поправьте переменную окружения",
                    legacy.rstrip(":/"),
                    DRIVER,
                )
            return f"{DRIVER}://" + raw[len(legacy) :]
    return raw


def url() -> str:
    return normalize(os.getenv("GH_POOL_DATABASE_URL", DEFAULT_URL))


@cache
def engine() -> AsyncEngine:
    return create_async_engine(url(), pool_size=5, max_overflow=5, pool_recycle=300)


@cache
def session() -> async_sessionmaker:
    return async_sessionmaker(engine(), expire_on_commit=False)


async def dispose() -> None:
    """Закрыть пул и забыть его. Нужно тестам и корректному выключению."""
    if engine.cache_info().currsize:
        await engine().dispose()
    session.cache_clear()
    engine.cache_clear()
