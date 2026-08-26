import pytest

from gh_pool.server.tasks import MAX_PAGE
from tests.conftest import as_client, as_worker, submit


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE + 1, 10_000_000])
async def test_a_page_outside_the_bounds_is_refused(client, blank, limit: int):
    answer = await client.get("/v1/tasks", params={"limit": limit}, headers=as_client())

    assert answer.status_code == 422


async def test_the_biggest_page_the_fleet_asks_for_still_fits(client, blank):
    answer = await client.get("/v1/tasks", params={"limit": 1000}, headers=as_client())

    assert answer.status_code == 200


async def test_a_page_still_returns_what_was_submitted(client, blank):
    await submit(client)

    answer = await client.get("/v1/tasks", params={"limit": 5}, headers=as_client())

    assert answer.status_code == 200
    assert len(answer.json()) == 1


@pytest.mark.parametrize("limit", [0, MAX_PAGE + 1])
async def test_the_artifact_page_is_bounded_too(client, blank, limit: int):
    answer = await client.get(
        "/v1/artifacts", params={"limit": limit}, headers=as_worker()
    )

    assert answer.status_code == 422


async def test_a_listed_task_still_carries_what_the_fleet_matches_on(client, blank):
    await submit(client, code="result = 1", kwargs={"slug": "owner/app"})

    row = (await client.get("/v1/tasks", headers=as_client())).json()[0]

    assert row["payload"]["kwargs"] == {"slug": "owner/app"}
    assert set(row) >= {"id", "type", "status", "created_at", "event_size"}
