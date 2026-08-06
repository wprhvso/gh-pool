from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
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

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, session_id: UUID) -> None:
        self._id = session_id
        self._server = ServerClient(session_id)
        self._display: Display | None = None
        self._browser: Browser | None = None
        self._capture: Capture | None = None
        self._actions: Actions | None = None
        self._config: RunnerConfig | None = None
        self._current: asyncio.Task[None] | None = None
        self._current_id: UUID | None = None
        self._stop = asyncio.Event()

    async def run(self) -> int:
        try:
            await self._setup()
        except Exception:
            log.exception("runner failed to start")
            await self._teardown(confirm=False)
            return 1
        beat = asyncio.create_task(self._beat())
        code = 0
        try:
            await self._consume()
        except Exception:
            log.exception("runner loop failed")
            code = 1
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        await self._teardown(confirm=code == 0)
        return code

    async def _setup(self) -> None:
        settings.workdir.mkdir(parents=True, exist_ok=True)
        config = await self._server.config()
        self._config = config
        if config.has_profile_archive:
            await profile.restore(self._server)
        self._display = Display(config.params.width, config.params.height)
        await self._display.start()
        self._browser = Browser(self._display, config.params)
        await self._browser.start()
        if self._browser.cdp is None:
            raise RuntimeError("chrome is not connected")
        self._actions = Actions(self._browser.cdp, self._display, self._server, config.params)
        await self._actions.start()
        self._capture = Capture(self._display, self._server, config)
        await self._capture.start()
        log.info("runner is ready for session %s", self._id)

    async def _consume(self) -> None:
        async with self._server.stream() as chunks:
            async for message in parse_sse(chunks):
                if message.event == "close":
                    log.info("close requested")
                    return
                if message.event == "cancel":
                    self._cancel(UUID(json.loads(message.data)["command_id"]))
                    continue
                if message.event != "command":
                    continue
                envelope = CommandEnvelope.model_validate_json(message.data)
                await self._await_current()
                if not self._healthy():
                    raise RuntimeError("browser or display died")
                self._current_id = envelope.command_id
                self._current = asyncio.create_task(self._execute(envelope))

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

    async def _execute(self, envelope: CommandEnvelope) -> None:
        if self._actions is None:
            return
        result: Any = None
        error: CommandError | None = None
        try:
            result = await self._actions.dispatch(envelope.args)
        except asyncio.CancelledError:
            error = CommandError(code=ErrorCode.CANCELLED, message="cancelled")
        except Exception as exc:
            error = self._actions.to_error(exc)
        with contextlib.suppress(Exception):
            await self._server.complete(envelope.command_id, result, error)

    def _healthy(self) -> bool:
        return (
            self._display is not None
            and self._display.alive()
            and self._browser is not None
            and self._browser.alive()
        )

    async def _beat(self) -> None:
        while not self._stop.is_set():
            try:
                alive = await self._server.heartbeat()
            except Exception:
                alive = True
            if not alive:
                log.warning("server considers the session finished")
                return
            await asyncio.sleep(settings.heartbeat_interval)

    async def _teardown(self, confirm: bool) -> None:
        self._stop.set()
        await self._await_current()
        if self._actions is not None:
            await self._actions.stop()
        if self._capture is not None:
            await self._capture.stop()
        if self._browser is not None:
            await self._browser.stop()
        if self._display is not None:
            await self._display.stop()
        if confirm and self._config is not None and self._config.persist and self._config.profile:
            with contextlib.suppress(Exception):
                await profile.store(self._server)
        if confirm:
            with contextlib.suppress(Exception):
                await self._server.confirm_close()
        await self._server.aclose()
