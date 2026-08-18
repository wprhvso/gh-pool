from __future__ import annotations

from dataclasses import dataclass
from typing import override


@dataclass(frozen=True)
class Stats:
    available: int = 0
    acquired: int = 0
    assigned: int = 0
    running: int = 0
    registered: int = 0
    busy: int = 0
    idle: int = 0

    @classmethod
    def parse(cls, raw: object) -> Stats:
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            available=int(raw.get("totalAvailableJobs", 0)),
            acquired=int(raw.get("totalAcquiredJobs", 0)),
            assigned=int(raw.get("totalAssignedJobs", 0)),
            running=int(raw.get("totalRunningJobs", 0)),
            registered=int(raw.get("totalRegisteredRunners", 0)),
            busy=int(raw.get("totalBusyRunners", 0)),
            idle=int(raw.get("totalIdleRunners", 0)),
        )

    @override
    def __str__(self) -> str:
        return (
            f"available={self.available} acquired={self.acquired} "
            f"assigned={self.assigned} running={self.running} "
            f"runners={self.registered} busy={self.busy} idle={self.idle}"
        )


@dataclass(frozen=True)
class Session:
    session_id: str
    queue_url: str
    queue_token: str
    queue_token_exp: float
    stats: Stats = Stats()
