import asyncio
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from uuid import UUID

from psycopg.types.json import Jsonb

from gh_chrome_protocol import Event, EventData, EventType
from gh_chrome_server.db import Database, Tx

QUEUE_SIZE = 1000
LIVE = ("pending", "active")


class SubscriberOverflow(Exception):
    pass


class Subscription:
    __slots__ = ("_overflow", "_queue")

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(QUEUE_SIZE)
        self._overflow = False

    def offer(self, event: Event) -> None:
        if self._overflow:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._overflow = True

    async def get(self) -> Event:
        if self._overflow:
            raise SubscriberOverflow
        return await self._queue.get()


class Events:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._subs: dict[UUID, set[Subscription]] = {}

    async def publish(self, tx: Tx, session_id: UUID, data: EventData) -> Event:
        row = await tx.one(
            "update sessions set last_seq = last_seq + 1 where id = %s returning last_seq",
            (session_id,),
        )
        if row is None:
            raise LookupError(session_id)
        event = Event(seq=int(row["last_seq"]), data=data)
        await tx.run(
            "insert into events (session_id, seq, type, data) values (%s, %s, %s, %s)",
            (
                session_id,
                event.seq,
                str(data.type),
                Jsonb(data.model_dump(mode="json")),
            ),
        )
        tx.after_commit(lambda: self._dispatch(session_id, event))
        return event

    async def stream(self, session_id: UUID, after_seq: int) -> AsyncGenerator[Event]:
        with self._subscribe(session_id) as sub:
            over = await self._over(session_id)
            delivered = after_seq
            for event in await self._history(session_id, after_seq):
                delivered = event.seq
                yield event
                if event.data.type is EventType.SESSION_CLOSED:
                    return
            if over:
                return
            while True:
                event = await sub.get()
                if event.seq <= delivered:
                    continue
                delivered = event.seq
                yield event
                if event.data.type is EventType.SESSION_CLOSED:
                    return

    @contextmanager
    def _subscribe(self, session_id: UUID) -> Generator[Subscription]:
        sub = Subscription()
        self._subs.setdefault(session_id, set()).add(sub)
        try:
            yield sub
        finally:
            subs = self._subs.get(session_id, set())
            subs.discard(sub)
            if not subs:
                self._subs.pop(session_id, None)

    def _dispatch(self, session_id: UUID, event: Event) -> None:
        for sub in tuple(self._subs.get(session_id, ())):
            sub.offer(event)

    async def _over(self, session_id: UUID) -> bool:
        row = await self._db.one(
            "select status from sessions where id = %s", (session_id,)
        )
        return row is None or row["status"] not in LIVE

    async def _history(self, session_id: UUID, after_seq: int) -> list[Event]:
        rows = await self._db.rows(
            "select seq, data from events where session_id = %s and seq > %s order by seq",
            (session_id, after_seq),
        )
        return [
            Event.model_validate({"seq": row["seq"], "data": row["data"]})
            for row in rows
        ]
