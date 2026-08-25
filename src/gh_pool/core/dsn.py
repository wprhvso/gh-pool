import logging

log = logging.getLogger(__name__)

DRIVER = "postgresql+psycopg"
SCHEMES = (
    f"{DRIVER}://",
    "postgresql+asyncpg://",
    "postgresql://",
    "postgres://",
)


def libpq(raw: str) -> str:
    for scheme in SCHEMES:
        if not raw.startswith(scheme):
            continue
        if scheme == "postgresql+asyncpg://":
            log.warning(
                "the database url names asyncpg, but the driver is psycopg; "
                "fix the environment variable"
            )
        return f"postgresql://{raw[len(scheme) :]}"
    return raw
