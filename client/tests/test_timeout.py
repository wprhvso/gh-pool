from __future__ import annotations

import asyncio

import gh_chrome
import pytest
from tests.fake_runner import FakeRunner, Mode

TOKEN = "test-token"


async def test_server_side_timeout_fails_command(server: str) -> None:
    session = await gh_chrome.new(timeout=0.5, close_timeout=2.0)
    runner = FakeRunner(server, TOKEN, session.id, mode=Mode.SILENT)
    await runner.start()
    async with session as s:
        await s.ready(timeout=10)
        with pytest.raises(gh_chrome.CommandTimeout):
            await s.click("#never")
    await runner.stop()


async def test_session_survives_command_timeout(server: str) -> None:
    session = await gh_chrome.new(timeout=0.5, close_timeout=2.0)
    runner = FakeRunner(server, TOKEN, session.id, mode=Mode.SILENT)
    await runner.start()
    async with session as s:
        await s.ready(timeout=10)
        with pytest.raises(gh_chrome.CommandTimeout):
            await s.click("#never")
        assert not s._finished.is_set()
        assert (await s._http.get_session(s.id)).status == "active"
    await runner.stop()


async def test_cancel_is_delivered_to_runner(server: str) -> None:
    session = await gh_chrome.new(timeout=0.5, close_timeout=2.0)
    runner = FakeRunner(server, TOKEN, session.id, mode=Mode.SILENT)
    await runner.start()
    async with session as s:
        await s.ready(timeout=10)
        command = s.click("#never")
        with pytest.raises(gh_chrome.CommandTimeout):
            await command
        await asyncio.sleep(1.0)
        assert command.command_id in runner.cancelled
    await runner.stop()


async def test_local_timeout_does_not_kill_command(server: str) -> None:
    session = await gh_chrome.new(timeout=10.0, close_timeout=2.0)
    runner = FakeRunner(server, TOKEN, session.id, delay=1.0)
    await runner.start()
    async with session as s:
        await s.ready(timeout=10)
        command = s.click("#slow")
        with pytest.raises(TimeoutError):
            await command.wait(timeout=0.2)
        assert await command.wait(timeout=10) == {"method": "click"}
    await runner.stop()
