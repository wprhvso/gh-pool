import time
from typing import TypedDict

from gh_pool.server.pool import state


class Report(TypedDict):
    ok: bool
    tasks: dict[str, int]
    queue: int
    workers: int
    started_at: float
    uptime: float
    pending_writes: int
    db: bool


def report() -> Report:
    counts: dict[str, int] = {}
    for t in state.current.tasks.values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return Report(
        ok=True,
        tasks=counts,
        queue=len(state.current.queue),
        workers=len(state.current.workers),
        started_at=state.current.started_at,
        uptime=round(time.time() - state.current.started_at, 1),
        pending_writes=len(state.current.dirty) + len(state.current.dirty_blobs),
        db=state.current.db_ok,
    )
