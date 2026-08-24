import asyncio
from typing import Any

import pytest

from gh_pool.browser import input as input_module
from gh_pool.browser.input import Input
from gh_pool.browser.locate import ElementIntercepted, ElementMissing
from gh_pool.browser.xtest import BUTTONS, Point
from gh_pool.protocol import SessionParams, Speed

VIEWPORT = {
    "screenX": 0,
    "screenY": 100,
    "scale": 1,
    "width": 1200,
    "height": 800,
}
BOX = {"x": 100.0, "y": 200.0, "width": 80.0, "height": 40.0}
TYPED = 0


class FakeXtest:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.at = Point(0, 0)

    def pointer(self) -> Point:
        return self.at

    def move(self, x: int, y: int) -> None:
        self.at = Point(int(x), int(y))
        self.calls.append(("move", int(x), int(y)))

    def button(self, code: int, press: bool) -> None:
        self.calls.append(("button", code, press))

    def wheel(self, up: bool) -> None:
        self.calls.append(("wheel", up))

    def char(self, character: str) -> None:
        self.calls.append(("char", character))

    def tap(self, name: str) -> None:
        self.calls.append(("tap", name))

    def key(self, name: str, press: bool) -> None:
        self.calls.append(("key", name, press))

    def resolve(self, _name: str) -> tuple[int, bool]:
        return (10, False)

    def close(self) -> None:
        self.calls.append(("close",))

    @property
    def clicks(self) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == "button"]


class FakeTabs:
    def __init__(
        self,
        *,
        box: dict[str, float] | None = BOX,
        hit: bool | list[bool] = True,
        select: str = "selected",
        scroll: list[int] | None = None,
    ) -> None:
        self.asked: list[str] = []
        self._box = box
        self._hit = hit if isinstance(hit, list) else [hit]
        self._select = select
        self._scroll = list(scroll or [0])

    async def evaluate(self, expression: str, _tab: object = None) -> Any:
        self.asked.append(expression)
        if expression.startswith("({"):
            return VIEWPORT
        if "getBoundingClientRect" in expression:
            return self._box
        if "elementFromPoint" in expression:
            return self._hit.pop(0) if len(self._hit) > 1 else self._hit[0]
        if "el.options" in expression:
            return self._select
        if "requestAnimationFrame" in expression:
            return True
        if "document.activeElement" in expression:
            return TYPED
        if expression == "window.scrollY":
            return self._scroll.pop(0) if len(self._scroll) > 1 else self._scroll[0]
        raise AssertionError(expression)


def _input(tabs: FakeTabs, speed: Speed = Speed.INSTANT) -> tuple[Input, FakeXtest]:
    xtest = FakeXtest()
    params = SessionParams(mouse_speed=speed, type_speed=speed, scroll_speed=speed)
    return Input(xtest, tabs, params), xtest  # pyright: ignore[reportArgumentType]


async def test_a_click_lands_inside_the_element_it_was_aimed_at():
    tabs = FakeTabs()
    pointer, xtest = _input(tabs)

    await pointer.click("#save")

    moves = [call for call in xtest.calls if call[0] == "move"]
    assert moves
    assert 100 <= moves[-1][1] <= 180
    assert 300 <= moves[-1][2] <= 340
    assert xtest.clicks == [
        ("button", BUTTONS["left"], True),
        ("button", BUTTONS["left"], False),
    ]


async def test_a_double_click_presses_twice():
    pointer, xtest = _input(FakeTabs())

    await pointer.click("#save", count=2)

    assert len(xtest.clicks) == 4


async def test_the_button_a_click_uses_is_the_one_it_was_told_to():
    pointer, xtest = _input(FakeTabs())

    await pointer.click("#save", button="right")

    assert xtest.clicks[0] == ("button", BUTTONS["right"], True)


async def test_hovering_moves_the_cursor_and_presses_nothing():
    pointer, xtest = _input(FakeTabs())

    await pointer.hover("#save")

    assert any(call[0] == "move" for call in xtest.calls)
    assert xtest.clicks == []


async def test_an_element_that_is_not_there_cannot_be_clicked():
    pointer, _ = _input(FakeTabs(box=None))

    with pytest.raises(ElementMissing):
        await pointer.click("#nope")


async def test_an_element_that_stays_covered_is_reported_rather_than_clicked(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(input_module, "CLICK_BUDGET", 0.2)
    monkeypatch.setattr(input_module, "MIN_ATTEMPTS", 2)
    pointer, xtest = _input(FakeTabs(hit=False))

    with pytest.raises(ElementIntercepted, match="#covered"):
        await pointer.click("#covered")

    assert xtest.clicks == []


async def test_a_click_that_had_to_wait_still_happens(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(input_module, "MIN_ATTEMPTS", 1)
    pointer, xtest = _input(FakeTabs(hit=[False, False, True]))

    await pointer.click("#slow")

    assert len(xtest.clicks) == 2


async def test_typing_clicks_the_field_first():
    pointer, xtest = _input(FakeTabs())

    await pointer.type_into("#name", "Ada")

    assert xtest.clicks
    assert [call for call in xtest.calls if call[0] == "char"] == [
        ("char", "A"),
        ("char", "d"),
        ("char", "a"),
    ]


async def test_typing_over_a_field_clears_it_first():
    pointer, xtest = _input(FakeTabs())

    await pointer.type_into("#name", "Ada", clear=True)

    names = [call for call in xtest.calls if call[0] in {"key", "tap"}]
    assert ("key", "Control_L", True) in names
    assert ("tap", "Delete") in names


class StuckTabs(FakeTabs):
    async def evaluate(self, expression: str, _tab: object = None) -> Any:
        if "requestAnimationFrame" in expression:
            await asyncio.Event().wait()
        return await super().evaluate(expression, _tab)


async def test_a_page_that_never_settles_does_not_hold_the_click(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(input_module, "SETTLE_TIMEOUT", 0.1)
    pointer, xtest = _input(StuckTabs())

    async with asyncio.timeout(5):
        await pointer.click("#save")

    assert len(xtest.clicks) == 2


async def test_an_option_the_page_has_is_chosen():
    tabs = FakeTabs()
    pointer, _ = _input(tabs)

    await pointer.select("#colour", "blue")

    assert any("el.options" in asked for asked in tabs.asked)


async def test_an_option_the_page_does_not_have_is_reported():
    pointer, _ = _input(FakeTabs(select="no-such-option"))

    with pytest.raises(ElementMissing, match="blue"):
        await pointer.select("#colour", "blue")


async def test_selecting_in_something_that_is_not_there_is_reported():
    pointer, _ = _input(FakeTabs(box=None))

    with pytest.raises(ElementMissing):
        await pointer.select("#colour", "blue")


async def test_an_element_already_in_view_is_not_scrolled_to():
    pointer, xtest = _input(FakeTabs())

    await pointer.scroll_to("#save")

    assert [call for call in xtest.calls if call[0] == "wheel"] == []


async def test_a_page_that_will_not_move_stops_being_asked_to():
    below = {"x": 100.0, "y": 5000.0, "width": 80.0, "height": 40.0}
    pointer, xtest = _input(FakeTabs(box=below, scroll=[0]))

    await pointer.scroll_to("#deep")

    assert len([call for call in xtest.calls if call[0] == "wheel"]) > 0


async def test_the_display_is_let_go_of_when_the_session_ends():
    pointer, xtest = _input(FakeTabs())

    pointer.close()

    assert ("close",) in xtest.calls


async def test_a_click_does_not_hold_the_runner_still(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(input_module, "CLICK_BUDGET", 30.0)
    monkeypatch.setattr(input_module, "MIN_ATTEMPTS", 1000)
    pointer, _ = _input(FakeTabs(hit=False))

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):
            await pointer.click("#covered")
