import logging
import os

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

log = logging.getLogger(__name__)

DRIVER = "postgresql+psycopg"
DEFAULT_URL = f"{DRIVER}://pool:pool@localhost/pool"

_engine: AsyncEngine | None = None
_factory: async_sessionmaker | None = None


def normalize(raw: str) -> str:
    """Привести URL к psycopg, каким бы драйвером он ни был записан.

    До слияния половины ходили в базу разными драйверами: pool через
    asyncpg, браузерная часть — голым psycopg без диалекта в схеме. Обе
    формы лежат в проде в переменных окружения, и молча упасть на импорте
    несуществующего драйвера — худший способ это обнаружить.
    """
    if raw.startswith(f"{DRIVER}://"):
        return raw
    for legacy in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if raw.startswith(legacy):
            fixed = f"{DRIVER}://" + raw[len(legacy) :]
            if legacy != "postgresql://":
                log.warning(
                    "DATABASE_URL записан как %s — читаю его как %s://; "
                    "поправьте переменную окружения",
                    legacy.rstrip(":/"),
                    DRIVER,
                )
            return fixed
    return raw


def url() -> str:
    return normalize(os.getenv("DATABASE_URL", DEFAULT_URL))


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            url(), pool_size=5, max_overflow=5, pool_recycle=300
        )
    return _engine


def session() -> async_sessionmaker:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(engine(), expire_on_commit=False)
    return _factory


async def dispose() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None
