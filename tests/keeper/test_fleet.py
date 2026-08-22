from __future__ import annotations

from pool.keeper.fleet import Fleet


def test_an_empty_fleet_has_nothing_to_check() -> None:
    fleet = Fleet()
    assert fleet.size() == 0
    assert fleet.tracking() is False


def test_a_submitted_runner_counts_as_capacity() -> None:
    fleet = Fleet()
    fleet.born("t1", "pool-a")
    assert fleet.size() == 1
    assert fleet.tracking() is True

    fleet.mark("t1", "running")
    assert fleet.size() == 1

    fleet.drop("t1")
    assert fleet.size() == 0


def test_status_change_resets_the_clock() -> None:
    fleet = Fleet()
    fleet.born("t1", "pool-a")
    slot = fleet.slots()[0]
    fleet.mark("t1", "running")
    assert fleet.slots()[0].since >= slot.since

    before = fleet.slots()[0].since
    fleet.mark("t1", "running")
    assert fleet.slots()[0].since == before


def test_only_unstarted_runners_are_spare() -> None:
    fleet = Fleet()
    for index in range(3):
        fleet.born(f"t{index}", f"pool-{index}")
    fleet.mark("t1", "running")

    spare = [slot.task_id for slot in fleet.spare()]

    assert "t1" not in spare
    assert spare == ["t2", "t0"]


def test_marking_a_stranger_is_harmless() -> None:
    fleet = Fleet()
    fleet.mark("нет такой", "running")
    assert fleet.drop("нет такой") is None
    assert fleet.size() == 0


def test_a_spent_runner_stops_counting() -> None:
    fleet = Fleet()
    fleet.born("t1", "pool-a")
    fleet.born("t2", "pool-b")
    fleet.mark("t1", "running")

    assert fleet.spend("pool-a") is True
    assert fleet.spend("pool-a") is False
    assert fleet.size() == 1
    assert [slot.task_id for slot in fleet.spare()] == ["t2"]
