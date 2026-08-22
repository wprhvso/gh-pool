import asyncio
import random

from pool.protocol import SessionParams
from pool.browser.keyboard import Keyboard
from pool.browser.locate import (
    Box,
    ElementIntercepted,
    ElementMissing,
    Locator,
    Viewport,
)
from pool.browser.locate import js_string as js
from pool.browser.mouse import TUNINGS, wind_mouse
from pool.browser.scroll import Scroller
from pool.browser.tabs import Tabs
from pool.browser.xtest import BUTTONS, Xtest

DRIFT_TOLERANCE = 2.0
MAX_ATTEMPTS = 40
SETTLE_DELAY = 0.05
CLICK_BUDGET = 10.0
MIN_ATTEMPTS = 5

SELECT_JS = """
(() => {
  const el = document.querySelector(%s);
  if (!el) return 'missing';
  const wanted = %s;
  const options = Array.from(el.options ?? []);
  const found = options.find((option) => option.value === wanted)
    ?? options.find((option) => option.label === wanted)
    ?? options.find((option) => option.text.trim() === wanted);
  if (!found) return 'no-such-option';
  el.value = found.value;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return 'selected';
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
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CLICK_BUDGET
        attempts = 0
        while attempts < MIN_ATTEMPTS or (
            attempts < MAX_ATTEMPTS and loop.time() < deadline
        ):
            attempts += 1
            viewport_x, viewport_y = await self._approach(selector)
            if not await self._locator.hit_test(selector, viewport_x, viewport_y):
                await asyncio.sleep(SETTLE_DELAY)
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
        if await self._locator.box(selector) is None:
            raise ElementMissing(selector)
        outcome = await self._tabs.evaluate(SELECT_JS % (js(selector), js(value)))
        if outcome == "missing":
            raise ElementMissing(selector)
        if outcome != "selected":
            raise ElementMissing(f"{selector} has no option {value!r}")

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
                return
        raise ElementIntercepted(f"could not bring {selector} into view")

    async def scroll_by(self, dy: int) -> None:
        await self._scroller.by_pixels(dy)

    async def _approach(self, selector: str) -> tuple[float, float]:
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
        if not moved.close_to(box):
            drifted = self._aim(moved, viewport)
            if any(
                abs(a - b) > DRIFT_TOLERANCE
                for a, b in zip(drifted, target, strict=True)
            ):
                await self._travel(drifted)
                target = drifted
        return viewport.to_viewport(*target)

    def _aim(self, box: Box, viewport: Viewport) -> tuple[float, float]:
        center_x, _ = box.center
        center_y = self._locator.aim_point(box, viewport)
        span = min(box.height, viewport.height) / 2
        jitter_x = self._rng.uniform(-0.2, 0.2) * box.width
        jitter_y = self._rng.uniform(-0.2, 0.2) * span
        return viewport.to_screen(center_x + jitter_x, center_y + jitter_y)

    async def _travel(self, target: tuple[float, float]) -> None:
        start = self._xtest.pointer()
        path = wind_mouse(
            (float(start.x), float(start.y)), target, self._tuning, self._rng
        )
        for x, y in path:
            self._xtest.move(x, y)
            if self._tuning.step_delay > 0:
                await asyncio.sleep(self._tuning.step_delay)
