from __future__ import annotations

import asyncio

import gh_chrome
from tests.fake_runner import FakeRunner, Mode

TOKEN = "test-token"


async def test_stream_replays_missed_events(server: str) -> None:
    session = await gh_chrome.new(close_timeout=2.0)
    runner = FakeRunner(server, TOKEN, session.id, mode=Mode.ECHO, delay=0.3)
    await runner.start()
    async with session as s:
        await s.ready(timeout=10)
        command = s.click("#a")
        await asyncio.sleep(0.1)
        await s._stream.stop()
        await asyncio.sleep(0.5)
        s._stream.start()
        assert await command.wait(timeout=10) == {"method": "click"}
    await runner.stop()


async def test_last_seq_advances(server: str) -> None:
    session = await gh_chrome.new(close_timeout=2.0)
    runner = FakeRunner(server, TOKEN, session.id)
    await runner.start()
    async with session as s:
        await s.ready(timeout=10)
        await s.click("#a")
        assert s._stream.last_seq >= 3
    await runner.stop()


async def test_result_arriving_before_registration(server: str) -> None:
    session = await gh_chrome.new(close_timeout=2.0)
    runner = FakeRunner(server, TOKEN, session.id)
    await runner.start()
    async with session as s:
        await s.ready(timeout=10)
        results = await asyncio.gather(*(s.click(f"#x{n}") for n in range(20)))
        assert len(results) == 20
    await runner.stop()
