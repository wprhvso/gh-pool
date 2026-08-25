from typing import Any

from gh_pool.client.errors import (
    Cancelled,
    CommandTimeout,
    ElementIntercepted,
    ElementNotFound,
    GhChromeError,
    NavigationFailed,
    Rejected,
    RunnerError,
    SessionDead,
    SessionNotReady,
    SessionUnavailable,
    TapError,
    TapRejected,
    TapTimeout,
    TooManySessions,
)
from gh_pool.client.http import Http
from gh_pool.client.session import Command, Session
from gh_pool.client.tap import Captured, Rule, Tap
from gh_pool.client.task import Failed, Pool, Remote, Task
from gh_pool.protocol import (
    ElementState,
    Event,
    EventType,
    ProfileInfo,
    SessionCreate,
    SessionParams,
    SessionStatus,
    Speed,
    Topic,
    WaitUntil,
)

__all__ = [
    "Cancelled",
    "Captured",
    "Command",
    "CommandTimeout",
    "ElementIntercepted",
    "ElementNotFound",
    "ElementState",
    "Event",
    "EventType",
    "Failed",
    "GhChromeError",
    "NavigationFailed",
    "Pool",
    "ProfileInfo",
    "Rejected",
    "Remote",
    "Rule",
    "RunnerError",
    "Session",
    "SessionDead",
    "SessionNotReady",
    "SessionParams",
    "SessionStatus",
    "SessionUnavailable",
    "Speed",
    "Tap",
    "TapError",
    "TapRejected",
    "TapTimeout",
    "Task",
    "TooManySessions",
    "Topic",
    "WaitUntil",
    "new",
    "profiles",
]


async def new(
    *,
    profile: str | None = None,
    persist: bool = True,
    max_sessions: int | None = None,
    close_timeout: float = 120.0,
    server: str | None = None,
    token: str | None = None,
    **params: Any,
) -> Session:
    http = Http(server, token)
    request = SessionCreate(
        profile=profile,
        persist=persist,
        max_sessions=max_sessions,
        params=SessionParams(**params),
    )
    try:
        state = await http.create_session(request)
    except BaseException:
        await http.aclose()
        raise
    return Session(http, state, close_timeout)


async def profiles(
    server: str | None = None, token: str | None = None
) -> list[ProfileInfo]:
    http = Http(server, token)
    try:
        return await http.profiles()
    finally:
        await http.aclose()
