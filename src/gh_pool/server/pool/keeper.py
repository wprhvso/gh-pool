import asyncio
import time

import structlog

from gh_pool.core.config import settings
from gh_pool.server.pool import state
from gh_pool.server.pool.metrics import tasks_lost
from gh_pool.server.pool.store import flush, recover
from gh_pool.status import TaskStatus

log = structlog.get_logger()


async def keeper() -> None:
    started = False
    while True:
        if not started:
            try:
                await recover()
                started = state.health["db"] = True
            except Exception as e:
                log.warning("db_unavailable", error=type(e).__name__, detail=str(e))
        now = time.time()
        for t in list(state.TASKS.values()):
            if (
                t["status"] == TaskStatus.RUNNING
                and t.get("heartbeat_at", now) < now - settings.lost_after
            ):
                t.update(
                    status=TaskStatus.LOST,
                    error="worker gone",
                    finished_at=now,
                    lease_token=None,
                )
                state.DIRTY.add(t["id"])
                state.event_locks.pop(t["id"], None)
                tasks_lost.add(1, {"reason": "worker_gone"})
                log.warning(
                    "task_lost",
                    task=t["id"],
                    type=t["type"],
                    worker=t["worker_id"],
                    quiet_for=round(now - t["heartbeat_at"], 1),
                )
        for wid, w in list(state.WORKERS.items()):
            if w["seen_at"] < now - settings.worker_stale:
                state.WORKERS.pop(wid, None)
                log.info(
                    "worker_gone",
                    worker=wid,
                    task=w.get("task_id"),
                    quiet_for=round(now - w["seen_at"], 1),
                )
        await flush()
        await asyncio.sleep(settings.flush_every)
