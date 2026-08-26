import asyncio

import pytest
from fastapi import FastAPI

from gh_pool.server import app as app_mod


class Recorder:
    def __init__(self) -> None:
        self.order: list[str] = []


@pytest.fixture
def told(monkeypatch, tmp_path):
    seen = Recorder()

    class FakeDb:
        async def open(self) -> None: ...
        async def close(self) -> None:
            seen.order.append("db closed")

    class FakeChore:
        def __init__(self, name: str) -> None:
            self.name = name

        async def start(self) -> None: ...
        async def stop(self) -> None:
            seen.order.append(f"{self.name} stopped")

    async def keeper() -> None:
        try:
            await asyncio.sleep(3600)
        finally:
            seen.order.append("keeper stopped")

    async def flush() -> bool:
        seen.order.append("flushed")
        return True

    monkeypatch.setattr(app_mod.storage, "ensure_dirs", lambda: None)
    monkeypatch.setattr(app_mod.pool_state, "boot", lambda: None)
    monkeypatch.setattr(app_mod.migrate, "upgrade", _nothing)
    monkeypatch.setattr(app_mod, "Database", lambda *_a, **_kw: FakeDb())
    monkeypatch.setattr(app_mod, "Events", lambda *_a: None)
    monkeypatch.setattr(app_mod, "Sessions", lambda *_a: None)
    monkeypatch.setattr(app_mod, "Watchdog", lambda *_a: FakeChore("watchdog"))
    monkeypatch.setattr(app_mod, "Cleaner", lambda *_a: FakeChore("cleaner"))
    monkeypatch.setattr(app_mod, "keeper", keeper)
    monkeypatch.setattr(app_mod, "flush", flush)
    monkeypatch.setattr(app_mod, "dispose", _nothing)
    return seen


async def _nothing(*_args, **_kwargs) -> None:
    return None


async def test_nothing_writes_after_the_database_is_closed(told):
    async with app_mod.lifespan(FastAPI()):
        await asyncio.sleep(0)

    assert told.order.index("flushed") < told.order.index("db closed")
    assert told.order.index("keeper stopped") < told.order.index("flushed")


async def test_every_background_chore_is_stopped_before_the_last_flush(told):
    async with app_mod.lifespan(FastAPI()):
        await asyncio.sleep(0)

    last_stop = max(
        told.order.index(name)
        for name in ("keeper stopped", "watchdog stopped", "cleaner stopped")
    )
    assert last_stop < told.order.index("flushed")
