from __future__ import annotations

from gh_pool.fleet.runners.budget import Budget
from gh_pool.fleet.runners.config import RATE_BLIND_WAIT, RATE_WINDOW
from tests.fleet.conftest import Clock, headers


def _fresh(clock: Clock, left: float, window: float = 3600.0) -> Budget:
    budget = Budget()
    budget.observe(
        headers(
            X_RateLimit_Remaining=str(left),
            X_RateLimit_Reset=str(clock.wall + window),
        )
    )
    return budget


def test_plain_403_is_not_a_limit(clock: Clock) -> None:
    budget = _fresh(clock, 4999)
    body = "Resource not accessible by personal access token"
    assert budget.refuse(403, headers(X_RateLimit_Remaining="4999"), body) == 0.0
    assert budget.shut() == 0.0


def test_primary_limit_closes_until_reset(clock: Clock) -> None:
    budget = _fresh(clock, 0)
    waiting = budget.refuse(
        403,
        headers(X_RateLimit_Remaining="0", X_RateLimit_Reset=str(clock.wall + 120)),
        "API rate limit exceeded for user ID 1.",
    )
    assert waiting == 120
    clock.tick(119)
    assert budget.shut() == 1
    clock.tick(1)
    assert budget.shut() == 0.0


def test_retry_after_wins(clock: Clock) -> None:
    assert _fresh(clock, 500).refuse(429, headers(Retry_After="42"), "slow down") == 42


def test_secondary_limit_without_headers(clock: Clock) -> None:
    body = "You have exceeded a secondary rate limit."
    assert _fresh(clock, 4000).refuse(403, headers(), body) == RATE_BLIND_WAIT


def test_absurd_wait_is_clamped_to_a_window(clock: Clock) -> None:
    assert (
        _fresh(clock, 0).refuse(429, headers(Retry_After="999999"), "") == RATE_WINDOW
    )


def test_a_hold_is_not_cancelled_by_the_next_success(clock: Clock) -> None:
    budget = _fresh(clock, 0)
    budget.refuse(
        429, headers(Retry_After="300"), "You have exceeded a secondary rate limit."
    )
    assert budget.shut() == 300

    budget.observe(headers(X_RateLimit_Remaining="4999"))
    assert budget.shut() == 300

    clock.tick(300)
    assert budget.shut() == 0.0


def test_headers_are_case_insensitive() -> None:
    budget = Budget()
    budget.observe(headers(x_ratelimit_remaining="7"))
    assert "остаток 7" in budget.state()
