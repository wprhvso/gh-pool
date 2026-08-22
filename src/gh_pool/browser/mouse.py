import math
import random
from collections.abc import Iterator
from dataclasses import dataclass

from gh_pool.protocol import Speed

SQRT3 = math.sqrt(3)
SQRT5 = math.sqrt(5)
STEP_LIMIT = 10_000


@dataclass(frozen=True, slots=True)
class Tuning:
    gravity: float
    wind: float
    max_step: float
    target_area: float
    step_delay: float

    @property
    def instant(self) -> bool:
        return self.max_step <= 0


TUNINGS: dict[Speed, Tuning] = {
    Speed.INSTANT: Tuning(
        gravity=0.0, wind=0.0, max_step=0.0, target_area=0.0, step_delay=0.0
    ),
    Speed.FAST: Tuning(
        gravity=12.0, wind=2.0, max_step=28.0, target_area=12.0, step_delay=0.004
    ),
    Speed.NORMAL: Tuning(
        gravity=9.0, wind=3.0, max_step=15.0, target_area=10.0, step_delay=0.008
    ),
    Speed.SLOW: Tuning(
        gravity=6.0, wind=4.0, max_step=8.0, target_area=8.0, step_delay=0.014
    ),
}


def wind_mouse(
    start: tuple[float, float],
    end: tuple[float, float],
    tuning: Tuning,
    rng: random.Random | None = None,
) -> Iterator[tuple[int, int]]:
    if tuning.instant:
        yield round(end[0]), round(end[1])
        return
    source = rng or random
    x, y = start
    target_x, target_y = end
    wind_x = wind_y = 0.0
    velocity_x = velocity_y = 0.0
    last: tuple[int, int] | None = None
    distance = math.hypot(target_x - x, target_y - y)
    steps = 0
    while distance >= 1.0 and steps < STEP_LIMIT:
        steps += 1
        wind = min(tuning.wind, distance)
        if distance >= tuning.target_area:
            wind_x = wind_x / SQRT3 + (source.random() * (2 * wind + 1) - wind) / SQRT5
            wind_y = wind_y / SQRT3 + (source.random() * (2 * wind + 1) - wind) / SQRT5
        else:
            wind_x /= SQRT3
            wind_y /= SQRT3
        velocity_x += wind_x + tuning.gravity * (target_x - x) / distance
        velocity_y += wind_y + tuning.gravity * (target_y - y) / distance
        speed = math.hypot(velocity_x, velocity_y)
        step = tuning.max_step
        if distance < tuning.target_area:
            step = min(
                distance, max(3.0, tuning.max_step * distance / tuning.target_area)
            )
        if speed > step:
            clip = step / 2 + source.random() * step / 2
            velocity_x = velocity_x / speed * clip
            velocity_y = velocity_y / speed * clip
        x += velocity_x
        y += velocity_y
        distance = math.hypot(target_x - x, target_y - y)
        point = (round(x), round(y))
        if point != last:
            last = point
            yield point
    final = (round(target_x), round(target_y))
    if final != last:
        yield final
