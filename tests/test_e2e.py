import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from pool.sdk import Failed, Pool, Remote

WORKER_TOKEN = "e2e-worker"
CLIENT_TOKEN = "e2e-client"
BOOT = 60.0


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Process:
    def __init__(self, args, env, logfile):
        self.logfile = logfile
        self.handle = logfile.open("w")
        self.proc = subprocess.Popen(
            [sys.executable, *args],
            env={**os.environ, **env},
            stdout=self.handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=15)
        self.handle.close()
        return self.logfile.read_text()[-4000:]


def spawn(args, env, logfile):
    return Process(args, env, logfile)


def wait_for_health(url, proc):
    deadline = time.monotonic() + BOOT
    while time.monotonic() < deadline:
        if not proc.alive():
            raise AssertionError(f"server died: {proc.stop()}")
        try:
            if httpx.get(f"{url}/healthz", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise AssertionError(f"server never came up: {proc.stop()}")


@pytest.fixture(scope="session")
def live(tmp_path_factory):
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    root = tmp_path_factory.mktemp("e2e")
    server_env = {
        "DATA_DIR": str(root / "data"),
        "BLOB_DIR": str(root / "blobs"),
        "WORKER_TOKEN": WORKER_TOKEN,
        "CLIENT_TOKEN": CLIENT_TOKEN,
        "LEASE_WAIT": "2",
        "FLUSH_EVERY": "0.5",
        "LOST_AFTER": "20",
        "DATABASE_URL": "postgresql+asyncpg://pool:pool@127.0.0.1:1/pool",
        "OTEL_SDK_DISABLED": "true",
    }
    server = spawn(
        [
            "-c",
            (
                "import uvicorn\nfrom pool.server import app\n"
                f"uvicorn.run(app, host='127.0.0.1', port={port},"
                " log_level='warning')"
            ),
        ],
        server_env,
        root / "server.log",
    )
    try:
        wait_for_health(url, server)
        (root / "spool").mkdir()
        worker = spawn(
            ["-m", "pool.worker"],
            {
                "POOL_SERVER": url,
                "POOL_TOKEN": WORKER_TOKEN,
                "WORKER_ID": "e2e-worker-1",
                "SPOOL_DIR": str(root / "spool"),
                "POOL_DEPS": str(root / "deps"),
                "OTEL_SDK_DISABLED": "true",
            },
            root / "worker.log",
        )
        try:
            yield url
        finally:
            worker.stop()
    finally:
        server.stop()


@pytest.fixture
def pool(live):
    with Pool(live, token=CLIENT_TOKEN, timeout=60.0) as p:
        yield p


def cli(live, *args, check=True):
    proc = subprocess.run(
        [sys.executable, "-m", "pool.cli", *args],
        env={
            **os.environ,
            "POOL_SERVER": live,
            "POOL_CLIENT_TOKEN": CLIENT_TOKEN,
            "OTEL_SDK_DISABLED": "true",
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if check:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def result_of(task):
    return next(e["value"] for e in task.events() if e["kind"] == "result")


def test_a_worker_signs_in_and_waits_for_work(live):
    deadline = time.monotonic() + BOOT
    while time.monotonic() < deadline:
        rows = httpx.get(
            f"{live}/v1/workers", headers={"Authorization": f"Bearer {CLIENT_TOKEN}"}
        ).json()
        if rows:
            assert rows[0]["id"] == "e2e-worker-1"
            return
        time.sleep(0.2)
    raise AssertionError("no worker ever showed up")


def test_a_script_runs_out_there_and_the_answer_comes_back(pool):
    task = pool.run("result = 2 + 2")

    assert result_of(task) == 4
    assert task.state()["status"] == "done"


def test_a_function_is_shipped_with_its_arguments(pool):
    def greet(name, punct="!"):
        return f"hello {name}{punct}"

    task = pool.run(greet, "world", punct="?")

    assert result_of(task) == "hello world?"


def test_what_the_task_printed_is_kept_alongside_its_result(pool):
    task = pool.run("print('a line of output')\nresult = 'ok'")

    assert "a line of output" in task.tail()
    assert result_of(task) == "ok"


def test_a_task_that_raises_comes_back_as_a_failure(pool):
    with pytest.raises(Failed) as raised:
        pool.run("raise ValueError('the reason')")

    assert raised.value.status == "failed"
    assert "the reason" in str(raised.value)
    assert raised.value.event["type"] == "ValueError"


def test_a_task_that_overruns_its_timeout_is_stopped(pool):
    remote = Remote(pool, "import time\ntime.sleep(30)", timeout=1)

    with pytest.raises(Failed) as raised:
        remote()

    assert "TimeoutError" in str(raised.value)


def test_events_can_be_watched_while_the_task_is_still_going(pool):
    task = pool.submit(
        "import time\n"
        "for i in range(3):\n"
        "    emit('step', i)\n"
        "    time.sleep(0.3)\n"
        "result = 'end'"
    )

    steps = [e["value"] for e in task.watch() if e["kind"] == "step"]

    assert steps == [0, 1, 2]
    assert task.state()["status"] == "done"


def test_an_artifact_written_by_a_task_can_be_fetched_by_the_client(pool):
    task = pool.run(
        "from pool import rpc\n"
        "rpc.put('e2e/report.txt', b'written from inside')\n"
        "result = 'stored'"
    )

    assert pool.get("e2e/report.txt") == b"written from inside"
    keys = [a["key"] for a in pool.artifacts(prefix="e2e/")]
    assert "e2e/report.txt" in keys
    assert result_of(task) == "stored"


def test_an_artifact_the_client_uploaded_can_be_read_by_a_task(pool):
    pool.put("e2e/input.txt", b"from the client")

    task = pool.run("from pool import rpc\nresult = rpc.get('e2e/input.txt').decode()")

    assert result_of(task) == "from the client"


def test_an_artifact_that_was_deleted_is_gone(pool):
    pool.put("e2e/gone.txt", b"briefly")
    pool.delete("e2e/gone.txt")

    with pytest.raises(RuntimeError, match="404"):
        pool.get("e2e/gone.txt")


def test_a_batch_of_tasks_all_come_home(pool):
    def square(n):
        return n * n

    tasks = pool.map(square, range(6))

    assert sorted(result_of(t) for t in tasks) == [0, 1, 4, 9, 16, 25]


def test_a_running_task_can_be_called_off(pool):
    task = pool.submit("import time\ntime.sleep(120)")
    deadline = time.monotonic() + BOOT
    while task.state()["status"] != "running" and time.monotonic() < deadline:
        time.sleep(0.1)

    task.cancel()

    assert task.wait()["status"] == "cancelled"


def test_a_retried_task_remembers_where_it_came_from(pool):
    first = pool.submit("result = 'again'")
    first.check()

    body = pool.call("POST", f"/v1/tasks/{first.id}/retry").json()
    second = pool.task(body["task_id"])
    second.check()

    assert body["parent_id"] == first.id
    assert second.state()["parent_id"] == first.id
    assert result_of(second) == "again"


def test_a_large_stream_of_output_survives_the_trip(pool):
    task = pool.run("for i in range(5000):\n    print('x' * 80)\nresult = 'done'")

    assert task.state()["event_size"] > 400000
    assert result_of(task) == "done"


def test_health_knows_about_the_pool(pool):
    health = pool.health()

    assert health["ok"] is True
    assert health["workers"] >= 1


def test_the_command_line_submits_and_follows_a_task(live):
    proc = cli(live, "submit", "python", "-p", "code=print('via the cli')", "-f")

    assert "via the cli" in proc.stdout
    assert "--- done" in proc.stderr


def test_the_command_line_exits_unhappy_when_the_task_failed(live):
    proc = cli(
        live,
        "submit",
        "python",
        "-p",
        "code=raise SystemError('boom')",
        "-f",
        check=False,
    )

    assert proc.returncode == 1
    assert "--- failed" in proc.stderr


def test_the_command_line_shows_the_task_and_the_listing(live):
    tid = cli(live, "submit", "python", "-p", "code=result = 1").stdout.strip()
    cli(live, "events", tid, "-f", check=False)

    assert json.loads(cli(live, "status", tid).stdout)["status"] == "done"
    assert tid in cli(live, "list").stdout


def test_the_command_line_moves_artifacts_both_ways(live, tmp_path):
    src = tmp_path / "up.bin"
    src.write_bytes(b"through the cli")
    dst = tmp_path / "down.bin"

    cli(live, "put", "e2e/cli.bin", str(src))
    cli(live, "get", "e2e/cli.bin", "-o", str(dst))

    assert dst.read_bytes() == b"through the cli"
    assert "e2e/cli.bin" in cli(live, "artifacts", "e2e/").stdout
    assert json.loads(cli(live, "rm", "e2e/cli.bin").stdout) == {"ok": True}


def test_the_command_line_shows_the_workers_and_the_health(live):
    assert "e2e-worker-1" in cli(live, "workers").stdout
    assert json.loads(cli(live, "health").stdout)["ok"] is True


def test_the_pool_keeps_going_without_a_database(pool):
    assert pool.health()["db"] is False
    assert result_of(pool.run("result = 'no database needed'")) == "no database needed"
