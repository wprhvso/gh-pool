"""Input that a page cannot tell apart from a person: XTEST, not CDP."""

import asyncio
import random

from gh_chrome_protocol import SessionParams
from gh_chrome_runner.keyboard import Keyboard
from gh_chrome_runner.locate import Box, ElementIntercepted, ElementMissing, Locator, Viewport
from gh_chrome_runner.locate import js_string as js
from gh_chrome_runner.mouse import TUNINGS, wind_mouse
from gh_chrome_runner.scroll import Scroller
from gh_chrome_runner.tabs import Tabs
from gh_chrome_runner.xtest import BUTTONS, Xtest

DRIFT_TOLERANCE = 2.0
MAX_ATTEMPTS = 40
SETTLE_DELAY = 0.05

SELECT_JS = """
(() => {
  const el = document.querySelector(%s);
  if (!el) return false;
  el.value = %s;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
})()
"""


class Input:
    def __init__(self, xtest: Xtest, tabs: Tabs, params: SessionParams) -> None:
        self._xtest = xtest
        self._tabs = tabs
        self._locator = Locator(tabs)
        self._keyboard = Keyboard(xtest, params.type_speed)
        self._scroller = Scroller(xtest, params.scroll_speed)
        self._tuning = TUNINGS[params.mouse_speed]
        self._rng = random.Random()

    def close(self) -> None:
        self._xtest.close()

    async def click(self, selector: str, count: int = 1, button: str = "left") -> None:
        code = BUTTONS[button]
        for _ in range(MAX_ATTEMPTS):
            viewport_x, viewport_y = await self._approach(selector)
            if not await self._locator.hit_test(selector, viewport_x, viewport_y):
                await asyncio.sleep(SETTLE_DELAY)  # something is on top; try again
                continue
            for index in range(count):
                self._xtest.button(code, True)
                await asyncio.sleep(self._rng.uniform(0.04, 0.09))
                self._xtest.button(code, False)
                if index + 1 < count:
                    await asyncio.sleep(self._rng.uniform(0.06, 0.12))
            return
        raise ElementIntercepted(f"{selector} kept moving or stayed covered")

    async def hover(self, selector: str) -> None:
        await self._approach(selector)

    async def type_into(self, selector: str, text: str, clear: bool = False) -> None:
        await self.click(selector)
        if clear:
            await self._keyboard.hotkey(["ctrl", "a"])
            await self._keyboard.press("delete")
        await self._keyboard.type_text(text)

    async def press(self, key: str) -> None:
        await self._keyboard.press(key)

    async def hotkey(self, keys: list[str]) -> None:
        await self._keyboard.hotkey(keys)

    async def select(self, selector: str, value: str) -> None:
        """A native select popup cannot be driven by XTEST, so set it from JS."""
        if await self._locator.box(selector) is None:
            raise ElementMissing(selector)
        await self._tabs.evaluate(SELECT_JS % (js(selector), js(value)))

    async def scroll_to(self, selector: str) -> None:
        for _ in range(MAX_ATTEMPTS):
            box = await self._locator.stable_box(selector)
            viewport = await self._locator.viewport()
            if self._locator.in_view(box, viewport):
                return
            offset = await self._tabs.evaluate("window.scrollY")
            await self._scroller.by_pixels(self._locator.scroll_delta(box, viewport))
            await asyncio.sleep(SETTLE_DELAY)
            if await self._tabs.evaluate("window.scrollY") == offset:
                return  # the page is already scrolled as far as it goes
        raise ElementIntercepted(f"could not bring {selector} into view")

    async def scroll_by(self, dy: int) -> None:
        await self._scroller.by_pixels(dy)

    async def _approach(self, selector: str) -> tuple[float, float]:
        """Move the cursor onto the element; return where it landed in the viewport."""
        box = await self._locator.stable_box(selector)
        viewport = await self._locator.viewport()
        if not self._locator.in_view(box, viewport):
            await self.scroll_to(selector)
            box = await self._locator.stable_box(selector)
            viewport = await self._locator.viewport()
        target = self._aim(box, viewport)
        await self._travel(target)
        moved = await self._locator.box(selector)
        if moved is None:
            raise ElementMissing(selector)
        if not moved.close_to(box):  # it shifted while we were on the way
            drifted = self._aim(moved, viewport)
            if any(abs(a - b) > DRIFT_TOLERANCE for a, b in zip(drifted, target, strict=True)):
                await self._travel(drifted)
                target = drifted
        return viewport.to_viewport(*target)

    def _aim(self, box: Box, viewport: Viewport) -> tuple[float, float]:
        """A point near the middle of the element, but never exactly the middle."""
        center_x, center_y = box.center
        jitter_x = self._rng.uniform(-0.2, 0.2) * box.width
        jitter_y = self._rng.uniform(-0.2, 0.2) * box.height
        return viewport.to_screen(center_x + jitter_x, center_y + jitter_y)

    async def _travel(self, target: tuple[float, float]) -> None:
        start = self._xtest.pointer()
        path = wind_mouse((float(start.x), float(start.y)), target, self._tuning, self._rng)
        for x, y in path:
            self._xtest.move(x, y)
            if self._tuning.step_delay > 0:
                await asyncio.sleep(self._tuning.step_delay)
