import pytest
from fastapi import HTTPException

from gh_pool.server.pool import state
from gh_pool.server.pool.queue import owned
from gh_pool.status import TaskStatus


@pytest.fixture(autouse=True)
def clean():
    state.reset()
    yield
    state.reset()


def test_a_recovered_task_without_a_lease_column_refuses_the_lease():
    state.TASKS["t1"] = {"id": "t1", "status": TaskStatus.PENDING}

    with pytest.raises(HTTPException) as caught:
        owned("t1", "a-token-from-nowhere")

    assert caught.value.status_code == 409


def test_a_running_task_recovered_without_a_lease_adopts_the_first_one():
    state.TASKS["t1"] = {"id": "t1", "status": TaskStatus.RUNNING}

    assert owned("t1", "the-worker-still-holding-it")["lease_token"] == (
        "the-worker-still-holding-it"
    )


def test_a_second_worker_cannot_steal_an_adopted_lease():
    state.TASKS["t1"] = {"id": "t1", "status": TaskStatus.RUNNING}
    owned("t1", "first")

    with pytest.raises(HTTPException) as caught:
        owned("t1", "second")

    assert caught.value.status_code == 409
