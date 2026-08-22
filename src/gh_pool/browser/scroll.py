import asyncio
import random

from gh_pool.protocol import Speed
from gh_pool.browser.xtest import Xtest

TICK_PIXELS = 120

PROFILES: dict[Speed, tuple[float, float]] = {
    Speed.INSTANT: (0.0, 0.0),
    Speed.FAST: (0.012, 0.030),
    Speed.NORMAL: (0.025, 0.070),
    Speed.SLOW: (0.045, 0.120),
}


class Scroller:
    def __init__(self, xtest: Xtest, speed: Speed) -> None:
        self._xtest = xtest
        self._speed = speed
        self._rng = random.Random()

    async def by_pixels(self, dy: int) -> None:
        if dy == 0:
            return
        ticks = max(1, round(abs(dy) / TICK_PIXELS))
        await self.by_ticks(ticks, up=dy < 0)

    async def by_ticks(self, ticks: int, up: bool) -> None:
        fast, slow = PROFILES[self._speed]
        for index in range(ticks):
            self._xtest.wheel(up)
            if fast <= 0:
                continue
            progress = (index + 1) / ticks
            delay = slow if progress < 0.2 or progress > 0.8 else fast
            await asyncio.sleep(delay * self._rng.uniform(0.7, 1.3))
