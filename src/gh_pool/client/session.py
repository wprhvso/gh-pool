import asyncio
import base64
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Generator
from contextlib import suppress
from contextvars import Context
from inspect import isawaitable
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID

from pydantic import ValidationError

from gh_pool.client.errors import (
    GhChromeError,
    Rejected,
    SessionDead,
    SessionNotReady,
    to_exception,
)
from gh_pool.client.http import Http
from gh_pool.protocol import (
    Attr,
    Bare,
    CommandArgs,
    CommandFailed,
    CommandFinished,
    ElementState,
    Event,
    EventType,
    Expression,
    Goto,
    Hotkey,
    Html,
    Index,
    InitScript,
    Method,
    NewTab,
    Press,
    ScrollBy,
    SelectOption,
    Selector,
    SessionClosed,
    SessionParams,
    SessionReady,
    SessionState,
    SessionStatus,
    Subscribe,
    Topic,
    TypeText,
    Upload,
    WaitFor,
    WaitForLoad,
    WaitForUrl,
    WaitUntil,
)
from gh_pool.protocol.sse import SseMessage, parse_sse

log = logging.getLogger(__name__)

USER_EVENTS = frozenset(
    {
        EventType.TAB_OPENED,
        EventType.TAB_CLOSED,
        EventType.TAB_ACTIVATED,
        EventType.DOWNLOAD,
        EventType.SESSION_CLOSED,
    }
)

MIN_BACKOFF = 0.5
MAX_BACKOFF = 8.0


class Command[T]:
    __slots__ = ("_future", "_task")

    def __init__(self) -> None:
        self._future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._task: asyncio.Task[None] | None = None

    def done(self) -> bool:
        return self._future.done()

    async def wait(self, timeout: float | None = None) -> T:
        if timeout is None:
            return await asyncio.shield(self._future)
        async with asyncio.timeout(timeout):
            return await asyncio.shield(self._future)

    def __await__(self) -> Generator[Any, None, T]:
        return self.wait().__await__()

    def _resolve(self, result: Any) -> None:
        if not self._future.done():
            self._future.set_result(result)

    def _fail(self, error: BaseException) -> None:
        if not self._future.done():
            self._future.set_exception(error)


def _seq_of(message: SseMessage, fallback: int) -> int:
    if message.id is None:
        return fallback
    try:
        return int(message.id)
    except ValueError:
        return fallback


async def _wait_any(*events: asyncio.Event, timeout: float) -> bool:
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        done, _ = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        return bool(done)
    finally:
        for waiter in waiters:
            waiter.cancel()


class Session:
    def __init__(self, http: Http, state: SessionState, close_timeout: float) -> None:
        self._http = http
        self._state = state
        self._close_timeout = close_timeout
        self._pending: dict[UUID, Command[Any]] = {}
        self._stash: dict[UUID, CommandFinished | CommandFailed] = {}
        self._subscribers: set[asyncio.Queue[Event | None]] = set()
        self._ready = asyncio.Event()
        self._finished = asyncio.Event()
        self._closed = False
        self._detached = False
        self._last_seq = 0
        self._reader = asyncio.create_task(self._read_events(), context=Context())

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
    def alive(self) -> bool:
        return not self._closed and not self._finished.is_set()

    @property
    def player_url(self) -> str:
        return f"{self._http.base_url}/s/{self.id}"

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
        settled = await _wait_any(self._ready, self._finished, timeout=timeout)
        if self._finished.is_set() and not self._ready.is_set():
            raise SessionDead("session finished before the runner connected")
        if not settled:
            raise SessionNotReady(f"runner did not connect in {timeout}s")

    async def close(self) -> None:
        if self._closed or self._detached:
            return
        self._closed = True
        try:
            await self._http.close_session(self.id)
            if self._close_timeout > 0:
                with suppress(TimeoutError):
                    async with asyncio.timeout(self._close_timeout):
                        await self._finished.wait()
        finally:
            await self._let_go(SessionDead("session closed"))

    async def detach(self) -> None:
        await self._let_go(SessionDead("session detached"))

    async def _let_go(self, error: BaseException) -> None:
        if self._detached:
            return
        self._detached = True
        self._reader.cancel()
        with suppress(asyncio.CancelledError):
            await self._reader
        self._fail_pending(error)
        for queue in tuple(self._subscribers):
            queue.put_nowait(None)
        await self._http.aclose()

    def events(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        if self._over:
            queue.put_nowait(None)
        else:
            self._subscribers.add(queue)
        return self._drain(queue)

    async def _drain(self, queue: asyncio.Queue[Event | None]) -> AsyncIterator[Event]:
        try:
            while (event := await queue.get()) is not None:
                yield event
                if event.data.type is EventType.SESSION_CLOSED:
                    return
        finally:
            self._subscribers.discard(queue)

    async def _read_events(self) -> None:
        backoff = MIN_BACKOFF
        while True:
            try:
                async with self._http.events(self.id, self._last_seq) as chunks:
                    backoff = MIN_BACKOFF
                    async for message in parse_sse(chunks):
                        self._take(message)
            except asyncio.CancelledError:
                raise
            except Rejected as refused:
                log.warning("the event stream is closed to us: %s", refused)
                self._end(SessionDead(str(refused)))
                return
            except Exception as exc:
                log.debug("event stream dropped: %s", exc)
                await asyncio.sleep(backoff * random.uniform(0.5, 1.5))
                backoff = min(backoff * 2, MAX_BACKOFF)
            else:
                if self._finished.is_set() or await self._is_over():
                    return
                await asyncio.sleep(backoff * random.uniform(0.5, 1.5))
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def _is_over(self) -> bool:
        try:
            state = await self._http.get_session(self.id)
        except Rejected:
            self._end(SessionDead("the session is gone"))
            return True
        except Exception as exc:
            log.debug("could not ask what became of the session: %s", exc)
            return False
        if state.status.live:
            return False
        self._end(SessionDead(f"session {state.status}"))
        return True

    def _end(self, error: BaseException) -> None:
        self._finished.set()
        self._fail_pending(error)
        for queue in tuple(self._subscribers):
            queue.put_nowait(None)

    def _take(self, message: SseMessage) -> None:
        try:
            event = Event.model_validate_json(message.data)
        except ValidationError:
            log.warning("skipping a %s event this client cannot read", message.event)
            self._last_seq = _seq_of(message, self._last_seq)
            return
        self._last_seq = event.seq
        self._on_event(event)

    def _on_event(self, event: Event) -> None:
        data = event.data
        if isinstance(data, SessionReady):
            self._state = self._state.model_copy(
                update={"state_stale": data.state_stale, "status": SessionStatus.ACTIVE}
            )
            self._ready.set()
        elif isinstance(data, SessionClosed):
            self._finished.set()
            self._fail_pending(SessionDead(f"session {data.reason}"))
        elif isinstance(data, CommandFinished | CommandFailed):
            command = self._pending.pop(data.command_id, None)
            if command is None:
                self._stash[data.command_id] = data
            else:
                _settle(command, data)
        if data.type in USER_EVENTS:
            for queue in tuple(self._subscribers):
                queue.put_nowait(event)

    def _fail_pending(self, error: BaseException) -> None:
        for command in tuple(self._pending.values()):
            command._fail(error)
        self._pending.clear()

    def _call[T](
        self, args: CommandArgs | Awaitable[CommandArgs], timeout: float | None = None
    ) -> Command[T]:
        command: Command[T] = Command()
        command._task = asyncio.create_task(self._submit(command, args, timeout))
        return command

    async def _submit(
        self,
        command: Command[Any],
        args: CommandArgs | Awaitable[CommandArgs],
        timeout: float | None,
    ) -> None:
        try:
            if isawaitable(args):
                args = await args
            if self._over:
                raise SessionDead("session is closed")
            accepted = await self._http.enqueue(self.id, args, timeout)
        except Exception as exc:
            command._fail(
                exc if isinstance(exc, GhChromeError) else GhChromeError(str(exc))
            )
            return
        stashed = self._stash.pop(accepted.command_id, None)
        if stashed is not None:
            _settle(command, stashed)
        elif self._over:
            command._fail(SessionDead("session is closed"))
        else:
            self._pending[accepted.command_id] = command

    @property
    def _over(self) -> bool:
        return self._closed or self._detached or self._finished.is_set()

    def goto(
        self,
        url: str,
        wait_until: WaitUntil = WaitUntil.LOAD,
        timeout: float | None = None,
    ) -> Command[None]:
        return self._call(Goto(url=url, wait_until=wait_until), timeout)

    def back(self, timeout: float | None = None) -> Command[None]:
        return self._call(Bare(method=Method.BACK), timeout)

    def forward(self, timeout: float | None = None) -> Command[None]:
        return self._call(Bare(method=Method.FORWARD), timeout)

    def reload(self, timeout: float | None = None) -> Command[None]:
        return self._call(Bare(method=Method.RELOAD), timeout)

    def new_tab(
        self, url: str | None = None, timeout: float | None = None
    ) -> Command[int]:
        return self._call(NewTab(url=url), timeout)

    def activate(self, index: int, timeout: float | None = None) -> Command[None]:
        return self._call(Index(method=Method.ACTIVATE, index=index), timeout)

    def close_tab(self, index: int, timeout: float | None = None) -> Command[None]:
        return self._call(Index(method=Method.CLOSE_TAB, index=index), timeout)

    def tabs(self, timeout: float | None = None) -> Command[list[dict[str, Any]]]:
        return self._call(Bare(method=Method.TABS), timeout)

    def click(self, selector: str, timeout: float | None = None) -> Command[None]:
        return self._call(Selector(method=Method.CLICK, selector=selector), timeout)

    def dblclick(self, selector: str, timeout: float | None = None) -> Command[None]:
        return self._call(Selector(method=Method.DBLCLICK, selector=selector), timeout)

    def right_click(self, selector: str, timeout: float | None = None) -> Command[None]:
        return self._call(
            Selector(method=Method.RIGHT_CLICK, selector=selector), timeout
        )

    def hover(self, selector: str, timeout: float | None = None) -> Command[None]:
        return self._call(Selector(method=Method.HOVER, selector=selector), timeout)

    def type(
        self,
        selector: str,
        text: str,
        clear: bool = False,
        timeout: float | None = None,
    ) -> Command[None]:
        return self._call(TypeText(selector=selector, text=text, clear=clear), timeout)

    def press(self, key: str, timeout: float | None = None) -> Command[None]:
        return self._call(Press(key=key), timeout)

    def hotkey(self, *keys: str, timeout: float | None = None) -> Command[None]:
        return self._call(Hotkey(keys=list(keys)), timeout)

    def select(
        self, selector: str, value: str, timeout: float | None = None
    ) -> Command[None]:
        return self._call(SelectOption(selector=selector, value=value), timeout)

    def scroll_to(self, selector: str, timeout: float | None = None) -> Command[None]:
        return self._call(Selector(method=Method.SCROLL_TO, selector=selector), timeout)

    def scroll_by(self, dy: int, timeout: float | None = None) -> Command[None]:
        return self._call(ScrollBy(dy=dy), timeout)

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
            return self._call(Upload(selector=selector, url=url), timeout)
        return self._call(
            self._upload_args(selector, Path(cast("Path | str", path))), timeout
        )

    async def _upload_args(self, selector: str, path: Path) -> CommandArgs:
        file_id = await self._http.upload_file(self.id, path)
        return Upload(selector=selector, file_id=file_id)

    def text(self, selector: str, timeout: float | None = None) -> Command[str]:
        return self._call(Selector(method=Method.TEXT, selector=selector), timeout)

    def html(
        self, selector: str | None = None, timeout: float | None = None
    ) -> Command[str]:
        return self._call(Html(selector=selector), timeout)

    def attr(
        self, selector: str, name: str, timeout: float | None = None
    ) -> Command[str | None]:
        return self._call(Attr(selector=selector, name=name), timeout)

    def value(self, selector: str, timeout: float | None = None) -> Command[str]:
        return self._call(Selector(method=Method.VALUE, selector=selector), timeout)

    def url(self, timeout: float | None = None) -> Command[str]:
        return self._call(Bare(method=Method.URL), timeout)

    def title(self, timeout: float | None = None) -> Command[str]:
        return self._call(Bare(method=Method.TITLE), timeout)

    def evaluate(self, expression: str, timeout: float | None = None) -> Command[Any]:
        return self._call(
            Expression(method=Method.EVAL, expression=expression), timeout
        )

    def init_script(self, source: str, timeout: float | None = None) -> Command[str]:
        return self._call(InitScript(source=source), timeout)

    def screenshot(self, timeout: float | None = None) -> Command[str]:
        return self._call(Bare(method=Method.SCREENSHOT), timeout)

    async def screenshot_bytes(self, timeout: float | None = None) -> bytes:
        return base64.b64decode(await self.screenshot(timeout))

    def wait_for(
        self,
        selector: str,
        state: ElementState = ElementState.VISIBLE,
        timeout: float | None = None,
    ) -> Command[None]:
        return self._call(WaitFor(selector=selector, state=state), timeout)

    def wait_for_hidden(
        self, selector: str, timeout: float | None = None
    ) -> Command[None]:
        return self._call(
            Selector(method=Method.WAIT_FOR_HIDDEN, selector=selector), timeout
        )

    def wait_for_url(self, pattern: str, timeout: float | None = None) -> Command[None]:
        return self._call(WaitForUrl(pattern=pattern), timeout)

    def wait_for_load(
        self, wait_until: WaitUntil = WaitUntil.LOAD, timeout: float | None = None
    ) -> Command[None]:
        return self._call(WaitForLoad(wait_until=wait_until), timeout)

    def wait_for_function(
        self, expression: str, timeout: float | None = None
    ) -> Command[None]:
        return self._call(
            Expression(method=Method.WAIT_FOR_FUNCTION, expression=expression), timeout
        )

    def subscribe(
        self, topics: list[Topic], timeout: float | None = None
    ) -> Command[None]:
        return self._call(Subscribe(topics=topics), timeout)

    async def download(self, name: str, target: Path | str) -> Path:
        return await self._http.download(self.id, name, Path(target))


def _settle(command: Command[Any], data: CommandFinished | CommandFailed) -> None:
    if isinstance(data, CommandFinished):
        command._resolve(data.result)
    else:
        command._fail(to_exception(data.error))
