from __future__ import annotations

import asyncio

import gh_chrome
import pytest
from gh_chrome_protocol import ErrorCode, EventType
from tests.fake_runner import FakeRunner, Mode

TOKEN = "test-token"


async def test_ready_and_command_roundtrip(server: str) -> None:
    session = await gh_chrome.new(close_timeout=2.0)
    runner = FakeRunner(server, TOKEN, session.id)
    await runner.start()
    async with session as s:
        await s.ready(timeout=10)
        assert not s.state_stale
        assert await s.goto("https://example.com") == {"method": "goto"}
    await runner.stop()


async def test_commands_keep_order(server: str) -> None:
    session = await gh_chrome.new(close_timeout=2.0)
    runner = FakeRunner(server, TOKEN, session.id)
    await runner.start()
    async with session as s:
        await s.ready(timeout=10)
        await asyncio.gather(*(s.click(f"#b{n}") for n in range(5)))
    assert [envelope.seq for envelope in runner.seen] == [1, 2, 3, 4, 5]
    await runner.stop()


async def test_failure_maps_to_exception(server: str) -> None:
    session = await gh_chrome.new(close_timeout=2.0, timeout=30.0)
    runner = FakeRunner(server, TOKEN, session.id, mode=Mode.SILENT)
    await runner.start()
    async with session as s:
        await s.ready(timeout=10)
        command = s.click("#nope")
        while command.command_id is None:
            await asyncio.sleep(0.05)
        await runner.fail_next(command.command_id, ErrorCode.NOT_FOUND, "no such element")
        with pytest.raises(gh_chrome.ElementNotFound):
            await command
    await runner.stop()


async def test_runner_death_fails_pending(server: str) -> None:
    session = await gh_chrome.new(close_timeout=2.0, timeout=60.0)
    runner = FakeRunner(server, TOKEN, session.id, mode=Mode.SILENT)
    await runner.start()
    async with session as s:
        await s.ready(timeout=10)
        command = s.click("#slow")
        await runner.stop()
        with pytest.raises(gh_chrome.SessionDead):
            await command.wait(timeout=20)


async def test_events_stream_yields_session_closed(server: str) -> None:
    session = await gh_chrome.new(close_timeout=5.0)
    runner = FakeRunner(server, TOKEN, session.id)
    await runner.start()
    seen: list[EventType] = []

    async def collect() -> None:
        async for event in session.events():
            seen.append(event.data.type)

    task = asyncio.create_task(collect())
    await session.ready(timeout=10)
    closing = asyncio.create_task(session.close())
    await asyncio.wait_for(runner.closed.wait(), timeout=10)
    await runner.stop(confirm_close=True)
    await closing
    await asyncio.wait_for(task, timeout=10)
    assert EventType.SESSION_CLOSED in seen
