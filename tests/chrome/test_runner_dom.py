import asyncio
from typing import Any

import pytest

from gh_pool.browser import dom
from gh_pool.browser.locate import ElementMissing
from gh_pool.protocol import ElementState, WaitUntil


class FakeTabs:
    def __init__(self, answers: list[Any] | None = None, default: Any = None) -> None:
        self.asked: list[str] = []
        self.sent: list[tuple[str, dict[str, Any] | None]] = []
        self.results: dict[str, dict[str, Any]] = {}
        self._answers = list(answers or [])
        self._default = default

    async def evaluate(self, expression: str, _tab: object = None) -> Any:
        self.asked.append(expression)
        if self._answers:
            return self._answers.pop(0)
        return self._default

    async def send(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.sent.append((method, params))
        return self.results.get(method, {})

    def inflight(self, _tab: object = None) -> int:
        return 0


def _tabs(answers: list[Any] | None = None, default: Any = None) -> Any:
    return FakeTabs(answers, default)


async def test_the_text_of_an_element_is_read_from_the_page():
    tabs = _tabs(["Save it"])

    assert await dom.text(tabs, "#save") == "Save it"
    assert '"#save"' in tabs.asked[0]
    assert "innerText" in tabs.asked[0]


async def test_the_value_of_a_field_is_read_from_the_page():
    assert await dom.value(_tabs(["Ada"]), "#name") == "Ada"


async def test_the_html_of_the_whole_document_is_read_without_a_selector():
    tabs = _tabs(["<html></html>"])

    assert await dom.html(tabs, None) == "<html></html>"
    assert tabs.asked[0] == "document.documentElement.outerHTML"


async def test_the_html_of_one_element_is_read_with_a_selector():
    tabs = _tabs(['<div id="saved"></div>'])

    assert await dom.html(tabs, "#saved") == '<div id="saved"></div>'
    assert "outerHTML" in tabs.asked[0]


@pytest.mark.parametrize("reader", [dom.text, dom.value])
async def test_reading_something_that_is_not_there_says_so(reader: Any):
    with pytest.raises(ElementMissing, match="#nope"):
        await reader(_tabs([dom.MISSING]), "#nope")


async def test_a_property_the_page_says_is_nothing_is_not_an_element():
    with pytest.raises(ElementMissing):
        await dom.text(_tabs([None]), "#nope")


async def test_an_attribute_is_read_from_the_element_that_has_it():
    tabs = _tabs(["name"])

    assert await dom.attr(tabs, "#name", "id") == "name"
    assert '"#name"' in tabs.asked[0]
    assert '"id"' in tabs.asked[0]


async def test_an_attribute_the_element_does_not_carry_is_nothing():
    assert await dom.attr(_tabs([None]), "#name", "placeholder") is None


async def test_an_attribute_of_an_element_that_is_not_there_says_so():
    with pytest.raises(ElementMissing, match="#nope"):
        await dom.attr(_tabs([dom.MISSING]), "#nope", "id")


async def test_the_address_and_the_title_come_from_the_document():
    assert await dom.url(_tabs(["https://example.com/x"])) == "https://example.com/x"
    assert await dom.title(_tabs(["a page"])) == "a page"


async def test_a_page_with_no_address_is_an_empty_one():
    assert await dom.url(_tabs([None])) == ""


async def test_an_expression_is_wrapped_so_it_is_an_expression():
    tabs = _tabs([2])

    assert await dom.evaluate(tabs, "1 + 1") == 2
    assert tabs.asked[0] == "(() => (1 + 1))()"


async def test_an_init_script_is_registered_and_named():
    tabs = FakeTabs()
    tabs.results["Page.addScriptToEvaluateOnNewDocument"] = {"identifier": "7"}

    assert await dom.init_script(tabs, "window.x = 1") == "7"
    assert tabs.sent[0][1] == {"source": "window.x = 1"}


async def test_a_screenshot_is_asked_for_as_a_png():
    tabs = FakeTabs()
    tabs.results["Page.captureScreenshot"] = {"data": "iVBORw0KGgo="}

    assert await dom.screenshot(tabs) == "iVBORw0KGgo="
    assert tabs.sent[0] == ("Page.captureScreenshot", {"format": "png"})


async def test_waiting_for_an_attached_element_ends_when_the_page_has_one():
    tabs = _tabs([False, False, True])

    async with asyncio.timeout(5):
        await dom.wait_for(tabs, "#late", ElementState.ATTACHED)

    assert len(tabs.asked) == 3


async def test_waiting_for_a_visible_element_waits_for_something_to_point_at():
    tabs = _tabs([None, {"x": 0, "y": 0, "width": 10, "height": 10}])

    async with asyncio.timeout(5):
        await dom.wait_for(tabs, "#late", ElementState.VISIBLE)


async def test_waiting_for_an_element_to_go_away_ends_when_it_has():
    tabs = _tabs([{"x": 0, "y": 0, "width": 10, "height": 10}, None])

    async with asyncio.timeout(5):
        await dom.wait_for_hidden(tabs, "#doomed")


async def test_waiting_for_an_address_ends_when_the_page_is_there():
    tabs = _tabs(["https://example.com/a", "https://example.com/done"])

    async with asyncio.timeout(5):
        await dom.wait_for_url(tabs, "/done$")


async def test_waiting_for_an_address_that_never_comes_keeps_waiting():
    tabs = _tabs(default="https://example.com/elsewhere")

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await dom.wait_for_url(tabs, "/done$")


async def test_waiting_for_a_condition_ends_when_the_page_agrees():
    tabs = _tabs([False, True])

    async with asyncio.timeout(5):
        await dom.wait_for_function(tabs, "window.ready")

    assert tabs.asked[0] == "Boolean(window.ready)"


async def test_waiting_for_the_load_waits_for_the_document():
    tabs = _tabs(["loading", "complete"])

    async with asyncio.timeout(5):
        await dom.wait_for_load(tabs, WaitUntil.LOAD)
