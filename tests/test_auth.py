import pytest

from tests.conftest import as_client, as_worker, submit, take


async def test_a_client_cannot_lease_work(client):
    answer = await client.post(
        "/v1/lease", json={"worker_id": "w1"}, headers=as_client()
    )

    assert answer.status_code == 401


async def test_a_worker_cannot_submit_work(client):
    answer = await client.post(
        "/v1/tasks", json={"type": "python", "payload": {}}, headers=as_worker()
    )

    assert answer.status_code == 401


async def test_a_worker_cannot_read_the_worker_listing(client):
    answer = await client.get("/v1/workers", headers=as_worker())

    assert answer.status_code == 401


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
async def test_an_unknown_token_reaches_nothing(client, headers):
    answer = await client.get("/v1/tasks", headers=headers)

    assert answer.status_code == 401


async def test_reading_a_task_belongs_to_the_client_alone(client):
    tid = await submit(client)

    assert (
        await client.get(f"/v1/tasks/{tid}", headers=as_client())
    ).status_code == 200
    assert (
        await client.get(f"/v1/tasks/{tid}", headers=as_worker())
    ).status_code == 401


async def test_either_token_reaches_the_artifacts(client):
    for headers in (as_client(), as_worker()):
        answer = await client.get("/v1/artifacts", headers=headers)
        assert answer.status_code == 200


async def test_a_lease_needs_a_worker_id(client):
    answer = await client.post("/v1/lease", json={}, headers=as_worker())

    assert answer.status_code == 400


async def test_a_task_needs_a_type(client):
    answer = await client.post("/v1/tasks", json={"payload": {}}, headers=as_client())

    assert answer.status_code == 400


async def test_healthz_is_open(client):
    answer = await client.get("/healthz")

    assert answer.status_code == 200


async def test_the_worker_listing_shows_what_each_worker_holds(client):
    tid = await submit(client)
    await take(client, "busy")

    answer = await client.get("/v1/workers", headers=as_client())

    rows = {row["id"]: row for row in answer.json()}
    assert rows["busy"]["task_id"] == tid
