import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from tests.e2e.stack import Stack, Watch, expression_of

from gh_chrome_client import EventType
from gh_chrome_protocol import (
    CloseReason,
    CommandEnvelope,
    Method,
    SessionClosed,
    TabOpened,
)
from gh_chrome_protocol.sse import SseMessage, parse_sse


async def _frames(
    api: httpx.AsyncClient,
    session_id: object,
    *,
    last_seq: int = 0,
    header: int | None = None,
    count: int = 1,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    headers = {"Accept": "text/event-stream"}
    if header is not None:
        headers["Last-Event-ID"] = str(header)
    collected: list[dict[str, Any]] = []
    async with api.stream(
        "GET",
        f"/sessions/{session_id}/events",
        params={"last_seq": last_seq},
        headers=headers,
        timeout=httpx.Timeout(30.0, read=None),
    ) as response:
        response.raise_for_status()
        async with asyncio.timeout(timeout):
            async for message in parse_sse(response.aiter_bytes()):
                collected.append(json.loads(message.data))
                if len(collected) >= count:
                    break
    return collected


async def test_the_stream_hands_a_latecomer_the_whole_history(
    stack: Stack, api: httpx.AsyncClient
):
    session, runner = await stack.scripted()
    runner.returns(Method.TITLE, "a page")
    await session.title()

    frames = await _frames(api, session.id, count=3)

    assert [frame["data"]["type"] for frame in frames] == [
        EventType.SESSION_READY,
        EventType.COMMAND_STARTED,
        EventType.COMMAND_FINISHED,
    ]
    assert [frame["seq"] for frame in frames] == [1, 2, 3]
    assert frames[2]["data"]["result"] == "a page"


async def test_resuming_after_a_sequence_number_skips_what_was_seen(
    stack: Stack, api: httpx.AsyncClient
):
    session, runner = await stack.scripted()
    runner.returns(Method.TITLE, "a page")
    await session.title()

    frames = await _frames(api, session.id, last_seq=2, count=1)

    assert frames[0]["seq"] == 3


async def test_the_last_event_id_header_wins_over_the_query(
    stack: Stack, api: httpx.AsyncClient
):
    session, runner = await stack.scripted()
    runner.returns(Method.TITLE, "a page")
    await session.title()

    frames = await _frames(api, session.id, last_seq=0, header=2, count=1)

    assert frames[0]["seq"] == 3


async def test_the_sequence_has_no_gaps_across_a_run_of_commands(
    stack: Stack, api: httpx.AsyncClient
):
    session, runner = await stack.scripted()
    runner.on(Method.EVAL, expression_of)
    for index in range(4):
        await session.evaluate(f"page-{index}")

    frames = await _frames(api, session.id, count=9)

    assert [frame["seq"] for frame in frames] == list(range(1, 10))


async def test_what_the_runner_announces_reaches_the_client(stack: Stack):
    session, runner = await stack.scripted()

    async with Watch(session) as watch:
        await runner.client.event(
            TabOpened(index=1, url="https://example.com/", active=True)
        )
        announced = await watch.wait_for(EventType.TAB_OPENED)

    assert isinstance(announced, TabOpened)
    assert announced.url == "https://example.com/"
    assert announced.index == 1


async def test_the_end_of_the_session_ends_every_stream(
    stack: Stack, api: httpx.AsyncClient
):
    session, _ = await stack.scripted()

    async with Watch(session) as watch:
        await session.close()
        ended = await watch.wait_for(EventType.SESSION_CLOSED)

    assert isinstance(ended, SessionClosed)
    assert ended.reason is CloseReason.CLOSED
    replayed = await _frames(api, session.id, count=99, timeout=10.0)
    assert replayed[-1]["data"]["type"] == EventType.SESSION_CLOSED


async def test_a_stream_resumed_past_the_end_does_not_wait_for_more(
    stack: Stack, api: httpx.AsyncClient
):
    session, _ = await stack.scripted()
    await session.close()
    ended = await _frames(api, session.id, count=99, timeout=10.0)

    async with asyncio.timeout(10):
        nothing = await _frames(
            api, session.id, last_seq=ended[-1]["seq"], count=99, timeout=5.0
        )

    assert nothing == []


async def test_the_client_picks_the_stream_back_up_after_a_restart(stack: Stack):
    session, first = await stack.scripted()
    first.returns(Method.TITLE, "before")
    assert await session.title() == "before"

    stack.server.restart()
    await first.stop()
    second = await stack.scripted_for(session)
    second.returns(Method.TITLE, "after")

    assert await session.title(timeout=60) == "after"


async def test_a_command_outlives_a_cancel_of_whoever_was_waiting(stack: Stack):
    session, runner = await stack.scripted()
    finish = asyncio.Event()

    async def handler(_envelope: CommandEnvelope) -> str:
        await finish.wait()
        return "a page"

    runner.on(Method.TITLE, handler)
    command = session.title()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(command, 0.2)

    assert not command.done()
    finish.set()
    assert await command.wait(timeout=30) == "a page"


async def test_the_stream_is_live_from_the_moment_it_is_asked_for(stack: Stack):
    session, runner = await stack.scripted()
    stream = session.events()

    await runner.client.event(
        TabOpened(index=2, url="https://example.com/", active=True)
    )
    await asyncio.sleep(1.0)

    async with asyncio.timeout(20):
        event = await anext(stream)
    assert isinstance(event.data, TabOpened)
    assert event.data.index == 2


async def test_an_event_this_client_cannot_read_does_not_wedge_the_stream(
    stack: Stack,
):
    session, runner = await stack.scripted()
    ahead = session._last_seq + 1
    unreadable = SseMessage(
        event="teleported",
        data=f'{{"seq": {ahead}, "data": {{"type": "teleported", "where": "away"}}}}',
        id=str(ahead),
    )

    session._take(unreadable)

    assert session._last_seq == ahead
    async with Watch(session) as watch:
        await runner.client.event(
            TabOpened(index=1, url="https://example.com/", active=True)
        )
        assert await watch.wait_for(EventType.TAB_OPENED)


async def test_an_unknown_session_has_no_stream(api: httpx.AsyncClient):
    response = await api.get(f"/sessions/{uuid4()}/events")

    assert response.status_code == 404
