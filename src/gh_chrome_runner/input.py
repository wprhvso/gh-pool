from __future__ import annotations

import asyncio
import logging
import random

from gh_chrome_protocol import SessionParams

from gh_chrome_runner.cdp import Cdp
from gh_chrome_runner.display import Display
from gh_chrome_runner.keyboard import Keyboard
from gh_chrome_runner.locate import (
    Box,
    ElementIntercepted,
    ElementMissing,
    Locator,
    Viewport,
    js_string,
)
from gh_chrome_runner.mouse import TUNINGS, wind_mouse
from gh_chrome_runner.scroll import Scroller
from gh_chrome_runner.tabs import Tabs
from gh_chrome_runner.xtest import BUTTONS, Xtest

log = logging.getLogger(__name__)

DRIFT_TOLERANCE = 2.0
MAX_ATTEMPTS = 40
SETTLE_DELAY = 0.05


class Input:
    def __init__(self, cdp: Cdp, display: Display, tabs: Tabs, params: SessionParams) -> None:
        self._display = display
        self._params = params
        self._locator = Locator(cdp, tabs)
        self._tabs = tabs
        self._xtest: Xtest | None = None
        self._keyboard: Keyboard | None = None
        self._scroller: Scroller | None = None
        self._rng = random.Random()

    async def start(self) -> None:
        self._xtest = await asyncio.to_thread(Xtest, self._display.name)
        self._keyboard = Keyboard(self._xtest, self._params.type_speed)
        self._scroller = Scroller(self._xtest, self._params.scroll_speed)

    async def stop(self) -> None:
        if self._xtest is not None:
            self._xtest.close()
            self._xtest = None

    def _require(self) -> tuple[Xtest, Keyboard, Scroller]:
        if self._xtest is None or self._keyboard is None or self._scroller is None:
            raise RuntimeError("input is not started")
        return self._xtest, self._keyboard, self._scroller

    async def click(self, selector: str, count: int = 1, button: str = "left") -> None:
        xtest, _, _ = self._require()
        code = BUTTONS[button]
        for _ in range(MAX_ATTEMPTS):
            _, _, viewport_x, viewport_y = await self._approach(selector)
            if not await self._locator.hit_test(selector, viewport_x, viewport_y):
                await asyncio.sleep(SETTLE_DELAY)
                continue
            for index in range(count):
                xtest.button(code, True)
                await asyncio.sleep(self._rng.uniform(0.04, 0.09))
                xtest.button(code, False)
                if index + 1 < count:
                    await asyncio.sleep(self._rng.uniform(0.06, 0.12))
            return
        raise ElementIntercepted(f"{selector} kept moving or stayed covered")

    async def hover(self, selector: str) -> None:
        await self._approach(selector)

    async def type_into(self, selector: str, text: str, clear: bool = False) -> None:
        _, keyboard, _ = self._require()
        await self.click(selector)
        if clear:
            await keyboard.hotkey(["ctrl", "a"])
            await keyboard.press("delete")
        await keyboard.type_text(text)

    async def press(self, key: str) -> None:
        _, keyboard, _ = self._require()
        await keyboard.press(key)

    async def hotkey(self, keys: list[str]) -> None:
        _, keyboard, _ = self._require()
        await keyboard.hotkey(keys)

    async def select(self, selector: str, value: str) -> None:
        if await self._locator.box(selector) is None:
            raise ElementMissing(selector)
        script = f"""
        (() => {{
          const el = document.querySelector({js_string(selector)});
          if (!el) return false;
          el.value = {js_string(value)};
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          el.dispatchEvent(new Event('change', {{bubbles: true}}));
          return true;
        }})()
        """
        await self._tabs.evaluate(script)

    async def scroll_to(self, selector: str) -> None:
        _, _, scroller = self._require()
        for _ in range(MAX_ATTEMPTS):
            box = await self._locator.stable_box(selector)
            viewport = await self._locator.viewport()
            if self._locator.in_viewport(box, viewport):
                return
            await scroller.by_pixels(self._locator.scroll_delta(box, viewport))
            await asyncio.sleep(SETTLE_DELAY)
        raise ElementIntercepted(f"could not bring {selector} into view")

    async def scroll_by(self, dy: int) -> None:
        _, _, scroller = self._require()
        await scroller.by_pixels(dy)

    async def _approach(self, selector: str) -> tuple[float, float, float, float]:
        box = await self._locator.stable_box(selector)
        viewport = await self._locator.viewport()
        if not self._locator.in_viewport(box, viewport):
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
            if (
                abs(drifted[0] - target[0]) > DRIFT_TOLERANCE
                or abs(drifted[1] - target[1]) > DRIFT_TOLERANCE
            ):
                await self._travel(drifted)
                target = drifted
        viewport_x, viewport_y = viewport.to_viewport(target[0], target[1])
        return target[0], target[1], viewport_x, viewport_y

    def _aim(self, box: Box, viewport: Viewport) -> tuple[float, float]:
        center_x, center_y = box.center
        jitter_x = self._rng.uniform(-0.2, 0.2) * box.width
        jitter_y = self._rng.uniform(-0.2, 0.2) * box.height
        return viewport.to_screen(center_x + jitter_x, center_y + jitter_y)

    async def _travel(self, target: tuple[float, float]) -> None:
        xtest, _, _ = self._require()
        tuning = TUNINGS[self._params.mouse_speed]
        start = xtest.pointer()
        for x, y in wind_mouse((float(start.x), float(start.y)), target, tuning, self._rng):
            xtest.move(x, y)
            if tuning.step_delay > 0:
                await asyncio.sleep(tuning.step_delay)
