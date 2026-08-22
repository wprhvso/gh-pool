import logging

log = logging.getLogger(__name__)

DRIVER = "postgresql+psycopg"
SCHEMES = (
    f"{DRIVER}://",
    "postgresql+asyncpg://",
    "postgresql://",
    "postgres://",
)


def _split(raw: str) -> tuple[str, str]:
    for scheme in SCHEMES:
        if raw.startswith(scheme):
            return scheme, raw[len(scheme) :]
    return "", raw


def sqlalchemy(raw: str) -> str:
    scheme, rest = _split(raw)
    if not scheme:
        return raw
    if scheme == "postgresql+asyncpg://":
        log.warning(
            "URL базы записан под asyncpg — читаю его как %s://; "
            "поправьте переменную окружения",
            DRIVER,
        )
    return f"{DRIVER}://{rest}"


def libpq(raw: str) -> str:
    scheme, rest = _split(raw)
    return f"postgresql://{rest}" if scheme else raw
