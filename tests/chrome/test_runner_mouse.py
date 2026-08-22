import math
import random
from itertools import pairwise

import pytest

from gh_pool.browser.mouse import STEP_LIMIT, TUNINGS, Tuning, wind_mouse
from gh_pool.protocol import Speed

MOVING = [speed for speed in Speed if speed is not Speed.INSTANT]


def _path(
    start: tuple[float, float], end: tuple[float, float], speed: Speed, seed: int = 7
) -> list[tuple[int, int]]:
    return list(wind_mouse(start, end, TUNINGS[speed], random.Random(seed)))


def test_every_speed_has_a_tuning():
    assert set(TUNINGS) == set(Speed)


def test_an_instant_move_is_a_single_jump_to_the_target():
    assert _path((0.0, 0.0), (640.0, 480.0), Speed.INSTANT) == [(640, 480)]


@pytest.mark.parametrize("speed", MOVING)
def test_a_move_ends_exactly_on_the_target(speed: Speed):
    path = _path((12.0, 30.0), (1600.0, 900.0), speed)

    assert path[-1] == (1600, 900)


@pytest.mark.parametrize("speed", MOVING)
def test_a_move_travels_rather_than_teleports(speed: Speed):
    path = _path((0.0, 0.0), (1200.0, 700.0), speed)

    assert len(path) > 5
    assert all(
        math.dist(before, after) <= TUNINGS[speed].max_step * 2
        for before, after in pairwise(path)
    )


@pytest.mark.parametrize("speed", MOVING)
def test_no_two_consecutive_points_are_the_same_pixel(speed: Speed):
    path = _path((0.0, 0.0), (900.0, 400.0), speed)

    assert all(before != after for before, after in pairwise(path))


@pytest.mark.parametrize("speed", MOVING)
def test_a_move_that_goes_nowhere_still_names_the_target(speed: Speed):
    assert _path((500.0, 500.0), (500.0, 500.0), speed) == [(500, 500)]


@pytest.mark.parametrize("speed", MOVING)
def test_a_short_move_terminates_instead_of_orbiting_its_target(speed: Speed):
    path = _path((0.0, 0.0), (6.0, 0.0), speed)

    assert path[-1] == (6, 0)
    assert len(path) < STEP_LIMIT


@pytest.mark.parametrize("speed", MOVING)
def test_every_move_across_a_full_screen_converges(speed: Speed):
    for seed in range(120):
        rng = random.Random(seed)
        start = (rng.uniform(0, 1920), rng.uniform(0, 1080))
        end = (rng.uniform(0, 1920), rng.uniform(0, 1080))

        path = list(wind_mouse(start, end, TUNINGS[speed], rng))

        assert path[-1] == (round(end[0]), round(end[1]))
        assert len(path) < STEP_LIMIT


def test_a_tuning_that_cannot_settle_is_still_bounded():
    runaway = Tuning(
        gravity=0.0, wind=0.0, max_step=1000.0, target_area=1.0, step_delay=0.0
    )

    path = list(wind_mouse((0.0, 0.0), (900.0, 0.0), runaway, random.Random(1)))

    assert len(path) <= STEP_LIMIT + 1


def test_the_same_seed_walks_the_same_path():
    assert _path((0.0, 0.0), (800.0, 600.0), Speed.NORMAL, seed=3) == _path(
        (0.0, 0.0), (800.0, 600.0), Speed.NORMAL, seed=3
    )


def test_a_slower_speed_takes_smaller_steps():
    fast = _path((0.0, 0.0), (1500.0, 800.0), Speed.FAST)
    slow = _path((0.0, 0.0), (1500.0, 800.0), Speed.SLOW)

    assert len(slow) > len(fast)
