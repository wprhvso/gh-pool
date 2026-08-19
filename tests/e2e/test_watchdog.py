import asyncio
from typing import Any

import httpx
import pytest
from tests.e2e.stack import Stack, Watch, until

from gh_chrome_client import CommandTimeout, EventType, SessionDead
from gh_chrome_protocol import CloseReason, Method, SessionClosed

HEARTBEAT_TIMEOUT = 2.0


@pytest.fixture
def server_options() -> dict[str, Any]:
    return {
        "heartbeat_timeout": HEARTBEAT_TIMEOUT,
        "ready_timeout": 3.0,
        "watchdog_interval": 0.2,
    }


async def test_a_runner_that_stops_beating_is_declared_dead(
    stack: Stack, api: httpx.AsyncClient
):
    session, runner = await stack.scripted(heartbeat=None, profile="left-open")
    runner.stalls(Method.TITLE)
    pending = session.title(timeout=60)

    async with Watch(session) as watch:
        ended = await watch.wait_for(EventType.SESSION_CLOSED, timeout=30)

    assert isinstance(ended, SessionClosed)
    assert ended.reason is CloseReason.DEAD
    assert not session.alive
    with pytest.raises(SessionDead):
        await pending

    profiles = (await api.get("/profiles")).json()
    assert [(item["name"], item["stale"]) for item in profiles] == [("left-open", True)]


async def test_a_session_the_watchdog_gives_up_on_tells_its_runner_to_stop(
    stack: Stack,
):
    _, runner = await stack.scripted(heartbeat=None)

    await runner.wait_for_close(timeout=30)

    assert runner.closed


async def test_a_session_that_keeps_beating_is_left_alone(stack: Stack):
    session, _ = await stack.scripted(heartbeat=0.3)

    await asyncio.sleep(HEARTBEAT_TIMEOUT * 1.5)

    assert session.alive


async def test_the_profile_of_a_dead_session_is_flagged_for_the_next_one(stack: Stack):
    first, _ = await stack.scripted(heartbeat=None, profile="interrupted")
    await until(lambda: not first.alive, 30.0, "the session to be given up on")

    later, _ = await stack.scripted(heartbeat=0.3, profile="interrupted")

    assert later.state_stale


async def test_a_session_that_never_ran_leaves_its_profile_alone(
    stack: Stack, api: httpx.AsyncClient
):
    session = await stack.session(profile="never-opened")

    with pytest.raises(SessionDead):
        await session.ready(timeout=30)

    profiles = (await api.get("/profiles")).json()
    assert [(item["name"], item["stale"]) for item in profiles] == [
        ("never-opened", False)
    ]


async def test_a_command_that_outstays_its_timeout_is_cut_short(stack: Stack):
    session, runner = await stack.scripted(heartbeat=0.3)
    runner.stalls(Method.TITLE)

    with pytest.raises(CommandTimeout):
        await session.title(timeout=1)

    assert await runner.wait_for_cancel() == runner.received[-1].command_id
    assert session.alive


async def test_a_runner_that_never_arrives_gives_the_session_up(stack: Stack):
    session = await stack.session()

    with pytest.raises(SessionDead):
        await session.ready(timeout=30)

    assert not session.alive
