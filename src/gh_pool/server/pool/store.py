import time

import structlog

from gh_pool.db import tasks as db
from gh_pool.server.pool import state
from gh_pool.status import FINISHED, TaskStatus

log = structlog.get_logger()


async def flush() -> None:
    async with state.flush_lock:
        ids, keys = list(state.DIRTY), list(state.DIRTY_BLOBS)
        if not ids and not keys:
            return
        state.DIRTY.difference_update(ids)
        state.DIRTY_BLOBS.difference_update(keys)
        try:
            await db.save(
                db.Task,
                [
                    {c: state.TASKS[i].get(c) for c in db.TASK_COLUMNS}
                    for i in ids
                    if i in state.TASKS
                ],
            )
            await db.save(
                db.Artifact, [state.BLOBS[k] for k in keys if k in state.BLOBS]
            )
        except Exception as e:
            state.DIRTY.update(ids)
            state.DIRTY_BLOBS.update(keys)
            state.health["db"] = False
            log.warning(
                "flush_failed",
                error=type(e).__name__,
                detail=str(e),
                tasks=len(ids),
                blobs=len(keys),
            )
            return
        state.health["db"] = True
        for i in ids:
            if (
                i not in state.DIRTY
                and state.TASKS.get(i, {}).get("status") in FINISHED
            ):
                state.TASKS.pop(i, None)
        for k in keys:
            if k not in state.DIRTY_BLOBS:
                state.BLOBS.pop(k, None)


async def recover() -> None:
    running = 0
    for t in await db.unfinished():
        if t["id"] in state.TASKS:
            continue
        state.TASKS[t["id"]] = t
        if t["status"] == TaskStatus.RUNNING:
            t.update(heartbeat_at=time.time(), lease_token=None)
            running += 1
        else:
            state.QUEUE.append(t["id"])
    if state.QUEUE:
        state.new_task.set()
    log.info("recovered", pending=len(state.QUEUE), running=running)
