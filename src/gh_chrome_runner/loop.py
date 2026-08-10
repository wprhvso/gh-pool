import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Protocol
from uuid import UUID

from gh_chrome_protocol import CommandEnvelope, CommandError, ErrorCode, RunnerConfig
from gh_chrome_protocol.sse import parse_sse
from gh_chrome_runner import profile
from gh_chrome_runner.actions import Actions
from gh_chrome_runner.browser import Browser
from gh_chrome_runner.capture import Capture
from gh_chrome_runner.config import settings
from gh_chrome_runner.display import Display
from gh_chrome_runner.http import ServerClient
from gh_chrome_runner.xtest import Xtest

log = logging.getLogger(__name__)


class Component(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


@asynccontextmanager
async def running[T: Component](component: T) -> AsyncGenerator[T]:
    try:
        await component.start()
        yield component
    finally:
        await component.stop()


class Runner:
    def __init__(self, session_id: UUID) -> None:
        self._id = session_id
        self._server = ServerClient(session_id)
        self._current: asyncio.Task[None] | None = None
        self._current_id: UUID | None = None

    async def run(self) -> int:
        code = 1
        try:
            config = await self._server.config()
            settings.workdir.mkdir(parents=True, exist_ok=True)
            if config.has_profile_archive:
                await profile.restore(self._server)
            code = await self._serve(config)
        except Exception:
            log.exception("runner failed")
        finally:
            await self._server.aclose()
        return code

    async def _serve(self, config: RunnerConfig) -> int:
        code = 1
        async with AsyncExitStack() as stack:
            enter = stack.enter_async_context
            display = await enter(
                running(Display(config.params.width, config.params.height))
            )
            browser = await enter(running(Browser(display, config.params)))
            await enter(running(Capture(display, self._server, config)))
            xtest = await asyncio.to_thread(Xtest, display.name)
            actions = await enter(
                running(Actions(browser.cdp, xtest, self._server, config.params))
            )

            log.info("runner is ready for session %s", self._id)
            beat = asyncio.create_task(self._beat())
            try:
                await self._consume(
                    actions, lambda: display.alive() and browser.alive()
                )
                code = 0
            except Exception:
                log.exception("runner loop failed")
            finally:
                beat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await beat
                await self._await_current()
        if code == 0:
            await self._save(config)
        return code

    async def _consume(self, actions: Actions, healthy: Callable[[], bool]) -> None:
        async with self._server.stream() as chunks:
            async for message in parse_sse(chunks):
                if message.event == "close":
                    log.info("close requested")
                    return
                if message.event == "cancel":
                    self._cancel(UUID(json.loads(message.data)["command_id"]))
                elif message.event == "command":
                    envelope = CommandEnvelope.model_validate_json(message.data)
                    await self._await_current()
                    if not healthy():
                        raise RuntimeError("browser or display died")
                    self._current_id = envelope.command_id
                    self._current = asyncio.create_task(
                        self._execute(actions, envelope)
                    )

    async def _execute(self, actions: Actions, envelope: CommandEnvelope) -> None:
        result: Any = None
        error: CommandError | None = None
        try:
            result = await actions.dispatch(envelope.args)
        except asyncio.CancelledError:
            error = CommandError(code=ErrorCode.CANCELLED, message="cancelled")
        except Exception as exc:
            error = actions.to_error(exc)
        with contextlib.suppress(Exception):
            await self._server.complete(envelope.command_id, result, error)

    async def _await_current(self) -> None:
        if self._current is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await self._current
        self._current = None
        self._current_id = None

    def _cancel(self, command_id: UUID) -> None:
        if self._current is not None and self._current_id == command_id:
            self._current.cancel()

    async def _beat(self) -> None:
        while True:
            try:
                alive = await self._server.heartbeat()
            except Exception:
                alive = True
            if not alive:
                log.warning("server considers the session finished")
                return
            await asyncio.sleep(settings.heartbeat_interval)

    async def _save(self, config: RunnerConfig) -> None:
        if config.persist and config.profile:
            with contextlib.suppress(Exception):
                await profile.store(self._server)
        with contextlib.suppress(Exception):
            await self._server.confirm_close()
