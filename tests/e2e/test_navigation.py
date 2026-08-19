import pytest
from tests.e2e.site import Site

from gh_chrome_client import NavigationFailed, Session, WaitUntil

pytestmark = pytest.mark.browser


async def test_the_browser_lands_on_the_page_it_was_sent_to(live: Session, site: Site):
    await live.goto(site.url("/title/hello"))

    assert await live.url() == site.url("/title/hello")
    assert await live.title() == "hello"
    assert await live.text("#what") == "hello"


async def test_history_walks_back_and_forward_and_reloads(live: Session, site: Site):
    await live.goto(site.url("/title/first"))
    await live.goto(site.url("/title/second"))

    await live.back()
    assert await live.title() == "first"

    await live.forward()
    assert await live.title() == "second"

    await live.evaluate("(window.marker = 'gone', true)")
    await live.reload()
    assert await live.evaluate("window.marker ?? null") is None


async def test_a_redirect_is_followed_and_can_be_waited_for(live: Session, site: Site):
    await live.goto(site.url("/redirect"))
    await live.wait_for_url("/title/redirected$")

    assert await live.title() == "redirected"


async def test_a_page_that_cannot_load_fails_the_command(live: Session):
    with pytest.raises(NavigationFailed):
        await live.goto("http://127.0.0.1:1/nothing-listens-here")


async def test_waiting_only_for_the_dom_returns_before_the_page_settles(
    live: Session, site: Site
):
    await live.goto(site.url("/slow?ms=6000"), wait_until=WaitUntil.DOMCONTENTLOADED)

    assert await live.evaluate("window.arrived ?? false") is False

    await live.wait_for("#late", timeout=30)
    assert await live.text("#late") == "late"


async def test_a_network_idle_wait_outlasts_the_last_request(live: Session, site: Site):
    await live.goto(site.url("/busy"))
    assert await live.evaluate("window.settled") is False

    await live.wait_for_load(wait_until=WaitUntil.NETWORKIDLE)

    assert await live.evaluate("window.settled") is True
