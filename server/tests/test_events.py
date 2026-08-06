from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from gh_chrome_protocol import CloseReason, EventData
from gh_chrome_protocol.events import SessionClosed, SessionReady
from gh_chrome_server.db import Database
from gh_chrome_server.events import Events, SubscriberOverflow, Subscription
from tests.helpers import command_started, fake_event


async def publish(events: Events, db: Database, session_id: UUID, data: EventData) -> int:
    async with db.tx() as tx:
        event = await events.publish(tx, session_id, data)
    return event.seq


async def test_seq_is_dense_under_concurrency(
    events: Events, db: Database, session_id: UUID
) -> None:
    payloads = [command_started() for _ in range(50)]
    seqs = await asyncio.gather(*(publish(events, db, session_id, p) for p in payloads))
    assert sorted(seqs) == list(range(1, 51))


async def test_history_returns_events_after_seq(
    events: Events, db: Database, session_id: UUID
) -> None:
    for _ in range(3):
        await publish(events, db, session_id, command_started())
    history = await events.history(session_id, 1)
    assert [event.seq for event in history] == [2, 3]


async def test_stream_delivers_history_then_live(
    events: Events, db: Database, session_id: UUID
) -> None:
    await publish(events, db, session_id, SessionReady(state_stale=False))
    received = []
    stream = events.stream(session_id, 0)
    received.append(await anext(stream))
    await publish(events, db, session_id, command_started())
    received.append(await anext(stream))
    await stream.aclose()
    assert [event.seq for event in received] == [1, 2]


async def test_stream_loses_nothing_between_history_and_live(
    events: Events, db: Database, session_id: UUID
) -> None:
    await publish(events, db, session_id, SessionReady(state_stale=False))
    stream = events.stream(session_id, 0)
    first = await anext(stream)
    await publish(events, db, session_id, command_started())
    await publish(events, db, session_id, command_started())
    second = await anext(stream)
    third = await anext(stream)
    await stream.aclose()
    assert [first.seq, second.seq, third.seq] == [1, 2, 3]


async def test_stream_ends_on_session_closed(
    events: Events, db: Database, session_id: UUID
) -> None:
    await publish(events, db, session_id, SessionClosed(reason=CloseReason.CLOSED))
    seen = [event async for event in events.stream(session_id, 0)]
    assert len(seen) == 1


async def test_stream_resumes_from_last_seq(events: Events, db: Database, session_id: UUID) -> None:
    for _ in range(4):
        await publish(events, db, session_id, command_started())
    stream = events.stream(session_id, 2)
    resumed = [await anext(stream), await anext(stream)]
    await stream.aclose()
    assert [event.seq for event in resumed] == [3, 4]


async def test_subscription_raises_after_overflow() -> None:
    sub = Subscription()
    for seq in range(1, 1102):
        sub.offer(fake_event(seq))
    with pytest.raises(SubscriberOverflow):
        await sub.get()
