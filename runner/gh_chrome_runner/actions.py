from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from gh_chrome_protocol import (
    CommandArgs,
    CommandError,
    ErrorCode,
    Method,
    SessionParams,
)

from gh_chrome_runner import extract, files, navigation, subscriptions, tabs, waits
from gh_chrome_runner.cdp import Cdp, CdpError
from gh_chrome_runner.display import Display
from gh_chrome_runner.http import ServerClient
from gh_chrome_runner.input import Input
from gh_chrome_runner.locate import ElementIntercepted, ElementMissing
from gh_chrome_runner.tabs import Tabs

log = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[Any]]


class Actions:
    def __init__(
        self, cdp: Cdp, display: Display, server: ServerClient, params: SessionParams
    ) -> None:
        self.cdp = cdp
        self.params = params
        self.tabs = Tabs(cdp)
        self.input = Input(cdp, display, self.tabs, params)
        self.files = files.Files(cdp, server, self.tabs)
        self.subscriptions = subscriptions.Subscriptions(cdp, server, self.tabs, self.files)
        self._handlers: dict[Method, Handler] = {
            Method.GOTO: lambda a: navigation.goto(self.tabs, a),
            Method.BACK: lambda a: navigation.back(self.tabs),
            Method.FORWARD: lambda a: navigation.forward(self.tabs),
            Method.RELOAD: lambda a: navigation.reload(self.tabs),
            Method.NEW_TAB: lambda a: tabs.new_tab(self.tabs, a),
            Method.ACTIVATE: lambda a: tabs.activate(self.tabs, a),
            Method.CLOSE_TAB: lambda a: tabs.close_tab(self.tabs, a),
            Method.TABS: lambda a: tabs.list_tabs(self.tabs),
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
            Method.TEXT: lambda a: extract.text(self.tabs, a.selector),
            Method.HTML: lambda a: extract.html(self.tabs, a.selector),
            Method.ATTR: lambda a: extract.attr(self.tabs, a.selector, a.name),
            Method.VALUE: lambda a: extract.value(self.tabs, a.selector),
            Method.URL: lambda a: extract.url(self.tabs),
            Method.TITLE: lambda a: extract.title(self.tabs),
            Method.EVAL: lambda a: extract.evaluate(self.tabs, a.expression),
            Method.SCREENSHOT: lambda a: extract.screenshot(self.tabs),
            Method.WAIT_FOR: lambda a: waits.wait_for(self.tabs, a.selector, a.state),
            Method.WAIT_FOR_HIDDEN: lambda a: waits.wait_for_hidden(self.tabs, a.selector),
            Method.WAIT_FOR_URL: lambda a: waits.wait_for_url(self.tabs, a.pattern),
            Method.WAIT_FOR_LOAD: lambda a: waits.wait_for_load(self.tabs, a.wait_until),
            Method.WAIT_FOR_FUNCTION: lambda a: waits.wait_for_function(self.tabs, a.expression),
            Method.SUBSCRIBE: lambda a: self.subscriptions.enable(a.topics),
        }

    async def start(self) -> None:
        await self.tabs.start()
        await self.input.start()
        if self.params.subscribe:
            await self.subscriptions.enable(self.params.subscribe)

    async def stop(self) -> None:
        await self.input.stop()
        await self.tabs.stop()

    async def dispatch(self, args: CommandArgs) -> Any:
        return await self._handlers[args.method](args)

    def to_error(self, exc: Exception) -> CommandError:
        if isinstance(exc, ElementMissing):
            return CommandError(code=ErrorCode.NOT_FOUND, message=str(exc))
        if isinstance(exc, ElementIntercepted):
            return CommandError(code=ErrorCode.INTERCEPTED, message=str(exc))
        if isinstance(exc, navigation.NavigationFailed):
            return CommandError(code=ErrorCode.NAVIGATION_FAILED, message=str(exc))
        if isinstance(exc, TimeoutError):
            return CommandError(code=ErrorCode.TIMEOUT, message=str(exc) or "timed out")
        if isinstance(exc, CdpError):
            return CommandError(code=ErrorCode.RUNNER_ERROR, message=str(exc))
        log.exception("unhandled command failure")
        return CommandError(code=ErrorCode.RUNNER_ERROR, message=repr(exc))
