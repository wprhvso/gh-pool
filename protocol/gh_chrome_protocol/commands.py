from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from gh_chrome_protocol.session import Topic


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


class GotoArgs(BaseModel):
    method: Literal[Method.GOTO] = Method.GOTO
    url: str
    wait_until: WaitUntil = WaitUntil.LOAD


class BackArgs(BaseModel):
    method: Literal[Method.BACK] = Method.BACK


class ForwardArgs(BaseModel):
    method: Literal[Method.FORWARD] = Method.FORWARD


class ReloadArgs(BaseModel):
    method: Literal[Method.RELOAD] = Method.RELOAD


class NewTabArgs(BaseModel):
    method: Literal[Method.NEW_TAB] = Method.NEW_TAB
    url: str | None = None


class ActivateArgs(BaseModel):
    method: Literal[Method.ACTIVATE] = Method.ACTIVATE
    index: int = Field(ge=0)


class CloseTabArgs(BaseModel):
    method: Literal[Method.CLOSE_TAB] = Method.CLOSE_TAB
    index: int = Field(ge=0)


class TabsArgs(BaseModel):
    method: Literal[Method.TABS] = Method.TABS


class ClickArgs(BaseModel):
    method: Literal[Method.CLICK] = Method.CLICK
    selector: str


class DblclickArgs(BaseModel):
    method: Literal[Method.DBLCLICK] = Method.DBLCLICK
    selector: str


class RightClickArgs(BaseModel):
    method: Literal[Method.RIGHT_CLICK] = Method.RIGHT_CLICK
    selector: str


class HoverArgs(BaseModel):
    method: Literal[Method.HOVER] = Method.HOVER
    selector: str


class TypeArgs(BaseModel):
    method: Literal[Method.TYPE] = Method.TYPE
    selector: str
    text: str
    clear: bool = False


class PressArgs(BaseModel):
    method: Literal[Method.PRESS] = Method.PRESS
    key: str


class HotkeyArgs(BaseModel):
    method: Literal[Method.HOTKEY] = Method.HOTKEY
    keys: list[str] = Field(min_length=1)


class SelectArgs(BaseModel):
    method: Literal[Method.SELECT] = Method.SELECT
    selector: str
    value: str


class ScrollToArgs(BaseModel):
    method: Literal[Method.SCROLL_TO] = Method.SCROLL_TO
    selector: str


class ScrollByArgs(BaseModel):
    method: Literal[Method.SCROLL_BY] = Method.SCROLL_BY
    dy: int


class UploadArgs(BaseModel):
    method: Literal[Method.UPLOAD] = Method.UPLOAD
    selector: str
    file_id: str | None = None
    url: str | None = None


class TextArgs(BaseModel):
    method: Literal[Method.TEXT] = Method.TEXT
    selector: str


class HtmlArgs(BaseModel):
    method: Literal[Method.HTML] = Method.HTML
    selector: str | None = None


class AttrArgs(BaseModel):
    method: Literal[Method.ATTR] = Method.ATTR
    selector: str
    name: str


class ValueArgs(BaseModel):
    method: Literal[Method.VALUE] = Method.VALUE
    selector: str


class UrlArgs(BaseModel):
    method: Literal[Method.URL] = Method.URL


class TitleArgs(BaseModel):
    method: Literal[Method.TITLE] = Method.TITLE


class EvalArgs(BaseModel):
    method: Literal[Method.EVAL] = Method.EVAL
    expression: str


class ScreenshotArgs(BaseModel):
    method: Literal[Method.SCREENSHOT] = Method.SCREENSHOT


class WaitForArgs(BaseModel):
    method: Literal[Method.WAIT_FOR] = Method.WAIT_FOR
    selector: str
    state: ElementState = ElementState.VISIBLE


class WaitForHiddenArgs(BaseModel):
    method: Literal[Method.WAIT_FOR_HIDDEN] = Method.WAIT_FOR_HIDDEN
    selector: str


class WaitForUrlArgs(BaseModel):
    method: Literal[Method.WAIT_FOR_URL] = Method.WAIT_FOR_URL
    pattern: str


class WaitForLoadArgs(BaseModel):
    method: Literal[Method.WAIT_FOR_LOAD] = Method.WAIT_FOR_LOAD
    wait_until: WaitUntil = WaitUntil.LOAD


class WaitForFunctionArgs(BaseModel):
    method: Literal[Method.WAIT_FOR_FUNCTION] = Method.WAIT_FOR_FUNCTION
    expression: str


class SubscribeArgs(BaseModel):
    method: Literal[Method.SUBSCRIBE] = Method.SUBSCRIBE
    topics: list[Topic]


CommandArgs = Annotated[
    GotoArgs
    | BackArgs
    | ForwardArgs
    | ReloadArgs
    | NewTabArgs
    | ActivateArgs
    | CloseTabArgs
    | TabsArgs
    | ClickArgs
    | DblclickArgs
    | RightClickArgs
    | HoverArgs
    | TypeArgs
    | PressArgs
    | HotkeyArgs
    | SelectArgs
    | ScrollToArgs
    | ScrollByArgs
    | UploadArgs
    | TextArgs
    | HtmlArgs
    | AttrArgs
    | ValueArgs
    | UrlArgs
    | TitleArgs
    | EvalArgs
    | ScreenshotArgs
    | WaitForArgs
    | WaitForHiddenArgs
    | WaitForUrlArgs
    | WaitForLoadArgs
    | WaitForFunctionArgs
    | SubscribeArgs,
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
