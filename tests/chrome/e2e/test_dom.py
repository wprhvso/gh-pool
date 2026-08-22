import base64
import struct

import pytest
from tests.e2e.site import Site
from tests.e2e.stack import Stack

from gh_chrome_client import ElementNotFound, ElementState, RunnerError, Session

pytestmark = pytest.mark.browser


def _png_size(image: bytes) -> tuple[int, int]:
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", image[16:24])
    return width, height


async def test_the_page_can_be_read_back_element_by_element(live: Session, site: Site):
    await live.goto(site.url("/form"))

    assert await live.text("#heading") == "Form"
    assert '<h1 id="heading">Form</h1>' in await live.html()
    assert (await live.html("#saved")).startswith('<div id="saved"')
    assert await live.attr("#name", "id") == "name"
    assert await live.attr("#name", "placeholder") is None
    assert await live.value("#colour") == "red"


async def test_reading_something_that_is_not_there_fails(live: Session, site: Site):
    await live.goto(site.url("/form"))

    with pytest.raises(ElementNotFound):
        await live.text("#nothing")
    with pytest.raises(ElementNotFound):
        await live.attr("#nothing", "id")
    with pytest.raises(ElementNotFound):
        await live.value("#nothing")


async def test_evaluate_returns_what_the_page_computed(live: Session, site: Site):
    await live.goto(site.url("/form"))

    assert await live.evaluate("1 + 1") == 2
    assert await live.evaluate("document.title") == "form"
    assert await live.evaluate("[1, 'two', null]") == [1, "two", None]
    assert await live.evaluate("({a: 1, b: [2]})") == {"a": 1, "b": [2]}
    assert (
        await live.evaluate("new Promise(r => setTimeout(() => r('later'), 50))")
        == "later"
    )


async def test_a_page_that_hands_back_a_nul_is_not_the_end_of_the_command(
    live: Session, site: Site
):
    await live.goto(site.url("/form"))

    assert await live.evaluate("'before\\u0000after'") == "before\ufffdafter"


async def test_an_expression_that_throws_comes_back_as_a_runner_error(
    live: Session, site: Site
):
    await live.goto(site.url("/form"))

    with pytest.raises(RunnerError):
        await live.evaluate("window.missing.attribute")


async def test_an_init_script_runs_before_the_page_of_the_next_document(
    live: Session, site: Site
):
    await live.init_script("window.__early = 'in place before the page';")

    await live.goto(site.url("/form"))

    assert await live.evaluate("window.__early") == "in place before the page"


async def test_a_screenshot_is_a_png_of_the_session_size(
    stack: Stack, site: Site, desktop: None
):
    session = await stack.live(width=1000, height=700)
    await session.goto(site.url("/form"))

    image = await session.screenshot_bytes()
    width, height = _png_size(image)

    assert base64.b64encode(image) == (await session.screenshot()).encode()
    viewport: list[int] = await session.evaluate("[innerWidth, innerHeight]")
    assert (width, height) == (viewport[0], viewport[1])
    assert 0 <= 1000 - width <= 40
    assert 0 <= 700 - height <= 200


async def test_waiting_for_an_element_outlasts_the_delay_that_creates_it(
    live: Session, site: Site
):
    await live.goto(site.url("/slow?ms=800"))

    await live.wait_for("#late", timeout=30)

    assert await live.text("#late") == "late"


async def test_waiting_for_an_element_to_go_away(live: Session, site: Site):
    await live.goto(site.url("/slow?ms=800"))

    await live.wait_for_hidden("#doomed", timeout=30)

    with pytest.raises(ElementNotFound):
        await live.text("#doomed")


async def test_an_attached_element_counts_even_while_it_is_invisible(
    live: Session, site: Site
):
    await live.goto(site.url("/form"))

    await live.wait_for("#saved", state=ElementState.ATTACHED, timeout=10)

    with pytest.raises(TimeoutError):
        await live.wait_for("#saved", timeout=2)


async def test_waiting_for_a_condition_the_page_reaches_on_its_own(
    live: Session, site: Site
):
    await live.goto(site.url("/slow?ms=800"))

    await live.wait_for_function("window.arrived === true", timeout=30)

    assert await live.evaluate("window.arrived") is True
