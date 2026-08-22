import pytest
from tests.chrome.e2e.site import Site
from tests.chrome.e2e.stack import TOKEN, Stack

import pool.client
from pool.client import ProfileInfo

pytestmark = pytest.mark.browser


async def _profiles(stack: Stack) -> list[ProfileInfo]:
    return await pool.client.profiles(server=stack.server.url, token=TOKEN)


async def test_a_profile_carries_the_browser_into_the_next_session(
    stack: Stack, site: Site, desktop: None, zstd: None
):
    first = await stack.live(profile="the-same-person")
    await first.goto(site.url("/state"))
    assert await first.text("#visits") == "1"
    assert not first.state_stale

    await first.close()

    stored = await _profiles(stack)
    assert [item.name for item in stored] == ["the-same-person"]
    assert stored[0].size
    assert not stored[0].stale
    assert stored[0].updated_at

    second = await stack.live(profile="the-same-person")
    await second.goto(site.url("/state"))

    assert await second.text("#visits") == "2"
    assert "visitor=the-same-browser" in await second.text("#cookie")
    assert not second.state_stale


async def test_a_session_that_does_not_persist_leaves_the_profile_alone(
    stack: Stack, site: Site, desktop: None, zstd: None
):
    session = await stack.live(profile="passing-through", persist=False)
    await session.goto(site.url("/state"))

    await session.close()

    stored = await _profiles(stack)
    assert [item.name for item in stored] == ["passing-through"]
    assert stored[0].size is None
