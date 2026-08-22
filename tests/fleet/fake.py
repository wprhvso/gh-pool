from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pool.fleet.runners.errors import HttpError
from pool.fleet.runners.models import Session, Stats

QUEUE_TYPE = "RunnerScaleSetJobMessages"


def job(request_id: int, kind: str = "JobAvailable", **extra: Any) -> dict[str, Any]:
    return {
        "messageType": kind,
        "runnerRequestId": request_id,
        "jobDisplayName": f"job-{request_id}",
        "workflowRunId": 100 + request_id,
        **extra,
    }


def message(
    message_id: int, body: list[dict[str, Any]], stats: Stats
) -> dict[str, Any]:
    return {
        "messageId": message_id,
        "messageType": QUEUE_TYPE,
        "body": json.dumps(body),
        "statistics": {
            "totalAvailableJobs": stats.available,
            "totalAcquiredJobs": stats.acquired,
            "totalAssignedJobs": stats.assigned,
            "totalRunningJobs": stats.running,
            "totalRegisteredRunners": stats.registered,
            "totalBusyRunners": stats.busy,
            "totalIdleRunners": stats.idle,
        },
    }


class FakeScaleSet:
    def __init__(self, target: Any, stats: Stats | None = None) -> None:
        self.target = target
        self.stats = stats or Stats()
        self.messages: list[dict[str, Any]] = []
        self.acquired: list[int] = []
        self.acked: list[int] = []
        self.jits: list[str] = []
        self.sessions: list[str] = []
        self.closed: list[str] = []
        self.dropped: list[int] = []
        self.forgotten: list[int] = []
        self.polls = 0
        self.lock = threading.Lock()

    def ensure(self, name: str) -> tuple[dict[str, Any], bool]:
        return {"id": 42, "name": name}, True

    def drop(self, scale_set_id: int) -> None:
        self.dropped.append(scale_set_id)

    def jit(self, _scale_set_id: int) -> tuple[int, str, str]:
        name = f"pool-{uuid.uuid4().hex[:6]}"
        self.jits.append(name)
        return len(self.jits), name, f"джит-для-{name}"

    def forget(self, runner_id: int) -> bool:
        self.forgotten.append(runner_id)
        return True

    def statistics(self, _scale_set_id: int) -> Stats:
        with self.lock:
            return self.stats

    def open(self, scale_set_id: int, owner: str) -> Session:
        session = Session(
            session_id=f"s-{len(self.sessions)}",
            queue_url="https://queue.example/x?token=1",
            queue_token="токен",
            queue_token_exp=time.time() + 3600,
            stats=self.stats,
        )
        self.sessions.append(session.session_id)
        del scale_set_id, owner
        return session

    def reopen(
        self, scale_set_id: int, _session: Session | None, owner: str
    ) -> Session:
        return self.open(scale_set_id, owner)

    def refresh(self, scale_set_id: int, session: Session) -> Session:
        return self.open(scale_set_id, session.session_id)

    def close(self, _scale_set_id: int, session: Session) -> None:
        self.closed.append(session.session_id)

    def offer(self, *items: dict[str, Any], stats: Stats | None = None) -> None:
        with self.lock:
            if stats is not None:
                self.stats = stats
            self.messages.append(
                message(len(self.messages) + 1, list(items), self.stats)
            )

    def poll(
        self, _session: Session, _last_message_id: int = 0, _capacity: int = 0
    ) -> dict[str, Any] | None:
        self.polls += 1
        with self.lock:
            if self.messages:
                return self.messages.pop(0)
        time.sleep(0.02)
        return None

    def ack(self, _session: Session, message_id: int) -> None:
        self.acked.append(message_id)

    def acquirable(self, _scale_set_id: int) -> list[dict[str, Any]]:
        return []

    def acquire(
        self, _scale_set_id: int, _session: Session, request_ids: list[int]
    ) -> int:
        self.acquired.extend(request_ids)
        return len(request_ids)


@dataclass
class Task:
    id: str
    code: str
    entry: str
    kwargs: dict[str, Any]
    timeout: float | None = None
    status: str = "pending"
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    outcome: Any = None


class FakePool:
    def __init__(self, server: Any = None, run: bool = True) -> None:
        self.url = getattr(server, "url", "https://pool.example")
        self.tasks: dict[str, Task] = {}
        self.cancelled: list[str] = []
        self.run = run
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    def submit(
        self,
        code: str,
        entry: str,
        kwargs: dict[str, Any],
        timeout: float | None = None,
    ) -> str:
        task = Task(
            id=uuid.uuid4().hex, code=code, entry=entry, kwargs=kwargs, timeout=timeout
        )
        with self.lock:
            self.tasks[task.id] = task
        if self.run:
            thread = threading.Thread(target=self._execute, args=(task,), daemon=True)
            self.threads.append(thread)
            thread.start()
        return task.id

    def _execute(self, task: Task) -> None:
        task.status = "running"

        def emit(kind: str, value: Any = None, **fields: Any) -> None:
            task.events.append({"kind": kind, "value": value, **fields})

        scope: dict[str, Any] = {
            "__name__": "__pool__",
            "emit": emit,
            "args": [],
            "kwargs": task.kwargs,
        }
        try:
            exec(compile(task.code, f"<{task.entry}>", "exec"), scope)  # noqa: S102
            task.outcome = scope[task.entry](**task.kwargs)
        except Exception as exc:
            task.status, task.error = "failed", f"{type(exc).__name__}: {exc}"
            return
        task.status = "done"

    def state(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            task = self.tasks[task_id]
        return {"id": task.id, "status": task.status, "error": task.error}

    def cancel(self, task_id: str) -> dict[str, Any]:
        self.cancelled.append(task_id)
        with self.lock:
            task = self.tasks.get(task_id)
        if task is not None and task.status in ("pending", "running"):
            task.status = "cancelled"
        return {"status": "cancelled"}

    def tail(self, task_id: str, _limit: int = 4000) -> str:
        with self.lock:
            task = self.tasks.get(task_id)
        return "" if task is None else json.dumps(task.events[-3:], ensure_ascii=False)

    def mine(self, slug: str, label: str) -> list[tuple[str, str, str]]:
        with self.lock:
            return [
                (task.id, str(task.kwargs.get("name") or "?"), task.status)
                for task in self.tasks.values()
                if task.status in ("pending", "running")
                and task.kwargs.get("slug") == slug
                and task.kwargs.get("label", label) == label
            ]

    def workers(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                {"id": f"w-{task.id[:4]}", "task_id": task.id, "idle_for": 1.0}
                for task in self.tasks.values()
                if task.status == "running"
            ]

    def health(self) -> dict[str, Any]:
        return {"ok": True, "tasks": {}, "workers": len(self.workers())}

    def wait(self, seconds: float = 20.0) -> None:
        for thread in list(self.threads):
            thread.join(seconds)

    def done(self) -> list[Task]:
        with self.lock:
            return [task for task in self.tasks.values() if task.status == "done"]


def refused(status: int, url: str = "https://api.github.com/x") -> HttpError:
    return HttpError(status, url, "нет")
