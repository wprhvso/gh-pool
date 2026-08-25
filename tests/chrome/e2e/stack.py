import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from inspect import isawaitable
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import pytest
import uvicorn
from starlette.types import ASGIApp

import gh_pool.client
from gh_pool.browser.config import settings as runner_settings
from gh_pool.browser.http import ServerClient
from gh_pool.client import Session
from gh_pool.core.config import settings as server_settings
from gh_pool.protocol import (
    CommandEnvelope,
    CommandError,
    ErrorCode,
    Event,
    EventData,
    EventType,
    Expression,
    Method,
    RunnerConfig,
    Upload,
)
from gh_pool.protocol.sse import parse_sse
from gh_pool.relay.app import create_app as create_relay
from gh_pool.server import dispatch
from gh_pool.server.app import create_app as create_server
from tests.chrome.e2e.gateway import Gateway

log = logging.getLogger(__name__)

TOKEN = "an-end-to-end-secret"
POLL = 0.02
HEARTBEAT_INTERVAL = 2

CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
DESKTOP_TOOLS = ("Xvfb", "xdpyinfo", "openbox")
CHATTER = ("httpcore", "httpx:", "websockets.client", "asyncio:")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


_HANDED_OUT: set[int] = set()


def free_display() -> int:
    for number in range(80, 250):
        lock = Path(f"/tmp/.X{number}-lock")
        socket_path = Path(f"/tmp/.X11-unix/X{number}")
        if number in _HANDED_OUT or lock.exists() or socket_path.exists():
            continue
        _HANDED_OUT.add(number)
        return number
    raise RuntimeError("every X display number is taken")


def chrome_binary() -> str | None:
    chosen = os.environ.get("GH_POOL_TEST_CHROME")
    if chosen:
        return chosen if Path(chosen).exists() else None
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def missing_desktop_tool() -> str | None:
    for name in DESKTOP_TOOLS:
        if shutil.which(name) is None:
            return name
    return None if chrome_binary() is not None else "chrome"


async def until(
    predicate: Callable[[], bool], timeout: float, what: str = "the condition"
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"{what} did not hold within {timeout}s")
        await asyncio.sleep(POLL)


class Background:
    def __init__(self, app: ASGIApp, port: int = 0) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                access_log=False,
                timeout_graceful_shutdown=2,
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self, timeout: float = 60.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("the background server never started")
            time.sleep(POLL)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=60)

    @property
    def port(self) -> int:
        return int(self._server.servers[0].sockets[0].getsockname()[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _behind_gateway() -> ASGIApp:
    return Gateway(create_server(), create_relay())


class Server:
    def __init__(self, database_url: str, storage: Path) -> None:
        self.dispatched: list[UUID] = []
        self.runner_tokens: dict[UUID, str] = {}
        self.launcher: Callable[[UUID], None] | None = None
        self.dispatch_error: str | None = None
        self._database_url = database_url
        self._storage = storage
        self._patch = pytest.MonkeyPatch()
        self._background: Background | None = None

    def start(
        self,
        *,
        heartbeat_timeout: float = 30.0,
        ready_timeout: float = 600.0,
        watchdog_interval: float = 0.2,
        segment_seconds: float = 1.0,
        max_upload: int = 1 << 30,
    ) -> None:
        self._patch.setattr(server_settings, "token", TOKEN)
        self._patch.setattr(server_settings, "database_url", self._database_url)
        self._patch.setattr(server_settings, "storage", self._storage)
        self._patch.setattr(server_settings, "data_dir", self._storage / "tasks")
        self._patch.setattr(server_settings, "blob_dir", self._storage / "blobs")
        self._patch.setattr(server_settings, "heartbeat_timeout", heartbeat_timeout)
        self._patch.setattr(server_settings, "ready_timeout", ready_timeout)
        self._patch.setattr(server_settings, "watchdog_interval", watchdog_interval)
        self._patch.setattr(server_settings, "segment_seconds", segment_seconds)
        self._patch.setattr(server_settings, "max_upload", max_upload)
        self._patch.setattr(dispatch, "dispatch", self._dispatch)
        try:
            self._background = Background(_behind_gateway())
            self._background.start()
        except BaseException:
            self._background = None
            self._patch.undo()
            raise
        self._patch.setattr(server_settings, "public_url", self.url)

    def restart(self) -> None:
        if self._background is None:
            raise RuntimeError("the server is not running")
        port = self._background.port
        self._background.stop()
        self._background = Background(_behind_gateway(), port=port)
        self._background.start()

    def stop(self) -> None:
        if self._background is not None:
            self._background.stop()
            self._background = None
        self._patch.undo()

    @property
    def url(self) -> str:
        if self._background is None:
            raise RuntimeError("the server is not running")
        return self._background.url

    @property
    def storage(self) -> Path:
        return self._storage

    @property
    def max_upload(self) -> int:
        return server_settings.max_upload

    async def _dispatch(self, session_id: UUID, runner_token: str) -> None:
        self.dispatched.append(session_id)
        self.runner_tokens[session_id] = runner_token
        runner_settings.token = runner_token
        if self.dispatch_error is not None:
            raise dispatch.DispatchError(self.dispatch_error)
        if self.launcher is not None:
            await asyncio.to_thread(self.launcher, session_id)


def expression_of(envelope: CommandEnvelope) -> str:
    args = envelope.args
    assert isinstance(args, Expression)
    return args.expression


def file_id_of(envelope: CommandEnvelope) -> str:
    args = envelope.args
    assert isinstance(args, Upload)
    assert args.file_id is not None
    return str(args.file_id)


class Rejected(Exception):
    def __init__(self, error: CommandError) -> None:
        super().__init__(error.message)
        self.error = error


type Handler = Callable[[CommandEnvelope], Awaitable[Any] | Any]


class ScriptedRunner:
    def __init__(
        self,
        session_id: UUID,
        *,
        heartbeat: float | None = 2.0,
        confirm_close: bool = True,
    ) -> None:
        self.client = ServerClient(session_id)
        self.received: list[CommandEnvelope] = []
        self.cancelled: list[UUID] = []
        self.handler_errors: list[BaseException] = []
        self.config: RunnerConfig | None = None
        self.closed = False
        self._heartbeat = heartbeat
        self._confirm_close = confirm_close
        self._handlers: dict[Method, Handler] = {}
        self._current: asyncio.Task[None] | None = None
        self._current_id: UUID | None = None
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> RunnerConfig:
        self.config = await self.client.config()
        self._tasks.append(asyncio.create_task(self._consume()))
        if self._heartbeat is not None:
            self._tasks.append(asyncio.create_task(self._beat()))
        return self.config

    async def stop(self) -> None:
        if self._current is not None:
            self._current.cancel()
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        await self._settle()
        await self.client.aclose()

    def on(self, method: Method, handler: Handler) -> None:
        self._handlers[method] = handler

    def returns(self, method: Method, value: Any) -> None:
        self.on(method, lambda _envelope: value)

    def raises(self, method: Method, code: ErrorCode, message: str) -> None:
        def handler(_envelope: CommandEnvelope) -> Any:
            raise Rejected(CommandError(code=code, message=message))

        self.on(method, handler)

    def stalls(self, method: Method) -> None:
        async def handler(_envelope: CommandEnvelope) -> Any:
            await asyncio.Event().wait()

        self.on(method, handler)

    async def wait_for_cancel(self, timeout: float = 15.0) -> UUID:
        await until(lambda: bool(self.cancelled), timeout, "a cancel")
        return self.cancelled[-1]

    async def wait_for_close(self, timeout: float = 15.0) -> None:
        await until(lambda: self.closed, timeout, "the close frame")

    async def _consume(self) -> None:
        async with self.client.stream() as chunks:
            async for message in parse_sse(chunks):
                if message.event == "close":
                    self.closed = True
                    if self._current is not None:
                        self._current.cancel()
                    await self._settle()
                    if self._confirm_close:
                        await self.client.confirm_close()
                    return
                if message.event == "cancel":
                    identifier = UUID(json.loads(message.data)["command_id"])
                    self.cancelled.append(identifier)
                    if self._current is not None and self._current_id == identifier:
                        self._current.cancel()
                elif message.event == "command":
                    envelope = CommandEnvelope.model_validate_json(message.data)
                    await self._settle()
                    self.received.append(envelope)
                    self._current_id = envelope.command_id
                    self._current = asyncio.create_task(self._execute(envelope))

    async def _settle(self) -> None:
        if self._current is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await self._current
        self._current = None
        self._current_id = None

    async def _execute(self, envelope: CommandEnvelope) -> None:
        result: Any = None
        error: CommandError | None = None
        handler = self._handlers.get(envelope.args.method)
        try:
            if handler is not None:
                outcome = handler(envelope)
                result = await outcome if isawaitable(outcome) else outcome
        except asyncio.CancelledError:
            error = CommandError(code=ErrorCode.CANCELLED, message="cancelled")
        except Rejected as rejected:
            error = rejected.error
        except Exception as exc:
            self.handler_errors.append(exc)
            log.exception("a scripted handler for %s failed", envelope.args.method)
            error = CommandError(
                code=ErrorCode.RUNNER_ERROR,
                message=f"the scripted handler raised {exc!r}",
            )
        with contextlib.suppress(Exception):
            await self.client.complete(envelope.command_id, result, error)

    async def _beat(self) -> None:
        interval = self._heartbeat or 0
        while True:
            with contextlib.suppress(Exception):
                await self.client.heartbeat()
            await asyncio.sleep(interval)


async def _nothing() -> AsyncIterator[Event]:
    return
    yield


class Watch:
    def __init__(self, session: Session) -> None:
        self.events: list[Event] = []
        self._session = session
        self._stream: AsyncIterator[Event] = _nothing()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self._stream = self._session.events()
        self._task = asyncio.create_task(self._collect())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def wait_for(self, kind: EventType, timeout: float = 30.0) -> EventData:
        def arrived() -> bool:
            return any(event.data.type is kind for event in self.events)

        await until(arrived, timeout, f"a {kind} event")
        return next(event.data for event in self.events if event.data.type is kind)

    def seen(self, kind: EventType) -> list[EventData]:
        return [event.data for event in self.events if event.data.type is kind]

    async def _collect(self) -> None:
        async for event in self._stream:
            self.events.append(event)


class LiveRunner:
    def __init__(
        self,
        session_id: UUID,
        *,
        server_url: str,
        workdir: Path,
        token: str = TOKEN,
        vnc: bool = False,
        env: dict[str, str] | None = None,
    ) -> None:
        self.display = free_display()
        self.workdir = workdir
        self._id = session_id
        self._log = workdir / "runner.log"
        self._server_url = server_url
        self._token = token
        self._vnc = vnc
        self._extra = env or {}
        self._process: subprocess.Popen[bytes] | None = None
        self._handle: Any = None

    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._handle = self._log.open("wb")
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "gh_pool.browser",
                "--session",
                str(self._id),
                "--verbose",
            ],
            env=self._environment(),
            stdout=self._handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def stop(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            self._signal(signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=60)
            if process.poll() is None:
                self._signal(signal.SIGKILL)
                process.wait(timeout=30)
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _signal(self, number: int) -> None:
        if self._process is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(self._process.pid), number)

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def returncode(self) -> int | None:
        return self._process.poll() if self._process is not None else None

    def tail(self, lines: int = 40) -> str:
        pieces: list[str] = []
        logs = [self._log, *sorted((self.workdir / "logs").glob("*.log"))]
        for path in logs:
            if not path.exists():
                continue
            kept = [
                line
                for line in path.read_text("utf-8", "replace").splitlines()
                if not any(name in line for name in CHATTER)
            ]
            pieces.append(f"--- {path.name}\n" + "\n".join(kept[-lines:]))
        return "\n".join(pieces) or "the runner left no logs"

    def _environment(self) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GH_POOL_") and "proxy" not in key.lower()
        }
        env |= {
            "GH_POOL_SERVER": self._server_url,
            "GH_POOL_TOKEN": self._token,
            "GH_POOL_WORKDIR": str(self.workdir),
            "GH_POOL_DISPLAY": str(self.display),
            "GH_POOL_DEBUG_PORT": str(free_port()),
            "GH_POOL_VNC_PORT": str(free_port()),
            "GH_POOL_VNC": "1" if self._vnc else "0",
            "GH_POOL_HEARTBEAT_INTERVAL": str(HEARTBEAT_INTERVAL),
            "GH_POOL_UPLOAD_ALLOW_PRIVATE": "1",
            "NO_PROXY": "*",
        }
        chrome = chrome_binary()
        if chrome is not None:
            env["GH_POOL_CHROME_BINARY"] = chrome
        return env | self._extra


class Stack:
    def __init__(self, server: Server, workdir: Path) -> None:
        self.server = server
        self.runners: list[LiveRunner] = []
        self._workdir = workdir
        self._sessions: list[Session] = []
        self._scripted: list[ScriptedRunner] = []
        self._patch = pytest.MonkeyPatch()
        self._patch.setattr(runner_settings, "url", server.url)
        self._patch.setattr(runner_settings, "token", TOKEN)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def session(self, *, close_timeout: float = 30.0, **params: Any) -> Session:
        session = await gh_pool.client.new(
            server=self.server.url,
            token=TOKEN,
            close_timeout=close_timeout,
            **params,
        )
        self._sessions.append(session)
        return session

    async def scripted(
        self,
        *,
        heartbeat: float | None = 2.0,
        confirm_close: bool = True,
        **params: Any,
    ) -> tuple[Session, ScriptedRunner]:
        session = await self.session(**params)
        runner = await self.scripted_for(
            session, heartbeat=heartbeat, confirm_close=confirm_close
        )
        return session, runner

    async def scripted_for(
        self,
        session: Session,
        *,
        heartbeat: float | None = 2.0,
        confirm_close: bool = True,
    ) -> ScriptedRunner:
        runner = ScriptedRunner(
            session.id, heartbeat=heartbeat, confirm_close=confirm_close
        )
        self._scripted.append(runner)
        await runner.start()
        await session.ready(timeout=30)
        return runner

    async def live(
        self,
        *,
        vnc: bool = False,
        ready_timeout: float = 180.0,
        runner_env: dict[str, str] | None = None,
        **params: Any,
    ) -> Session:
        def launcher(session_id: UUID) -> None:
            self.launch(session_id, vnc=vnc, env=runner_env)

        self.server.launcher = launcher
        params.setdefault("timeout", 120.0)
        try:
            session = await self.session(close_timeout=20.0, **params)
        finally:
            self.server.launcher = None
        runner = self.runners[-1]
        waiter = asyncio.ensure_future(session.ready(timeout=ready_timeout))
        try:
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=0.2)
                if done:
                    await waiter
                    return session
                if not runner.alive:
                    raise RuntimeError(f"the runner exited with {runner.returncode}")
        except Exception as failure:  # pragma: no cover - only on a broken runner
            waiter.cancel()
            pytest.fail(f"the runner never got ready: {failure}\n{runner.tail()}")

    def launch(
        self,
        session_id: UUID,
        *,
        vnc: bool = False,
        env: dict[str, str] | None = None,
    ) -> LiveRunner:
        runner = LiveRunner(
            session_id,
            server_url=self.server.url,
            workdir=self._workdir / f"runner-{len(self.runners)}",
            token=self.server.runner_tokens.get(session_id, TOKEN),
            vnc=vnc,
            env=env,
        )
        runner.start()
        self.runners.append(runner)
        return runner

    async def aclose(self) -> None:
        for session in self._sessions:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(25):
                    await session.close()
        for scripted in self._scripted:
            with contextlib.suppress(Exception):
                await scripted.stop()
        for runner in self.runners:
            with contextlib.suppress(Exception):
                runner.stop()
        self._patch.undo()
