import asyncio
import time

from gh_pool.core.config import settings
from gh_pool.server.pool import keeper as pool_keeper
from gh_pool.server.pool import state as pool_state
from gh_pool.server.pool import store as pool_store
from tests.conftest import as_client, as_worker, submit, take


async def until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return False


async def reaping(monkeypatch, **overrides):
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "flush_every", 0.01)
    return asyncio.ensure_future(pool_keeper.keeper())


async def test_a_task_whose_worker_went_quiet_is_declared_lost(
    client, blank, monkeypatch
):
    tid = await submit(client)
    await take(client)
    pool_state.TASKS[tid]["heartbeat_at"] = time.time() - 100
    reaper = await reaping(monkeypatch, lost_after=1.0)

    try:
        assert await until(lambda: any(r["id"] == tid for r in blank.saved))
    finally:
        reaper.cancel()

    written = next(r for r in blank.saved if r["id"] == tid)
    assert written["status"] == "lost"
    assert written["error"] == "worker gone"


async def test_a_task_that_keeps_beating_is_left_alone(client, monkeypatch):
    tid = await submit(client)
    await take(client)
    reaper = await reaping(monkeypatch, lost_after=60.0)

    try:
        await asyncio.sleep(0.1)
    finally:
        reaper.cancel()

    assert pool_state.TASKS[tid]["status"] == "running"


async def test_a_worker_that_stopped_asking_drops_off_the_listing(client, monkeypatch):
    await client.post(
        "/v1/lease",
        json={"worker_id": "w1"},
        headers={"Authorization": "Bearer dev-worker"},
    )
    pool_state.WORKERS["w1"]["seen_at"] = time.time() - 100
    reaper = await reaping(monkeypatch, worker_stale=1.0)

    try:
        assert await until(lambda: "w1" not in pool_state.WORKERS)
    finally:
        reaper.cancel()


async def test_work_left_over_from_a_previous_server_is_picked_up(client, blank):
    blank.pending = [
        {"id": "waiting", "status": "pending", "payload": {}, "type": "python"},
        {"id": "gone", "status": "running", "payload": {}, "type": "python"},
    ]

    await pool_store.recover()

    assert list(pool_state.QUEUE) == ["waiting"]
    assert pool_state.TASKS["gone"]["status"] == "running"


async def test_a_finished_task_leaves_memory_once_it_is_written_down(client):
    tid = await submit(client)
    leased = await take(client)
    await client.post(
        f"/v1/tasks/{tid}/complete",
        json={"status": "done"},
        headers={
            "Authorization": "Bearer dev-worker",
            "X-Lease-Token": leased["lease_token"],
        },
    )

    await pool_store.flush()

    assert tid not in pool_state.TASKS


async def test_nothing_is_forgotten_while_the_database_refuses(client, blank):
    tid = await submit(client)
    blank.broken = True

    await pool_store.flush()

    assert tid in pool_state.DIRTY
    assert pool_state.health["db"] is False


async def test_health_says_when_the_server_came_up(client):
    answer = (await client.get("/healthz")).json()

    assert answer["ok"] is True
    assert answer["started_at"] <= time.time()
    assert answer["uptime"] >= 0


async def test_health_counts_the_queue_and_the_workers(client):
    await submit(client)
    await submit(client)
    await take(client)

    answer = (await client.get("/healthz")).json()

    assert answer["queue"] == 1
    assert answer["workers"] == 1
    assert answer["tasks"] == {"pending": 1, "running": 1}


async def test_tasks_can_be_listed_newest_first(client):
    first = await submit(client)
    second = await submit(client)

    answer = await client.get("/v1/tasks", headers=as_client())

    assert [row["id"] for row in answer.json()][:2] == [second, first]


async def test_a_worker_reports_how_long_it_has_been_serving(client, blank):
    await submit(client)
    await take(client)

    listing = (await client.get("/v1/workers", headers=as_client())).json()

    assert [w["serving_for"] >= 0 for w in listing] == [True]


async def test_taking_another_task_does_not_reset_the_serving_clock(client, blank):
    await submit(client)
    await take(client)
    born = pool_state.WORKERS["w1"]["first_seen"]
    await submit(client)
    await take(client)

    assert pool_state.WORKERS["w1"]["first_seen"] == born


async def test_listing_tasks_writes_nothing_to_disk(client, blank):
    blank.rows = {}
    blank.pending = []
    tid = await submit(client)
    pool_state.TASKS.clear()
    blank.rows[tid] = {
        "id": tid,
        "type": "python",
        "payload": {},
        "status": "done",
        "worker_id": None,
        "error": None,
        "parent_id": None,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
    }

    await client.get(f"/v1/tasks/{tid}", headers=as_client())

    assert list(settings.data_dir.iterdir()) == []


async def test_a_pending_task_the_server_forgot_is_still_cancelled(client, blank):
    row = {
        "id": "old",
        "type": "python",
        "payload": {},
        "status": "pending",
        "worker_id": None,
        "error": None,
        "parent_id": None,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
    }
    blank.rows["old"] = row

    answer = await client.post("/v1/tasks/old/cancel", headers=as_client())
    await pool_store.flush()

    assert answer.json()["status"] == "cancelled"
    assert [r["status"] for r in blank.saved if r["id"] == "old"] == ["cancelled"]


async def test_a_listing_still_answers_while_the_database_is_away(client, blank):
    tid = await submit(client)
    blank.broken = True

    answer = await client.get("/v1/tasks", headers=as_client())

    assert answer.status_code == 200
    assert [t["id"] for t in answer.json()] == [tid]


async def test_a_task_the_server_holds_is_readable_while_the_database_is_away(
    client, blank
):
    tid = await submit(client)
    blank.broken = True

    answer = await client.get(f"/v1/tasks/{tid}", headers=as_client())

    assert answer.status_code == 200
    assert answer.json()["status"] == "pending"


async def test_a_task_nobody_remembers_is_still_a_plain_absence(client, blank):
    blank.broken = True

    assert (await client.get("/v1/tasks/ghost", headers=as_client())).status_code == 404


async def test_artifacts_can_be_listed_and_dropped_while_the_database_is_away(
    client, blank
):
    await client.put("/v1/artifacts/k", content=b"x", headers=as_worker())
    blank.broken = True

    listing = await client.get("/v1/artifacts", headers=as_client())
    removed = await client.delete("/v1/artifacts/k", headers=as_client())

    assert [b["key"] for b in listing.json()] == ["k"]
    assert removed.json() == {"ok": True}


async def test_a_running_task_survives_the_server_it_was_started_on(client, blank):
    blank.pending = [
        {
            "id": "busy",
            "status": "running",
            "payload": {},
            "type": "python",
            "worker_id": "w1",
        }
    ]

    await pool_store.recover()

    answer = await client.post(
        "/v1/tasks/busy/heartbeat",
        headers={**as_worker(), "X-Lease-Token": "token-from-before-the-restart"},
    )

    assert answer.status_code == 200
    assert pool_state.TASKS["busy"]["status"] == "running"
    assert pool_state.TASKS["busy"]["lease_token"] == "token-from-before-the-restart"


async def test_a_recovered_task_gets_a_fresh_grace_period(client, blank):
    blank.pending = [
        {
            "id": "busy",
            "status": "running",
            "payload": {},
            "type": "python",
            "worker_id": "w1",
        }
    ]

    await pool_store.recover()

    assert pool_state.TASKS["busy"]["heartbeat_at"] > time.time() - 5


async def test_a_recovered_task_whose_worker_never_returns_is_declared_lost(
    client, blank, monkeypatch
):
    blank.pending = [
        {
            "id": "busy",
            "status": "running",
            "payload": {},
            "type": "python",
            "worker_id": "w1",
        }
    ]
    await pool_store.recover()
    pool_state.TASKS["busy"]["heartbeat_at"] = time.time() - 1000

    task = await reaping(monkeypatch, lost_after=1)
    ok = await until(
        lambda: any(r["id"] == "busy" and r["status"] == "lost" for r in blank.saved)
    )
    task.cancel()

    assert ok


async def test_a_second_token_cannot_steal_a_recovered_task(client, blank):
    blank.pending = [
        {
            "id": "busy",
            "status": "running",
            "payload": {},
            "type": "python",
            "worker_id": "w1",
        }
    ]
    await pool_store.recover()

    first = await client.post(
        "/v1/tasks/busy/heartbeat",
        headers={**as_worker(), "X-Lease-Token": "mine"},
    )
    second = await client.post(
        "/v1/tasks/busy/heartbeat",
        headers={**as_worker(), "X-Lease-Token": "someone-else"},
    )

    assert first.status_code == 200
    assert second.status_code == 409
