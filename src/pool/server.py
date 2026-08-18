import asyncio
import hashlib
import os
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import BufferedWriter
from pathlib import Path
from typing import Annotated, Any

import anyio
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from pool import db

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
BLOB_DIR = Path(os.getenv("BLOB_DIR", str(DATA_DIR / "blobs")))
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "dev-worker")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN", "dev-client")
EVENT_CAP = int(os.getenv("EVENT_CAP", str(100 * 1024 * 1024)))
LOST_AFTER = float(os.getenv("LOST_AFTER", "300"))
LEASE_WAIT = float(os.getenv("LEASE_WAIT", "30"))
WORKER_STALE = float(os.getenv("WORKER_STALE", "120"))
FLUSH_EVERY = float(os.getenv("FLUSH_EVERY", "0.2"))
# WORKERS живёт в памяти, поэтому сразу после рестарта он пуст, и всякий, кто судит
# по нему о живости флота, увидит ноль там, где на деле двадцать воркеров. Отдаём
# момент старта, чтобы такой вывод можно было придержать.
STARTED = time.time()

TERMINAL = ("done", "failed", "cancelled")
FINISHED = (*TERMINAL, "lost")

TASKS: dict[str, dict] = {}
QUEUE: deque[str] = deque()
WORKERS: dict[str, dict] = {}
BLOBS: dict[str, dict] = {}
DIRTY: set[str] = set()
DIRTY_BLOBS: set[str] = set()

new_task = asyncio.Event()
event_locks: dict[str, asyncio.Lock] = {}
flush_lock = asyncio.Lock()
state = {"db": False}

DATA_DIR.mkdir(parents=True, exist_ok=True)
BLOB_DIR.mkdir(parents=True, exist_ok=True)


def task_dir(tid: str) -> Path:
    d = DATA_DIR / tid
    d.mkdir(parents=True, exist_ok=True)
    return d


def events_path(tid: str) -> Path:
    return task_dir(tid) / "events.txt"


def events_size(tid: str) -> int:
    p = events_path(tid)
    return p.stat().st_size if p.exists() else 0


def blob_path(key: str) -> Path:
    h = hashlib.sha256(key.encode()).hexdigest()
    return BLOB_DIR / h[:2] / h


def auth_worker(h: str | None) -> None:
    if h != f"Bearer {WORKER_TOKEN}":
        raise HTTPException(401, "bad worker token")


def auth_client(h: str | None) -> None:
    if h != f"Bearer {CLIENT_TOKEN}":
        raise HTTPException(401, "bad client token")


def auth_any(h: str | None) -> None:
    if h not in (f"Bearer {WORKER_TOKEN}", f"Bearer {CLIENT_TOKEN}"):
        raise HTTPException(401, "bad token")


def public(t: dict[str, Any]) -> dict[str, Any]:
    d = {c: t.get(c) for c in db.TASK_COLUMNS}
    d["cancel_requested"] = bool(t.get("cancel_requested"))
    d["event_size"] = events_size(d["id"])
    return d


async def find(tid: str) -> dict[str, Any]:
    t = TASKS.get(tid) or await db.fetch(db.Task, tid)
    if t is None:
        raise HTTPException(404, "no such task")
    return t


def owned(tid: str, lease_token: str | None) -> dict[str, Any]:
    t = TASKS.get(tid)
    if t is None or not lease_token or t.get("lease_token") != lease_token:
        raise HTTPException(409, "stale lease")
    return t


def grab(worker_id: str) -> dict[str, Any] | None:
    now = time.time()
    while QUEUE:
        t = TASKS.get(QUEUE.popleft())
        if t is None or t["status"] != "pending":
            continue
        token = uuid.uuid4().hex
        t.update(
            status="running",
            worker_id=worker_id,
            lease_token=token,
            started_at=now,
            heartbeat_at=now,
        )
        DIRTY.add(t["id"])
        WORKERS[worker_id] = {"seen_at": now, "task_id": t["id"]}
        return {
            "task_id": t["id"],
            "type": t["type"],
            "payload": t["payload"],
            "lease_token": token,
            "event_offset": events_size(t["id"]),
        }
    return None


async def flush() -> None:
    async with flush_lock:
        ids, keys = list(DIRTY), list(DIRTY_BLOBS)
        if not ids and not keys:
            return
        DIRTY.difference_update(ids)
        DIRTY_BLOBS.difference_update(keys)
        try:
            await db.save(
                db.Task,
                [
                    {c: TASKS[i].get(c) for c in db.TASK_COLUMNS}
                    for i in ids
                    if i in TASKS
                ],
            )
            await db.save(db.Artifact, [BLOBS[k] for k in keys if k in BLOBS])
        except Exception as e:
            DIRTY.update(ids)
            DIRTY_BLOBS.update(keys)
            state["db"] = False
            print("flush:", type(e).__name__, e)  # noqa: T201
            return
        state["db"] = True
        for i in ids:
            if i not in DIRTY and TASKS.get(i, {}).get("status") in FINISHED:
                TASKS.pop(i, None)
        for k in keys:
            if k not in DIRTY_BLOBS:
                BLOBS.pop(k, None)


async def recover() -> None:
    for t in await db.unfinished():
        if t["id"] in TASKS:
            continue
        TASKS[t["id"]] = t
        if t["status"] == "running":
            t.update(status="lost", error="server restarted", finished_at=time.time())
            DIRTY.add(t["id"])
        else:
            QUEUE.append(t["id"])
    if QUEUE:
        new_task.set()
    print(f"recovered: {len(QUEUE)} pending")  # noqa: T201


async def keeper() -> None:
    started = False
    while True:
        if not started:
            try:
                await db.setup()
                await recover()
                started = state["db"] = True
            except Exception as e:
                print("db:", type(e).__name__, e)  # noqa: T201
        now = time.time()
        for t in list(TASKS.values()):
            if (
                t["status"] == "running"
                and t.get("heartbeat_at", now) < now - LOST_AFTER
            ):
                t.update(
                    status="lost",
                    error="worker gone",
                    finished_at=now,
                    lease_token=None,
                )
                DIRTY.add(t["id"])
        for wid, w in list(WORKERS.items()):
            if w["seen_at"] < now - WORKER_STALE:
                WORKERS.pop(wid, None)
        await flush()
        await asyncio.sleep(FLUSH_EVERY)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    t = asyncio.create_task(keeper())
    yield
    t.cancel()
    await flush()


app = FastAPI(lifespan=lifespan)


@app.post("/v1/lease")
async def lease(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Any:
    auth_worker(authorization)
    body = await request.json()
    worker_id = body.get("worker_id")
    if not worker_id:
        raise HTTPException(400, "worker_id required")
    WORKERS[worker_id] = {"seen_at": time.time(), "task_id": None}
    deadline = time.monotonic() + LEASE_WAIT
    while True:
        t = grab(worker_id)
        if t:
            return t
        left = deadline - time.monotonic()
        if left <= 0:
            return Response(status_code=204)
        try:
            await asyncio.wait_for(new_task.wait(), timeout=left)
            new_task.clear()
        except TimeoutError:
            return Response(status_code=204)


@app.post("/v1/tasks/{tid}/heartbeat")
async def heartbeat(
    tid: str,
    x_lease_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    auth_worker(authorization)
    t = owned(tid, x_lease_token)
    now = time.time()
    t["heartbeat_at"] = now
    if t["worker_id"] in WORKERS:
        WORKERS[t["worker_id"]]["seen_at"] = now
    return {"cancel": bool(t.get("cancel_requested"))}


@app.post("/v1/tasks/{tid}/events")
async def append_events(
    tid: str,
    request: Request,
    offset: Annotated[int, Query()],
    x_lease_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Any:
    auth_worker(authorization)
    owned(tid, x_lease_token)
    lock = event_locks.setdefault(tid, asyncio.Lock())
    async with lock:
        size = events_size(tid)
        if size >= EVENT_CAP:
            return {"offset": size, "accepting": False}
        if offset != size:
            return JSONResponse({"offset": size, "accepting": True}, status_code=409)
        data = await request.body()
        if data:
            p = events_path(tid)

            def w() -> int:
                with p.open("ab") as f:
                    f.write(data)
                    return f.tell()

            size = await anyio.to_thread.run_sync(w)
        return {"offset": size, "accepting": size < EVENT_CAP}


@app.post("/v1/tasks/{tid}/complete")
async def complete(
    tid: str,
    request: Request,
    x_lease_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    auth_worker(authorization)
    t = owned(tid, x_lease_token)
    body = await request.json()
    status = body.get("status", "done")
    if status not in TERMINAL:
        raise HTTPException(400, "bad status")
    if t["status"] in FINISHED:
        return {"ok": True, "status": t["status"], "note": "already terminal"}
    t.update(
        status=status,
        error=body.get("error"),
        finished_at=time.time(),
        lease_token=None,
    )
    DIRTY.add(tid)
    if t["worker_id"] in WORKERS:
        WORKERS[t["worker_id"]]["task_id"] = None
    event_locks.pop(tid, None)
    return {"ok": True, "status": status}


@app.post("/v1/tasks")
async def create_task(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> dict[str, str]:
    auth_client(authorization)
    body = await request.json()
    ttype = body.get("type")
    if not ttype:
        raise HTTPException(400, "type required")
    tid = uuid.uuid4().hex
    TASKS[tid] = {
        "id": tid,
        "type": ttype,
        "payload": body.get("payload") or {},
        "status": "pending",
        "worker_id": None,
        "error": None,
        "parent_id": body.get("parent_id"),
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
    }
    QUEUE.append(tid)
    DIRTY.add(tid)
    new_task.set()
    return {"task_id": tid}


@app.get("/v1/tasks")
async def list_tasks(
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = 100,
    authorization: Annotated[str | None, Header()] = None,
) -> list[dict[str, Any]]:
    auth_client(authorization)
    live = [t for t in TASKS.values() if not status or t["status"] == status]
    seen = {t["id"] for t in live}
    stored = [t for t in await db.tasks(status, limit) if t["id"] not in seen]
    rows = sorted(live + stored, key=lambda t: t["created_at"], reverse=True)
    return [public(t) for t in rows[:limit]]


@app.get("/v1/tasks/{tid}")
async def task_status(
    tid: str, authorization: Annotated[str | None, Header()] = None
) -> dict[str, Any]:
    auth_client(authorization)
    return public(await find(tid))


@app.get("/v1/tasks/{tid}/events")
async def read_events(
    tid: str,
    offset: Annotated[int, Query()] = 0,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    auth_client(authorization)
    t = await find(tid)
    p = events_path(tid)
    size = p.stat().st_size if p.exists() else 0
    data = b""
    if offset < size:

        def r() -> bytes:
            with p.open("rb") as f:
                f.seek(offset)
                return f.read()

        data = await anyio.to_thread.run_sync(r)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-Event-Offset": str(offset + len(data)),
            "X-Task-Status": t["status"],
            "X-Event-Size": str(size),
        },
    )


@app.post("/v1/tasks/{tid}/cancel")
async def cancel(
    tid: str, authorization: Annotated[str | None, Header()] = None
) -> dict[str, Any]:
    auth_client(authorization)
    t = await find(tid)
    if t["status"] == "pending":
        t.update(
            status="cancelled", finished_at=time.time(), error="cancelled before start"
        )
        DIRTY.add(tid)
        return {"status": "cancelled"}
    if t["status"] == "running":
        t["cancel_requested"] = True
        return {"status": "running", "cancel_requested": True}
    return {"status": t["status"], "note": "already terminal"}


@app.post("/v1/tasks/{tid}/retry")
async def retry(
    tid: str, authorization: Annotated[str | None, Header()] = None
) -> dict[str, str]:
    auth_client(authorization)
    t = await find(tid)
    nid = uuid.uuid4().hex
    TASKS[nid] = {
        **{c: t[c] for c in db.TASK_COLUMNS},
        "id": nid,
        "status": "pending",
        "worker_id": None,
        "error": None,
        "parent_id": tid,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
    }
    QUEUE.append(nid)
    DIRTY.add(nid)
    new_task.set()
    return {"task_id": nid, "parent_id": tid}


def _write(f: BufferedWriter, digest: "hashlib._Hash", chunk: bytes) -> None:
    digest.update(chunk)
    f.write(chunk)


@app.put("/v1/artifacts/{key:path}")
async def put_artifact(
    key: str,
    request: Request,
    task_id: Annotated[str | None, Query()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    auth_any(authorization)
    final = blob_path(key)
    part = final.with_suffix(".part")
    digest = hashlib.sha256()
    size = 0

    def start() -> BufferedWriter:
        final.parent.mkdir(parents=True, exist_ok=True)
        return part.open("wb")

    f = await anyio.to_thread.run_sync(start)
    try:
        async for chunk in request.stream():
            if chunk:
                size += len(chunk)
                await anyio.to_thread.run_sync(_write, f, digest, chunk)
    finally:
        await anyio.to_thread.run_sync(f.close)
    await anyio.to_thread.run_sync(os.replace, part, final)

    row = {
        "key": key,
        "path": str(final),
        "size": size,
        "sha256": digest.hexdigest(),
        "task_id": task_id,
        "created_at": time.time(),
    }
    BLOBS[key] = row
    DIRTY_BLOBS.add(key)
    return row


@app.get("/v1/artifacts")
async def list_artifacts(
    prefix: Annotated[str, Query()] = "",
    limit: Annotated[int, Query()] = 100,
    authorization: Annotated[str | None, Header()] = None,
) -> list[dict[str, Any]]:
    auth_any(authorization)
    live = [b for b in BLOBS.values() if b["key"].startswith(prefix)]
    seen = {b["key"] for b in live}
    stored = [b for b in await db.artifacts(prefix, limit) if b["key"] not in seen]
    return sorted(live + stored, key=lambda b: b["created_at"], reverse=True)[:limit]


@app.get("/v1/artifacts/{key:path}")
async def get_artifact(
    key: str, authorization: Annotated[str | None, Header()] = None
) -> FileResponse:
    auth_any(authorization)
    p = blob_path(key)
    if not await anyio.to_thread.run_sync(p.exists):
        raise HTTPException(404, "no such key")
    return FileResponse(p, filename=Path(key).name or "artifact")


@app.delete("/v1/artifacts/{key:path}")
async def del_artifact(
    key: str, authorization: Annotated[str | None, Header()] = None
) -> dict[str, bool]:
    auth_any(authorization)
    async with flush_lock:
        DIRTY_BLOBS.discard(key)
        BLOBS.pop(key, None)
    await anyio.to_thread.run_sync(blob_path(key).unlink, True)
    await db.drop(db.Artifact, key)
    return {"ok": True}


@app.get("/v1/workers")
async def workers(
    authorization: Annotated[str | None, Header()] = None,
) -> list[dict[str, Any]]:
    auth_client(authorization)
    now = time.time()
    return [
        {"id": wid, "task_id": w["task_id"], "idle_for": round(now - w["seen_at"], 1)}
        for wid, w in sorted(WORKERS.items(), key=lambda kv: -kv[1]["seen_at"])
    ]


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    counts = {}
    for t in TASKS.values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {
        "ok": True,
        "tasks": counts,
        "queue": len(QUEUE),
        "workers": len(WORKERS),
        "started_at": STARTED,
        "uptime": round(time.time() - STARTED, 1),
        "pending_writes": len(DIRTY) + len(DIRTY_BLOBS),
        "db": state["db"],
    }


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),  # noqa: S104
        port=int(os.getenv("PORT", "8000")),
        access_log=False,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
