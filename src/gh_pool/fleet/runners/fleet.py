from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from gh_pool.status import TaskStatus


@dataclass
class Slot:
    task_id: str
    name: str
    runner_id: int = 0
    born: float = field(default_factory=time.monotonic)
    since: float = field(default_factory=time.monotonic)
    status: str = TaskStatus.PENDING
    spent: bool = False

    def age(self) -> float:
        return time.monotonic() - self.since

    def lived(self) -> float:
        return time.monotonic() - self.born


class Fleet:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._slots: dict[str, Slot] = {}

    def born(self, task_id: str, name: str, runner_id: int = 0) -> Slot:
        slot = Slot(task_id=task_id, name=name, runner_id=runner_id)
        with self._lock:
            self._slots[task_id] = slot
        return slot

    def drop(self, task_id: str) -> Slot | None:
        with self._lock:
            return self._slots.pop(task_id, None)

    def mark(self, task_id: str, status: str) -> None:
        with self._lock:
            slot = self._slots.get(task_id)
            if slot is None or slot.status == status:
                return
            slot.status = status
            slot.since = time.monotonic()

    def size(self) -> int:
        with self._lock:
            return sum(1 for slot in self._slots.values() if not slot.spent)

    def spend(self, name: str) -> bool:
        with self._lock:
            for slot in self._slots.values():
                if slot.name == name and not slot.spent:
                    slot.spent = True
                    return True
        return False

    def slots(self) -> list[Slot]:
        with self._lock:
            return sorted(self._slots.values(), key=lambda slot: slot.born)

    def spare(self) -> list[Slot]:
        with self._lock:
            waiting = [
                slot
                for slot in self._slots.values()
                if slot.status == TaskStatus.PENDING and not slot.spent
            ]
        return sorted(waiting, key=lambda slot: slot.born, reverse=True)

    def tracking(self) -> bool:
        with self._lock:
            return bool(self._slots)
