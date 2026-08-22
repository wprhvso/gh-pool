import logging
from collections.abc import Awaitable, Callable
from typing import Any

from gh_chrome_protocol import (
    CommandArgs,
    CommandError,
    ErrorCode,
    Method,
    SessionParams,
    Topic,
)
from gh_chrome_runner import dom, navigation
from gh_chrome_runner.cdp import Cdp, CdpError
from gh_chrome_runner.files import Files
from gh_chrome_runner.http import ServerClient
from gh_chrome_runner.input import Input
from gh_chrome_runner.locate import ElementIntercepted, ElementMissing
from gh_chrome_runner.tabs import Tabs
from gh_chrome_runner.xtest import Xtest

log = logging.getLogger(__name__)

ERROR_CODES: tuple[tuple[type[Exception], ErrorCode], ...] = (
    (ElementMissing, ErrorCode.NOT_FOUND),
    (ElementIntercepted, ErrorCode.INTERCEPTED),
    (navigation.NavigationFailed, ErrorCode.NAVIGATION_FAILED),
    (TimeoutError, ErrorCode.TIMEOUT),
    (CdpError, ErrorCode.RUNNER_ERROR),
)


class Actions:
    def __init__(
        self, cdp: Cdp, xtest: Xtest, server: ServerClient, params: SessionParams
    ) -> None:
        self._server = server
        self._params = params
        self.tabs = Tabs(cdp)
        self.input = Input(xtest, self.tabs, params)
        self.files = Files(cdp, server, self.tabs)
        self._handlers: dict[Method, Callable[[Any], Awaitable[Any]]] = {
            Method.GOTO: lambda a: navigation.goto(self.tabs, a),
            Method.BACK: lambda a: navigation.back(self.tabs),
            Method.FORWARD: lambda a: navigation.forward(self.tabs),
            Method.RELOAD: lambda a: navigation.reload(self.tabs),
            Method.NEW_TAB: lambda a: self.tabs.create(a.url),
            Method.ACTIVATE: lambda a: self.tabs.activate(a.index),
            Method.CLOSE_TAB: lambda a: self.tabs.close(a.index),
            Method.TABS: lambda a: self.tabs.snapshot(),
            Method.CLICK: lambda a: self.input.click(a.selector),
            Method.DBLCLICK: lambda a: self.input.click(a.selector, count=2),
            Method.RIGHT_CLICK: lambda a: self.input.click(a.selector, button="right"),
            Method.HOVER: lambda a: self.input.hover(a.selector),
            Method.TYPE: lambda a: self.input.type_into(a.selector, a.text, a.clear),
            Method.PRESS: lambda a: self.input.press(a.key),
            Method.HOTKEY: lambda a: self.input.hotkey(a.keys),
            Method.SELECT: lambda a: self.input.select(a.selector, a.value),
            Method.SCROLL_TO: lambda a: self.input.scroll_to(a.selector),
            Method.SCROLL_BY: lambda a: self.input.scroll_by(a.dy),
            Method.UPLOAD: self.files.upload,
            Method.TEXT: lambda a: dom.text(self.tabs, a.selector),
            Method.HTML: lambda a: dom.html(self.tabs, a.selector),
            Method.ATTR: lambda a: dom.attr(self.tabs, a.selector, a.name),
            Method.VALUE: lambda a: dom.value(self.tabs, a.selector),
            Method.URL: lambda a: dom.url(self.tabs),
            Method.TITLE: lambda a: dom.title(self.tabs),
            Method.EVAL: lambda a: dom.evaluate(self.tabs, a.expression),
            Method.INIT_SCRIPT: lambda a: dom.init_script(self.tabs, a.source),
            Method.SCREENSHOT: lambda a: dom.screenshot(self.tabs),
            Method.WAIT_FOR: lambda a: dom.wait_for(self.tabs, a.selector, a.state),
            Method.WAIT_FOR_HIDDEN: lambda a: dom.wait_for_hidden(
                self.tabs, a.selector
            ),
            Method.WAIT_FOR_URL: lambda a: dom.wait_for_url(self.tabs, a.pattern),
            Method.WAIT_FOR_LOAD: lambda a: dom.wait_for_load(self.tabs, a.wait_until),
            Method.WAIT_FOR_FUNCTION: lambda a: dom.wait_for_function(
                self.tabs, a.expression
            ),
            Method.SUBSCRIBE: lambda a: self.subscribe(a.topics),
        }

    async def start(self) -> None:
        await self.tabs.start()
        if self._params.subscribe:
            await self.subscribe(self._params.subscribe)

    async def stop(self) -> None:
        await self.files.settle()
        self.input.close()
        await self.tabs.stop()

    async def dispatch(self, args: CommandArgs) -> Any:
        return await self._handlers[args.method](args)

    async def subscribe(self, topics: list[Topic]) -> None:
        self.tabs.on_event = self._server.event if Topic.TABS in topics else None
        if Topic.DOWNLOADS in topics:
            self.files.watch()
        else:
            self.files.unwatch()

    def to_error(self, exc: Exception) -> CommandError:
        for error, code in ERROR_CODES:
            if isinstance(exc, error):
                return CommandError(code=code, message=str(exc) or str(code))
        log.error("unhandled command failure", exc_info=exc)
        return CommandError(code=ErrorCode.RUNNER_ERROR, message=repr(exc))
