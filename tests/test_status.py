import pytest

from gh_pool.status import FINISHED, LIVE, REPORTABLE, TaskStatus


def test_a_status_is_its_own_wire_string():
    assert TaskStatus.DONE == "done"
    assert list(TaskStatus) == [
        "pending",
        "running",
        "done",
        "failed",
        "cancelled",
        "lost",
    ]


@pytest.mark.parametrize("status", ["pending", "running"])
def test_a_live_status_is_not_finished(status: str):
    assert status in LIVE
    assert status not in FINISHED


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "lost"])
def test_a_finished_status_is_not_live(status: str):
    assert status in FINISHED
    assert status not in LIVE


def test_only_the_worker_reportable_statuses_leave_out_lost():
    assert "lost" in FINISHED
    assert "lost" not in REPORTABLE
    assert REPORTABLE < FINISHED


def test_the_properties_agree_with_the_sets():
    for status in TaskStatus:
        assert status.live is (status in LIVE)
        assert status.finished is (status not in LIVE)
        assert status.reportable is (status in REPORTABLE)
