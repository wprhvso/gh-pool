from __future__ import annotations

import pytest
from pool.fleet.runners.models import Session, Stats, to_int


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (7, 7),
        (7.9, 7),
        ("7", 7),
        (" 7 ", 7),
        ("семь", 0),
        (None, 0),
        (True, 0),
        ([], 0),
    ],
)
def test_numbers_are_read_leniently(raw: object, expected: int) -> None:
    assert to_int(raw) == expected


def test_a_default_stands_in_for_nonsense() -> None:
    assert to_int("нет", -1) == -1


def test_statistics_are_parsed() -> None:
    stats = Stats.parse(
        {
            "totalAvailableJobs": 1,
            "totalAcquiredJobs": 2,
            "totalAssignedJobs": 3,
            "totalRunningJobs": 4,
            "totalRegisteredRunners": 5,
            "totalBusyRunners": 6,
            "totalIdleRunners": 7,
        }
    )
    assert (stats.available, stats.assigned, stats.idle) == (1, 3, 7)


def test_missing_statistics_are_zeroes() -> None:
    assert Stats.parse(None) == Stats()
    assert Stats.parse("мусор") == Stats()
    assert Stats.parse({}) == Stats()


def test_a_junk_field_does_not_break_the_parse() -> None:
    stats = Stats.parse({"totalAssignedJobs": "непонятно", "totalRunningJobs": "2"})
    assert stats.assigned == 0
    assert stats.running == 2


def test_statistics_read_as_one_line() -> None:
    assert "assigned=3" in str(Stats(assigned=3))


def test_a_session_keeps_what_it_was_given() -> None:
    session = Session(
        session_id="s1", queue_url="https://q", queue_token="t", queue_token_exp=1.0
    )
    assert session.stats == Stats()
    assert session.session_id == "s1"
