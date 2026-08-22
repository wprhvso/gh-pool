from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import psycopg
import pytest

from gh_pool.protocol import Method
from gh_pool.server import storage
from gh_pool.server.cleaner import Cleaner
from gh_pool.server.config import settings
from gh_pool.server.db import Database
from gh_pool.server.events import Events
from gh_pool.server.sessions import Sessions
from tests.chrome.e2e.stack import Server, Stack

KIB = 1 << 10


@pytest.fixture
async def cleaner(server: Server, database: str) -> AsyncIterator[Cleaner]:
    db = Database(database)
    await db.open()
    try:
        yield Cleaner(Sessions(db, Events(db)))
    finally:
        await db.close()


def _backdate(database: str, session_id: UUID, days: float) -> None:
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(
            "update sessions set closed_at = now() - make_interval(secs => %s) "
            "where id = %s",
            (days * 86400.0, session_id),
        )


def _traces(database: str, session_id: UUID) -> tuple[int, int]:
    with psycopg.connect(database) as conn:
        events = conn.execute(
            "select count(*) from events where session_id = %s", (session_id,)
        ).fetchone()
        commands = conn.execute(
            "select count(*) from commands where session_id = %s", (session_id,)
        ).fetchone()
    return (
        0 if events is None else int(events[0]),
        0 if commands is None else int(commands[0]),
    )


def _record(session_id: UUID, kib: int) -> None:
    segments = storage.segments_dir(session_id)
    segments.mkdir(parents=True, exist_ok=True)
    (segments / "1.m4s").write_bytes(b"x" * (kib * KIB))


def _left(session_id: UUID) -> bool:
    return storage.session_dir(session_id).exists()


async def test_a_session_closed_days_ago_leaves_neither_row_nor_recording(
    stack: Stack,
    cleaner: Cleaner,
    api: httpx.AsyncClient,
    database: str,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "cleanup_max_days", 1.0)
    session, runner = await stack.scripted()
    runner.returns(Method.TITLE, "a page nobody will ask about again")
    await session.title()
    await session.close()
    _record(session.id, kib=16)
    _backdate(database, session.id, days=3)
    assert _traces(database, session.id) > (0, 0)

    await cleaner._tick()

    assert not _left(session.id)
    assert _traces(database, session.id) == (0, 0)
    assert (await api.get(f"/sessions/{session.id}")).status_code == 404


async def test_a_session_that_is_still_running_survives_the_pass(
    stack: Stack,
    cleaner: Cleaner,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "cleanup_max_days", 0.0)
    session, _ = await stack.scripted()
    _record(session.id, kib=16)

    await cleaner._tick()

    assert _left(session.id)
    assert (await api.get(f"/sessions/{session.id}")).status_code == 200


async def test_a_session_closed_a_moment_ago_survives_the_pass(
    stack: Stack, cleaner: Cleaner, api: httpx.AsyncClient
):
    session, _ = await stack.scripted()
    await session.close()
    _record(session.id, kib=16)

    await cleaner._tick()

    assert _left(session.id)
    assert (await api.get(f"/sessions/{session.id}")).status_code == 200


async def test_a_profile_outlives_every_session_that_ever_used_it(
    stack: Stack,
    cleaner: Cleaner,
    api: httpx.AsyncClient,
    database: str,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "cleanup_max_days", 1.0)
    archive = storage.profile_path("aistudio-one")
    archive.write_bytes(b"an account somebody signed in by hand")
    session, _ = await stack.scripted(profile="aistudio-one")
    await session.close()
    _record(session.id, kib=16)
    _backdate(database, session.id, days=3)

    await cleaner._tick()

    assert not _left(session.id)
    assert archive.read_bytes() == b"an account somebody signed in by hand"
    listed = (await api.get("/profiles")).json()
    assert [item["name"] for item in listed] == ["aistudio-one"]


async def test_storage_over_the_limit_gives_up_its_oldest_session_first(
    stack: Stack, cleaner: Cleaner, database: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "cleanup_max_bytes", 40 * KIB)
    oldest, _ = await stack.scripted()
    newest, _ = await stack.scripted()
    for session in (oldest, newest):
        await session.close()
        _record(session.id, kib=32)
    _backdate(database, oldest.id, days=2)
    _backdate(database, newest.id, days=1)

    await cleaner._tick()

    assert not _left(oldest.id)
    assert _left(newest.id)


async def test_a_session_whose_runner_may_still_be_talking_keeps_its_room(
    stack: Stack,
    cleaner: Cleaner,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "cleanup_max_bytes", KIB)
    session, _ = await stack.scripted()
    await session.close()
    _record(session.id, kib=32)

    await cleaner._tick()

    assert _left(session.id)
    assert (await api.get(f"/sessions/{session.id}")).status_code == 200
