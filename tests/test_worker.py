import asyncio
import subprocess
import sys

import httpx
import pytest
from conftest import as_client, submit, take

from pool import server, worker


@pytest.fixture
def nap(monkeypatch):
    async def instantly(_seconds):
        await real_sleep(0)

    real_sleep = asyncio.sleep
    monkeypatch.setattr(worker.asyncio, "sleep", instantly)


@pytest.fixture
def spool(tmp_path):
    s = worker.Spool(tmp_path / "spool.events")
    yield s
    s.close()


@pytest.fixture
async def wired(blank, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "SERVER", "http://pool")
    monkeypatch.setattr(worker, "TOKEN", "dev-worker")
    monkeypatch.setattr(worker, "SPOOL_DIR", tmp_path)
    monkeypatch.setattr(worker, "HEARTBEAT", 0.05)
    monkeypatch.setattr(worker, "KILL_GRACE", 10.0)
    monkeypatch.setattr(worker, "_current", None)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://pool") as c:
        yield c


async def run_one(client, code, worker_id="w1"):
    tid = await submit(client, code=code)
    lease = await take(client, worker_id)
    status = await worker._run(client, lease)
    return tid, status


async def events_of(client, tid):
    answer = await client.get(f"/v1/tasks/{tid}/events", headers=as_client())
    return answer.content.decode()


def test_a_spool_keeps_what_it_is_given(spool):
    spool.write(b"hello ")
    spool.write(b"world")

    assert spool.read_at(0, 100) == b"hello world"
    assert spool.size == 11


def test_a_spool_that_resumes_reads_from_the_offset_it_was_handed(tmp_path):
    spool = worker.Spool(tmp_path / "resumed.events", base=500)
    try:
        spool.write(b"the rest")

        assert spool.sent == 500
        assert spool.size == 508
        assert spool.read_at(500, 100) == b"the rest"
    finally:
        spool.close()


def test_a_spool_stops_growing_once_it_is_full(spool, monkeypatch):
    monkeypatch.setattr(worker, "SPOOL_CAP", 4)
    spool.write(b"12345")
    spool.write(b"dropped")
    spool.write(b"also dropped")

    assert spool.read_at(0, 100) == b"12345"
    assert spool.dropped == 19


def test_a_spool_admits_what_it_dropped_when_it_has_room_again(spool, monkeypatch):
    monkeypatch.setattr(worker, "SPOOL_CAP", 4)
    spool.write(b"12345")
    spool.write(b"gone")
    spool.sent = spool.size
    spool.write(b"back")

    assert b"dropped 4 bytes locally" in spool.read_at(0, 200)


def test_a_stopped_spool_takes_nothing_more(spool):
    spool.stopped = True
    spool.write(b"too late")

    assert spool.size == 0


async def test_a_network_error_is_retried_until_it_works(nap):
    tries = []

    class Flaky:
        async def request(self, _method, url, **_kw):
            tries.append(url)
            if len(tries) < 3:
                raise httpx.ConnectError("no route")
            return httpx.Response(200, json={"ok": True})

    r = await worker.req(Flaky(), "GET", "/healthz")

    assert r.status_code == 200
    assert len(tries) == 3


async def test_a_server_error_is_retried(nap):
    codes = [503, 500, 200]

    class Sick:
        async def request(self, _method, _url, **_kw):
            return httpx.Response(codes.pop(0), json={})

    r = await worker.req(Sick(), "GET", "/healthz")

    assert r.status_code == 200
    assert codes == []


async def test_a_rejection_is_permanent():
    class Rude:
        async def request(self, _method, _url, **_kw):
            return httpx.Response(401, text="bad worker token")

    with pytest.raises(worker.Permanent, match="401"):
        await worker.req(Rude(), "GET", "/healthz")


@pytest.mark.parametrize("code", [204, 409])
async def test_an_answer_the_caller_expects_comes_straight_back(code):
    class Terse:
        async def request(self, _method, _url, **_kw):
            return httpx.Response(code, json={})

    assert (await worker.req(Terse(), "GET", "/x")).status_code == code


async def test_a_task_that_runs_through_is_reported_as_done(wired):
    tid, status = await run_one(wired, "print('hello from the task')\nresult = 4")

    assert status == "done"
    body = (await wired.get(f"/v1/tasks/{tid}", headers=as_client())).json()
    assert body["status"] == "done"
    assert body["error"] is None
    text = await events_of(wired, tid)
    assert "hello from the task" in text
    assert '"kind": "result"' in text
    assert '"value": 4' in text


async def test_a_task_that_raises_is_reported_as_failed(wired):
    tid, status = await run_one(wired, "raise ValueError('nope')")

    assert status == "failed"
    body = (await wired.get(f"/v1/tasks/{tid}", headers=as_client())).json()
    assert body["status"] == "failed"
    assert body["error"] == "exit code 1"
    assert "ValueError" in await events_of(wired, tid)


async def test_an_unknown_type_fails_the_task_rather_than_the_worker(wired):
    answer = await wired.post(
        "/v1/tasks", json={"type": "nonsense", "payload": {}}, headers=as_client()
    )
    tid = answer.json()["task_id"]
    lease = await take(wired)

    assert await worker._run(wired, lease) == "failed"
    assert "unknown task type: nonsense" in await events_of(wired, tid)


async def test_a_cancelled_task_is_stopped_and_says_so(wired):
    tid = await submit(wired, code="import time\ntime.sleep(60)")
    lease = await take(wired)
    await wired.post(f"/v1/tasks/{tid}/cancel", headers=as_client())

    assert await worker._run(wired, lease) == "cancelled"
    body = (await wired.get(f"/v1/tasks/{tid}", headers=as_client())).json()
    assert body["status"] == "cancelled"
    assert body["error"] == "cancelled by client"


async def test_a_lease_the_server_forgot_ends_the_task_as_lost(wired):
    tid = await submit(wired, code="import time\ntime.sleep(60)")
    lease = await take(wired)
    server.TASKS[tid]["lease_token"] = "someone else"

    assert await worker._run(wired, lease) == "lost"
    assert server.TASKS[tid]["status"] == "running"


async def test_the_worker_leaves_no_spool_behind(wired, tmp_path):
    await run_one(wired, "result = 1")

    assert list(tmp_path.glob("pool-*")) == []


async def test_the_events_the_task_printed_arrive_in_order(wired):
    tid, status = await run_one(
        wired, "for i in range(200):\n    print(f'line {i}')\nresult = 'ok'"
    )

    assert status == "done"
    lines = [
        line for line in (await events_of(wired, tid)).splitlines() if "line " in line
    ]
    assert lines == [f"line {i}" for i in range(200)]


async def test_a_worker_that_reached_its_age_stops_asking_for_work(wired, monkeypatch):
    monkeypatch.setattr(worker, "MAX_AGE", 0.01)
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kw: _Handed(wired))

    await asyncio.wait_for(worker.loop(), timeout=5)


async def test_the_loop_runs_the_task_the_server_hands_it(wired, monkeypatch):
    tid = await submit(wired, code="result = 'from the loop'")
    monkeypatch.setattr(worker, "MAX_AGE", 0)
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kw: _Handed(wired))

    async def stop_after_one():
        while server.TASKS.get(tid, {}).get("status") not in server.FINISHED:
            await asyncio.sleep(0.05)

    runner = asyncio.create_task(worker.loop())
    await asyncio.wait_for(stop_after_one(), timeout=30)
    runner.cancel()

    assert "from the loop" in await events_of(wired, tid)


class _Handed:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *_):
        return False


def exec_child(ttype, payload, code):
    proc = subprocess.run(
        [sys.executable, "-m", "pool.worker", "exec", ttype, str(payload)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == code, proc.stdout + proc.stderr
    return proc.stdout


def test_a_type_nobody_registered_exits_with_its_own_code(tmp_path):
    payload = tmp_path / "p.json"
    payload.write_text("{}")

    assert "unknown task type" in exec_child("mystery", payload, 2)


def test_a_child_that_finishes_reports_its_result(tmp_path):
    payload = tmp_path / "p.json"
    payload.write_text('{"code": "result = [1, 2]"}')

    assert '"value": [1, 2]' in exec_child("python", payload, 0)


def test_a_child_that_breaks_exits_unhappy(tmp_path):
    payload = tmp_path / "p.json"
    payload.write_text('{"code": "1 / 0"}')

    assert '"kind": "error"' in exec_child("python", payload, 1)
