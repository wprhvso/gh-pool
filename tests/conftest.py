import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pool-tests-"))

import httpx
import pytest

from gh_pool import db, server

WORKER = "dev-worker"
CLIENT = "dev-client"


class FakeDb:
    def __init__(self):
        self.saved = []
        self.rows = {}
        self.pending = []
        self.broken = False

    async def setup(self):
        self._check()

    async def save(self, _model, rows):
        self._check()
        self.saved.extend(rows)

    def _check(self):
        if self.broken:
            raise RuntimeError("no database")

    async def fetch(self, _model, value):
        self._check()
        return self.rows.get(value)

    async def unfinished(self):
        self._check()
        return self.pending

    async def tasks(self, status=None, _limit=100):
        self._check()
        return [r for r in self.rows.values() if not status or r["status"] == status]

    async def artifacts(self, _prefix="", _limit=100):
        self._check()
        return []

    async def drop(self, _model, _value):
        self._check()


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeDb()
    for name in ("setup", "save", "fetch", "unfinished", "tasks", "artifacts", "drop"):
        monkeypatch.setattr(db, name, getattr(fake, name))
    return fake


@pytest.fixture
def blank(monkeypatch, tmp_path, fake_db):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(server, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(server, "LEASE_WAIT", 0.05)
    (tmp_path / "data").mkdir()
    (tmp_path / "blobs").mkdir()
    server.TASKS.clear()
    server.QUEUE.clear()
    server.WORKERS.clear()
    server.BLOBS.clear()
    server.DIRTY.clear()
    server.DIRTY_BLOBS.clear()
    server.event_locks.clear()
    server.new_task = __import__("asyncio").Event()
    server.state["db"] = False
    yield fake_db
    server.TASKS.clear()
    server.QUEUE.clear()
    server.WORKERS.clear()


@pytest.fixture
async def client(blank):
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://pool") as c:
        yield c


def as_client(headers=None):
    return {"Authorization": f"Bearer {CLIENT}", **(headers or {})}


def as_worker(headers=None):
    return {"Authorization": f"Bearer {WORKER}", **(headers or {})}


async def submit(client, **payload):
    answer = await client.post(
        "/v1/tasks",
        json={"type": "python", "payload": payload or {"code": "result = 1"}},
        headers=as_client(),
    )
    answer.raise_for_status()
    return answer.json()["task_id"]


async def take(client, worker_id="w1"):
    answer = await client.post(
        "/v1/lease", json={"worker_id": worker_id}, headers=as_worker()
    )
    if answer.status_code == 204:
        raise AssertionError("the queue had nothing to hand out")
    return answer.json()
