import asyncio
import json
import os
import random
import signal
import sys
import time
import traceback
import uuid
from pathlib import Path

import httpx

SERVER = os.getenv("POOL_SERVER", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("POOL_TOKEN", "dev-worker")
WORKER_ID = os.getenv("WORKER_ID") or f"local-{uuid.uuid4().hex[:8]}"
SPOOL_DIR = Path(os.getenv("SPOOL_DIR", "/tmp"))
SPOOL_CAP = int(os.getenv("SPOOL_CAP", str(256 * 1024 * 1024)))
HEARTBEAT = 15.0
KILL_GRACE = 30.0
CHUNK = 1 << 20

CURRENT = None


class Permanent(Exception):
    pass


class Cancelled(Exception):
    pass


def wlog(msg):
    line = f"[worker] {msg}\n".encode()
    sys.stderr.write(line.decode())
    sys.stderr.flush()
    if CURRENT is not None:
        CURRENT.write(line)


class Spool:
    def __init__(self, path):
        self.path = path
        self.fh = open(path, "wb")
        self.size = 0
        self.sent = 0
        self.dropped = 0
        self.stopped = False
        self.event = asyncio.Event()

    def write(self, data):
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

    def read_at(self, offset, n):
        with open(self.path, "rb") as f:
            f.seek(offset)
            return f.read(n)

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


async def req(client, method, path, content_factory=None, **kw):
    delay = 0.5
    while True:
        if content_factory is not None:
            kw["content"] = content_factory()
        try:
            r = await client.request(method, SERVER + path, **kw)
        except Exception as e:
            wlog(f"net {type(e).__name__}, retry in {delay:.1f}s")
            await asyncio.sleep(delay * (1 + random.random()))
            delay = min(delay * 2, 30)
            continue
        if r.status_code < 400 or r.status_code in (204, 409):
            return r
        if 400 <= r.status_code < 500 and r.status_code != 429:
            raise Permanent(f"{r.status_code} {r.text[:200]}")
        wlog(f"http {r.status_code}, retry in {delay:.1f}s")
        await asyncio.sleep(delay * (1 + random.random()))
        delay = min(delay * 2, 30)


def hdr(token):
    return {"Authorization": f"Bearer {TOKEN}", "X-Lease-Token": token}


async def sender(client, spool, tid, token, finished):
    while True:
        if spool.stopped or spool.sent >= spool.size:
            if finished.is_set() and spool.sent >= spool.size:
                return
            try:
                await asyncio.wait_for(spool.event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            spool.event.clear()
            continue
        data = spool.read_at(spool.sent, CHUNK)
        if not data:
            continue
        r = await req(
            client,
            "POST",
            f"/v1/tasks/{tid}/log?offset={spool.sent}",
            content=data,
            headers=hdr(token),
        )
        body = r.json()
        spool.sent = body["offset"]
        if not body.get("accepting", True):
            wlog("server log cap reached, dropping the rest")
            spool.stopped = True
            return


async def heartbeat(client, tid, token, cancel, stale):
    while True:
        try:
            r = await req(client, "POST", f"/v1/tasks/{tid}/heartbeat", headers=hdr(token))
        except Permanent as e:
            wlog(f"heartbeat rejected: {e}")
            stale.set()
            return
        if r.status_code == 409:
            wlog("lease is stale, server gave up on this task")
            stale.set()
            return
        if r.json().get("cancel"):
            cancel.set()
            return
        await asyncio.sleep(HEARTBEAT)


async def pump(proc, spool):
    while True:
        data = await proc.stdout.read(1 << 16)
        if not data:
            return
        spool.write(data)


async def execute(client, lease):
    global CURRENT
    tid = lease["task_id"]
    token = lease["lease_token"]
    ttype = lease["type"]
    base = SPOOL_DIR / f"pool-{tid}"
    spool_path = base.with_suffix(".log")
    payload_path = base.with_suffix(".payload")
    result_path = base.with_suffix(".result")
    payload_path.write_text(json.dumps(lease["payload"]))

    spool = Spool(spool_path)
    CURRENT = spool
    spool.sent = lease.get("log_offset", 0)
    spool.size = spool.sent

    finished = asyncio.Event()
    cancel = asyncio.Event()
    stale = asyncio.Event()

    wlog(f"start {ttype} {tid}")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pool.worker",
        "exec",
        ttype,
        str(payload_path),
        str(result_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
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
        wlog(f"{why}, terminating")
        killed = True
        _signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=KILL_GRACE)
        except asyncio.TimeoutError:
            wlog("grace expired, killing")
            _signal_group(proc, signal.SIGKILL)
            await wait_task

    stop_task.cancel()
    rc = wait_task.result()
    await pump_task

    if stale.is_set():
        wlog("dropping results, nobody is waiting")
        finished.set()
        spool.event.set()
        send_task.cancel()
        beat_task.cancel()
        spool.close()
        _cleanup(spool_path, payload_path, result_path)
        CURRENT = None
        return

    if killed and cancel.is_set():
        status, error = "cancelled", "cancelled by client"
    elif rc == 0:
        status, error = "done", None
    else:
        status, error = "failed", f"exit code {rc}"
    wlog(f"finished: {status}")

    finished.set()
    spool.event.set()
    await send_task

    if result_path.exists() and result_path.stat().st_size > 0:
        wlog(f"uploading result, {result_path.stat().st_size} bytes")

        async def body():
            with open(result_path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        return
                    yield chunk

        await req(
            client,
            "POST",
            f"/v1/tasks/{tid}/result?filename=result.bin",
            content_factory=body,
            headers=hdr(token),
            timeout=None,
        )

    await req(
        client,
        "POST",
        f"/v1/tasks/{tid}/complete",
        json={"status": status, "error": error},
        headers=hdr(token),
    )
    beat_task.cancel()
    spool.close()
    _cleanup(spool_path, payload_path, result_path)
    CURRENT = None


def _signal_group(proc, sig):
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


async def _first(*events):
    await asyncio.wait([asyncio.create_task(e.wait()) for e in events], return_when=asyncio.FIRST_COMPLETED)


def _cleanup(*paths):
    for p in paths:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


async def loop():
    timeout = httpx.Timeout(60.0, read=90.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        wlog(f"worker {WORKER_ID} up, server {SERVER}")
        while True:
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
                wlog(f"task aborted: {e}")
            except Exception:
                traceback.print_exc()
                await asyncio.sleep(5)


def run_exec(ttype, payload_file, result_file):
    import importlib

    tasks = importlib.import_module(os.getenv("POOL_TASKS", "pool.tasks"))
    fn = tasks.REGISTRY.get(ttype)
    if fn is None:
        print(f"unknown task type: {ttype}")
        sys.exit(2)

    def on_term(*_):
        raise Cancelled()

    signal.signal(signal.SIGTERM, on_term)
    payload = json.loads(Path(payload_file).read_text())
    try:
        out = fn(payload, Path(result_file))
    except Cancelled:
        print("[task] cancelled")
        sys.exit(75)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    if out is not None:
        data = out if isinstance(out, bytes) else (out.encode() if isinstance(out, str) else json.dumps(out).encode())
        Path(result_file).write_bytes(data)
    sys.exit(0)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "exec":
        run_exec(sys.argv[2], sys.argv[3], sys.argv[4])
        return
    try:
        asyncio.run(loop())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
