import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

from gh_pool.db import engine

SCRIPTS = Path(__file__).parent / "migrations"


def config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPTS))
    cfg.set_main_option("sqlalchemy.url", engine.url())
    return cfg


def upgrade_now(revision: str = "head") -> None:
    command.upgrade(config(), revision)


async def upgrade(revision: str = "head") -> None:
    await asyncio.to_thread(upgrade_now, revision)
