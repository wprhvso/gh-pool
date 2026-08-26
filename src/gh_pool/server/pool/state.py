import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from gh_pool.core.config import settings


@dataclass(slots=True)
class Pool:
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    queue: deque[str] = field(default_factory=deque)
    workers: dict[str, dict[str, Any]] = field(default_factory=dict)
    blobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    dirty: set[str] = field(default_factory=set)
    dirty_blobs: set[str] = field(default_factory=set)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    event_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    db_ok: bool = False
    started_at: float = 0.0

    def pending(self) -> int:
        return len(self.dirty) + len(self.dirty_blobs)

    def overloaded(self) -> bool:
        return self.pending() >= settings.max_pending_writes


current = Pool()


def boot() -> Pool:
    global current  # noqa: PLW0603
    current = Pool(started_at=time.time())
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    return current


def reset() -> Pool:
    global current  # noqa: PLW0603
    current = Pool(started_at=time.time())
    return current
