import time
import uuid
from collections.abc import Coroutine
from typing import Any

import structlog
from fastapi import HTTPException

from gh_pool.db import tasks as db
from gh_pool.server.pool import state
from gh_pool.server.pool.metrics import lease_wait
from gh_pool.server.pool.paths import events_size
from gh_pool.status import TaskStatus

log = structlog.get_logger()


def public(t: dict[str, Any]) -> dict[str, Any]:
    d: dict[str, Any] = {c: t.get(c) for c in db.TASK_COLUMNS}
    d["cancel_requested"] = bool(t.get("cancel_requested"))
    d["event_size"] = events_size(d["id"])
    return d


async def from_db[T](what: str, call: Coroutine[Any, Any, T], fallback: T) -> T:
    try:
        return await call
    except Exception as e:
        state.health["db"] = False
        log.warning("db_read_failed", what=what, error=type(e).__name__, detail=str(e))
        return fallback


async def find(tid: str) -> dict[str, Any]:
    t = state.TASKS.get(tid) or await from_db("task", db.fetch(db.Task, tid), None)
    if t is None:
        raise HTTPException(404, "no such task")
    return t


def owned(tid: str, lease_token: str | None) -> dict[str, Any]:
    t = state.TASKS.get(tid)
    if t is None or not lease_token:
        raise HTTPException(409, "stale lease")
    if t.get("lease_token") is None and t["status"] == TaskStatus.RUNNING:
        t["lease_token"] = lease_token
    if t["lease_token"] != lease_token:
        raise HTTPException(409, "stale lease")
    return t


def touch(worker_id: str, task_id: str | None, now: float) -> None:
    w = state.WORKERS.get(worker_id)
    if w is None:
        state.WORKERS[worker_id] = {
            "first_seen": now,
            "seen_at": now,
            "task_id": task_id,
        }
    else:
        w["seen_at"] = now
        w["task_id"] = task_id


def grab(worker_id: str) -> dict[str, Any] | None:
    now = time.time()
    while state.QUEUE:
        t = state.TASKS.get(state.QUEUE.popleft())
        if t is None or t["status"] != TaskStatus.PENDING:
            continue
        token = uuid.uuid4().hex
        t.update(
            status=TaskStatus.RUNNING,
            worker_id=worker_id,
            lease_token=token,
            started_at=now,
            heartbeat_at=now,
        )
        state.DIRTY.add(t["id"])
        touch(worker_id, t["id"], now)
        lease_wait.record(max(now - t["created_at"], 0.0), {"type": t["type"]})
        return {
            "task_id": t["id"],
            "type": t["type"],
            "payload": t["payload"],
            "lease_token": token,
            "event_offset": events_size(t["id"]),
        }
    return None
