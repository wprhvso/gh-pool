from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from gh_chrome_protocol import SessionParams
from gh_chrome_server.db import Database
from gh_chrome_server.events import Events
from gh_chrome_server.sessions import Sessions
from psycopg.types.json import Jsonb


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set")
    return url


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[Database]:
    database = Database(database_url)
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def events(db: Database) -> Events:
    return Events(db)


@pytest.fixture
def sessions(db: Database, events: Events) -> Sessions:
    return Sessions(db, events)


@pytest.fixture
async def session_id(db: Database) -> UUID:
    new_id = uuid4()
    async with db.tx() as tx:
        await tx.conn.execute(
            "insert into sessions (id, params) values (%s, %s)",
            (new_id, Jsonb(SessionParams().model_dump(mode="json"))),
        )
    return new_id
