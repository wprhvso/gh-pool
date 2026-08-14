import asyncio
from typing import Any

import httpx
import pytest
from tests.e2e.site import Site
from tests.e2e.stack import HEARTBEAT_INTERVAL, TOKEN, Stack, Watch, until

import gh_chrome_client
from gh_chrome_client import (
    Cancelled,
    CommandTimeout,
    EventType,
    SessionDead,
    SessionStatus,
)
from gh_chrome_protocol import CloseReason, SessionClosed

pytestmark = pytest.mark.browser


@pytest.fixture
def server_options(request: pytest.FixtureRequest) -> dict[str, Any]:
    """A short heartbeat fuse: a runner that dies should be missed quickly.

    Short, not tight. Four missed beats rather than one and a half: the tests
    that kill a runner on purpose allow ninety seconds for the verdict, so
    nothing here is paid for by hurrying it, while the ones that kill nothing
    used to be one late beat away from having the session declared dead
    underneath them. One test wants the opposite — a watchdog that stays out of
    the way while the runner decides what to make of a stream that ended.
    """
    if request.node.get_closest_marker("patient_watchdog") is not None:
        return {"heartbeat_timeout": 120.0, "watchdog_interval": 0.2}
    return {"heartbeat_timeout": HEARTBEAT_INTERVAL * 4, "watchdog_interval": 0.2}


async def test_closing_the_session_shuts_the_runner_down(
    stack: Stack, site: Site, desktop: None
):
    session = await stack.live()
    await session.goto(site.url("/form"))
    runner = stack.runners[-1]

    await session.close()

    await until(lambda: not runner.alive, 90.0, "the runner to finish")
    assert runner.returncode == 0
    assert not session.alive


async def test_closing_while_a_command_is_running_still_shuts_the_runner_down(
    stack: Stack, site: Site, desktop: None
):
    session = await stack.live()
    await session.goto(site.url("/form"))
    runner = stack.runners[-1]
    pending = session.wait_for("#never-appears", timeout=300)
    await asyncio.sleep(1.0)

    await session.close()

    await until(lambda: not runner.alive, 90.0, "the runner to finish")
    assert runner.returncode == 0
    # Either the runner got its word in before it went, or the session ended
    # first; what matters is that the caller is not left waiting.
    with pytest.raises((Cancelled, SessionDead)):
        await pending


async def test_a_command_that_timed_out_does_not_hold_the_close(
    stack: Stack, site: Site, desktop: None
):
    session = await stack.live()
    await session.goto(site.url("/form"))
    runner = stack.runners[-1]
    with pytest.raises(CommandTimeout):
        await session.wait_for("#saved", timeout=2)

    await session.close()

    await until(lambda: not runner.alive, 90.0, "the runner to finish")
    assert runner.returncode == 0


async def test_a_runner_that_is_killed_leaves_the_session_dead(
    stack: Stack, site: Site, desktop: None
):
    session = await stack.live(profile="cut-short")
    await session.goto(site.url("/form"))

    async with Watch(session) as watch:
        # What the six hour limit does to a job, only sooner.
        stack.runners[-1].stop()
        ended = await watch.wait_for(EventType.SESSION_CLOSED, timeout=90)

    assert isinstance(ended, SessionClosed)
    assert ended.reason is CloseReason.DEAD

    profiles = await gh_chrome_client.profiles(server=stack.server.url, token=TOKEN)
    assert [(item.name, item.stale, item.size) for item in profiles] == [
        ("cut-short", True, None)
    ]


@pytest.mark.patient_watchdog
async def test_a_server_restart_does_not_leave_the_session_looking_closed(
    stack: Stack, api: httpx.AsyncClient, site: Site, desktop: None
):
    """A server that goes away and comes back has closed nothing.

    The runner loses its command stream and gives up on the session, but only
    the watchdog gets to decide what became of it: a session reported as
    cleanly closed here would tell the client all its work had finished.
    """
    session = await stack.live(profile="restarted")
    await session.goto(site.url("/form"))
    runner = stack.runners[-1]

    stack.server.restart()

    await until(lambda: not runner.alive, 90.0, "the runner to finish")
    # Long enough for a confirm_close to have arrived, had one been sent.
    await asyncio.sleep(2.0)
    state = (await api.get(f"/sessions/{session.id}")).json()
    assert state["status"] == SessionStatus.ACTIVE


async def test_the_next_session_is_told_the_state_it_inherits_is_stale(
    stack: Stack, site: Site, desktop: None
):
    session = await stack.live(profile="cut-short")
    await session.goto(site.url("/state"))
    stack.runners[-1].stop()
    await until(lambda: not session.alive, 90.0, "the session to be given up on")

    later = await stack.live(profile="cut-short")

    assert later.state_stale
    await later.goto(site.url("/state"))
    assert await later.text("#visits") == "1"
