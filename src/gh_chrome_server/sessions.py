import asyncio
import contextlib
import secrets
from datetime import datetime
from uuid import UUID, uuid4

from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from gh_chrome_protocol import (
    CloseReason,
    CommandError,
    CommandFailed,
    CommandFinished,
    CommandRequest,
    CommandStarted,
    ErrorCode,
    EventData,
    SessionClosed,
    SessionCreate,
    SessionParams,
    SessionReady,
    SessionState,
    SessionStatus,
)
from gh_chrome_protocol.trace import TraceContext
from gh_chrome_server.config import settings
from gh_chrome_server.db import Database, Tx
from gh_chrome_server.events import Events

LIVE = ("pending", "active")
_PARALLEL_LOCK = 0x6768_6301


class SessionNotFound(Exception):
    pass


class SessionUnavailable(Exception):
    pass


class TooManySessions(Exception):
    pass


class Sessions:
    def __init__(self, db: Database, events: Events) -> None:
        self._db = db
        self._events = events
        self._cancels: dict[UUID, list[UUID]] = {}
        self._work: dict[UUID, asyncio.Event] = {}
        self.closing: set[UUID] = set()

    def announce_work(self, session_id: UUID) -> None:
        self._work.setdefault(session_id, asyncio.Event()).set()

    async def wait_for_work(self, session_id: UUID, timeout: float) -> None:
        event = self._work.setdefault(session_id, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await event.wait()
        event.clear()

    async def get(self, session_id: UUID) -> SessionState:
        row = await self._db.one("select * from sessions where id = %s", (session_id,))
        if row is None:
            raise SessionNotFound(f"unknown session {session_id}")
        return _state(row)

    async def runner_token(self, session_id: UUID) -> str | None:
        row = await self._db.one(
            "select runner_token from sessions where id = %s and ("
            "status in ('pending', 'active') "
            "or closed_at > now() - make_interval(secs => %s))",
            (session_id, settings.runner_grace),
        )
        return None if row is None else row["runner_token"]

    async def live(self, session_id: UUID) -> bool:
        """Whether the session is still worth a runner's time.

        A session that was never closed by hand can still be over: the watchdog
        gives up on it, or it was closed before it ever went active. Anything
        holding a stream open on its behalf needs to hear about that.
        """
        row = await self._db.one(
            "select status from sessions where id = %s", (session_id,)
        )
        return row is not None and SessionStatus(row["status"]).live

    async def require_live(self, session_id: UUID) -> SessionState:
        state = await self.get(session_id)
        if not state.status.live:
            raise SessionUnavailable(f"session is {state.status}")
        return state

    async def started_at(self, session_id: UUID) -> datetime:
        row = await self._db.one(
            "select coalesce(ready_at, created_at) as at from sessions where id = %s",
            (session_id,),
        )
        if row is None:
            raise SessionNotFound(f"unknown session {session_id}")
        at: datetime = row["at"]
        return at

    async def create(self, request: SessionCreate) -> SessionState:
        session_id = uuid4()
        async with self._db.tx() as tx:
            if request.max_parallel is not None:
                # Without this two creates count each other's absence and both
                # pass a limit of one.
                await tx.run("select pg_advisory_xact_lock(%s)", (_PARALLEL_LOCK,))
                live = await tx.one(
                    "select count(*) as live from sessions where status in ('pending', 'active')"
                )
                if live is not None and int(live["live"]) >= request.max_parallel:
                    raise TooManySessions(
                        f"at most {request.max_parallel} sessions at a time"
                    )
            stale = await self._claim_profile(tx, request.profile)
            row = await tx.one(
                "insert into sessions "
                "(id, params, profile, persist, state_stale, runner_token) "
                "values (%s, %s, %s, %s, %s, %s) returning *",
                (
                    session_id,
                    Jsonb(request.params.model_dump(mode="json")),
                    request.profile,
                    request.persist,
                    stale,
                    secrets.token_urlsafe(32),
                ),
            )
        if row is None:
            raise SessionNotFound(f"unknown session {session_id}")
        return _state(row)

    async def _claim_profile(self, tx: Tx, profile: str | None) -> bool:
        if profile is None:
            return False
        row = await tx.one("select stale from profiles where name = %s", (profile,))
        if row is None:
            await tx.run(
                "insert into profiles (name) values (%s) on conflict do nothing",
                (profile,),
            )
            return False
        return bool(row["stale"])

    async def mark_ready(self, session_id: UUID) -> None:
        async with self._db.tx() as tx:
            row = await tx.one(
                "update sessions set status = 'active', ready_at = now(), heartbeat_at = now() "
                "where id = %s and status = 'pending' returning state_stale",
                (session_id,),
            )
            if row is not None:
                stale = bool(row["state_stale"])
                await self._events.publish(
                    tx, session_id, SessionReady(state_stale=stale)
                )

    async def heartbeat(self, session_id: UUID) -> bool:
        async with self._db.tx() as tx:
            row = await tx.one(
                "update sessions set heartbeat_at = now() "
                "where id = %s and status in ('pending', 'active') returning id",
                (session_id,),
            )
        return row is not None

    async def request_close(self, session_id: UUID) -> None:
        async with self._db.tx() as tx:
            row = await tx.one(
                "select status from sessions where id = %s for update", (session_id,)
            )
            if row is None or row["status"] not in LIVE:
                return
            if row["status"] == SessionStatus.PENDING:
                await self._finish(tx, session_id, CloseReason.CLOSED)
                return
        self.closing.add(session_id)
        self.announce_work(session_id)

    async def finish(self, session_id: UUID, reason: CloseReason) -> None:
        async with self._db.tx() as tx:
            await self._finish(tx, session_id, reason)
        self.closing.discard(session_id)
        self._cancels.pop(session_id, None)
        self.announce_work(session_id)

    async def _finish(self, tx: Tx, session_id: UUID, reason: CloseReason) -> None:
        status = (
            SessionStatus.CLOSED if reason is CloseReason.CLOSED else SessionStatus.DEAD
        )
        row = await tx.one(
            "update sessions set status = %s, closed_at = now() "
            "where id = %s and status in ('pending', 'active') "
            "returning profile, persist, ready_at",
            (str(status), session_id),
        )
        if row is None:
            return
        error = CommandError(code=ErrorCode.SESSION_DEAD, message=f"session {reason}")
        orphaned = await tx.rows(
            "update commands set status = 'failed', error = %s, finished_at = now() "
            "where session_id = %s and status in ('queued', 'started') returning id",
            (Jsonb(error.model_dump(mode="json")), session_id),
        )
        for pending in orphaned:
            await self._events.publish(
                tx, session_id, CommandFailed(command_id=pending["id"], error=error)
            )
        if (
            status is SessionStatus.DEAD
            # A session that never went active never opened the profile: a
            # dispatch that failed, or a runner that never arrived, leaves the
            # archive exactly as the last session left it.
            and row["ready_at"] is not None
            and row["profile"] is not None
            and row["persist"]
        ):
            await tx.run(
                "update profiles set stale = true where name = %s "
                "and (updated_at is null or updated_at < %s)",
                (row["profile"], row["ready_at"]),
            )
        await self._events.publish(tx, session_id, SessionClosed(reason=reason))

    async def enqueue(
        self,
        session_id: UUID,
        request: CommandRequest,
        trace: TraceContext | None = None,
    ) -> tuple[UUID, int]:
        command_id = uuid4()
        async with self._db.tx() as tx:
            row = await tx.one(
                "update sessions set last_cmd_seq = last_cmd_seq + 1 "
                "where id = %s and status in ('pending', 'active') returning last_cmd_seq, params",
                (session_id,),
            )
            if row is None:
                raise await self._rejection(tx, session_id)
            seq = int(row["last_cmd_seq"])
            params = SessionParams.model_validate(row["params"])
            timeout = request.timeout if request.timeout is not None else params.timeout
            await tx.run(
                "insert into commands "
                "(id, session_id, seq, method, args, timeout_ms, traceparent, tracestate) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    command_id,
                    session_id,
                    seq,
                    str(request.args.method),
                    Jsonb(_printable(request.args.model_dump(mode="json"))),
                    int(timeout * 1000),
                    trace.traceparent if trace is not None else None,
                    trace.tracestate if trace is not None else None,
                ),
            )
        self.announce_work(session_id)
        return command_id, seq

    async def _claim(self, tx: Tx, session_id: UUID) -> None:
        """Takes the session row before its commands.

        _finish and enqueue lock the session first and its commands second; a
        transaction that went the other way round would deadlock against them
        for as long as postgres takes to notice.
        """
        await tx.run("select id from sessions where id = %s for update", (session_id,))

    async def _rejection(self, tx: Tx, session_id: UUID) -> Exception:
        row = await tx.one("select status from sessions where id = %s", (session_id,))
        if row is None:
            return SessionNotFound(f"unknown session {session_id}")
        return SessionUnavailable(f"session is {row['status']}")

    async def take_next(self, session_id: UUID) -> DictRow | None:
        async with self._db.tx() as tx:
            await self._claim(tx, session_id)
            outstanding = await tx.one(
                "select id from commands where session_id = %s and status = 'started' limit 1",
                (session_id,),
            )
            if outstanding is not None:
                return None
            row = await tx.one(
                "update commands set status = 'started', started_at = now() where id = ("
                "select id from commands where session_id = %s and status = 'queued' "
                "order by seq limit 1 for update skip locked) returning *",
                (session_id,),
            )
            if row is None:
                return None
            await self._events.publish(
                tx, session_id, CommandStarted(command_id=row["id"])
            )
        return row

    async def complete(
        self,
        session_id: UUID,
        command_id: UUID,
        result: object,
        error: CommandError | None,
    ) -> None:
        failed = error is not None
        result = _printable(result)
        async with self._db.tx() as tx:
            await self._claim(tx, session_id)
            row = await tx.one(
                "update commands set status = %s, result = %s, error = %s, finished_at = now() "
                "where id = %s and session_id = %s and status = 'started' returning id",
                (
                    "failed" if failed else "finished",
                    None if failed else Jsonb(result),
                    Jsonb(_printable(error.model_dump(mode="json")))
                    if error is not None
                    else None,
                    command_id,
                    session_id,
                ),
            )
            if row is None:
                return
            event = (
                CommandFailed(command_id=command_id, error=error)
                if error is not None
                else CommandFinished(command_id=command_id, result=result)
            )
            await self._events.publish(tx, session_id, event)
        self.announce_work(session_id)

    async def publish_runner_event(self, session_id: UUID, data: EventData) -> None:
        async with self._db.tx() as tx:
            await self._events.publish(tx, session_id, data)

    def request_cancel(self, session_id: UUID, command_id: UUID) -> None:
        self._cancels.setdefault(session_id, []).append(command_id)
        self.announce_work(session_id)

    def take_cancels(self, session_id: UUID) -> list[UUID]:
        return self._cancels.pop(session_id, [])

    async def expired_commands(self) -> list[DictRow]:
        return await self._db.rows(
            "select id, session_id from commands where status = 'started' "
            "and started_at + make_interval(secs => timeout_ms / 1000.0) < now()"
        )

    async def dead_candidates(
        self, heartbeat_timeout: float, ready_timeout: float
    ) -> list[UUID]:
        rows = await self._db.rows(
            "select id from sessions where "
            "(status = 'active' and heartbeat_at + make_interval(secs => %s) < now()) or "
            "(status = 'pending' and created_at + make_interval(secs => %s) < now())",
            (heartbeat_timeout, ready_timeout),
        )
        return [row["id"] for row in rows]


def _printable(value: object) -> object:
    """A result the database will take.

    A page can put a NUL in anything it hands back — the text of an element, a
    key of an event, the body of a response — and postgres refuses one inside
    jsonb. Dropping the byte costs the caller a character; refusing the write
    loses the answer and leaves the command hanging until it times out.
    """
    if isinstance(value, str):
        return value.replace("\x00", "�")
    if isinstance(value, list):
        return [_printable(item) for item in value]
    if isinstance(value, dict):
        return {str(_printable(key)): _printable(item) for key, item in value.items()}
    return value


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
