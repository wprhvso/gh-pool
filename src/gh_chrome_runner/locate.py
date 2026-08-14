import asyncio
import json
from dataclasses import dataclass

from gh_chrome_runner.tabs import Tabs

STABLE_INTERVAL = 0.1
STABLE_EPSILON = 1.0
POLL_INTERVAL = 0.1
APPEAR_TIMEOUT = 10.0


class ElementMissing(Exception):
    pass


class ElementIntercepted(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Box:
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    def close_to(self, other: "Box") -> bool:
        return all(
            abs(mine - theirs) <= STABLE_EPSILON
            for mine, theirs in (
                (self.x, other.x),
                (self.y, other.y),
                (self.width, other.width),
                (self.height, other.height),
            )
        )


@dataclass(frozen=True, slots=True)
class Viewport:
    screen_x: float
    screen_y: float
    scale: float
    width: float
    height: float

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        return self.screen_x + x * self.scale, self.screen_y + y * self.scale

    def to_viewport(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.screen_x) / self.scale, (y - self.screen_y) / self.scale


VIEWPORT_JS = """({
  screenX: window.screenX + (window.outerWidth - window.innerWidth) / 2,
  screenY: window.screenY + (window.outerHeight - window.innerHeight),
  scale: window.devicePixelRatio,
  width: window.innerWidth,
  height: window.innerHeight
})"""

BOX_JS = """
(() => {
  const el = document.querySelector(%s);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const style = getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none') return null;
  return {x: r.x, y: r.y, width: r.width, height: r.height};
})()
"""

HIT_JS = """
(() => {
  const target = document.querySelector(%s);
  if (!target) return false;
  const hit = document.elementFromPoint(%f, %f);
  if (!hit) return false;
  return target === hit || target.contains(hit) || hit.contains(target);
})()
"""


def js_string(value: str) -> str:
    return json.dumps(value)


class Locator:
    def __init__(self, tabs: Tabs) -> None:
        self._tabs = tabs

    async def viewport(self) -> Viewport:
        data = await self._tabs.evaluate(VIEWPORT_JS)
        return Viewport(
            screen_x=float(data["screenX"]),
            screen_y=float(data["screenY"]),
            scale=float(data["scale"]),
            width=float(data["width"]),
            height=float(data["height"]),
        )

    async def box(self, selector: str) -> Box | None:
        data = await self._tabs.evaluate(BOX_JS % js_string(selector))
        if data is None:
            return None
        box = Box(
            x=float(data["x"]),
            y=float(data["y"]),
            width=float(data["width"]),
            height=float(data["height"]),
        )
        return box if box.width > 0 and box.height > 0 else None

    async def wait_for_box(self, selector: str, timeout: float = APPEAR_TIMEOUT) -> Box:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            box = await self.box(selector)
            if box is not None:
                return box
            if asyncio.get_running_loop().time() >= deadline:
                raise ElementMissing(f"{selector} did not appear in {timeout}s")
            await asyncio.sleep(POLL_INTERVAL)

    async def stable_box(self, selector: str, timeout: float = APPEAR_TIMEOUT) -> Box:
        previous = await self.wait_for_box(selector, timeout)
        while True:
            await asyncio.sleep(STABLE_INTERVAL)
            current = await self.box(selector)
            if current is None:
                previous = await self.wait_for_box(selector, timeout)
            elif current.close_to(previous):
                return current
            else:
                previous = current

    async def hit_test(self, selector: str, x: float, y: float) -> bool:
        return bool(await self._tabs.evaluate(HIT_JS % (js_string(selector), x, y)))

    def in_view(self, box: Box, viewport: Viewport, margin: float = 8.0) -> bool:
        """Whether there is somewhere on this element the cursor can be put.

        Not whether the whole of it fits: a hero image or a full-page dialog is
        taller than the window and would never satisfy that, so scroll_to would
        push it up and down until it gave up.
        """
        top, bottom = margin, viewport.height - margin
        return box.y < bottom and box.y + box.height > top and top < bottom

    def aim_point(self, box: Box, viewport: Viewport, margin: float = 8.0) -> float:
        """The y of the visible middle of the element, in viewport coordinates."""
        top = max(box.y, margin)
        bottom = min(box.y + box.height, viewport.height - margin)
        return (top + bottom) / 2

    def scroll_delta(self, box: Box, viewport: Viewport) -> int:
        return round(box.y + box.height / 2 - viewport.height / 2)
