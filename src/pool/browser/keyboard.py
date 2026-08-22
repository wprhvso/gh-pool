import asyncio
import random

from pool.protocol import Speed
from pool.browser.xtest import SPECIAL_KEYS, Xtest

MEDIANS: dict[Speed, float] = {
    Speed.INSTANT: 0.0,
    Speed.FAST: 0.040,
    Speed.NORMAL: 0.110,
    Speed.SLOW: 0.200,
}

SIGMA = 0.35
PAUSE_AFTER = frozenset(" .,!?;:\n")
PAUSE_CHANCE = 0.12


def keysym_name(key: str) -> str:
    return SPECIAL_KEYS.get(key.lower(), key)


class Keyboard:
    def __init__(self, xtest: Xtest, speed: Speed) -> None:
        self._xtest = xtest
        self._speed = speed
        self._rng = random.Random()

    async def type_text(self, text: str) -> None:
        median = MEDIANS[self._speed]
        for character in text:
            if character == "\n":
                self._xtest.tap("Return")
            else:
                self._xtest.char(character)
            if median <= 0:
                await asyncio.sleep(0)
                continue
            delay = self._rng.lognormvariate(0.0, SIGMA) * median
            if character in PAUSE_AFTER and self._rng.random() < PAUSE_CHANCE:
                delay += self._rng.uniform(0.3, 0.6)
            await asyncio.sleep(delay)

    async def press(self, key: str) -> None:
        self._xtest.tap(keysym_name(key))
        await asyncio.sleep(0.02)

    async def hotkey(self, keys: list[str]) -> None:
        names = [keysym_name(key) for key in keys]
        for name in names:
            self._xtest.resolve(name)
        held: list[str] = []
        try:
            for name in names[:-1]:
                self._xtest.key(name, True)
                held.append(name)
                await asyncio.sleep(0.02)
            self._xtest.tap(names[-1])
        finally:
            for name in reversed(held):
                self._xtest.key(name, False)
                await asyncio.sleep(0.01)
