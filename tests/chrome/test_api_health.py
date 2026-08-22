import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from gh_chrome_server import api_health


class FakeDatabase:
    def __init__(self, failure: Exception | None = None) -> None:
        self.probes = 0
        self._failure = failure

    async def probe(self) -> None:
        self.probes += 1
        if self._failure is not None:
            raise self._failure


@pytest.fixture
def probe():
    def build(database: FakeDatabase) -> TestClient:
        app = FastAPI()
        app.include_router(api_health.router)
        app.state.db = database
        return TestClient(app)

    return build


def test_a_server_whose_database_answers_is_healthy(probe):
    database = FakeDatabase()

    with probe(database) as client:
        answer = client.get("/healthz")

    assert answer.status_code == 200
    assert answer.json() == {"status": "ok"}
    assert database.probes == 1


@pytest.mark.parametrize(
    "failure",
    [OSError("connection refused"), TimeoutError("the pool never handed one over")],
)
def test_a_server_whose_database_went_away_says_so(probe, failure: Exception):
    with probe(FakeDatabase(failure)) as client:
        answer = client.get("/healthz")

    assert answer.status_code == 503
    assert answer.json() == {"status": "down"}


def test_the_probe_asks_for_no_credentials(probe):
    with probe(FakeDatabase()) as client:
        answer = client.get("/healthz", headers={"Authorization": "Bearer nonsense"})

    assert answer.status_code == 200
