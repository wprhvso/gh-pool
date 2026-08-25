import asyncio
import json
from typing import Any

import pytest

from gh_pool.browser.locate import (
    Box,
    ElementMissing,
    Locator,
    Viewport,
    js_string,
)

VIEWPORT = Viewport(screen_x=0.0, screen_y=100.0, scale=1.0, width=1200.0, height=800.0)


class FakeTabs:
    def __init__(self, answers: list[Any] | None = None, default: Any = None) -> None:
        self.asked: list[str] = []
        self._answers = list(answers or [])
        self._default = default

    async def evaluate(self, expression: str, _tab: object = None) -> Any:
        self.asked.append(expression)
        if self._answers:
            return self._answers.pop(0)
        return self._default


def _box(**overrides: float) -> dict[str, float]:
    measured = {"x": 100.0, "y": 200.0, "width": 80.0, "height": 40.0}
    measured.update(overrides)
    return measured


def _locator(answers: list[Any] | None = None, default: Any = None) -> Locator:
    return Locator(FakeTabs(answers, default))


def test_a_selector_becomes_a_string_the_page_can_read():
    assert js_string("#save") == '"#save"'
    assert json.loads(js_string('a[href="x"]')) == 'a[href="x"]'
    assert json.loads(js_string("a\nb\\c")) == "a\nb\\c"


def test_the_middle_of_a_box_is_where_a_click_aims():
    assert Box(x=10.0, y=20.0, width=100.0, height=40.0).center == (60.0, 40.0)


def test_a_box_that_barely_moved_is_the_same_box():
    settled = Box(x=10.0, y=20.0, width=100.0, height=40.0)

    assert settled.close_to(Box(x=10.5, y=20.0, width=100.0, height=40.0))
    assert not settled.close_to(Box(x=12.0, y=20.0, width=100.0, height=40.0))
    assert not settled.close_to(Box(x=10.0, y=20.0, width=104.0, height=40.0))


def test_a_point_on_the_page_and_a_point_on_the_screen_are_the_same_point():
    on_screen = VIEWPORT.to_screen(300.0, 400.0)

    assert on_screen == (300.0, 500.0)
    assert VIEWPORT.to_viewport(*on_screen) == (300.0, 400.0)


def test_a_page_the_browser_scaled_is_converted_with_its_scale():
    retina = Viewport(
        screen_x=20.0, screen_y=50.0, scale=2.0, width=600.0, height=400.0
    )

    assert retina.to_screen(100.0, 100.0) == (220.0, 250.0)
    assert retina.to_viewport(220.0, 250.0) == (100.0, 100.0)


@pytest.mark.parametrize(
    ("y", "height", "visible"),
    [
        (100.0, 40.0, True),
        (-20.0, 40.0, True),
        (-100.0, 40.0, False),
        (790.0, 40.0, True),
        (900.0, 40.0, False),
        (-500.0, 2000.0, True),
    ],
)
def test_an_element_is_in_view_when_any_of_it_can_be_pointed_at(
    y: float, height: float, visible: bool
):
    box = Box(x=0.0, y=y, width=100.0, height=height)

    assert _locator().in_view(box, VIEWPORT) is visible


def test_the_aim_is_the_middle_of_what_is_actually_on_screen():
    locator = _locator()
    whole = Box(x=0.0, y=100.0, width=100.0, height=40.0)
    taller_than_the_window = Box(x=0.0, y=-400.0, width=100.0, height=2000.0)

    assert locator.aim_point(whole, VIEWPORT) == 120.0
    assert locator.aim_point(taller_than_the_window, VIEWPORT) == pytest.approx(400.0)


def test_the_scroll_that_brings_an_element_to_the_middle():
    locator = _locator()

    assert (
        locator.scroll_delta(Box(x=0.0, y=1000.0, width=10.0, height=100.0), VIEWPORT)
        == 650
    )
    assert (
        locator.scroll_delta(Box(x=0.0, y=-200.0, width=10.0, height=100.0), VIEWPORT)
        == -550
    )


async def test_the_viewport_is_read_from_the_page():
    locator = _locator(
        [{"screenX": 5, "screenY": 60, "scale": 1.5, "width": 800, "height": 600}]
    )

    viewport = await locator.viewport()

    assert viewport == Viewport(
        screen_x=5.0, screen_y=60.0, scale=1.5, width=800.0, height=600.0
    )


async def test_an_element_the_page_can_measure_comes_back_as_a_box():
    locator = _locator([_box()])

    assert await locator.box("#save") == Box(x=100.0, y=200.0, width=80.0, height=40.0)


async def test_an_element_that_is_not_there_has_no_box():
    assert await _locator([None]).box("#nope") is None


@pytest.mark.parametrize("measured", [_box(width=0.0), _box(height=0.0)])
async def test_an_element_with_no_area_cannot_be_pointed_at(
    measured: dict[str, float],
):
    assert await _locator([measured]).box("#invisible") is None


async def test_waiting_for_an_element_ends_the_moment_it_appears():
    locator = _locator([None, None, _box()])

    found = await locator.wait_for_box("#late", timeout=5)

    assert found.width == 80.0


async def test_waiting_for_an_element_that_never_appears_says_so():
    locator = _locator(default=None)

    with pytest.raises(ElementMissing, match="#never"):
        await locator.wait_for_box("#never", timeout=0.05)


async def test_a_box_is_stable_once_two_readings_agree():
    locator = _locator([_box(y=200.0), _box(y=260.0), _box(y=260.0)])

    settled = await locator.stable_box("#moving", timeout=5)

    assert settled.y == 260.0


async def test_an_element_that_went_away_while_settling_is_waited_for_again():
    locator = _locator([_box(), None, _box(y=300.0), _box(y=300.0)])

    settled = await locator.stable_box("#flickering", timeout=5)

    assert settled.y == 300.0


async def test_a_hit_test_asks_the_page_what_is_under_the_point():
    tabs = FakeTabs([True])
    locator = Locator(tabs)

    assert await locator.hit_test("#save", 120.0, 220.0) is True
    assert '"#save"' in tabs.asked[0]
    assert "120" in tabs.asked[0]


async def test_a_point_covered_by_something_else_is_not_a_hit():
    assert await _locator([False]).hit_test("#covered", 1.0, 2.0) is False


async def test_measuring_does_not_hold_the_runner_still():
    locator = _locator(default=None)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await locator.wait_for_box("#never", timeout=30)
