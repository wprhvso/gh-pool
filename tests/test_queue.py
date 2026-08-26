import asyncio

import pytest

from gh_pool.core.config import settings
from gh_pool.server.pool import state as pool_state
from tests.conftest import as_client, as_worker, submit, take


async def test_a_submitted_task_is_leased_with_its_payload(client):
    tid = await submit(client, code="result = 2 + 2", entry=None)

    leased = await take(client)

    assert leased["task_id"] == tid
    assert leased["payload"]["code"] == "result = 2 + 2"
    assert leased["lease_token"]
    assert pool_state.current.tasks[tid]["status"] == "running"


async def test_tasks_are_handed_out_in_the_order_they_arrived(client):
    first = await submit(client, code="result = 1")
    second = await submit(client, code="result = 2")

    assert (await take(client, "a"))["task_id"] == first
    assert (await take(client, "b"))["task_id"] == second


async def test_an_empty_queue_answers_no_content_once_the_wait_is_over(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "lease_wait", 0.05)

    answer = await client.post(
        "/v1/lease", json={"worker_id": "w1"}, headers=as_worker()
    )

    assert answer.status_code == 204


async def test_a_waiting_worker_is_woken_by_a_new_task(client, monkeypatch):
    monkeypatch.setattr(settings, "lease_wait", 5.0)
    waiting = asyncio.ensure_future(take(client))
    await asyncio.sleep(0.05)

    tid = await submit(client, code="result = 1")

    leased = await asyncio.wait_for(waiting, timeout=2)
    assert leased["task_id"] == tid


async def test_a_lease_is_only_good_for_the_worker_that_holds_it(client):
    tid = await submit(client)
    await take(client)

    answer = await client.post(
        f"/v1/tasks/{tid}/heartbeat",
        headers=as_worker({"X-Lease-Token": "not-the-token"}),
    )

    assert answer.status_code == 409


async def test_a_heartbeat_keeps_the_task_and_its_worker_alive(client):
    tid = await submit(client)
    leased = await take(client)
    pool_state.current.tasks[tid]["heartbeat_at"] = 0

    answer = await client.post(
        f"/v1/tasks/{tid}/heartbeat",
        headers=as_worker({"X-Lease-Token": leased["lease_token"]}),
    )

    assert answer.json() == {"cancel": False}
    assert pool_state.current.tasks[tid]["heartbeat_at"] > 0


async def test_completing_a_task_frees_its_worker(client):
    tid = await submit(client)
    leased = await take(client)

    answer = await client.post(
        f"/v1/tasks/{tid}/complete",
        json={"status": "done"},
        headers=as_worker({"X-Lease-Token": leased["lease_token"]}),
    )

    assert answer.json()["status"] == "done"
    assert pool_state.current.workers["w1"]["task_id"] is None


async def test_a_second_completion_is_refused_because_the_lease_is_gone(client):
    tid = await submit(client)
    leased = await take(client)
    headers = as_worker({"X-Lease-Token": leased["lease_token"]})
    await client.post(
        f"/v1/tasks/{tid}/complete", json={"status": "done"}, headers=headers
    )

    again = await client.post(
        f"/v1/tasks/{tid}/complete", json={"status": "failed"}, headers=headers
    )

    assert again.status_code == 409


@pytest.mark.parametrize("status", ["running", "queued", "nonsense"])
async def test_a_task_cannot_be_completed_into_a_status_that_is_not_terminal(
    client, status
):
    tid = await submit(client)
    leased = await take(client)

    answer = await client.post(
        f"/v1/tasks/{tid}/complete",
        json={"status": status},
        headers=as_worker({"X-Lease-Token": leased["lease_token"]}),
    )

    assert answer.status_code == 400


async def test_cancelling_a_task_nobody_took_ends_it_outright(client):
    tid = await submit(client)

    answer = await client.post(f"/v1/tasks/{tid}/cancel", headers=as_client())

    assert answer.json()["status"] == "cancelled"
    assert pool_state.current.tasks[tid]["status"] == "cancelled"


async def test_cancelling_a_running_task_only_asks_its_worker_to_stop(client):
    tid = await submit(client)
    leased = await take(client)

    answer = await client.post(f"/v1/tasks/{tid}/cancel", headers=as_client())

    assert answer.json() == {"status": "running", "cancel_requested": True}
    beat = await client.post(
        f"/v1/tasks/{tid}/heartbeat",
        headers=as_worker({"X-Lease-Token": leased["lease_token"]}),
    )
    assert beat.json() == {"cancel": True}


async def test_a_cancelled_task_is_skipped_when_a_worker_asks_for_work(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "lease_wait", 0.05)
    skipped = await submit(client)
    wanted = await submit(client)
    await client.post(f"/v1/tasks/{skipped}/cancel", headers=as_client())

    assert (await take(client))["task_id"] == wanted


async def test_a_retry_is_a_new_task_that_remembers_its_parent(client):
    tid = await submit(client, code="result = 7")
    leased = await take(client)
    await client.post(
        f"/v1/tasks/{tid}/complete",
        json={"status": "failed"},
        headers=as_worker({"X-Lease-Token": leased["lease_token"]}),
    )

    answer = await client.post(f"/v1/tasks/{tid}/retry", headers=as_client())

    child = answer.json()["task_id"]
    assert answer.json()["parent_id"] == tid
    assert pool_state.current.tasks[child]["status"] == "pending"
    assert pool_state.current.tasks[child]["payload"]["code"] == "result = 7"


async def test_a_busy_worker_returns_to_the_list_after_a_restart(client, blank):
    tid = await submit(client)
    task = await take(client)
    pool_state.current.workers.clear()

    answer = await client.post(
        f"/v1/tasks/{tid}/heartbeat",
        headers={**as_worker(), "X-Lease-Token": task["lease_token"]},
    )

    assert answer.status_code == 200
    assert "w1" in pool_state.current.workers
    assert pool_state.current.workers["w1"]["task_id"] == tid
