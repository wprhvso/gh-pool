import asyncio
import contextlib
import json
import os
import random
import signal
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import httpx

SERVER = os.getenv("POOL_SERVER", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("POOL_TOKEN", "dev-worker")
WORKER_ID = os.getenv("WORKER_ID") or f"local-{uuid.uuid4().hex[:8]}"
SPOOL_DIR = Path(os.getenv("SPOOL_DIR", "/tmp"))
SPOOL_CAP = int(os.getenv("SPOOL_CAP", str(256 * 1024 * 1024)))
HEARTBEAT = 15.0
KILL_GRACE = 30.0
CHUNK = 1 << 20
# Ноль — жить до таймаута самой джобы. Иначе воркер уходит сам, но только между
# задачами: снятый на середине уносит задачу в lost, а CI-джобу — на второй круг.
# Джиттер задаёт тот, кто запускает, чтобы флот не вымирал одной когортой.
MAX_AGE = float(os.getenv("WORKER_MAX_AGE", "0"))

_current: "Spool | None" = None


class Permanent(Exception):
    pass


class Cancelled(Exception):
    pass


def note(msg: str) -> None:
    line = f"[worker] {msg}\n".encode()
    sys.stderr.write(line.decode())
    sys.stderr.flush()
    if _current is not None:
        _current.write(line)


class Spool:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fh = path.open("wb")
        self.size = 0
        self.sent = 0
        self.dropped = 0
        self.stopped = False
        self.event = asyncio.Event()

    def write(self, data: bytes) -> None:
        if self.stopped:
            return
        if self.size - self.sent > SPOOL_CAP:
            self.dropped += len(data)
            return
        if self.dropped:
            marker = f"\n[worker] dropped {self.dropped} bytes locally\n".encode()
            self.dropped = 0
            self.fh.write(marker)
            self.size += len(marker)
        self.fh.write(data)
        self.fh.flush()
        self.size += len(data)
        self.event.set()

    def read_at(self, offset: int, n: int) -> bytes:
        with self.path.open("rb") as f:
            f.seek(offset)
            return f.read(n)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.fh.close()


async def req(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    content_factory: Callable[[], Any] | None = None,
    **kw: Any,
) -> httpx.Response:
    delay = 0.5
    while True:
        if content_factory is not None:
            kw["content"] = content_factory()
        try:
            r = await client.request(method, SERVER + path, **kw)
        except Exception as e:
            note(f"net {type(e).__name__}, retry in {delay:.1f}s")
            await asyncio.sleep(delay * (1 + random.random()))
            delay = min(delay * 2, 30)
            continue
        if r.status_code < 400 or r.status_code in (204, 409):
            return r
        if 400 <= r.status_code < 500 and r.status_code != 429:
            raise Permanent(f"{r.status_code} {r.text[:200]}")
        note(f"http {r.status_code}, retry in {delay:.1f}s")
        await asyncio.sleep(delay * (1 + random.random()))
        delay = min(delay * 2, 30)


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "X-Lease-Token": token}


async def sender(
    client: httpx.AsyncClient,
    spool: Spool,
    tid: str,
    token: str,
    finished: asyncio.Event,
) -> None:
    while True:
        if spool.stopped or spool.sent >= spool.size:
            if finished.is_set() and spool.sent >= spool.size:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(spool.event.wait(), timeout=1.0)
            spool.event.clear()
            continue
        data = spool.read_at(spool.sent, CHUNK)
        if not data:
            continue
        r = await req(
            client,
            "POST",
            f"/v1/tasks/{tid}/events?offset={spool.sent}",
            content=data,
            headers=hdr(token),
        )
        body = r.json()
        spool.sent = body["offset"]
        if not body.get("accepting", True):
            note("server event cap reached, dropping the rest")
            spool.stopped = True
            return


async def heartbeat(
    client: httpx.AsyncClient,
    tid: str,
    token: str,
    cancel: asyncio.Event,
    stale: asyncio.Event,
) -> None:
    while True:
        try:
            r = await req(
                client, "POST", f"/v1/tasks/{tid}/heartbeat", headers=hdr(token)
            )
        except Permanent as e:
            note(f"heartbeat rejected: {e}")
            stale.set()
            return
        if r.status_code == 409:
            note("lease is stale, server gave up on this task")
            stale.set()
            return
        if r.json().get("cancel"):
            cancel.set()
            return
        await asyncio.sleep(HEARTBEAT)


async def pump(proc: asyncio.subprocess.Process, spool: Spool) -> None:
    stdout = proc.stdout
    if stdout is None:
        raise RuntimeError("subprocess started without a stdout pipe")
    while True:
        data = await stdout.read(1 << 16)
        if not data:
            return
        spool.write(data)


async def execute(client: httpx.AsyncClient, lease: dict[str, Any]) -> None:
    global _current  # noqa: PLW0603
    tid = lease["task_id"]
    token = lease["lease_token"]
    ttype = lease["type"]
    base = SPOOL_DIR / f"pool-{tid}"
    spool_path = base.with_suffix(".events")
    payload_path = base.with_suffix(".payload")
    payload_path.write_text(json.dumps(lease["payload"]))

    spool = Spool(spool_path)
    _current = spool
    spool.sent = lease.get("event_offset", 0)
    spool.size = spool.sent

    finished = asyncio.Event()
    cancel = asyncio.Event()
    stale = asyncio.Event()

    note(f"start {ttype} {tid}")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pool.worker",
        "exec",
        ttype,
        str(payload_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "POOL_TASK": tid},
        start_new_session=True,
    )

    send_task = asyncio.create_task(sender(client, spool, tid, token, finished))
    beat_task = asyncio.create_task(heartbeat(client, tid, token, cancel, stale))
    pump_task = asyncio.create_task(pump(proc, spool))
    wait_task = asyncio.create_task(proc.wait())
    stop_task = asyncio.create_task(_first(cancel, stale))

    await asyncio.wait({wait_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    killed = False
    if not wait_task.done():
        why = "cancelled" if cancel.is_set() else "stale lease"
        note(f"{why}, terminating")
        killed = True
        _signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=KILL_GRACE)
        except TimeoutError:
            note("grace expired, killing")
            _signal_group(proc, signal.SIGKILL)
            await wait_task

    stop_task.cancel()
    rc = wait_task.result()
    await pump_task

    if stale.is_set():
        note("dropping the rest, nobody is waiting")
        finished.set()
        spool.event.set()
        send_task.cancel()
        beat_task.cancel()
        spool.close()
        _cleanup(spool_path, payload_path)
        _current = None
        return

    if killed and cancel.is_set():
        status, error = "cancelled", "cancelled by client"
    elif rc == 0:
        status, error = "done", None
    else:
        status, error = "failed", f"exit code {rc}"
    note(f"finished: {status}")

    finished.set()
    spool.event.set()
    await send_task

    await req(
        client,
        "POST",
        f"/v1/tasks/{tid}/complete",
        json={"status": status, "error": error},
        headers=hdr(token),
    )
    beat_task.cancel()
    spool.close()
    _cleanup(spool_path, payload_path)
    _current = None


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)


async def _first(*events: asyncio.Event) -> None:
    await asyncio.wait(
        [asyncio.create_task(e.wait()) for e in events],
        return_when=asyncio.FIRST_COMPLETED,
    )


def _cleanup(*paths: Path) -> None:
    for p in paths:
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


async def loop() -> None:
    timeout = httpx.Timeout(60.0, read=90.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        note(f"worker {WORKER_ID} up, server {SERVER}")
        born = time.monotonic()
        while True:
            if MAX_AGE and time.monotonic() - born > MAX_AGE:
                note(f"worker {WORKER_ID} retiring after {MAX_AGE:.0f}s")
                return
            r = await req(
                client,
                "POST",
                "/v1/lease",
                json={"worker_id": WORKER_ID},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            if r.status_code == 204:
                continue
            try:
                await execute(client, r.json())
            except Permanent as e:
                note(f"task aborted: {e}")
            except Exception:
                traceback.print_exc()
                await asyncio.sleep(5)


def run_exec(ttype: str, payload_file: str) -> NoReturn:
    import importlib

    tasks = importlib.import_module(os.getenv("POOL_TASKS", "pool.tasks"))
    fn = tasks.REGISTRY.get(ttype)
    if fn is None:
        print(f"unknown task type: {ttype}")  # noqa: T201
        sys.exit(2)

    def on_term(*_: Any) -> NoReturn:
        raise Cancelled

    signal.signal(signal.SIGTERM, on_term)
    payload = json.loads(Path(payload_file).read_text())
    try:
        fn(payload)
    except Cancelled:
        print("[task] cancelled")  # noqa: T201
        sys.exit(75)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "exec":
        run_exec(sys.argv[2], sys.argv[3])
        return
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(loop())


if __name__ == "__main__":
    main()
