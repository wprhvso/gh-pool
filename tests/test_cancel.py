from gh_pool.db import tasks as db
from gh_pool.server.pool import state
from gh_pool.status import TaskStatus
from tests.conftest import as_client, submit, take


async def test_cancelling_a_running_task_is_written_down(client, blank):
    tid = await submit(client)
    await take(client)

    answer = await client.post(f"/v1/tasks/{tid}/cancel", headers=as_client())

    assert answer.json() == {"status": "running", "cancel_requested": True}
    assert tid in state.current.dirty


async def test_the_promise_survives_what_a_restart_would_flush(client, blank):
    tid = await submit(client)
    await take(client)
    await client.post(f"/v1/tasks/{tid}/cancel", headers=as_client())

    row = {c: state.current.tasks[tid].get(c) for c in db.TASK_COLUMNS}

    assert row["cancel_requested"] is True
    assert row["status"] == TaskStatus.RUNNING


async def test_a_retry_does_not_inherit_the_cancellation(client, blank):
    tid = await submit(client)
    await take(client)
    await client.post(f"/v1/tasks/{tid}/cancel", headers=as_client())

    nid = (await client.post(f"/v1/tasks/{tid}/retry", headers=as_client())).json()[
        "task_id"
    ]

    assert state.current.tasks[nid]["cancel_requested"] is False


async def test_a_fresh_task_carries_the_column(client, blank):
    tid = await submit(client)

    assert state.current.tasks[tid]["cancel_requested"] is False
    answer = await client.get(f"/v1/tasks/{tid}", headers=as_client())
    assert answer.json()["cancel_requested"] is False
