from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ErrorCode(StrEnum):
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    INTERCEPTED = "intercepted"
    NAVIGATION_FAILED = "navigation_failed"
    CANCELLED = "cancelled"
    SESSION_DEAD = "session_dead"
    RUNNER_ERROR = "runner_error"


class CommandError(BaseModel):
    code: ErrorCode
    message: str
