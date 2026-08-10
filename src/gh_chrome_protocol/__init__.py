from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Speed(StrEnum):
    INSTANT = "instant"
    FAST = "fast"
    NORMAL = "normal"
    SLOW = "slow"


class Topic(StrEnum):
    TABS = "tabs"
    DOWNLOADS = "downloads"


class SessionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    CLOSED = "closed"
    DEAD = "dead"

    @property
    def live(self) -> bool:
        return self in {SessionStatus.PENDING, SessionStatus.ACTIVE}


class CloseReason(StrEnum):
    CLOSED = "closed"
    DEAD = "dead"
    TIMEOUT = "timeout"


class SessionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=240, le=2160)
    fps: int = Field(default=15, ge=1, le=60)
    bitrate: str = "2M"
    mouse_speed: Speed = Speed.NORMAL
    type_speed: Speed = Speed.NORMAL
    scroll_speed: Speed = Speed.NORMAL
    timeout: float = Field(default=30.0, gt=0)
    subscribe: list[Topic] = Field(default_factory=list)


class SessionCreate(BaseModel):
    profile: str | None = None
    persist: bool = True
    max_parallel: int | None = Field(default=None, ge=1)
    params: SessionParams = Field(default_factory=SessionParams)


class SessionState(BaseModel):
    id: UUID
    status: SessionStatus
    state_stale: bool
    profile: str | None
    persist: bool
    params: SessionParams
    last_seq: int


class RunnerConfig(BaseModel):
    session_id: UUID
    params: SessionParams
    profile: str | None
    persist: bool
    has_profile_archive: bool
    segment_seconds: float = 1.0


class ProfileInfo(BaseModel):
    name: str
    size: int | None
    stale: bool
    updated_at: str | None


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


class Method(StrEnum):
    GOTO = "goto"
    BACK = "back"
    FORWARD = "forward"
    RELOAD = "reload"
    NEW_TAB = "new_tab"
    ACTIVATE = "activate"
    CLOSE_TAB = "close_tab"
    TABS = "tabs"
    CLICK = "click"
    DBLCLICK = "dblclick"
    RIGHT_CLICK = "right_click"
    HOVER = "hover"
    TYPE = "type"
    PRESS = "press"
    HOTKEY = "hotkey"
    SELECT = "select"
    SCROLL_TO = "scroll_to"
    SCROLL_BY = "scroll_by"
    UPLOAD = "upload"
    TEXT = "text"
    HTML = "html"
    ATTR = "attr"
    VALUE = "value"
    URL = "url"
    TITLE = "title"
    EVAL = "eval"
    SCREENSHOT = "screenshot"
    WAIT_FOR = "wait_for"
    WAIT_FOR_HIDDEN = "wait_for_hidden"
    WAIT_FOR_URL = "wait_for_url"
    WAIT_FOR_LOAD = "wait_for_load"
    WAIT_FOR_FUNCTION = "wait_for_function"
    SUBSCRIBE = "subscribe"


class WaitUntil(StrEnum):
    LOAD = "load"
    DOMCONTENTLOADED = "domcontentloaded"
    NETWORKIDLE = "networkidle"


class ElementState(StrEnum):
    ATTACHED = "attached"
    VISIBLE = "visible"


class Bare(BaseModel):
    method: Literal[
        Method.BACK,
        Method.FORWARD,
        Method.RELOAD,
        Method.TABS,
        Method.URL,
        Method.TITLE,
        Method.SCREENSHOT,
    ]


class Selector(BaseModel):
    method: Literal[
        Method.CLICK,
        Method.DBLCLICK,
        Method.RIGHT_CLICK,
        Method.HOVER,
        Method.SCROLL_TO,
        Method.TEXT,
        Method.VALUE,
        Method.WAIT_FOR_HIDDEN,
    ]
    selector: str


class Index(BaseModel):
    method: Literal[Method.ACTIVATE, Method.CLOSE_TAB]
    index: int = Field(ge=0)


class Expression(BaseModel):
    method: Literal[Method.EVAL, Method.WAIT_FOR_FUNCTION]
    expression: str


class Goto(BaseModel):
    method: Literal[Method.GOTO] = Method.GOTO
    url: str
    wait_until: WaitUntil = WaitUntil.LOAD


class NewTab(BaseModel):
    method: Literal[Method.NEW_TAB] = Method.NEW_TAB
    url: str | None = None


class TypeText(BaseModel):
    method: Literal[Method.TYPE] = Method.TYPE
    selector: str
    text: str
    clear: bool = False


class Press(BaseModel):
    method: Literal[Method.PRESS] = Method.PRESS
    key: str


class Hotkey(BaseModel):
    method: Literal[Method.HOTKEY] = Method.HOTKEY
    keys: list[str] = Field(min_length=1)


class SelectOption(BaseModel):
    method: Literal[Method.SELECT] = Method.SELECT
    selector: str
    value: str


class ScrollBy(BaseModel):
    method: Literal[Method.SCROLL_BY] = Method.SCROLL_BY
    dy: int


class Upload(BaseModel):
    method: Literal[Method.UPLOAD] = Method.UPLOAD
    selector: str
    file_id: str | None = None
    url: str | None = None


class Html(BaseModel):
    method: Literal[Method.HTML] = Method.HTML
    selector: str | None = None


class Attr(BaseModel):
    method: Literal[Method.ATTR] = Method.ATTR
    selector: str
    name: str


class WaitFor(BaseModel):
    method: Literal[Method.WAIT_FOR] = Method.WAIT_FOR
    selector: str
    state: ElementState = ElementState.VISIBLE


class WaitForUrl(BaseModel):
    method: Literal[Method.WAIT_FOR_URL] = Method.WAIT_FOR_URL
    pattern: str


class WaitForLoad(BaseModel):
    method: Literal[Method.WAIT_FOR_LOAD] = Method.WAIT_FOR_LOAD
    wait_until: WaitUntil = WaitUntil.LOAD


class Subscribe(BaseModel):
    method: Literal[Method.SUBSCRIBE] = Method.SUBSCRIBE
    topics: list[Topic]


CommandArgs = Annotated[
    Bare
    | Selector
    | Index
    | Expression
    | Goto
    | NewTab
    | TypeText
    | Press
    | Hotkey
    | SelectOption
    | ScrollBy
    | Upload
    | Html
    | Attr
    | WaitFor
    | WaitForUrl
    | WaitForLoad
    | Subscribe,
    Field(discriminator="method"),
]


class CommandRequest(BaseModel):
    args: CommandArgs
    timeout: float | None = Field(default=None, gt=0)


class CommandAccepted(BaseModel):
    command_id: UUID
    seq: int


class CommandEnvelope(BaseModel):
    command_id: UUID
    seq: int
    args: CommandArgs
    timeout_ms: int


class CommandResult(BaseModel):
    command_id: UUID
    result: object = None
    error: object = None


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
