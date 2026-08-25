from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from gh_pool.fleet.runners.models import Stats

if TYPE_CHECKING:
    from gh_pool.fleet.runners.api import ScaleSet
    from gh_pool.fleet.runners.config import Target
    from gh_pool.fleet.runners.fleet import Fleet
    from gh_pool.fleet.runners.pool import Pool

TIMEOUT_GRACE = 480.0

CODE = (Path(__file__).resolve().parent / "agent.py").read_text(encoding="utf-8")
FORCE = threading.Event()


class Latest:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._stats: Stats = Stats()

    def set(self, stats: Stats) -> Stats:
        with self._lock:
            self._stats = stats
            return stats

    def get(self) -> Stats:
        with self._lock:
            return self._stats

    def took(self, taken: int) -> Stats:
        with self._lock:
            self._stats = replace(self._stats, assigned=self._stats.assigned + taken)
            return self._stats


@dataclass(frozen=True)
class Ctx:
    api: ScaleSet
    pool: Pool
    target: Target
    scale_set_id: int
    fleet: Fleet
    latest: Latest
    scaling: threading.Lock
    closing: threading.Event

    @property
    def slug(self) -> str:
        return self.target.slug
