import pytest
from tests.e2e.site import Site
from tests.e2e.stack import Stack, Watch

from gh_chrome_client import EventType, Session, Topic
from gh_chrome_protocol import TabActivated, TabClosed, TabOpened

pytestmark = pytest.mark.browser


async def test_a_new_tab_opens_and_takes_over_the_session(live: Session, site: Site):
    await live.goto(site.url("/title/first"))

    index = await live.new_tab(site.url("/title/second"))

    assert index == 1
    assert await live.title() == "second"
    tabs = await live.tabs()
    assert [tab["index"] for tab in tabs] == [0, 1]
    assert [tab["active"] for tab in tabs] == [False, True]
    assert [tab["url"] for tab in tabs] == [
        site.url("/title/first"),
        site.url("/title/second"),
    ]
    assert [tab["title"] for tab in tabs] == ["first", "second"]


async def test_tabs_can_be_switched_between_and_closed(live: Session, site: Site):
    await live.goto(site.url("/title/first"))
    await live.new_tab(site.url("/title/second"))

    await live.activate(0)
    assert await live.title() == "first"

    await live.close_tab(1)
    tabs = await live.tabs()
    assert len(tabs) == 1
    assert tabs[0]["url"] == site.url("/title/first")
    assert tabs[0]["title"] == "first"
    assert await live.title() == "first"


async def test_closing_the_active_tab_leaves_the_session_on_the_one_on_screen(
    live: Session, site: Site
):
    await live.goto(site.url("/title/first"))
    await live.new_tab(site.url("/form"))
    await live.new_tab(site.url("/title/third"))
    await live.activate(0)

    await live.close_tab(0)

    assert await live.title() == "form"
    tabs = await live.tabs()
    assert [tab["title"] for tab in tabs] == ["form", "third"]
    assert [tab["active"] for tab in tabs] == [True, False]
    await live.click("#save")
    assert (
        await live.evaluate(
            "getComputedStyle(document.querySelector('#saved')).display"
        )
        == "block"
    )


async def test_a_link_that_opens_a_tab_carries_the_session_with_it(
    live: Session, site: Site
):
    await live.goto(site.url("/links"))

    await live.click("#external")

    await live.wait_for_url("/title/opened$", timeout=30)
    assert await live.title() == "opened"
    assert len(await live.tabs()) == 2


async def test_the_session_announces_the_tabs_it_opens_and_closes(
    stack: Stack, site: Site, desktop: None
):
    session = await stack.live(subscribe=[Topic.TABS])
    await session.goto(site.url("/links"))

    async with Watch(session) as watch:
        await session.click("#popup")
        opened = await watch.wait_for(EventType.TAB_OPENED)
        assert isinstance(opened, TabOpened)
        assert (opened.index, opened.active) == (1, True)

        await session.close_tab(1)
        closed = await watch.wait_for(EventType.TAB_CLOSED)
        assert isinstance(closed, TabClosed)
        assert closed.index == 1


async def test_a_session_can_subscribe_once_it_is_running(live: Session, site: Site):
    await live.goto(site.url("/links"))
    await live.subscribe([Topic.TABS])

    async with Watch(live) as watch:
        await live.new_tab(site.url("/title/second"))
        await live.activate(0)

        assert isinstance(await watch.wait_for(EventType.TAB_ACTIVATED), TabActivated)


async def test_a_session_without_the_subscription_stays_quiet(
    live: Session, site: Site
):
    await live.goto(site.url("/links"))

    async with Watch(live) as watch:
        await live.new_tab(site.url("/title/second"))
        await live.activate(0)

        assert watch.seen(EventType.TAB_OPENED) == []
