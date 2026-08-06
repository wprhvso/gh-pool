from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable
from contextlib import suppress
from inspect import isawaitable
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID

from gh_chrome_protocol import (
    CommandArgs,
    ElementState,
    Event,
    EventType,
    SessionParams,
    SessionState,
    SessionStatus,
    Topic,
    WaitUntil,
)
from gh_chrome_protocol.commands import (
    ActivateArgs,
    AttrArgs,
    BackArgs,
    ClickArgs,
    CloseTabArgs,
    DblclickArgs,
    EvalArgs,
    ForwardArgs,
    GotoArgs,
    HotkeyArgs,
    HoverArgs,
    HtmlArgs,
    NewTabArgs,
    PressArgs,
    ReloadArgs,
    RightClickArgs,
    ScreenshotArgs,
    ScrollByArgs,
    ScrollToArgs,
    SelectArgs,
    SubscribeArgs,
    TabsArgs,
    TextArgs,
    TitleArgs,
    TypeArgs,
    UploadArgs,
    UrlArgs,
    ValueArgs,
    WaitForArgs,
    WaitForFunctionArgs,
    WaitForHiddenArgs,
    WaitForLoadArgs,
    WaitForUrlArgs,
)
from gh_chrome_protocol.events import (
    CommandFailed,
    CommandFinished,
    SessionClosed,
    SessionReady,
)

from gh_chrome_client.command import Command
from gh_chrome_client.errors import GhChromeError, SessionDead, SessionNotReady, to_exception
from gh_chrome_client.http import Http
from gh_chrome_client.stream import EventStream

USER_EVENTS = frozenset(
    {
        EventType.TAB_OPENED,
        EventType.TAB_CLOSED,
        EventType.TAB_ACTIVATED,
        EventType.DOWNLOAD,
        EventType.SESSION_CLOSED,
    }
)


class Session:
    def __init__(self, http: Http, state: SessionState, close_timeout: float) -> None:
        self._http = http
        self._state = state
        self._close_timeout = close_timeout
        self._pending: dict[UUID, Command[Any]] = {}
        self._stash: dict[UUID, Event] = {}
        self._subscribers: set[asyncio.Queue[Event | None]] = set()
        self._ready = asyncio.Event()
        self._finished = asyncio.Event()
        self._closed = False
        self._stream = EventStream(http, state.id, self._on_event)

    @property
    def id(self) -> UUID:
        return self._state.id

    @property
    def params(self) -> SessionParams:
        return self._state.params

    @property
    def state_stale(self) -> bool:
        return self._state.state_stale

    @property
    def player_url(self) -> str:
        return f"/s/{self._state.id}"

    def _start(self) -> None:
        self._stream.start()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def ready(self, timeout: float = 300.0) -> None:
        waiters = (
            asyncio.ensure_future(self._ready.wait()),
            asyncio.ensure_future(self._finished.wait()),
        )
        try:
            done, _ = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in waiters:
                waiter.cancel()
        if self._finished.is_set() and not self._ready.is_set():
            raise SessionDead("session finished before the runner connected")
        if not done:
            raise SessionNotReady(f"runner did not connect in {timeout}s")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._http.close_session(self.id)
            if self._close_timeout > 0:
                with suppress(TimeoutError):
                    async with asyncio.timeout(self._close_timeout):
                        await self._finished.wait()
        finally:
            await self._stream.stop()
            self._drain(SessionDead("session closed"))
            for queue in tuple(self._subscribers):
                queue.put_nowait(None)
            await self._http.aclose()

    async def events(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
                if event.data.type is EventType.SESSION_CLOSED:
                    return
        finally:
            self._subscribers.discard(queue)

    def _on_event(self, event: Event) -> None:
        data = event.data
        if isinstance(data, SessionReady):
            self._state = self._state.model_copy(
                update={"state_stale": data.state_stale, "status": SessionStatus.ACTIVE}
            )
            self._ready.set()
        elif isinstance(data, SessionClosed):
            self._finished.set()
            self._drain(SessionDead(f"session {data.reason}"))
        elif isinstance(data, CommandFinished | CommandFailed):
            command = self._pending.pop(data.command_id, None)
            if command is None:
                self._stash[data.command_id] = event
            elif isinstance(data, CommandFinished):
                command._resolve(data.result)
            else:
                command._fail(to_exception(data.error))
        if data.type in USER_EVENTS:
            for queue in tuple(self._subscribers):
                queue.put_nowait(event)

    def _drain(self, error: BaseException) -> None:
        for command in tuple(self._pending.values()):
            command._fail(error)
        self._pending.clear()

    async def _submit(
        self,
        command: Command[Any],
        args: CommandArgs | Awaitable[CommandArgs],
        timeout: float | None,
    ) -> None:
        if isawaitable(args):
            try:
                args = await args
            except Exception as exc:
                command._fail(GhChromeError(str(exc)))
                return
        if self._finished.is_set():
            command._fail(SessionDead("session is closed"))
            return
        try:
            accepted = await self._http.enqueue(self.id, args, timeout)
        except GhChromeError as exc:
            command._fail(exc)
            return
        command._accepted(accepted.command_id, accepted.seq)
        stashed = self._stash.pop(accepted.command_id, None)
        if stashed is not None:
            data = stashed.data
            if isinstance(data, CommandFinished):
                command._resolve(data.result)
            elif isinstance(data, CommandFailed):
                command._fail(to_exception(data.error))
            return
        if self._finished.is_set():
            command._fail(SessionDead("session is closed"))
            return
        self._pending[accepted.command_id] = command

    def _call[T](
        self, args: CommandArgs | Awaitable[CommandArgs], timeout: float | None
    ) -> Command[T]:
        return cast("Command[T]", Command(self, args, timeout))

    def goto(
        self,
        url: str,
        wait_until: WaitUntil = WaitUntil.LOAD,
        timeout: float | None = None,
    ) -> Command[None]:
        return self._call(GotoArgs(url=url, wait_until=wait_until), timeout)

    def back(self, timeout: float | None = None) -> Command[None]:
        return self._call(BackArgs(), timeout)

    def forward(self, timeout: float | None = None) -> Command[None]:
        return self._call(ForwardArgs(), timeout)

    def reload(self, timeout: float | None = None) -> Command[None]:
        return self._call(ReloadArgs(), timeout)

    def new_tab(self, url: str | None = None, timeout: float | None = None) -> Command[int]:
        return self._call(NewTabArgs(url=url), timeout)

    def activate(self, index: int, timeout: float | None = None) -> Command[None]:
        return self._call(ActivateArgs(index=index), timeout)

    def close_tab(self, index: int, timeout: float | None = None) -> Command[None]:
        return self._call(CloseTabArgs(index=index), timeout)

    def tabs(self, timeout: float | None = None) -> Command[list[dict[str, Any]]]:
        return self._call(TabsArgs(), timeout)

    def click(self, selector: str, timeout: float | None = None) -> Command[None]:
        return self._call(ClickArgs(selector=selector), timeout)

    def dblclick(self, selector: str, timeout: float | None = None) -> Command[None]:
        return self._call(DblclickArgs(selector=selector), timeout)

    def right_click(self, selector: str, timeout: float | None = None) -> Command[None]:
        return self._call(RightClickArgs(selector=selector), timeout)

    def hover(self, selector: str, timeout: float | None = None) -> Command[None]:
        return self._call(HoverArgs(selector=selector), timeout)

    def type(
        self,
        selector: str,
        text: str,
        clear: bool = False,
        timeout: float | None = None,
    ) -> Command[None]:
        return self._call(TypeArgs(selector=selector, text=text, clear=clear), timeout)

    def press(self, key: str, timeout: float | None = None) -> Command[None]:
        return self._call(PressArgs(key=key), timeout)

    def hotkey(self, *keys: str, timeout: float | None = None) -> Command[None]:
        return self._call(HotkeyArgs(keys=list(keys)), timeout)

    def select(self, selector: str, value: str, timeout: float | None = None) -> Command[None]:
        return self._call(SelectArgs(selector=selector, value=value), timeout)

    def scroll_to(self, selector: str, timeout: float | None = None) -> Command[None]:
        return self._call(ScrollToArgs(selector=selector), timeout)

    def scroll_by(self, dy: int, timeout: float | None = None) -> Command[None]:
        return self._call(ScrollByArgs(dy=dy), timeout)

    def upload(
        self,
        selector: str,
        path: Path | str | None = None,
        url: str | None = None,
        timeout: float | None = None,
    ) -> Command[None]:
        if (path is None) == (url is None):
            raise ValueError("pass exactly one of path or url")
        if url is not None:
            return self._call(UploadArgs(selector=selector, url=url), timeout)
        return self._call(self._upload_args(selector, Path(cast("Path | str", path))), timeout)

    async def _upload_args(self, selector: str, path: Path) -> CommandArgs:
        file_id = await self._http.upload_file(self.id, path)
        return UploadArgs(selector=selector, file_id=str(file_id))

    def text(self, selector: str, timeout: float | None = None) -> Command[str]:
        return self._call(TextArgs(selector=selector), timeout)

    def html(self, selector: str | None = None, timeout: float | None = None) -> Command[str]:
        return self._call(HtmlArgs(selector=selector), timeout)

    def attr(self, selector: str, name: str, timeout: float | None = None) -> Command[str | None]:
        return self._call(AttrArgs(selector=selector, name=name), timeout)

    def value(self, selector: str, timeout: float | None = None) -> Command[str]:
        return self._call(ValueArgs(selector=selector), timeout)

    def url(self, timeout: float | None = None) -> Command[str]:
        return self._call(UrlArgs(), timeout)

    def title(self, timeout: float | None = None) -> Command[str]:
        return self._call(TitleArgs(), timeout)

    def evaluate(self, expression: str, timeout: float | None = None) -> Command[Any]:
        return self._call(EvalArgs(expression=expression), timeout)

    def screenshot(self, timeout: float | None = None) -> Command[str]:
        return self._call(ScreenshotArgs(), timeout)

    async def screenshot_bytes(self, timeout: float | None = None) -> bytes:
        return base64.b64decode(await self.screenshot(timeout))

    def wait_for(
        self,
        selector: str,
        state: ElementState = ElementState.VISIBLE,
        timeout: float | None = None,
    ) -> Command[None]:
        return self._call(WaitForArgs(selector=selector, state=state), timeout)

    def wait_for_hidden(self, selector: str, timeout: float | None = None) -> Command[None]:
        return self._call(WaitForHiddenArgs(selector=selector), timeout)

    def wait_for_url(self, pattern: str, timeout: float | None = None) -> Command[None]:
        return self._call(WaitForUrlArgs(pattern=pattern), timeout)

    def wait_for_load(
        self, wait_until: WaitUntil = WaitUntil.LOAD, timeout: float | None = None
    ) -> Command[None]:
        return self._call(WaitForLoadArgs(wait_until=wait_until), timeout)

    def wait_for_function(self, expression: str, timeout: float | None = None) -> Command[None]:
        return self._call(WaitForFunctionArgs(expression=expression), timeout)

    def subscribe(self, topics: list[Topic], timeout: float | None = None) -> Command[None]:
        return self._call(SubscribeArgs(topics=topics), timeout)

    async def download(self, name: str, target: Path | str) -> Path:
        return await self._http.download(self.id, name, Path(target))
