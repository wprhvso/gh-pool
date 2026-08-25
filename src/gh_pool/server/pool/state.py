import asyncio
import time
from collections import deque
from typing import Any

from gh_pool.core.config import settings

TASKS: dict[str, dict[str, Any]] = {}
QUEUE: deque[str] = deque()
WORKERS: dict[str, dict[str, Any]] = {}
BLOBS: dict[str, dict[str, Any]] = {}
DIRTY: set[str] = set()
DIRTY_BLOBS: set[str] = set()

new_task = asyncio.Event()
event_locks: dict[str, asyncio.Lock] = {}
flush_lock = asyncio.Lock()
health: dict[str, Any] = {"db": False, "started_at": 0.0}


def boot() -> None:
    health["started_at"] = time.time()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)


def reset() -> None:
    TASKS.clear()
    QUEUE.clear()
    WORKERS.clear()
    BLOBS.clear()
    DIRTY.clear()
    DIRTY_BLOBS.clear()
    event_locks.clear()
    health["db"] = False
