from collections.abc import Iterable

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from gh_pool.server.pool import state

meter = metrics.get_meter("gh_pool.server")


def _observe_queue(options: CallbackOptions) -> Iterable[Observation]:
    return [Observation(len(state.current.queue))]


def _observe_workers(options: CallbackOptions) -> Iterable[Observation]:
    busy = sum(1 for w in state.current.workers.values() if w.get("task_id"))
    return [
        Observation(busy, {"state": "busy"}),
        Observation(len(state.current.workers) - busy, {"state": "idle"}),
    ]


def _observe_tasks(options: CallbackOptions) -> Iterable[Observation]:
    counts: dict[str, int] = {}
    for t in list(state.current.tasks.values()):
        status = t["status"]
        counts[status] = counts.get(status, 0) + 1
    return [Observation(n, {"status": s}) for s, n in counts.items()]


queue_depth = meter.create_observable_gauge(
    "pool.queue.depth",
    callbacks=[_observe_queue],
    unit="{task}",
    description="Tasks waiting to be leased",
)
worker_gauge = meter.create_observable_gauge(
    "pool.workers",
    callbacks=[_observe_workers],
    unit="{worker}",
    description="Known workers by lease state",
)
task_gauge = meter.create_observable_gauge(
    "pool.tasks",
    callbacks=[_observe_tasks],
    unit="{task}",
    description="In-memory tasks by status",
)
lease_wait = meter.create_histogram(
    "pool.task.lease.wait",
    unit="s",
    description="Time a task spent queued before it was leased",
)
tasks_created = meter.create_counter(
    "pool.tasks.created",
    unit="{task}",
    description="Tasks accepted into the queue",
)
tasks_completed = meter.create_counter(
    "pool.tasks.completed",
    unit="{task}",
    description="Tasks that reached a terminal status",
)
tasks_lost = meter.create_counter(
    "pool.tasks.lost",
    unit="{task}",
    description="Tasks declared lost",
)
