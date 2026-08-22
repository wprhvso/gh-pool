from __future__ import annotations

from dataclasses import dataclass
from typing import override


def to_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except ValueError:
        return default


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
            available=to_int(raw.get("totalAvailableJobs")),
            acquired=to_int(raw.get("totalAcquiredJobs")),
            assigned=to_int(raw.get("totalAssignedJobs")),
            running=to_int(raw.get("totalRunningJobs")),
            registered=to_int(raw.get("totalRegisteredRunners")),
            busy=to_int(raw.get("totalBusyRunners")),
            idle=to_int(raw.get("totalIdleRunners")),
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
