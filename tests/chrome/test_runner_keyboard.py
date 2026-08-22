import asyncio

import pytest

from gh_chrome_protocol import Speed
from gh_chrome_runner.keyboard import MEDIANS, Keyboard, keysym_name
from gh_chrome_runner.scroll import PROFILES, TICK_PIXELS, Scroller
from gh_chrome_runner.xtest import SPECIAL_KEYS, XtestError, keysym_of


class FakeXtest:
    def __init__(self, unknown: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._unknown = set(unknown)

    def resolve(self, name: str) -> tuple[int, bool]:
        if name in self._unknown:
            raise XtestError(f"unknown key: {name}")
        self.calls.append(("resolve", name))
        return (10, False)

    def char(self, character: str) -> None:
        self.calls.append(("char", character))

    def tap(self, name: str) -> None:
        self.calls.append(("tap", name))

    def key(self, name: str, press: bool) -> None:
        self.calls.append(("key", name, press))

    def wheel(self, up: bool) -> None:
        self.calls.append(("wheel", up))


def _keyboard(
    speed: Speed = Speed.INSTANT, **kwargs: object
) -> tuple[Keyboard, FakeXtest]:
    xtest = FakeXtest(**kwargs)  # pyright: ignore[reportArgumentType]
    return Keyboard(xtest, speed), xtest  # pyright: ignore[reportArgumentType]


def _scroller(speed: Speed = Speed.INSTANT) -> tuple[Scroller, FakeXtest]:
    xtest = FakeXtest()
    return Scroller(xtest, speed), xtest  # pyright: ignore[reportArgumentType]


def test_every_speed_has_a_pace_for_typing_and_for_scrolling():
    assert set(MEDIANS) == set(Speed)
    assert set(PROFILES) == set(Speed)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("enter", "Return"),
        ("ENTER", "Return"),
        ("esc", "Escape"),
        ("pagedown", "Next"),
        ("ctrl", "Control_L"),
        ("win", "Super_L"),
        ("a", "a"),
        ("F5", "F5"),
    ],
)
def test_a_key_is_named_the_way_the_layout_names_it(given: str, expected: str):
    assert keysym_name(given) == expected


def test_every_friendly_name_maps_to_something():
    assert all(SPECIAL_KEYS.values())


@pytest.mark.parametrize(
    ("character", "keysym"), [("a", 0x61), (" ", 0x20), ("é", 0xE9), ("中", 0x1004E2D)]
)
def test_a_character_is_typed_by_the_keysym_that_means_it(character: str, keysym: int):
    assert keysym_of(character) == keysym


async def test_typing_sends_one_character_at_a_time():
    keyboard, xtest = _keyboard()

    await keyboard.type_text("hi!")

    assert xtest.calls == [("char", "h"), ("char", "i"), ("char", "!")]


async def test_a_newline_is_typed_as_the_return_key():
    keyboard, xtest = _keyboard()

    await keyboard.type_text("a\nb")

    assert xtest.calls == [("char", "a"), ("tap", "Return"), ("char", "b")]


async def test_typing_nothing_presses_nothing():
    keyboard, xtest = _keyboard()

    await keyboard.type_text("")

    assert xtest.calls == []


async def test_typing_at_the_fastest_speed_still_lets_the_runner_breathe():
    keyboard, xtest = _keyboard()
    ticks = 0

    async def counting() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    counter = asyncio.create_task(counting())
    await keyboard.type_text("a longer sentence to type")
    counter.cancel()

    assert len(xtest.calls) == len("a longer sentence to type")
    assert ticks > 1


async def test_a_slower_speed_waits_between_characters():
    keyboard, _ = _keyboard(Speed.SLOW)
    loop = asyncio.get_running_loop()

    started = loop.time()
    await keyboard.type_text("abc")

    assert loop.time() - started > MEDIANS[Speed.SLOW]


async def test_a_key_press_is_a_tap_of_the_key_it_names():
    keyboard, xtest = _keyboard()

    await keyboard.press("enter")

    assert xtest.calls == [("tap", "Return")]


async def test_a_hotkey_holds_its_modifiers_around_the_last_key():
    keyboard, xtest = _keyboard()

    await keyboard.hotkey(["ctrl", "shift", "a"])

    assert xtest.calls == [
        ("resolve", "Control_L"),
        ("resolve", "Shift_L"),
        ("resolve", "a"),
        ("key", "Control_L", True),
        ("key", "Shift_L", True),
        ("tap", "a"),
        ("key", "Shift_L", False),
        ("key", "Control_L", False),
    ]


async def test_a_hotkey_of_one_key_is_a_tap():
    keyboard, xtest = _keyboard()

    await keyboard.hotkey(["escape"])

    assert xtest.calls == [("resolve", "Escape"), ("tap", "Escape")]


async def test_a_key_the_layout_cannot_produce_leaves_no_modifier_held():
    keyboard, xtest = _keyboard(unknown=("nonsense",))

    with pytest.raises(XtestError, match="nonsense"):
        await keyboard.hotkey(["ctrl", "nonsense"])

    assert all(call[0] != "key" for call in xtest.calls)


async def test_a_scroll_is_a_wheel_tick_for_every_notch_of_the_distance():
    scroller, xtest = _scroller()

    await scroller.by_pixels(TICK_PIXELS * 3)

    assert xtest.calls == [("wheel", False)] * 3


async def test_scrolling_up_turns_the_wheel_the_other_way():
    scroller, xtest = _scroller()

    await scroller.by_pixels(-TICK_PIXELS * 2)

    assert xtest.calls == [("wheel", True)] * 2


async def test_a_distance_smaller_than_a_notch_still_scrolls_once():
    scroller, xtest = _scroller()

    await scroller.by_pixels(10)

    assert xtest.calls == [("wheel", False)]


async def test_scrolling_nowhere_leaves_the_page_where_it_was():
    scroller, xtest = _scroller()

    await scroller.by_pixels(0)

    assert xtest.calls == []


async def test_a_scroll_by_ticks_is_exactly_that_many_ticks():
    scroller, xtest = _scroller()

    await scroller.by_ticks(5, up=True)

    assert xtest.calls == [("wheel", True)] * 5
