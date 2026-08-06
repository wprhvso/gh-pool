from __future__ import annotations

import asyncio

from gh_chrome_runner.input import Input
from gh_chrome_runner.locate import Locator
from gh_chrome_runner.tabs import Tabs


async def test_box_returns_none_for_missing(page: tuple[Tabs, Input]) -> None:
    tabs, _ = page
    locator = Locator(tabs.cdp, tabs)
    assert await locator.box("#nope") is None


async def test_viewport_offsets_are_positive(page: tuple[Tabs, Input]) -> None:
    tabs, _ = page
    locator = Locator(tabs.cdp, tabs)
    viewport = await locator.viewport()
    assert viewport.screen_y > 0
    assert viewport.width > 0


async def test_viewport_roundtrip_is_stable(page: tuple[Tabs, Input]) -> None:
    tabs, _ = page
    viewport = await Locator(tabs.cdp, tabs).viewport()
    screen = viewport.to_screen(120.0, 240.0)
    back = viewport.to_viewport(*screen)
    assert abs(back[0] - 120.0) < 0.001
    assert abs(back[1] - 240.0) < 0.001


async def test_stable_box_waits_for_animation(page: tuple[Tabs, Input]) -> None:
    tabs, controls = page
    locator = Locator(tabs.cdp, tabs)
    await controls.scroll_to("#moving")
    first = await locator.stable_box("#moving")
    await asyncio.sleep(0.3)
    assert first.close_to(await locator.stable_box("#moving"))


async def test_hit_test_detects_overlay(page: tuple[Tabs, Input]) -> None:
    tabs, controls = page
    locator = Locator(tabs.cdp, tabs)
    await controls.scroll_to("#btn")
    box = await locator.box("#btn")
    assert box is not None
    x, y = box.center
    assert await locator.hit_test("#btn", x, y)
    await tabs.evaluate("document.querySelector('#cover').style.display = 'block'")
    assert not await locator.hit_test("#btn", x, y)


async def test_click_reaches_the_button(page: tuple[Tabs, Input]) -> None:
    tabs, controls = page
    await controls.click("#btn")
    assert await tabs.evaluate("document.querySelector('#btn').dataset.clicked") == "yes"
