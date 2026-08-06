from __future__ import annotations

from uuid import UUID

from gh_chrome_protocol import CloseReason, CommandRequest, SessionStatus
from gh_chrome_protocol.commands import GotoArgs
from gh_chrome_server.db import Database
from gh_chrome_server.sessions import Sessions


async def activate(db: Database, session_id: UUID) -> None:
    async with db.tx() as tx:
        await tx.conn.execute(
            "update sessions set status = 'active', ready_at = now(), heartbeat_at = now() "
            "where id = %s",
            (session_id,),
        )


async def test_expired_command_is_detected(
    sessions: Sessions, db: Database, session_id: UUID
) -> None:
    await activate(db, session_id)
    request = CommandRequest(args=GotoArgs(url="https://example.com"), timeout=0.001)
    await sessions.enqueue(session_id, request)
    await sessions.take_next(session_id)
    expired = await sessions.expired_commands()
    assert [row["session_id"] for row in expired] == [session_id]


async def test_stale_heartbeat_makes_session_dead(
    sessions: Sessions, db: Database, session_id: UUID
) -> None:
    await activate(db, session_id)
    async with db.tx() as tx:
        await tx.conn.execute(
            "update sessions set heartbeat_at = now() - interval '1 hour' where id = %s",
            (session_id,),
        )
    assert await sessions.dead_candidates(30.0, 600.0) == [session_id]


async def test_pending_session_expires_by_ready_timeout(
    sessions: Sessions, db: Database, session_id: UUID
) -> None:
    async with db.tx() as tx:
        await tx.conn.execute(
            "update sessions set created_at = now() - interval '1 hour' where id = %s",
            (session_id,),
        )
    assert await sessions.dead_candidates(30.0, 600.0) == [session_id]


async def test_finish_fails_pending_commands_and_marks_profile_stale(
    sessions: Sessions, db: Database, session_id: UUID
) -> None:
    await activate(db, session_id)
    request = CommandRequest(args=GotoArgs(url="https://example.com"))
    command_id, _ = await sessions.enqueue(session_id, request)
    await sessions.finish(session_id, CloseReason.DEAD)
    async with db.conn() as conn:
        cur = await conn.execute("select status from commands where id = %s", (command_id,))
        row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert (await sessions.get(session_id)).status is SessionStatus.DEAD
