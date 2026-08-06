from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from gh_chrome_protocol import (
    CloseReason,
    CommandError,
    CommandRequest,
    ErrorCode,
    EventData,
    SessionCreate,
    SessionParams,
    SessionState,
    SessionStatus,
)
from gh_chrome_protocol.events import (
    CommandFailed,
    CommandFinished,
    CommandStarted,
    SessionClosed,
    SessionReady,
)
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from gh_chrome_server.db import Database, Tx
from gh_chrome_server.events import Events

TERMINAL = ("closed", "dead")


class SessionNotFound(Exception):
    pass


class SessionUnavailable(Exception):
    pass


class TooManySessions(Exception):
    pass


def _state(row: DictRow) -> SessionState:
    return SessionState(
        id=row["id"],
        status=SessionStatus(row["status"]),
        state_stale=row["state_stale"],
        profile=row["profile"],
        persist=row["persist"],
        params=SessionParams.model_validate(row["params"]),
        last_seq=row["last_seq"],
    )


class Sessions:
    def __init__(self, db: Database, events: Events) -> None:
        self._db = db
        self._events = events
        self._cancels: dict[UUID, list[UUID]] = {}
        self.closing: set[UUID] = set()

    async def get(self, session_id: UUID) -> SessionState:
        async with self._db.conn() as conn:
            cur = await conn.execute("select * from sessions where id = %s", (session_id,))
            row = await cur.fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return _state(row)

    async def started_at(self, session_id: UUID) -> datetime:
        async with self._db.conn() as conn:
            cur = await conn.execute(
                "select coalesce(ready_at, created_at) as at from sessions where id = %s",
                (session_id,),
            )
            row = await cur.fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        at: datetime = row["at"]
        return at

    async def create(self, request: SessionCreate) -> SessionState:
        session_id = uuid4()
        async with self._db.tx() as tx:
            if request.max_parallel is not None:
                cur = await tx.conn.execute(
                    "select count(*) as live from sessions where status in ('pending', 'active')"
                )
                row = await cur.fetchone()
                if row is not None and int(row["live"]) >= request.max_parallel:
                    raise TooManySessions(request.max_parallel)
            stale = False
            if request.profile is not None:
                cur = await tx.conn.execute(
                    "select stale from profiles where name = %s", (request.profile,)
                )
                row = await cur.fetchone()
                if row is None:
                    await tx.conn.execute(
                        "insert into profiles (name) values (%s) on conflict do nothing",
                        (request.profile,),
                    )
                else:
                    stale = bool(row["stale"])
            cur = await tx.conn.execute(
                "insert into sessions (id, params, profile, persist, state_stale) "
                "values (%s, %s, %s, %s, %s) returning *",
                (
                    session_id,
                    Jsonb(request.params.model_dump(mode="json")),
                    request.profile,
                    request.persist,
                    stale,
                ),
            )
            row = await cur.fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return _state(row)

    async def mark_ready(self, session_id: UUID) -> None:
        async with self._db.tx() as tx:
            cur = await tx.conn.execute(
                "update sessions set status = 'active', ready_at = now(), heartbeat_at = now() "
                "where id = %s and status = 'pending' returning state_stale",
                (session_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return
            await self._events.publish(
                tx, session_id, SessionReady(state_stale=bool(row["state_stale"]))
            )

    async def heartbeat(self, session_id: UUID) -> bool:
        async with self._db.tx() as tx:
            cur = await tx.conn.execute(
                "update sessions set heartbeat_at = now() where id = %s "
                "and status in ('pending', 'active') returning id",
                (session_id,),
            )
            return await cur.fetchone() is not None

    async def request_close(self, session_id: UUID) -> None:
        async with self._db.tx() as tx:
            cur = await tx.conn.execute(
                "select status from sessions where id = %s for update", (session_id,)
            )
            row = await cur.fetchone()
            if row is None or row["status"] in TERMINAL:
                return
            if row["status"] == "pending":
                await self._finish_locked(tx, session_id, CloseReason.CLOSED, SessionStatus.CLOSED)
                return
        self.closing.add(session_id)

    async def finish(self, session_id: UUID, reason: CloseReason) -> None:
        status = SessionStatus.CLOSED if reason is CloseReason.CLOSED else SessionStatus.DEAD
        async with self._db.tx() as tx:
            await self._finish_locked(tx, session_id, reason, status)
        self.closing.discard(session_id)
        self._cancels.pop(session_id, None)

    async def _finish_locked(
        self, tx: Tx, session_id: UUID, reason: CloseReason, status: SessionStatus
    ) -> None:
        cur = await tx.conn.execute(
            "update sessions set status = %s, closed_at = now() where id = %s "
            "and status in ('pending', 'active') returning profile, persist",
            (str(status), session_id),
        )
        row = await cur.fetchone()
        if row is None:
            return
        error = CommandError(code=ErrorCode.SESSION_DEAD, message=f"session {reason}")
        cur = await tx.conn.execute(
            "update commands set status = 'failed', error = %s, finished_at = now() "
            "where session_id = %s and status in ('queued', 'started') returning id",
            (Jsonb(error.model_dump(mode="json")), session_id),
        )
        for pending in await cur.fetchall():
            await self._events.publish(
                tx, session_id, CommandFailed(command_id=pending["id"], error=error)
            )
        if status is SessionStatus.DEAD and row["profile"] is not None and row["persist"]:
            await tx.conn.execute(
                "update profiles set stale = true where name = %s", (row["profile"],)
            )
        await self._events.publish(tx, session_id, SessionClosed(reason=reason))

    async def enqueue(self, session_id: UUID, request: CommandRequest) -> tuple[UUID, int]:
        command_id = uuid4()
        async with self._db.tx() as tx:
            cur = await tx.conn.execute(
                "update sessions set last_cmd_seq = last_cmd_seq + 1 where id = %s "
                "and status in ('pending', 'active') returning last_cmd_seq, params",
                (session_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise await self._rejection(session_id)
            seq = int(row["last_cmd_seq"])
            params = SessionParams.model_validate(row["params"])
            timeout = request.timeout if request.timeout is not None else params.timeout
            await tx.conn.execute(
                "insert into commands (id, session_id, seq, method, args, timeout_ms) "
                "values (%s, %s, %s, %s, %s, %s)",
                (
                    command_id,
                    session_id,
                    seq,
                    str(request.args.method),
                    Jsonb(request.args.model_dump(mode="json")),
                    int(timeout * 1000),
                ),
            )
        return command_id, seq

    async def _rejection(self, session_id: UUID) -> Exception:
        async with self._db.conn() as conn:
            cur = await conn.execute("select status from sessions where id = %s", (session_id,))
            row = await cur.fetchone()
        if row is None:
            return SessionNotFound(session_id)
        return SessionUnavailable(row["status"])

    async def take_next(self, session_id: UUID) -> DictRow | None:
        async with self._db.tx() as tx:
            cur = await tx.conn.execute(
                "update commands set status = 'started', started_at = now() where id = ("
                "select id from commands where session_id = %s and status = 'queued' "
                "order by seq limit 1 for update skip locked) returning *",
                (session_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await self._events.publish(tx, session_id, CommandStarted(command_id=row["id"]))
        return row

    async def complete(
        self, session_id: UUID, command_id: UUID, result: object, error: CommandError | None
    ) -> None:
        async with self._db.tx() as tx:
            cur = await tx.conn.execute(
                "update commands set status = %s, result = %s, error = %s, finished_at = now() "
                "where id = %s and session_id = %s and status = 'started' returning id",
                (
                    "failed" if error is not None else "finished",
                    Jsonb(result) if error is None else None,
                    Jsonb(error.model_dump(mode="json")) if error is not None else None,
                    command_id,
                    session_id,
                ),
            )
            if await cur.fetchone() is None:
                return
            if error is not None:
                await self._events.publish(
                    tx, session_id, CommandFailed(command_id=command_id, error=error)
                )
            else:
                await self._events.publish(
                    tx, session_id, CommandFinished(command_id=command_id, result=result)
                )

    async def fail_command(self, session_id: UUID, command_id: UUID, error: CommandError) -> None:
        await self.complete(session_id, command_id, None, error)

    async def publish_runner_event(self, session_id: UUID, data: EventData) -> None:
        async with self._db.tx() as tx:
            await self._events.publish(tx, session_id, data)

    def request_cancel(self, session_id: UUID, command_id: UUID) -> None:
        self._cancels.setdefault(session_id, []).append(command_id)

    def take_cancels(self, session_id: UUID) -> list[UUID]:
        return self._cancels.pop(session_id, [])

    async def expired_commands(self) -> list[DictRow]:
        async with self._db.conn() as conn:
            cur = await conn.execute(
                "select id, session_id from commands where status = 'started' "
                "and started_at + make_interval(secs => timeout_ms / 1000.0) < now()"
            )
            return list(await cur.fetchall())

    async def dead_candidates(self, timeout: float, ready_timeout: float) -> list[UUID]:
        async with self._db.conn() as conn:
            cur = await conn.execute(
                "select id from sessions where "
                "(status = 'active' and heartbeat_at + make_interval(secs => %s) < now()) or "
                "(status = 'pending' and created_at + make_interval(secs => %s) < now())",
                (timeout, ready_timeout),
            )
            return [row["id"] for row in await cur.fetchall()]
