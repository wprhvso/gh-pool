import os
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from gh_pool.cli.shell import Escape
from gh_pool.core.config import settings
from gh_pool.server import shell as api
from gh_pool.server import tasks as server
from tests.conftest import CLIENT, WORKER, as_client, as_worker

BOOT = 20.0
BEAT = 10.0


def seed(tid="", ttype="shell"):
    tid = tid or uuid.uuid4().hex
    now = time.time()
    server.TASKS[tid] = {
        "id": tid,
        "type": ttype,
        "payload": {},
        "status": "running",
        "worker_id": "w1",
        "error": None,
        "parent_id": None,
        "created_at": now,
        "started_at": now,
        "finished_at": None,
    }
    return tid


@pytest.fixture
def shell_room(blank, monkeypatch):
    monkeypatch.setattr(settings, "shell_poll", 0.05)
    api.SHELLS.clear()
    yield
    api.SHELLS.clear()


async def out_up(client, tid, data, offset):
    return await client.post(
        f"/v1/shells/{tid}/out",
        params={"offset": offset},
        content=data,
        headers=as_worker(),
    )


async def out_down(client, tid, offset=0):
    return await client.get(
        f"/v1/shells/{tid}/out", params={"offset": offset}, headers=as_client()
    )


async def in_up(client, tid, data, offset):
    return await client.post(
        f"/v1/shells/{tid}/in",
        params={"offset": offset},
        content=data,
        headers=as_client(),
    )


async def in_down(client, tid, offset=0):
    return await client.get(
        f"/v1/shells/{tid}/in", params={"offset": offset}, headers=as_worker()
    )


async def test_what_the_runner_writes_the_client_reads(client, shell_room):
    tid = seed()

    first = await out_up(client, tid, b"hel", 0)
    await out_up(client, tid, b"lo\n", first.json()["offset"])

    seen = await out_down(client, tid)
    assert seen.content == b"hello\n"
    assert seen.headers["X-Shell-Offset"] == "6"


async def test_a_chunk_sent_twice_lands_once(client, shell_room):
    tid = seed()
    await out_up(client, tid, b"once\n", 0)

    again = await out_up(client, tid, b"once\n", 0)

    assert again.json() == {"offset": 5}
    assert (await out_down(client, tid)).content == b"once\n"


async def test_the_client_reads_only_what_it_has_not_seen(client, shell_room):
    tid = seed()
    await out_up(client, tid, b"one\ntwo\n", 0)

    rest = await out_down(client, tid, offset=4)

    assert rest.content == b"two\n"


async def test_what_the_client_types_the_runner_picks_up(client, shell_room):
    tid = seed()

    await in_up(client, tid, b"echo hi\n", 0)

    seen = await in_down(client, tid)
    assert seen.content == b"echo hi\n"
    assert seen.headers["X-Shell-Size"] == "80x24"


async def test_the_runner_is_told_the_window_the_client_sits_in(client, shell_room):
    tid = seed()

    await client.post(
        f"/v1/shells/{tid}/size", json={"cols": 120, "rows": 40}, headers=as_client()
    )

    assert (await in_down(client, tid)).headers["X-Shell-Size"] == "120x40"


async def test_a_client_that_comes_back_is_told_where_the_streams_stand(
    client, shell_room
):
    tid = seed()
    await out_up(client, tid, b"hello\n", 0)
    await in_up(client, tid, b"echo hi\n", 0)

    opening = await client.get(f"/v1/shells/{tid}", headers=as_client())

    assert opening.json() == {"out": 6, "in": 8, "cols": 80, "rows": 24}


async def test_a_client_that_fell_behind_the_buffer_gets_what_is_left(
    client, shell_room, monkeypatch
):
    monkeypatch.setattr(settings, "shell_cap", 8)
    tid = seed()
    await out_up(client, tid, b"0123456789abcdef", 0)

    late = await out_down(client, tid, offset=0)

    assert late.content == b"89abcdef"
    assert late.headers["X-Shell-Offset"] == "16"


async def test_the_runner_is_sent_home_when_nobody_is_attached(
    client, shell_room, monkeypatch
):
    monkeypatch.setattr(settings, "shell_idle", -1.0)
    tid = seed()

    assert (await in_down(client, tid)).status_code == 410


async def test_a_shell_for_a_task_that_is_over_is_refused(client, shell_room):
    tid = seed()
    server.TASKS[tid]["status"] = "done"

    assert (await out_down(client, tid)).status_code == 410


async def test_only_a_shell_task_carries_a_shell(client, shell_room):
    tid = seed(ttype="python")

    assert (await out_down(client, tid)).status_code == 409


async def test_each_side_of_the_channel_takes_its_own_token(client, shell_room):
    tid = seed()

    assert (
        await client.get(f"/v1/shells/{tid}/out", headers=as_worker())
    ).status_code == 401
    assert (
        await client.get(f"/v1/shells/{tid}/in", headers=as_client())
    ).status_code == 401


def test_the_escape_sequence_detaches_only_at_the_start_of_a_line():
    escape = Escape()

    assert escape.filter(b"ls -la\n") == (b"ls -la\n", False)
    assert escape.filter(b"~") == (b"", False)
    assert escape.filter(b".") == (b"", True)


def test_a_tilde_in_the_middle_of_a_line_is_just_a_tilde():
    escape = Escape()

    assert escape.filter(b"cat ~/.bashrc\n") == (b"cat ~/.bashrc\n", False)


def test_a_tilde_at_the_start_of_a_line_survives_when_nothing_follows_it():
    escape = Escape()
    escape.filter(b"\n")

    assert escape.filter(b"~") == (b"", False)
    assert escape.filter(b"/tmp\n") == (b"~/tmp\n", False)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Listening(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        pass


@pytest.fixture
def live(blank):
    api.SHELLS.clear()
    app = FastAPI()
    app.include_router(server.router)
    app.include_router(api.router)
    port = free_port()
    running = Listening(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None)
    )
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + BOOT
    while not running.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not running.started:
        pytest.fail("сервер не поднялся")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        running.should_exit = True
        thread.join(BOOT)
        api.SHELLS.clear()


class Typist:
    def __init__(self, base, tid):
        self.http = httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {CLIENT}"}, timeout=30.0
        )
        self.tid = tid
        self.read = 0
        self.sent = 0
        self.seen = bytearray()

    def type(self, text):
        answer = self.http.post(
            f"/v1/shells/{self.tid}/in",
            params={"offset": self.sent},
            content=text.encode(),
        )
        answer.raise_for_status()
        self.sent = answer.json()["offset"]

    def until(self, needle, timeout=BEAT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            answer = self.http.get(
                f"/v1/shells/{self.tid}/out", params={"offset": self.read}
            )
            answer.raise_for_status()
            self.seen += answer.content
            self.read = int(answer.headers["X-Shell-Offset"])
            if needle.encode() in self.seen:
                return bytes(self.seen)
        raise AssertionError(f"не дождался {needle!r} в {bytes(self.seen)!r}")

    def close(self):
        self.http.close()


def runner(base, tid, payload="{}"):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from gh_pool.shell import shell; shell({payload})",
        ],
        env={
            **os.environ,
            "GH_POOL_SERVER": base,
            "GH_POOL_WORKER_TOKEN": WORKER,
            "GH_POOL_TASK": tid,
            "PYTHONUNBUFFERED": "1",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_a_real_shell_on_the_other_end_answers_what_it_is_asked(live):
    tid = seed()
    proc = runner(live, tid)
    typist = Typist(live, tid)
    try:
        typist.type("echo из-пула-$((6*7))\n")

        assert b"\xd0\xb8\xd0\xb7-\xd0\xbf\xd1\x83\xd0\xbb\xd0\xb0-42" in typist.until(
            "из-пула-42"
        )

        typist.type("exit\n")
        assert proc.wait(timeout=BEAT) == 0
    finally:
        typist.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=BEAT)


def test_a_resize_reaches_the_terminal_the_shell_runs_in(live):
    tid = seed()
    proc = runner(live, tid, payload="{'cols': 80, 'rows': 24}")
    typist = Typist(live, tid)
    try:
        typist.type("echo ширина-$(tput cols)\n")
        typist.until("ширина-80")

        typist.http.post(f"/v1/shells/{tid}/size", json={"cols": 132, "rows": 50})
        typist.type("echo ширина-$(tput cols)\n")

        typist.until("ширина-132")
    finally:
        typist.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=BEAT)


def test_the_shell_gives_up_when_the_client_stops_coming(live, monkeypatch):
    monkeypatch.setattr(settings, "shell_idle", 0.5)
    tid = seed()
    proc = runner(live, tid)
    try:
        typist = Typist(live, tid)
        typist.type("echo живой\n")
        typist.until("живой")
        typist.close()

        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=BEAT)
