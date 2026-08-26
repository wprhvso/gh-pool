import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Protocol
from uuid import UUID

from gh_pool.browser import profile
from gh_pool.browser.actions import Actions
from gh_pool.browser.browser import Browser
from gh_pool.browser.capture import Capture
from gh_pool.browser.config import settings
from gh_pool.browser.display import Display
from gh_pool.browser.http import ServerClient
from gh_pool.browser.tunnel import Tunnel
from gh_pool.browser.xtest import Xtest
from gh_pool.protocol import CommandEnvelope, CommandError, ErrorCode, RunnerConfig
from gh_pool.protocol.sse import parse_sse
from gh_pool.protocol.trace import TraceContext, bound

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
        told = False
        beat: asyncio.Task[None] | None = None
        try:
            async with AsyncExitStack() as stack:
                enter = stack.enter_async_context
                display = await enter(
                    running(Display(config.params.width, config.params.height))
                )
                browser = await enter(running(Browser(display, config.params)))
                await enter(running(Capture(display, self._server, config)))
                if display.vnc_port is not None:
                    await enter(running(Tunnel(self._id, display.vnc_port)))
                xtest = await asyncio.to_thread(Xtest, display.name)
                actions = await enter(
                    running(Actions(browser.cdp, xtest, self._server, config.params))
                )

                log.info("runner is ready for session %s", self._id)
                consume = asyncio.create_task(
                    self._consume(actions, lambda: display.alive() and browser.alive())
                )
                beat = asyncio.create_task(self._beat(consume))
                try:
                    with contextlib.suppress(asyncio.CancelledError):
                        told = await consume
                    code = 0
                except Exception:
                    log.exception("runner loop failed")
                finally:
                    await self._await_current()
            if code == 0:
                await self._save(config, told)
        finally:
            if beat is not None:
                beat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await beat
        return code

    async def _consume(self, actions: Actions, healthy: Callable[[], bool]) -> bool:
        async with self._server.stream() as chunks:
            async for message in parse_sse(chunks):
                if message.event == "close":
                    log.info("close requested")
                    if self._current is not None:
                        self._current.cancel()
                    return True
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
        log.warning("the command stream ended without a close")
        return False

    async def _execute(self, actions: Actions, envelope: CommandEnvelope) -> None:
        trace = TraceContext.parse(envelope.traceparent, envelope.tracestate)
        with bound(trace):
            await self._run(actions, envelope)

    async def _run(self, actions: Actions, envelope: CommandEnvelope) -> None:
        result: Any = None
        error: CommandError | None = None
        log.debug("command %s started", envelope.args.method)
        try:
            async with asyncio.timeout(envelope.timeout_ms / 1000):
                result = await actions.dispatch(envelope.args)
        except asyncio.CancelledError:
            error = CommandError(code=ErrorCode.CANCELLED, message="cancelled")
        except Exception as exc:
            error = actions.to_error(exc)
        if error is not None:
            log.info("command %s failed: %s", envelope.args.method, error.code)
        try:
            await self._server.complete(envelope.command_id, result, error)
        except Exception:
            log.warning(
                "could not hand back the result of %s",
                envelope.args.method,
                exc_info=True,
            )

    async def _await_current(self) -> None:
        if self._current is None:
            return
        done, _ = await asyncio.wait([self._current])
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.warning("the command task failed", exc_info=task.exception())
        self._current = None
        self._current_id = None

    def _cancel(self, command_id: UUID) -> None:
        if self._current is not None and self._current_id == command_id:
            self._current.cancel()

    async def _beat(self, consume: asyncio.Task[bool]) -> None:
        while True:
            try:
                alive = await self._server.heartbeat()
            except Exception:
                alive = True
            if not alive:
                log.warning("the server has finished with this session")
                consume.cancel()
                return
            await asyncio.sleep(settings.heartbeat_interval)

    async def _save(self, config: RunnerConfig, told: bool) -> None:
        if config.persist and config.profile:
            try:
                await profile.store(self._server)
            except Exception:
                # Suppressed, this is the failure that makes the next session
                # start signed out with nothing anywhere saying why.
                log.warning(
                    "could not store the profile for %s", config.profile, exc_info=True
                )
        if told:
            try:
                await self._server.confirm_close()
            except Exception:
                log.warning("could not confirm the close", exc_info=True)
