from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"

    @property
    def live(self) -> bool:
        return self in LIVE

    @property
    def reportable(self) -> bool:
        return self in REPORTABLE

    @property
    def finished(self) -> bool:
        return self not in LIVE


LIVE = frozenset({TaskStatus.PENDING, TaskStatus.RUNNING})
REPORTABLE = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED})
FINISHED = frozenset({*REPORTABLE, TaskStatus.LOST})
