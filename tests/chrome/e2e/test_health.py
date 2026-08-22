from collections.abc import AsyncIterator

import httpx
import pytest
from psycopg.conninfo import conninfo_to_dict

from tests.chrome.e2e.stack import Cluster, Server


@pytest.fixture
async def anonymous(server: Server) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, timeout=30.0) as client:
        yield client


async def test_a_server_that_can_reach_its_database_is_healthy(
    anonymous: httpx.AsyncClient,
):
    answer = await anonymous.get("/healthz")

    assert answer.status_code == 200
    assert answer.json() == {"status": "ok"}


async def test_a_server_whose_database_went_away_is_taken_out_of_service(
    anonymous: httpx.AsyncClient, cluster: Cluster, database: str
):
    cluster.drop(str(conninfo_to_dict(database)["dbname"]))

    answer = await anonymous.get("/healthz")

    assert answer.status_code == 503
    assert answer.json() == {"status": "down"}
