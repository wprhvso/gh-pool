"""Drive a Chrome running on a GitHub Actions runner, over plain HTTPS."""

from typing import Any

from gh_chrome_client.errors import (
    Cancelled,
    CommandTimeout,
    ElementIntercepted,
    ElementNotFound,
    GhChromeError,
    NavigationFailed,
    RunnerError,
    SessionDead,
    SessionNotReady,
    SessionUnavailable,
    TooManySessions,
)
from gh_chrome_client.http import Http
from gh_chrome_client.session import Command, Session
from gh_chrome_protocol import (
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
    "Command",
    "CommandTimeout",
    "ElementIntercepted",
    "ElementNotFound",
    "ElementState",
    "Event",
    "EventType",
    "GhChromeError",
    "NavigationFailed",
    "ProfileInfo",
    "RunnerError",
    "Session",
    "SessionDead",
    "SessionNotReady",
    "SessionParams",
    "SessionStatus",
    "SessionUnavailable",
    "Speed",
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
    max_parallel: int | None = None,
    close_timeout: float = 120.0,
    server: str | None = None,
    token: str | None = None,
    **params: Any,
) -> Session:
    """Ask the server for a session; the runner connects a minute or so later.

    Extra keyword arguments are SessionParams fields (width, height, fps,
    bitrate, mouse_speed, type_speed, scroll_speed, timeout, subscribe).
    """
    http = Http(server, token)
    request = SessionCreate(
        profile=profile,
        persist=persist,
        max_parallel=max_parallel,
        params=SessionParams(**params),
    )
    try:
        state = await http.create_session(request)
    except BaseException:
        await http.aclose()
        raise
    return Session(http, state, close_timeout)


async def profiles(server: str | None = None, token: str | None = None) -> list[ProfileInfo]:
    http = Http(server, token)
    try:
        return await http.profiles()
    finally:
        await http.aclose()
