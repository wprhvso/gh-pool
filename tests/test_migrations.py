from dataclasses import dataclass

import pytest
import sqlalchemy as sa
from alembic import command

from gh_pool.core.config import settings
from gh_pool.db.base import Base
from gh_pool.db.engine import url
from gh_pool.db.migrate import config


@dataclass
class Schema:
    engine: sa.Engine

    def columns(self, table: str) -> set[str]:
        with self.engine.connect() as conn:
            return {c["name"] for c in sa.inspect(conn).get_columns(table)}

    def revision(self) -> str | None:
        with self.engine.connect() as conn:
            found = conn.execute(
                sa.text("select version_num from alembic_version")
            ).first()
            return found[0] if found else None


@pytest.fixture
def schema(database: str, monkeypatch):
    monkeypatch.setattr(settings, "database_url", database)
    engine = sa.create_engine(url().set(drivername="postgresql+psycopg"))
    try:
        yield Schema(engine)
    finally:
        engine.dispose()


def test_the_schema_carries_the_cancellation_flag(schema: Schema):
    command.upgrade(config(), "head")

    assert schema.revision() == "0002"
    assert "cancel_requested" in schema.columns("tasks")


def test_the_last_revision_goes_back_and_forward_again(schema: Schema):
    command.upgrade(config(), "head")

    command.downgrade(config(), "-1")
    assert schema.revision() == "0001"
    assert "cancel_requested" not in schema.columns("tasks")

    command.upgrade(config(), "head")
    assert "cancel_requested" in schema.columns("tasks")


def test_every_mapped_column_exists_in_the_migrated_schema(schema: Schema):
    command.upgrade(config(), "head")

    for table in Base.metadata.tables.values():
        mapped = {c.name for c in table.columns}
        assert mapped <= schema.columns(table.name), table.name
