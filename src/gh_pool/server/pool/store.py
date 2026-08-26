import time

import structlog

from gh_pool.db import tasks as db
from gh_pool.server.pool import state
from gh_pool.status import FINISHED, TaskStatus

log = structlog.get_logger()


def pending() -> int:
    return state.current.pending()


def overloaded() -> bool:
    return state.current.overloaded()


async def flush() -> bool:
    async with state.current.flush_lock:
        ids, keys = list(state.current.dirty), list(state.current.dirty_blobs)
        if not ids and not keys:
            return True
        state.current.dirty.difference_update(ids)
        state.current.dirty_blobs.difference_update(keys)
        try:
            await db.save(
                db.Task,
                [
                    {c: state.current.tasks[i].get(c) for c in db.TASK_COLUMNS}
                    for i in ids
                    if i in state.current.tasks
                ],
            )
            await db.save(
                db.Artifact,
                [state.current.blobs[k] for k in keys if k in state.current.blobs],
            )
        except Exception as e:
            state.current.dirty.update(ids)
            state.current.dirty_blobs.update(keys)
            state.current.db_ok = False
            log.warning(
                "flush_failed",
                error=type(e).__name__,
                detail=str(e),
                tasks=len(ids),
                blobs=len(keys),
                pending=pending(),
            )
            return False
        state.current.db_ok = True
        for i in ids:
            if (
                i not in state.current.dirty
                and state.current.tasks.get(i, {}).get("status") in FINISHED
            ):
                state.current.tasks.pop(i, None)
        for k in keys:
            if k not in state.current.dirty_blobs:
                state.current.blobs.pop(k, None)
        return True


async def recover() -> None:
    running = 0
    for t in await db.unfinished():
        if t["id"] in state.current.tasks:
            continue
        state.current.tasks[t["id"]] = t
        if t["status"] == TaskStatus.RUNNING:
            t.update(heartbeat_at=time.time(), lease_token=None)
            running += 1
        else:
            state.current.queue.append(t["id"])
    if state.current.queue:
        state.current.arrived.set()
    log.info("recovered", pending=len(state.current.queue), running=running)
