import pytest

from gh_pool.core.config import settings
from gh_pool.server.pool import state
from gh_pool.server.pool.keeper import _rest
from gh_pool.server.pool.store import flush, overloaded, pending
from tests.conftest import as_client, submit


class Refusing:
    async def save(self, *_args, **_kwargs) -> None:
        raise RuntimeError("the database is down")


@pytest.fixture(autouse=True)
def clean():
    state.reset()
    yield
    state.reset()


def test_the_rest_between_flushes_grows_and_then_stops_growing():
    assert _rest(0) == settings.flush_every
    rests = [_rest(n) for n in range(1, 20)]
    assert rests == sorted(rests)
    assert rests[-1] == settings.flush_backoff_cap
    assert max(rests) <= settings.flush_backoff_cap


async def test_a_failing_flush_keeps_the_work_and_says_it_failed(monkeypatch):
    monkeypatch.setattr("gh_pool.server.pool.store.db", Refusing())
    state.current.tasks["t1"] = {"id": "t1", "status": "pending"}
    state.current.dirty.add("t1")

    assert await flush() is False
    assert pending() == 1


async def test_a_quiet_pool_reports_a_successful_flush():
    assert await flush() is True


def test_the_pool_reports_overload_only_past_the_cap(monkeypatch):
    monkeypatch.setattr(settings, "max_pending_writes", 3)
    assert not overloaded()

    state.current.dirty.update({"a", "b"})
    assert not overloaded()

    state.current.dirty.add("c")
    assert overloaded()


async def test_a_pool_that_cannot_write_stops_taking_new_work(
    client, blank, monkeypatch
):
    assert await submit(client) is not None

    monkeypatch.setattr(settings, "max_pending_writes", 1)
    answer = await client.post(
        "/v1/tasks",
        json={"type": "python", "payload": {}},
        headers=as_client(),
    )

    assert answer.status_code == 503
    assert "unflushed" in answer.json()["detail"]


async def test_the_work_it_already_took_is_still_served(client, blank, monkeypatch):
    tid = await submit(client)
    monkeypatch.setattr(settings, "max_pending_writes", 1)

    answer = await client.get(f"/v1/tasks/{tid}", headers=as_client())

    assert answer.status_code == 200
    assert answer.json()["status"] == "pending"
