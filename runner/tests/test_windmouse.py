from __future__ import annotations

import itertools
import math
import random

import pytest
from gh_chrome_protocol import Speed
from gh_chrome_runner.mouse import TUNINGS, wind_mouse


def path(start: tuple[float, float], end: tuple[float, float], speed: Speed, seed: int = 0):
    return list(wind_mouse(start, end, TUNINGS[speed], random.Random(seed)))


@pytest.mark.parametrize("speed", [Speed.FAST, Speed.NORMAL, Speed.SLOW])
def test_path_ends_exactly_on_target(speed: Speed) -> None:
    points = path((0.0, 0.0), (743.0, 219.0), speed)
    assert points[-1] == (743, 219)


@pytest.mark.parametrize("speed", [Speed.FAST, Speed.NORMAL, Speed.SLOW])
def test_path_has_intermediate_points(speed: Speed) -> None:
    points = path((10.0, 10.0), (900.0, 600.0), speed)
    assert len(points) > 10


def test_instant_speed_teleports() -> None:
    assert path((0.0, 0.0), (500.0, 500.0), Speed.INSTANT) == [(500, 500)]


def test_path_is_not_a_straight_line() -> None:
    start, end = (0.0, 0.0), (800.0, 0.0)
    points = path(start, end, Speed.NORMAL)
    assert max(abs(y) for _, y in points) > 1


def test_path_is_deterministic_for_a_seed() -> None:
    assert path((0.0, 0.0), (400.0, 300.0), Speed.NORMAL, seed=7) == path(
        (0.0, 0.0), (400.0, 300.0), Speed.NORMAL, seed=7
    )


def test_consecutive_points_are_close() -> None:
    points = path((0.0, 0.0), (1000.0, 700.0), Speed.NORMAL)
    steps = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in itertools.pairwise(points)]
    assert max(steps) <= TUNINGS[Speed.NORMAL].max_step * 1.5


def test_slow_path_has_more_points_than_fast() -> None:
    slow = path((0.0, 0.0), (900.0, 500.0), Speed.SLOW, seed=3)
    fast = path((0.0, 0.0), (900.0, 500.0), Speed.FAST, seed=3)
    assert len(slow) > len(fast)


def test_zero_distance_path_is_trivial() -> None:
    assert path((100.0, 100.0), (100.0, 100.0), Speed.NORMAL) == [(100, 100)]
