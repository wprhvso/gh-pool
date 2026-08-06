from __future__ import annotations

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

from gh_chrome_client.command import Command
from gh_chrome_client.errors import (
    Cancelled,
    CommandTimeout,
    ConnectionLost,
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
from gh_chrome_client.session import Session

__all__ = [
    "Cancelled",
    "Command",
    "CommandTimeout",
    "ConnectionLost",
    "ElementIntercepted",
    "ElementNotFound",
    "ElementState",
    "Event",
    "EventType",
    "GhChromeError",
    "Http",
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
    width: int = 1920,
    height: int = 1080,
    fps: int = 15,
    bitrate: str = "2M",
    mouse_speed: Speed = Speed.NORMAL,
    type_speed: Speed = Speed.NORMAL,
    scroll_speed: Speed = Speed.NORMAL,
    timeout: float = 30.0,
    subscribe: list[Topic] | None = None,
    max_parallel: int | None = None,
    close_timeout: float = 120.0,
    server: str | None = None,
    token: str | None = None,
) -> Session:
    http = Http(server, token)
    request = SessionCreate(
        profile=profile,
        persist=persist,
        max_parallel=max_parallel,
        params=SessionParams(
            width=width,
            height=height,
            fps=fps,
            bitrate=bitrate,
            mouse_speed=mouse_speed,
            type_speed=type_speed,
            scroll_speed=scroll_speed,
            timeout=timeout,
            subscribe=subscribe or [],
        ),
    )
    try:
        state = await http.create_session(request)
    except BaseException:
        await http.aclose()
        raise
    session = Session(http, state, close_timeout)
    session._start()
    return session


async def profiles(server: str | None = None, token: str | None = None) -> list[ProfileInfo]:
    http = Http(server, token)
    try:
        return await http.profiles()
    finally:
        await http.aclose()
