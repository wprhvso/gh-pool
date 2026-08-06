from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from gh_chrome_protocol.errors import CommandError
from gh_chrome_protocol.session import CloseReason


class EventType(StrEnum):
    SESSION_READY = "session_ready"
    SESSION_CLOSED = "session_closed"
    COMMAND_STARTED = "command_started"
    COMMAND_FINISHED = "command_finished"
    COMMAND_FAILED = "command_failed"
    TAB_OPENED = "tab_opened"
    TAB_CLOSED = "tab_closed"
    TAB_ACTIVATED = "tab_activated"
    DOWNLOAD = "download"


class SessionReady(BaseModel):
    type: Literal[EventType.SESSION_READY] = EventType.SESSION_READY
    state_stale: bool


class SessionClosed(BaseModel):
    type: Literal[EventType.SESSION_CLOSED] = EventType.SESSION_CLOSED
    reason: CloseReason


class CommandStarted(BaseModel):
    type: Literal[EventType.COMMAND_STARTED] = EventType.COMMAND_STARTED
    command_id: UUID


class CommandFinished(BaseModel):
    type: Literal[EventType.COMMAND_FINISHED] = EventType.COMMAND_FINISHED
    command_id: UUID
    result: object = None


class CommandFailed(BaseModel):
    type: Literal[EventType.COMMAND_FAILED] = EventType.COMMAND_FAILED
    command_id: UUID
    error: CommandError


class TabOpened(BaseModel):
    type: Literal[EventType.TAB_OPENED] = EventType.TAB_OPENED
    index: int
    url: str
    active: bool


class TabClosed(BaseModel):
    type: Literal[EventType.TAB_CLOSED] = EventType.TAB_CLOSED
    index: int


class TabActivated(BaseModel):
    type: Literal[EventType.TAB_ACTIVATED] = EventType.TAB_ACTIVATED
    index: int


class Download(BaseModel):
    type: Literal[EventType.DOWNLOAD] = EventType.DOWNLOAD
    name: str
    size: int
    url: str


EventData = Annotated[
    SessionReady
    | SessionClosed
    | CommandStarted
    | CommandFinished
    | CommandFailed
    | TabOpened
    | TabClosed
    | TabActivated
    | Download,
    Field(discriminator="type"),
]


class Event(BaseModel):
    seq: int
    data: EventData


class RunnerEvent(BaseModel):
    data: EventData
