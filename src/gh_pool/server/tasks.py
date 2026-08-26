import asyncio
import hashlib
import os
import time
import uuid
from io import BufferedWriter
from pathlib import Path
from typing import Annotated, Any

import structlog
from anyio import to_thread
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from gh_pool.core.config import settings
from gh_pool.db import tasks as db
from gh_pool.server.pool import state
from gh_pool.server.pool.auth import auth_any, auth_client, auth_worker
from gh_pool.server.pool.metrics import tasks_completed, tasks_created
from gh_pool.server.pool.paths import blob_path, events_path, events_size
from gh_pool.server.pool.queue import find, from_db, grab, owned, public, touch
from gh_pool.server.pool.store import overloaded
from gh_pool.status import FINISHED, REPORTABLE, TaskStatus

log = structlog.get_logger()

MAX_PAGE = 1000


class TaskView(BaseModel):
    id: str
    type: str
    payload: dict[str, Any]
    status: TaskStatus
    worker_id: str | None
    error: str | None
    parent_id: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None
    cancel_requested: bool
    event_size: int


router = APIRouter()


@router.post("/v1/lease")
async def lease(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Any:
    auth_worker(authorization)
    body = await request.json()
    worker_id = body.get("worker_id")
    if not worker_id:
        raise HTTPException(400, "worker_id required")
    touch(worker_id, None, time.time())
    deadline = time.monotonic() + settings.lease_wait
    while True:
        t = grab(worker_id)
        if t:
            return t
        left = deadline - time.monotonic()
        if left <= 0:
            return Response(status_code=204)
        try:
            await asyncio.wait_for(state.current.arrived.wait(), timeout=left)
            state.current.arrived.clear()
        except TimeoutError:
            return Response(status_code=204)


@router.post("/v1/tasks/{tid}/heartbeat")
async def heartbeat(
    tid: str,
    x_lease_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    auth_worker(authorization)
    t = owned(tid, x_lease_token)
    now = time.time()
    t["heartbeat_at"] = now
    worker = t.get("worker_id")
    if worker:
        touch(worker, tid, now)
    return {"cancel": bool(t.get("cancel_requested"))}


@router.post("/v1/tasks/{tid}/events")
async def append_events(
    tid: str,
    request: Request,
    offset: Annotated[int, Query()],
    x_lease_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Any:
    auth_worker(authorization)
    owned(tid, x_lease_token)
    lock = state.current.event_locks.setdefault(tid, asyncio.Lock())
    async with lock:
        size = events_size(tid)
        if size >= settings.event_cap:
            return {"offset": size, "accepting": False}
        if offset != size:
            return JSONResponse({"offset": size, "accepting": True}, status_code=409)
        data = await request.body()
        if data:
            p = events_path(tid)

            def w() -> int:
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("ab") as f:
                    f.write(data)
                    return f.tell()

            size = await to_thread.run_sync(w)
        return {"offset": size, "accepting": size < settings.event_cap}


@router.post("/v1/tasks/{tid}/complete")
async def complete(
    tid: str,
    request: Request,
    x_lease_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    auth_worker(authorization)
    t = owned(tid, x_lease_token)
    body = await request.json()
    status = body.get("status", TaskStatus.DONE)
    if status not in REPORTABLE:
        raise HTTPException(400, "bad status")
    if t["status"] in FINISHED:
        return {"ok": True, "status": t["status"], "note": "already terminal"}
    t.update(
        status=status,
        error=body.get("error"),
        finished_at=time.time(),
        lease_token=None,
    )
    state.current.dirty.add(tid)
    if t["worker_id"] in state.current.workers:
        state.current.workers[t["worker_id"]]["task_id"] = None
    state.current.event_locks.pop(tid, None)
    tasks_completed.add(1, {"status": status, "type": t["type"]})
    log.info(
        "task_finished",
        task=tid,
        type=t["type"],
        status=status,
        worker=t["worker_id"],
        error=t["error"],
        seconds=round(t["finished_at"] - (t["started_at"] or t["finished_at"]), 3),
    )
    return {"ok": True, "status": status}


@router.post("/v1/tasks")
async def create_task(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> dict[str, str]:
    auth_client(authorization)
    body = await request.json()
    ttype = body.get("type")
    if not ttype:
        raise HTTPException(400, "type required")
    if overloaded():
        raise HTTPException(503, "too many unflushed writes, try again shortly")
    tid = uuid.uuid4().hex
    state.current.tasks[tid] = {
        "id": tid,
        "type": ttype,
        "payload": body.get("payload") or {},
        "status": TaskStatus.PENDING,
        "worker_id": None,
        "error": None,
        "parent_id": body.get("parent_id"),
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "cancel_requested": False,
    }
    state.current.queue.append(tid)
    state.current.dirty.add(tid)
    state.current.arrived.set()
    tasks_created.add(1, {"type": ttype})
    return {"task_id": tid}


@router.get("/v1/tasks")
async def list_tasks(
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
    authorization: Annotated[str | None, Header()] = None,
) -> list[TaskView]:
    auth_client(authorization)
    live = [
        t for t in state.current.tasks.values() if not status or t["status"] == status
    ]
    seen = {t["id"] for t in live}
    rows_from_db = await from_db("tasks", db.tasks(status, limit), [])
    stored = [t for t in rows_from_db if t["id"] not in seen]
    rows = sorted(live + stored, key=lambda t: t["created_at"], reverse=True)
    return [TaskView(**public(t)) for t in rows[:limit]]


@router.get("/v1/tasks/{tid}")
async def task_status(
    tid: str, authorization: Annotated[str | None, Header()] = None
) -> TaskView:
    auth_client(authorization)
    return TaskView(**public(await find(tid)))


@router.get("/v1/tasks/{tid}/events")
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

        data = await to_thread.run_sync(r)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-Event-Offset": str(offset + len(data)),
            "X-Task-Status": t["status"],
            "X-Event-Size": str(size),
        },
    )


@router.post("/v1/tasks/{tid}/cancel")
async def cancel(
    tid: str, authorization: Annotated[str | None, Header()] = None
) -> dict[str, Any]:
    auth_client(authorization)
    t = await find(tid)
    if t["status"] == TaskStatus.PENDING:
        t.update(
            status=TaskStatus.CANCELLED,
            finished_at=time.time(),
            error="cancelled before start",
        )
        state.current.tasks.setdefault(tid, t)
        state.current.dirty.add(tid)
        tasks_completed.add(1, {"status": TaskStatus.CANCELLED, "type": t["type"]})
        return {"status": TaskStatus.CANCELLED}
    if t["status"] == TaskStatus.RUNNING:
        t["cancel_requested"] = True
        state.current.tasks.setdefault(tid, t)
        state.current.dirty.add(tid)
        return {"status": TaskStatus.RUNNING, "cancel_requested": True}
    return {"status": t["status"], "note": "already terminal"}


@router.post("/v1/tasks/{tid}/retry")
async def retry(
    tid: str, authorization: Annotated[str | None, Header()] = None
) -> dict[str, str]:
    auth_client(authorization)
    t = await find(tid)
    nid = uuid.uuid4().hex
    state.current.tasks[nid] = {
        **{c: t[c] for c in db.TASK_COLUMNS},
        "id": nid,
        "status": TaskStatus.PENDING,
        "worker_id": None,
        "error": None,
        "parent_id": tid,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "cancel_requested": False,
    }
    state.current.queue.append(nid)
    state.current.dirty.add(nid)
    state.current.arrived.set()
    tasks_created.add(1, {"type": t["type"], "retry": True})
    return {"task_id": nid, "parent_id": tid}


def _write(f: BufferedWriter, digest: hashlib._Hash, chunk: bytes) -> None:
    digest.update(chunk)
    f.write(chunk)


@router.put("/v1/artifacts/{key:path}")
async def put_artifact(
    key: str,
    request: Request,
    task_id: Annotated[str | None, Query()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    auth_any(authorization)
    final = blob_path(key)
    part = final.with_suffix(f".{uuid.uuid4().hex}.part")
    digest = hashlib.sha256()
    size = 0

    def start() -> BufferedWriter:
        final.parent.mkdir(parents=True, exist_ok=True)
        return part.open("wb")

    f = await to_thread.run_sync(start)
    try:
        async for chunk in request.stream():
            if chunk:
                size += len(chunk)
                await to_thread.run_sync(_write, f, digest, chunk)
        await to_thread.run_sync(f.close)
        await to_thread.run_sync(os.replace, part, final)
    except BaseException:
        await to_thread.run_sync(f.close)
        await to_thread.run_sync(part.unlink, True)
        raise

    row = {
        "key": key,
        "path": str(final),
        "size": size,
        "sha256": digest.hexdigest(),
        "task_id": task_id,
        "created_at": time.time(),
    }
    state.current.blobs[key] = row
    state.current.dirty_blobs.add(key)
    return row


@router.get("/v1/artifacts")
async def list_artifacts(
    prefix: Annotated[str, Query()] = "",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
    authorization: Annotated[str | None, Header()] = None,
) -> list[dict[str, Any]]:
    auth_any(authorization)
    live = [b for b in state.current.blobs.values() if b["key"].startswith(prefix)]
    seen = {b["key"] for b in live}
    rows_from_db = await from_db("artifacts", db.artifacts(prefix, limit), [])
    stored = [b for b in rows_from_db if b["key"] not in seen]
    return sorted(live + stored, key=lambda b: b["created_at"], reverse=True)[:limit]


@router.get("/v1/artifacts/{key:path}")
async def get_artifact(
    key: str, authorization: Annotated[str | None, Header()] = None
) -> FileResponse:
    auth_any(authorization)
    p = blob_path(key)
    if not await to_thread.run_sync(p.exists):
        raise HTTPException(404, "no such key")
    return FileResponse(p, filename=Path(key).name or "artifact")


@router.delete("/v1/artifacts/{key:path}")
async def del_artifact(
    key: str, authorization: Annotated[str | None, Header()] = None
) -> dict[str, bool]:
    auth_any(authorization)
    async with state.current.flush_lock:
        state.current.dirty_blobs.discard(key)
        state.current.blobs.pop(key, None)
    await to_thread.run_sync(blob_path(key).unlink, True)
    await from_db("drop", db.drop(db.Artifact, key), None)
    return {"ok": True}


@router.get("/v1/workers")
async def workers(
    authorization: Annotated[str | None, Header()] = None,
) -> list[dict[str, Any]]:
    auth_client(authorization)
    now = time.time()
    return [
        {
            "id": wid,
            "task_id": w["task_id"],
            "idle_for": round(now - w["seen_at"], 1),
            "serving_for": round(now - w["first_seen"], 1),
        }
        for wid, w in sorted(
            state.current.workers.items(), key=lambda kv: -kv[1]["seen_at"]
        )
    ]
