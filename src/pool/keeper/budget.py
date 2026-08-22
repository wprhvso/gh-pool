from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from pool_runners.config import RATE_BLIND_WAIT, RATE_STATUSES, RATE_WINDOW

if TYPE_CHECKING:
    from email.message import Message

_MARKERS = ("rate limit", "abuse detection")


def _number(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class Budget:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._left: float | None = None
        self._reset: float = 0.0
        self._closed: float = 0.0

    def observe(self, headers: Message) -> None:
        left = _number(headers.get("X-RateLimit-Remaining"))
        if left is None:
            return
        reset = _number(headers.get("X-RateLimit-Reset"))
        now = time.monotonic()
        with self._lock:
            self._left = left
            if reset is not None:
                self._reset = now + max(0.0, reset - time.time())

    def refuse(self, status: int, headers: Message, body: str) -> float:
        if status not in RATE_STATUSES:
            return 0.0

        wait = _number(headers.get("Retry-After"))
        if wait is None:
            left = _number(headers.get("X-RateLimit-Remaining"))
            reset = _number(headers.get("X-RateLimit-Reset"))
            if left == 0 and reset is not None:
                wait = reset - time.time()
            elif any(mark in body.lower() for mark in _MARKERS):
                wait = RATE_BLIND_WAIT
        if wait is None:
            return 0.0

        wait = min(max(wait, 1.0), RATE_WINDOW)
        now = time.monotonic()
        with self._lock:
            self._left = 0.0
            self._closed = max(self._closed, now + wait)
            return self._closed - now

    def shut(self) -> float:
        with self._lock:
            return max(0.0, self._closed - time.monotonic())

    def state(self) -> str:
        with self._lock:
            left = "?" if self._left is None else f"{self._left:.0f}"
            window = max(0.0, self._reset - time.monotonic())
        return f"остаток {left}, окно ещё {window / 60:.0f} мин"


REST = Budget()
